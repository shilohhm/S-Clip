"""Hardware probing and recommended-settings derivation.

S-Clip tries to be useful the moment it is opened, with no trip to a settings
screen. On the first launch it inspects the machine - which hardware encoder
actually works, what the primary display is, which audio devices exist - and
writes a tuned configuration. The user can still take the wheel through the
Advanced settings, but they should never *have* to.

The encoder check is deliberately empirical. FFmpeg reports ``h264_nvenc`` as
a compiled-in encoder on every machine, whether or not an NVIDIA GPU is
present, so the only trustworthy test is to ask FFmpeg to encode a couple of
throwaway frames and see whether it succeeds.

There are two strengths of check here, and the difference matters. The probe
asks only whether an encoder *runs*, which is quick enough for a first launch
where the user is waiting to see a window. The benchmark in
:mod:`sclip.core.benchmark` asks whether it runs *fast enough for the display
it is pointed at*, which is a better answer and takes seconds rather than
milliseconds. So the probe is the default and the benchmark is what the
Re-detect action runs, where the user has asked for the measurement and can be
shown it happening.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from sclip.contracts import DeviceRegistry, Settings, encoder_by_codec, encoder_label
from sclip.core.benchmark import EncoderTrial, benchmark_encoder, find_best_configuration
from sclip.core.ffmpeg import FFmpegNotFoundError, parse_resolution, run_ffmpeg

logger = logging.getLogger(__name__)


# Encoders to try, best first. A hardware encoder keeps the capture off the
# CPU, which is exactly what a gamer wants - the game keeps the headroom.
# libx264 is the universal software fallback and always succeeds.
_ENCODER_PRIORITY: tuple[str, ...] = ("h264_nvenc", "h264_amf", "h264_qsv", "libx264")

# ``encoder_label`` and ``_ENCODER_LABELS`` now live in ``sclip.contracts`` so
# that the UI can import them without touching a core implementation module.
# The import above re-exports ``encoder_label`` for any callers that reach it
# via this module rather than directly via the contracts.

# Recommended speed preset per encoder family. The hardware encoders are given
# a balanced preset - they have the headroom - while the CPU encoder is kept
# brisk so a recording never starves the game of processor time.
_RECOMMENDED_PRESET: dict[str, str] = {
    "h264_nvenc": "p5",
    "hevc_nvenc": "p5",
    "h264_amf": "balanced",
    "h264_qsv": "medium",
    "libx264": "veryfast",
}

# A constant-quality target that looks clean without producing needlessly
# large files. Lower is better quality; the low twenties is the sweet spot
# for screen content.
_RECOMMENDED_CRF: int = 21

# 60 fps is the universal clip frame rate: smooth, widely accepted by every
# sharing platform, and a sensible file size. Higher rates are available in
# the Advanced settings for anyone who wants them.
_RECOMMENDED_FPS: int = 60


def _probe_encoder(codec: str) -> bool:
    """Return ``True`` if FFmpeg can actually encode with ``codec`` here.

    Encodes a fraction of a second of a synthetic black clip to the null
    muxer. A hardware encoder with no matching GPU fails immediately, so this
    is a quick and trustworthy capability test.
    """
    try:
        result = run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x144:r=30:d=0.2",
                "-c:v",
                codec,
                "-f",
                "null",
                "-",
            ],
            timeout=20.0,
        )
    except FFmpegNotFoundError:
        logger.warning("FFmpeg not found while probing encoder %s", codec)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Encoder probe for %s timed out", codec)
        return False
    except OSError as exc:
        logger.warning("Encoder probe for %s could not run: %s", codec, exc)
        return False
    return result.returncode == 0


def detect_best_encoder() -> str:
    """Pick the best encoder this machine can actually use.

    Walks the priority list and returns the first codec that survives a probe
    encode. libx264 is last and always works, so a sensible value is always
    returned.
    """
    for codec in _ENCODER_PRIORITY:
        if _probe_encoder(codec):
            logger.info("Recommended encoder: %s", codec)
            return codec
    # _ENCODER_PRIORITY ends in libx264, so reaching here means even the
    # software encoder failed - most likely FFmpeg itself is missing.
    logger.warning("No encoder passed the probe; defaulting to libx264")
    return "libx264"


def _recommended_preset(encoder: str) -> str:
    """Return a sensible preset for ``encoder``, validated against its spec."""
    preset = _RECOMMENDED_PRESET.get(encoder, "veryfast")
    spec = encoder_by_codec(encoder)
    if spec is not None and preset not in spec.presets:
        # The preferred preset is not offered by this encoder - fall back to
        # the middle of whatever it does offer, which is a fair speed/quality
        # compromise.
        preset = spec.presets[len(spec.presets) // 2]
    return preset


def measure_encoder_choice(
    *, width: int, height: int, fps: int, quality: int = _RECOMMENDED_CRF
) -> tuple[str, str, list[EncoderTrial]]:
    """Choose an encoder and preset by measuring them at a real target.

    Returns the chosen pair along with every trial run, so a caller can explain
    the choice. Falls back to the capability probe when nothing clears the bar,
    which is the honest outcome on a machine that genuinely cannot sustain the
    requested display: something still has to be recommended, and the fastest
    preset of the best available encoder is the closest thing to a right answer.
    """
    best, attempts = find_best_configuration(
        list(_ENCODER_PRIORITY), width=width, height=height, fps=fps, quality=quality
    )
    if best is not None:
        return best.encoder, best.preset, attempts

    logger.warning(
        "No encoder sustained %dx%d at %d fps; falling back to the capability probe",
        width,
        height,
        fps,
    )
    encoder = detect_best_encoder()
    # The measured ladder was exhausted, so ask for speed over quality rather
    # than repeating a preset already shown to be too slow.
    fastest = next(
        (t.preset for t in reversed(attempts) if t.encoder == encoder and t.available),
        _recommended_preset(encoder),
    )
    return encoder, fastest, attempts


def assess_settings(settings: Settings) -> EncoderTrial:
    """Measure whether ``settings`` can actually be captured on this machine.

    This is the check that was missing. Advanced mode will happily accept an
    encoder and preset that cannot keep up with the chosen display, and the
    symptom is not an error: it is a clip that stutters, because dropped frames
    are silently replaced by repeats of the frame before.
    """
    width, height = parse_resolution(settings.resolution)
    return benchmark_encoder(
        settings.encoder,
        settings.preset,
        width=width,
        height=height,
        fps=settings.fps,
        quality=settings.crf,
    )


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A recommended configuration together with the evidence behind it.

    The measurement is worth carrying alongside the settings rather than being
    thrown away. A user who has just waited for a benchmark is owed the number
    it produced: "S-Clip picked NVENC" invites the question the trial already
    answers.
    """

    settings: Settings
    # ``None`` when the choice came from the capability probe rather than a
    # measurement, which is the case on first launch.
    trial: EncoderTrial | None


def recommend_measured(base: Settings, registry: DeviceRegistry) -> Recommendation:
    """Benchmark the machine and recommend what it can actually sustain."""
    return _build_recommendation(base, registry, benchmark=True)


def recommend_settings(
    base: Settings, registry: DeviceRegistry, *, benchmark: bool = False
) -> Settings:
    """Derive a hardware-tuned :class:`Settings` from a base configuration.

    The capture-related fields (encoder, preset, quality, resolution, monitor,
    audio devices) are replaced with detected values. The user's own
    preferences - hotkeys, replay-buffer length, clip folder - are carried
    across from ``base`` untouched.

    With ``benchmark`` set, the encoder and preset are chosen by timing them at
    the display that was just selected rather than by a capability probe. That
    costs seconds rather than milliseconds, so it is off by default: first
    launch should produce a window, not a wait.

    Use :func:`recommend_measured` instead when the measurement itself is
    wanted, rather than only the settings derived from it.
    """
    return _build_recommendation(base, registry, benchmark=benchmark).settings


def _build_recommendation(
    base: Settings, registry: DeviceRegistry, *, benchmark: bool
) -> Recommendation:
    """Do the work behind both public recommendation entry points."""
    monitor_name, resolution = _recommend_display(base, registry)
    trial: EncoderTrial | None = None

    if benchmark:
        width, height = parse_resolution(resolution)
        encoder, preset, attempts = measure_encoder_choice(
            width=width, height=height, fps=_RECOMMENDED_FPS
        )
        trial = next(
            (t for t in reversed(attempts) if t.encoder == encoder and t.preset == preset), None
        )
    else:
        encoder = detect_best_encoder()
        preset = _recommended_preset(encoder)

    microphone = _recommend_microphone(registry)

    recommended = Settings(
        resolution=resolution,
        fps=_RECOMMENDED_FPS,
        encoder=encoder,
        preset=preset,
        crf=_RECOMMENDED_CRF,
        audio_input=microphone,
        capture_audio=True,
        # Desktop audio is captured through the WASAPI loopback pump, which
        # works on any modern Windows machine, so it is on by default.
        capture_desktop_audio=True,
        replay_buffer=base.replay_buffer,
        replay_seconds=base.replay_seconds,
        monitor=monitor_name,
        clip_hotkey=base.clip_hotkey,
        record_hotkey=base.record_hotkey,
        output_dir=base.output_dir,
        auto_configure=True,
    )
    logger.info(
        "Recommended: %s / %s @ %dfps on %s (mic=%r, desktop audio on)",
        encoder,
        resolution,
        _RECOMMENDED_FPS,
        monitor_name,
        microphone or "none",
    )
    return Recommendation(settings=recommended, trial=trial)


def _recommend_display(base: Settings, registry: DeviceRegistry) -> tuple[str, str]:
    """Choose the monitor to capture and its native resolution."""
    try:
        monitors = registry.monitors()
    except Exception:
        logger.exception("Monitor enumeration failed during recommendation")
        monitors = []

    if not monitors:
        return base.monitor, base.resolution

    primary = next((m for m in monitors if m.is_primary), monitors[0])
    return primary.name, f"{primary.width}x{primary.height}"


def _recommend_microphone(registry: DeviceRegistry) -> str:
    """Choose a microphone from the available capture devices.

    Comes back empty on a machine with no microphone, which the capture
    engine and the settings screen both cope with. Desktop audio is not
    chosen here - it is captured by the WASAPI loopback pump, which needs no
    device to be picked.
    """
    try:
        devices = registry.audio_devices()
    except Exception:
        logger.exception("Audio enumeration failed during recommendation")
        return ""

    return next((d.name for d in devices if d.kind == "input"), "")


__all__ = [
    "Recommendation",
    "assess_settings",
    "detect_best_encoder",
    "encoder_label",
    "measure_encoder_choice",
    "recommend_measured",
    "recommend_settings",
]
