"""Tests for the rolling replay buffer in :mod:`sclip.core.replay_buffer`.

These tests drive :class:`RollingBuffer` against the fake FFmpeg binary from
:mod:`tests.conftest`, so they exercise the real process plumbing without
actually capturing a desktop. They are marked ``slow`` because each test
spawns a subprocess and waits long enough for the fake to emit a couple of
``.ts`` segments — easily under a second each, but slower than the pure
in-memory tests in the rest of the suite.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from sclip.core.replay_buffer import BufferSpec, RollingBuffer

# How long we let the fake FFmpeg buffer run before we look for segments.
# The fake writes its segments synchronously on startup, so this is mostly a
# guard against scheduler jitter — half a second is plenty.
_WARMUP_SECONDS: float = 0.6


def _make_spec(directory: Path) -> BufferSpec:
    """Build a buffer spec with empty capture args.

    The real capture args (gdigrab, dshow, encoder) are irrelevant here —
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

        # Second start with the same spec — should be a no-op.
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
