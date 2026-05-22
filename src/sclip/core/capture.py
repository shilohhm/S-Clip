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


# How long ``shutdown()`` waits for an in-flight clip-save worker to finish
# before giving up on it. A stitch normally completes in a few seconds; this
# ceiling is generous enough to let a long buffer finish writing while still
# bounding how long quitting the app can hang.
_SAVE_JOIN_TIMEOUT = 30.0


class FFmpegCaptureEngine:
    """Capture engine used by the desktop application."""

    def __init__(
        self,
        settings_store: SettingsStore,
        device_registry: DeviceRegistry,
        *,
        buffer_factory: Callable[[Path], RollingBuffer] = RollingBuffer,
    ) -> None:
        """Wire the engine to its settings store and device registry.

        ``buffer_factory`` builds the :class:`RollingBuffer` the engine owns;
        it defaults to the real class and exists so a test can substitute a
        fake buffer whose stitch is instant and FFmpeg-free.
        """
        self._settings_store = settings_store
        self._device_registry = device_registry
        self._lock = threading.RLock()

        self.state: CaptureState = CaptureState.IDLE

        # Each event type fans out to a list of listeners rather than a single
        # callback slot, so the capture page and the main window can both
        # observe the engine. A bad listener is isolated by the try/except in
        # the notifier methods, so one cannot break the others.
        self._state_listeners: list[StateCallback] = []
        self._clip_listeners: list[ClipCallback] = []
        self._error_listeners: list[ErrorCallback] = []

        self._manual_process: subprocess.Popen[str] | None = None
        self._manual_context: AbstractContextManager[subprocess.Popen[str]] | None = None
        self._manual_output: Path | None = None

        # The clip-save stitch runs on a daemon worker thread so it never
        # blocks the GUI. At most one save runs at a time; this handle lets
        # ``shutdown()`` join a save that is still in flight.
        self._save_thread: threading.Thread | None = None

        self._buffer = buffer_factory(app_paths().replay_buffer_dir)
        self._buffer.set_error_handler(self._handle_error)

        # One pump, reused for every capture. It is started just before an
        # FFmpeg process and stopped once that process ends.
        self._pump = DesktopAudioPump()

    # --- listener registration -----------------------------------------------

    def add_state_listener(self, listener: StateCallback) -> None:
        """Register a callback notified whenever the capture state changes.

        The callback receives the new :class:`CaptureState`. It may be invoked
        from a worker thread, so a Qt-based listener must marshal back onto the
        GUI thread itself.
        """
        self._state_listeners.append(listener)

    def add_clip_listener(self, listener: ClipCallback) -> None:
        """Register a callback notified when a clip has been written to disk.

        The callback receives the saved clip's :class:`~pathlib.Path`. As with
        the other listeners it can fire on a worker thread.
        """
        self._clip_listeners.append(listener)

    def add_error_listener(self, listener: ErrorCallback) -> None:
        """Register a callback notified when the engine reports an error.

        The callback receives a human-readable message. It can fire on a
        worker thread, the same as the other listeners.
        """
        self._error_listeners.append(listener)

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

    def save_replay_clip(self) -> None:
        """Save the current rolling-buffer window as an MP4, off the GUI thread.

        The stitch is a full FFmpeg re-encode that can take seconds (minutes
        for a long buffer), so it runs on a daemon worker thread and this
        method returns immediately. The rolling buffer (and its desktop-audio
        pump) keep running throughout, so the user misses nothing while the
        clip is written.

        Calling this while a save is already in flight is a no-op — the engine
        is in the ``SAVING`` state and a second worker would race the first
        for the same segments. The completed clip is announced through the
        registered clip listeners; a failed stitch through the error listeners.
        """
        with self._lock:
            if self.state is not CaptureState.BUFFERING:
                # Either the buffer is not armed, or a save is already running
                # (state SAVING). In both cases there is nothing to start.
                return
            settings = self._settings_store.load()
            destination = self._clip_path("clip", settings)
            self._set_state(CaptureState.SAVING)
            thread = threading.Thread(
                target=self._run_save_clip,
                args=(destination,),
                name="sclip-clip-save",
                daemon=True,
            )
            self._save_thread = thread
            thread.start()

    def _run_save_clip(self, destination: Path) -> None:
        """Worker-thread body for :meth:`save_replay_clip`.

        Runs the blocking stitch, then restores the engine state under the
        lock (back to ``BUFFERING`` while the buffer rolls, or ``IDLE`` if it
        has since stopped). The clip/error notification happens *outside* the
        lock so a listener cannot deadlock against an engine call it makes in
        response, and so a long fan-out does not hold up other threads.
        """
        saved: Path | None = None
        try:
            saved = self._buffer.save_clip(destination)
        finally:
            with self._lock:
                if self._buffer.is_running:
                    self._set_state(CaptureState.BUFFERING)
                # Only fall back to IDLE if nothing else moved the engine on
                # (an error handler may have switched it to ERROR meanwhile).
                elif self.state is CaptureState.SAVING:
                    self._set_state(CaptureState.IDLE)

        if saved is not None:
            self._emit_clip_saved(saved)
        else:
            # save_clip returns None when the buffer had no segments to
            # stitch, or when the stitch itself failed. Reporting it here
            # (after the finally block above has otherwise restored BUFFERING)
            # ensures the error state and message actually reach the listeners
            # rather than being overwritten by the state restore.
            self._handle_error("Could not save the replay clip.")

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
        """Stop any live FFmpeg process owned by the engine.

        A clip save running on the worker thread is given a bounded chance to
        finish first: it is reading segment files that ``self._buffer.stop()``
        would otherwise purge out from under it. The join is time-limited so a
        wedged stitch cannot block the application from quitting.
        """
        try:
            self.stop_manual_recording()
        except Exception:
            logger.exception("Manual recording shutdown failed")
        self._join_save_thread()
        try:
            self._buffer.stop()
        except Exception:
            logger.exception("Replay buffer shutdown failed")
        self._stop_desktop_pump()
        self._set_state(CaptureState.IDLE)

    def _join_save_thread(self) -> None:
        """Wait for an in-flight clip-save worker to finish, within a timeout.

        Called during shutdown. A save normally completes in seconds; the
        timeout bounds the wait so a stuck stitch cannot wedge application
        exit. If the worker is still alive after the timeout we log and move
        on — it is a daemon thread, so it dies with the process.
        """
        with self._lock:
            thread = self._save_thread
        if thread is None or not thread.is_alive():
            return
        logger.info("Waiting for the in-flight clip save to finish")
        thread.join(timeout=_SAVE_JOIN_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                "Clip save did not finish within %.0fs of shutdown; abandoning it",
                _SAVE_JOIN_TIMEOUT,
            )

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
        """Record the new state and notify every state listener.

        Each listener is invoked inside its own ``try/except`` so a listener
        that raises — including one called from a worker thread — cannot stop
        the others from running or escape into the caller.
        """
        self.state = state
        for listener in list(self._state_listeners):
            try:
                listener(state)
            except Exception:
                logger.exception("State-change listener failed")

    def _emit_clip_saved(self, path: Path) -> None:
        """Notify every clip listener that a clip has been written.

        Listener failures are isolated exactly as in :meth:`_set_state`.
        """
        for listener in list(self._clip_listeners):
            try:
                listener(path)
            except Exception:
                logger.exception("Clip-saved listener failed")

    def _handle_error(self, message: str) -> None:
        """Move to the error state and notify every error listener.

        Listener failures are isolated exactly as in :meth:`_set_state`.
        """
        logger.error(message)
        self._set_state(CaptureState.ERROR)
        for listener in list(self._error_listeners):
            try:
                listener(message)
            except Exception:
                logger.exception("Error listener failed")


__all__ = ["FFmpegCaptureEngine"]
