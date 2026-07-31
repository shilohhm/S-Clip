"""Human-readable renderings of the numbers the interface shows.

Shared by the capture and library pages so a byte count reads identically
wherever it appears. Pure functions with no Qt dependency, which also makes
them cheap to test.
"""

from __future__ import annotations


def format_bytes(num_bytes: int) -> str:
    """Render a byte count in a friendly unit, e.g. ``"1.2 MB"``.

    Only divides while there is still a larger unit to fall through to, so a
    file at or above a terabyte is reported in the right order of magnitude
    rather than wrapping around to a small number of gigabytes.

    Binary units (1024) are correct here: this describes files on disk, which
    is how every file manager on Windows reports them.
    """
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_bitrate(bits_per_second: float) -> str:
    """Render a bitrate the way a capture tool quotes one, e.g. ``"18.3 Mb/s"``.

    Decimal units (1000) rather than binary, matching how encoders, streaming
    services and FFmpeg itself express bitrate. Returns an em dash for a
    non-positive rate so an empty buffer shows a placeholder instead of
    ``0.0 b/s``.
    """
    if bits_per_second <= 0:
        return " - "
    value = float(bits_per_second)
    for unit in ("b/s", "kb/s", "Mb/s"):
        if value < 1000.0:
            return f"{value:.0f} {unit}" if unit == "b/s" else f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{value:.1f} Gb/s"


__all__ = ["format_bitrate", "format_bytes"]
