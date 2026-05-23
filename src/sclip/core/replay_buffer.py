"""A genuine rolling replay buffer backed by FFmpeg's segment muxer.

The previous version of S-Clip cheated: pressing the clip hotkey just kicked
off a fresh 30-second recording, so the "last 30 seconds" feature was really
"the next 30 seconds". This module fixes that by keeping a long-running
FFmpeg process that continuously writes short ``.ts`` segments to a temp
directory and rotates them with ``-segment_wrap``.

When the user asks for a clip, the finished segments are stitched into one
MP4. That stitch is a re-encode rather than a stream copy, and the choice is
deliberate: copying the segments verbatim leaves a faint timing seam at every
join, because each segment's audio track makes its container a hair longer
than its video. Re-encoding lays down a single, unbroken constant-rate
timeline, so the saved clip plays back perfectly smoothly. The cost is a few
seconds of encoding when a clip is saved, which is a fair price for a clip
that never judders.

Two FFmpeg processes can therefore exist at once: the rolling producer and a
short-lived stitch job. The producer is never interrupted while a clip is
being saved, so the user does not miss the next few seconds of action.
"""

from __future__ import annotations

import contextlib
import logging
import math
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sclip.core.ffmpeg import (
    _AUDIO_BITRATE,
    build_quality_args,
    expected_segment_paths,
    iter_argv_flat,
    read_stderr_tail,
    remove_quietly,
    run_ffmpeg,
    start_ffmpeg,
    stop_ffmpeg,
)

logger = logging.getLogger(__name__)


# How many seconds each segment lasts. On save we discard the segment FFmpeg
# is still writing (see _snapshot_segments_locked), so the saved clip ends up
# to SEGMENT_SECONDS short of "now". Keeping segments brief keeps that gap
# small; two seconds loses at most a moment off the tail while still giving
# the encoder a sensible keyframe interval.
SEGMENT_SECONDS: int = 2

# Cap on how many concat retries to attempt. One retry is enough to dodge
# the rare race where the segment muxer is halfway through writing the
# newest segment when we list the directory.
_CONCAT_RETRIES: int = 1

# How long to wait after the muxer finishes rotating a segment before we
# trust the file to be safe for concat. Tuned to be comfortably less than
# SEGMENT_SECONDS so a retry still completes in well under a second.
_CONCAT_RETRY_DELAY: float = 0.5


@dataclass(frozen=True, slots=True)
class BufferSpec:
    """The wiring needed to spawn the rolling muxer for one session.

    Keeping these together (rather than passing a fistful of positional
    arguments to ``start``) means the engine can build the spec once and pass
    it through even if the user changes settings mid-session — the buffer
    keeps the original wiring until the next explicit restart.

    ``encoder``, ``preset`` and ``crf`` describe how the save-time stitch
    should re-encode the clip; they mirror the settings the capture itself
    used so a saved clip matches the buffered footage.
    """

    capture_args: Sequence[str]  # everything before the segment-muxer flags
    directory: Path
    seconds: int
    encoder: str = "libx264"
    preset: str = "veryfast"
    crf: int = 20
    segment_seconds: int = SEGMENT_SECONDS

    @property
    def segment_wrap(self) -> int:
        """How many segment slots the muxer is allowed to rotate through.

        ``segment_wrap=N`` means FFmpeg writes ``seg_000.ts`` ... ``seg_(N-1).ts``
        and then loops back to overwrite ``seg_000.ts``. At any instant one
        slot holds the segment being written and the rest hold finished
        segments. Since the save step discards that in-progress segment, we
        need ``ceil(seconds / segment_seconds)`` finished slots plus the one
        in-progress slot — hence the ``+ 1``.
        """
        slots = math.ceil(self.seconds / self.segment_seconds) + 1
        return max(2, slots)

    @property
    def pattern(self) -> Path:
        """Filename template the muxer writes through."""
        return self.directory / "seg_%03d.ts"


def build_segment_args(spec: BufferSpec) -> list[str]:
    """Compose the FFmpeg argv tail that turns a capture into a rolling buffer.

    The capture portion (the ddagrab filter, audio inputs, the encoder, ...)
    lives in ``spec.capture_args``. We append the segment muxer options here.

    MPEG-TS is the segment format on purpose: a TS stream is self-describing
    and concatenates byte-for-byte, where rolling MP4 segments would each need
    their own moov atom and could not be stitched together cleanly.
    ``-reset_timestamps 1`` restarts each segment's clock at zero so the
    concat demuxer can re-base them without the timeline drifting.
    """
    return iter_argv_flat(
        [
            spec.capture_args,
            [
                "-f",
                "segment",
                "-segment_time",
                str(spec.segment_seconds),
                "-segment_wrap",
                str(spec.segment_wrap),
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
                str(spec.pattern),
            ],
        ]
    )


class RollingBuffer:
    """Owns the long-running FFmpeg process that maintains the replay window.

    Thread safety: every public method takes ``_lock`` (a re-entrant lock so
    a handler that wants to restart the buffer can call ``stop`` and then
    ``start`` from inside the same critical section). The lock guards both
    the process handle and the bookkeeping flags.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._spec: BufferSpec | None = None
        self._on_error: Callable[[str], None] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def set_error_handler(self, handler: Callable[[str], None] | None) -> None:
        """Register a callback for non-fatal errors that occur in the background.

        Used to surface concat failures to the GUI without blowing up the
        engine state machine.
        """
        with self._lock:
            self._on_error = handler

    def start(self, spec: BufferSpec) -> None:
        """Spawn the rolling muxer with the supplied capture wiring.

        Idempotent against the same spec: if the buffer is already running
        we leave it alone. If the spec has changed (different monitor,
        audio device, etc.) we restart so the new clip matches the live
        capture configuration.
        """
        with self._lock:
            if self.is_running:
                if self._spec == spec:
                    logger.debug("Replay buffer already running with same spec; no-op")
                    return
                logger.info("Replay buffer spec changed; restarting")
                self._stop_locked()

            self._directory.mkdir(parents=True, exist_ok=True)
            self._purge_segments_locked()

            argv = build_segment_args(spec)
            logger.info(
                "Starting replay buffer: seconds=%s segments=%s slots=%s dir=%s",
                spec.seconds,
                spec.segment_seconds,
                spec.segment_wrap,
                self._directory,
            )

            self._process = start_ffmpeg(argv)
            self._spec = spec

            # If the process exits within a few hundred ms it almost
            # certainly failed to start (wrong audio device, missing
            # codec, etc.). Catch that loudly so the GUI can react.
            self._poll_for_early_exit_locked()

    def stop(self) -> None:
        """Stop the rolling muxer and tidy up the segments on disk."""
        with self._lock:
            self._stop_locked()
            self._purge_segments_locked()

    def save_clip(self, destination: Path) -> Path | None:
        """Stitch the finished segments into a single, smooth MP4.

        Returns the path to the written file, or ``None`` if there was
        nothing to save. Safe to call while the rolling muxer keeps running:
        the stitch runs in its own short-lived FFmpeg process and only reads
        the segments, never the muxer's live output.
        """
        with self._lock:
            if not self.is_running:
                logger.warning("save_clip called while replay buffer is not running")
                return None

            # Snapshot the segment list under the lock so concurrent
            # rotation cannot reshuffle it underneath us.
            segments = self._snapshot_segments_locked()

        if not segments:
            logger.warning("Replay buffer has no segments yet; nothing to save")
            return None

        destination.parent.mkdir(parents=True, exist_ok=True)

        for attempt in range(_CONCAT_RETRIES + 1):
            list_file = self._write_concat_list(segments)
            try:
                ok = self._run_concat(list_file, destination)
            finally:
                remove_quietly(list_file)

            if ok:
                logger.info("Replay clip saved: %s", destination)
                return destination

            if attempt < _CONCAT_RETRIES:
                logger.warning(
                    "Concat attempt %d failed; retrying after %.2fs",
                    attempt + 1,
                    _CONCAT_RETRY_DELAY,
                )
                time.sleep(_CONCAT_RETRY_DELAY)
                # Re-snapshot segments — the rolling muxer might have
                # rotated a slot we were about to read.
                with self._lock:
                    segments = self._snapshot_segments_locked()
                if not segments:
                    break

        self._notify_error("Failed to stitch the replay buffer into a clip")
        return None

    # --- internals -------------------------------------------------------

    def _stop_locked(self) -> None:
        """Stop the muxer assuming we already hold the lock."""
        process = self._process
        if process is None:
            return

        self._process = None
        self._spec = None

        if process.poll() is None:
            exit_code = stop_ffmpeg(process)
            logger.info("Replay buffer FFmpeg exited with code %s", exit_code)
        else:
            logger.debug("Replay buffer FFmpeg had already exited (code %s)", process.returncode)

    def _purge_segments_locked(self) -> None:
        """Delete every leftover ``.ts`` segment in the buffer directory."""
        if not self._directory.exists():
            return
        for segment in self._directory.iterdir():
            if segment.suffix == ".ts" and segment.is_file():
                remove_quietly(segment)

    def _snapshot_segments_locked(self) -> list[Path]:
        """Return the finished segments on disk, oldest first.

        The newest segment is the one the muxer is still actively writing to.
        Copying a half-written segment leaves a corrupt frame at the join — a
        visible glitch — so it is dropped here. The cost is losing up to
        ``SEGMENT_SECONDS`` of the most recent footage, which is exactly why
        the segments are kept short.

        If only one segment exists yet (the buffer has only just started) we
        keep it: a slightly rough clip beats refusing to save anything.
        """
        segments = expected_segment_paths(self._directory)
        if len(segments) > 1:
            return segments[:-1]
        return segments

    def _poll_for_early_exit_locked(self) -> None:
        """Check for instant FFmpeg failure and raise a sensible error.

        Run inside the lock immediately after spawn. We give the muxer a very
        short grace window — long enough for it to die on misuse but not long
        enough that the user notices the startup latency.

        The error is raised, not pushed through ``_on_error``: the caller owns
        the fallback decision (ddagrab failing is expected on some machines
        and triggers a quiet retry with gdigrab), so surfacing it here would
        flash a spurious error before the retry even runs.
        """
        process = self._process
        if process is None:
            return
        try:
            exit_code = process.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            # Healthy: it is still running and busy capturing.
            return

        tail = read_stderr_tail(process)
        self._process = None
        self._spec = None
        # The process has already exited, but defensively ensure it is fully
        # reaped before surfacing the error. kill() on an already-dead process
        # is a no-op on all major platforms.
        with contextlib.suppress(OSError):
            process.kill()
        message = (
            f"Replay buffer FFmpeg exited immediately (code {exit_code}). "
            f"FFmpeg said: {tail.strip() or '<no output>'}"
        )
        logger.error(message)
        raise RuntimeError(message)

    def _write_concat_list(self, segments: list[Path]) -> Path:
        """Generate the concat-demuxer manifest for the supplied segments.

        Returns the temp file path. The format is FFmpeg's plain-text
        concat protocol: ``file '<path>'`` per line, with single quotes
        around the path so spaces survive intact.
        """
        list_file = self._directory / "concat.txt"
        with list_file.open("w", encoding="utf-8") as handle:
            for segment in segments:
                # FFmpeg's concat demuxer expects forward slashes or
                # escaped backslashes. ``as_posix`` keeps it portable.
                handle.write(f"file '{segment.as_posix()}'\n")
        return list_file

    def _run_concat(self, list_file: Path, destination: Path) -> bool:
        """Stitch the listed segments into one MP4; return True on success.

        The segments are fed through the concat demuxer and re-encoded into a
        fresh stream. ``-fps_mode cfr`` lays down an exactly constant frame
        rate, which is what removes the faint timing seam a plain stream copy
        leaves at each segment join. The encoder, preset and quality mirror
        the capture settings so the saved clip looks like the buffered
        footage rather than a degraded copy of it.
        """
        spec = self._spec
        if spec is None:
            logger.error("Cannot stitch a clip without an active buffer spec")
            return False

        tune_args = ["-tune", "hq"] if spec.encoder.endswith("_nvenc") else []
        argv = [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:v",
            spec.encoder,
            "-preset",
            spec.preset,
            *tune_args,
            *build_quality_args(spec.encoder, spec.crf),
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            _AUDIO_BITRATE,
            # ``faststart`` puts the moov atom at the front so the resulting
            # MP4 is seekable straight from disk and uploadable to most
            # services without a remux.
            "-movflags",
            "+faststart",
            str(destination),
        ]
        try:
            # A long buffer means a long re-encode; the timeout has to comfortably
            # cover stitching the largest window the user could have configured.
            result = run_ffmpeg(argv, timeout=300.0)
        except subprocess.TimeoutExpired:
            logger.error("Clip stitch job timed out")
            return False
        if result.returncode != 0:
            logger.error(
                "Clip stitch failed (code %s): %s", result.returncode, result.stderr.strip()
            )
            return False
        return destination.exists() and destination.stat().st_size > 0

    def _notify_error(self, message: str) -> None:
        with self._lock:
            handler = self._on_error
        if handler is not None:
            try:
                handler(message)
            except Exception:
                logger.exception("Replay buffer error handler raised")


__all__ = [
    "SEGMENT_SECONDS",
    "BufferSpec",
    "RollingBuffer",
    "build_segment_args",
]
