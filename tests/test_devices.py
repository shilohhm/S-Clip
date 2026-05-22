"""Tests for :mod:`sclip.core.devices`.

We are interested in the parsing logic, the cache invalidation rules and
the friendliness of the failure paths. Anything that would shell out to a
real FFmpeg binary is reserved for the ``ffmpeg`` mark so unit runs stay
fast and deterministic.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from sclip.contracts import AudioDevice, Monitor
from sclip.core import devices as devices_module
from sclip.core.devices import SystemDeviceRegistry, _parse_dshow_devices

# A representative dshow stderr blob, modelled on the real FFmpeg 7.x output.
# Each device appears as a quoted display name followed by ``(audio)`` or
# ``(video)`` on the same line, then a second line carrying the alternative
# name we want to ignore. We include an output-side section header so the
# parser has to use section context to tell input mics from playback devices.
_ALT_MIC1 = (
    "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}"
    "\\wave_{F2C6C6B0-CC30-4862-A5D6-13B14EFFE777}"
)
_ALT_MIC2 = (
    "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}"
    "\\wave_{6BFD0BBB-1C7A-4F8C-AF44-7C0E5E7E7000}"
)
_ALT_SPK = "@device_sw_{0.0.0.00000000}.{12345678-90AB-CDEF-1234-567890ABCDEF}"
_REAL_DSHOW_OUTPUT: str = (
    "[dshow @ 0x000001f400000000] DirectShow audio devices\n"
    '[dshow @ 0x000001f400000000] "Microphone (Realtek(R) Audio)" (audio)\n'
    f'[dshow @ 0x000001f400000000]   Alternative name "{_ALT_MIC1}"\n'
    '[dshow @ 0x000001f400000000] "Line In (Focusrite USB Audio)" (audio)\n'
    f'[dshow @ 0x000001f400000000]   Alternative name "{_ALT_MIC2}"\n'
    "[dshow @ 0x000001f400000000] DirectShow audio output devices\n"
    '[dshow @ 0x000001f400000000] "Speakers (Realtek(R) Audio)" (audio)\n'
    f'[dshow @ 0x000001f400000000]   Alternative name "{_ALT_SPK}"\n'
    "[dshow @ 0x000001f400000000] DirectShow video devices\n"
    '[dshow @ 0x000001f400000000] "Integrated Webcam" (video)\n'
    "[dshow @ 0x000001f400000000]   Alternative name "
    '"@device_pnp_\\\\?\\\\usb#vid_0bda&pid_5650"\n'
)


# ----------------------------------------------------------------- parsing


def test_parse_dshow_devices_finds_input_endpoints() -> None:
    devices = _parse_dshow_devices(_REAL_DSHOW_OUTPUT)

    input_names = [d.name for d in devices if d.kind == "input"]
    assert "Microphone (Realtek(R) Audio)" in input_names
    assert "Line In (Focusrite USB Audio)" in input_names


def test_parse_dshow_devices_marks_output_section_as_output() -> None:
    devices = _parse_dshow_devices(_REAL_DSHOW_OUTPUT)

    speakers = next(
        (d for d in devices if d.name == "Speakers (Realtek(R) Audio)"),
        None,
    )
    assert speakers is not None, "Speakers device should appear in the parsed list"
    assert speakers.kind == "output"


def test_parse_dshow_devices_skips_video_devices() -> None:
    devices = _parse_dshow_devices(_REAL_DSHOW_OUTPUT)

    assert all("webcam" not in d.name.lower() for d in devices)
    assert all(d.kind in {"input", "output"} for d in devices)


def test_parse_dshow_devices_returns_empty_for_empty_input() -> None:
    assert _parse_dshow_devices("") == []


def test_parse_dshow_devices_deduplicates_repeated_entries() -> None:
    """Some FFmpeg builds repeat the same device entry — only one should reach us."""
    blob = (
        '[dshow @ 0x1] DirectShow audio devices\n'
        '[dshow @ 0x1] "Mic A" (audio)\n'
        '[dshow @ 0x1]   Alternative name "@device_cm_xxx"\n'
        '[dshow @ 0x1] "Mic A" (audio)\n'
        '[dshow @ 0x1]   Alternative name "@device_cm_xxx"\n'
    )

    devices = _parse_dshow_devices(blob)

    assert devices == [AudioDevice(name="Mic A", kind="input")]


# ----------------------------------------------------------------- audio_devices()


@dataclass
class _FakeCompletedProcess:
    """A minimal stand-in for :class:`subprocess.CompletedProcess`."""

    returncode: int
    stderr: str
    stdout: str = ""


def test_audio_devices_runs_ffmpeg_and_parses_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``audio_devices()`` should shell out exactly once and parse the result."""
    monkeypatch.setattr(devices_module, "_resolve_ffmpeg", lambda: "C:/fake/ffmpeg.exe")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=1, stderr=_REAL_DSHOW_OUTPUT)

    monkeypatch.setattr(subprocess, "run", fake_run)

    registry = SystemDeviceRegistry()
    devices = registry.audio_devices()

    assert len(calls) == 1, "FFmpeg should only be invoked once"
    cmd = calls[0]
    assert cmd[0] == "C:/fake/ffmpeg.exe"
    assert "-list_devices" in cmd
    assert AudioDevice(name="Microphone (Realtek(R) Audio)", kind="input") in devices


def test_audio_devices_returns_empty_when_ffmpeg_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devices_module, "_resolve_ffmpeg", lambda: None)
    registry = SystemDeviceRegistry()

    assert registry.audio_devices() == []


def test_audio_devices_returns_empty_when_ffmpeg_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devices_module, "_resolve_ffmpeg", lambda: "C:/fake/ffmpeg.exe")

    def fake_run(*_args: object, **_kwargs: object) -> _FakeCompletedProcess:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = SystemDeviceRegistry()

    assert registry.audio_devices() == []


# ---------------------------------------------------------------- monitors()


def _fake_screeninfo_monitor(
    *, x: int, y: int, width: int, height: int, is_primary: bool = False
) -> object:
    """Build a duck-typed screeninfo.Monitor stand-in."""

    @dataclass
    class FakeMonitor:
        x: int
        y: int
        width: int
        height: int
        is_primary: bool

    return FakeMonitor(x=x, y=y, width=width, height=height, is_primary=is_primary)


def _install_fake_screeninfo(
    monkeypatch: pytest.MonkeyPatch,
    get_monitors_impl: object,
) -> None:
    """Drop a fake ``screeninfo`` module into ``sys.modules`` for the test.

    ``_enumerate_monitors`` imports ``screeninfo`` lazily inside the function
    body, so a module-level stub here is enough — we do not have to import
    the real package first.
    """
    import sys
    import types

    fake_module = types.ModuleType("screeninfo")
    fake_module.get_monitors = get_monitors_impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "screeninfo", fake_module)


def test_monitors_returns_screeninfo_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_monitors = [
        _fake_screeninfo_monitor(x=0, y=0, width=1920, height=1080, is_primary=True),
        _fake_screeninfo_monitor(x=1920, y=0, width=2560, height=1440),
    ]
    _install_fake_screeninfo(monkeypatch, lambda: fake_monitors)

    registry = SystemDeviceRegistry()
    monitors = registry.monitors()

    assert len(monitors) == 2
    assert monitors[0] == Monitor(
        name="Monitor 1", x=0, y=0, width=1920, height=1080, is_primary=True
    )
    assert monitors[1] == Monitor(name="Monitor 2", x=1920, y=0, width=2560, height=1440)


def test_monitors_returns_empty_when_screeninfo_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving screeninfo install should not bring down the app."""

    def explode() -> list[object]:
        raise RuntimeError("screeninfo broke")

    _install_fake_screeninfo(monkeypatch, explode)

    registry = SystemDeviceRegistry()
    assert registry.monitors() == []


# --------------------------------------------------------------- refresh()


def test_refresh_invalidates_cached_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices_module, "_resolve_ffmpeg", lambda: "C:/fake/ffmpeg.exe")

    # First call returns one device; refresh; next call returns two.
    state: dict[str, str] = {"blob": _REAL_DSHOW_OUTPUT}

    def fake_run(*_args: object, **_kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=1, stderr=state["blob"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    registry = SystemDeviceRegistry()
    first = registry.audio_devices()

    # Swap the source data and verify the cache still serves the first result.
    state["blob"] = ""
    cached = registry.audio_devices()
    assert cached == first

    # After refresh, the next call sees the new (empty) source.
    registry.refresh()
    assert registry.audio_devices() == []


# ------------------------------------------------------------- live FFmpeg


@pytest.mark.ffmpeg
def test_live_ffmpeg_audio_devices_returns_correct_types() -> None:
    """Smoke test that exercises the real FFmpeg binary on the host.

    Skipped automatically when no FFmpeg binary is available on PATH or via
    the bundled lookup. We only assert on the *type* of the result — the
    concrete devices vary from machine to machine.
    """
    if devices_module._resolve_ffmpeg() is None:
        pytest.skip("FFmpeg not available on this machine")

    registry = SystemDeviceRegistry()
    audio = registry.audio_devices()

    assert isinstance(audio, list)
    for device in audio:
        assert isinstance(device, AudioDevice)
        assert device.kind in {"input", "output"}
        assert isinstance(device.name, str) and device.name
