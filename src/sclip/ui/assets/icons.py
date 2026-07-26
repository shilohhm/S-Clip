"""Inline SVG icon library.

The icons are kept as raw SVG strings rather than separate files for two
reasons: they ship inside the wheel without needing a force-include rule, and
they can be recoloured at draw time without inventing a templating layer for
the colour. :func:`icon` substitutes the desired stroke/fill colour into the
template and renders the result to a :class:`PySide6.QtGui.QIcon` via
``QSvgRenderer``.

All icons use a 24-unit viewBox so the supplied ``size`` is interpreted in
device-independent pixels -- ``QSvgRenderer`` scales the viewBox to the target
pixmap dimensions, and the pixmap itself is allocated at the screen's device
pixel ratio so HiDPI displays stay crisp.
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

logger = logging.getLogger(__name__)


# -- SVG templates ---------------------------------------------------------
# Every template uses ``{colour}`` as the single substitution point so we can
# share one rendering routine. Strokes are 2 units wide on a 24-unit grid to
# read clearly at 18px and scale up gracefully to 32px without going chunky.
# Where the icon needs to be filled (record dot, stop square) we substitute
# into the ``fill`` attribute; otherwise the stroke holds the colour.

_RECORD: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<circle cx="12" cy="12" r="6" fill="{colour}"/>'
    "</svg>"
)

_BRAND: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round">'
    '<circle cx="12" cy="12" r="8" opacity=".28"/>'
    '<path d="M5.65 16.86A8 8 0 1 0 7.9 5.3"/>'
    '<circle cx="12" cy="12" r="2.4" fill="{colour}" stroke="none"/>'
    "</svg>"
)

_STOP: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<rect x="6" y="6" width="12" height="12" rx="2" fill="{colour}"/>'
    "</svg>"
)

_CLIP: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<circle cx="6" cy="6" r="3"/>'
    '<circle cx="6" cy="18" r="3"/>'
    '<line x1="20" y1="4" x2="8.12" y2="15.88"/>'
    '<line x1="14.47" y1="14.48" x2="20" y2="20"/>'
    '<line x1="8.12" y1="8.12" x2="12" y2="12"/>'
    "</svg>"
)

_SETTINGS: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83'
    "l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0"
    "v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83"
    "l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09"
    "a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83"
    "l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09"
    "a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83"
    "l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4"
    'h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
    "</svg>"
)

_LIBRARY: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
    '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
    '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'
    "</svg>"
)

_ABOUT: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="9"/>'
    '<line x1="12" y1="11" x2="12" y2="17"/>'
    '<circle cx="12" cy="7.5" r="0.6" fill="{colour}"/>'
    "</svg>"
)

_MONITOR: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<rect x="2" y="3" width="20" height="14" rx="2"/>'
    '<line x1="8" y1="21" x2="16" y2="21"/>'
    '<line x1="12" y1="17" x2="12" y2="21"/>'
    "</svg>"
)

_MICROPHONE: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<rect x="9" y="2" width="6" height="12" rx="3"/>'
    '<path d="M19 10v1a7 7 0 0 1-14 0v-1"/>'
    '<line x1="12" y1="18" x2="12" y2="22"/>'
    '<line x1="8" y1="22" x2="16" y2="22"/>'
    "</svg>"
)

_HEADPHONES: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="{colour}" stroke-width="1.8" stroke-linecap="round"'
    ' stroke-linejoin="round">'
    '<path d="M3 14v-2a9 9 0 1 1 18 0v2"/>'
    '<path d="M21 14v4a2 2 0 0 1-2 2h-1v-7h1a2 2 0 0 1 2 1z"/>'
    '<path d="M3 14v4a2 2 0 0 0 2 2h1v-7H5a2 2 0 0 0-2 1z"/>'
    "</svg>"
)

_ICONS: Final[dict[str, str]] = {
    "brand": _BRAND,
    "record": _RECORD,
    "stop": _STOP,
    "clip": _CLIP,
    "settings": _SETTINGS,
    "library": _LIBRARY,
    "about": _ABOUT,
    "monitor": _MONITOR,
    "microphone": _MICROPHONE,
    "headphones": _HEADPHONES,
}


def available_icons() -> tuple[str, ...]:
    """List the icon names this module knows about.

    Exposed mainly so tests can iterate over the catalogue without reaching
    into the private ``_ICONS`` dict.
    """
    return tuple(_ICONS)


def _device_pixel_ratio() -> float:
    """Best-effort lookup of the current screen's DPR.

    On a HiDPI display Qt allocates pixmaps at the native resolution so the
    SVG renders crisply; on a standard 96 DPI display this returns ``1.0``.
    We fall back to ``1.0`` if QGuiApplication has not been initialised yet
    (for example, when this module is imported during a test that pre-builds
    icons before the app exists).
    """
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return 1.0
    screen = app.primaryScreen()
    if screen is None:
        return 1.0
    return float(screen.devicePixelRatio())


def icon(name: str, colour: str = "#E6E8EC", size: int = 18) -> QIcon:
    """Render the named icon at the given colour and logical size.

    Parameters
    ----------
    name:
        Key into the icon catalogue. Use :func:`available_icons` to discover
        valid names.
    colour:
        CSS-style colour string substituted into the SVG's ``stroke`` or
        ``fill`` attribute. Hex, named and ``rgb(...)`` notations all work
        because the value is passed through verbatim.
    size:
        Logical pixel size of the resulting square icon. The pixmap is
        allocated at ``size * device_pixel_ratio`` so it stays sharp on
        HiDPI displays.

    Returns
    -------
    QIcon
        Always returns a valid (possibly empty) icon -- callers should never
        receive ``None`` and have to null-check their UI code.
    """
    template = _ICONS.get(name)
    if template is None:
        logger.warning("Unknown icon name '%s'; returning an empty QIcon.", name)
        return QIcon()

    svg = template.format(colour=colour)

    renderer = QSvgRenderer(svg.encode("utf-8"))
    if not renderer.isValid():
        # Bad SVG would mean a typo in the templates above -- not something
        # the user can fix at runtime. Log it loudly and fall back to empty.
        logger.error("QSvgRenderer rejected SVG for icon '%s'.", name)
        return QIcon()

    dpr = _device_pixel_ratio()
    pixel_size = max(1, round(size * dpr))

    # QImage with the explicit format keeps the alpha channel and avoids the
    # platform-specific surface format guessing that QPixmap would otherwise
    # do. We paint onto the image, then wrap it in a QPixmap for QIcon.
    image = QImage(pixel_size, pixel_size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter)
    finally:
        # If render() raises (it shouldn't, but Qt has been known to), the
        # painter must still be ended or the QImage stays locked.
        painter.end()

    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)

    qicon = QIcon(pixmap)
    # Register an explicit logical size so QIcon doesn't down-scale or
    # interpolate when Qt asks for an alternate resolution.
    qicon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.Off)
    qicon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.On)
    return qicon


def icon_size(size: int) -> QSize:
    """Sugar for ``QSize(size, size)`` -- icons are always square here."""
    return QSize(size, size)


__all__ = ["available_icons", "icon", "icon_size"]
