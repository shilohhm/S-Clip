"""Card surface widget.

A Card is a rounded container that pages compose their content into. It owns
its own title/subtitle header but leaves the body completely up to the caller
-- you can drop a single widget in via :meth:`set_body`, or a layout via
:meth:`set_body_layout`, and the card adapts. Multiple bodies replace each
other, so reusing the same card for different states (e.g. "loading", "ready")
is straightforward.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.ui.theme import SPACING_LG, SPACING_SM

logger = logging.getLogger(__name__)


class Card(QFrame):
    """Rounded surface container with an optional header.

    The card uses a :class:`QFrame` rather than a plain :class:`QWidget`
    because QFrame's QSS support includes border-radius painting that the
    base QWidget skips on some Qt styles. The object name ``Card`` lets the
    QSS apply the surface colour and radius from the design tokens.
    """

    def __init__(
        self,
        title: str | None = None,
        subtitle: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # QFrame defaults to a sunken/raised paint depending on the platform
        # style; we want a flat surface and rely entirely on QSS.
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        self._outer.setSpacing(SPACING_SM)

        # -- Header (title + subtitle) ---------------------------------
        # Both labels are constructed up front and shown/hidden as needed.
        # Creating them lazily would mean the caller could not change the
        # title from None to a string without surprising re-layout cost.
        self._title_label = QLabel("", self)
        self._title_label.setObjectName("CardTitle")
        self._title_label.setVisible(False)
        self._outer.addWidget(self._title_label)

        self._subtitle_label = QLabel("", self)
        self._subtitle_label.setObjectName("CardSubtitle")
        self._subtitle_label.setVisible(False)
        self._subtitle_label.setWordWrap(True)
        self._outer.addWidget(self._subtitle_label)

        # -- Body host -------------------------------------------------
        # The body lives inside its own container so swapping bodies does
        # not perturb the header position. The container has no margins of
        # its own; the caller's widget/layout controls its internal spacing.
        self._body_host = QWidget(self)
        self._body_host_layout = QVBoxLayout(self._body_host)
        self._body_host_layout.setContentsMargins(0, 0, 0, 0)
        self._body_host_layout.setSpacing(0)
        self._outer.addWidget(self._body_host)

        if title is not None:
            self.set_title(title)
        if subtitle is not None:
            self.set_subtitle(subtitle)

    # -- Title / subtitle ---------------------------------------------------

    def set_title(self, title: str | None) -> None:
        """Update the card's title text, or hide the title when ``None``."""
        if title is None or title == "":
            self._title_label.clear()
            self._title_label.setVisible(False)
            return
        self._title_label.setText(title)
        self._title_label.setVisible(True)

    def set_subtitle(self, subtitle: str | None) -> None:
        """Update the card's subtitle text, or hide the subtitle when ``None``."""
        if subtitle is None or subtitle == "":
            self._subtitle_label.clear()
            self._subtitle_label.setVisible(False)
            return
        self._subtitle_label.setText(subtitle)
        self._subtitle_label.setVisible(True)

    # -- Body slot ----------------------------------------------------------

    def set_body(self, widget: QWidget) -> None:
        """Install ``widget`` as the card's body, replacing any previous body.

        The previous body widget is unparented (not deleted) so callers who
        kept a reference can reuse it elsewhere. If they didn't, the widget
        will be garbage-collected normally once it goes out of scope.
        """
        self._clear_body()
        self._body_host_layout.addWidget(widget)

    def set_body_layout(self, layout: QLayout) -> None:
        """Install ``layout`` as the card's body content.

        Useful when the caller wants to lay out several widgets in the body
        without first wrapping them in their own QWidget. The layout is
        nested under the host's QVBoxLayout, so any margin/spacing the
        caller configured is preserved.
        """
        self._clear_body()
        # QLayout cannot live alone -- we wrap it in a transparent host widget
        # so removing the body later is just a matter of dropping that widget.
        host = QWidget(self._body_host)
        host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        host.setLayout(layout)
        self._body_host_layout.addWidget(host)

    def clear_body(self) -> None:
        """Public wrapper around :meth:`_clear_body` for callers."""
        self._clear_body()

    def body_layout(self) -> QVBoxLayout:
        """Return the layout callers can append page-specific content to."""
        return self._body_host_layout

    # -- Internal helpers ---------------------------------------------------

    def _clear_body(self) -> None:
        # Remove every child widget from the body host layout, hiding it and
        # detaching it from its parent so Qt does not paint the leftover.
        while self._body_host_layout.count():
            item = self._body_host_layout.takeAt(0)
            if item is None:
                continue
            child = item.widget()
            if child is not None:
                child.setParent(None)
            # No need to walk sub-layouts: anything we install lives inside a
            # widget host (see set_body_layout), so removing the host widget
            # unparents its contained layout cleanly.


__all__ = ["Card"]
