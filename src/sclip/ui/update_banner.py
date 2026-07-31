"""A quiet strip saying a newer S-Clip exists.

Deliberately not a dialog. A modal on start-up interrupts someone who opened
the application to record something, and the news that a newer version exists
is never urgent enough to justify that. The banner sits under the title bar,
takes one line, and can be dismissed.

The check itself runs on a worker thread and is described in
:mod:`sclip.core.updates`. Nothing here downloads or installs anything: the
link opens the release page in the user's browser and S-Clip's involvement
ends there.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from sclip.core.updates import Release, check_for_update
from sclip.ui.theme import SPACING_MD, SPACING_SM
from sclip.ui.widgets import IconButton

logger = logging.getLogger(__name__)


class _UpdateCheckSignals(QObject):
    """Carrier so the runnable can return its result to the GUI thread."""

    # Emits the Release, or None when there is nothing to report.
    finished = Signal(object)


class _UpdateCheckWorker(QRunnable):
    """Run the update check away from the GUI thread.

    The check makes a network request, and a request against an unreachable
    host can sit there for the whole timeout. On the GUI thread that would be
    a window that does not paint for six seconds on start-up.
    """

    def __init__(self, *, version: str, state_file: Path, enabled: bool) -> None:
        super().__init__()
        self._version = version
        self._state_file = state_file
        self._enabled = enabled
        self.signals = _UpdateCheckSignals()

    def run(self) -> None:  # pragma: no cover - exercised at runtime only
        try:
            release = check_for_update(
                self._version, state_file=self._state_file, enabled=self._enabled
            )
        except Exception:
            # check_for_update already swallows the failures it expects. This
            # is the backstop: an update check must never be able to take the
            # main window down with it.
            logger.exception("Update check failed unexpectedly")
            self.signals.finished.emit(None)
            return
        self.signals.finished.emit(release)


class UpdateBanner(QWidget):
    """One-line notice that a newer release is available.

    Starts hidden and stays hidden unless a check finds something, so on the
    overwhelmingly common path the user never sees it at all.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("UpdateBanner")
        self.setVisible(False)

        self._release: Release | None = None

        # A single-thread pool: there is only ever one check in flight, and
        # owning it here means it is torn down with the banner.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING_MD, SPACING_SM, SPACING_SM, SPACING_SM)
        layout.setSpacing(SPACING_SM)

        self._message = QLabel("", self)
        self._message.setObjectName("UpdateBannerText")
        layout.addWidget(self._message)
        layout.addStretch(1)

        self._view_button = IconButton(text="View release", role="ghost", parent=self)
        self._view_button.clicked.connect(self._on_view_release)
        layout.addWidget(self._view_button)

        self._dismiss_button = IconButton(text="Dismiss", role="ghost", parent=self)
        self._dismiss_button.clicked.connect(self._on_dismiss)
        layout.addWidget(self._dismiss_button)

    def start_check(self, *, version: str, state_file: Path, enabled: bool) -> None:
        """Begin a background check. Safe to call when updates are switched off.

        The disabled case is still handed to the worker rather than returned
        early here, so there is exactly one code path to reason about.
        """
        worker = _UpdateCheckWorker(version=version, state_file=state_file, enabled=enabled)
        worker.signals.finished.connect(self._on_check_finished)
        self._pool.start(worker)

    def _on_check_finished(self, release: object) -> None:
        """Show the banner if, and only if, there is something to show."""
        if not isinstance(release, Release):
            return
        self._release = release
        self._message.setText(release.headline)
        self.setVisible(True)

    def _on_view_release(self) -> None:
        """Open the release page in the user's browser."""
        if self._release is None:  # pragma: no cover - button is only reachable with one
            return
        QDesktopServices.openUrl(QUrl(self._release.url))
        self._on_dismiss()

    def _on_dismiss(self) -> None:
        """Hide the banner for this session.

        Not persisted on purpose. The next launch is a day away at least, and
        a dismissal that survived restarts would silently suppress every future
        release too.
        """
        self.setVisible(False)


__all__ = ["UpdateBanner"]
