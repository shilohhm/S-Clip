"""Application entry point -- wires the subsystems together and runs the loop.

This module is intentionally the only place that knows about *everything*:
it constructs the settings store, the device registry, the capture engine,
the hotkey listener and the main window, then hands control to the Qt event
loop. Every other module accepts its collaborators through constructor
arguments so it can be tested without spinning up the whole stack.

A small single-instance guard sits between the bootstrap and the rest of the
app. Launching S-Clip while another copy is already running raises the
running window (via a tiny local-socket handshake) and exits cleanly with
code 0, rather than starting a competing capture engine.
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QSharedMemory, Qt
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from sclip.hotkeys import HotkeyListener
from sclip.logging_config import configure_logging
from sclip.paths import app_paths

if TYPE_CHECKING:
    from collections.abc import Callable

    from sclip.contracts import (
        AudioDevice,
        CaptureEngine,
        CaptureState,
        DeviceRegistry,
        Monitor,
        Settings,
        SettingsStore,
    )
    from sclip.ui.main_window import MainWindow


logger = logging.getLogger(__name__)


# -- single-instance protocol ----------------------------------------------
# Two pieces of state cooperate: a :class:`QSharedMemory` segment claims the
# "we are running" flag, and a :class:`QLocalServer` listens on a matching
# name so a second launch can ask the first to raise its window. Using both
# avoids a race where the shared memory survives a crash but the server name
# is dead -- :meth:`QSharedMemory.attach` tells us about the former, the
# local socket connect tests the latter.

_SINGLETON_KEY: str = "sclip-singleton"
_SINGLETON_SOCKET_NAME: str = "sclip-singleton-socket"
_RAISE_MESSAGE: bytes = b"raise\n"
_SINGLETON_BYPASS_FLAGS: frozenset[str] = frozenset(
    {"--no-single-instance", "--dev-no-singleton"}
)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the QApplication exit code.

    Single-instance lock: if another S-Clip is already running, raise its
    window via QSharedMemory + a tiny QLocalServer/QLocalSocket handshake
    and exit cleanly with code 0.
    """
    args = list(argv) if argv is not None else sys.argv
    bypass_singleton = _consume_flag(args, _SINGLETON_BYPASS_FLAGS)

    # Logging must come first so any failure below produces useful diagnostics.
    configure_logging(logging.INFO)
    logger.info("Starting S-Clip")

    try:
        app_paths().ensure_writable()
    except OSError as exc:
        # If we cannot even write to AppData something is seriously wrong;
        # there is no point trying to keep going.
        logger.critical("Cannot prepare writable paths: %s", exc)
        _show_fatal_message_box("Cannot prepare app data directories. See the log for details.")
        return 1

    # The single-instance check needs a :class:`QCoreApplication` to install
    # the local server, but the proper :class:`QApplication` should not be
    # constructed twice. We therefore set the high-DPI policy attribute
    # *before* creating QApplication, then run the singleton check using the
    # same QApplication that the main window will use.
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    qt_app = QApplication(args)
    qt_app.setApplicationName("S-Clip")
    qt_app.setOrganizationName("S-Clip")
    qt_app.setQuitOnLastWindowClosed(True)

    # Apply the application icon as early as possible so even an early modal
    # (eg. a fatal-error box) carries the brand.
    icon_path = app_paths().assets_dir / "icon.png"
    if icon_path.is_file():
        qt_app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("Application icon not found at %s", icon_path)

    shared_memory = QSharedMemory(_SINGLETON_KEY)
    if not bypass_singleton and _another_instance_is_running(shared_memory):
        logger.info("Another S-Clip instance is already running; raising and exiting")
        _ping_running_instance()
        # ``shared_memory`` is detached automatically by Qt when it goes out
        # of scope; we do not need to do anything special.
        return 0

    try:
        return _run(qt_app, install_singleton_server=not bypass_singleton)
    except Exception as exc:
        # Any uncaught exception at this stage means we never even reached
        # the event loop -- show the user something useful before dying.
        logger.critical("Unhandled exception during bootstrap: %s", exc, exc_info=True)
        details = traceback.format_exc()
        _show_fatal_message_box(
            "S-Clip failed to start.\n\nSee the log file for details.",
            details=details,
        )
        return 1
    finally:
        # Make sure the shared-memory claim is released even if we explode --
        # otherwise a crash leaves a stale lock and the next launch wrongly
        # thinks an instance is still running.
        if shared_memory.isAttached():
            shared_memory.detach()


def _run(qt_app: QApplication, *, install_singleton_server: bool = True) -> int:
    """Construct everything and enter the Qt event loop.

    Split out from :func:`main` so the outer ``try/except`` reliably catches
    construction failures regardless of where they occur.
    """
    # Lazy imports so the protocol-only modules import cheaply and so missing
    # parallel-agent files surface here, not at the top of the bootstrap.
    from sclip.contracts import CaptureEngine, DeviceRegistry, SettingsStore  # noqa: F401

    settings_store = _build_settings_store()
    device_registry = _build_device_registry()
    _apply_first_run_recommendation(settings_store, device_registry)
    hotkey_listener = HotkeyListener()
    engine = _build_capture_engine(settings_store, device_registry)

    # The main window is the last thing constructed because it consumes all
    # of the above.
    from sclip.ui.main_window import MainWindow

    window = MainWindow(engine, settings_store, device_registry, hotkey_listener)

    # Wire the single-instance "raise" listener now that we have a window
    # to raise. The local server lives for the life of the process.
    local_server = _install_singleton_server(window) if install_singleton_server else None

    # Shutdown order: stop accepting hotkey events first (so nothing fires
    # into a half-torn-down engine), then shut the engine down. The shared
    # memory is detached by the ``finally`` block in :func:`main`.
    def _on_about_to_quit() -> None:
        logger.info("Shutting down")
        try:
            hotkey_listener.stop()
        except Exception:
            logger.exception("Hotkey listener failed to stop cleanly")
        try:
            engine.shutdown()
        except Exception:
            logger.exception("Capture engine failed to shut down cleanly")
        if local_server is not None:
            try:
                local_server.close()
            except Exception:
                logger.debug("Local server close raised", exc_info=True)

    qt_app.aboutToQuit.connect(_on_about_to_quit)

    window.show()
    hotkey_listener.start()

    return qt_app.exec()


# -- subsystem constructors ------------------------------------------------
# Each builder degrades to a no-op stub if its concrete module is missing,
# so a half-built tree (where parallel agents have not yet landed every
# file) still starts. The stub logs loudly so the gap is obvious.


def _consume_flag(args: list[str], flags: frozenset[str]) -> bool:
    """Remove any matching flag from argv and report whether one was present."""
    found = False
    kept: list[str] = []
    for arg in args:
        if arg in flags:
            found = True
        else:
            kept.append(arg)
    if found:
        args[:] = kept
    return found


def _apply_first_run_recommendation(
    settings_store: SettingsStore,
    device_registry: DeviceRegistry,
) -> None:
    """On the very first launch, tune the settings to the detected hardware.

    Presence of the settings file is the "has run before" signal. When it is
    absent we probe the machine and persist a recommended configuration, so
    the user opens straight into something sensible. Any failure here is
    non-fatal — the app simply falls back to the built-in defaults.
    """
    if app_paths().settings_file.exists():
        return
    logger.info("First launch detected; auto-configuring for this hardware")
    try:
        from sclip.contracts import Settings
        from sclip.core.hardware import recommend_settings

        recommended = recommend_settings(Settings(), device_registry)
        settings_store.save(recommended)
    except Exception:
        logger.exception("First-run hardware auto-configuration failed; using defaults")


def _build_settings_store() -> SettingsStore:
    """Construct the JSON-backed settings store, or a transient fallback."""
    try:
        from sclip.core.settings import JsonSettingsStore

        return JsonSettingsStore()
    except Exception:
        logger.exception("Could not build JsonSettingsStore; using in-memory stub")
        return _InMemorySettingsStore()


def _build_device_registry() -> DeviceRegistry:
    """Construct the OS device registry, or an empty fallback."""
    try:
        from sclip.core.devices import SystemDeviceRegistry

        return SystemDeviceRegistry()
    except Exception:
        logger.exception("Could not build SystemDeviceRegistry; using empty stub")
        return _EmptyDeviceRegistry()


def _build_capture_engine(
    settings_store: SettingsStore,
    device_registry: DeviceRegistry,
) -> CaptureEngine:
    """Construct the FFmpeg capture engine, or a disabled fallback.

    A missing FFmpeg binary is the single most likely reason for failure
    here -- the stub returned in that case lets the UI start so the user can
    open Settings and read the diagnostic, rather than crashing on launch.
    """
    try:
        from sclip.core.capture import FFmpegCaptureEngine

        return FFmpegCaptureEngine(settings_store, device_registry)
    except Exception:
        logger.exception("Could not build FFmpegCaptureEngine; capture features disabled")
        return _DisabledCaptureEngine()


# -- single-instance helpers ----------------------------------------------


def _another_instance_is_running(shared_memory: QSharedMemory) -> bool:
    """Return True if a previous S-Clip already holds the singleton segment.

    We try to ``create`` the segment; success means we are the first one.
    Failing with :data:`QSharedMemory.AlreadyExists` confirms another live
    instance. Any other failure (rare) is treated as "no lock" so we do not
    refuse to start because the OS gave us an unexpected error.
    """
    if shared_memory.create(1):
        return False

    error = shared_memory.error()
    if error == QSharedMemory.SharedMemoryError.AlreadyExists:
        return True

    logger.warning("Shared memory create returned %s -- assuming first instance", error)
    return False


def _ping_running_instance() -> None:
    """Tell the existing instance to raise its window, if it is listening.

    Best-effort: a stale socket or a slow handshake means the user simply
    gets nothing visible, which is no worse than the legacy behaviour. The
    log captures whichever path we took.
    """
    socket = QLocalSocket()
    socket.connectToServer(_SINGLETON_SOCKET_NAME)
    # The waitForConnected/write/waitForBytesWritten pattern is the standard
    # synchronous shape recommended by the Qt docs for a one-shot client.
    if not socket.waitForConnected(500):
        logger.warning("Could not reach existing S-Clip instance via local socket")
        return
    socket.write(QByteArray(_RAISE_MESSAGE))
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()


def _install_singleton_server(window: MainWindow) -> QLocalServer:
    """Start listening for "raise" messages from later S-Clip launches.

    We unconditionally remove any stale server before listening because the
    name may survive a previous process crash on Windows.
    """
    QLocalServer.removeServer(_SINGLETON_SOCKET_NAME)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

    def _on_new_connection() -> None:
        # We deliberately do not read the payload -- any connection at all
        # counts as a "please come to the front" request. Reading and
        # discarding still keeps the socket tidy.
        connection = server.nextPendingConnection()
        if connection is None:
            return
        try:
            connection.waitForReadyRead(500)
            _ = connection.readAll()
        finally:
            connection.disconnectFromServer()
            connection.deleteLater()
        try:
            window.raise_from_other_instance()
        except Exception:
            logger.exception("Failed to raise window after singleton ping")

    server.newConnection.connect(_on_new_connection)
    if not server.listen(_SINGLETON_SOCKET_NAME):
        logger.warning(
            "Singleton local server could not listen on %s: %s",
            _SINGLETON_SOCKET_NAME,
            server.errorString(),
        )
    return server


# -- fallback stubs --------------------------------------------------------
# These exist only so the app boots when a parallel agent has not yet landed
# the real implementation. They satisfy the :class:`SettingsStore`,
# :class:`DeviceRegistry` and :class:`CaptureEngine` protocols just enough
# for the GUI to come up and tell the user something has gone wrong.


class _InMemorySettingsStore:
    """Volatile settings store -- discards changes when the app exits."""

    def __init__(self) -> None:
        from sclip.contracts import Settings

        self._settings = Settings()

    def load(self) -> Settings:

        return self._settings.copy()

    def save(self, settings: Settings) -> None:
        self._settings = settings.copy()


class _EmptyDeviceRegistry:
    """Device registry that reports nothing -- used when enumeration fails."""

    def monitors(self) -> list[Monitor]:
        return []

    def audio_devices(self) -> list[AudioDevice]:
        return []


class _DisabledCaptureEngine:
    """Engine stub that reports an error state and refuses to record.

    All methods are no-ops; this keeps the GUI navigable when FFmpeg is
    missing or the capture engine module has not landed yet. The listener
    registration methods accept callbacks and quietly drop them — this stub
    never emits an event, so there is nothing for a listener to observe.
    """

    def __init__(self) -> None:
        from sclip.contracts import CaptureState

        self.state = CaptureState.ERROR

    def start_manual_recording(self) -> None:
        logger.error("Capture engine unavailable; cannot start recording")

    def stop_manual_recording(self) -> Path | None:
        return None

    def start_replay_buffer(self) -> None:
        logger.error("Capture engine unavailable; cannot start replay buffer")

    def stop_replay_buffer(self) -> None:
        pass

    def save_replay_clip(self) -> Path | None:
        logger.error("Capture engine unavailable; cannot save replay clip")
        return None

    def shutdown(self) -> None:
        pass

    def add_state_listener(self, listener: Callable[[CaptureState], None]) -> None:
        pass

    def add_clip_listener(self, listener: Callable[[Path], None]) -> None:
        pass

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        pass


def _show_fatal_message_box(message: str, *, details: str | None = None) -> None:
    """Display a critical error to the user when the app cannot start.

    A best-effort affair: if even creating the message box fails (because
    QApplication itself has not initialised) we fall back to stderr so the
    operator still sees something.
    """
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox(app.activeWindow() if hasattr(app, "activeWindow") else None)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("S-Clip")
        box.setText(message)
        if details:
            box.setDetailedText(details)
        box.exec()
    except Exception:
        # Last-ditch resort -- the GUI is unusable so we print to stderr.
        sys.stderr.write(f"S-Clip fatal error: {message}\n")
        if details:
            sys.stderr.write(details + "\n")


__all__ = ["main"]
