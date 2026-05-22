"""Custom window title bar for the frameless main window.

Going frameless lets S-Clip carry its own chrome — the brand mark, and window
controls that match the rest of the interface rather than the grey system
buttons. The bar handles the two interactions a title bar owes the user:
dragging it moves the window, and double-clicking it toggles maximised state.

The three control glyphs (minimise, maximise/restore, close) are painted by
hand rather than shipped as image assets — they are three or four straight
lines each, and painting them keeps them crisp at any scale and recolourable
on hover without a second set of files.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from sclip.ui.theme import THEME

logger = logging.getLogger(__name__)


TITLE_BAR_HEIGHT: int = 44

# Logical edge length of the painted control glyphs.
_GLYPH_SIZE: int = 10


class _WindowButton(QPushButton):
    """A flat title-bar control button that paints its own glyph.

    ``kind`` is one of ``"minimise"``, ``"maximise"``, ``"restore"`` or
    ``"close"``. The maximise button swaps to the restore glyph while the
    window is maximised, which is why the kind is mutable.
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setObjectName("WinBtnClose" if kind == "close" else "WinBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(46, TITLE_BAR_HEIGHT)

    def set_kind(self, kind: str) -> None:
        """Swap the glyph — used to flip maximise to restore and back."""
        if kind != self._kind:
            self._kind = kind
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)  # let the QSS paint the hover background
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # The close button glows white on its red hover fill; the others
            # simply brighten from the muted resting colour.
            if self.underMouse():
                colour = QColor("#FFFFFF") if self._kind == "close" else QColor(THEME.text_primary)
            else:
                colour = QColor(THEME.text_secondary)
            pen = QPen(colour, 1.3)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)

            centre = self.rect().center()
            half = _GLYPH_SIZE // 2
            box = QRect(centre.x() - half, centre.y() - half, _GLYPH_SIZE, _GLYPH_SIZE)
            self._paint_glyph(painter, box)
        finally:
            painter.end()

    def _paint_glyph(self, painter: QPainter, box: QRect) -> None:
        """Draw the glyph for the current kind inside ``box``."""
        if self._kind == "minimise":
            mid_y = box.center().y()
            painter.drawLine(box.left(), mid_y, box.right(), mid_y)
        elif self._kind == "maximise":
            painter.drawRect(box)
        elif self._kind == "restore":
            # Two offset squares — the classic "restore down" mark.
            offset = 2
            back = QRect(
                box.left() + offset, box.top(), box.width() - offset, box.height() - offset
            )
            front = QRect(
                box.left(), box.top() + offset, box.width() - offset, box.height() - offset
            )
            painter.drawRect(back)
            painter.fillRect(front, QColor(self._resting_fill()))
            painter.drawRect(front)
        elif self._kind == "close":
            painter.drawLine(box.topLeft(), box.bottomRight())
            painter.drawLine(box.topRight(), box.bottomLeft())

    def _resting_fill(self) -> str:
        """Background colour to punch the front restore square out against."""
        return THEME.bg_alt


class TitleBar(QWidget):
    """The frameless window's own title bar.

    Emits a signal for each window control rather than reaching into the
    window directly, so the main window stays the single owner of its own
    show/hide/close behaviour.
    """

    minimise_requested = Signal()
    maximise_requested = Signal()
    close_requested = Signal()

    def __init__(self, title: str = "S-Clip", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Offset from the window's top-left to the cursor at the moment a drag
        # begins; ``None`` whenever no drag is in progress.
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(THEME.space_md, 0, 0, 0)
        layout.setSpacing(THEME.space_xs)

        self._brand_mark = QLabel(self)
        self._brand_mark.setObjectName("TitleBarMark")
        self._apply_brand_mark()
        layout.addWidget(self._brand_mark)

        self._title_label = QLabel(title, self)
        self._title_label.setObjectName("TitleBarLabel")
        layout.addWidget(self._title_label)

        layout.addStretch(1)

        self._minimise_button = _WindowButton("minimise", self)
        self._minimise_button.clicked.connect(self.minimise_requested)
        layout.addWidget(self._minimise_button)

        self._maximise_button = _WindowButton("maximise", self)
        self._maximise_button.clicked.connect(self.maximise_requested)
        layout.addWidget(self._maximise_button)

        self._close_button = _WindowButton("close", self)
        self._close_button.clicked.connect(self.close_requested)
        layout.addWidget(self._close_button)

    # -- public API ---------------------------------------------------------

    def set_maximised(self, maximised: bool) -> None:
        """Flip the middle button between the maximise and restore glyphs."""
        self._maximise_button.set_kind("restore" if maximised else "maximise")

    # -- window dragging ----------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            # Record where the cursor sits relative to the window so the drag
            # keeps the window pinned to the pointer rather than jumping.
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is None:
            return
        window = self.window()
        # A maximised window cannot be dragged; restore it first so the drag
        # continues smoothly from under the cursor.
        if window.isMaximized():
            self.maximise_requested.emit()
            pt = event.position().toPoint()
            self._drag_offset = QPoint(pt.x(), pt.y())
            return
        window.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximise_requested.emit()
            event.accept()

    # -- internals ----------------------------------------------------------

    def _apply_brand_mark(self) -> None:
        """Render the small clip glyph that sits at the start of the bar."""
        try:
            from sclip.ui.assets.icons import icon

            pixmap = icon("clip", THEME.accent_text, 18).pixmap(18, 18)
        except Exception:
            logger.debug("Brand mark icon unavailable", exc_info=True)
            pixmap = QPixmap()
        self._brand_mark.setPixmap(pixmap)
        self._brand_mark.setFixedWidth(22)


__all__ = ["TITLE_BAR_HEIGHT", "TitleBar"]
