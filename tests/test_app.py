"""Tests for the bootstrap helpers in :mod:`sclip.app` and logging setup.

``main`` itself is not exercised here: it builds a :class:`QApplication` and
enters the event loop, which a test run cannot sit through. What *is* covered
is everything around it — argument handling, the single-instance guard, and
the fallback collaborators that let the window open and explain itself when a
subsystem cannot be built.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSharedMemory

from sclip.app import (
    _SINGLETON_BYPASS_FLAGS,
    _another_instance_is_running,
    _build_capture_engine,
    _build_device_registry,
    _build_settings_store,
    _consume_flag,
    _DisabledCaptureEngine,
    _EmptyDeviceRegistry,
    _InMemorySettingsStore,
)
from sclip.contracts import CaptureState, Settings
from sclip.logging_config import configure_logging

# --------------------------------------------------------------- argv flags


def test_consume_flag_removes_the_flag_and_reports_it() -> None:
    args = ["sclip", "--no-single-instance", "--other"]

    found = _consume_flag(args, _SINGLETON_BYPASS_FLAGS)

    assert found is True
    assert args == ["sclip", "--other"]


def test_consume_flag_leaves_argv_alone_when_absent() -> None:
    args = ["sclip", "--other"]

    found = _consume_flag(args, _SINGLETON_BYPASS_FLAGS)

    assert found is False
    assert args == ["sclip", "--other"]


def test_consume_flag_removes_every_occurrence() -> None:
    args = ["sclip", "--dev-no-singleton", "x", "--no-single-instance"]

    assert _consume_flag(args, _SINGLETON_BYPASS_FLAGS) is True
    assert args == ["sclip", "x"]


# ------------------------------------------------------- single-instance guard


def test_first_instance_claims_the_segment() -> None:
    segment = QSharedMemory("sclip-test-singleton-first")
    try:
        assert _another_instance_is_running(segment) is False
    finally:
        if segment.isAttached():
            segment.detach()


def test_a_second_instance_is_detected() -> None:
    """The second launch must stand down rather than start a rival engine."""
    key = "sclip-test-singleton-second"
    first = QSharedMemory(key)
    second = QSharedMemory(key)
    try:
        assert _another_instance_is_running(first) is False
        assert _another_instance_is_running(second) is True
    finally:
        for segment in (second, first):
            if segment.isAttached():
                segment.detach()


# ------------------------------------------------------------ fallback stubs


def test_in_memory_settings_store_round_trips() -> None:
    store = _InMemorySettingsStore()

    store.save(Settings(fps=120, monitor="Monitor 2"))
    loaded = store.load()

    assert loaded.fps == 120
    assert loaded.monitor == "Monitor 2"


def test_in_memory_settings_store_hands_out_copies() -> None:
    """A caller mutating what it loaded must not corrupt the stored value."""
    store = _InMemorySettingsStore()
    store.save(Settings(fps=60))

    first = store.load()
    first.fps = 999

    assert store.load().fps == 60


def test_empty_device_registry_reports_nothing() -> None:
    registry = _EmptyDeviceRegistry()
    assert registry.monitors() == []
    assert registry.audio_devices() == []


def test_disabled_engine_reports_the_error_state() -> None:
    """With no engine the UI must still open and show something is wrong."""
    engine = _DisabledCaptureEngine()
    assert engine.state is CaptureState.ERROR


def test_disabled_engine_refuses_every_capture_action() -> None:
    engine = _DisabledCaptureEngine()

    # None of these may raise: the window wires them to real buttons.
    engine.start_manual_recording()
    engine.start_replay_buffer()
    engine.save_replay_clip()
    engine.stop_replay_buffer()
    engine.shutdown()
    engine.add_state_listener(lambda _state: None)
    engine.add_clip_listener(lambda _path: None)
    engine.add_error_listener(lambda _message: None)

    assert engine.stop_manual_recording() is None
    assert engine.telemetry() is None


# ------------------------------------------------------ first-run tuning


def test_first_run_tuning_is_skipped_once_settings_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presence of the settings file is the "has run before" signal.

    Re-running the hardware detection on every launch would silently discard
    whatever the user had configured.
    """
    import sclip.app as app_module

    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        app_module, "app_paths", lambda: SimpleNamespace(settings_file=settings_file)
    )

    store = _InMemorySettingsStore()
    store.save(Settings(fps=30))
    app_module._apply_first_run_recommendation(store, _EmptyDeviceRegistry())

    assert store.load().fps == 30  # untouched


def test_first_run_tuning_persists_a_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sclip.app as app_module

    monkeypatch.setattr(
        app_module,
        "app_paths",
        lambda: SimpleNamespace(settings_file=tmp_path / "absent.json"),
    )

    store = _InMemorySettingsStore()
    app_module._apply_first_run_recommendation(store, _EmptyDeviceRegistry())

    # The concrete values depend on the host, but something must be written.
    assert isinstance(store.load(), Settings)


def test_first_run_tuning_survives_a_detection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hardware probing is best-effort; failing it must not block startup."""
    import sclip.app as app_module
    import sclip.core.hardware as hardware_module

    monkeypatch.setattr(
        app_module,
        "app_paths",
        lambda: SimpleNamespace(settings_file=tmp_path / "absent.json"),
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("hardware probe blew up")

    monkeypatch.setattr(hardware_module, "recommend_settings", explode)

    # Must not raise; the app falls back to the built-in defaults.
    app_module._apply_first_run_recommendation(_InMemorySettingsStore(), _EmptyDeviceRegistry())


# ------------------------------------------------------------- builders


def test_settings_store_builder_falls_back_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken settings file must not stop the application from starting."""
    import sclip.core.settings as settings_module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("config directory is unwritable")

    monkeypatch.setattr(settings_module, "JsonSettingsStore", explode)

    store = _build_settings_store()

    assert isinstance(store, _InMemorySettingsStore)


def test_device_registry_builder_falls_back_when_enumeration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sclip.core.devices as devices_module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(devices_module, "SystemDeviceRegistry", explode)

    assert isinstance(_build_device_registry(), _EmptyDeviceRegistry)


def test_capture_engine_builder_falls_back_when_ffmpeg_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing FFmpeg is the likeliest failure, and must stay recoverable.

    The user needs the window to open so they can read the diagnostic on the
    About page, rather than meeting a crash on launch.
    """
    import sclip.core.capture as capture_module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ffmpeg not found on PATH")

    monkeypatch.setattr(capture_module, "FFmpegCaptureEngine", explode)

    engine = _build_capture_engine(_InMemorySettingsStore(), _EmptyDeviceRegistry())

    assert isinstance(engine, _DisabledCaptureEngine)
    assert engine.state is CaptureState.ERROR


# --------------------------------------------------------------- logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> object:
    """Undo whatever a logging test does to the process-wide root logger."""
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    previous_flag = getattr(root, "_sclip_logging_configured", False)
    yield
    for handler in list(root.handlers):
        if handler not in previous_handlers:
            handler.close()
            root.removeHandler(handler)
    root.handlers = previous_handlers
    root.setLevel(previous_level)
    root._sclip_logging_configured = previous_flag  # type: ignore[attr-defined]


def test_configure_logging_writes_to_the_given_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    root._sclip_logging_configured = False  # type: ignore[attr-defined]
    log_file = tmp_path / "logs" / "sclip.log"

    configure_logging(logging.INFO, log_file=log_file)
    logging.getLogger("sclip.test").info("hello from the test")
    for handler in root.handlers:
        handler.flush()

    assert log_file.exists()
    assert "hello from the test" in log_file.read_text(encoding="utf-8")


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """A second call must not double every log line."""
    root = logging.getLogger()
    root._sclip_logging_configured = False  # type: ignore[attr-defined]
    log_file = tmp_path / "sclip.log"

    configure_logging(logging.INFO, log_file=log_file)
    handler_count = len(root.handlers)
    configure_logging(logging.DEBUG, log_file=log_file)

    assert len(root.handlers) == handler_count
    # The level is still updated on the repeat call.
    assert root.level == logging.DEBUG
