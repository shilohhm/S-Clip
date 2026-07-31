"""Tests for the desktop-audio pump's write loop.

The loop itself is testable without WASAPI: it asks a stream how many frames
are ready, writes either those frames or silence, and paces itself. Standing in
a fake stream and a fake pipe exercises exactly the behaviour that was wrong,
on any machine, without needing a loopback device or a sound to be playing.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from sclip.core.desktop_audio import _READ_FRAMES, DesktopAudioPump

_RATE = 48000
_CHANNELS = 2
_CHUNK_BYTES = _READ_FRAMES * _CHANNELS * 2  # 16-bit samples


class _Stream:
    """Loopback stream stand-in with a controllable amount of ready audio."""

    def __init__(self, available: int, payload: bytes = b"") -> None:
        self._available = available
        self._payload = payload or bytes(_CHUNK_BYTES)
        self.reads = 0

    def get_read_available(self) -> int:
        return self._available

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        self.reads += 1
        return self._payload


class _Pipe:
    """Records what the pump writes; never refuses."""

    def __init__(self, accept: int = 10_000) -> None:
        self.writes: list[bytes] = []
        self._accept = accept

    def write(self, data: bytes) -> bool:
        if len(self.writes) >= self._accept:
            return False
        self.writes.append(data)
        return True


def _pump_for(stream: Any, pipe: Any, seconds: float) -> DesktopAudioPump:
    """Run the write loop on a thread for a short while, then stop it."""
    pump = DesktopAudioPump()
    thread = threading.Thread(
        target=pump._pump_until_stopped,
        args=(stream, pipe, _RATE, _CHANNELS),
        daemon=True,
    )
    thread.start()
    time.sleep(seconds)
    pump._stop_event.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the pump loop did not stop when asked"
    return pump


def test_silence_still_keeps_the_pipe_moving() -> None:
    """A silent desktop must not stall the capture.

    A WASAPI loopback endpoint hands back nothing at all while the system is
    quiet rather than buffers of zeroes. The loop used to block on read, so the
    pipe stopped advancing and FFmpeg starved on an input that never moved -
    the capture produced no segments whatsoever. A muted game was enough to
    make S-Clip silently record nothing.
    """
    stream = _Stream(available=0)
    pipe = _Pipe()

    _pump_for(stream, pipe, seconds=0.4)

    assert pipe.writes, "nothing was written while the device was silent"
    assert stream.reads == 0, "read() must not be called when no frames are ready"
    assert all(set(chunk) == {0} for chunk in pipe.writes), "padding must be silence"

    # Padding is sized to the shortfall rather than to a fixed chunk, so the
    # useful assertion is about rate: roughly a second of audio per second.
    frames = sum(len(chunk) // (_CHANNELS * 2) for chunk in pipe.writes)
    expected = 0.4 * _RATE
    assert 0.5 * expected < frames < 1.5 * expected, (
        f"emitted {frames} frames of silence where about {expected:.0f} were due"
    )


def test_real_audio_is_forwarded_rather_than_replaced() -> None:
    """When the device has frames, they must reach the pipe untouched."""
    payload = bytes(range(256)) * (_CHUNK_BYTES // 256)
    stream = _Stream(available=_READ_FRAMES * 4, payload=payload)
    pipe = _Pipe()

    _pump_for(stream, pipe, seconds=0.3)

    assert stream.reads > 0, "ready frames were never read"
    assert payload in pipe.writes, "captured audio was not forwarded"


def test_the_loop_paces_itself_instead_of_spinning() -> None:
    """Silence must be emitted at roughly real time, not as fast as possible.

    Without pacing the silence path becomes a busy loop that floods the pipe
    far faster than the capture drains it.
    """
    stream = _Stream(available=0)
    pipe = _Pipe()
    duration = 0.5

    _pump_for(stream, pipe, seconds=duration)

    # The loop is deficit-driven, not chunk-driven, so measure the audio it
    # actually produced rather than how many writes it took to get there.
    frames = sum(len(chunk) // (_CHANNELS * 2) for chunk in pipe.writes)
    expected = duration * _RATE
    assert 0.5 * expected < frames < 1.5 * expected, (
        f"emitted {frames} frames in {duration}s; real time is about {expected:.0f}"
    )


def test_the_loop_stops_when_the_reader_goes_away() -> None:
    """FFmpeg closing the pipe ends the recording; the thread must not linger."""
    stream = _Stream(available=0)
    pipe = _Pipe(accept=3)

    pump = DesktopAudioPump()
    thread = threading.Thread(
        target=pump._pump_until_stopped,
        args=(stream, pipe, _RATE, _CHANNELS),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "the loop kept running after the pipe closed"
    assert len(pipe.writes) == 3


def test_a_stream_without_get_read_available_still_works() -> None:
    """Not every backend implements the query; fall back to reading."""

    class _Minimal:
        def __init__(self) -> None:
            self.reads = 0

        def get_read_available(self) -> int:
            raise NotImplementedError

        def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
            self.reads += 1
            return bytes(_CHUNK_BYTES)

    stream = _Minimal()
    pipe = _Pipe()

    _pump_for(stream, pipe, seconds=0.3)

    assert stream.reads > 0, "the fallback path never read from the stream"
    assert pipe.writes
