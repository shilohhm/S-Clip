"""Tests for the rolling replay buffer in :mod:`sclip.core.replay_buffer`.

These tests drive :class:`RollingBuffer` against the fake FFmpeg binary from
:mod:`tests.conftest`, so they exercise the real process plumbing without
actually capturing a desktop. They are marked ``slow`` because each test
spawns a subprocess and waits long enough for the fake to emit a couple of
``.ts`` segments - easily under a second each, but slower than the pure
in-memory tests in the rest of the suite.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sclip.core.replay_buffer import BufferSpec, RollingBuffer

# How long we let the fake FFmpeg buffer run before we look for segments.
# The fake writes its segments synchronously on startup, so this is mostly a
# guard against scheduler jitter - half a second is plenty.
_WARMUP_SECONDS: float = 0.6


def _make_spec(directory: Path) -> BufferSpec:
    """Build a buffer spec with empty capture args.

    The real capture args (gdigrab, dshow, encoder) are irrelevant here -
    the fake binary ignores anything it does not recognise. Keeping the
    spec minimal makes the tests easier to read.
    """
    return BufferSpec(capture_args=(), directory=directory, seconds=30)


@pytest.fixture()
def patched_ffmpeg(install_fake_ffmpeg: Path) -> Path:
    """Re-export :data:`install_fake_ffmpeg` under the name the tests use.

    The actual wiring (patching ``find_ffmpeg`` and ``_argv_with_binary`` so
    invocations route through the host Python interpreter) lives in
    :func:`tests.conftest.install_fake_ffmpeg`.
    """
    return install_fake_ffmpeg


@pytest.fixture()
def buffer_dir(tmp_path: Path) -> Path:
    """A scratch directory the buffer uses for ``.ts`` segments."""
    target = tmp_path / "replay_buffer"
    target.mkdir()
    return target


@pytest.fixture()
def clips_dir(tmp_path: Path) -> Path:
    """A scratch directory where saved clips are written."""
    target = tmp_path / "clips"
    target.mkdir()
    return target


# ----------------------------------------------------------------- lifecycle


@pytest.mark.slow
def test_start_buffer_produces_segment_files(
    patched_ffmpeg: Path,
    buffer_dir: Path,
) -> None:
    buffer = RollingBuffer(buffer_dir)
    spec = _make_spec(buffer_dir)
    try:
        buffer.start(spec)
        # Give the fake a moment to emit its placeholder segments.
        time.sleep(_WARMUP_SECONDS)

        segments = sorted(buffer_dir.glob("seg_*.ts"))
        assert segments, "Expected at least one .ts segment to be produced"
        assert all(p.stat().st_size > 0 for p in segments)
    finally:
        buffer.stop()


@pytest.mark.slow
def test_save_replay_clip_writes_file_into_clips_dir(
    patched_ffmpeg: Path,
    buffer_dir: Path,
    clips_dir: Path,
) -> None:
    buffer = RollingBuffer(buffer_dir)
    spec = _make_spec(buffer_dir)
    try:
        buffer.start(spec)
        time.sleep(_WARMUP_SECONDS)

        destination = clips_dir / "clip.mp4"
        result = buffer.save_clip(destination)

        assert result == destination
        assert destination.exists()
        assert destination.stat().st_size > 0
        # Sanity: the clip must live inside the clips directory we provided.
        assert destination.parent == clips_dir
    finally:
        buffer.stop()


@pytest.mark.slow
def test_stop_terminates_buffer_within_timeout(
    patched_ffmpeg: Path,
    buffer_dir: Path,
) -> None:
    buffer = RollingBuffer(buffer_dir)
    spec = _make_spec(buffer_dir)
    buffer.start(spec)
    time.sleep(_WARMUP_SECONDS)
    assert buffer.is_running

    deadline = time.monotonic() + 10.0
    buffer.stop()
    elapsed = time.monotonic() - (deadline - 10.0)

    assert not buffer.is_running
    # The graceful stop path waits up to 8s for FFmpeg to honour 'q'; our
    # fake responds within milliseconds, so the elapsed time should be far
    # below that ceiling. We assert generously to stay reliable on slow CI.
    assert elapsed < 8.0


@pytest.mark.slow
def test_start_is_idempotent_with_same_spec(
    patched_ffmpeg: Path,
    buffer_dir: Path,
) -> None:
    """Calling ``start`` twice with the same spec should not spawn a second process."""
    buffer = RollingBuffer(buffer_dir)
    spec = _make_spec(buffer_dir)
    try:
        buffer.start(spec)
        time.sleep(_WARMUP_SECONDS)
        first_pid = buffer._process.pid if buffer._process else None
        assert first_pid is not None

        # Second start with the same spec - should be a no-op.
        buffer.start(spec)
        second_pid = buffer._process.pid if buffer._process else None

        assert second_pid == first_pid, "Second start spawned a new process"
    finally:
        buffer.stop()


@pytest.mark.slow
def test_save_clip_returns_none_when_buffer_not_running(
    patched_ffmpeg: Path,
    buffer_dir: Path,
    clips_dir: Path,
) -> None:
    buffer = RollingBuffer(buffer_dir)
    # Deliberately do not start the buffer.
    result = buffer.save_clip(clips_dir / "clip.mp4")
    assert result is None


# ---------------------------------------------------------------- pure helpers


def test_buffer_spec_segment_wrap_uses_ceiling_plus_one() -> None:
    """``segment_wrap`` is ceil(seconds / segment_seconds) + 1 with a floor of 2."""
    # 30s window, 5s per segment -> ceil(30/5) + 1 = 7 slots.
    spec = BufferSpec(capture_args=(), directory=Path("/tmp/x"), seconds=30, segment_seconds=5)
    assert spec.segment_wrap == 7

    # 12s window, 5s per segment -> ceil(12/5) + 1 = 4 slots.
    spec2 = BufferSpec(capture_args=(), directory=Path("/tmp/x"), seconds=12, segment_seconds=5)
    assert spec2.segment_wrap == 4

    # 1s window, 5s per segment -> ceil(1/5) + 1 = 2 slots (floor enforced).
    spec3 = BufferSpec(capture_args=(), directory=Path("/tmp/x"), seconds=1, segment_seconds=5)
    assert spec3.segment_wrap == 2


def test_buffer_spec_pattern_includes_directory_and_template() -> None:
    spec = BufferSpec(capture_args=(), directory=Path("/tmp/x"), seconds=30)
    # The pattern lives inside the directory and uses the FFmpeg ``%03d`` template.
    assert spec.pattern.name == "seg_%03d.ts"
    assert spec.pattern.parent == Path("/tmp/x")


def test_build_segment_args_appends_muxer_flags() -> None:
    from sclip.core.replay_buffer import build_segment_args

    spec = BufferSpec(
        capture_args=["-f", "lavfi", "-i", "anullsrc"],
        directory=Path("/tmp/x"),
        seconds=30,
        segment_seconds=5,
    )
    argv = build_segment_args(spec)

    # The capture args come first, untouched.
    assert argv[:4] == ["-f", "lavfi", "-i", "anullsrc"]
    # The muxer block follows.
    assert "-f" in argv and "segment" in argv
    assert "-segment_time" in argv
    assert str(spec.segment_seconds) in argv
    assert "-segment_wrap" in argv
    assert str(spec.segment_wrap) in argv
    assert argv[-1] == str(spec.pattern)


# -------------------------------------------------------------------- telemetry
# These drive the real segment-scanning code against real files on disk, but
# stand in a fake process for the FFmpeg muxer so they stay fast enough to run
# in the default (non-slow) suite.


class _FakeProcess:
    """Minimal stand-in for a live FFmpeg process.

    :meth:`RollingBuffer.is_running` only asks for ``poll()``; returning
    ``None`` means "still running", which is all telemetry needs.
    """

    def __init__(self, *, alive: bool = True) -> None:
        self.returncode: int | None = None if alive else 0

    def poll(self) -> int | None:
        return self.returncode


def _write_segments(directory: Path, count: int, *, size: int = 1024) -> list[Path]:
    """Write ``count`` fake ``.ts`` segments with strictly increasing mtimes.

    ``expected_segment_paths`` orders segments by modification time so the
    wrap-around case sorts correctly. Files written back-to-back can land on
    the same timestamp - especially on Windows, whose filesystem timestamp
    granularity is coarse - so the mtimes are set explicitly. Without this the
    "newest segment" the snapshot discards would be arbitrary and the test
    would flake.
    """
    written: list[Path] = []
    for index in range(count):
        segment = directory / f"seg_{index:03d}.ts"
        segment.write_bytes(b"\0" * size)
        os.utime(segment, (1_000_000 + index, 1_000_000 + index))
        written.append(segment)
    return written


def _stat_vanishing_after_listing(doomed: Path) -> Callable[..., os.stat_result]:
    """A ``Path.stat`` replacement that models one segment rotating away.

    The error carries ``ENOENT`` deliberately. :meth:`pathlib.Path.is_file`
    inspects ``errno`` to decide whether to swallow an error or re-raise it, so
    a ``FileNotFoundError`` built from a bare message - which leaves ``errno``
    as ``None`` - escapes ``is_file`` instead of being treated as "absent".
    How much that matters varies by Python version: 3.10 calls ``stat()`` with
    no arguments from ``is_file``, 3.13 passes ``follow_symlinks``. Raising a
    faithful ENOENT makes the fake behave like a genuinely deleted file on
    every version rather than only the one it was written on.
    """
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == doomed:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    return flaky_stat


def _running_buffer(
    directory: Path, *, seconds: int = 30, segment_seconds: int = 2
) -> RollingBuffer:
    """A buffer that believes it is running, without spawning FFmpeg."""
    buffer = RollingBuffer(directory)
    buffer._process = _FakeProcess()  # type: ignore[assignment]
    buffer._spec = BufferSpec(
        capture_args=(),
        directory=directory,
        seconds=seconds,
        segment_seconds=segment_seconds,
    )
    return buffer


def test_a_clip_never_includes_segments_from_a_previous_session(buffer_dir: Path) -> None:
    """Leftovers from an earlier capture must not be stitched into a new clip.

    An FFmpeg orphaned by a crash keeps writing into this directory, so purging
    on start cannot be relied on alone: the files may be held open, or arrive
    after the purge. They sort in by modification time like any other segment,
    which means a save would hand back footage from a session the user believed
    had ended. This is the regression test for that.
    """
    stale = _write_segments(buffer_dir, 3)  # survivors of a failed purge
    buffer = _running_buffer(buffer_dir)
    buffer._remember_survivors_locked()

    # Now this session writes its own, which are newer than the session start.
    fresh = []
    for index in range(3, 6):
        segment = buffer_dir / f"seg_{index:03d}.ts"
        segment.write_bytes(b"\0" * 2048)
        fresh.append(segment)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    # Three fresh segments, newest dropped as still-being-written, leaves two.
    assert telemetry.segment_count == 2
    assert telemetry.bytes_on_disk == 2 * 2048
    assert all(path.exists() for path in stale), "stale files should be ignored, not deleted"


def test_the_window_is_not_exceeded_by_leftover_segments(buffer_dir: Path) -> None:
    """A 20s window must not report 26s because old files were counted.

    This is the shape of the bug seen in real capture: the ring holds eleven
    slots, yet telemetry reported thirteen segments and the saved clip ran six
    seconds longer than the configured window.
    """
    _write_segments(buffer_dir, 8)  # a previous session's full ring
    buffer = _running_buffer(buffer_dir, seconds=20, segment_seconds=2)
    buffer._remember_survivors_locked()

    for index in range(8, 12):
        (buffer_dir / f"seg_{index:03d}.ts").write_bytes(b"\0" * 1024)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    assert telemetry.segment_count <= telemetry.segment_capacity
    assert telemetry.buffered_seconds <= telemetry.window_seconds


def test_telemetry_returns_none_when_the_buffer_is_not_running(buffer_dir: Path) -> None:
    buffer = RollingBuffer(buffer_dir)
    assert buffer.telemetry() is None


def test_telemetry_excludes_the_segment_still_being_written(buffer_dir: Path) -> None:
    """Telemetry must report what a save would produce, not what is on disk.

    ``save_clip`` discards the newest segment because the muxer is still
    writing it. Telemetry reads through the same snapshot, so five files on
    disk means four saveable segments - eight seconds at the default two-second
    segment length.
    """
    _write_segments(buffer_dir, 5)
    buffer = _running_buffer(buffer_dir, segment_seconds=2)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    assert telemetry.segment_count == 4
    assert telemetry.buffered_seconds == 8.0


def test_telemetry_keeps_a_lone_segment(buffer_dir: Path) -> None:
    """A just-started buffer keeps its single segment, mirroring ``save_clip``."""
    _write_segments(buffer_dir, 1)
    buffer = _running_buffer(buffer_dir, segment_seconds=2)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    assert telemetry.segment_count == 1
    assert telemetry.buffered_seconds == 2.0


def test_telemetry_sums_only_the_saveable_segments(buffer_dir: Path) -> None:
    _write_segments(buffer_dir, 4, size=2048)
    buffer = _running_buffer(buffer_dir)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    # Three counted segments (the newest is dropped), 2048 bytes each.
    assert telemetry.bytes_on_disk == 3 * 2048


def test_telemetry_reports_the_configured_window_and_capacity(buffer_dir: Path) -> None:
    _write_segments(buffer_dir, 3)
    buffer = _running_buffer(buffer_dir, seconds=30, segment_seconds=2)

    telemetry = buffer.telemetry()

    assert telemetry is not None
    assert telemetry.window_seconds == 30
    # ceil(30 / 2) + 1 == 16 rotation slots.
    assert telemetry.segment_capacity == 16


def test_telemetry_tolerates_a_segment_vanishing_mid_scan(
    buffer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotating muxer can delete a segment between listing and sizing it.

    Telemetry polls once a second, so it meets this race far more often than
    the occasional save does. A vanished file must cost us its bytes, not the
    whole snapshot.
    """
    segments = _write_segments(buffer_dir, 4, size=1000)
    doomed = segments[0]
    buffer = _running_buffer(buffer_dir)
    monkeypatch.setattr(Path, "stat", _stat_vanishing_after_listing(doomed))

    telemetry = buffer.telemetry()

    assert telemetry is not None
    # Four segments on disk. The vanished one is dropped while listing, leaving
    # three, and the newest of those is discarded as still-being-written. Two
    # remain -- and crucially, nothing raised.
    assert telemetry.segment_count == 2
    assert telemetry.buffered_seconds == 4.0
    assert telemetry.bytes_on_disk == 2 * 1000


def test_expected_segment_paths_skips_a_file_that_disappears_while_sorting(
    buffer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing sorts by mtime, so it must survive a mid-sort deletion.

    ``save_clip`` and telemetry both route through here; a raise would take
    out a clip save, not just a readout.
    """
    from sclip.core.ffmpeg import expected_segment_paths

    segments = _write_segments(buffer_dir, 3)
    doomed = segments[1]
    monkeypatch.setattr(Path, "stat", _stat_vanishing_after_listing(doomed))

    listed = expected_segment_paths(buffer_dir)

    # The surviving segments are still returned, in mtime order.
    assert [p.name for p in listed] == ["seg_000.ts", "seg_002.ts"]
