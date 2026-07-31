"""FFmpeg binary discovery, process plumbing, and capture command building.

The capture engine and the rolling replay buffer both lean on this module
for two things: spawning FFmpeg with the right plumbing, and turning the
application's capture settings into the argv FFmpeg actually wants.

A note on screen capture, because it is the whole reason this rewrite exists.
The previous version captured with ``gdigrab``, which scrapes the screen
through the GDI BitBlt path. At 1440p60 that path simply cannot keep up: it
delivers frames late and unevenly, so every saved clip judders. We now
capture with ``ddagrab`` instead - FFmpeg's wrapper around the Windows
Desktop Duplication API. It runs on the GPU, hands frames back at the true
refresh rate, and (with ``dup_frames``) emits perfectly constant-rate video.
``gdigrab`` is kept only as a fallback for the rare machine where Desktop
Duplication is unavailable, such as an RDP session.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from sclip.contracts import Monitor, encoder_by_codec
from sclip.core.process_guard import guard_child

logger = logging.getLogger(__name__)


# Windows console-suppression flag. Resolved once at module scope so tests on
# other platforms can still import this module without tripping over it.
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A generous audio queue keeps dshow from dropping samples when the video
# encoder briefly runs ahead of the audio thread.
_AUDIO_THREAD_QUEUE: str = "1024"

# AAC at this bitrate is transparent for a screen recording and, unlike the
# legacy MP3 track the old code wrote, sits cleanly inside both MP4 and the
# MPEG-TS segments the replay buffer rotates through. Public so the replay
# buffer's clip-stitch step encodes audio at the same bitrate as the live
# capture - one source of truth keeps the two paths in lockstep.
AUDIO_BITRATE: str = "192k"


class FFmpegNotFoundError(RuntimeError):
    """Raised when no FFmpeg binary can be located on this machine."""


class VideoBackend(str, Enum):
    """Which screen-capture path FFmpeg should use.

    ``DDAGRAB`` is the default and the reason clips are smooth. ``GDIGRAB``
    is the fallback the engine drops to only if Desktop Duplication refuses
    to start.
    """

    DDAGRAB = "ddagrab"
    GDIGRAB = "gdigrab"


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Resolved audio-capture configuration for one FFmpeg invocation.

    S-Clip can capture two distinct sources. The microphone is a DirectShow
    device, named straight from the settings. Desktop audio cannot come from
    DirectShow at all, so it arrives as raw PCM through a Windows named pipe
    that the desktop-audio pump fills (see :mod:`sclip.core.desktop_audio`);
    ``desktop_pipe`` is the path FFmpeg reads, and the rate and channel count
    describe the PCM on it.

    An empty ``microphone`` means no microphone; an empty ``desktop_pipe``
    means no desktop audio.
    """

    microphone: str = ""  # dshow microphone device; "" means disabled
    desktop_pipe: str = ""  # named pipe carrying desktop PCM; "" means disabled
    desktop_rate: int = 48000
    desktop_channels: int = 2

    @property
    def has_microphone(self) -> bool:
        return bool(self.microphone.strip())

    @property
    def has_desktop(self) -> bool:
        return bool(self.desktop_pipe.strip())

    @property
    def any_audio(self) -> bool:
        return self.has_microphone or self.has_desktop


@dataclass(frozen=True, slots=True)
class CapturePlan:
    """Everything FFmpeg needs to know to capture one screen plus its audio.

    Built by the capture engine from the live settings and a resolved
    :class:`~sclip.contracts.Monitor`. Kept immutable so a half-built plan can
    never leak into a running capture.
    """

    monitor: Monitor
    monitor_index: int  # zero-based DXGI output index for ddagrab
    fps: int
    encoder: str
    preset: str
    crf: int
    audio: AudioConfig


def _bundled_ffmpeg_candidates(root: Path, binary_name: str) -> list[Path]:
    """Every place a binary could sit under one root, best candidate first.

    Two shapes are supported deliberately. ``ffmpeg/`` (with or without a
    ``bin`` subdirectory, since the official zips differ) is where an install
    puts a deliberate copy, so it is checked first. ``ffmpeg-<version>-.../``
    is what dropping an official archive into a checkout leaves behind; the
    version used to be hardcoded here, which meant any release other than the
    one named went unnoticed. Versioned matches are sorted newest-first.
    """
    candidates = [
        root / "ffmpeg" / "bin" / binary_name,
        root / "ffmpeg" / binary_name,
    ]
    try:
        versioned = sorted(root.glob(f"ffmpeg-*/bin/{binary_name}"), reverse=True)
    except OSError as exc:  # unreadable directory; treat as "nothing here"
        logger.debug("Could not scan %s for a bundled FFmpeg: %s", root, exc)
        versioned = []
    candidates.extend(versioned)
    return candidates


def find_ffmpeg() -> Path:
    """Locate the FFmpeg binary, preferring a copy shipped alongside the app.

    A bundled distribution is checked before PATH so a portable install always
    uses the version it shipped with, rather than silently picking up whatever
    else the machine happens to have. A clear exception here beats a confusing
    failure deep inside a later Popen call.
    """
    binary_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # paths.app_paths() is imported lazily so a missing platformdirs install
    # cannot stop the rest of this module from importing.
    from sclip.paths import app_paths

    paths = app_paths()
    for root in (paths.project_root, paths.package_root):
        for candidate in _bundled_ffmpeg_candidates(root, binary_name):
            if candidate.is_file():
                logger.debug("Using bundled FFmpeg at %s", candidate)
                return candidate

    on_path = shutil.which("ffmpeg")
    if on_path is not None:
        logger.debug("Using FFmpeg from PATH at %s", on_path)
        return Path(on_path)

    raise FFmpegNotFoundError(
        "FFmpeg binary was not found. Install FFmpeg and make sure it is on "
        "PATH, or place a copy in an 'ffmpeg' folder beside S-Clip."
    )


# Some callers (the device registry, the about page) were written against an
# older name. The alias keeps them working without a second discovery routine.
get_ffmpeg_path = find_ffmpeg


def ffprobe_path() -> Path:
    """Locate ffprobe, which sits next to ffmpeg in every distribution."""
    ffmpeg = find_ffmpeg()
    probe = ffmpeg.with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    if probe.is_file():
        return probe
    on_path = shutil.which("ffprobe")
    if on_path is not None:
        return Path(on_path)
    raise FFmpegNotFoundError("ffprobe binary was not found next to ffmpeg.")


def popen_kwargs() -> dict[str, Any]:
    """Subprocess kwargs that suppress the console window on Windows.

    Used by every FFmpeg invocation, including the brief device-list probe -
    flashing a black console for a 200 ms call is just as ugly as flashing
    one for a long-running recording.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": _CREATE_NO_WINDOW}


def _argv_with_binary(binary: Path, args: Sequence[str]) -> list[str]:
    """Prepend the binary path and the standard quiet flags to a flag list.

    ``-loglevel error`` is not only tidy, it is load-bearing: a long capture
    runs with its stderr piped, and a chattier log level would fill the pipe
    buffer and wedge FFmpeg. At ``error`` a healthy capture stays silent.
    """
    return [str(binary), "-hide_banner", "-loglevel", "error", *args]


def run_ffmpeg(
    args: Sequence[str],
    *,
    binary: Path | None = None,
    timeout: float = 30.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg synchronously, capturing stdout and stderr as text.

    Suited to short-lived helpers such as the concat job. Long-running
    captures should use :func:`start_ffmpeg` instead.
    """
    ff = binary or find_ffmpeg()
    cmdline = _argv_with_binary(ff, args)
    logger.debug("Running FFmpeg synchronously: %s", " ".join(cmdline))
    return subprocess.run(
        cmdline,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
        **popen_kwargs(),
    )


def start_ffmpeg(
    args: Sequence[str],
    *,
    binary: Path | None = None,
) -> subprocess.Popen[str]:
    """Start a long-lived FFmpeg process and return the ``Popen`` handle.

    The returned process has its stdin wired to a pipe so the caller can
    write ``q`` to request a graceful shutdown, and its stderr piped so a
    failure can be diagnosed. stdout is discarded; ``bufsize=0`` ensures
    ``q`` reaches FFmpeg immediately without sitting in a write buffer.

    The caller owns the process lifecycle entirely. It must stop the process
    explicitly via :func:`stop_ffmpeg` when the capture is no longer needed.
    There is no automatic teardown: this function is a plain factory, not a
    context manager. That is intentional - a long-lived process that has its
    own graceful-stop protocol (``q`` → terminate → kill) does not benefit
    from automatic termination on scope exit, and forcing that model causes
    subtle bugs when the process is stored across scope boundaries.
    """
    ff = binary or find_ffmpeg()
    cmdline = _argv_with_binary(ff, args)
    logger.debug("Starting FFmpeg: %s", " ".join(cmdline))
    process = subprocess.Popen(
        cmdline,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=0,  # unbuffered: 'q' should reach FFmpeg the moment we send it
        **popen_kwargs(),
    )
    # The stop path below is careful, but it only runs if S-Clip lives long
    # enough to run it. Enrol the child so the kernel kills it if we are killed
    # outright; see sclip.core.process_guard for why that matters more than it
    # sounds.
    guard_child(process)
    return process


def stop_ffmpeg(process: subprocess.Popen[str], *, timeout: float = 8.0) -> int:
    """Ask FFmpeg to stop cleanly, escalating if it refuses.

    The graceful path writes ``q`` to stdin so FFmpeg finalises the file and
    exits, leaving a playable result. A wedged process is escalated through
    ``terminate`` and then ``kill``. Returns the eventual exit code, or
    ``-1`` if even kill could not collect it.
    """
    if process.poll() is not None:
        return process.returncode

    try:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.write("q\n")
            process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        logger.debug("Could not send 'q' to FFmpeg: %s", exc)

    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg did not honour 'q'; sending terminate")

    try:
        process.terminate()
        return process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg ignored terminate; sending kill")

    try:
        process.kill()
        return process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg refused even kill; leaving it dangling")
        return -1


def read_stderr_tail(process: subprocess.Popen[str], max_chars: int = 2000) -> str:
    """Drain whatever FFmpeg left on stderr without blocking forever.

    Handy once a process has exited and we want a one-line summary of what
    went wrong. Returns at most ``max_chars`` of trailing text so the log
    does not balloon when FFmpeg has been noisy.
    """
    if process.stderr is None:
        return ""
    try:
        data = process.stderr.read() or ""
    except (ValueError, OSError) as exc:
        logger.debug("Could not read FFmpeg stderr: %s", exc)
        return ""
    if len(data) <= max_chars:
        return data
    return "..." + data[-max_chars:]


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse a ``WIDTHxHEIGHT`` string with friendly errors.

    Invalid input becomes a clear ``ValueError`` so the GUI can show a
    sensible message rather than waiting for FFmpeg to fail mid-record.
    """
    parts = value.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"resolution must look like 1920x1080, got {value!r}")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"resolution must contain integers, got {value!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"resolution must be positive, got {value!r}")
    return width, height


def iter_argv_flat(parts: Iterable[Iterable[str] | str]) -> list[str]:
    """Flatten a sequence of argument chunks into a single argv list.

    Lets callers compose a command line out of named pieces without writing
    a nested comprehension every time.
    """
    flat: list[str] = []
    for part in parts:
        if isinstance(part, str):
            flat.append(part)
        else:
            flat.extend(part)
    return flat


def expected_segment_paths(directory: Path) -> list[Path]:
    """List the ``.ts`` segments on disk, oldest first.

    Sorting by modification time (rather than filename) handles the
    wrap-around case: once the segment muxer loops, ``seg_000.ts`` is the
    newest file even though its name sorts first.

    The mtime is read while the list is built rather than from inside the sort
    key, so a segment the muxer rotates away mid-scan is dropped instead of
    raising. Both the save path and the once-a-second telemetry poll come
    through here, and the poll meets that race often enough that an unguarded
    ``stat`` would eventually take down a clip save.
    """
    if not directory.exists():
        return []
    dated: list[tuple[float, Path]] = []
    for path in directory.iterdir():
        if path.suffix != ".ts" or not path.is_file():
            continue
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            # Rotated away between the listing and the stat. It is no longer
            # part of the buffer, so leaving it out is the correct answer.
            continue
    dated.sort(key=lambda pair: pair[0])
    return [path for _mtime, path in dated]


def remove_quietly(path: Path) -> None:
    """Delete a file if it exists, swallowing benign errors.

    The replay buffer tidies its temp files on stop; if another process
    still holds a handle we would rather log and move on than crash the
    engine's shutdown sequence.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)


def build_quality_args(codec: str, crf: int) -> list[str]:
    """Translate the user's quality slider into encoder-appropriate flags.

    Each encoder family interprets quality differently. libx264 speaks
    ``-crf``; NVENC ignores CRF entirely and wants ``-cq``; AMF takes a
    constant QP; Intel QSV uses ``-global_quality``. Picking the right knob
    keeps the slider behaving consistently from the user's point of view.
    """
    spec = encoder_by_codec(codec)
    if spec is None:
        logger.warning("Unknown encoder %r; assuming libx264-style CRF", codec)
        return ["-crf", str(crf)]

    if codec == "libx264":
        return ["-crf", str(crf)]
    if codec.endswith("_nvenc"):
        # VBR with a quality target and no bitrate cap: NVENC then chases the
        # requested quality rather than a fixed bitrate.
        return ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if codec.endswith("_amf"):
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    if codec.endswith("_qsv"):
        return ["-global_quality", str(crf)]
    return ["-crf", str(crf)]


def encoder_is_gpu_native(encoder: str) -> bool:
    """True when the encoder can consume ddagrab's GPU frames without a copy.

    ddagrab hands back Direct3D 11 surfaces. NVENC accepts them directly, so
    the whole capture-to-encode path stays on the GPU. Every other encoder
    needs the frames pulled into system memory first, which we arrange with
    a ``hwdownload`` filter.
    """
    return encoder.endswith("_nvenc")


def build_encoder_args(
    plan: CapturePlan,
    *,
    keyframe_seconds: int,
    force_keyframes: bool,
    frames_on_gpu: bool,
) -> list[str]:
    """Build the video-encoder portion of an FFmpeg command line.

    ``keyframe_seconds`` sets the GOP length. For the replay buffer it equals
    the segment length and ``force_keyframes`` is on, which pins a keyframe
    to the start of every segment - that is what lets the segments be
    stitched back together without a judder at each five-second boundary.

    ``-bf 0`` disables B-frames everywhere. B-frames reference future frames,
    which would reach across a segment cut and corrupt the join; banning them
    keeps every segment a self-contained, cleanly decodable unit.
    """
    gop = max(1, plan.fps * keyframe_seconds)
    args: list[str] = ["-c:v", plan.encoder, "-preset", plan.preset]

    # NVENC's "hq" tuning favours quality over latency, which is the right
    # trade for a recorder (a replay buffer is not a live stream).
    if plan.encoder.endswith("_nvenc"):
        args += ["-tune", "hq"]

    args += build_quality_args(plan.encoder, plan.crf)
    args += [
        "-g",
        str(gop),
        "-bf",
        "0",
        "-fps_mode",
        "cfr",  # force constant frame rate on the output as well as the input
    ]
    if force_keyframes:
        # Place a keyframe exactly on every segment boundary. Combined with
        # the segment muxer's own keyframe-aligned cutting, this gives clean,
        # independently decodable segments.
        args += ["-force_key_frames", f"expr:gte(t,n_forced*{keyframe_seconds})"]

    if not frames_on_gpu:
        # System-memory encoders need an explicit, widely compatible pixel
        # format. NVENC working straight off the GPU surface does not.
        args += ["-pix_fmt", "yuv420p"]
    return args


def _ddagrab_video_chain(plan: CapturePlan, *, label: str) -> str:
    """Build the ddagrab filter chain that produces the video stream.

    ``dup_frames`` is left at its default of on, so ddagrab repeats the last
    frame whenever the desktop has not changed. That is what guarantees a
    genuinely constant frame rate even while the screen is static.

    A CPU encoder cannot read the Direct3D surfaces ddagrab emits, so for
    those we append ``hwdownload`` to copy each frame into system memory.
    """
    chain = f"ddagrab=output_idx={plan.monitor_index}:framerate={plan.fps}:draw_mouse=1"
    if not encoder_is_gpu_native(plan.encoder):
        chain += ",hwdownload,format=bgra"
    return f"{chain}[{label}]"


def _amix_chain(audio_indices: Sequence[int], *, label: str) -> str:
    """Mix two audio inputs into a single track.

    Most editors and players handle a single audio track far more gracefully
    than parallel ones, so when the user captures both a microphone and the
    system output we fold them together with ``amix``.
    """
    inputs = "".join(f"[{index}:a]" for index in audio_indices)
    return (
        f"{inputs}amix=inputs={len(audio_indices)}:duration=longest:dropout_transition=0[{label}]"
    )


def _gdigrab_input(plan: CapturePlan) -> list[str]:
    """Build the legacy gdigrab input block, pinned to one monitor.

    Only reached when Desktop Duplication is unavailable. ``-framerate`` is an
    input option here because gdigrab needs the rate before it starts
    grabbing.
    """
    monitor = plan.monitor
    return [
        "-f",
        "gdigrab",
        "-framerate",
        str(plan.fps),
        "-offset_x",
        str(monitor.x),
        "-offset_y",
        str(monitor.y),
        "-video_size",
        f"{monitor.width}x{monitor.height}",
        "-draw_mouse",
        "1",
        "-i",
        "desktop",
    ]


def _build_audio_inputs(audio: AudioConfig, first_index: int) -> tuple[list[str], list[int]]:
    """Build audio ``-i`` blocks, returning argv and a list of stream indices."""
    argv: list[str] = []
    indices: list[int] = []
    next_index = first_index

    if audio.has_microphone:
        argv += [
            "-f",
            "dshow",
            "-thread_queue_size",
            _AUDIO_THREAD_QUEUE,
            "-i",
            f"audio={audio.microphone}",
        ]
        indices.append(next_index)
        next_index += 1
    if audio.has_desktop:
        # Desktop audio is raw PCM the desktop-audio pump streams through a
        # named pipe; FFmpeg needs to be told the format up front.
        argv += [
            "-f",
            "s16le",
            "-ar",
            str(audio.desktop_rate),
            "-ac",
            str(audio.desktop_channels),
            "-thread_queue_size",
            _AUDIO_THREAD_QUEUE,
            "-i",
            audio.desktop_pipe,
        ]
        indices.append(next_index)

    return argv, indices


def build_capture_io(
    plan: CapturePlan,
    *,
    backend: VideoBackend,
    keyframe_seconds: int,
    force_keyframes: bool,
) -> list[str]:
    """Build the FFmpeg command line from the global flags up to the codecs.

    The returned argv covers everything except the output target: the manual
    recorder appends an MP4 path, and the replay buffer appends the segment
    muxer. Splitting it this way keeps the single source of truth for the
    capture pipeline here while letting each caller own its output format.

    The input layout differs by backend. ddagrab is a source *filter* with no
    ``-i`` of its own, so audio inputs take index 0 upward. gdigrab is a real
    input at index 0, pushing audio to index 1 upward.
    """
    argv: list[str] = ["-y"]

    # --- inputs ----------------------------------------------------------
    if backend is VideoBackend.GDIGRAB:
        argv += _gdigrab_input(plan)
        first_audio_index = 1
    else:
        first_audio_index = 0

    # Audio inputs are added in a fixed order - microphone, then desktop - so
    # the indices the filter graph and the maps reference stay predictable.
    audio_argv, audio_indices = _build_audio_inputs(plan.audio, first_audio_index)
    argv += audio_argv

    # --- filter graph ----------------------------------------------------
    graph_parts: list[str] = []
    if backend is VideoBackend.DDAGRAB:
        graph_parts.append(_ddagrab_video_chain(plan, label="v"))

    mixed_audio_label: str | None = None
    if len(audio_indices) >= 2:
        mixed_audio_label = "a"
        graph_parts.append(_amix_chain(audio_indices, label=mixed_audio_label))

    if graph_parts:
        argv += ["-filter_complex", ";".join(graph_parts)]

    # --- stream mapping --------------------------------------------------
    if backend is VideoBackend.DDAGRAB:
        argv += ["-map", "[v]"]
    else:
        argv += ["-map", "0:v"]

    if mixed_audio_label is not None:
        argv += ["-map", f"[{mixed_audio_label}]"]
    elif len(audio_indices) == 1:
        argv += ["-map", f"{audio_indices[0]}:a"]

    # --- codecs ----------------------------------------------------------
    frames_on_gpu = backend is VideoBackend.DDAGRAB and encoder_is_gpu_native(plan.encoder)
    argv += build_encoder_args(
        plan,
        keyframe_seconds=keyframe_seconds,
        force_keyframes=force_keyframes,
        frames_on_gpu=frames_on_gpu,
    )
    if audio_indices:
        argv += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    else:
        argv += ["-an"]

    return argv


__all__ = [
    "AUDIO_BITRATE",
    "AudioConfig",
    "CapturePlan",
    "FFmpegNotFoundError",
    "VideoBackend",
    "build_capture_io",
    "build_encoder_args",
    "build_quality_args",
    "encoder_is_gpu_native",
    "expected_segment_paths",
    "ffprobe_path",
    "find_ffmpeg",
    "get_ffmpeg_path",
    "iter_argv_flat",
    "parse_resolution",
    "popen_kwargs",
    "read_stderr_tail",
    "remove_quietly",
    "run_ffmpeg",
    "start_ffmpeg",
    "stop_ffmpeg",
]
