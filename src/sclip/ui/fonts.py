"""Deterministic application-font loading.

S-Clip ships its interface fonts with the wheel rather than assuming a
particular Windows font installation. That keeps the UI consistent across
developer machines, packaged builds, and headless screenshot runs.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

from PySide6.QtGui import QFontDatabase

logger = logging.getLogger(__name__)

_FONT_FILES: tuple[str, ...] = (
    "Figtree-Variable.ttf",
    "Unbounded-Variable.ttf",
)


@cache
def install_application_fonts() -> tuple[str, ...]:
    """Load the bundled typefaces and return the registered family names.

    A missing font is a packaging error, but not a reason to prevent capture
    from starting. Qt will use the fallbacks declared in the stylesheet while
    the log retains enough detail to diagnose the broken package.
    """

    font_dir = Path(__file__).resolve().parent / "assets" / "fonts"
    families: list[str] = []

    for filename in _FONT_FILES:
        path = font_dir / filename
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            logger.error("Could not load bundled font: %s", path)
            continue
        registered = QFontDatabase.applicationFontFamilies(font_id)
        if not registered:
            logger.error("Bundled font registered without a family name: %s", path)
            continue
        families.extend(registered)

    return tuple(dict.fromkeys(families))


__all__ = ["install_application_fonts"]
