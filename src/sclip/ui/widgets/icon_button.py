"""Flat push button with an optional leading SVG icon.

The visual variants -- default, primary, danger, ghost -- are selected with
the dynamic property ``role`` rather than with subclassing. The QSS reads the
property via ``QPushButton[role="primary"]`` selectors, which means a single
component class can produce every CTA shape the app needs and a designer can
re-style any variant by editing tokens or QSS, never Python.
"""

from __future__ import annotations

import logging
from typing import Literal

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton, QSizePolicy, QStyle, QWidget

logger = logging.getLogger(__name__)


# Closed set of acceptable role names. Adding a new role requires both a QSS
# rule and an entry here so the type checker can flag accidental typos.
ButtonRole = Literal["default", "primary", "danger", "ghost"]
_ALLOWED_ROLES: frozenset[ButtonRole] = frozenset(("default", "primary", "danger", "ghost"))

# Default icon edge length. Tuned to sit comfortably beside body-size text
# without dwarfing it; callers can override per-button via :meth:`set_icon_size`.
_DEFAULT_ICON_SIZE: int = 16


class IconButton(QPushButton):
    """A QPushButton with a role-driven visual variant and an optional icon."""

    def __init__(
        self,
        text: str = "",
        icon: QIcon | None = None,
        role: ButtonRole = "default",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        if icon is not None:
            self.setIcon(icon)
        self.setIconSize(QSize(_DEFAULT_ICON_SIZE, _DEFAULT_ICON_SIZE))
        # The pointing-hand cursor lets users see at a glance that the
        # element is interactive -- particularly helpful for ghost buttons
        # which have no border to hint at clickability.
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Setting an objectName is optional but useful when something goes
        # wrong: Qt's debug output names the widget instead of "QPushButton".
        self.setObjectName("IconButton")

        # Initial role drives the QSS variant; assign via the public setter
        # so the validation/repaint path runs the same way for the first
        # state as for a runtime update.
        self.set_role(role)

    # -- Public API ---------------------------------------------------------

    def role(self) -> ButtonRole:
        """Return the current variant role."""
        prop = self.property("role")
        if isinstance(prop, str) and prop in _ALLOWED_ROLES:
            return prop
        return "default"

    def set_role(self, role: ButtonRole) -> None:
        """Switch the button to a different visual variant.

        Reapplies the QSS at the end so the new background/border colour
        takes effect immediately, even on Qt styles that cache the previous
        property value.
        """
        if role not in _ALLOWED_ROLES:
            logger.warning("IconButton.set_role: unknown role %r, defaulting to 'default'.", role)
            role = "default"

        self.setProperty("role", role)
        self._repolish()

    def set_icon_size(self, size: int) -> None:
        """Override the icon edge length in logical pixels."""
        self.setIconSize(QSize(size, size))

    # -- Internal helpers ---------------------------------------------------

    def _repolish(self) -> None:
        # Dynamic property changes require a forced unpolish/polish so Qt
        # re-evaluates the QSS selectors. Without this the role swap appears
        # to take effect only after the next hover or focus event.
        style: QStyle | None = self.style()
        if style is None:
            # Highly unlikely outside of tests with a stripped QApplication,
            # but better to no-op than to crash.
            return
        style.unpolish(self)
        style.polish(self)
        self.update()


__all__ = ["ButtonRole", "IconButton"]
