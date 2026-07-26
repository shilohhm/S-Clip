"""About page — identity, a plain-English explainer, credits and diagnostics.

This screen is deliberately calm: it is where a puzzled user lands when an
install misbehaves, so it must always render cleanly even when the
environment is broken. Every call that touches the filesystem or shells out
to FFmpeg is guarded, and the FFmpeg probe runs off the GUI thread so the
page paints instantly and fills its values in a moment later.

Pure presentation — the page owns no engine state and persists nothing.
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sclip.core.ffmpeg import find_ffmpeg, run_ffmpeg
from sclip.ui.assets.icons import icon
from sclip.ui.theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
    THEME,
)
from sclip.ui.widgets import Card, IconButton
from sclip.version import __version__

logger = logging.getLogger(__name__)


# -- Static content --------------------------------------------------------
# Project links use placeholder URLs; kept as module constants so the page
# can be retargeted without unpicking widget code.
_REPO_URL = "https://github.com/sclip/sclip"
_BUG_REPORT_URL = "https://github.com/sclip/sclip/issues"

_TAGLINE = "A lightweight Windows screen recorder with a true rolling replay buffer."

# The replay-buffer explainer, kept warm and plain. UK English throughout.
_REPLAY_EXPLAINER = (
    "S-Clip quietly keeps the last few minutes of your screen in a rolling "
    "buffer on disk, overwriting the oldest footage as it goes. When "
    "something worth keeping happens, press the clip hotkey and S-Clip "
    "stitches that buffer into a smooth MP4. Nothing is ever uploaded — your "
    "clips stay on your machine, and yours alone."
)

# Acknowledgements as plain data: name, licence, one-line description. Held
# as a tuple so the row builder can format every entry identically.
_ACKNOWLEDGEMENTS: Sequence[tuple[str, str, str]] = (
    ("PySide6", "LGPL v3", "Qt for Python, the application framework."),
    ("FFmpeg", "LGPL / GPL", "Screen capture and video encoding."),
    ("screeninfo", "MIT", "Enumerates the connected monitors."),
    ("pynput", "LGPL v3", "The global keyboard hook for hotkeys."),
    ("platformdirs", "MIT", "Finds the per-user config and data folders."),
    ("PyAudioWPatch", "MIT", "WASAPI loopback capture for desktop audio."),
)

# Placeholder text shown until the asynchronous FFmpeg probe returns.
_PROBE_PLACEHOLDER = "checking…"

# Fixed column widths for the two table-like lists. Pinning them keeps the
# names and licences in tidy vertical columns regardless of content length.
_ACK_NAME_WIDTH = 130
_ACK_LICENCE_WIDTH = 110
_DIAG_KEY_WIDTH = 150

# The app mark sits on a square inset well; 72px reads as a generous,
# unhurried badge without crowding the identity text beside it.
_APP_MARK_SIZE = 72
_APP_MARK_ICON_SIZE = 40


# ---------------------------------------------------------------------------
# Background worker for the FFmpeg diagnostics probe
# ---------------------------------------------------------------------------


class _FFmpegProbeSignals(QObject):
    """Carrier object so the runnable can emit results back to the GUI thread.

    ``QRunnable`` is not a ``QObject`` and therefore cannot own signals; a
    small companion object is the standard Qt way to bridge the two.
    """

    # Emits (path, version) — either field is an empty string when unknown.
    finished = Signal(str, str)


class _FFmpegProbeWorker(QRunnable):
    """Resolve the FFmpeg path and version on a worker thread.

    Both steps can block: ``find_ffmpeg`` walks the filesystem, and probing
    the version spawns the binary. Doing this work here keeps page
    construction instant — the GUI thread never waits on a subprocess.
    """

    def run(self) -> None:  # pragma: no cover - exercised at runtime only
        path_text = ""
        version_text = ""

        # find_ffmpeg raises FFmpegNotFoundError (and potentially other
        # filesystem errors) on a broken install; treat any failure as
        # "not found" so the page still shows a sensible value.
        try:
            path_text = str(find_ffmpeg())
        except Exception as exc:
            logger.info("Could not resolve FFmpeg: %s", exc)
            self.signals.finished.emit("", "")
            return

        # The version lives on the first line of ``ffmpeg -version``, e.g.
        # "ffmpeg version 7.1-essentials_build ...". We want just the token
        # after the word "version".
        try:
            result = run_ffmpeg(["-version"])
            version_text = _parse_ffmpeg_version(result.stdout or "")
        except Exception as exc:
            logger.warning("FFmpeg version probe failed: %s", exc)

        self.signals.finished.emit(path_text, version_text)

    def __init__(self) -> None:
        super().__init__()
        self.signals = _FFmpegProbeSignals()


def _parse_ffmpeg_version(stdout: str) -> str:
    """Pull the version token out of ``ffmpeg -version`` output.

    Returns an empty string when the output is empty or unexpectedly shaped,
    so the caller can fall back to a friendly placeholder.
    """
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    tokens = first_line.split()
    # Expect "ffmpeg version <token> ..."; the token sits at index 2.
    if len(tokens) >= 3 and tokens[0] == "ffmpeg" and tokens[1] == "version":
        return tokens[2]
    return ""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class AboutPage(QWidget):
    """Static information about the app, its credits, and the local environment."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Diagnostics launches exactly one probe; a single-thread pool keeps
        # the work off the shared global pool without over-provisioning.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        # The four diagnostic values, cached so "Copy diagnostics" can hand
        # them to the clipboard without re-reading any widget.
        self._diag_values: dict[str, str] = {
            "FFmpeg path": _PROBE_PLACEHOLDER,
            "FFmpeg version": _PROBE_PLACEHOLDER,
            "Operating system": platform.platform(),
            "Python": platform.python_version(),
        }

        self._build_ui()
        self._start_ffmpeg_probe()

    # ------------------------------------------------------ UI assembly

    def _build_ui(self) -> None:
        """Assemble the page: a scroll area wrapping a column of cards.

        The page root holds nothing but the scroll area so the cards can grow
        past the window height and remain reachable; the generous outer
        margins give the calm, unhurried feel the design asks for.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("AboutContent")
        column = QVBoxLayout(content)
        column.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        column.setSpacing(SPACING_LG)

        title = QLabel("About", content)
        title.setProperty("role", "display")
        column.addWidget(title)

        column.addWidget(self._build_identity_card(content))
        column.addWidget(self._build_replay_card(content))
        column.addWidget(self._build_acknowledgements_card(content))
        column.addWidget(self._build_diagnostics_card(content))
        column.addWidget(self._build_links_card(content))
        # Trailing stretch so the cards stay top-aligned when the window is
        # taller than the content.
        column.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll)

    def _build_identity_card(self, parent: QWidget) -> Card:
        """The hero card: the app mark beside the name, version and tagline."""
        card = Card(parent=parent)
        body = card.body_layout()

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING_LG)

        row.addWidget(self._build_app_mark(card), 0, Qt.AlignmentFlag.AlignTop)

        # Right-hand identity stack: name, version, tagline.
        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(SPACING_XS)

        name = QLabel("S-Clip", card)
        name.setProperty("role", "display")
        text_column.addWidget(name)

        version = QLabel(f"Version {__version__}", card)
        version.setProperty("role", "muted")
        text_column.addWidget(version)

        tagline = QLabel(_TAGLINE, card)
        tagline.setProperty("role", "caption")
        tagline.setWordWrap(True)
        # A little breathing room above the tagline separates it from the
        # version line without needing a divider.
        text_column.addSpacing(SPACING_XS)
        text_column.addWidget(tagline)
        text_column.addStretch(1)

        row.addLayout(text_column, 1)
        body.addLayout(row)
        return card

    def _build_app_mark(self, parent: QWidget) -> QWidget:
        """Render the clip icon onto a rounded inset square — the app badge."""
        mark = QFrame(parent)
        mark.setObjectName("Inset")
        mark.setFixedSize(_APP_MARK_SIZE, _APP_MARK_SIZE)

        layout = QVBoxLayout(mark)
        layout.setContentsMargins(0, 0, 0, 0)

        glyph = QLabel(mark)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # icon() yields a QIcon; a QLabel needs a pixmap, so render one at
        # the desired logical size and account for HiDPI via the icon API.
        pixmap = icon("clip", THEME.accent_text, _APP_MARK_ICON_SIZE).pixmap(
            _APP_MARK_ICON_SIZE, _APP_MARK_ICON_SIZE
        )
        glyph.setPixmap(pixmap)
        layout.addWidget(glyph)

        return mark

    def _build_replay_card(self, parent: QWidget) -> Card:
        """A friendly explainer of what the rolling replay buffer actually does."""
        card = Card(title="How the replay buffer works", parent=parent)
        body = card.body_layout()

        explainer = QLabel(_REPLAY_EXPLAINER, card)
        explainer.setProperty("role", "caption")
        explainer.setWordWrap(True)
        body.addWidget(explainer)

        return card

    def _build_acknowledgements_card(self, parent: QWidget) -> Card:
        """Credit the open-source projects S-Clip is built on, one row each."""
        card = Card(title="Acknowledgements", parent=parent)
        body = card.body_layout()
        body.setSpacing(SPACING_SM)

        for name, licence, description in _ACKNOWLEDGEMENTS:
            body.addLayout(self._build_acknowledgement_row(card, name, licence, description))

        return card

    @staticmethod
    def _build_acknowledgement_row(
        parent: QWidget, name: str, licence: str, description: str
    ) -> QHBoxLayout:
        """One acknowledgement row: name, licence and description in columns."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING_MD)

        name_label = QLabel(name, parent)
        name_label.setProperty("role", "value")
        name_label.setFixedWidth(_ACK_NAME_WIDTH)
        row.addWidget(name_label, 0, Qt.AlignmentFlag.AlignTop)

        licence_label = QLabel(licence, parent)
        licence_label.setProperty("role", "muted")
        licence_label.setFixedWidth(_ACK_LICENCE_WIDTH)
        row.addWidget(licence_label, 0, Qt.AlignmentFlag.AlignTop)

        description_label = QLabel(description, parent)
        description_label.setProperty("role", "caption")
        description_label.setWordWrap(True)
        row.addWidget(description_label, 1)

        return row

    def _build_diagnostics_card(self, parent: QWidget) -> Card:
        """Environment facts in a recessed panel, handy for bug reports."""
        card = Card(
            title="Diagnostics",
            subtitle="Handy when reporting a bug",
            parent=parent,
        )
        body = card.body_layout()
        body.setSpacing(SPACING_MD)

        # The recessed inset groups the key/value rows into one quiet panel.
        inset = QFrame(card)
        inset.setObjectName("Inset")
        inset_layout = QVBoxLayout(inset)
        inset_layout.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        inset_layout.setSpacing(SPACING_SM)

        # Keep references to the two async value labels so the probe callback
        # can fill them in once the worker returns.
        self._value_labels: dict[str, QLabel] = {}
        for key in self._diag_values:
            row, value_label = self._build_diagnostic_row(inset, key, self._diag_values[key])
            self._value_labels[key] = value_label
            inset_layout.addLayout(row)

        body.addWidget(inset)

        # The copy action sits below the panel, left-aligned, not stretched.
        copy_button = IconButton(text="Copy diagnostics", role="ghost", parent=card)
        copy_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        copy_button.clicked.connect(self._copy_diagnostics)

        copy_row = QHBoxLayout()
        copy_row.setContentsMargins(0, 0, 0, 0)
        copy_row.addWidget(copy_button)
        copy_row.addStretch(1)
        body.addLayout(copy_row)

        return card

    @staticmethod
    def _build_diagnostic_row(parent: QWidget, key: str, value: str) -> tuple[QHBoxLayout, QLabel]:
        """One key/value row: a label key and a monospace value."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING_MD)

        key_label = QLabel(key, parent)
        key_label.setProperty("role", "label")
        key_label.setFixedWidth(_DIAG_KEY_WIDTH)
        row.addWidget(key_label, 0, Qt.AlignmentFlag.AlignTop)

        value_label = QLabel(value, parent)
        value_label.setProperty("role", "mono")
        value_label.setWordWrap(True)
        # The value is the useful part of a bug report — let users select it.
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(value_label, 1)

        return row, value_label

    def _build_links_card(self, parent: QWidget) -> Card:
        """A row of project links: open the repository, or report a bug."""
        card = Card(title="Links", parent=parent)
        body = card.body_layout()

        repo_button = IconButton(text="Open repository", role="primary", parent=card)
        repo_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        repo_button.clicked.connect(lambda: self._open_url(_REPO_URL))

        bug_button = IconButton(text="Report a bug", role="ghost", parent=card)
        bug_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        bug_button.clicked.connect(lambda: self._open_url(_BUG_REPORT_URL))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING_SM)
        row.addWidget(repo_button)
        row.addWidget(bug_button)
        # Trailing stretch so the buttons keep their natural width instead of
        # spanning the whole card.
        row.addStretch(1)
        body.addLayout(row)

        return card

    # ------------------------------------------------------ FFmpeg probe

    def _start_ffmpeg_probe(self) -> None:
        """Kick off the diagnostics probe on a worker thread.

        Construction must never block, so the path lookup and version probe
        both run on the pool; the placeholder text stands in until the
        worker emits its result.
        """
        worker = _FFmpegProbeWorker()
        worker.signals.finished.connect(self._on_ffmpeg_probe_finished)
        self._pool.start(worker)

    def _on_ffmpeg_probe_finished(self, path: str, version: str) -> None:
        """Fill the FFmpeg diagnostic rows once the worker thread returns."""
        path_text = path if path else "not found"
        version_text = version if version else "not found"

        self._set_diagnostic("FFmpeg path", path_text)
        self._set_diagnostic("FFmpeg version", version_text)

    def _set_diagnostic(self, key: str, value: str) -> None:
        """Update one diagnostic value, both on screen and in the copy cache."""
        self._diag_values[key] = value
        label = self._value_labels.get(key)
        if label is not None:
            label.setText(value)

    # ------------------------------------------------------ Actions

    def _copy_diagnostics(self) -> None:
        """Copy the four diagnostic values to the clipboard as plain text."""
        lines = [f"{key}: {value}" for key, value in self._diag_values.items()]
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(lines))
            logger.debug("Diagnostics copied to clipboard")

    @staticmethod
    def _open_url(url: str) -> None:
        """Open an external URL in the user's default browser."""
        if not QDesktopServices.openUrl(QUrl(url)):
            logger.warning("Could not open URL: %s", url)


__all__ = ["AboutPage"]
