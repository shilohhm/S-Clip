"""iOS-style segmented control.

A horizontal strip of mutually exclusive buttons used for short, closed
option lists (typically two to four entries). The selected option is
highlighted with the accent colour; the rest sit flat against the control's
surface. Pages should reach for this widget instead of a QComboBox when the
list is short enough that hiding options behind a drop-down would feel
heavy-handed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QWidget,
)

logger = logging.getLogger(__name__)


class SegmentedControl(QWidget):
    """Mutually exclusive horizontal toggle.

    Selection is positional: callers refer to options by their index in the
    list passed at construction. The widget emits :attr:`current_changed`
    whenever the index actually changes -- redundant programmatic selects
    of the already-current index are silently dropped to keep listeners
    free of debouncing logic.
    """

    current_changed = Signal(int)

    def __init__(
        self,
        options: Sequence[str],
        current_index: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SegmentedControl")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        if not options:
            # A control with zero options is structurally invalid: there is
            # nothing for the user to pick and no sensible default index.
            # Loudly refuse rather than show an empty bar.
            raise ValueError("SegmentedControl requires at least one option.")

        layout = QHBoxLayout(self)
        # 2px inset matches the QSS padding so the highlighted option's
        # rounded background fits inside the container's outline neatly.
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self._on_clicked)

        self._buttons: list[QPushButton] = []
        for index, label in enumerate(options):
            button = QPushButton(label, self)
            button.setObjectName("SegmentedControlOption")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            # Equal stretch lets the options share the available width
            # evenly, which is the look the iOS control originated.
            layout.addWidget(button, 1)
            self._group.addButton(button, index)
            self._buttons.append(button)

        # Validate the initial index after the buttons exist so we can fall
        # back to zero without crashing on bad input.
        if current_index < 0 or current_index >= len(self._buttons):
            logger.warning(
                "SegmentedControl initial index %d out of range; falling back to 0.",
                current_index,
            )
            current_index = 0
        self._current_index: int = current_index
        self._buttons[current_index].setChecked(True)

    # -- Public API ---------------------------------------------------------

    @property
    def current_index(self) -> int:
        """The index of the currently selected option."""
        return self._current_index

    def set_current_index(self, index: int) -> None:
        """Programmatically change the selected option.

        Emits :attr:`current_changed` if (and only if) the index actually
        moves; this keeps signal handlers idempotent without each having to
        guard against spurious emissions.
        """
        if index < 0 or index >= len(self._buttons):
            logger.warning("SegmentedControl.set_current_index: %d out of range.", index)
            return
        if index == self._current_index:
            return
        self._buttons[index].setChecked(True)
        self._current_index = index
        self.current_changed.emit(index)

    def options(self) -> tuple[str, ...]:
        """Snapshot of the option labels in display order."""
        return tuple(button.text() for button in self._buttons)

    # -- Internal helpers ---------------------------------------------------

    def _on_clicked(self, index: int) -> None:
        if index == self._current_index:
            return
        self._current_index = index
        self.current_changed.emit(index)


__all__ = ["SegmentedControl"]
