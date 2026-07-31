"""A slim horizontal meter for a 0..1 fill level.

Painted rather than styled because QSS has no way to draw a partial rounded
bar whose width tracks a value. The widget is deliberately dumb: it holds a
fraction and paints it. The capture page decides what the fraction *means*, so
the same component could show disk headroom or encode progress unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from sclip.ui.theme import THEME

# A hairline bar reads as an instrument rather than a download progress bar,
# which is the register the capture screen wants.
_TRACK_HEIGHT: int = 6

# A fully rounded cap: radius is half the height, so the ends are semicircles.
_RADIUS: float = _TRACK_HEIGHT / 2

# Below this fraction the filled rounded rect degenerates into a squashed
# lozenge narrower than its own corner radius, which paints as a smear. We
# floor the drawn width so a barely-started buffer still shows a clean dot.
_MIN_VISIBLE_WIDTH: float = float(_TRACK_HEIGHT)


class BufferMeter(QWidget):
    """Paints a clamped 0..1 fill level as a rounded bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fraction: float = 0.0
        self.setFixedHeight(_TRACK_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def fraction(self) -> float:
        """The current fill level, always within ``0.0..1.0``."""
        return self._fraction

    def set_fraction(self, fraction: float) -> None:
        """Set the fill level, clamping out-of-range input.

        Clamping here as well as in :class:`~sclip.contracts.BufferTelemetry`
        keeps the widget safe for any caller, not just the one that happens to
        feed it today.
        """
        clamped = min(1.0, max(0.0, float(fraction)))
        if clamped == self._fraction:
            return
        self._fraction = clamped
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)

            track = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
            painter.setBrush(QColor(THEME.border))
            painter.drawRoundedRect(track, _RADIUS, _RADIUS)

            if self._fraction <= 0.0:
                return

            filled = QRectF(track)
            filled.setWidth(max(_MIN_VISIBLE_WIDTH, track.width() * self._fraction))
            painter.setBrush(QColor(THEME.accent_primary))
            painter.drawRoundedRect(filled, _RADIUS, _RADIUS)
        finally:
            painter.end()


__all__ = ["BufferMeter"]
