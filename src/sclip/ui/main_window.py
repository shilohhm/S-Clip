"""The application main window.

This is the chrome that hosts the four navigation destinations (capture,
library, settings, about) and bridges the non-Qt subsystems -- capture engine,
hotkey listener, settings store -- into the GUI thread. The window also owns
the system tray icon, so closing the window minimises to tray while still
keeping the capture engine alive.

The window receives every dependency through its constructor; nothing inside
is built from globals. That makes it trivial to swap a real engine for a fake
during pytest-qt tests, and it keeps the dependency direction explicit:
``app.py`` wires, the window consumes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from sclip.contracts import CaptureEngine, DeviceRegistry, Hotkey, Settings, SettingsStore
from sclip.paths import app_paths
from sclip.ui.theme import load_stylesheet

if TYPE_CHECKING:
    from sclip.hotkeys import HotkeyListener
    from sclip.ui.pages import CapturePage, SettingsPage


logger = logging.getLogger(__name__)


# -- navigation indices ----------------------------------------------------
# Naming the stack indices keeps the sidebar wiring readable -- the magic
# numbers ``0..3`` would otherwise drift apart from the page order.
_PAGE_CAPTURE: int = 0
_PAGE_LIBRARY: int = 1
_PAGE_SETTINGS: int = 2
_PAGE_ABOUT: int = 3


class MainWindow(QMainWindow):
    """The S-Clip main window.

    Hosts a left sidebar, a right :class:`QStackedWidget` containing the four
    page widgets, and a system tray icon. All cross-thread traffic from the
    hotkey listener funnels through the :attr:`_hotkey_clip_requested` and
    :attr:`_hotkey_record_requested` signals so we never touch Qt widgets from
    a non-Qt thread.
    """

    # Signals fired from the hotkey listener thread; connecting them to a
    # ``Slot`` on this object marshals execution onto the GUI thread via Qt's
    # queued connection machinery -- safer than ``QMetaObject.invokeMethod``.
    _hotkey_clip_requested = Signal()
    _hotkey_record_requested = Signal()

    # Signals fired from the capture engine's listener callbacks, which run on
    # the engine's clip-save worker thread. They are bridged onto the GUI
    # thread exactly like the hotkey signals above, so the tray balloon is
    # only ever touched from the Qt thread.
    _engine_clip_saved = Signal(object)
    _engine_error = Signal(str)

    def __init__(
        self,
        engine: CaptureEngine,
        settings_store: SettingsStore,
        device_registry: DeviceRegistry,
        hotkey_listener: HotkeyListener,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        # Hold every collaborator by name so handlers below can reach them
        # without dragging the whole bootstrap context around with them.
        self._engine = engine
        self._settings_store = settings_store
        self._device_registry = device_registry
        self._hotkey_listener = hotkey_listener

        # Working copy of the persisted settings -- used to compare against
        # the new value when ``settings_saved`` fires so we know which hotkeys
        # to unregister before re-registering.
        self._current_settings: Settings = settings_store.load()
        # Quit-from-tray flag: ``closeEvent`` normally minimises to tray, but
        # the tray menu's Quit action sets this so we really close.
        self._force_quit: bool = False

        self.setWindowTitle("S-Clip")
        # Frameless: S-Clip paints its own title bar (see TitleBar) so the
        # window chrome matches the rest of the interface.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumSize(QSize(960, 640))
        self.resize(QSize(1240, 820))
        self.setStyleSheet(load_stylesheet())
        self._apply_window_icon()

        # Build chrome before wiring signals so the slots have valid targets.
        self._stack = QStackedWidget(self)
        self._capture_page: QWidget | None = None
        self._library_page: QWidget | None = None
        self._settings_page: QWidget | None = None
        self._about_page: QWidget | None = None
        self._sidebar: QWidget | None = None

        self._build_pages()
        self._build_layout()
        self._build_tray()

        self._connect_hotkey_signals()
        self._connect_engine_signals()
        self._register_settings_hotkeys(self._current_settings)

    # -- construction helpers ----------------------------------------------

    def _apply_window_icon(self) -> None:
        """Set the window icon, falling back gracefully when the asset is missing.

        The assets agent is in flight; missing the icon during early bring-up
        should not crash the bootstrap. We log so the gap is visible without
        becoming a user-facing failure.
        """
        icon_path = app_paths().assets_dir / "icon.png"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            logger.warning("Window icon not found at %s; using default", icon_path)

    def _build_pages(self) -> None:
        """Instantiate the four page widgets.

        Each page is wrapped in a ``try/except ImportError`` so the window
        still constructs even if a parallel agent has not yet landed a page
        -- we drop in a placeholder ``QWidget`` so the layout keeps its
        slots. Once every page module exists the placeholders disappear.
        """
        self._capture_page = self._safe_make_page(
            "capture",
            lambda: _import_capture_page()(self._engine, self._settings_store),
        )
        self._library_page = self._safe_make_page("library", _import_library_page)
        self._settings_page = self._safe_make_page(
            "settings",
            lambda: _import_settings_page()(self._settings_store, self._device_registry),
        )
        self._about_page = self._safe_make_page("about", _import_about_page)

        # Order matters: the page indices defined at module scope must match.
        for page in (self._capture_page, self._library_page, self._settings_page, self._about_page):
            assert page is not None  # _safe_make_page never returns None
            self._stack.addWidget(page)

        # Wire page-level signals where they exist; using ``getattr`` keeps
        # us tolerant of pages that have not yet implemented the navigation
        # or save signals.
        navigate = getattr(self._capture_page, "request_navigate", None)
        if navigate is not None:
            navigate.connect(self._on_navigate_requested)

        saved = getattr(self._settings_page, "settings_saved", None)
        if saved is not None:
            saved.connect(self._apply_settings)

    def _safe_make_page(self, name: str, factory: object) -> QWidget:
        """Try to build a page; on failure log and return a placeholder.

        Parallel agents may not have landed every page yet. Rather than
        refuse to start, we display an empty placeholder so the user can at
        least navigate to the pages that do exist.
        """
        try:
            page = factory()  # type: ignore[operator]
        except Exception:
            logger.exception("Could not build %s page; using placeholder", name)
            placeholder = QWidget(self)
            placeholder.setObjectName(f"placeholder_{name}")
            return placeholder
        if not isinstance(page, QWidget):
            logger.error("%s factory returned %r; expected QWidget", name, type(page))
            return QWidget(self)
        return page

    def _build_layout(self) -> None:
        """Compose the title bar, sidebar and page stack into the window.

        The window is frameless, so the layout stacks our own title bar above
        a horizontal body of sidebar plus pages. A corner size grip is the
        one piece of resize affordance a frameless window still needs.
        """
        from sclip.ui.widgets import TitleBar

        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- Title bar --------------------------------------------------
        self._title_bar = TitleBar("S-Clip", self)
        self._title_bar.minimise_requested.connect(self.showMinimized)
        self._title_bar.maximise_requested.connect(self._toggle_maximised)
        self._title_bar.close_requested.connect(self.close)
        outer.addWidget(self._title_bar)

        # -- Body: sidebar + pages --------------------------------------
        body = QWidget(central)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._sidebar = self._build_sidebar()
        body_layout.addWidget(self._sidebar)
        body_layout.addWidget(self._stack, stretch=1)
        outer.addWidget(body, stretch=1)

        self.setCentralWidget(central)

        # A frameless window keeps no native resize border, so we float a
        # size grip in the bottom-right corner. _reposition_size_grip keeps
        # it pinned there as the window resizes.
        self._size_grip = QSizeGrip(central)
        self._size_grip.resize(16, 16)
        self._size_grip.raise_()

        self._set_current_page(_PAGE_CAPTURE)

    def _toggle_maximised(self) -> None:
        """Flip the window between maximised and normal size."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _reposition_size_grip(self) -> None:
        """Keep the corner size grip pinned to the bottom-right."""
        grip = getattr(self, "_size_grip", None)
        if grip is None:
            return
        parent = grip.parentWidget()
        if parent is None:
            return
        grip.move(parent.width() - grip.width(), parent.height() - grip.height())
        grip.raise_()

    def _build_sidebar(self) -> QWidget:
        """Construct the navigation sidebar.

        If the shared ``Sidebar`` widget is not yet available we fall back to
        a tiny shim with four ``QPushButton`` entries so navigation still
        works while the design agent lands the real component.
        """
        try:
            from sclip.ui.widgets import Sidebar, SidebarItem
        except Exception:
            logger.warning("Sidebar widget unavailable; using fallback navigation")
            return self._build_fallback_sidebar()

        # Each nav row carries a recoloured SVG glyph. A missing icon module
        # is non-fatal — the sidebar simply shows text-only rows.
        try:
            from sclip.ui.assets.icons import icon
            from sclip.ui.theme import THEME

            tint = THEME.text_secondary
            nav_icons = {
                "capture": icon("record", tint, 18),
                "library": icon("library", tint, 18),
                "settings": icon("settings", tint, 18),
                "about": icon("about", tint, 18),
            }
        except Exception:
            logger.debug("Sidebar icons unavailable; using text-only navigation")
            nav_icons = {}

        try:
            sidebar = Sidebar(
                [
                    SidebarItem("Capture", icon=nav_icons.get("capture")),
                    SidebarItem("Library", icon=nav_icons.get("library")),
                    SidebarItem("Settings", icon=nav_icons.get("settings")),
                    SidebarItem("About", icon=nav_icons.get("about")),
                ],
                # The frameless title bar already carries the brand, so the
                # sidebar opens straight onto its navigation.
                app_name="",
                parent=self,
            )
            # The shared widget is expected to expose either a ``navigate``
            # signal carrying the page index, or a ``currentChanged`` signal.
            # We bind whichever it has so the navigation works regardless.
            for signal_name in (
                "navigate",
                "current_changed",
                "currentChanged",
                "currentIndexChanged",
            ):
                signal = getattr(sidebar, signal_name, None)
                if signal is None:
                    continue
                try:
                    signal.connect(self._on_navigate_index)
                    break
                except (TypeError, AttributeError):
                    continue
        except Exception:
            logger.exception("Sidebar widget failed to construct; using fallback")
            return self._build_fallback_sidebar()

        return sidebar

    def _build_fallback_sidebar(self) -> QWidget:
        """A simple sidebar so the app is navigable during bring-up.

        Replaced once :class:`sclip.ui.widgets.Sidebar` is available.
        """
        from PySide6.QtWidgets import QPushButton, QVBoxLayout

        holder = QWidget(self)
        holder.setObjectName("sidebar_fallback")
        holder.setFixedWidth(180)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        for label, index in (
            ("Capture", _PAGE_CAPTURE),
            ("Library", _PAGE_LIBRARY),
            ("Settings", _PAGE_SETTINGS),
            ("About", _PAGE_ABOUT),
        ):
            button = QPushButton(label, holder)
            # Default-binding by lambda would close over the loop variable;
            # ``partial``-style closure via default arg avoids the classic
            # late-binding bug.
            button.clicked.connect(lambda _checked=False, idx=index: self._on_navigate_index(idx))
            layout.addWidget(button)
        layout.addStretch(1)
        return holder

    def _build_tray(self) -> None:
        """Create the system tray icon, menu, and click handler.

        On Windows the tray is always available. We still guard the
        availability check so the same code path works on developer machines
        that do not have a tray.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray unavailable -- skipping tray icon")
            self._tray: QSystemTrayIcon | None = None
            self._tray_menu: QMenu | None = None
            self._tray_clip_action: QAction | None = None
            self._tray_record_action: QAction | None = None
            return

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self.windowIcon())
        self._tray.setToolTip("S-Clip")

        menu = QMenu(self)

        show_action = QAction("Show S-Clip", menu)
        show_action.triggered.connect(self._on_tray_show_requested)
        menu.addAction(show_action)

        self._tray_clip_action = QAction(self._clip_action_label(self._current_settings), menu)
        self._tray_clip_action.triggered.connect(self._on_clip_requested)
        menu.addAction(self._tray_clip_action)

        self._tray_record_action = QAction(
            self._record_action_label(self._current_settings), menu
        )
        self._tray_record_action.triggered.connect(self._on_record_requested)
        menu.addAction(self._tray_record_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._on_quit_requested)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        self._tray_menu = menu

    # -- signal wiring ------------------------------------------------------

    def _connect_hotkey_signals(self) -> None:
        """Connect the cross-thread signals to their Qt-thread slots.

        ``QueuedConnection`` is the explicit, defensive choice -- even though
        Qt would auto-detect the cross-thread case, stating it makes the
        contract clear to a future reader.
        """
        self._hotkey_clip_requested.connect(
            self._on_clip_requested, Qt.ConnectionType.QueuedConnection
        )
        self._hotkey_record_requested.connect(
            self._on_record_requested, Qt.ConnectionType.QueuedConnection
        )

    def _connect_engine_signals(self) -> None:
        """Subscribe to the capture engine and bridge its events to the GUI.

        The engine's clip-save now runs on a worker thread, so the
        ``add_clip_listener`` / ``add_error_listener`` callbacks fire off the
        GUI thread. Each callback only emits a :class:`Signal`; the
        ``QueuedConnection`` then hands the slot to the Qt thread, where it is
        safe to show a tray balloon. This mirrors the hotkey bridge above.
        """
        self._engine_clip_saved.connect(
            self._on_engine_clip_saved, Qt.ConnectionType.QueuedConnection
        )
        self._engine_error.connect(
            self._on_engine_error, Qt.ConnectionType.QueuedConnection
        )
        self._engine.add_clip_listener(self._engine_clip_saved.emit)
        self._engine.add_error_listener(self._engine_error.emit)

    def _register_settings_hotkeys(self, settings: Settings) -> None:
        """Install (or refresh) the global hotkey bindings from ``settings``."""
        # Re-emit the signals from the listener thread; the signal then hops
        # over to the Qt thread via the queued connection above.
        self._hotkey_listener.register(
            settings.clip_hotkey,
            self._hotkey_clip_requested.emit,
        )
        self._hotkey_listener.register(
            settings.record_hotkey,
            self._hotkey_record_requested.emit,
        )

    # -- slots --------------------------------------------------------------

    @Slot(int)
    def _on_navigate_index(self, index: int) -> None:
        """Switch the stack to ``index``, clamping out-of-range values.

        Defensive clamping keeps a misbehaving sidebar implementation from
        breaking navigation -- we always show *something*.
        """
        if not 0 <= index < self._stack.count():
            logger.warning("Navigation index %d out of range; ignoring", index)
            return
        self._set_current_page(index)

    @Slot(str)
    def _on_navigate_requested(self, destination: str) -> None:
        """Handle navigation requests carrying a string identifier.

        :class:`~sclip.ui.pages.capture_page.CapturePage` emits names like
        ``"library"`` or ``"settings"``; this slot translates them to indices.
        Unknown names are logged but otherwise ignored.
        """
        lookup = {
            "capture": _PAGE_CAPTURE,
            "library": _PAGE_LIBRARY,
            "settings": _PAGE_SETTINGS,
            "about": _PAGE_ABOUT,
        }
        index = lookup.get(destination.strip().lower())
        if index is None:
            logger.warning("Unknown navigation destination: %r", destination)
            return
        self._set_current_page(index)

    @Slot(object)
    def _apply_settings(self, settings: Settings) -> None:
        """Re-apply ``settings`` after the user saves them.

        Three things change at most: the hotkey bindings, the tray menu
        labels, and -- if the engine exposes one -- the engine's own reload
        hook. We swap each piece individually rather than rebuilding the
        whole tray to keep the user's tray icon state stable.
        """
        previous = self._current_settings
        self._current_settings = settings

        self._refresh_hotkeys(previous, settings)
        self._refresh_tray_labels(settings)
        self._reload_engine_if_supported()

    def _refresh_hotkeys(self, previous: Settings, current: Settings) -> None:
        """Unregister the previous chord pair and install the new one."""
        # Drop the previous bindings first so we never leave stale chords
        # firing after a rename.
        self._hotkey_listener.unregister(previous.clip_hotkey)
        self._hotkey_listener.unregister(previous.record_hotkey)
        self._register_settings_hotkeys(current)

    def _refresh_tray_labels(self, settings: Settings) -> None:
        """Update the tray menu so the labels reflect the live hotkeys."""
        if self._tray_clip_action is not None:
            self._tray_clip_action.setText(self._clip_action_label(settings))
        if self._tray_record_action is not None:
            self._tray_record_action.setText(self._record_action_label(settings))

    def _reload_engine_if_supported(self) -> None:
        """Ask the engine to pick up the new settings, if it has a reload hook.

        The :class:`~sclip.contracts.CaptureEngine` protocol does not require
        a reload method; the concrete implementation may grow one, in which
        case we call it here. Otherwise the engine will pick up the new
        configuration on its next start.
        """
        reload_hook = getattr(self._engine, "reload_settings", None)
        if reload_hook is None:
            return
        try:
            reload_hook()
        except Exception:
            logger.exception("Capture engine failed to reload settings")

    # -- action handlers ----------------------------------------------------

    @Slot()
    def _on_clip_requested(self) -> None:
        """Kick off saving the rolling replay buffer's last N seconds to disk.

        The save is asynchronous: :meth:`CaptureEngine.save_replay_clip` no
        longer returns the written path -- it spawns a worker and returns at
        once. We therefore check the engine state up front to decide which
        tray balloon to show, then let the engine's clip/error listeners
        (bridged through :meth:`_on_engine_clip_saved` and
        :meth:`_on_engine_error`) announce the eventual outcome.
        """
        from sclip.contracts import CaptureState

        if self._engine.state is not CaptureState.BUFFERING:
            self._show_tray_message("S-Clip", "Replay buffer is not running.")
            return
        try:
            self._engine.save_replay_clip()
        except Exception:
            logger.exception("Saving replay clip failed")
            self._show_tray_message(
                "S-Clip", "Could not save the replay clip -- see the log for details."
            )
            return
        self._show_tray_message("S-Clip", "Saving clip…")

    @Slot(object)
    def _on_engine_clip_saved(self, path: object) -> None:
        """Announce a finished clip save via the tray balloon.

        Bridged from the engine's clip listener, so this always runs on the
        GUI thread (see :meth:`_connect_engine_signals`).
        """
        from pathlib import Path

        clip_path = path if isinstance(path, Path) else Path(str(path))
        self._show_tray_message("S-Clip", f"Clip saved: {clip_path.name}")

    @Slot(str)
    def _on_engine_error(self, message: str) -> None:
        """Surface a capture-engine error via the tray balloon.

        Bridged from the engine's error listener, so this always runs on the
        GUI thread (see :meth:`_connect_engine_signals`).
        """
        logger.error("Capture engine error: %s", message)
        self._show_tray_message("S-Clip", message)

    @Slot()
    def _on_record_requested(self) -> None:
        """Toggle manual recording on or off.

        The :class:`~sclip.contracts.CaptureEngine` does not expose a single
        ``toggle`` method by design -- we inspect the live ``state`` and pick
        the right call. Errors are swallowed and surfaced to the user via a
        tray balloon so a misconfigured engine cannot dump a traceback.
        """
        from sclip.contracts import CaptureState

        try:
            if self._engine.state == CaptureState.RECORDING:
                self._engine.stop_manual_recording()
            else:
                self._engine.start_manual_recording()
        except Exception:
            logger.exception("Toggling manual recording failed")
            self._show_tray_message(
                "S-Clip", "Recording toggle failed -- see the log for details."
            )

    @Slot()
    def _on_tray_show_requested(self) -> None:
        """Bring the window back from a tray-minimised state."""
        self._raise_to_foreground()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Single left-click on the tray toggles window visibility.

        Right-click is handled by Qt automatically (it opens the context
        menu) so we only react to the trigger and double-click activations.
        """
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self._raise_to_foreground()

    @Slot()
    def _on_quit_requested(self) -> None:
        """Quit the application -- the tray's Quit action."""
        self._force_quit = True
        if self._tray is not None:
            self._tray.hide()
        # ``QApplication`` shuts down once the main window closes provided
        # ``quitOnLastWindowClosed`` is true; ``close`` is enough.
        self.close()

    # -- helpers ------------------------------------------------------------

    def raise_from_other_instance(self) -> None:
        """Used by the single-instance guard to wake an already-running window.

        Public so :mod:`sclip.app` can call it from the local-server listener
        without dragging the slot into the protocol's public API.
        """
        self._raise_to_foreground()

    def _raise_to_foreground(self) -> None:
        """Restore, show and activate the window.

        Windows sometimes leaves an inactive window stranded behind other
        applications; the combined ``showNormal`` + ``raise_`` + ``activateWindow``
        dance is the recommended way to force it forward.
        """
        # ``showNormal`` clears a minimised state; falls through to ``show``
        # on a window that was simply hidden.
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _show_tray_message(self, title: str, body: str) -> None:
        """Surface a short message via the tray balloon when one exists."""
        if self._tray is None:
            return
        self._tray.showMessage(
            title,
            body,
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _set_current_page(self, index: int) -> None:
        """Select a page and keep the sidebar's checked row aligned."""
        if self._stack.currentIndex() != index:
            self._stack.setCurrentIndex(index)

        sidebar_setter = getattr(self._sidebar, "set_current_index", None)
        if callable(sidebar_setter):
            sidebar_setter(index)

    def _clip_action_label(self, settings: Settings) -> str:
        """Tray menu label that surfaces the active clip hotkey."""
        return f"Save replay clip ({self._hotkey_display(settings.clip_hotkey)})"

    def _record_action_label(self, settings: Settings) -> str:
        """Tray menu label that surfaces the active record hotkey."""
        return f"Toggle recording ({self._hotkey_display(settings.record_hotkey)})"

    def _hotkey_display(self, hotkey: Hotkey) -> str:
        """Human-readable rendering of a chord -- shared by all labels."""
        return hotkey.to_display()

    # -- Qt overrides -------------------------------------------------------

    def resizeEvent(self, event: QEvent) -> None:
        """Keep the corner size grip pinned as the window resizes."""
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._reposition_size_grip()

    def changeEvent(self, event: QEvent) -> None:
        """Track maximise state so the title bar shows the right glyph."""
        if event.type() == QEvent.Type.WindowStateChange:
            title_bar = getattr(self, "_title_bar", None)
            if title_bar is not None:
                title_bar.set_maximised(self.isMaximized())
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Intercept window close to minimise into the tray.

        When the user clicks the title-bar close button we treat it as
        "hide", so the capture engine keeps recording in the background. The
        tray's Quit action sets :attr:`_force_quit` to override that.
        """
        if self._force_quit or self._tray is None:
            super().closeEvent(event)
            return

        # First time we minimise to tray we drop a balloon so the user knows
        # the app is still alive -- it is otherwise easy to think you closed
        # it. We use ``property("_tray_notice_shown")`` so the flag survives
        # a window restore without persisting it to settings.
        already_shown = bool(self.property("_tray_notice_shown") or False)
        if not already_shown:
            self._show_tray_message(
                "S-Clip is still running",
                "Use the tray icon to bring the window back or quit.",
            )
            self.setProperty("_tray_notice_shown", True)

        event.ignore()
        self.hide()


# -- lazy page imports -----------------------------------------------------
# Lazy because parallel agents may land the page modules after this file is
# committed. We catch the ImportError at the call site and substitute a
# placeholder so the bootstrap survives the gap.


def _import_capture_page() -> type[CapturePage]:
    from sclip.ui.pages import CapturePage

    return CapturePage


def _import_library_page() -> QWidget:
    from sclip.ui.pages import LibraryPage

    return LibraryPage()


def _import_settings_page() -> type[SettingsPage]:
    from sclip.ui.pages import SettingsPage

    return SettingsPage


def _import_about_page() -> QWidget:
    from sclip.ui.pages import AboutPage

    return AboutPage()


# Silence the unused-import linter for the helper QMessageBox; it is only
# referenced via runtime calls but importing it at module scope keeps the
# import locality with the rest of the Qt widgets.
_ = QMessageBox


__all__ = ["MainWindow"]
