"""Tests for the encoder benchmark and the recommendation it drives.

The benchmark shells out to FFmpeg, so these tests substitute the timing
function rather than encoding anything. What is worth testing is the judgement
built on top of the measurement: the thresholds, the fallbacks, and above all
that the ladder walk settles on the highest quality preset that still keeps up.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import pytest

from sclip.contracts import Settings
from sclip.core import benchmark as bench
from sclip.core import hardware
from sclip.core.benchmark import EncoderTrial, benchmark_encoder, find_best_configuration
from sclip.core.ffmpeg import FFmpegNotFoundError


def _trial(encoder: str, preset: str, achieved: float, *, fps: int = 60) -> EncoderTrial:
    return EncoderTrial(
        encoder=encoder,
        preset=preset,
        width=2560,
        height=1440,
        fps=fps,
        available=True,
        achieved_fps=achieved,
    )


class TestEncoderTrial:
    def test_headroom_is_achieved_over_target(self) -> None:
        assert _trial("libx264", "veryfast", 180.0).headroom == pytest.approx(3.0)

    def test_headroom_is_zero_when_target_fps_is_nonsense(self) -> None:
        # Guards a division by zero on a corrupted settings file.
        assert _trial("libx264", "veryfast", 180.0, fps=0).headroom == 0.0

    def test_gpu_encoder_clears_a_lower_bar_than_a_cpu_one(self) -> None:
        # A hardware encoder leaves the CPU to the game, so it needs less spare
        # capacity to be trusted with a capture.
        gpu = _trial("h264_nvenc", "p5", 90.0)
        cpu = _trial("libx264", "medium", 90.0)
        assert gpu.required_headroom < cpu.required_headroom
        assert gpu.sustains_capture
        assert not cpu.sustains_capture

    def test_the_configuration_that_stuttered_in_practice_is_rejected(self) -> None:
        # libx264 medium measured about 1.9x at 1440p60 on the development
        # machine and still dropped a fifth of its frames in a real capture,
        # because the encode is only part of what a software encoder costs.
        assert not _trial("libx264", "medium", 60 * 1.9).sustains_capture

    def test_the_configuration_that_worked_is_accepted(self) -> None:
        assert _trial("libx264", "veryfast", 60 * 3.4).sustains_capture
        assert _trial("h264_nvenc", "p5", 60 * 2.6).sustains_capture

    def test_an_unavailable_encoder_never_sustains_capture(self) -> None:
        missing = EncoderTrial(
            encoder="h264_nvenc", preset="p5", width=2560, height=1440, fps=60, available=False
        )
        assert not missing.sustains_capture
        assert "not available" in missing.describe()

    def test_describe_reports_the_measurement(self) -> None:
        described = _trial("h264_nvenc", "p5", 156.0).describe()
        assert "156 fps" in described
        assert "2560x1440" in described
        assert "comfortable" in described


class TestBenchmarkEncoder:
    def test_rate_is_taken_from_the_difference_between_two_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both runs pay a 0.5s fixed start-up cost. 90 frames then cost 1.0s and
        # 180 frames cost 2.0s, so the honest rate is 90 fps, not the 60 fps a
        # single timed run would have reported.
        def fake_time(*_args: object, frames: int, **_kwargs: object) -> float:
            return 0.5 + frames / 90.0

        monkeypatch.setattr(bench, "_time_encode", fake_time)
        trial = benchmark_encoder("h264_nvenc", "p5", width=2560, height=1440, fps=60)
        assert trial.available
        assert trial.achieved_fps == pytest.approx(90.0)

    def test_startup_cost_does_not_leak_into_the_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same encoder measured with a much larger fixed cost must report
        # the same rate; that is the whole point of subtracting two runs.
        rates = []
        for startup in (0.1, 2.0):

            def fake_time(*_a: object, frames: int, _s: float = startup, **_k: object) -> float:
                return _s + frames / 120.0

            monkeypatch.setattr(bench, "_time_encode", fake_time)
            rates.append(
                benchmark_encoder(
                    "libx264", "veryfast", width=1920, height=1080, fps=60
                ).achieved_fps
            )
        assert rates[0] == pytest.approx(rates[1])

    def test_falls_back_to_a_single_run_when_the_difference_is_noise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A negative difference means scheduling noise beat the signal. The
        # single-run figure is pessimistic, which is the safe direction.
        monkeypatch.setattr(
            bench,
            "_time_encode",
            lambda *_a, frames, **_k: 1.0 if frames > 100 else 1.5,
        )
        trial = benchmark_encoder("libx264", "ultrafast", width=1920, height=1080, fps=60)
        assert trial.available
        assert trial.achieved_fps == pytest.approx(180.0)

    def test_an_encoder_that_cannot_run_is_reported_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bench, "_time_encode", lambda *_a, **_k: None)
        trial = benchmark_encoder("h264_nvenc", "p5", width=2560, height=1440, fps=60)
        assert not trial.available
        assert trial.achieved_fps == 0.0

    @pytest.mark.parametrize(
        "failure",
        [
            FFmpegNotFoundError("no ffmpeg"),
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60.0),
            OSError("cannot spawn"),
        ],
    )
    def test_every_ffmpeg_failure_is_contained(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        # A benchmark must never take the settings page down with it.
        def explode(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(bench, "run_ffmpeg", explode)
        trial = benchmark_encoder("libx264", "medium", width=1920, height=1080, fps=60)
        assert not trial.available

    def test_a_nonzero_exit_marks_the_encoder_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bench,
            "run_ffmpeg",
            lambda *_a, **_k: subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            ),
        )
        trial = benchmark_encoder("h264_nvenc", "p5", width=2560, height=1440, fps=60)
        assert not trial.available


class TestFindBestConfiguration:
    def test_the_first_sustainable_encoder_wins_without_walking_further(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hardware encoder that merely keeps up beats a software one that
        # benchmarks faster, because the software encoder spends the cores the
        # game needs. Preference order, not measured speed, decides between
        # encoders; measurement only decides whether a candidate is viable.
        seen: list[str] = []

        def fake(encoder: str, preset: str, **kwargs: object) -> EncoderTrial:
            seen.append(f"{encoder}/{preset}")
            achieved = 60 * (2.0 if encoder == "h264_nvenc" else 8.0)
            return _trial(encoder, preset, achieved)

        monkeypatch.setattr(bench, "benchmark_encoder", fake)
        best, attempts = find_best_configuration(
            ["h264_nvenc", "libx264"], width=2560, height=1440, fps=60
        )
        assert best is not None
        assert best.encoder == "h264_nvenc"
        assert len(attempts) == 1
        assert seen == ["h264_nvenc/p5"]

    def test_the_ladder_settles_on_the_best_preset_that_keeps_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Quality first: only presets that fail the bar are stepped past.
        speeds = {"medium": 2.0, "fast": 2.2, "faster": 2.5, "veryfast": 3.6, "ultrafast": 9.0}

        monkeypatch.setattr(
            bench,
            "benchmark_encoder",
            lambda encoder, preset, **_k: _trial(encoder, preset, 60 * speeds[preset]),
        )
        best, attempts = find_best_configuration(["libx264"], width=2560, height=1440, fps=60)
        assert best is not None
        assert best.preset == "veryfast"
        # It should not have gone on to try the visibly worse ultrafast.
        assert [t.preset for t in attempts] == ["medium", "fast", "faster", "veryfast"]

    def test_a_missing_encoder_is_abandoned_after_one_preset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the encoder itself is absent, other presets cannot rescue it.
        def fake(encoder: str, preset: str, **_kwargs: object) -> EncoderTrial:
            if encoder == "h264_nvenc":
                return EncoderTrial(
                    encoder=encoder,
                    preset=preset,
                    width=2560,
                    height=1440,
                    fps=60,
                    available=False,
                )
            return _trial(encoder, preset, 60 * 5.0)

        monkeypatch.setattr(bench, "benchmark_encoder", fake)
        best, attempts = find_best_configuration(
            ["h264_nvenc", "libx264"], width=2560, height=1440, fps=60
        )
        assert best is not None
        assert best.encoder == "libx264"
        assert sum(1 for t in attempts if t.encoder == "h264_nvenc") == 1

    def test_nothing_sustainable_returns_no_winner_but_keeps_the_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bench,
            "benchmark_encoder",
            lambda encoder, preset, **_k: _trial(encoder, preset, 30.0),
        )
        best, attempts = find_best_configuration(["libx264"], width=3840, height=2160, fps=60)
        assert best is None
        assert attempts  # the failed trials are still reportable to the user


class TestRecommendationUsesTheBenchmark:
    @pytest.fixture
    def registry(self) -> Iterator[object]:
        class Registry:
            def monitors(self) -> list[object]:
                class Monitor:
                    name, width, height, is_primary = "Main", 2560, 1440, True

                return [Monitor()]

            def audio_devices(self) -> list[object]:
                return []

        yield Registry()

    def test_recommend_settings_does_not_benchmark_by_default(
        self, monkeypatch: pytest.MonkeyPatch, registry: object
    ) -> None:
        # First launch must produce a window rather than a wait.
        def explode(**_kwargs: object) -> None:
            raise AssertionError("the benchmark should not run on first launch")

        monkeypatch.setattr(hardware, "measure_encoder_choice", explode)
        monkeypatch.setattr(hardware, "detect_best_encoder", lambda: "h264_nvenc")
        result = hardware.recommend_settings(Settings(), registry)  # type: ignore[arg-type]
        assert result.encoder == "h264_nvenc"

    def test_benchmark_mode_measures_at_the_chosen_display(
        self, monkeypatch: pytest.MonkeyPatch, registry: object
    ) -> None:
        # The measurement has to be taken at the resolution actually selected,
        # not at some fixed reference size.
        captured: dict[str, object] = {}

        def fake_measure(**kwargs: object) -> tuple[str, str, list[EncoderTrial]]:
            captured.update(kwargs)
            return "h264_nvenc", "p5", []

        monkeypatch.setattr(hardware, "measure_encoder_choice", fake_measure)
        result = hardware.recommend_settings(Settings(), registry, benchmark=True)  # type: ignore[arg-type]
        assert (captured["width"], captured["height"]) == (2560, 1440)
        assert result.encoder == "h264_nvenc"
        assert result.preset == "p5"

    def test_measure_falls_back_to_the_probe_when_nothing_keeps_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A machine that cannot sustain its own display still needs an answer,
        # and it should be the fastest thing measured rather than a preset
        # already shown to be too slow.
        attempts = [
            _trial("libx264", "medium", 30.0),
            _trial("libx264", "veryfast", 90.0),
        ]
        monkeypatch.setattr(hardware, "find_best_configuration", lambda *_a, **_k: (None, attempts))
        monkeypatch.setattr(hardware, "detect_best_encoder", lambda: "libx264")
        encoder, preset, reported = hardware.measure_encoder_choice(width=3840, height=2160, fps=60)
        assert encoder == "libx264"
        assert preset == "veryfast"
        assert reported == attempts

    def test_assess_settings_measures_the_users_own_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_benchmark(encoder: str, preset: str, **kwargs: object) -> EncoderTrial:
            captured.update({"encoder": encoder, "preset": preset, **kwargs})
            return _trial(encoder, preset, 120.0)

        monkeypatch.setattr(hardware, "benchmark_encoder", fake_benchmark)
        settings = Settings(resolution="1920x1080", fps=120, encoder="libx264", preset="medium")
        hardware.assess_settings(settings)
        assert captured["encoder"] == "libx264"
        assert captured["preset"] == "medium"
        assert (captured["width"], captured["height"]) == (1920, 1080)
        assert captured["fps"] == 120
