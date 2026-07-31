"""Left-rail navigation widget.

The Sidebar shows the application name and version at the top, followed by
a list of navigation items. Each item is a checkable QPushButton with the
``SidebarItem`` object name so the QSS can paint the selected state with an
accent-coloured left edge. Selection is mutually exclusive across items.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.ui.theme import SPACING_MD, SPACING_XS

logger = logging.getLogger(__name__)


# Fixed width keeps the rail stable when the window is resized; pages can
# rely on this number when calculating their own layout proportions.
SIDEBAR_WIDTH: int = 220


@dataclass(frozen=True, slots=True)
class SidebarItem:
    """One row in the sidebar's navigation list.

    The icon is optional so a page with no relevant glyph still slots in
    cleanly. Identifiers are positional (the index of the item in the list
    passed to :class:`Sidebar`), which keeps the public API simple at the
    cost of a small chance of mis-wiring during refactors. If pages start
    being reordered frequently we should add a string id field here.
    """

    label: str
    icon: QIcon | None = None
    tooltip: str | None = None


class Sidebar(QWidget):
    """Vertical navigation strip.

    The widget emits :attr:`current_changed` whenever a user clicks a
    different item; programmatic calls to :meth:`set_current_index` also
    fire the signal because most consumers want a single code path for
    "selection moved" regardless of trigger.
    """

    current_changed = Signal(int)

    def __init__(
        self,
        items: list[SidebarItem],
        app_name: str = "S-Clip",
        version: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(SIDEBAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # Resolve the version lazily so the import graph stays acyclic: the
        # sidebar lives in sclip.ui, which is below sclip.version in the
        # dependency tree, but Python imports are cheap and this keeps the
        # default behaviour ("show whatever the installed package reports")
        # in the widget rather than at the call site.
        if version is None:
            try:
                from sclip.version import __version__

                version = __version__
            except ImportError:
                logger.debug("sclip.version unavailable; sidebar will hide version label.")
                version = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # -- Header -----------------------------------------------------
        # The header is optional. When the frameless title bar already shows
        # the brand, the caller passes an empty ``app_name`` and the sidebar
        # opens straight onto its navigation, which reads as cleaner and more
        # modern than repeating the name twice.
        self._title_label: QLabel | None = None
        self._version_label: QLabel | None = None
        if app_name:
            self._title_label = QLabel(app_name, self)
            self._title_label.setObjectName("SidebarTitle")
            layout.addWidget(self._title_label)

            if version:
                self._version_label = QLabel(f"v{version}", self)
                self._version_label.setObjectName("SidebarVersion")
                layout.addWidget(self._version_label)
            else:
                # Replace the bottom padding the version label would have
                # provided so the first nav item does not crowd the title.
                layout.addSpacing(SPACING_XS)
        else:
            # No header - give the first nav item a little breathing room
            # below the title bar.
            layout.addSpacing(SPACING_MD)

        # -- Nav items --------------------------------------------------
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        # idClicked fires only on a user-initiated change. We also wire the
        # buttons individually below to catch programmatic toggles routed
        # through setChecked(true).
        self._button_group.idClicked.connect(self._on_button_clicked)

        self._buttons: list[QPushButton] = []
        for index, item in enumerate(items):
            button = self._make_item_button(item)
            self._button_group.addButton(button, index)
            layout.addWidget(button)
            self._buttons.append(button)

        # Push remaining space to the bottom so the nav list sits at the
        # top of the rail; trailing items added later still flow naturally.
        layout.addStretch(1)

        # Default selection: the first item, if any.
        self._current_index: int = -1
        if self._buttons:
            self._buttons[0].setChecked(True)
            self._current_index = 0
            # Don't emit current_changed during construction -- the consumer
            # is responsible for showing the default page itself. Emitting
            # here would force callers to defer their signal connection.

    # -- Public API ---------------------------------------------------------

    def current_index(self) -> int:
        """Index of the currently selected item, or ``-1`` if none."""
        return self._current_index

    def set_current_index(self, index: int) -> None:
        """Programmatically select a nav item by index.

        Out-of-range indices are logged and ignored rather than raising,
        because the caller is usually a page that may have computed an
        index from settings the user could have edited by hand.
        """
        if index < 0 or index >= len(self._buttons):
            logger.warning("Sidebar.set_current_index: %d out of range.", index)
            return
        if index == self._current_index:
            return
        self._buttons[index].setChecked(True)
        # Manually drive the signal because programmatic setChecked does not
        # trigger QButtonGroup.idClicked.
        self._current_index = index
        self.current_changed.emit(index)

    def add_item(self, item: SidebarItem) -> int:
        """Append a new navigation row and return its index."""
        index = len(self._buttons)
        button = self._make_item_button(item)
        self._button_group.addButton(button, index)
        # Insert before the trailing stretch so the new item lines up under
        # the last existing entry rather than hugging the bottom edge.
        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.insertWidget(layout.count() - 1, button)
        self._buttons.append(button)
        return index

    # -- Internal helpers ---------------------------------------------------

    def _make_item_button(self, item: SidebarItem) -> QPushButton:
        button = QPushButton(item.label, self)
        button.setObjectName("SidebarItem")
        button.setCheckable(True)
        # autoExclusive is redundant inside a QButtonGroup but harmless; it
        # provides a fallback if the group is dissolved by a future refactor.
        button.setAutoExclusive(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        if item.icon is not None:
            button.setIcon(item.icon)
            button.setIconSize(QSize(18, 18))
        if item.tooltip:
            button.setToolTip(item.tooltip)
        return button

    def _on_button_clicked(self, index: int) -> None:
        if index == self._current_index:
            return
        self._current_index = index
        self.current_changed.emit(index)


__all__ = ["SIDEBAR_WIDTH", "Sidebar", "SidebarItem"]
