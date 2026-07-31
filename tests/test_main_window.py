"""Tests for the application shell in :mod:`sclip.ui.main_window`.

The window is the seam where the non-Qt subsystems meet the GUI: it owns
navigation, the tray, and the bridges that carry engine and hotkey events onto
the Qt thread. All of that is driven here through fakes, so the tests exercise
the real wiring without FFmpeg, a global keyboard hook, or a system tray.

The offscreen Qt platform reports no system tray, so :meth:`_build_tray` leaves
``_tray`` as ``None``. Tests that care about tray behaviour install a fake tray
afterwards rather than trying to conjure a real one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtGui import QAction, QCloseEvent
from pytestqt.qtbot import QtBot

from sclip.contracts import BufferTelemetry, CaptureState, Hotkey, Settings
from sclip.ui import main_window as main_window_module
from sclip.ui.main_window import (
    _PAGE_ABOUT,
    _PAGE_CAPTURE,
    _PAGE_LIBRARY,
    _PAGE_SETTINGS,
    MainWindow,
)
from sclip.ui.pages import capture_page, library_page, settings_page

_ASSETS = Path(__file__).resolve().parent.parent / "src" / "sclip" / "ui" / "assets"


# ------------------------------------------------------------------- fakes


class _Engine:
    """CaptureEngine fake that records the calls the window makes."""

    def __init__(self, state: CaptureState = CaptureState.IDLE) -> None:
        self.state = state
        self.calls: list[str] = []
        self.reloaded = 0
        self.raise_on_save = False
        self._clip_listeners: list[Callable[[Path], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []
        self._state_listeners: list[Callable[[CaptureState], None]] = []

    def start_manual_recording(self) -> None:
        self.calls.append("start_manual")

    def stop_manual_recording(self) -> Path | None:
        self.calls.append("stop_manual")
        return None

    def start_replay_buffer(self) -> None:
        self.calls.append("start_buffer")

    def stop_replay_buffer(self) -> None:
        self.calls.append("stop_buffer")

    def save_replay_clip(self) -> None:
        self.calls.append("save_clip")
        if self.raise_on_save:
            raise RuntimeError("engine exploded")

    def telemetry(self) -> BufferTelemetry | None:
        return None

    def reload_settings(self) -> None:
        self.reloaded += 1

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        self._state_listeners.append(listener)

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        self._clip_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        self._error_listeners.append(listener)


class _Store:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def load(self) -> Settings:
        return self.settings.copy()

    def save(self, settings: Settings) -> None:
        self.settings = settings.copy()


class _Devices:
    def monitors(self) -> list[Any]:
        return []

    def audio_devices(self) -> list[Any]:
        return []


class _Hotkeys:
    """Hotkey listener fake that records every bind and unbind."""

    def __init__(self) -> None:
        self.registered: list[Hotkey] = []
        self.unregistered: list[Hotkey] = []
        self.started = False
        self.stopped = False

    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        self.registered.append(hotkey)

    def unregister(self, hotkey: Hotkey) -> None:
        self.unregistered.append(hotkey)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _Tray:
    """Minimal stand-in for the tray icon, capturing balloon messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.hidden = False

    def showMessage(self, title: str, body: str, *_args: Any) -> None:
        self.messages.append((title, body))

    def hide(self) -> None:
        self.hidden = True


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every module that reads ``app_paths`` at a temp clips directory.

    Each of these modules did ``from sclip.paths import app_paths``, binding the
    name locally, so patching ``sclip.paths`` alone would miss them.
    """
    clips = tmp_path / "clips"
    clips.mkdir()
    fake = SimpleNamespace(clips_dir=clips, assets_dir=_ASSETS)
    for module in (main_window_module, capture_page, library_page, settings_page):
        monkeypatch.setattr(module, "app_paths", lambda: fake)
    return fake


@pytest.fixture()
def engine() -> _Engine:
    return _Engine()


@pytest.fixture()
def hotkeys() -> _Hotkeys:
    return _Hotkeys()


@pytest.fixture()
def store() -> _Store:
    return _Store()


@pytest.fixture()
def window(
    qtbot: QtBot,
    paths: SimpleNamespace,
    engine: _Engine,
    store: _Store,
    hotkeys: _Hotkeys,
) -> MainWindow:
    instance = MainWindow(engine, store, _Devices(), hotkeys)
    qtbot.addWidget(instance)
    return instance


# -------------------------------------------------------------- navigation


def test_construction_registers_both_hotkeys(window: MainWindow, hotkeys: _Hotkeys) -> None:
    settings = Settings()
    assert settings.clip_hotkey in hotkeys.registered
    assert settings.record_hotkey in hotkeys.registered


def test_window_opens_on_the_capture_page(window: MainWindow) -> None:
    assert window._stack.currentIndex() == _PAGE_CAPTURE


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("library", _PAGE_LIBRARY),
        ("settings", _PAGE_SETTINGS),
        ("capture", _PAGE_CAPTURE),
        ("about", _PAGE_ABOUT),
        ("  LIBRARY  ", _PAGE_LIBRARY),  # trimmed and case-folded
    ],
)
def test_navigate_by_name(window: MainWindow, destination: str, expected: int) -> None:
    window._on_navigate_requested(destination)
    assert window._stack.currentIndex() == expected


def test_navigate_to_an_unknown_name_is_ignored(window: MainWindow) -> None:
    window._on_navigate_requested("library")
    window._on_navigate_requested("nowhere")
    assert window._stack.currentIndex() == _PAGE_LIBRARY


@pytest.mark.parametrize("index", [-1, 4, 99])
def test_navigate_to_an_out_of_range_index_is_ignored(window: MainWindow, index: int) -> None:
    """A misbehaving sidebar must not be able to blank the window."""
    window._on_navigate_index(_PAGE_SETTINGS)
    window._on_navigate_index(index)
    assert window._stack.currentIndex() == _PAGE_SETTINGS


# ----------------------------------------------------------- settings apply


def test_applying_settings_rebinds_the_hotkeys(window: MainWindow, hotkeys: _Hotkeys) -> None:
    """The previous chords must be released before the new ones are bound.

    Skipping the unregister would leave the old chord live, so a user who
    rebound clip-save to F8 would find F5 still firing it.
    """
    previous = Settings()
    hotkeys.registered.clear()

    updated = Settings(clip_hotkey=Hotkey(key="F8"), record_hotkey=Hotkey(key="F9", alt=True))
    window._apply_settings(updated)

    assert previous.clip_hotkey in hotkeys.unregistered
    assert previous.record_hotkey in hotkeys.unregistered
    assert hotkeys.registered == [updated.clip_hotkey, updated.record_hotkey]


def test_applying_settings_asks_the_engine_to_reload(window: MainWindow, engine: _Engine) -> None:
    window._apply_settings(Settings(replay_seconds=45))
    assert engine.reloaded == 1


def test_applying_settings_refreshes_the_capture_page(window: MainWindow) -> None:
    """The profile chips must not keep showing the pre-save values."""
    window._apply_settings(Settings(monitor="Monitor 7", fps=144))

    page = window._capture_page
    assert page is not None
    assert page._monitor_value.text() == "Monitor 7"
    assert "144fps" in page._quality_value.text()


def test_applying_settings_updates_the_tray_labels(window: MainWindow) -> None:
    window._tray_clip_action = QAction("", window)
    window._tray_record_action = QAction("", window)

    window._apply_settings(Settings(clip_hotkey=Hotkey(key="F8")))

    assert "F8" in window._tray_clip_action.text()


# --------------------------------------------------------------- clip save


def test_clip_request_while_idle_explains_itself(window: MainWindow, engine: _Engine) -> None:
    """Pressing the clip key with no buffer running should say so, not fail silently."""
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]
    engine.state = CaptureState.IDLE

    window._on_clip_requested()

    assert "save_clip" not in engine.calls
    assert tray.messages and "not running" in tray.messages[0][1]


def test_clip_request_while_buffering_starts_a_save(window: MainWindow, engine: _Engine) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]
    engine.state = CaptureState.BUFFERING

    window._on_clip_requested()

    assert engine.calls == ["save_clip"]
    assert tray.messages[-1][1].startswith("Saving")


def test_a_failing_save_surfaces_instead_of_crashing(window: MainWindow, engine: _Engine) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]
    engine.state = CaptureState.BUFFERING
    engine.raise_on_save = True

    window._on_clip_requested()  # must not propagate

    assert "Could not save" in tray.messages[-1][1]


def test_clip_saved_event_names_the_file(window: MainWindow) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]

    window._on_engine_clip_saved(Path("C:/clips/clip_2026-07-30_21-14-08.mp4"))

    assert "clip_2026-07-30_21-14-08.mp4" in tray.messages[-1][1]


def test_engine_errors_reach_the_tray(window: MainWindow) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]

    window._on_engine_error("Replay buffer FFmpeg exited immediately")

    assert tray.messages[-1][1] == "Replay buffer FFmpeg exited immediately"


# ---------------------------------------------------------- record toggling


def test_record_request_starts_when_idle(window: MainWindow, engine: _Engine) -> None:
    engine.state = CaptureState.IDLE
    window._on_record_requested()
    assert engine.calls == ["start_manual"]


def test_record_request_stops_when_recording(window: MainWindow, engine: _Engine) -> None:
    engine.state = CaptureState.RECORDING
    window._on_record_requested()
    assert engine.calls == ["stop_manual"]


def test_a_failing_toggle_surfaces_instead_of_crashing(window: MainWindow, engine: _Engine) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]

    def explode() -> None:
        raise RuntimeError("no engine")

    engine.start_manual_recording = explode  # type: ignore[method-assign]
    engine.state = CaptureState.IDLE

    window._on_record_requested()  # must not propagate

    assert "Recording toggle failed" in tray.messages[-1][1]


# ------------------------------------------------------------- close to tray


def test_closing_hides_to_tray_and_warns_once(window: MainWindow) -> None:
    """Closing must not stop the capture; the app keeps running in the tray.

    The balloon fires only the first time, so a user who minimises repeatedly
    is not nagged.
    """
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]
    window.show()

    first = QCloseEvent()
    window.closeEvent(first)
    assert not first.isAccepted()
    assert window.isHidden()
    assert len(tray.messages) == 1

    window.show()
    second = QCloseEvent()
    window.closeEvent(second)
    assert len(tray.messages) == 1  # still just the one


def test_quitting_from_the_tray_really_closes(window: MainWindow) -> None:
    tray = _Tray()
    window._tray = tray  # type: ignore[assignment]
    window._tray_clip_action = QAction("", window)
    window._tray_record_action = QAction("", window)

    window._on_quit_requested()

    assert window._force_quit is True
    assert tray.hidden is True


def test_closing_without_a_tray_closes_for_real(window: MainWindow) -> None:
    """With no tray there is nowhere to hide to, so close must mean close."""
    window._tray = None
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted()


# ------------------------------------------------------------ tray labels


def test_tray_labels_name_the_active_hotkeys(window: MainWindow) -> None:
    settings = Settings(clip_hotkey=Hotkey(key="F5"), record_hotkey=Hotkey(key="F6", ctrl=True))
    assert window._clip_action_label(settings) == "Save replay clip (F5)"
    assert window._record_action_label(settings) == "Toggle recording (Ctrl+F6)"


# -------------------------------------------------------- degraded startup


def test_a_page_that_fails_to_build_becomes_a_placeholder(window: MainWindow) -> None:
    """One broken page must not stop the rest of the app from opening."""

    def explode() -> object:
        raise RuntimeError("page is broken")

    placeholder = window._safe_make_page("capture", explode)

    assert placeholder.objectName() == "placeholder_capture"


def test_a_factory_returning_a_non_widget_is_replaced(window: MainWindow) -> None:
    placeholder = window._safe_make_page("library", lambda: "not a widget")
    assert placeholder is not None


def test_raise_from_another_instance_is_safe(window: MainWindow) -> None:
    """The single-instance guard calls this from a socket callback."""
    window.raise_from_other_instance()
    assert window.isVisible()
