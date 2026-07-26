"""Animated capture-state indicator.

The StatusPill is the only widget in the application that draws its own dot
with QPainter -- everything else is QSS. A QPainter is necessary because the
dot needs to share its opacity with a QPropertyAnimation, and QSS has no
animation primitives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from sclip.contracts import CaptureState
from sclip.ui.theme import SPACING_SM, SPACING_XS, THEME

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StatusVisual:
    """Per-state visual configuration.

    Kept as a frozen dataclass rather than scattered ``if/elif`` arms so
    extending the indicator with a new state is a one-line change.
    """

    label: str
    dot_colour: str
    pulse_duration_ms: int  # 0 disables the pulse


# Map from contract state to visuals. We pull dot colours from the live
# THEME instance so a palette swap propagates automatically.
def _state_visuals() -> dict[CaptureState, _StatusVisual]:
    return {
        CaptureState.IDLE: _StatusVisual(
            label="IDLE",
            dot_colour=THEME.text_secondary,
            pulse_duration_ms=0,
        ),
        CaptureState.BUFFERING: _StatusVisual(
            label="BUFFERING",
            dot_colour=THEME.accent_secondary,
            pulse_duration_ms=1400,  # slow, ambient pulse
        ),
        CaptureState.RECORDING: _StatusVisual(
            label="RECORDING",
            dot_colour=THEME.danger,
            pulse_duration_ms=700,  # fast, urgent pulse
        ),
        CaptureState.SAVING: _StatusVisual(
            label="SAVING",
            dot_colour=THEME.accent_primary,
            pulse_duration_ms=0,
        ),
        CaptureState.ERROR: _StatusVisual(
            label="ERROR",
            dot_colour=THEME.warning,
            pulse_duration_ms=0,
        ),
    }


class StatusPill(QWidget):
    """Pill-shaped indicator showing the current capture state.

    The widget owns a small text label and paints a coloured dot on the left
    side of the pill. When the state demands a pulse, a QPropertyAnimation
    drives the dot's opacity in a loop between 0.35 and 1.0; otherwise the
    opacity stays pinned at 1.0 so the dot reads as a solid colour.
    """

    # Lower bound of the pulse animation. The dot never fades to fully
    # transparent because that reads as "off" rather than "alive".
    _PULSE_MIN_OPACITY: float = 0.35
    _PULSE_MAX_OPACITY: float = 1.0

    def __init__(
        self, state: CaptureState = CaptureState.IDLE, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatusPill")
        # Honour the QSS background/border; without this the QPainter fill
        # would paint over a transparent rectangle and the pill outline would
        # disappear.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        # -- Layout -----------------------------------------------------
        # Leave room on the left for the painted dot. The dot is drawn in
        # paintEvent and sits in the gap created by setContentsMargins.
        self._dot_diameter = 10
        self._dot_padding = SPACING_SM
        left_margin = self._dot_padding * 2 + self._dot_diameter
        layout = QHBoxLayout(self)
        layout.setContentsMargins(left_margin, SPACING_XS, SPACING_SM, SPACING_XS)
        layout.setSpacing(0)

        self._label = QLabel("", self)
        self._label.setObjectName("StatusPillLabel")
        # Object-name lets the QSS reach the label without us touching its
        # font properties directly from Python.
        layout.addWidget(self._label)

        # -- Animation backing field -----------------------------------
        # _opacity is a true Python float -- the Qt property machinery reads
        # it through the descriptor below. We initialise to fully opaque so
        # non-pulsing states render immediately.
        self._dot_opacity: float = self._PULSE_MAX_OPACITY
        self._dot_qcolor: QColor = QColor(THEME.text_secondary)

        self._animation = QPropertyAnimation(self, b"_opacity", self)
        self._animation.setLoopCount(-1)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)

        self._state = state
        self._apply_state(state)

    # -- Public API ---------------------------------------------------------

    def state(self) -> CaptureState:
        """Return the current capture state."""
        return self._state

    def set_state(self, state: CaptureState) -> None:
        """Update the visuals to reflect a new capture state.

        Safe to call from any thread because Qt marshals widget updates
        through the event loop; callers in capture-engine threads should
        still prefer to emit a signal and let the slot run here, but a
        direct call will not corrupt the widget.
        """
        if state == self._state:
            return
        self._state = state
        self._apply_state(state)

    # -- Internal helpers ---------------------------------------------------

    def _apply_state(self, state: CaptureState) -> None:
        visuals = _state_visuals().get(state)
        if visuals is None:
            # Defensive: the enum is closed today, but if a future change adds
            # a new member without updating the visuals map we want a loud,
            # debuggable failure mode rather than silent breakage.
            logger.warning("StatusPill received unknown state %r; falling back to IDLE.", state)
            visuals = _state_visuals()[CaptureState.IDLE]

        self._label.setText(visuals.label)
        self._dot_qcolor = QColor(visuals.dot_colour)

        if visuals.pulse_duration_ms > 0:
            self._start_pulse(visuals.pulse_duration_ms)
        else:
            self._stop_pulse()
        self.update()

    def _start_pulse(self, duration_ms: int) -> None:
        # Restart the animation from a known state so a transition from one
        # pulsing state to another doesn't inherit the previous opacity at
        # an awkward moment.
        self._animation.stop()
        self._animation.setStartValue(self._PULSE_MAX_OPACITY)
        self._animation.setKeyValueAt(0.5, self._PULSE_MIN_OPACITY)
        self._animation.setEndValue(self._PULSE_MAX_OPACITY)
        self._animation.setDuration(duration_ms)
        self._animation.start()

    def _stop_pulse(self) -> None:
        self._animation.stop()
        # Lock the dot at full opacity so static states (IDLE, SAVING, ERROR)
        # render as a solid coin.
        self._dot_opacity = self._PULSE_MAX_OPACITY

    # -- Qt property for the animation --------------------------------------

    def _get_opacity(self) -> float:
        return self._dot_opacity

    def _set_opacity(self, value: float) -> None:
        self._dot_opacity = float(value)
        # An update() schedules a repaint without forcing it synchronously;
        # Qt batches multiple updates into one paint, which keeps the pulse
        # smooth even when the rest of the UI is busy.
        self.update()

    _opacity = Property(float, fget=_get_opacity, fset=_set_opacity)

    # -- Painting -----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        # The QSS draws the background and border because of WA_StyledBackground;
        # we just paint the coloured dot on top. Calling super().paintEvent
        # first ensures the styled background is laid down before the dot.
        super().paintEvent(event)

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            colour = QColor(self._dot_qcolor)
            colour.setAlphaF(self._dot_opacity)
            painter.setBrush(colour)
            painter.setPen(Qt.PenStyle.NoPen)

            # Centre the dot vertically inside the pill's content area.
            cy = self.height() // 2
            x = self._dot_padding
            y = cy - self._dot_diameter // 2
            painter.drawEllipse(x, y, self._dot_diameter, self._dot_diameter)
        finally:
            painter.end()

    def sizeHint(self) -> QSize:
        # Give the layout enough room for a comfortable pill. The actual size
        # is still driven by the label width plus margins; this just biases
        # toward "not tiny" when the parent layout asks.
        hint = super().sizeHint()
        if hint.height() < 28:
            hint.setHeight(28)
        return hint


__all__ = ["StatusPill"]
