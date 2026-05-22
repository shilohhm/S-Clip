"""Concrete FFmpeg-backed capture engine.

The GUI talks to the small :class:`sclip.contracts.CaptureEngine` protocol.
This module is the production implementation behind it: it turns the current
settings into an FFmpeg command line, owns the manual-recording process, and
delegates rolling replay capture to :class:`RollingBuffer`.

Capture is attempted with the GPU ``ddagrab`` backend first. If Desktop
Duplication will not start — an RDP session, say, or an unusual display
driver — the engine quietly retries with the legacy ``gdigrab`` backend so
the user still gets a recording, just a less smooth one.

Desktop audio is captured through :class:`DesktopAudioPump`, which streams the
system sound into FFmpeg over a named pipe. The pump is started before each
FFmpeg process so the pipe exists when FFmpeg opens it, and stopped once the
capture ends.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path

from sclip.contracts import CaptureState, DeviceRegistry, Monitor, Settings, SettingsStore
from sclip.core.desktop_audio import DesktopAudioPump, DesktopAudioStream
from sclip.core.ffmpeg import (
    AudioConfig,
    CapturePlan,
    VideoBackend,
    build_capture_io,
    read_stderr_tail,
    spawn_ffmpeg,
    stop_ffmpeg,
)
from sclip.core.replay_buffer import SEGMENT_SECONDS, BufferSpec, RollingBuffer
from sclip.paths import app_paths

logger = logging.getLogger(__name__)


StateCallback = Callable[[CaptureState], None]
ClipCallback = Callable[[Path], None]
ErrorCallback = Callable[[str], None]

# Keyframe spacing for a manual recording. The replay buffer uses the segment
# length instead; a plain recording just wants keyframes often enough to seek
# comfortably without bloating the file.
_MANUAL_KEYFRAME_SECONDS = 2

# Backends to try, in order. ddagrab is smooth and cheap; gdigrab is the
# universal fallback for the rare machine where Desktop Duplication is off.
_BACKEND_ORDER: tuple[VideoBackend, ...] = (VideoBackend.DDAGRAB, VideoBackend.GDIGRAB)


class FFmpegCaptureEngine:
    """Capture engine used by the desktop application."""

    def __init__(self, settings_store: SettingsStore, device_registry: DeviceRegistry) -> None:
        self._settings_store = settings_store
        self._device_registry = device_registry
        self._lock = threading.RLock()

        self.state: CaptureState = CaptureState.IDLE
        self.on_state_change: StateCallback | None = None
        self.on_clip_saved: ClipCallback | None = None
        self.on_error: ErrorCallback | None = None

        self._manual_process: subprocess.Popen[str] | None = None
        self._manual_context: AbstractContextManager[subprocess.Popen[str]] | None = None
        self._manual_output: Path | None = None

        self._buffer = RollingBuffer(app_paths().replay_buffer_dir)
        self._buffer.set_error_handler(self._handle_error)

        # One pump, reused for every capture. It is started just before an
        # FFmpeg process and stopped once that process ends.
        self._pump = DesktopAudioPump()

    # --- manual recording ----------------------------------------------------

    def start_manual_recording(self) -> None:
        """Start a continuous MP4 recording using the current settings."""
        with self._lock:
            if self.state is CaptureState.RECORDING and self._manual_process is not None:
                logger.debug("Manual recording already running; start is a no-op")
                return
            if self.state is not CaptureState.IDLE:
                raise RuntimeError(f"Cannot start manual recording while {self.state.value}")

            settings = self._settings_store.load()
            destination = self._clip_path("recording", settings)
            self._spawn_manual_with_fallback(settings, destination)
            self._set_state(CaptureState.RECORDING)

    def stop_manual_recording(self) -> Path | None:
        """Stop the manual recording and return the written MP4 path."""
        with self._lock:
            process = self._manual_process
            ctx = self._manual_context
            destination = self._manual_output
            if process is None:
                return None

            self._manual_process = None
            self._manual_context = None
            self._manual_output = None
            self._set_state(CaptureState.SAVING)

        try:
            stop_ffmpeg(process)
        finally:
            if ctx is not None:
                ctx.__exit__(None, None, None)
            # FFmpeg has let go of the audio pipe; the pump can stop now.
            self._stop_desktop_pump()

        if destination is not None and destination.exists() and destination.stat().st_size > 0:
            self._emit_clip_saved(destination)
            self._set_state(CaptureState.IDLE)
            return destination

        self._handle_error("Manual recording stopped, but no playable file was written.")
        return None

    # --- replay buffer -------------------------------------------------------

    def start_replay_buffer(self) -> None:
        """Start the rolling replay buffer using the current settings."""
        with self._lock:
            if self.state is CaptureState.BUFFERING:
                return
            if self.state is not CaptureState.IDLE:
                raise RuntimeError(f"Cannot start replay buffer while {self.state.value}")

            settings = self._settings_store.load()
            self._start_buffer_with_fallback(settings)
            self._set_state(CaptureState.BUFFERING)

    def stop_replay_buffer(self) -> None:
        """Stop the rolling replay buffer."""
        with self._lock:
            self._buffer.stop()
            self._stop_desktop_pump()
            if self.state is CaptureState.BUFFERING:
                self._set_state(CaptureState.IDLE)

    def save_replay_clip(self) -> Path | None:
        """Save the current rolling-buffer window as an MP4.

        The rolling buffer (and its desktop-audio pump) keep running — only a
        short-lived stitch process is spawned — so the user misses nothing
        while the clip is written.
        """
        with self._lock:
            if self.state is not CaptureState.BUFFERING:
                return None
            settings = self._settings_store.load()
            destination = self._clip_path("clip", settings)
            self._set_state(CaptureState.SAVING)

        try:
            saved = self._buffer.save_clip(destination)
        finally:
            with self._lock:
                if self._buffer.is_running:
                    self._set_state(CaptureState.BUFFERING)
                # mypy narrows self.state to BUFFERING after the early return
                # above and cannot see that _set_state() reassigned it.
                elif self.state is CaptureState.SAVING:  # type: ignore[comparison-overlap]
                    self._set_state(CaptureState.IDLE)

        if saved is not None:
            self._emit_clip_saved(saved)
        return saved

    def reload_settings(self) -> None:
        """Restart the replay buffer if settings changed while it was running."""
        with self._lock:
            if self.state is not CaptureState.BUFFERING:
                return
            self._buffer.stop()
            self._stop_desktop_pump()
            settings = self._settings_store.load()
            self._start_buffer_with_fallback(settings)
            self._set_state(CaptureState.BUFFERING)

    def shutdown(self) -> None:
        """Stop any live FFmpeg process owned by the engine."""
        try:
            self.stop_manual_recording()
        except Exception:
            logger.exception("Manual recording shutdown failed")
        try:
            self._buffer.stop()
        except Exception:
            logger.exception("Replay buffer shutdown failed")
        self._stop_desktop_pump()
        self._set_state(CaptureState.IDLE)

    # --- desktop-audio pump --------------------------------------------------

    def _start_desktop_pump(self, settings: Settings) -> DesktopAudioStream | None:
        """Start desktop-audio capture if the settings ask for it.

        Returns the pipe details FFmpeg should read, or ``None`` when desktop
        audio is switched off or unavailable on this machine.
        """
        if not (settings.capture_audio and settings.capture_desktop_audio):
            return None
        return self._pump.start()

    def _stop_desktop_pump(self) -> None:
        """Stop the desktop-audio pump — safe to call when it is already idle."""
        try:
            self._pump.stop()
        except Exception:
            logger.exception("Desktop audio pump failed to stop cleanly")

    # --- command building ----------------------------------------------------

    def _build_capture_io(
        self,
        settings: Settings,
        *,
        backend: VideoBackend,
        for_buffer: bool,
        desktop: DesktopAudioStream | None,
    ) -> list[str]:
        """Turn the current settings into the FFmpeg argv up to the codecs."""
        monitor, monitor_index = self._resolve_monitor(settings)
        plan = CapturePlan(
            monitor=monitor,
            monitor_index=monitor_index,
            fps=int(settings.fps),
            encoder=settings.encoder,
            preset=settings.preset,
            crf=int(settings.crf),
            audio=self._resolve_audio(settings, desktop),
        )
        keyframe_seconds = SEGMENT_SECONDS if for_buffer else _MANUAL_KEYFRAME_SECONDS
        return build_capture_io(
            plan,
            backend=backend,
            keyframe_seconds=keyframe_seconds,
            force_keyframes=for_buffer,
        )

    def _spawn_manual_with_fallback(self, settings: Settings, destination: Path) -> None:
        """Start the manual-recording process, falling back to gdigrab if needed.

        Must be called with the engine lock held. The desktop-audio pump is
        (re)started for each attempt so a fresh pipe is in place; a failed
        attempt tears its pump down before the next one begins.
        """
        last_error: RuntimeError | None = None
        for backend in _BACKEND_ORDER:
            desktop = self._start_desktop_pump(settings)
            io_args = self._build_capture_io(
                settings, backend=backend, for_buffer=False, desktop=desktop
            )
            args = [*io_args, "-movflags", "+faststart", str(destination)]

            ctx = spawn_ffmpeg(args)
            process = ctx.__enter__()
            try:
                self._check_started(process, f"Manual recording ({backend.value})")
            except RuntimeError as exc:
                ctx.__exit__(None, None, None)
                self._stop_desktop_pump()
                last_error = exc
                if backend is not _BACKEND_ORDER[-1]:
                    logger.warning(
                        "%s capture failed to start; trying the next backend", backend.value
                    )
                    continue
                break

            self._manual_context = ctx
            self._manual_process = process
            self._manual_output = destination
            logger.info("Manual recording started with %s backend", backend.value)
            return

        message = str(last_error) if last_error else "Manual recording failed to start."
        self._handle_error(message)
        raise RuntimeError(message)

    def _start_buffer_with_fallback(self, settings: Settings) -> None:
        """Start the rolling buffer, falling back to gdigrab if ddagrab fails.

        Must be called with the engine lock held.
        """
        last_error: RuntimeError | None = None
        for backend in _BACKEND_ORDER:
            desktop = self._start_desktop_pump(settings)
            spec = BufferSpec(
                capture_args=self._build_capture_io(
                    settings, backend=backend, for_buffer=True, desktop=desktop
                ),
                directory=app_paths().replay_buffer_dir,
                seconds=int(settings.replay_seconds),
                encoder=settings.encoder,
                preset=settings.preset,
                crf=int(settings.crf),
            )
            try:
                self._buffer.start(spec)
            except RuntimeError as exc:
                self._stop_desktop_pump()
                last_error = exc
                if backend is not _BACKEND_ORDER[-1]:
                    logger.warning(
                        "Replay buffer %s start failed; trying the next backend", backend.value
                    )
                    continue
                break
            logger.info("Replay buffer started with %s backend", backend.value)
            return

        message = str(last_error) if last_error else "Replay buffer failed to start."
        self._handle_error(message)
        raise RuntimeError(message)

    def _resolve_monitor(self, settings: Settings) -> tuple[Monitor, int]:
        """Find the monitor the user picked and its zero-based output index.

        The index doubles as the ddagrab ``output_idx``. It is taken from the
        position in the registry's list, which matches the DXGI output order
        on the single-GPU machines this app targets.
        """
        monitors = self._device_registry.monitors()
        for index, monitor in enumerate(monitors):
            if monitor.name == settings.monitor:
                return monitor, index
        if monitors:
            return monitors[0], 0

        # Nothing enumerated — fabricate a primary display so a capture can
        # still be attempted rather than failing outright.
        fallback = Monitor(
            name=settings.monitor or "Display 1",
            x=0,
            y=0,
            width=1920,
            height=1080,
            is_primary=True,
        )
        return fallback, 0

    def _resolve_audio(
        self,
        settings: Settings,
        desktop: DesktopAudioStream | None,
    ) -> AudioConfig:
        """Build the audio configuration for one capture.

        The microphone is whatever the settings name (empty means none). The
        desktop side is present only when the pump actually started — the
        ``desktop`` stream carries the pipe FFmpeg should read.
        """
        if not settings.capture_audio:
            return AudioConfig()

        microphone = settings.audio_input.strip()
        if desktop is None:
            return AudioConfig(microphone=microphone)
        return AudioConfig(
            microphone=microphone,
            desktop_pipe=desktop.pipe_name,
            desktop_rate=desktop.sample_rate,
            desktop_channels=desktop.channels,
        )

    def _clip_path(self, prefix: str, settings: Settings) -> Path:
        clips_dir = (
            Path(settings.output_dir).expanduser() if settings.output_dir else app_paths().clips_dir
        )
        clips_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return clips_dir / f"{prefix}_{timestamp}.mp4"

    @staticmethod
    def _check_started(process: subprocess.Popen[str], label: str) -> None:
        """Raise ``RuntimeError`` if FFmpeg died inside the startup grace window.

        A bad audio device or an unavailable codec makes FFmpeg exit within a
        few hundred milliseconds. If the process is still alive after the
        grace window we treat the capture as healthy.
        """
        try:
            exit_code = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return  # still running — healthy
        tail = read_stderr_tail(process)
        raise RuntimeError(
            f"{label} FFmpeg exited immediately (code {exit_code}). "
            f"FFmpeg said: {tail.strip() or '<no output>'}"
        )

    # --- callbacks -----------------------------------------------------------

    def _set_state(self, state: CaptureState) -> None:
        self.state = state
        callback = self.on_state_change
        if callback is not None:
            try:
                callback(state)
            except Exception:
                logger.exception("State-change callback failed")

    def _emit_clip_saved(self, path: Path) -> None:
        callback = self.on_clip_saved
        if callback is not None:
            try:
                callback(path)
            except Exception:
                logger.exception("Clip-saved callback failed")

    def _handle_error(self, message: str) -> None:
        logger.error(message)
        self._set_state(CaptureState.ERROR)
        callback = self.on_error
        if callback is not None:
            try:
                callback(message)
            except Exception:
                logger.exception("Error callback failed")


__all__ = ["FFmpegCaptureEngine"]
