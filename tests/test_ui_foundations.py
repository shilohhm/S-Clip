"""Focused regression tests for the portfolio-grade UI foundations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from sclip.contracts import CaptureMode, CaptureState, Settings
from sclip.ui.fonts import install_application_fonts
from sclip.ui.pages import capture_page
from sclip.ui.pages.capture_page import CapturePage
from sclip.ui.theme import load_stylesheet
from sclip.ui.widgets.record_orb import RecordOrb


class _Engine:
    def __init__(self) -> None:
        self.state = CaptureState.BUFFERING
        self.state_listeners: list[Callable[[CaptureState], None]] = []
        self.clip_listeners: list[Callable[[Path], None]] = []
        self.error_listeners: list[Callable[[str], None]] = []

    def start_manual_recording(self) -> None:
        return

    def stop_manual_recording(self) -> Path | None:
        return None

    def start_replay_buffer(self) -> None:
        return

    def stop_replay_buffer(self) -> None:
        return

    def save_replay_clip(self) -> None:
        return

    def shutdown(self) -> None:
        return

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        self.state_listeners.append(listener)

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        self.clip_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        self.error_listeners.append(listener)


class _Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def load(self) -> Settings:
        return self.settings.copy()

    def save(self, settings: Settings) -> None:
        self.settings = settings.copy()


def test_bundled_fonts_register_with_qt(qapp: QApplication) -> None:
    families = install_application_fonts()

    assert "Figtree" in families
    assert "Unbounded" in families


def test_stylesheet_uses_packaged_type_and_signal_palette() -> None:
    stylesheet = load_stylesheet()

    assert "font-family: 'Figtree'" in stylesheet
    assert "font-family: 'Unbounded'" in stylesheet
    assert "#D8F45B" in stylesheet
    assert "#7C5CFF" not in stylesheet


def test_record_orb_supports_keyboard_activation(qtbot: QtBot) -> None:
    orb = RecordOrb()
    qtbot.addWidget(orb)
    activated: list[bool] = []
    orb.clicked.connect(lambda: activated.append(True))

    orb.show()
    orb.setFocus()
    qtbot.keyClick(orb, Qt.Key.Key_Space)

    assert orb.focusPolicy() is Qt.FocusPolicy.StrongFocus
    assert activated == [True]


def test_replay_users_land_on_the_replay_control(
    qtbot: QtBot,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        capture_page,
        "app_paths",
        lambda: SimpleNamespace(clips_dir=tmp_path),
    )
    page = CapturePage(_Engine(), _Store(Settings(replay_buffer=True)))
    qtbot.addWidget(page)
    qtbot.waitUntil(lambda: not page._mode_selector.isEnabled())

    assert page._mode is CaptureMode.REPLAY_BUFFER
    assert page._mode_selector.current_index == 1
    assert not page._mode_selector.isEnabled()
