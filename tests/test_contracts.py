"""Tests for the data classes and helpers in :mod:`sclip.contracts`.

These are pure-Python value types with no I/O or threading involved, so the
tests can run very quickly and form the floor of the coverage report.
"""

from __future__ import annotations

import pytest

from sclip.contracts import (
    ENCODERS,
    CaptureMode,
    CaptureState,
    EncoderSpec,
    Hotkey,
    Settings,
    encoder_by_codec,
)

# ----------------------------------------------------------------------------- Hotkey


@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        (Hotkey(key="F5"), "F5"),
        (Hotkey(key="f5"), "F5"),  # case-folded for display
        (Hotkey(key="F5", ctrl=True), "Ctrl+F5"),
        (Hotkey(key="F5", shift=True), "Shift+F5"),
        (Hotkey(key="F5", alt=True), "Alt+F5"),
        (Hotkey(key="F5", ctrl=True, shift=True), "Ctrl+Shift+F5"),
        (Hotkey(key="A", ctrl=True, shift=True, alt=True), "Ctrl+Shift+Alt+A"),
    ],
)
def test_hotkey_to_display_renders_modifier_order(chord: Hotkey, expected: str) -> None:
    """``to_display`` always emits modifiers in Ctrl, Shift, Alt order."""
    assert chord.to_display() == expected


# ---------------------------------------------------------------------------- Settings


def test_settings_copy_is_independent_of_original() -> None:
    """Mutating the copy must not bleed through to the original."""
    # Arrange
    original = Settings()
    copy = original.copy()

    # Act — mutate everything in the copy.
    copy.resolution = "640x480"
    copy.fps = 30
    copy.encoder = "h264_nvenc"
    copy.preset = "p7"
    copy.crf = 30
    copy.audio_input = "Test"
    copy.replay_seconds = 90

    # Assert — the original still holds the contract defaults.
    assert original.resolution == "1920x1080"
    assert original.fps == 60
    assert original.encoder == "libx264"
    assert original.preset == "veryfast"
    assert original.crf == 20
    assert original.audio_input == ""
    assert original.replay_seconds == 30


def test_settings_copy_returns_a_distinct_instance() -> None:
    original = Settings()
    copy = original.copy()
    assert copy is not original
    # Same field values, but distinct objects.
    assert copy == original


def test_settings_default_hotkeys_are_sensible_defaults() -> None:
    """The default hotkeys should be a sane out-of-the-box experience."""
    settings = Settings()
    assert settings.clip_hotkey == Hotkey(key="F5")
    assert settings.record_hotkey == Hotkey(key="F6", ctrl=True)


# ------------------------------------------------------------------ encoder_by_codec


def test_encoder_by_codec_returns_libx264_spec() -> None:
    spec = encoder_by_codec("libx264")
    assert spec is not None
    assert spec.codec == "libx264"
    assert "veryfast" in spec.presets
    assert spec.needs_gpu is False


def test_encoder_by_codec_returns_nvenc_spec_marked_as_gpu() -> None:
    spec = encoder_by_codec("h264_nvenc")
    assert spec is not None
    assert spec.needs_gpu is True
    assert "p1" in spec.presets


def test_encoder_by_codec_returns_none_for_unknown_codec() -> None:
    assert encoder_by_codec("nonsense") is None
    assert encoder_by_codec("") is None


def test_every_declared_encoder_resolves() -> None:
    """Belt-and-braces: every codec in ``ENCODERS`` must resolve via the lookup."""
    for spec in ENCODERS:
        assert isinstance(spec, EncoderSpec)
        assert encoder_by_codec(spec.codec) is spec


# ------------------------------------------------------------------------- CaptureState


def test_capture_state_is_string_enum() -> None:
    """``CaptureState`` should be a string-valued enum so it serialises cleanly."""
    assert CaptureState("recording") is CaptureState.RECORDING
    assert CaptureState.IDLE.value == "idle"
    assert str(CaptureState.RECORDING.value) == "recording"


def test_capture_mode_is_string_enum() -> None:
    """``CaptureMode`` mirrors :class:`CaptureState` — also a string enum."""
    assert CaptureMode("replay_buffer") is CaptureMode.REPLAY_BUFFER
    assert CaptureMode("manual") is CaptureMode.MANUAL
