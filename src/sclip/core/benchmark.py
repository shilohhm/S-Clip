"""Measure what this machine can actually encode, rather than guessing.

Capability detection answers "does this encoder run here", which is not the
question a recorder needs answering. An encoder can run perfectly well and
still be far too slow for the resolution and frame rate it is pointed at, and
the result is not an error message: it is a clip that judders, because frames
were dropped and the constant-rate output quietly duplicated the ones before
them.

That failure was reachable in practice. Choosing libx264 at the ``medium``
preset for 1440p60 measured 227 percent of a CPU core and dropped one frame in
five, and nothing in the interface said so.

Each candidate is timed encoding a synthetic clip at the user's real target
resolution and frame rate. Nothing is captured from the screen and no file is
written, so the measurement is quick, repeatable, and reveals nothing.

A note on thresholds, because the obvious one is wrong. Sustaining real time is
not enough. The measured figure covers the encode alone, while a live capture
also pays for frame acquisition, muxing and disk, and on a CPU encoder it pays
to copy every frame out of GPU memory as well. Worse, the machine is
simultaneously running the game worth recording. So the bar is set well above
1.0, and higher for encoders that run on the CPU, where that copy competes for
the very cores doing the encoding.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

from sclip.contracts import encoder_by_codec
from sclip.core.ffmpeg import (
    FFmpegNotFoundError,
    build_quality_args,
    encoder_is_gpu_native,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)

# Seconds of synthetic video in the shorter of the two timed runs. See
# ``benchmark_encoder`` for why there are two.
_TRIAL_SECONDS: float = 1.5

# How much faster than real time an encoder must run to be trusted with a live
# capture, measured on the encode alone.
#
# A GPU encoder needs comparatively little: it does not touch the frames on
# their way out of Desktop Duplication, and it burns almost no CPU, so the rest
# of the pipeline and the game are barely affected by it.
_GPU_HEADROOM: float = 1.25

# A CPU encoder needs far more, and this figure is calibrated rather than
# chosen. libx264 at ``medium`` measured 1.67x here and still dropped a fifth of
# its frames in a real capture, because the encode is only part of what it costs
# that machine. ``veryfast`` measured 3.35x and is the preset this bar is meant
# to admit.
_CPU_HEADROOM: float = 3.0

# Guards against a wedged encoder holding up the whole recommendation.
_TRIAL_TIMEOUT: float = 60.0


@dataclass(frozen=True, slots=True)
class EncoderTrial:
    """What one encoder and preset managed at the requested target."""

    encoder: str
    preset: str
    width: int
    height: int
    fps: int
    available: bool
    achieved_fps: float = 0.0

    @property
    def headroom(self) -> float:
        """Achieved rate as a multiple of the target. 1.0 is exactly real time."""
        if self.fps <= 0:
            return 0.0
        return self.achieved_fps / self.fps

    @property
    def required_headroom(self) -> float:
        """The bar this encoder has to clear, which depends on where it runs."""
        return _GPU_HEADROOM if encoder_is_gpu_native(self.encoder) else _CPU_HEADROOM

    @property
    def sustains_capture(self) -> bool:
        """Whether this combination can be trusted with a live capture."""
        return self.available and self.headroom >= self.required_headroom

    def describe(self) -> str:
        """One line fit to show a user."""
        if not self.available:
            return f"{self.encoder} is not available on this PC"
        verdict = "comfortable" if self.sustains_capture else "too slow"
        return (
            f"{self.encoder} {self.preset}: {self.achieved_fps:.0f} fps at "
            f"{self.width}x{self.height} ({self.headroom:.1f}x real time, {verdict})"
        )


def _time_encode(
    encoder: str, preset: str, *, width: int, height: int, fps: int, quality: int, frames: int
) -> float | None:
    """Wall-clock seconds to encode ``frames`` synthetic frames, or ``None`` on failure."""
    argv = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}",
        "-frames:v",
        str(frames),
        "-c:v",
        encoder,
        "-preset",
        preset,
        *(["-tune", "hq"] if encoder.endswith("_nvenc") else []),
        *build_quality_args(encoder, quality),
        "-f",
        "null",
        "-",
    ]
    started = time.monotonic()
    try:
        result = run_ffmpeg(argv, timeout=_TRIAL_TIMEOUT)
    except FFmpegNotFoundError:
        logger.warning("FFmpeg not found while benchmarking %s", encoder)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Benchmark for %s %s timed out", encoder, preset)
        return None
    except OSError as exc:
        logger.warning("Benchmark for %s %s could not run: %s", encoder, preset, exc)
        return None
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        logger.debug("Encoder %s %s is unavailable here", encoder, preset)
        return None
    return elapsed


def benchmark_encoder(
    encoder: str,
    preset: str,
    *,
    width: int,
    height: int,
    fps: int,
    quality: int = 21,
    seconds: float = _TRIAL_SECONDS,
) -> EncoderTrial:
    """Measure what ``encoder`` sustains at the given target.

    Output goes to the null muxer, so this measures the encoder rather than the
    disk, and leaves nothing behind.

    The same clip is encoded at two lengths and the rate is taken from the
    *difference* between them. Timing a single run would fold FFmpeg's start-up
    into the result, and that is not a small effect at these durations: it
    understated a hardware encoder by roughly a quarter here, because setting up
    an NVENC session is markedly more expensive than starting libx264. Charging
    a fixed cost against the encoder least able to spare it is exactly the wrong
    bias, and subtracting two runs cancels it without needing to know what it is.
    """
    unavailable = EncoderTrial(
        encoder=encoder, preset=preset, width=width, height=height, fps=fps, available=False
    )
    short_frames = max(1, round(fps * seconds))
    long_frames = short_frames * 2

    short_elapsed = _time_encode(
        encoder, preset, width=width, height=height, fps=fps, quality=quality, frames=short_frames
    )
    if short_elapsed is None:
        return unavailable
    long_elapsed = _time_encode(
        encoder, preset, width=width, height=height, fps=fps, quality=quality, frames=long_frames
    )
    if long_elapsed is None:
        return unavailable

    marginal = long_elapsed - short_elapsed
    if marginal > 0:
        achieved = short_frames / marginal
    else:
        # Scheduling noise swamped the difference, which only happens when the
        # encode is far quicker than the start-up it was meant to cancel. The
        # single-run figure is pessimistic but still safe to act on.
        achieved = long_frames / long_elapsed if long_elapsed > 0 else 0.0

    trial = EncoderTrial(
        encoder=encoder,
        preset=preset,
        width=width,
        height=height,
        fps=fps,
        available=True,
        achieved_fps=achieved,
    )
    logger.info("Benchmark: %s", trial.describe())
    return trial


def _presets_to_try(encoder: str) -> list[str]:
    """Candidate presets for one encoder, best quality first.

    Ordered so the search settles on the highest quality that still clears the
    bar, rather than defaulting to the fastest and leaving quality on the table.
    """
    spec = encoder_by_codec(encoder)
    if spec is None:
        return []
    if encoder.endswith("_nvenc"):
        # p7 is slowest/best, p1 fastest. Measurement showed p7 buys nothing
        # over p5 on this pipeline, so p5 leads.
        preferred = ["p5", "p4", "p3", "p2", "p1"]
    elif encoder == "libx264":
        preferred = ["medium", "fast", "faster", "veryfast", "superfast", "ultrafast"]
    else:
        preferred = list(spec.presets)
    return [preset for preset in preferred if preset in spec.presets] or list(spec.presets)


def find_best_configuration(
    candidates: list[str],
    *,
    width: int,
    height: int,
    fps: int,
    quality: int = 21,
) -> tuple[EncoderTrial | None, list[EncoderTrial]]:
    """Benchmark ``candidates`` and return the best sustainable one.

    Returns the winning trial (or ``None`` if nothing clears the bar) alongside
    every trial run, so the interface can explain the choice rather than just
    announce it.

    Encoders are tried in the order given, and the first that sustains capture
    wins: the list is a preference order, so a hardware encoder that is merely
    good enough is still preferable to a CPU encoder that benchmarks faster but
    would spend the machine's cores doing it.
    """
    attempts: list[EncoderTrial] = []
    for encoder in candidates:
        for preset in _presets_to_try(encoder):
            trial = benchmark_encoder(
                encoder, preset, width=width, height=height, fps=fps, quality=quality
            )
            attempts.append(trial)
            if not trial.available:
                break  # the encoder itself is missing; other presets cannot help
            if trial.sustains_capture:
                return trial, attempts
    return None, attempts


__all__ = [
    "EncoderTrial",
    "benchmark_encoder",
    "find_best_configuration",
]
