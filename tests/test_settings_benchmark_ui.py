"""Tests for the benchmark controls on the settings page.

The measurement itself is covered in ``test_benchmark.py``. What matters here
is what the page does with a result: whether it reports the finding, whether it
warns when the user's own choices cannot keep up, and whether it throws the
verdict away once it no longer describes the form.

No FFmpeg runs in this module. The handlers are invoked directly with
fabricated results, which is also how the worker calls them at runtime.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from sclip.contracts import Settings
from sclip.core.benchmark import EncoderTrial
from sclip.core.hardware import Recommendation
from sclip.ui.pages import settings_page as page_module
from sclip.ui.pages.settings_page import SettingsPage, _verdict_text


class _Store:
    """In-memory settings store, so no test touches the real config file."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def load(self) -> Settings:
        return self._settings.copy()

    def save(self, settings: Settings) -> None:
        self._settings = settings.copy()


class _Registry:
    """Device registry reporting one 1440p display and no audio hardware."""

    def monitors(self) -> list[object]:
        class _Monitor:
            name, width, height, is_primary = "Primary", 2560, 1440, True

        return [_Monitor()]

    def audio_devices(self) -> list[object]:
        return []


def _trial(encoder: str, preset: str, achieved: float, *, available: bool = True) -> EncoderTrial:
    return EncoderTrial(
        encoder=encoder,
        preset=preset,
        width=2560,
        height=1440,
        fps=60,
        available=available,
        achieved_fps=achieved,
    )


@pytest.fixture()
def page(qapp: QApplication) -> SettingsPage:
    return SettingsPage(_Store(), _Registry())  # type: ignore[arg-type]


class TestVerdictText:
    def test_a_comfortable_result_names_the_measured_rate(self) -> None:
        text = _verdict_text(_trial("h264_nvenc", "p5", 133.0))
        assert "Comfortable" in text
        assert "133 fps" in text
        assert "2.2 times" in text

    def test_a_slow_result_says_what_to_change(self) -> None:
        # This is the configuration that silently stuttered before the
        # benchmark existed, so the wording has to be actionable.
        text = _verdict_text(_trial("libx264", "medium", 110.0))
        assert "Too slow" in text
        assert "110 fps" in text
        assert "stutter" in text
        assert "faster preset" in text

    def test_an_unavailable_encoder_is_reported_as_such(self) -> None:
        text = _verdict_text(_trial("h264_nvenc", "p5", 0.0, available=False))
        assert "does not run on this PC" in text


class TestMeasurementResults:
    def test_a_recommendation_is_adopted_and_explained(self, page: SettingsPage) -> None:
        recommended = Settings(resolution="2560x1440", fps=60, encoder="h264_nvenc", preset="p5")
        page._on_hardware_measured(
            Recommendation(settings=recommended, trial=_trial("h264_nvenc", "p5", 133.0))
        )
        assert page._working.encoder == "h264_nvenc"
        assert "Comfortable" in page._recommended_verdict.text()
        assert page._recommended_verdict.property("role") is None

    def test_a_machine_that_cannot_keep_up_is_told_so(self, page: SettingsPage) -> None:
        # ``trial=None`` means the ladder was exhausted without a winner.
        recommended = Settings(
            resolution="3840x2160", fps=60, encoder="libx264", preset="ultrafast"
        )
        page._on_hardware_measured(Recommendation(settings=recommended, trial=None))
        assert "dropped frames" in page._recommended_verdict.text()
        assert page._recommended_verdict.property("role") == "warning"

    def test_a_failed_measurement_leaves_the_form_alone(self, page: SettingsPage) -> None:
        before = page._working.copy()
        page._on_hardware_measured(None)
        assert page._working == before
        assert "Could not measure" in page._recommended_verdict.text()

    def test_a_slow_setup_is_flagged_as_a_warning(self, page: SettingsPage) -> None:
        page._on_setup_checked(_trial("libx264", "medium", 110.0))
        assert page._check_verdict.property("role") == "warning"
        assert "Too slow" in page._check_verdict.text()

    def test_a_fast_setup_is_not_flagged(self, page: SettingsPage) -> None:
        page._on_setup_checked(_trial("h264_nvenc", "p5", 133.0))
        assert page._check_verdict.property("role") is None

    def test_a_failed_check_says_so(self, page: SettingsPage) -> None:
        page._on_setup_checked(None)
        assert "Could not measure" in page._check_verdict.text()


class TestStaleVerdicts:
    @pytest.mark.parametrize(
        ("change", "value"),
        [
            ("_on_fps_changed", 120),
            ("_on_quality_changed", 30),
            ("_on_resolution_changed", "3840x2160"),
        ],
    )
    def test_editing_a_measured_field_discards_the_verdict(
        self, page: SettingsPage, change: str, value: object
    ) -> None:
        # A "Comfortable" left sitting under settings it was never measured
        # against would be believed, which is worse than showing nothing.
        page._on_setup_checked(_trial("h264_nvenc", "p5", 133.0))
        assert page._check_verdict.text()
        getattr(page, change)(value)
        assert page._check_verdict.text() == ""

    def test_a_measurement_in_flight_keeps_its_label(self, page: SettingsPage) -> None:
        # The running benchmark owns the label and will write the result; a
        # stray field edit must not wipe the progress message.
        page._begin_measuring("Measuring...")
        page._on_fps_changed(120)
        assert page._check_verdict.text() == "Measuring..."


class TestMeasuringState:
    def test_buttons_are_disabled_while_measuring(self, page: SettingsPage) -> None:
        page._begin_measuring("Measuring...")
        assert not page._redetect_button.isEnabled()
        assert not page._check_button.isEnabled()
        page._end_measuring()
        assert page._redetect_button.isEnabled()
        assert page._check_button.isEnabled()

    def test_a_second_run_is_refused_while_one_is_in_flight(
        self, page: SettingsPage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two concurrent benchmarks would contend for the machine and measure
        # each other slow, so the second request has to be dropped.
        started: list[object] = []
        monkeypatch.setattr(page._pool, "start", started.append)
        page._on_redetect_hardware()
        page._on_redetect_hardware()
        page._on_check_setup()
        assert len(started) == 1

    def test_the_benchmark_runs_off_the_gui_thread(
        self, page: SettingsPage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A ladder walk can run for tens of seconds; on the GUI thread that is
        # indistinguishable from a hang.
        monkeypatch.setattr(
            page_module, "recommend_measured", lambda *_a, **_k: pytest.fail("ran inline")
        )
        started: list[object] = []
        monkeypatch.setattr(page._pool, "start", started.append)
        page._on_redetect_hardware()
        assert started  # handed to the pool rather than executed here
