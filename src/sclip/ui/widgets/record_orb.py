"""The record orb — the capture screen's centrepiece control.

One large, custom-painted button that is always "the obvious next action":
start a recording, arm the replay buffer, or save a clip. It carries the
engine's state visually — a calm violet rim at rest, a breathing red pulse
while recording, a cyan arc sweeping round while the replay buffer rolls, a
fast spinner while a clip is being written.

Everything here is QPainter work because the orb needs a soft radial glow and
animated arcs, neither of which a stylesheet can express. The page above it
stays in charge of *meaning* — it decides which glyph and animation suit the
current state and tells the orb through :meth:`set_visual`; the orb only
decides how to paint them.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from sclip.ui.theme import THEME

logger = logging.getLogger(__name__)


# Glyph drawn in the centre of the orb.
GLYPH_CIRCLE = "circle"  # a filled disc — "go": start, arm, save
GLYPH_SQUARE = "square"  # a rounded square — "stop"
GLYPH_SPINNER = "spinner"  # no centre glyph; the ring spins instead

# Ring animation styles.
ANIM_NONE = "none"  # a steady ring — armed but idle
ANIM_PULSE = "pulse"  # the ring and glow breathe — recording
ANIM_SWEEP = "sweep"  # a bright arc circles slowly — the buffer is rolling
ANIM_SPIN = "spin"  # a short arc circles quickly — saving a clip

# The orb claims a generous square so the glow has room to fall off softly.
ORB_EXTENT_DEFAULT: int = 248

# A tighter square for narrow windows, where the readout stacks above the
# instrument and vertical space becomes the scarce resource rather than
# horizontal.
ORB_EXTENT_COMPACT: int = 168

# Below this the ring weight and centre glyph stop reading as a control.
_ORB_MIN_EXTENT: int = 96


class RecordOrb(QWidget):
    """A large, animated, click-to-act capture control."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RecordOrb")
        self.setFixedSize(ORB_EXTENT_DEFAULT, ORB_EXTENT_DEFAULT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._accent = QColor(THEME.accent_primary)
        self._glyph = GLYPH_CIRCLE
        self._animation = ANIM_NONE
        self._interactive = True
        self._hovered = False
        self._pressed = False

        # Animation backing fields. ``_pulse`` breathes 0..1; ``_phase`` is the
        # rotation, in degrees, of the sweeping and spinning arcs.
        self._pulse_value = 0.0
        self._phase_value = 0.0

        self._pulse_anim = QPropertyAnimation(self, b"pulse", self)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setKeyValueAt(0.5, 1.0)
        self._pulse_anim.setEndValue(0.0)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_anim.setLoopCount(-1)

        self._phase_anim = QPropertyAnimation(self, b"phase", self)
        self._phase_anim.setStartValue(0.0)
        self._phase_anim.setEndValue(360.0)
        self._phase_anim.setLoopCount(-1)

    # -- public API ---------------------------------------------------------

    def set_extent(self, extent: int) -> None:
        """Resize the orb's square and repaint.

        Every dimension in :meth:`paintEvent` is derived from the widget's own
        size, so the control scales without any further work. The capture page
        shrinks it when the window is too narrow for the side-by-side layout,
        which is what keeps the save action above the fold at the documented
        minimum window size.
        """
        side = max(_ORB_MIN_EXTENT, int(extent))
        if side == self.width() and side == self.height():
            return
        self.setFixedSize(side, side)
        self.update()

    def set_visual(self, *, accent: str, glyph: str, animation: str, interactive: bool) -> None:
        """Set everything about the orb's appearance in one atomic call.

        Taking the whole appearance at once means the orb can never be caught
        mid-update showing, say, a recording colour with an idle glyph.
        """
        self._accent = QColor(accent)
        self._glyph = glyph
        self._interactive = interactive
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if interactive else Qt.CursorShape.ArrowCursor
        )
        if animation != self._animation:
            self._animation = animation
            self._restart_animations()
        self.update()

    # -- animation plumbing -------------------------------------------------

    def _restart_animations(self) -> None:
        """Start or stop the loops to match the current animation style."""
        self._pulse_anim.stop()
        self._phase_anim.stop()
        self._pulse_value = 0.0
        self._phase_value = 0.0

        if self._animation == ANIM_PULSE:
            self._pulse_anim.setDuration(1500)
            self._pulse_anim.start()
        elif self._animation == ANIM_SWEEP:
            self._phase_anim.setDuration(2600)
            self._phase_anim.start()
        elif self._animation == ANIM_SPIN:
            self._phase_anim.setDuration(900)
            self._phase_anim.start()
        self.update()

    def _get_pulse(self) -> float:
        return self._pulse_value

    def _set_pulse(self, value: float) -> None:
        self._pulse_value = float(value)
        self.update()

    def _get_phase(self) -> float:
        return self._phase_value

    def _set_phase(self, value: float) -> None:
        self._phase_value = float(value)
        self.update()

    pulse = Property(float, fget=_get_pulse, fset=_set_pulse)
    phase = Property(float, fget=_get_phase, fset=_set_phase)

    # -- interaction --------------------------------------------------------

    def _within_orb(self, point: QPointF) -> bool:
        """True when ``point`` falls inside the clickable inner disc."""
        centre = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) * 0.30
        delta = point - centre
        return (delta.x() ** 2 + delta.y() ** 2) <= radius**2

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hovered = self._within_orb(event.position())
        if hovered != self._hovered:
            self._hovered = hovered
            self.update()

    def leaveEvent(self, event: object) -> None:
        if self._hovered:
            self._hovered = False
            self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            self._interactive
            and event.button() == Qt.MouseButton.LeftButton
            and self._within_orb(event.position())
        ):
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_pressed = self._pressed
        self._pressed = False
        if (
            was_pressed
            and self._interactive
            and event.button() == Qt.MouseButton.LeftButton
            and self._within_orb(event.position())
        ):
            self.update()
            self.clicked.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Arm keyboard activation without firing repeatedly on key repeat."""
        if (
            self._interactive
            and not event.isAutoRepeat()
            and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            self._pressed = True
            self.update()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        """Activate on key release, mirroring the mouse interaction."""
        if (
            self._pressed
            and self._interactive
            and not event.isAutoRepeat()
            and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            self._pressed = False
            self.update()
            self.clicked.emit()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusInEvent(self, event: object) -> None:
        self.update()
        super().focusInEvent(event)  # type: ignore[arg-type]

    def focusOutEvent(self, event: object) -> None:
        self._pressed = False
        self.update()
        super().focusOutEvent(event)  # type: ignore[arg-type]

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            side = min(self.width(), self.height())
            centre = QPointF(self.width() / 2, self.height() / 2)

            self._paint_glow(painter, centre, side)
            self._paint_ring(painter, centre, side)
            self._paint_focus_ring(painter, centre, side)
            self._paint_orb_body(painter, centre, side)
            self._paint_glyph(painter, centre, side)
        finally:
            painter.end()

    def _paint_glow(self, painter: QPainter, centre: QPointF, side: float) -> None:
        """Lay down the soft radial glow behind the orb."""
        radius = side * 0.49
        # The glow is strongest while recording (it breathes) and dimmer at
        # rest; hovering lifts it a little either way.
        if self._animation == ANIM_PULSE:
            strength = 0.18 + 0.26 * self._pulse_value
        elif self._animation in (ANIM_SWEEP, ANIM_SPIN):
            strength = 0.20
        else:
            strength = 0.12
        if self._hovered and self._interactive:
            strength += 0.06
        if not self._interactive:
            strength *= 0.4

        gradient = QRadialGradient(centre, radius)
        inner = QColor(self._accent)
        inner.setAlphaF(min(1.0, strength))
        edge = QColor(self._accent)
        edge.setAlphaF(0.0)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(1.0, edge)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(centre, radius, radius)

    def _paint_ring(self, painter: QPainter, centre: QPointF, side: float) -> None:
        """Paint the ring — steady, breathing, sweeping or spinning."""
        radius = side * 0.38
        width = max(3.0, side * 0.022)
        ring_rect = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)

        # The dim base ring is always present so the orb keeps its shape even
        # when no arc is lit.
        base = QColor(THEME.border_strong)
        if not self._interactive:
            base = QColor(THEME.border)
        painter.setPen(QPen(base, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius, radius)

        if self._animation == ANIM_PULSE:
            lit = QColor(self._accent)
            lit.setAlphaF(0.45 + 0.55 * self._pulse_value)
            painter.setPen(QPen(lit, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawEllipse(centre, radius, radius)
        elif self._animation == ANIM_SWEEP:
            self._paint_arc(painter, ring_rect, width, span_deg=120.0)
        elif self._animation == ANIM_SPIN:
            self._paint_arc(painter, ring_rect, width, span_deg=80.0)
        elif self._interactive:
            # Idle but armed — a steady, half-lit accent ring.
            lit = QColor(self._accent)
            lit.setAlphaF(0.55)
            painter.setPen(QPen(lit, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawEllipse(centre, radius, radius)

    def _paint_focus_ring(self, painter: QPainter, centre: QPointF, side: float) -> None:
        """Draw a clear keyboard focus indicator outside the animated ring."""
        if not self.hasFocus() or not self._interactive:
            return
        colour = QColor(self._accent)
        colour.setAlphaF(0.85)
        radius = side * 0.435
        painter.setPen(
            QPen(
                colour,
                2.0,
                Qt.PenStyle.DashLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius, radius)

    def _paint_arc(self, painter: QPainter, rect: QRectF, width: float, *, span_deg: float) -> None:
        """Draw the moving accent arc used by the sweep and spin animations."""
        lit = QColor(self._accent)
        pen = QPen(lit, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # Qt angles are in sixteenths of a degree, measured anticlockwise from
        # three o'clock; negating the phase makes the arc travel clockwise.
        start_angle = int((-self._phase_value) * 16)
        painter.drawArc(rect, start_angle, int(-span_deg * 16))

    def _paint_orb_body(self, painter: QPainter, centre: QPointF, side: float) -> None:
        """Paint the raised inner disc the glyph sits on."""
        radius = side * 0.30
        top = QColor(THEME.surface_elevated)
        bottom = QColor(THEME.surface)
        if self._hovered and self._interactive:
            top = top.lighter(118)
            bottom = bottom.lighter(112)
        if self._pressed:
            top, bottom = bottom, top

        gradient = QLinearGradient(centre.x(), centre.y() - radius, centre.x(), centre.y() + radius)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.setBrush(gradient)

        rim = QColor(self._accent)
        rim.setAlphaF(0.55 if self._interactive else 0.25)
        painter.setPen(QPen(rim, 1.5))
        painter.drawEllipse(centre, radius, radius)

    def _paint_glyph(self, painter: QPainter, centre: QPointF, side: float) -> None:
        """Paint the centre glyph: a disc, a square, or nothing for the spinner."""
        glyph_colour = QColor(self._accent)
        if not self._interactive:
            glyph_colour.setAlphaF(0.4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glyph_colour)

        if self._glyph == GLYPH_CIRCLE:
            radius = side * 0.085
            painter.drawEllipse(centre, radius, radius)
        elif self._glyph == GLYPH_SQUARE:
            half = side * 0.072
            square = QRectF(centre.x() - half, centre.y() - half, half * 2, half * 2)
            painter.drawRoundedRect(square, half * 0.32, half * 0.32)
        # GLYPH_SPINNER intentionally draws no centre mark — the spinning ring
        # already reads as "working".


__all__ = [
    "ANIM_NONE",
    "ANIM_PULSE",
    "ANIM_SPIN",
    "ANIM_SWEEP",
    "GLYPH_CIRCLE",
    "GLYPH_SPINNER",
    "GLYPH_SQUARE",
    "RecordOrb",
]
