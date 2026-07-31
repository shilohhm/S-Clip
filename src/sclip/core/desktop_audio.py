"""Desktop-audio capture via the Windows WASAPI loopback API.

FFmpeg cannot capture desktop sound on Windows on its own - its ``dshow``
input only sees *capture* devices (microphones), never the speakers. Tools
like OBS and Medal get around this with WASAPI loopback, a built-in Windows
facility that hands back exactly what a playback device is outputting, with
no "Stereo Mix" and no virtual cable.

This module does the same. :class:`DesktopAudioPump` opens the default
playback device in loopback mode, then streams the raw PCM into a Windows
named pipe. The capture engine points an extra FFmpeg input at that pipe and
mixes it with the microphone, so a saved clip carries the game audio.

The whole feature degrades gracefully: if the optional ``PyAudioWPatch``
dependency is missing, or no loopback device can be opened, :meth:`start`
returns ``None`` and the engine simply records without desktop audio.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# How many sample frames to pull from WASAPI per read. About 21 ms at 48 kHz -
# small enough to keep latency low, large enough to avoid thrashing the pipe.
_READ_FRAMES: int = 1024

# Size of the named pipe's kernel buffer. A generous 256 KB rides out the
# occasional moment when FFmpeg is busy and not draining the pipe.
_PIPE_BUFFER_BYTES: int = 256 * 1024


@dataclass(frozen=True, slots=True)
class DesktopAudioStream:
    """Describes the pipe a running pump is streaming PCM into.

    The capture engine turns this straight into FFmpeg input arguments:
    ``-f s16le -ar {sample_rate} -ac {channels} -i {pipe_name}``.
    """

    pipe_name: str
    sample_rate: int
    channels: int


def _kernel32() -> ctypes.WinDLL:
    """Return the kernel32 handle used for the named-pipe calls."""
    return ctypes.WinDLL("kernel32", use_last_error=True)


# Win32 constants for the named pipe. Named here rather than inline so the
# CreateNamedPipe call below reads as prose.
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class _OutboundPipe:
    """A one-writer Windows named pipe the pump streams PCM into.

    FFmpeg is the single reader. The class hides the handful of ctypes calls
    the rest of the module would otherwise be cluttered with.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._k32 = _kernel32()
        self._handle: int | None = None

    @property
    def name(self) -> str:
        return self._name

    def create(self) -> None:
        """Create the pipe server end. Must run before FFmpeg opens the pipe."""
        handle = self._k32.CreateNamedPipeW(
            self._name,
            _PIPE_ACCESS_OUTBOUND,
            _PIPE_TYPE_BYTE | _PIPE_WAIT,
            1,  # one instance - exactly one FFmpeg reader
            _PIPE_BUFFER_BYTES,
            _PIPE_BUFFER_BYTES,
            0,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
        self._handle = handle

    def wait_for_reader(self) -> None:
        """Block until FFmpeg opens the other end of the pipe.

        :meth:`unblock_wait` can be called from another thread to free this
        call early when a capture is torn down before FFmpeg ever connected.
        """
        if self._handle is None:
            raise RuntimeError("pipe was not created")
        connected = self._k32.ConnectNamedPipe(self._handle, None)
        if not connected:
            error = ctypes.get_last_error()
            # ERROR_PIPE_CONNECTED (535) means a reader connected in the gap
            # between create and connect - that is success, not failure.
            if error not in (0, 535):
                raise OSError(error, "ConnectNamedPipe failed")

    def unblock_wait(self) -> None:
        """Open and drop a client handle so a blocked :meth:`wait_for_reader` returns."""
        client = self._k32.CreateFileW(self._name, _GENERIC_READ, 0, None, _OPEN_EXISTING, 0, None)
        if client not in (0, _INVALID_HANDLE_VALUE):
            self._k32.CloseHandle(client)

    def write(self, data: bytes) -> bool:
        """Write a PCM chunk. Returns False once the reader has gone away."""
        if self._handle is None:
            return False
        written = wintypes.DWORD(0)
        ok = self._k32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
        return bool(ok)

    def close(self) -> None:
        """Close the pipe handle, if it is still open."""
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None


class DesktopAudioPump:
    """Captures the default playback device and streams it to a named pipe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pipe: _OutboundPipe | None = None
        self._connected = threading.Event()
        # Set once we know whether the optional dependency is importable.
        self._available: bool | None = None

    @property
    def is_available(self) -> bool:
        """True when WASAPI loopback capture can be attempted on this machine."""
        if self._available is None:
            self._available = self._probe_availability()
        return self._available

    @staticmethod
    def _probe_availability() -> bool:
        """Check the optional dependency imports and a loopback device exists."""
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            logger.info("PyAudioWPatch is not installed; desktop audio is unavailable")
            return False
        try:
            audio = pyaudio.PyAudio()
            try:
                next(audio.get_loopback_device_info_generator())
            finally:
                audio.terminate()
        except StopIteration:
            logger.info("No WASAPI loopback device found; desktop audio is unavailable")
            return False
        except Exception:
            logger.exception("Probing WASAPI loopback failed")
            return False
        return True

    def start(self) -> DesktopAudioStream | None:
        """Begin capturing. Returns the pipe details, or ``None`` if unavailable.

        The named pipe is created synchronously before this returns, so the
        caller can safely spawn an FFmpeg process that opens it straight away.
        """
        with self._lock:
            if self._thread is not None:
                logger.debug("Desktop audio pump already running")
                return None
            if not self.is_available:
                return None

            try:
                import pyaudiowpatch as pyaudio
            except ImportError:
                return None

            device = self._resolve_loopback_device(pyaudio)
            if device is None:
                return None

            sample_rate = int(device["defaultSampleRate"])
            channels = int(device["maxInputChannels"])
            pipe_name = rf"\\.\pipe\sclip-desktop-audio-{os.getpid()}"

            pipe = _OutboundPipe(pipe_name)
            try:
                pipe.create()
            except OSError as exc:
                logger.error("Could not create the desktop-audio pipe: %s", exc)
                return None

            self._pipe = pipe
            self._stop_event.clear()
            self._connected.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(int(device["index"]), sample_rate, channels),
                name="sclip-desktop-audio",
                daemon=True,
            )
            self._thread.start()

            logger.info(
                "Desktop audio pump started: %s @ %d Hz, %d ch",
                device["name"],
                sample_rate,
                channels,
            )
            return DesktopAudioStream(pipe_name, sample_rate, channels)

    def stop(self) -> None:
        """Stop capturing and tidy up the pipe and the worker thread."""
        with self._lock:
            thread = self._thread
            pipe = self._pipe
            if thread is None:
                return
            self._stop_event.set()
            # If FFmpeg never connected, the worker is parked in
            # wait_for_reader - nudge it loose so the thread can exit.
            if pipe is not None and not self._connected.is_set():
                pipe.unblock_wait()

        thread.join(timeout=4.0)
        with self._lock:
            if self._pipe is not None:
                self._pipe.close()
            self._pipe = None
            self._thread = None
        logger.info("Desktop audio pump stopped")

    # -- worker -------------------------------------------------------------

    @staticmethod
    def _resolve_loopback_device(pyaudio_module: object) -> dict[str, Any] | None:
        """Find the loopback device that mirrors the default playback device.

        PyAudio's ``get_loopback_device_info_generator`` is a one-shot
        generator: iterating it a second time yields nothing.  We therefore
        materialise it into a list once and do both the name-match pass and
        the first-device fallback against that list, so neither pass silently
        sees an empty sequence.
        """
        try:
            audio = pyaudio_module.PyAudio()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Could not open PyAudio for desktop capture")
            return None
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio_module.paWASAPI)  # type: ignore[attr-defined]
            default_output = audio.get_device_info_by_index(int(wasapi["defaultOutputDevice"]))
            target = str(default_output["name"])
            # Collect the generator into a list so we can traverse it twice
            # without the second loop seeing an exhausted iterator.
            loopbacks = list(audio.get_loopback_device_info_generator())
            for loopback in loopbacks:
                if target in str(loopback["name"]):
                    return dict(loopback)
            # No exact match - fall back to the first loopback device so the
            # feature still works on an unusual audio configuration.
            if loopbacks:
                return dict(loopbacks[0])
            return None
        except Exception:
            logger.exception("Could not resolve a loopback device")
            return None
        finally:
            audio.terminate()

    def _pump_until_stopped(
        self,
        stream: Any,
        pipe: _OutboundPipe,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Forward loopback PCM to the pipe until asked to stop.

        Two failure modes shape this loop, and they pull in opposite
        directions.

        A WASAPI loopback endpoint delivers nothing at all while the system is
        silent - it does not hand back buffers of zeroes. Blocking on ``read``
        therefore stalls the thread whenever nothing is playing, the pipe stops
        advancing, and FFmpeg starves on an input that never moves. A muted game
        was enough to make the capture produce no segments whatsoever.

        Padding that gap on a fixed timer then caused the opposite problem.
        Windows' default timer granularity is around 15 ms against a chunk
        period of 23 ms, so a loop that sleeps its computed slack overshoots and
        emits slower than 44.1 kHz. FFmpeg then waits on audio and drags video
        down with it: capture keep-up measured 72% with desktop audio on
        against 97% with it off, and the video was not the thing at fault.

        So the device's own clock drives the loop whenever audio is flowing -
        available frames are taken immediately, with no sleep in the path - and
        silence is added only to make up a shortfall measured against elapsed
        wall time. When sound is playing the correction is zero and nothing is
        injected; when the endpoint goes quiet the deficit grows and is filled
        exactly. The result is self-correcting and needs no accurate timer.
        """
        bytes_per_frame = channels * 2  # 16-bit samples
        started = time.monotonic()
        written_frames = 0

        # Only bother padding once the shortfall is worth a write, and never
        # emit a huge block at once, so a long silence stays smooth.
        min_pad = _READ_FRAMES // 2
        max_pad = _READ_FRAMES * 4

        while not self._stop_event.is_set():
            try:
                available = int(stream.get_read_available())
            except Exception:  # not every backend implements it
                available = _READ_FRAMES

            if available > 0:
                # Returns straight away: we only ask for what is already there,
                # so the device paces us without a sleep.
                chunk = stream.read(min(available, _READ_FRAMES), exception_on_overflow=False)
                if not pipe.write(chunk):
                    if not self._stop_event.is_set():
                        logger.debug("Desktop-audio reader closed the pipe")
                    return
                written_frames += len(chunk) // bytes_per_frame
                continue

            expected = int((time.monotonic() - started) * sample_rate)
            deficit = expected - written_frames
            if deficit >= min_pad:
                pad = min(deficit, max_pad)
                if not pipe.write(bytes(pad * bytes_per_frame)):
                    if not self._stop_event.is_set():
                        logger.debug("Desktop-audio reader closed the pipe")
                    return
                written_frames += pad
                continue

            # Nothing ready and nothing owed: idle briefly. The wait is short
            # enough that the coarse Windows timer cannot make us fall behind,
            # because the deficit above corrects for it either way.
            self._stop_event.wait(0.002)

    def _run(self, device_index: int, sample_rate: int, channels: int) -> None:
        """Worker thread: read loopback PCM and forward it to the pipe."""
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            return

        pipe = self._pipe
        if pipe is None:
            return

        # Block until FFmpeg opens the read end of the pipe.
        try:
            pipe.wait_for_reader()
        except OSError as exc:
            logger.error("Desktop-audio pipe never received a reader: %s", exc)
            return
        if self._stop_event.is_set():
            return
        self._connected.set()

        audio = None
        stream = None
        try:
            audio = pyaudio.PyAudio()
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                frames_per_buffer=_READ_FRAMES,
                input=True,
                input_device_index=device_index,
            )
            self._pump_until_stopped(stream, pipe, sample_rate, channels)

        except Exception:
            logger.exception("Desktop audio capture loop failed")
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    logger.debug("Closing the loopback stream raised", exc_info=True)
            if audio is not None:
                audio.terminate()


__all__ = ["DesktopAudioPump", "DesktopAudioStream"]
