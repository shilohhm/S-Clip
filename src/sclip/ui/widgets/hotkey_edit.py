"""Keyboard-chord capture field.

Renders the current hotkey as text inside a read-only :class:`QLineEdit`, but
hijacks the key-press pipeline so that focusing the field puts it into
"capture mode": the next chord the user types becomes the new hotkey and is
emitted as a :class:`sclip.contracts.Hotkey`. Pressing Escape cancels the
capture and restores the previous chord, mirroring the convention games and
power-user tools have settled on.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QLineEdit, QWidget

from sclip.contracts import Hotkey

logger = logging.getLogger(__name__)


# Qt keys that act purely as modifiers and should not, on their own, finish
# a capture. We wait for a "real" key before emitting the new hotkey.
_MODIFIER_KEYS: frozenset[int] = frozenset(
    {
        Qt.Key.Key_Control,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Meta,
        Qt.Key.Key_AltGr,
    }
)


# Keys that abort or commit capture without producing a hotkey output.
_CANCEL_KEY: int = int(Qt.Key.Key_Escape)


def _key_to_name(key: int) -> str | None:
    """Convert a Qt key code to the textual representation used by ``Hotkey``.

    Returns ``None`` for keys that do not make sense as the body of a hotkey
    (modifier keys, dead keys, navigation keys we don't want to bind by
    accident). The caller treats ``None`` as "ignore this press".
    """
    qt_key = Qt.Key(key) if not isinstance(key, Qt.Key) else key

    # Function keys F1..F35 -- the F-key family covers what most users want
    # for global shortcuts and avoids conflicts with regular typing.
    if Qt.Key.Key_F1 <= qt_key <= Qt.Key.Key_F35:
        offset = int(qt_key) - int(Qt.Key.Key_F1) + 1
        return f"F{offset}"

    # Letters and digits map directly to their character representation.
    if Qt.Key.Key_A <= qt_key <= Qt.Key.Key_Z:
        return chr(int(qt_key))
    if Qt.Key.Key_0 <= qt_key <= Qt.Key.Key_9:
        return chr(int(qt_key))

    # A handful of named keys that users sometimes want to bind. Anything
    # not listed here is rejected so we don't accidentally bind Caps Lock
    # or Print Screen on the first focus.
    named: dict[Qt.Key, str] = {
        Qt.Key.Key_Space: "Space",
        Qt.Key.Key_Tab: "Tab",
        Qt.Key.Key_Return: "Enter",
        Qt.Key.Key_Enter: "Enter",
        Qt.Key.Key_Backspace: "Backspace",
        Qt.Key.Key_Delete: "Delete",
        Qt.Key.Key_Insert: "Insert",
        Qt.Key.Key_Home: "Home",
        Qt.Key.Key_End: "End",
        Qt.Key.Key_PageUp: "PageUp",
        Qt.Key.Key_PageDown: "PageDown",
    }
    return named.get(qt_key)


class HotkeyEdit(QLineEdit):
    """Read-only line edit that captures keyboard chords."""

    # Emitted whenever the user finishes a successful capture. The widget
    # also updates its displayed text before emitting, so listeners can
    # treat the signal as "the visible hotkey has changed".
    hotkey_changed = Signal(Hotkey)

    def __init__(self, hotkey: Hotkey | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HotkeyEdit")
        self.setReadOnly(True)
        # We do *not* set Qt.NoFocus here -- focus is what tells the widget
        # to start listening for a new chord. Tab and click-to-focus both
        # work as the user would expect from a regular text field.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPlaceholderText("Click and press a chord")
        # Disabling context menu prevents the "Cut/Copy/Paste" menu from
        # popping up on right-click -- those make no sense for a hotkey.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self._hotkey: Hotkey | None = hotkey
        self._capturing: bool = False
        self._render()

    # -- Public API ---------------------------------------------------------

    def hotkey(self) -> Hotkey | None:
        """Return the most recently committed hotkey, if any."""
        return self._hotkey

    def set_hotkey(self, hotkey: Hotkey | None) -> None:
        """Programmatically replace the displayed hotkey.

        Does *not* emit :attr:`hotkey_changed` -- programmatic updates
        usually originate from the same code path that listens for the
        signal, and re-emitting would cause unnecessary work or loops.
        """
        self._hotkey = hotkey
        self._render()

    def is_capturing(self) -> bool:
        """``True`` while the widget is waiting for a fresh chord."""
        return self._capturing

    # -- Qt event hooks -----------------------------------------------------

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        # Entering the field arms it: the first non-modifier key becomes
        # the new hotkey. Without this, callers would have to expose a
        # separate "edit" button which would be tedious for a power user.
        self._set_capturing(True)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        # Leaving the field without finishing a chord commits whatever the
        # field had before -- we don't surprise the user by clearing a
        # previously saved hotkey just because they clicked elsewhere.
        self._set_capturing(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Only intercept keys while we're actively capturing; otherwise
        # the line edit's read-only behaviour does the right thing.
        if not self._capturing:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == _CANCEL_KEY:
            # Escape aborts -- restore the previous hotkey and drop focus.
            self._set_capturing(False)
            self._render()
            self.clearFocus()
            event.accept()
            return

        if key in _MODIFIER_KEYS:
            # A modifier on its own can't form a complete chord. We swallow
            # the event so the line edit doesn't try to interpret it, then
            # wait for the next press.
            event.accept()
            return

        key_name = _key_to_name(key)
        if key_name is None:
            # Unsupported key -- log at debug because this is a normal user
            # interaction, not a programming error.
            logger.debug("HotkeyEdit ignored unsupported key code %r.", key)
            event.accept()
            return

        modifiers = event.modifiers()
        new_hotkey = Hotkey(
            key=key_name,
            ctrl=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
            shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
            alt=bool(modifiers & Qt.KeyboardModifier.AltModifier),
        )

        # The spec says: "pressing a regular key without any modifier
        # accepts F-keys as-is." Restrict bare presses to F-keys because
        # binding "A" to a global shortcut would consume every "A" typed
        # in any application -- almost certainly not what the user wants.
        is_function_key = key_name.startswith("F") and key_name[1:].isdigit()
        has_modifier = new_hotkey.ctrl or new_hotkey.shift or new_hotkey.alt
        if not has_modifier and not is_function_key:
            logger.debug(
                "HotkeyEdit rejected bare %r press: bare chords require an F-key.",
                key_name,
            )
            event.accept()
            return

        self._hotkey = new_hotkey
        self._set_capturing(False)
        self._render()
        self.hotkey_changed.emit(new_hotkey)
        # Clearing focus visually confirms the capture has ended and lets
        # the user navigate on with Tab.
        self.clearFocus()
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        # Release events are not meaningful for chord capture but we swallow
        # them while capturing to keep the line edit from acting on them.
        if self._capturing:
            event.accept()
            return
        super().keyReleaseEvent(event)

    # -- Internal helpers ---------------------------------------------------

    def _set_capturing(self, value: bool) -> None:
        if self._capturing == value:
            return
        self._capturing = value
        # Dynamic property drives the focus-coloured border in QSS.
        self.setProperty("capturing", "true" if value else "false")
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self._render()

    def _render(self) -> None:
        if self._capturing:
            self.setText("Press a chord...")
            return
        if self._hotkey is None:
            self.setText("")
            return
        self.setText(self._hotkey.to_display())


__all__ = ["HotkeyEdit"]
