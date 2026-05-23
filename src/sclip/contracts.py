"""Shared interface contracts.

The capture engine, the GUI, the hotkey listener and the tests all talk to
one another through the protocols and data classes defined here. Keeping the
surface area small and explicit means each subsystem can evolve on its own as
long as the published shape does not change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class CaptureState(str, Enum):
    """Lifecycle states the capture engine can be in.

    Modelled as strings so the enum survives a round trip through JSON
    settings and Qt signals without bespoke marshalling.
    """

    IDLE = "idle"
    BUFFERING = "buffering"
    RECORDING = "recording"
    SAVING = "saving"
    ERROR = "error"


class CaptureMode(str, Enum):
    """High-level operation the capture engine should perform."""

    MANUAL = "manual"
    REPLAY_BUFFER = "replay_buffer"


@dataclass(frozen=True, slots=True)
class Monitor:
    """A single physical display, in screen-space coordinates."""

    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        """``(x, y, width, height)`` — handy for FFmpeg ``gdigrab`` args."""
        return self.x, self.y, self.width, self.height


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """An audio capture endpoint, as exposed by FFmpeg ``-f dshow``."""

    name: str
    kind: str  # "input" or "output"


@dataclass(frozen=True, slots=True)
class Hotkey:
    """A keyboard chord stored as modifier flags plus a key name.

    Modelled as plain data so it survives JSON serialisation; the hotkey
    listener turns this back into platform-specific key codes at registration
    time.
    """

    key: str  # e.g. "F5", "F12", "A"
    ctrl: bool = False
    shift: bool = False
    alt: bool = False

    def to_display(self) -> str:
        parts: list[str] = []
        if self.ctrl:
            parts.append("Ctrl")
        if self.shift:
            parts.append("Shift")
        if self.alt:
            parts.append("Alt")
        parts.append(self.key.upper())
        return "+".join(parts)


@dataclass(frozen=True, slots=True)
class EncoderSpec:
    """Description of one FFmpeg video encoder and the presets it accepts."""

    codec: str
    presets: tuple[str, ...]
    needs_gpu: bool = False


ENCODERS: tuple[EncoderSpec, ...] = (
    EncoderSpec(
        codec="libx264",
        presets=(
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "veryslow",
        ),
    ),
    EncoderSpec(
        codec="h264_nvenc",
        presets=("p1", "p2", "p3", "p4", "p5", "p6", "p7"),
        needs_gpu=True,
    ),
    EncoderSpec(
        codec="hevc_nvenc",
        presets=("p1", "p2", "p3", "p4", "p5", "p6", "p7"),
        needs_gpu=True,
    ),
    EncoderSpec(
        codec="h264_amf",
        presets=("speed", "balanced", "quality"),
        needs_gpu=True,
    ),
    EncoderSpec(
        codec="h264_qsv",
        presets=("veryfast", "faster", "fast", "medium", "slow"),
        needs_gpu=True,
    ),
)


def encoder_by_codec(codec: str) -> EncoderSpec | None:
    """Find an :class:`EncoderSpec` by codec name, returning ``None`` if unknown."""
    for spec in ENCODERS:
        if spec.codec == codec:
            return spec
    return None


# Friendly display names for every encoder codec S-Clip knows about. Codecs
# not listed here fall back to the raw codec string in :func:`encoder_label`,
# which is still readable enough for an uncommon or future encoder.
#
# Kept in the contracts layer (rather than in ``core.hardware``) because the UI
# needs these labels without any dependency on hardware-probing code. The
# previous placement — inside the hardware module — was a layering violation:
# the UI reached into a core *implementation* module for what is really pure,
# portable data.
_ENCODER_LABELS: dict[str, str] = {
    "h264_nvenc": "NVIDIA NVENC (H.264)",
    "hevc_nvenc": "NVIDIA NVENC (HEVC)",
    "h264_amf": "AMD AMF (H.264)",
    "hevc_amf": "AMD AMF (HEVC)",
    "h264_qsv": "Intel Quick Sync (H.264)",
    "libx264": "Software x264 (CPU)",
}


def encoder_label(codec: str) -> str:
    """Return a human-friendly name for an encoder codec.

    Falls back to the raw codec string for any codec not in :data:`_ENCODER_LABELS`,
    so callers receive something readable even for future or third-party encoders.
    """
    return _ENCODER_LABELS.get(codec, codec)


@dataclass(slots=True)
class Settings:
    """User-editable application settings.

    Mutable on purpose: the settings page mutates a working copy and writes it
    back atomically. Defaults match the values the previous version of the app
    shipped, so existing users see no behavioural change after upgrading.
    """

    resolution: str = "1920x1080"
    fps: int = 60
    encoder: str = "libx264"
    preset: str = "veryfast"
    crf: int = 20
    audio_input: str = ""  # dshow microphone device name; "" means no microphone
    capture_audio: bool = True  # master switch — when off, no audio is captured
    capture_desktop_audio: bool = True  # capture system sound via WASAPI loopback
    replay_buffer: bool = True
    replay_seconds: int = 30
    monitor: str = "Monitor 1"
    clip_hotkey: Hotkey = field(default_factory=lambda: Hotkey(key="F5"))
    record_hotkey: Hotkey = field(default_factory=lambda: Hotkey(key="F6", ctrl=True))
    output_dir: str = ""  # blank -> use platformdirs default
    # True while S-Clip is managing the capture settings for the user. The
    # first launch turns this on and writes hardware-tuned values; saving the
    # Advanced settings form turns it off so the user's choices are respected.
    auto_configure: bool = True

    def copy(self) -> Settings:
        """Return an independent copy — used when the settings page begins editing.

        Implemented with :func:`dataclasses.replace` so adding a new field to
        :class:`Settings` is automatically reflected here.  The old hand-written
        field-by-field approach silently dropped any new field from the copy,
        which would have caused the settings page to lose the value on every
        edit cycle.
        """
        return dataclasses.replace(self)


@runtime_checkable
class CaptureEngine(Protocol):
    """The capture engine, as far as the GUI is concerned.

    Concrete implementations live in :mod:`sclip.core.capture`. The protocol
    keeps the GUI free of FFmpeg knowledge and makes the engine swappable for
    a fake during tests.

    Observers register through the ``add_*_listener`` methods. The engine
    supports any number of listeners per event, so several parts of the GUI
    (the capture page, the main window, the tray) can each react to a state
    change, a saved clip or an error without contending for a single slot.
    Listener callbacks may fire on a worker thread — the GUI marshals them
    back onto the Qt thread itself.
    """

    state: CaptureState

    def start_manual_recording(self) -> None: ...

    def stop_manual_recording(self) -> Path | None: ...

    def start_replay_buffer(self) -> None: ...

    def stop_replay_buffer(self) -> None: ...

    def save_replay_clip(self) -> None: ...

    def shutdown(self) -> None: ...

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None: ...

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None: ...

    def add_error_listener(self, listener: Callable[[str], None]) -> None: ...


@runtime_checkable
class SettingsStore(Protocol):
    """Persists :class:`Settings` between sessions."""

    def load(self) -> Settings: ...

    def save(self, settings: Settings) -> None: ...


@runtime_checkable
class DeviceRegistry(Protocol):
    """Enumerates the monitors and audio devices the OS exposes."""

    def monitors(self) -> list[Monitor]: ...

    def audio_devices(self) -> list[AudioDevice]: ...


__all__ = [
    "ENCODERS",
    "AudioDevice",
    "CaptureEngine",
    "CaptureMode",
    "CaptureState",
    "DeviceRegistry",
    "EncoderSpec",
    "Hotkey",
    "Monitor",
    "Settings",
    "SettingsStore",
    "encoder_by_codec",
    "encoder_label",
]
