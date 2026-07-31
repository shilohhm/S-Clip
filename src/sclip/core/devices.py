"""Enumerate the monitors and audio devices exposed by the host OS.

Listing devices is surprisingly slow on Windows - every call to FFmpeg's dshow
probe spins up the binary, queries the COM subsystem and waits for it to time
out trying to open ``dummy`` as an input. We cache the results until a caller
explicitly invokes :meth:`SystemDeviceRegistry.refresh`.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
from typing import Final

from sclip.contracts import AudioDevice, Monitor

logger = logging.getLogger(__name__)

# The dshow probe is happy to time out at five seconds on machines with broken
# virtual audio drivers, so we cap our patience well above that.
_FFMPEG_TIMEOUT_SECONDS: Final = 15.0

# Sections in FFmpeg's stderr that switch us between "we're now listing audio
# capture endpoints" and "we're now listing video capture endpoints". The exact
# wording has shifted slightly between FFmpeg versions, so we match loosely.
_AUDIO_SECTION_RE: Final = re.compile(r"DirectShow audio", re.IGNORECASE)
_VIDEO_SECTION_RE: Final = re.compile(r"DirectShow video", re.IGNORECASE)
_OUTPUT_HINT_RE: Final = re.compile(r"\b(output|render|playback)\b", re.IGNORECASE)

# A device entry looks like:
#   [dshow @ 0xdeadbeef] "Some Microphone" (audio)
_DEVICE_LINE_RE: Final = re.compile(
    r'^\[dshow @ [^\]]+\]\s+"(?P<name>[^"]+)"\s+\((?P<kind>[a-z]+)\)'
)

# The line two below each device entry is the alternative-name brand line -
# it has no double-quoted device name, just the @device path. We detect it
# loosely so we don't accidentally pick up the next device entry.
_ALT_NAME_RE: Final = re.compile(r'^\[dshow @ [^\]]+\]\s+Alternative name\s+"(?P<alt>[^"]+)"')

# Try the new ffmpeg helper; fall back to a system lookup if the module is not
# yet in the tree (another agent is writing it in parallel). The import is
# guarded so this file at least parses and imports cleanly during development.
try:
    from sclip.core.ffmpeg import find_ffmpeg

    def _resolve_ffmpeg() -> str | None:
        try:
            return str(find_ffmpeg())
        except Exception:
            return None

except ImportError:  # pragma: no cover - only triggered before the sibling lands

    def _resolve_ffmpeg() -> str | None:
        """Fallback FFmpeg resolver - system PATH only, no bundled lookup."""
        return shutil.which("ffmpeg")


class SystemDeviceRegistry:
    """Implementation of :class:`DeviceRegistry` backed by ``screeninfo`` + dshow."""

    def __init__(self) -> None:
        # Caches are populated lazily on first access. The lock guards against
        # two GUI threads each refreshing the list at the same time on startup.
        self._monitors_cache: list[Monitor] | None = None
        self._audio_cache: list[AudioDevice] | None = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- public

    def monitors(self) -> list[Monitor]:
        """Return the displays attached to the system, primary first if known."""
        with self._lock:
            if self._monitors_cache is None:
                self._monitors_cache = _enumerate_monitors()
            return list(self._monitors_cache)

    def audio_devices(self) -> list[AudioDevice]:
        """Return audio endpoints exposed by FFmpeg's dshow demuxer."""
        with self._lock:
            if self._audio_cache is None:
                self._audio_cache = _enumerate_audio_devices()
            return list(self._audio_cache)

    def refresh(self) -> None:
        """Invalidate the caches so the next call rediscovers everything."""
        with self._lock:
            self._monitors_cache = None
            self._audio_cache = None
        logger.debug("Device caches cleared")


# ====================================================================== monitors


def _enumerate_monitors() -> list[Monitor]:
    """Wrap :func:`screeninfo.get_monitors` so a failure does not kill the app."""
    try:
        # Imported inside the function so a packaging fault around screeninfo
        # only bites callers who actually ask for monitors, not at import time.
        from screeninfo import get_monitors

        raw_monitors = get_monitors()
    except Exception as exc:
        logger.warning("Could not enumerate monitors: %s", exc)
        return []

    monitors: list[Monitor] = []
    for index, m in enumerate(raw_monitors, start=1):
        # ``is_primary`` is missing on some platforms - default to False if so.
        is_primary = bool(getattr(m, "is_primary", False))
        monitors.append(
            Monitor(
                name=f"Monitor {index}",
                x=int(m.x),
                y=int(m.y),
                width=int(m.width),
                height=int(m.height),
                is_primary=is_primary,
            )
        )
    return monitors


# ================================================================= audio devices


def _enumerate_audio_devices() -> list[AudioDevice]:
    """Run FFmpeg's dshow probe and parse the stderr block it spits out."""
    ffmpeg_path = _resolve_ffmpeg()
    if not ffmpeg_path:
        logger.warning("FFmpeg not available; cannot enumerate audio devices")
        return []

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]

    try:
        result = subprocess.run(  # noqa: PLW1510 - we accept the non-zero exit
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            creationflags=_no_window_flag(),
        )
    except FileNotFoundError:
        logger.warning("FFmpeg binary at %s went missing between probes", ffmpeg_path)
        return []
    except subprocess.TimeoutExpired:
        logger.warning("Audio device probe timed out after %.1fs", _FFMPEG_TIMEOUT_SECONDS)
        return []
    except OSError as exc:
        logger.warning("Could not run FFmpeg for device probe: %s", exc)
        return []

    return _parse_dshow_devices(result.stderr or "")


def _parse_dshow_devices(stderr: str) -> list[AudioDevice]:
    """Walk FFmpeg's stderr line by line, picking out audio devices.

    We look for the section header (``DirectShow audio devices``) so that
    "(audio)" entries appearing after an ``output``/``render``/``playback``
    hint can be tagged as output devices. When we cannot decide cleanly we
    fall back to "input" rather than guess - the upstream contract is happy
    with that and the device name is preserved verbatim.
    """
    devices: list[AudioDevice] = []
    seen: set[tuple[str, str]] = set()
    section_kind = "input"  # default until a header tells us otherwise

    lines = stderr.splitlines()
    for line in lines:
        # Track section context. The header line itself is not a device entry.
        if _AUDIO_SECTION_RE.search(line):
            section_kind = "output" if _OUTPUT_HINT_RE.search(line) else "input"
            continue
        if _VIDEO_SECTION_RE.search(line):
            # We don't surface video devices, but knowing where they are means
            # we won't mistake them for audio in the same loop.
            section_kind = "video"
            continue

        match = _DEVICE_LINE_RE.match(line.rstrip())
        if not match:
            continue

        name = match.group("name")
        kind_tag = match.group("kind").lower()
        if kind_tag != "audio":
            # ``(video)`` entries get skipped here; we don't enumerate cameras.
            continue

        # Within an audio section we trust the section context; outside any
        # section we cannot really know, so we default to "input".
        kind = section_kind if section_kind in {"input", "output"} else "input"

        key = (name, kind)
        if key in seen:
            continue
        seen.add(key)
        devices.append(AudioDevice(name=name, kind=kind))

    if not devices:
        # Stderr was empty or didn't match - log enough to debug without
        # spamming the log with the full FFmpeg banner.
        logger.info("No audio devices recognised in FFmpeg output")
    return devices


# ===================================================================== platform


def _no_window_flag() -> int:
    """Return the Windows ``CREATE_NO_WINDOW`` flag, or zero elsewhere.

    Without this the FFmpeg probe will flash a console window each time the
    settings dialog refreshes, which looks awful in a packaged app.
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0
