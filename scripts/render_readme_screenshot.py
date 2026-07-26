"""Render the deterministic product screenshot used by the README.

Run from the repository root:

    python scripts/render_readme_screenshot.py

The renderer uses protocol-compatible in-memory collaborators and generated
clip thumbnails. It never starts FFmpeg, registers a global hotkey, or reads
the developer's real capture directory.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sclip.contracts import (  # noqa: E402
    AudioDevice,
    CaptureState,
    Hotkey,
    Monitor,
    Settings,
)
from sclip.ui import main_window as main_window_module  # noqa: E402
from sclip.ui.main_window import MainWindow  # noqa: E402
from sclip.ui.pages import capture_page, library_page, settings_page  # noqa: E402


class DemoEngine:
    """CaptureEngine stand-in fixed in the buffering state."""

    def __init__(self) -> None:
        self.state = CaptureState.BUFFERING
        self._state_listeners: list[Callable[[CaptureState], None]] = []
        self._clip_listeners: list[Callable[[Path], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []

    def start_manual_recording(self) -> None:
        self.state = CaptureState.RECORDING

    def stop_manual_recording(self) -> Path | None:
        self.state = CaptureState.IDLE
        return None

    def start_replay_buffer(self) -> None:
        self.state = CaptureState.BUFFERING

    def stop_replay_buffer(self) -> None:
        self.state = CaptureState.IDLE

    def save_replay_clip(self) -> None:
        self.state = CaptureState.SAVING

    def shutdown(self) -> None:
        return

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        self._state_listeners.append(listener)

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        self._clip_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        self._error_listeners.append(listener)


class DemoSettingsStore:
    """SettingsStore stand-in with a competition-oriented capture profile."""

    def __init__(self) -> None:
        self._settings = Settings(
            resolution="2560x1440",
            fps=144,
            encoder="h264_nvenc",
            preset="p5",
            audio_input="Tournament microphone",
            replay_buffer=True,
            replay_seconds=45,
            monitor="Display 1",
            clip_hotkey=Hotkey(key="F5"),
            record_hotkey=Hotkey(key="F6", ctrl=True),
        )

    def load(self) -> Settings:
        return self._settings.copy()

    def save(self, settings: Settings) -> None:
        self._settings = settings.copy()


class DemoDevices:
    """DeviceRegistry stand-in for the settings page."""

    def monitors(self) -> list[Monitor]:
        return [Monitor("Display 1", 0, 0, 2560, 1440, is_primary=True)]

    def audio_devices(self) -> list[AudioDevice]:
        return [AudioDevice("Tournament microphone", "input")]


class DemoHotkeys:
    """No-op hotkey collaborator for the main-window wiring."""

    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        return

    def unregister(self, hotkey: Hotkey) -> None:
        return

    def start(self) -> None:
        return

    def stop(self) -> None:
        return


class DemoPaths:
    """Small path surface consumed by the UI modules during rendering."""

    def __init__(self, clips_dir: Path) -> None:
        self.clips_dir = clips_dir
        self.assets_dir = _ROOT / "src" / "sclip" / "ui" / "assets"


def _paint_thumbnail(path: Path, variant: int) -> None:
    """Create a fictional gameplay thumbnail without external assets."""

    image = QImage(336, 188, QImage.Format.Format_RGB32)
    image.fill(QColor("#0B0D0E"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accents = ("#D8F45B", "#FF5D66", "#73DC8C", "#F0B85C")
        accent = QColor(accents[variant % len(accents)])

        painter.fillRect(QRect(0, 0, 336, 34), QColor("#161A1D"))
        painter.fillRect(QRect(18, 54, 190, 116), QColor("#101315"))
        painter.fillRect(QRect(222, 54, 96, 52), QColor("#1D2226"))
        painter.fillRect(QRect(222, 118, 96, 52), QColor("#1D2226"))

        painter.setPen(QPen(QColor("#292F33"), 1))
        for x in range(32, 336, 32):
            painter.drawLine(x, 34, x, 188)
        for y in range(50, 188, 24):
            painter.drawLine(0, y, 336, y)

        painter.setPen(QPen(QColor("#3D454B"), 3))
        painter.drawRect(40, 72, 148, 76)
        painter.drawLine(74, 72, 74, 112)
        painter.drawLine(118, 108, 188, 108)
        painter.drawLine(152, 108, 152, 148)

        target_x = 60 + variant * 28
        target_y = 92 + (variant % 2) * 28
        painter.setPen(QPen(accent, 3))
        painter.drawEllipse(target_x - 8, target_y - 8, 16, 16)
        painter.drawLine(target_x - 15, target_y, target_x + 15, target_y)
        painter.drawLine(target_x, target_y - 15, target_x, target_y + 15)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(276 - variant * 7, 74 + variant * 5, 12, 12)
    finally:
        painter.end()

    if not image.save(str(path), quality=92):
        raise RuntimeError(f"Could not write generated thumbnail to {path}")


def _seed_clips(clips_dir: Path) -> None:
    names = (
        "ranked_final_round.mp4",
        "overtime_clutch.mp4",
        "clean_entry.mp4",
        "match_point.mp4",
    )
    for index, name in enumerate(names):
        clip = clips_dir / name
        clip.write_bytes(b"SCLIP_README_DEMO")
        _paint_thumbnail(clip.with_suffix(".mp4.thumb.jpg"), index)


def render(output: Path, *, width: int = 1240, height: int = 900) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance()
    owns_app = app is None
    qt_app = QApplication([]) if app is None else app

    with tempfile.TemporaryDirectory(prefix="sclip-readme-", dir=_ROOT) as temp_dir:
        clips_dir = Path(temp_dir) / "clips"
        clips_dir.mkdir()
        _seed_clips(clips_dir)
        demo_paths = DemoPaths(clips_dir)

        # These UI modules import app_paths directly, so patch each local name.
        main_window_module.app_paths = lambda: demo_paths
        capture_page.app_paths = lambda: demo_paths
        library_page.app_paths = lambda: demo_paths
        settings_page.app_paths = lambda: demo_paths

        window = MainWindow(
            DemoEngine(),
            DemoSettingsStore(),
            DemoDevices(),
            DemoHotkeys(),
        )
        window.resize(width, height)
        window.show()
        for _ in range(16):
            qt_app.processEvents()

        if not window.grab().toImage().save(str(output)):
            raise RuntimeError(f"Could not write screenshot to {output}")

        library = getattr(window, "_library_page", None)
        pool = getattr(library, "_pool", None)
        if pool is not None:
            pool.waitForDone(5_000)
        watcher = getattr(library, "_watcher", None)
        if watcher is not None:
            watcher.removePaths(watcher.directories())
        window.close()
        window.deleteLater()
        for _ in range(4):
            qt_app.processEvents()

    if owns_app:
        qt_app.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_ROOT / "docs" / "assets" / "sclip-capture.png",
    )
    parser.add_argument("--width", type=int, default=1240)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    render(args.output.resolve(), width=max(960, args.width), height=max(640, args.height))
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
