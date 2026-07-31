"""Render the README demo animation.

Run from the repository root:

    python scripts/render_demo_gif.py

The animation walks the capture screen through the replay-buffer story: armed
and empty, filling second by second, full, then a save and the finished clip
landing in the recent strip.

It is a recording of the real window. Every frame is
:class:`~sclip.ui.main_window.MainWindow` with the real widgets, stylesheet and
packaged fonts, grabbed offscreen. What is scripted is the *engine*: a stub
feeds the window a sequence of states and buffer snapshots instead of FFmpeg
doing it in real time.

That distinction is deliberate. Driving a genuine capture would mean recording
whatever happened to be on the developer's screen and committing the result to
a public repository, and it would make the output different every run. Scripting
the engine keeps the animation reproducible and keeps private desktop content
out of the repository, while every pixel of interface remains real.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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

# The window is rendered at this size and the GIF scaled to _GIF_WIDTH. The
# height is chosen so the recent-clip strip stays on screen: the finished clip
# landing there is the payoff of the whole sequence, and a shorter window puts
# it below the fold where the animation cannot show it.
_WINDOW = (1100, 1000)
_GIF_WIDTH = 860
_FPS = 10

_WINDOW_SECONDS = 30
_SEGMENT_SECONDS = 2
_SEGMENT_CAPACITY = 16

# Roughly 8.4 Mb/s at 1080p60, matching the screenshot profile.
_BYTES_PER_SECOND = 1_048_576


@dataclass(frozen=True, slots=True)
class Beat:
    """One moment in the animation, held for ``frames`` frames."""

    state: CaptureState
    buffered: float | None  # None means the engine reports no rolling window
    frames: int
    drop_clip: bool = False  # land the finished clip in the recent strip


def _storyboard() -> tuple[Beat, ...]:
    """The sequence the animation walks through.

    Filling is stepped two seconds at a time because that is the real segment
    length: the buffered figure genuinely advances in segment-sized jumps
    rather than smoothly, and the animation should not pretend otherwise.
    """
    beats: list[Beat] = [
        Beat(CaptureState.IDLE, None, frames=8),
        Beat(CaptureState.BUFFERING, 0.0, frames=5),
    ]
    beats += [
        Beat(CaptureState.BUFFERING, float(seconds), frames=2)
        for seconds in range(_SEGMENT_SECONDS, _WINDOW_SECONDS, _SEGMENT_SECONDS)
    ]
    beats += [
        Beat(CaptureState.BUFFERING, float(_WINDOW_SECONDS), frames=10),
        Beat(CaptureState.SAVING, float(_WINDOW_SECONDS), frames=10),
        Beat(CaptureState.BUFFERING, float(_WINDOW_SECONDS), frames=6, drop_clip=True),
        Beat(CaptureState.BUFFERING, float(_WINDOW_SECONDS), frames=8),
    ]
    return tuple(beats)


class ScriptedEngine:
    """CaptureEngine stand-in whose state and telemetry the script sets."""

    def __init__(self) -> None:
        self.state = CaptureState.IDLE
        self._buffered: float | None = None
        self._state_listeners: list[Callable[[CaptureState], None]] = []
        self._clip_listeners: list[Callable[[Path], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []

    def play(self, beat: Beat) -> None:
        self.state = beat.state
        self._buffered = beat.buffered

    def telemetry(self) -> BufferTelemetry | None:
        if self._buffered is None:
            return None
        segments = int(self._buffered // _SEGMENT_SECONDS)
        return BufferTelemetry(
            buffered_seconds=self._buffered,
            window_seconds=_WINDOW_SECONDS,
            segment_count=segments,
            segment_capacity=_SEGMENT_CAPACITY,
            bytes_on_disk=int(self._buffered * _BYTES_PER_SECOND),
        )

    def start_manual_recording(self) -> None: ...
    def stop_manual_recording(self) -> Path | None:
        return None

    def start_replay_buffer(self) -> None: ...
    def stop_replay_buffer(self) -> None: ...
    def save_replay_clip(self) -> None: ...
    def shutdown(self) -> None: ...

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        self._state_listeners.append(listener)

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        self._clip_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        self._error_listeners.append(listener)


class DemoStore:
    def __init__(self) -> None:
        self._settings = Settings(
            resolution="1920x1080",
            fps=60,
            encoder="h264_nvenc",
            preset="p5",
            crf=21,
            audio_input="Microphone (Realtek High Definition Audio)",
            replay_buffer=True,
            replay_seconds=_WINDOW_SECONDS,
            monitor="Monitor 1",
            clip_hotkey=Hotkey(key="F5"),
            record_hotkey=Hotkey(key="F6", ctrl=True),
        )

    def load(self) -> Settings:
        return self._settings.copy()

    def save(self, settings: Settings) -> None:
        self._settings = settings.copy()


class DemoDevices:
    def monitors(self) -> list[Monitor]:
        return [Monitor("Monitor 1", 0, 0, 1920, 1080, is_primary=True)]

    def audio_devices(self) -> list[AudioDevice]:
        return [AudioDevice("Microphone (Realtek High Definition Audio)", "input")]


class DemoHotkeys:
    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None: ...
    def unregister(self, hotkey: Hotkey) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


class DemoPaths:
    def __init__(self, clips_dir: Path) -> None:
        self.clips_dir = clips_dir
        self.assets_dir = _ROOT / "src" / "sclip" / "ui" / "assets"


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
            f"  FFmpeg exited {result.returncode}: {result.stderr.strip()[-300:]}",
            file=sys.stderr,
        )
        return False
    return True


def _encode_clip(
    ffmpeg: Path, destination: Path, *, seconds: int, colours: tuple[str, str]
) -> bool:
    """Encode one sample recording and its thumbnail, as the app would name it."""
    ok = _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                f"gradients=size=1280x720:rate=30:c0={colours[0]}:c1={colours[1]}"
                ":x0=120:y0=80:x1=1160:y1=640:nb_colors=2:speed=0.02"
            ),
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
            str(destination),
        ],
        timeout=120.0,
    )
    if not ok:
        return False
    _run_ffmpeg(
        ffmpeg,
        [
            "-ss",
            "1",
            "-i",
            str(destination),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-1",
            "-y",
            str(destination.with_suffix(destination.suffix + ".thumb.jpg")),
        ],
        timeout=60.0,
    )
    _backdate(destination)
    return True


def _backdate(clip: Path) -> None:
    """Match the file's mtime to the timestamp in its name."""
    try:
        _prefix, date_part, time_part = clip.stem.split("_", 2)
        moment = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return
    stamp = moment.timestamp()
    os.utime(clip, (stamp, stamp))
    thumb = clip.with_suffix(clip.suffix + ".thumb.jpg")
    if thumb.is_file():
        os.utime(thumb, (stamp, stamp))


def _capture_frames(
    app: QApplication,
    window: MainWindow,
    *,
    engine: ScriptedEngine,
    frame_dir: Path,
    staged_clip: Path | None,
    clips_dir: Path,
) -> int:
    """Walk the storyboard, writing one PNG per frame. Returns the frame count."""
    page = window._capture_page
    orb = getattr(page, "_orb", None)
    pill = getattr(page, "_status_pill", None)
    index = 0
    phase = 0.0

    # The page refreshes its live readout on a one-second timer. This script
    # drives every render itself, so the timer only adds a wall-clock-dependent
    # extra render that restarts the animations mid-frame -- which is exactly
    # what stops the output being reproducible.
    tick = getattr(page, "_tick_timer", None)
    if tick is not None:
        tick.stop()

    for beat in _storyboard():
        engine.play(beat)

        if beat.drop_clip and staged_clip is not None and staged_clip.is_file():
            # The finished clip lands in the watched folder, exactly as a real
            # save would leave it, and the page picks it up on its next scan.
            for source in (staged_clip, staged_clip.with_suffix(staged_clip.suffix + ".thumb.jpg")):
                if source.is_file():
                    shutil.move(str(source), str(clips_dir / source.name))
            refresh = getattr(page, "_refresh_recent_clips", None)
            if refresh is not None:
                refresh()

        for _ in range(beat.frames):
            render = getattr(page, "_render_state", None)
            if render is not None:
                render(engine.state)
            for _ in range(3):
                app.processEvents()

            # Both animations are normally driven by a QPropertyAnimation off
            # the wall clock, and frames are grabbed far faster than real time.
            # Rendering restarts them, so they are stopped and then advanced by
            # hand, last thing before the grab. The values come from the frame
            # index rather than the clock, which keeps the ring sweeping and the
            # status dot breathing while making the output reproducible.
            phase = (phase + 11.0) % 360.0
            _freeze_animations(orb, pill, phase=phase, index=index)
            window.grab().toImage().save(str(frame_dir / f"frame_{index:04d}.png"))
            index += 1

    return index


def _freeze_animations(orb: object, pill: object, *, phase: float, index: int) -> None:
    """Halt the running animations and set them to a frame-derived value.

    Stopping first matters: a live ``QPropertyAnimation`` would otherwise
    overwrite the value between here and the grab, on its own clock.
    """
    if orb is not None:
        for name in ("_phase_anim", "_pulse_anim"):
            animation = getattr(orb, name, None)
            if animation is not None:
                animation.stop()
        orb.setProperty("phase", phase)  # type: ignore[attr-defined]
        orb.setProperty("pulse", 0.5 + 0.5 * math.sin(index * 0.3))  # type: ignore[attr-defined]

    if pill is not None:
        animation = getattr(pill, "_animation", None)
        if animation is not None:
            animation.stop()
        pill.setProperty("_opacity", 0.65 + 0.35 * math.sin(index * 0.35))  # type: ignore[attr-defined]


def _encode_sample_library(ffmpeg: Path, *, clips_dir: Path, staging: Path) -> Path:
    """Encode the clips the library starts with, plus the one the save produces.

    The last one is written to ``staging`` rather than the watched folder so it
    can land mid-animation, the way a finished save actually appears.
    """
    for name, seconds, colours in (
        ("clip_2026-07-30_20-52-31.mp4", 30, ("0x141018", "0x6E4A8C")),
        ("recording_2026-07-30_20-05-17.mp4", 12, ("0x101613", "0x3F7A55")),
        ("clip_2026-07-29_23-41-55.mp4", 30, ("0x1A1410", "0x8C6A3A")),
    ):
        _encode_clip(ffmpeg, clips_dir / name, seconds=seconds, colours=colours)
        print(f"  encoded {name}")

    staged = staging / "clip_2026-07-30_21-14-08.mp4"
    _encode_clip(ffmpeg, staged, seconds=30, colours=("0x101B24", "0x2E6F8E"))
    print(f"  staged  {staged.name}")
    return staged


def _assemble_gif(ffmpeg: Path, *, frame_dir: Path, work_dir: Path, output: Path) -> None:
    """Turn the grabbed frames into a looping GIF.

    Two passes, because a GIF carries only 256 colours. Letting the encoder
    pick them per frame makes a mess of this interface — the flat dark
    surfaces band badly — so a palette is generated from the whole sequence
    first and then applied.
    """
    palette = work_dir / "palette.png"
    scale = f"scale={_GIF_WIDTH}:-1:flags=lanczos"
    pattern = str(frame_dir / "frame_%04d.png")

    if not _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-framerate",
            str(_FPS),
            "-i",
            pattern,
            "-vf",
            f"{scale},palettegen=stats_mode=diff",
            str(palette),
        ],
        timeout=180.0,
    ):
        raise SystemExit("palette generation failed")

    if not _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-framerate",
            str(_FPS),
            "-i",
            pattern,
            "-i",
            str(palette),
            "-lavfi",
            f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
            "-loop",
            "0",
            str(output),
        ],
        timeout=180.0,
    ):
        raise SystemExit("GIF assembly failed")


def render(output: Path) -> Path:
    """Render the animation and return the written GIF path."""
    try:
        ffmpeg = find_ffmpeg()
    except Exception as exc:
        raise SystemExit(f"FFmpeg is required to build the demo GIF: {exc}") from exc

    app = QApplication.instance() or QApplication([])
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sclip-gif-", dir=_ROOT) as temp_dir:
        temp = Path(temp_dir)
        clips_dir = temp / "clips"
        clips_dir.mkdir()
        staging = temp / "staging"
        staging.mkdir()
        frame_dir = temp / "frames"
        frame_dir.mkdir()

        print("Encoding sample recordings...")
        staged = _encode_sample_library(ffmpeg, clips_dir=clips_dir, staging=staging)

        demo_paths = DemoPaths(clips_dir)
        main_window_module.app_paths = lambda: demo_paths
        capture_page.app_paths = lambda: demo_paths
        library_page.app_paths = lambda: demo_paths
        settings_page.app_paths = lambda: demo_paths

        engine = ScriptedEngine()
        window = MainWindow(engine, DemoStore(), DemoDevices(), DemoHotkeys())
        window.resize(*_WINDOW)
        window.show()
        for _ in range(20):
            app.processEvents()

        print("Grabbing frames...")
        count = _capture_frames(
            app,
            window,
            engine=engine,
            frame_dir=frame_dir,
            staged_clip=staged,
            clips_dir=clips_dir,
        )
        print(f"  {count} frames")

        print("Assembling GIF...")
        _assemble_gif(ffmpeg, frame_dir=frame_dir, work_dir=temp, output=output)

        library = getattr(window, "_library_page", None)
        pool = getattr(library, "_pool", None)
        if pool is not None:
            pool.waitForDone(10_000)
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
            app.processEvents()

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"  wrote {_display_path(output)} ({size_mb:.1f} MB)")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_ROOT / "docs" / "assets" / "sclip-demo.gif",
    )
    args = parser.parse_args()
    render(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
