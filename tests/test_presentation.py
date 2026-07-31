"""Tests for the shared presentation helpers.

:mod:`sclip.ui.formatting` renders the numbers the capture and library screens
show, and :class:`~sclip.ui.widgets.buffer_meter.BufferMeter` draws the replay
buffer's fill level. Both are small, pure-ish pieces that the telemetry readout
depends on being right.
"""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from sclip.ui.formatting import format_bitrate, format_bytes
from sclip.ui.widgets import BufferMeter

# ------------------------------------------------------------- format_bytes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (31_457_280, "30.0 MB"),
        (1024**3, "1.0 GB"),
    ],
)
def test_format_bytes_picks_a_sensible_unit(value: int, expected: str) -> None:
    assert format_bytes(value) == expected


def test_format_bytes_reaches_terabytes() -> None:
    """The loop must not run out of units and report a huge gigabyte figure.

    An earlier version of this helper divided before its last branch fired, so
    a terabyte-scale value came out an order of magnitude wrong.
    """
    assert format_bytes(1024**4) == "1.0 TB"
    assert format_bytes(5 * 1024**4) == "5.0 TB"


def test_format_bytes_shows_whole_bytes_without_a_decimal() -> None:
    """ "512 B" reads better than "512.0 B" for a count of individual bytes."""
    assert "." not in format_bytes(999)


# ----------------------------------------------------------- format_bitrate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, " - "),
        (-5.0, " - "),
        (500.0, "500 b/s"),
        (1_500.0, "1.5 kb/s"),
        (8_388_608.0, "8.4 Mb/s"),
        (1_500_000_000.0, "1.5 Gb/s"),
    ],
)
def test_format_bitrate_uses_decimal_units(value: float, expected: str) -> None:
    """Bitrates are quoted in powers of ten, unlike file sizes on disk."""
    assert format_bitrate(value) == expected


def test_bitrate_and_size_units_differ_on_purpose() -> None:
    """1000 bytes is under a kilobyte; 1000 bits per second is over a kbit.

    The two helpers deliberately use different bases, and this pins that so a
    future tidy-up does not "helpfully" unify them.
    """
    assert format_bytes(1000) == "1000 B"
    assert format_bitrate(1000.0) == "1.0 kb/s"


# -------------------------------------------------------------- BufferMeter


def test_meter_starts_empty(qtbot: QtBot) -> None:
    meter = BufferMeter()
    qtbot.addWidget(meter)
    assert meter.fraction() == 0.0


@pytest.mark.parametrize(
    ("given", "expected"),
    [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (-2.0, 0.0), (7.5, 1.0)],
)
def test_meter_clamps_its_fill(qtbot: QtBot, given: float, expected: float) -> None:
    """The widget clamps independently of the telemetry that feeds it.

    BufferTelemetry already clamps its own ratio, but the meter must be safe
    for any caller rather than trusting the one it happens to have today.
    """
    meter = BufferMeter()
    qtbot.addWidget(meter)

    meter.set_fraction(given)

    assert meter.fraction() == expected


def test_meter_paints_at_every_fill_level(qtbot: QtBot) -> None:
    """Exercise the paint path, including the near-empty degenerate case.

    A tiny fraction produces a filled rect narrower than its own corner radius,
    which is why the widget floors the drawn width.
    """
    meter = BufferMeter()
    qtbot.addWidget(meter)
    meter.resize(200, 6)
    meter.show()
    qtbot.waitExposed(meter)

    for fraction in (0.0, 0.001, 0.25, 0.6, 1.0):
        meter.set_fraction(fraction)
        meter.repaint()  # must not raise at any level

    assert meter.fraction() == 1.0


def test_meter_keeps_a_fixed_height(qtbot: QtBot) -> None:
    """A hairline bar reads as an instrument, not a download progress bar."""
    meter = BufferMeter()
    qtbot.addWidget(meter)
    assert meter.minimumHeight() == meter.maximumHeight()
