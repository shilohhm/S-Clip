"""Global keyboard chord listener.

The legacy listener only understood bare F-keys; this module replaces it with
something modifier-aware so the user can bind, for example, ``Ctrl+F6`` to
toggle recording without colliding with their game's bindings.

The :class:`HotkeyListener` runs a :mod:`pynput` listener on its own daemon
thread. Because pynput delivers events from that thread, the callbacks we
invoke must not touch Qt widgets directly -- the :class:`~sclip.ui.main_window.MainWindow`
adapter is the one that bridges back to the GUI thread via signals.

On platforms where pynput cannot start a listener (notably a Linux box without
an X server) we log a warning and carry on without global hotkeys rather than
killing the whole application -- everything else is still useful.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sclip.contracts import Hotkey

if TYPE_CHECKING:
    from pynput.keyboard import Key, KeyCode

logger = logging.getLogger(__name__)


# -- pynput key name mapping ------------------------------------------------
# Built lazily inside the listener so importing this module on a system
# without a usable input subsystem still succeeds. ``_FUNCTION_KEY_NAMES`` is
# pure data, safe to define at module scope.
_FUNCTION_KEY_NAMES: tuple[str, ...] = tuple(f"F{i}" for i in range(1, 25))


def _normalise_key_name(value: str) -> str:
    """Canonicalise a user-supplied key name so comparisons are reliable.

    Hotkey definitions can arrive from JSON settings or from code; some users
    type ``f5``, some ``F5``, some ``F 5``. We trim whitespace and uppercase
    a single token so the look-up succeeds regardless.
    """
    return value.strip().upper()


class HotkeyListener:
    """Listens for global keyboard chords and dispatches callbacks.

    Modifier-aware: Ctrl/Shift/Alt + a base key. Runs the pynput listener in
    its own daemon thread; callbacks are invoked from that thread, so the
    main-window adapter wraps them with Qt signals for thread safety.
    """

    def __init__(self) -> None:
        # Each registration is a normalised :class:`Hotkey` mapped to the
        # callback. We re-create the lookup key on every event so changing
        # modifiers do not desynchronise from the registered table.
        self._registry: dict[Hotkey, Callable[[], None]] = {}
        # Mutating ``_pressed_modifiers`` from the listener thread alongside
        # ``register``/``unregister`` from the GUI thread would race; a lock
        # keeps the two safe without forcing the GUI to wait on key events.
        self._lock = threading.RLock()
        self._pressed_modifiers: set[str] = set()
        # ``_listener`` is created lazily so a misbehaving pynput backend does
        # not blow up at import time -- ``start`` is where we accept the risk.
        self._listener: Any | None = None
        self._key_map: dict[str, object] = {}
        self._started = False

    # -- public API ---------------------------------------------------------

    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        """Bind ``callback`` to fire whenever ``hotkey`` is pressed globally.

        Re-registering the same chord overwrites the previous callback; we
        deliberately do not raise because the typical caller is reacting to a
        settings save and would otherwise need extra bookkeeping.
        """
        normalised = self._normalise(hotkey)
        with self._lock:
            self._registry[normalised] = callback
        logger.debug("Registered hotkey %s", normalised.to_display())

    def unregister(self, hotkey: Hotkey) -> None:
        """Remove the binding for ``hotkey`` if one exists.

        Unknown hotkeys are silently ignored -- making this idempotent keeps
        the calling code (which may be tidying up speculatively) simple.
        """
        normalised = self._normalise(hotkey)
        with self._lock:
            if self._registry.pop(normalised, None) is not None:
                logger.debug("Unregistered hotkey %s", normalised.to_display())

    def start(self) -> None:
        """Begin listening on a background daemon thread.

        Safe to call repeatedly -- subsequent calls are no-ops so the app
        bootstrap does not need to track whether the listener is already up.
        Failure to spin up the listener is logged but never raised; the
        application is still useful with only the GUI controls.
        """
        if self._started:
            return

        try:
            # pynput is imported lazily so a missing or broken backend (no X
            # server on Linux, missing accessibility permission on macOS) does
            # not turn into an ImportError at process start.
            from pynput import keyboard as _kb
        except ImportError as exc:
            logger.warning("pynput unavailable -- global hotkeys disabled: %s", exc)
            return

        try:
            self._key_map = self._build_key_map(_kb)
            self._listener = _kb.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            # ``daemon`` so a stuck listener cannot prevent process shutdown.
            self._listener.daemon = True
            self._listener.start()
            self._started = True
            logger.info("Global hotkey listener started")
        except Exception:
            # pynput raises a variety of platform-specific errors here; we do
            # not want to crash the app because the user is on an unusual
            # setup -- log fully and move on.
            logger.exception("Could not start global hotkey listener; continuing without it")
            self._listener = None
            self._started = False

    def stop(self) -> None:
        """Tear the listener down. Safe to call when it never started."""
        listener = self._listener
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            logger.exception("Failed to stop hotkey listener cleanly")
        finally:
            self._listener = None
            self._started = False
            with self._lock:
                self._pressed_modifiers.clear()
            logger.info("Global hotkey listener stopped")

    # -- internals ----------------------------------------------------------

    def _normalise(self, hotkey: Hotkey) -> Hotkey:
        """Return a copy of ``hotkey`` with a canonical key name.

        Storing the registry with uppercased keys lets us compare against the
        names produced from live key events without per-lookup string work.
        """
        return Hotkey(
            key=_normalise_key_name(hotkey.key),
            ctrl=hotkey.ctrl,
            shift=hotkey.shift,
            alt=hotkey.alt,
        )

    def _build_key_map(self, kb_module: object) -> dict[str, object]:
        """Build the name to ``pynput.keyboard.Key`` lookup table.

        Done at start time rather than module scope so importing this module
        on a host without pynput's native backend still succeeds.
        """
        Key = kb_module.Key  # type: ignore[attr-defined]
        mapping: dict[str, object] = {}
        # Function keys cover the bulk of our default bindings; the loop keeps
        # us honest if pynput ever changes how it spells them.
        for name in _FUNCTION_KEY_NAMES:
            attr = name.lower()
            if hasattr(Key, attr):
                mapping[name] = getattr(Key, attr)
        # A handful of named keys are useful for chord defaults even though we
        # do not ship them out of the box -- listing them keeps custom user
        # bindings working when they come from settings.json.
        for name, attr in (
            ("SPACE", "space"),
            ("ENTER", "enter"),
            ("RETURN", "enter"),
            ("ESC", "esc"),
            ("ESCAPE", "esc"),
            ("TAB", "tab"),
            ("BACKSPACE", "backspace"),
            ("DELETE", "delete"),
            ("HOME", "home"),
            ("END", "end"),
            ("PAGEUP", "page_up"),
            ("PAGEDOWN", "page_down"),
            ("INSERT", "insert"),
            ("UP", "up"),
            ("DOWN", "down"),
            ("LEFT", "left"),
            ("RIGHT", "right"),
        ):
            if hasattr(Key, attr):
                mapping[name] = getattr(Key, attr)
        return mapping

    def _on_press(self, key: Key | KeyCode | None) -> None:
        """Listener callback for any key press.

        Modifier keys update the pressed-set and return early. Non-modifier
        keys assemble a :class:`Hotkey` from the current modifier state and
        consult the registry; if there is a hit we fire the bound callback.
        Exceptions inside callbacks are caught so a buggy handler cannot
        silently kill the listener thread.
        """
        modifier = self._modifier_name(key)
        if modifier is not None:
            with self._lock:
                self._pressed_modifiers.add(modifier)
            return

        key_name = self._resolve_key_name(key)
        if key_name is None:
            return

        with self._lock:
            chord = Hotkey(
                key=key_name,
                ctrl="ctrl" in self._pressed_modifiers,
                shift="shift" in self._pressed_modifiers,
                alt="alt" in self._pressed_modifiers,
            )
            callback = self._registry.get(chord)

        if callback is None:
            return

        logger.debug("Dispatching hotkey %s", chord.to_display())
        try:
            callback()
        except Exception:
            logger.exception("Hotkey callback for %s raised", chord.to_display())

    def _on_release(self, key: Key | KeyCode | None) -> None:
        """Listener callback for key releases -- only used to track modifiers."""
        modifier = self._modifier_name(key)
        if modifier is None:
            return
        with self._lock:
            self._pressed_modifiers.discard(modifier)

    def _modifier_name(self, key: Key | KeyCode | None) -> str | None:
        """Classify ``key`` as a modifier (returning canonical name) or ``None``.

        We treat left/right modifier variants as the same logical key because
        users rarely care which Ctrl they pressed, and pynput exposes them as
        separate enum members on Windows. ``alt_gr`` collapses into ``alt`` so
        non-US layouts do not need bespoke handling.
        """
        if key is None:
            return None
        try:
            from pynput.keyboard import Key as _Key
        except ImportError:
            return None
        if key in (_Key.ctrl, _Key.ctrl_l, _Key.ctrl_r):
            return "ctrl"
        if key in (_Key.shift, _Key.shift_l, _Key.shift_r):
            return "shift"
        if key in (_Key.alt, _Key.alt_l, _Key.alt_r) or key == getattr(_Key, "alt_gr", None):
            return "alt"
        return None

    def _resolve_key_name(self, key: Key | KeyCode | None) -> str | None:
        """Return the canonical name we use in :class:`Hotkey` for ``key``.

        Returns ``None`` for keys we do not care to match against -- printable
        characters arrive as :class:`pynput.keyboard.KeyCode`, special keys
        arrive as :class:`pynput.keyboard.Key`. Anything we cannot match maps
        to ``None`` so the press is simply ignored.
        """
        if key is None:
            return None

        try:
            from pynput.keyboard import Key as _Key
            from pynput.keyboard import KeyCode as _KeyCode
        except ImportError:
            return None

        if isinstance(key, _KeyCode):
            char = getattr(key, "char", None)
            stripped = char.strip() if char is not None else ""
            return stripped.upper() if stripped else None

        if isinstance(key, _Key):
            for map_name, candidate in self._key_map.items():
                if candidate == key:
                    return map_name
            # Some named keys (eg. ``Key.media_play_pause``) are valid pynput
            # values but not in our default map -- expose them by their
            # underscore name so power users can still bind them via settings.
            key_attr = getattr(key, "name", None)
            if isinstance(key_attr, str):
                return key_attr.upper()

        return None


__all__ = ["HotkeyListener"]
