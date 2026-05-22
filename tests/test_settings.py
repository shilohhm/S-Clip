"""Tests for :class:`sclip.core.settings.JsonSettingsStore`.

The store has three jobs: tolerate broken or legacy files on load, write
atomically on save, and validate every field on the way in and out. Each
test below targets exactly one of those responsibilities so a failure
points at the misbehaving slice rather than the whole stack.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from sclip.contracts import Hotkey, Settings
from sclip.core.settings import JsonSettingsStore

# --------------------------------------------------------------------------- load


def test_load_missing_file_returns_defaults(tmp_settings_file: Path) -> None:
    # Arrange — no file on disk.
    assert not tmp_settings_file.exists()
    store = JsonSettingsStore(tmp_settings_file)

    # Act
    loaded = store.load()

    # Assert — every field matches the contract's default.
    assert loaded == Settings()


def test_load_invalid_json_returns_defaults(tmp_settings_file: Path) -> None:
    tmp_settings_file.write_text("{not-valid-json", encoding="utf-8")
    store = JsonSettingsStore(tmp_settings_file)

    loaded = store.load()

    assert loaded == Settings()


def test_load_non_object_returns_defaults(tmp_settings_file: Path) -> None:
    tmp_settings_file.write_text("[1, 2, 3]", encoding="utf-8")
    store = JsonSettingsStore(tmp_settings_file)

    loaded = store.load()

    assert loaded == Settings()


# ----------------------------------------------------------------------- round-trip


def test_save_then_load_round_trips_every_field(tmp_settings_file: Path) -> None:
    # Arrange — populate every field with a non-default value so the round-trip
    # has something to verify against.
    original = Settings(
        resolution="2560x1440",
        fps=120,
        encoder="h264_nvenc",
        preset="p5",
        crf=18,
        audio_input="Microphone (Test)",
        capture_audio=False,
        capture_desktop_audio=False,
        replay_buffer=False,
        replay_seconds=60,
        monitor="Monitor 2",
        clip_hotkey=Hotkey(key="F8", ctrl=True, shift=True),
        record_hotkey=Hotkey(key="F9", alt=True),
        output_dir="D:/clips",
        auto_configure=False,
    )
    store = JsonSettingsStore(tmp_settings_file)

    # Act
    store.save(original)
    reloaded = store.load()

    # Assert
    assert reloaded == original


# ------------------------------------------------------------------------- migration


def test_legacy_audio_device_migrates_to_audio_input(tmp_settings_file: Path) -> None:
    """Old files used ``audio_device``; the new schema uses ``audio_input``."""
    tmp_settings_file.write_text(
        json.dumps({"audio_device": "Analogue 1 (Focusrite)"}),
        encoding="utf-8",
    )

    loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.audio_input == "Analogue 1 (Focusrite)"


def test_legacy_string_hotkey_migrates_to_structured_clip_hotkey(
    tmp_settings_file: Path,
) -> None:
    """Old files stored ``hotkey: "F5"``; the new schema needs a structured chord."""
    tmp_settings_file.write_text(
        json.dumps({"hotkey": "F5"}),
        encoding="utf-8",
    )

    loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.clip_hotkey == Hotkey(key="F5")


def test_partial_migration_does_not_overwrite_new_keys(tmp_settings_file: Path) -> None:
    """If both old and new keys are present the new key wins."""
    tmp_settings_file.write_text(
        json.dumps(
            {
                "audio_device": "Old",
                "audio_input": "New",
                "hotkey": "F5",
                "clip_hotkey": {"key": "F8", "shift": True},
            }
        ),
        encoding="utf-8",
    )

    loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.audio_input == "New"
    assert loaded.clip_hotkey == Hotkey(key="F8", shift=True)


# -------------------------------------------------------------------------- atomic


def test_save_is_atomic_against_temp_file_failure(
    tmp_settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure halfway through the temp-file write must leave the old file intact."""
    store = JsonSettingsStore(tmp_settings_file)

    # Seed the destination with a known-good payload so we can verify it
    # survives the failed save attempt below.
    original = Settings(resolution="1280x720", fps=30)
    store.save(original)
    on_disk_before = tmp_settings_file.read_text(encoding="utf-8")
    assert tmp_settings_file.exists()

    # Patch ``Path.write_text`` so the very next attempt (the temp file
    # during the second save) blows up midway. We bind the original method
    # and forward calls that are not the doomed one through unchanged.
    real_write_text = Path.write_text

    def exploding_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.suffix == ".tmp":
            raise OSError("Simulated mid-write failure")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", exploding_write_text)

    # Try (and fail) to save a different settings object.
    with pytest.raises(OSError):
        store.save(Settings(resolution="3840x2160", fps=60))

    # The destination file's content must not have changed.
    assert tmp_settings_file.read_text(encoding="utf-8") == on_disk_before
    # And no stray .tmp file should be left behind.
    assert not tmp_settings_file.with_suffix(tmp_settings_file.suffix + ".tmp").exists()


# ----------------------------------------------------------------------- validation


def test_invalid_resolution_falls_back_to_default_and_warns(
    tmp_settings_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tmp_settings_file.write_text(
        json.dumps({"resolution": "not-a-resolution"}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="sclip.core.settings"):
        loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.resolution == Settings().resolution
    assert any("not-a-resolution" in record.getMessage() for record in caplog.records)


def test_unknown_encoder_falls_back_to_libx264(
    tmp_settings_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tmp_settings_file.write_text(
        json.dumps({"encoder": "videotoaster_3000"}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="sclip.core.settings"):
        loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.encoder == "libx264"
    assert any("videotoaster_3000" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("written_fps", "expected_fps"),
    [
        (0, 1),  # below the floor — clamps up to 1
        (-30, 1),  # negative — clamps up to 1
        (480, 240),  # above the ceiling — clamps down to 240
        ("sixty", Settings().fps),  # nonsense — falls back to the default
    ],
)
def test_fps_outside_supported_range_is_clamped_or_rejected(
    tmp_settings_file: Path,
    written_fps: int | str,
    expected_fps: int,
) -> None:
    tmp_settings_file.write_text(
        json.dumps({"fps": written_fps}),
        encoding="utf-8",
    )

    loaded = JsonSettingsStore(tmp_settings_file).load()

    assert loaded.fps == expected_fps


def test_hotkey_round_trip_preserves_modifiers(tmp_settings_file: Path) -> None:
    """A hotkey written with every modifier set should re-emerge identical."""
    chord = Hotkey(key="F12", ctrl=True, shift=True, alt=True)
    settings = Settings(clip_hotkey=chord, record_hotkey=Hotkey(key="A", alt=True))
    store = JsonSettingsStore(tmp_settings_file)

    store.save(settings)
    reloaded = store.load()

    assert reloaded.clip_hotkey == chord
    assert reloaded.record_hotkey == Hotkey(key="A", alt=True)


def test_save_normalises_invalid_settings_on_write(tmp_settings_file: Path) -> None:
    """If a programmatic caller hands us nonsense, the file on disk is still sane."""
    # ``Settings`` is mutable on purpose so the GUI can edit a draft; here we
    # deliberately mutate it into an invalid state to exercise the save-side
    # validation.
    settings = Settings()
    settings.encoder = "fake_encoder"
    settings.fps = 9_999
    store = JsonSettingsStore(tmp_settings_file)

    store.save(settings)
    raw = json.loads(tmp_settings_file.read_text(encoding="utf-8"))

    assert raw["encoder"] == "libx264"
    assert raw["fps"] == 240  # clamped to the upper bound
