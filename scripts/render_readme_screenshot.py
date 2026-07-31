"""Render the deterministic product screenshots used by the README.

Run from the repository root:

    python scripts/render_readme_screenshot.py

What this script is, precisely: the real :class:`~sclip.ui.main_window.MainWindow`,
with the real widgets, the real stylesheet and the real packaged fonts, driven by
in-memory collaborators so it can be rendered offscreen and reproducibly. It
never starts a capture, registers a global hotkey, or reads the developer's own
clips directory.

What it deliberately does *not* do: invent things the application could not
produce. An earlier version of this script painted four "gameplay" thumbnails
with :class:`QPainter`, gave them esports file names the app's own naming scheme
cannot generate, and attributed the capture to an audio device that exists on no
machine. The screenshot flattered the app rather than describing it.

Now the sample recordings are genuinely encoded by FFmpeg, thumbnailed through
the same command the library page uses, and named the way S-Clip actually names
files. The video content is an FFmpeg-generated gradient - plainly synthetic rather
than borrowed footage - and the README says so. If FFmpeg is not
on PATH the script still renders, showing the library's real empty state.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from sclip.contracts import (  # noqa: E402
    AudioDevice,
    BufferTelemetry,
    CaptureState,
    Hotkey,
    Monitor,
    Settings,
)
from sclip.core.ffmpeg import find_ffmpeg, popen_kwargs  # noqa: E402
from sclip.ui import main_window as main_window_module  # noqa: E402
from sclip.ui.main_window import MainWindow  # noqa: E402
from sclip.ui.pages import capture_page, library_page, settings_page  # noqa: E402

# Sample recordings, named exactly the way ``FFmpegCaptureEngine._clip_path``
# names them: ``<prefix>_<YYYY-MM-DD>_<HH-MM-SS>.mp4``. Fixed timestamps keep
# the render reproducible. Durations vary so the library shows a realistic
# spread of sizes rather than four identical tiles.
_SAMPLE_CLIPS: tuple[tuple[str, int, str, str], ...] = (
    ("clip_2026-07-30_21-14-08.mp4", 30, "0x101B24", "0x2E6F8E"),
    ("clip_2026-07-30_20-52-31.mp4", 30, "0x141018", "0x6E4A8C"),
    ("recording_2026-07-30_20-05-17.mp4", 12, "0x101613", "0x3F7A55"),
    ("clip_2026-07-29_23-41-55.mp4", 30, "0x1A1410", "0x8C6A3A"),
)

# Matches the thumbnail command in ``sclip.ui.pages.library_page``.
_THUMB_SCALE_WIDTH = 320


class DemoEngine:
    """CaptureEngine stand-in holding a full replay window."""

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

    def telemetry(self) -> BufferTelemetry:
        """A full 30-second window, sized like a real 1080p60 capture.

        The figures are consistent with one another rather than flattering:
        15 finished two-second segments is 30 seconds, and 31.4 MB across those
        30 seconds works out at about 8.4 Mb/s - what this profile would
        actually produce.
        """
        return BufferTelemetry(
            buffered_seconds=30.0,
            window_seconds=30,
            segment_count=15,
            segment_capacity=16,
            bytes_on_disk=31_457_280,
        )

    def shutdown(self) -> None:
        return

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        self._state_listeners.append(listener)

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        self._clip_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        self._error_listeners.append(listener)


class DemoSettingsStore:
    """SettingsStore stand-in holding an ordinary 1080p60 capture profile.

    These are the application's own defaults for everything except the encoder,
    which is set to NVENC so the Settings screenshot shows what the hardware
    detection actually selects on a machine with an NVIDIA GPU.
    """

    def __init__(self) -> None:
        self._settings = Settings(
            resolution="1920x1080",
            fps=60,
            encoder="h264_nvenc",
            preset="p5",
            crf=21,
            audio_input="Microphone (Realtek High Definition Audio)",
            replay_buffer=True,
            replay_seconds=30,
            monitor="Monitor 1",
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
        return [Monitor("Monitor 1", 0, 0, 1920, 1080, is_primary=True)]

    def audio_devices(self) -> list[AudioDevice]:
        return [AudioDevice("Microphone (Realtek High Definition Audio)", "input")]


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


def _pin_animations(window: MainWindow) -> None:
    """Freeze every running animation at a fixed point before grabbing.

    The record orb sweeps and the status dot pulses, both driven by
    ``QPropertyAnimation`` off the wall clock. Grabbing without pinning them
    makes the output depend on how long the process happened to take to reach
    this line, so two runs of a supposedly deterministic renderer produce
    different files. The values below are arbitrary but fixed: the sweep sits
    at a pleasant angle rather than at its seam.
    """
    page = getattr(window, "_capture_page", None)
    orb = getattr(page, "_orb", None)
    if orb is not None:
        orb.setProperty("phase", 300.0)
        orb.setProperty("pulse", 0.5)
    pill = getattr(page, "_status_pill", None)
    if pill is not None:
        pill.setProperty("_opacity", 1.0)


def _display_path(path: Path) -> str:
    """Show a repo-relative path when possible, else the absolute one.

    ``--output`` and ``--output-dir`` accept any location, including one
    outside the working tree, so ``relative_to`` cannot be assumed to succeed.
    """
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def _run_ffmpeg(ffmpeg: Path, argv: list[str], *, timeout: float) -> bool:
    """Run one FFmpeg invocation, reporting success rather than raising."""
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **popen_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  FFmpeg call failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"  FFmpeg exited {result.returncode}: {result.stderr.strip()[-200:]}",
            file=sys.stderr,
        )
        return False
    return True


def _encode_sample_clips(clips_dir: Path) -> bool:
    """Encode real sample recordings and their thumbnails with FFmpeg.

    Returns ``False`` when FFmpeg is unavailable, in which case the render
    proceeds and simply shows the genuine empty state.
    """
    try:
        ffmpeg = find_ffmpeg()
    except Exception as exc:
        print(f"  FFmpeg not found ({exc}); rendering the empty library state")
        return False

    for name, seconds, colour_a, colour_b in _SAMPLE_CLIPS:
        clip = clips_dir / name
        encoded = _run_ffmpeg(
            ffmpeg,
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"gradients=size=1280x720:rate=30:c0={colour_a}:c1={colour_b}"
                f":x0=120:y0=80:x1=1160:y1=640:nb_colors=2:speed=0.02",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:duration={seconds}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-t",
                str(seconds),
                "-movflags",
                "+faststart",
                str(clip),
            ],
            timeout=120.0,
        )
        if not encoded:
            return False

        # Same command the library's thumbnail worker runs, so the tiles in the
        # screenshot come from the real thumbnail path.
        _run_ffmpeg(
            ffmpeg,
            [
                "-ss",
                "1",
                "-i",
                str(clip),
                "-frames:v",
                "1",
                "-vf",
                f"scale={_THUMB_SCALE_WIDTH}:-1",
                "-y",
                str(clip.with_suffix(clip.suffix + ".thumb.jpg")),
            ],
            timeout=60.0,
        )
        _stamp_from_name(clip)
        print(f"  encoded {name} ({seconds}s)")
    return True


def _stamp_from_name(clip: Path) -> None:
    """Backdate a sample clip so its mtime agrees with its file name.

    The library sorts by modification time and prints it under each tile, so a
    clip called ``clip_2026-07-29_23-41-55.mp4`` that claims it was written
    today reads as an obvious prop. Parsing the timestamp back out of the name
    keeps the demo internally consistent.
    """
    stem = clip.stem
    try:
        _prefix, date_part, time_part = stem.split("_", 2)
        moment = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return
    stamp = moment.timestamp()
    os.utime(clip, (stamp, stamp))
    thumb = clip.with_suffix(clip.suffix + ".thumb.jpg")
    if thumb.is_file():
        os.utime(thumb, (stamp, stamp))


def render(output_dir: Path, *, width: int = 1240, height: int = 900) -> list[Path]:
    """Render every page and return the files written."""
    output_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance()
    owns_app = app is None
    qt_app = QApplication([]) if app is None else app

    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="sclip-readme-", dir=_ROOT) as temp_dir:
        clips_dir = Path(temp_dir) / "clips"
        clips_dir.mkdir()
        print("Encoding sample recordings...")
        _encode_sample_clips(clips_dir)
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

        library = getattr(window, "_library_page", None)
        pool = getattr(library, "_pool", None)

        for index, name in enumerate(("capture", "library", "settings", "about")):
            window._set_current_page(index)
            # The library page thumbnails on a worker pool; let it settle so the
            # screenshot shows loaded tiles rather than "Generating preview…".
            if pool is not None:
                pool.waitForDone(20_000)
            for _ in range(24):
                qt_app.processEvents()
            _pin_animations(window)

            target = output_dir / f"sclip-{name}.png"
            if not window.grab().toImage().save(str(target)):
                raise RuntimeError(f"Could not write screenshot to {target}")
            written.append(target)
            print(f"  wrote {_display_path(target)}")

        if pool is not None:
            pool.waitForDone(5_000)
        watcher = getattr(library, "_watcher", None)
        if watcher is not None:
            watcher.removePaths(watcher.directories())
        tray = getattr(window, "_tray", None)
        if tray is not None:
            tray.hide()
        window._force_quit = True
        window.close()
        window.deleteLater()
        for _ in range(4):
            qt_app.processEvents()

    if owns_app:
        qt_app.quit()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_ROOT / "docs" / "assets",
    )
    parser.add_argument("--width", type=int, default=1240)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    render(
        args.output_dir.resolve(),
        width=max(960, args.width),
        height=max(640, args.height),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
