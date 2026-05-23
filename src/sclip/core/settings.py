"""Persistence layer for :class:`Settings`.

The store is deliberately tolerant on the way in and strict on the way out:
existing users can have hand-edited files, half-migrated schemas or files
truncated by a power cut, and we still want the app to open. On save we
normalise every field and write atomically so a crash cannot leave the user
with an unparseable file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from sclip.contracts import ENCODERS, Hotkey, Settings, encoder_by_codec
from sclip.paths import app_paths

logger = logging.getLogger(__name__)

# Defaults used to backfill missing or invalid fields. Kept in one place so the
# fallback behaviour is obvious and easy to change.
_DEFAULT_ENCODER = "libx264"
_DEFAULT_PRESET = "veryfast"

# Sanity bounds. Anything outside these is almost certainly a typo or a stale
# value from an older schema; clamp rather than refuse to start.
_RESOLUTION_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
_MIN_DIMENSION = 16
_MAX_DIMENSION = 16_384  # well past any real display, but bounded
_FPS_MIN, _FPS_MAX = 1, 240
_CRF_MIN, _CRF_MAX = 0, 51
_REPLAY_MIN, _REPLAY_MAX = 5, 600

# Set of supported encoder codecs derived from the contract so the two
# definitions never drift apart.
_VALID_ENCODERS: frozenset[str] = frozenset(spec.codec for spec in ENCODERS)

# Maximum byte length for a device name that came from settings.json.  A
# legitimate Windows audio-device name is rarely more than ~100 characters;
# 256 is generous while still bounding the attack surface.
_AUDIO_INPUT_MAX_LEN: int = 256


class JsonSettingsStore:
    """JSON-backed :class:`SettingsStore` with atomic writes and schema migration.

    The optional ``path`` argument exists so tests can point at a temp file
    without having to monkey-patch :func:`app_paths`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path if path is not None else app_paths().settings_file

    # ------------------------------------------------------------------ load

    def load(self) -> Settings:
        """Read settings from disk, returning defaults if anything goes wrong.

        We never raise on a missing or corrupt file — the app needs to launch
        even if the user has scribbled invalid JSON over the config.
        """
        if not self._path.exists():
            logger.info("No settings file at %s; using defaults", self._path)
            return Settings()

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Settings file at %s is unreadable (%s); using defaults",
                self._path,
                exc,
            )
            return Settings()

        if not isinstance(data, dict):
            logger.warning(
                "Settings file at %s is not a JSON object; using defaults", self._path
            )
            return Settings()

        return _settings_from_dict(data)

    # ------------------------------------------------------------------ save

    def save(self, settings: Settings) -> None:
        """Validate, then atomically persist ``settings`` to disk.

        Writes to ``settings.json.tmp`` first and then ``os.replace``s it onto
        the real file — on POSIX and modern Windows this is atomic, so a crash
        mid-write leaves either the old file or the new one, never a torn one.
        """
        # Run the same validation pass we use on load so a programmatic caller
        # who hands us nonsense still ends up with a sane file on disk.
        sanitised = _validated_copy(settings)
        payload = _settings_to_dict(sanitised)

        # Make sure the directory exists before we try to drop a temp file in it.
        self._path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            # ``sort_keys`` gives us stable diffs when the file is checked in or
            # synced via OneDrive; ``indent=2`` keeps it human-editable.
            tmp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("Could not save settings to %s: %s", self._path, exc)
            # Best-effort tidy: leaving a stray .tmp file confuses backup tools.
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

        logger.debug("Settings saved to %s", self._path)


# ====================================================================== helpers
#
# Everything below is module-private. Kept as plain functions rather than
# methods because they are pure: input dict in, validated values out, with no
# reference to the store's path or any other state.


def _settings_from_dict(data: dict[str, Any]) -> Settings:
    """Build a :class:`Settings` from a raw JSON dict, migrating as needed."""
    # Legacy key migration. The old shape stored ``audio_device`` for what the
    # new contract calls ``audio_input``, and a bare string hotkey rather than
    # a structured chord. We only honour the legacy key if the new one is
    # absent — that way a partially migrated file keeps its newer values.
    if "audio_input" not in data and "audio_device" in data:
        data = {**data, "audio_input": data["audio_device"]}

    # Old single-string hotkey -> structured Hotkey under the new "clip_hotkey".
    if "clip_hotkey" not in data and isinstance(data.get("hotkey"), str):
        data = {**data, "clip_hotkey": {"key": data["hotkey"]}}

    defaults = Settings()
    encoder = _coerce_encoder(data.get("encoder"))
    preset = _coerce_preset(encoder, data.get("preset"))

    return Settings(
        resolution=_coerce_resolution(data.get("resolution"), defaults.resolution),
        fps=_coerce_int(data.get("fps"), defaults.fps, _FPS_MIN, _FPS_MAX, "fps"),
        encoder=encoder,
        preset=preset,
        crf=_coerce_int(data.get("crf"), defaults.crf, _CRF_MIN, _CRF_MAX, "crf"),
        audio_input=_coerce_audio_input(data.get("audio_input"), defaults.audio_input),
        capture_audio=_coerce_bool(data.get("capture_audio"), defaults.capture_audio),
        capture_desktop_audio=_coerce_bool(
            data.get("capture_desktop_audio"), defaults.capture_desktop_audio
        ),
        replay_buffer=_coerce_bool(data.get("replay_buffer"), defaults.replay_buffer),
        replay_seconds=_coerce_int(
            data.get("replay_seconds"),
            defaults.replay_seconds,
            _REPLAY_MIN,
            _REPLAY_MAX,
            "replay_seconds",
        ),
        monitor=_coerce_str(data.get("monitor"), defaults.monitor),
        clip_hotkey=_coerce_hotkey(data.get("clip_hotkey"), defaults.clip_hotkey),
        record_hotkey=_coerce_hotkey(data.get("record_hotkey"), defaults.record_hotkey),
        output_dir=_coerce_output_dir(data.get("output_dir"), defaults.output_dir),
        auto_configure=_coerce_bool(data.get("auto_configure"), defaults.auto_configure),
    )


def _settings_to_dict(settings: Settings) -> dict[str, Any]:
    """Serialise a validated :class:`Settings` to a plain JSON-ready dict."""
    return {
        "resolution": settings.resolution,
        "fps": settings.fps,
        "encoder": settings.encoder,
        "preset": settings.preset,
        "crf": settings.crf,
        "audio_input": settings.audio_input,
        "capture_audio": settings.capture_audio,
        "capture_desktop_audio": settings.capture_desktop_audio,
        "replay_buffer": settings.replay_buffer,
        "replay_seconds": settings.replay_seconds,
        "monitor": settings.monitor,
        "clip_hotkey": _hotkey_to_dict(settings.clip_hotkey),
        "record_hotkey": _hotkey_to_dict(settings.record_hotkey),
        "output_dir": settings.output_dir,
        "auto_configure": settings.auto_configure,
    }


def _validated_copy(settings: Settings) -> Settings:
    """Return a :class:`Settings` with every field passed through the coercers.

    Called from :meth:`JsonSettingsStore.save` so anything we write was checked
    the same way we check things we read — if a bug elsewhere is mutating a
    field into something silly, the file on disk still ends up sane.
    """
    return _settings_from_dict(_settings_to_dict(settings))


# -------------------------------------------------------- per-field coercers


def _coerce_resolution(value: Any, default: str) -> str:
    """Accept ``"WIDTHxHEIGHT"`` with reasonable bounds, else fall back."""
    if isinstance(value, str) and _RESOLUTION_RE.match(value):
        width_str, height_str = value.split("x", 1)
        width, height = int(width_str), int(height_str)
        if _MIN_DIMENSION <= width <= _MAX_DIMENSION and _MIN_DIMENSION <= height <= _MAX_DIMENSION:
            return value
    if value is not None:
        logger.warning("Invalid resolution %r; falling back to %s", value, default)
    return default


def _coerce_int(value: Any, default: int, lo: int, hi: int, field_name: str) -> int:
    """Coerce ``value`` to ``int`` and clamp it into ``[lo, hi]``."""
    try:
        # ``bool`` is a subclass of ``int`` — exclude it explicitly so True
        # doesn't sneak in as ``1`` for things like fps.
        if isinstance(value, bool):
            raise TypeError
        as_int = int(value)
    except (TypeError, ValueError):
        if value is not None:
            logger.warning("Invalid %s %r; falling back to %d", field_name, value, default)
        return default
    if as_int < lo or as_int > hi:
        logger.warning(
            "%s value %d outside %d..%d; clamping", field_name, as_int, lo, hi
        )
        return max(lo, min(hi, as_int))
    return as_int


def _coerce_bool(value: Any, default: bool) -> bool:
    """Coerce ``value`` to ``bool``; non-bool values fall through to the default."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    logger.warning("Invalid bool %r; falling back to %r", value, default)
    return default


def _coerce_str(value: Any, default: str) -> str:
    """Pass strings straight through, coerce ``None`` to the default."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    logger.warning("Invalid string %r; falling back to %r", value, default)
    return default


def _coerce_audio_input(value: Any, default: str) -> str:  # noqa: PLR0911 — early returns per rejection reason are clearer than a flag/break ladder
    """Validate a DirectShow audio-device name loaded from settings.

    ``settings.json`` is hand-editable, so the device name reaches us as raw
    user input before it is glued into an FFmpeg argv element. We accept any
    legitimate Windows audio-device name but reject values that look engineered
    to confuse FFmpeg's dshow demuxer or its argument parsing — leading ``-``
    (which the parser might read as an option), embedded ASCII control
    characters, or absurd length. A rejected value falls back to ``""`` (no
    microphone) so a hostile or fat-fingered file still leaves the app
    starting cleanly.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        logger.warning("Invalid audio_input %r; falling back to %r", value, default)
        return default
    if not value:
        return ""
    if len(value) > _AUDIO_INPUT_MAX_LEN:
        logger.warning(
            "audio_input is %d chars (max %d); falling back to %r",
            len(value),
            _AUDIO_INPUT_MAX_LEN,
            default,
        )
        return default
    if value.lstrip().startswith("-"):
        logger.warning(
            "audio_input %r begins with '-'; falling back to %r", value, default
        )
        return default
    # ASCII control characters (0x00-0x1F, 0x7F) have no place in a real audio
    # device name and could be used to escape from a logged context or upset
    # FFmpeg's stderr handling.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        logger.warning(
            "audio_input %r contains control characters; falling back to %r",
            value,
            default,
        )
        return default
    return value


def _coerce_output_dir(value: Any, default: str) -> str:  # noqa: PLR0911 — early returns per rejection reason are clearer than a flag/break ladder
    """Validate a custom clip output directory loaded from settings.

    Accepts an empty string (meaning "use the platform default") and absolute
    local paths. A non-absolute path is ambiguous (relative to *what*?) and a
    UNC path is a remote-mount footgun — opening one as a write target can
    trigger an outbound SMB auth that leaks the user's NTLM hash. Either
    pattern falls back to ``""`` so the engine uses :func:`app_paths().clips_dir`.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        logger.warning("Invalid output_dir %r; falling back to %r", value, default)
        return default
    if not value:
        return ""
    # UNC paths begin with two separators on either Windows or POSIX style.
    if value.startswith(("\\\\", "//")):
        logger.warning(
            "output_dir %r is a UNC path; falling back to the default location",
            value,
        )
        return ""
    try:
        is_absolute = Path(value).expanduser().is_absolute()
    except (OSError, ValueError) as exc:
        logger.warning("output_dir %r could not be parsed (%s); ignoring", value, exc)
        return ""
    if not is_absolute:
        logger.warning(
            "output_dir %r is not absolute; falling back to the default location", value
        )
        return ""
    return value


def _coerce_encoder(value: Any) -> str:
    """Resolve to a known encoder codec, falling back to the safe libx264 default."""
    if isinstance(value, str) and value in _VALID_ENCODERS:
        return value
    if value is not None:
        logger.warning(
            "Unknown encoder %r; falling back to %s", value, _DEFAULT_ENCODER
        )
    return _DEFAULT_ENCODER


def _coerce_preset(encoder: str, value: Any) -> str:
    """Ensure the preset belongs to the chosen encoder, else pick a sensible one."""
    spec = encoder_by_codec(encoder) or encoder_by_codec(_DEFAULT_ENCODER)
    if spec is None:  # should be unreachable — libx264 is always in ENCODERS
        return _DEFAULT_PRESET

    if isinstance(value, str) and value in spec.presets:
        return value

    # Pick a reasonable fallback. The default preset for libx264 is "veryfast";
    # for other encoders we cannot guarantee that name exists, so we just take
    # the first preset the spec declares.
    fallback = _DEFAULT_PRESET if _DEFAULT_PRESET in spec.presets else spec.presets[0]
    if value is not None:
        logger.warning(
            "Preset %r not valid for %s; falling back to %s", value, encoder, fallback
        )
    return fallback


def _coerce_hotkey(value: Any, default: Hotkey) -> Hotkey:
    """Accept a structured dict or a bare key string."""
    if isinstance(value, Hotkey):
        return value
    if isinstance(value, str) and value:
        # Legacy: ``"F5"`` rather than ``{"key": "F5"}``.
        return Hotkey(key=value)
    if isinstance(value, dict):
        key = value.get("key")
        if isinstance(key, str) and key:
            return Hotkey(
                key=key,
                ctrl=bool(value.get("ctrl", False)),
                shift=bool(value.get("shift", False)),
                alt=bool(value.get("alt", False)),
            )
    if value is not None:
        logger.warning("Invalid hotkey %r; falling back to %s", value, default.to_display())
    return default


def _hotkey_to_dict(hotkey: Hotkey) -> dict[str, Any]:
    """Serialise a :class:`Hotkey` to the canonical four-field JSON shape."""
    return {
        "key": hotkey.key,
        "ctrl": hotkey.ctrl,
        "shift": hotkey.shift,
        "alt": hotkey.alt,
    }
