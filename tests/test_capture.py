"""Tests for the FFmpeg-backed capture engine in :mod:`sclip.core.capture`.

The engine's headline behaviour after Remediation R1 is that saving a replay
clip no longer blocks the caller: :meth:`FFmpegCaptureEngine.save_replay_clip`
spawns a worker thread for the (potentially slow) FFmpeg stitch and returns at
once. These tests verify that promptness directly, and that a registered
clip-listener is still notified once the worker finishes.

No real FFmpeg process is spawned. :class:`FFmpegCaptureEngine` accepts a
``buffer_factory`` so a test can inject a fake :class:`RollingBuffer` whose
``save_clip`` is instant (or deliberately slow, to prove the call did not
block on it). The capture-side argv building is pure and exercised for free;
anything that would touch FFmpeg or audio hardware is kept out by disabling
audio capture in the fake settings.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sclip.contracts import (
    AudioDevice,
    CaptureState,
    Monitor,
    Settings,
)
from sclip.core import capture as capture_module
from sclip.core.capture import FFmpegCaptureEngine
from sclip.paths import AppPaths

# Upper bound on how long save_replay_clip itself may take. The fake stitch
# below sleeps far longer than this, so a return inside this window proves the
# call did not wait on the stitch. Generous enough to stay reliable on slow CI.
_PROMPT_RETURN_SECONDS: float = 0.5

# How long the fake stitch sleeps. Comfortably longer than _PROMPT_RETURN_SECONDS
# so "returned promptly" cannot be satisfied by the stitch simply being fast.
_FAKE_STITCH_SECONDS: float = 1.0

# How long a test waits for the worker thread to deliver its clip notification.
_NOTIFY_TIMEOUT_SECONDS: float = 5.0


class _FakeRollingBuffer:
    """A drop-in stand-in for :class:`~sclip.core.replay_buffer.RollingBuffer`.

    It implements only the surface the capture engine touches: ``start``,
    ``stop``, ``is_running``, ``set_error_handler`` and ``save_clip``. The
    ``start`` method is a no-op flag flip — no FFmpeg process is involved —
    and ``save_clip`` sleeps to imitate a slow re-encode so a test can prove
    the engine did not block on it.
    """

    def __init__(self, directory: Path, *, stitch_seconds: float = 0.0) -> None:
        self.directory = directory
        self._stitch_seconds = stitch_seconds
        self.is_running = False
        self._error_handler: object | None = None
        # Set on the thread that actually runs save_clip, so a test can prove
        # the stitch happened off the calling thread.
        self.save_thread_name: str | None = None
        self.save_calls = 0

    def set_error_handler(self, handler: object) -> None:
        self._error_handler = handler

    def start(self, spec: object) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False

    def save_clip(self, destination: Path) -> Path | None:
        """Pretend to stitch a clip, slowly, then write the destination file."""
        self.save_calls += 1
        self.save_thread_name = threading.current_thread().name
        if self._stitch_seconds:
            time.sleep(self._stitch_seconds)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"FAKE_CLIP\n")
        return destination


class _FakeSettingsStore:
    """Minimal settings store with desktop/microphone audio switched off.

    Disabling audio keeps the engine's capture-IO building entirely in pure
    Python — no desktop-audio pump is started — so the test never touches
    real audio hardware.
    """

    def __init__(self) -> None:
        self._settings = Settings(capture_audio=False, capture_desktop_audio=False)

    def load(self) -> Settings:
        return self._settings.copy()

    def save(self, settings: Settings) -> None:
        self._settings = settings.copy()


class _FakeDeviceRegistry:
    """Device registry exposing one monitor and no audio devices."""

    def monitors(self) -> list[Monitor]:
        return [
            Monitor(name="Monitor 1", x=0, y=0, width=1920, height=1080, is_primary=True)
        ]

    def audio_devices(self) -> list[AudioDevice]:
        return []


@pytest.fixture()
def sandbox_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the capture engine's path resolution into a temp directory.

    :mod:`sclip.core.capture` binds :func:`sclip.paths.app_paths` at import
    time, so the patch is applied to the name as the engine sees it. The
    returned directory is the temp root the saved clips and the replay buffer
    live under. ``monkeypatch`` reverts the patch automatically on teardown.
    """
    data_dir = tmp_path / "data"
    fake_paths = AppPaths(
        package_root=tmp_path / "pkg",
        project_root=tmp_path / "project",
        assets_dir=tmp_path / "pkg" / "assets",
        styles_qss=tmp_path / "pkg" / "styles.qss",
        config_dir=tmp_path / "config",
        settings_file=tmp_path / "config" / "settings.json",
        clips_dir=data_dir / "clips",
        replay_buffer_dir=data_dir / "replay_buffer",
        log_file=data_dir / "logs" / "sclip.log",
    )
    monkeypatch.setattr(capture_module, "app_paths", lambda: fake_paths)
    return tmp_path


@pytest.fixture()
def fast_engine(sandbox_paths: Path) -> FFmpegCaptureEngine:
    """An engine wired to an instant fake buffer, with paths sandboxed.

    ``_FakeRollingBuffer`` matches the ``Callable[[Path], RollingBuffer]``
    factory shape directly — its extra ``stitch_seconds`` argument is
    keyword-only with a default — so it can be passed as the factory as-is.
    """
    return FFmpegCaptureEngine(
        _FakeSettingsStore(),
        _FakeDeviceRegistry(),
        buffer_factory=_FakeRollingBuffer,
    )


# --------------------------------------------------------- multi-listener API


def test_multiple_state_listeners_are_all_notified(fast_engine: FFmpegCaptureEngine) -> None:
    """Every registered state listener should receive each state change."""
    seen_a: list[CaptureState] = []
    seen_b: list[CaptureState] = []
    fast_engine.add_state_listener(seen_a.append)
    fast_engine.add_state_listener(seen_b.append)

    fast_engine.start_replay_buffer()

    assert seen_a == [CaptureState.BUFFERING]
    assert seen_b == [CaptureState.BUFFERING]


def test_a_failing_listener_does_not_break_the_others(
    fast_engine: FFmpegCaptureEngine,
) -> None:
    """One listener raising must not stop the engine notifying the rest."""
    survivor: list[CaptureState] = []

    def _boom(_state: CaptureState) -> None:
        raise RuntimeError("listener intentionally explodes")

    fast_engine.add_state_listener(_boom)
    fast_engine.add_state_listener(survivor.append)

    # Must not raise, and the healthy listener must still have been called.
    fast_engine.start_replay_buffer()

    assert survivor == [CaptureState.BUFFERING]


# ------------------------------------------------------- async clip saving


def test_save_replay_clip_returns_before_the_stitch_finishes(
    sandbox_paths: Path,
) -> None:
    """``save_replay_clip`` must not block on the (slow) FFmpeg stitch.

    The fake buffer's ``save_clip`` sleeps for ``_FAKE_STITCH_SECONDS``; the
    call itself must return well inside ``_PROMPT_RETURN_SECONDS``, proving the
    stitch was handed to a worker thread rather than run inline.
    """
    buffer = _FakeRollingBuffer(sandbox_paths, stitch_seconds=_FAKE_STITCH_SECONDS)
    engine = FFmpegCaptureEngine(
        _FakeSettingsStore(),
        _FakeDeviceRegistry(),
        buffer_factory=lambda _directory: buffer,
    )
    try:
        engine.start_replay_buffer()
        assert engine.state is CaptureState.BUFFERING

        started = time.monotonic()
        engine.save_replay_clip()
        elapsed = time.monotonic() - started

        assert elapsed < _PROMPT_RETURN_SECONDS, (
            f"save_replay_clip blocked for {elapsed:.2f}s — it should return "
            "immediately and stitch on a worker thread"
        )
        # The engine flips to SAVING synchronously while the worker runs.
        assert engine.state is CaptureState.SAVING
    finally:
        engine.shutdown()


def test_save_replay_clip_notifies_clip_listener_when_done(
    sandbox_paths: Path,
) -> None:
    """A registered clip-listener is eventually notified with the saved path."""
    buffer = _FakeRollingBuffer(sandbox_paths, stitch_seconds=_FAKE_STITCH_SECONDS)
    engine = FFmpegCaptureEngine(
        _FakeSettingsStore(),
        _FakeDeviceRegistry(),
        buffer_factory=lambda _directory: buffer,
    )

    notified = threading.Event()
    saved_paths: list[Path] = []

    def _on_clip(path: Path) -> None:
        saved_paths.append(path)
        notified.set()

    engine.add_clip_listener(_on_clip)

    try:
        engine.start_replay_buffer()
        engine.save_replay_clip()

        # The notification arrives only after the worker thread finishes the
        # stitch, so we wait for it rather than asserting immediately.
        assert notified.wait(timeout=_NOTIFY_TIMEOUT_SECONDS), (
            "clip listener was never notified after the save completed"
        )
        assert len(saved_paths) == 1
        assert saved_paths[0].exists()
        assert saved_paths[0].suffix == ".mp4"
        # The stitch ran off the GUI/calling thread, on the engine's worker.
        assert buffer.save_thread_name == "sclip-clip-save"
        # The engine settles back to BUFFERING once the save is done.
        assert engine.state is CaptureState.BUFFERING
    finally:
        engine.shutdown()


def test_save_replay_clip_is_a_no_op_when_not_buffering(
    fast_engine: FFmpegCaptureEngine,
) -> None:
    """Saving while idle must not start a worker or change state."""
    assert fast_engine.state is CaptureState.IDLE
    fast_engine.save_replay_clip()
    assert fast_engine.state is CaptureState.IDLE


def test_a_second_save_while_one_is_in_flight_is_ignored(
    sandbox_paths: Path,
) -> None:
    """A save call while a save is already running must not spawn a second worker."""
    buffer = _FakeRollingBuffer(sandbox_paths, stitch_seconds=_FAKE_STITCH_SECONDS)
    engine = FFmpegCaptureEngine(
        _FakeSettingsStore(),
        _FakeDeviceRegistry(),
        buffer_factory=lambda _directory: buffer,
    )
    try:
        engine.start_replay_buffer()

        engine.save_replay_clip()
        assert engine.state is CaptureState.SAVING
        # Second request lands while the first worker is still stitching.
        engine.save_replay_clip()

        # shutdown joins the in-flight worker, so by here every save is done.
        engine.shutdown()
        assert buffer.save_calls == 1, "a second save worker should not have started"
    finally:
        engine.shutdown()
