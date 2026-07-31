"""Tests for the global chord listener in :mod:`sclip.hotkeys`.

These drive :class:`HotkeyListener` by calling the ``on_press``/``on_release``
callbacks pynput would deliver, rather than by starting the listener. That
exercises the real registration, modifier-tracking and dispatch logic without
installing a system-wide keyboard hook on whichever machine runs the suite —
which matters, because a test that grabbed F5 globally would fight the
developer's own keyboard.

The two tests that do call :meth:`HotkeyListener.start` replace
``pynput.keyboard.Listener`` first, for the same reason.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pynput import keyboard
from pynput.keyboard import Key, KeyCode

from sclip.contracts import Hotkey
from sclip.hotkeys import HotkeyListener, _normalise_key_name


@pytest.fixture()
def listener() -> HotkeyListener:
    """A listener with its key map built, but no global hook installed.

    ``start`` is what installs the hook; the key map is normally built inside
    it, so we build it directly to get the same lookup behaviour safely.
    """
    instance = HotkeyListener()
    instance._key_map = instance._build_key_map(keyboard)
    return instance


class _FakeListener:
    """Stand-in for ``pynput.keyboard.Listener`` that never touches the OS."""

    instances: ClassVar[list[_FakeListener]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.daemon = False
        self.started = False
        self.stopped = False
        _FakeListener.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


# ------------------------------------------------------------------ helpers


def _press(listener: HotkeyListener, *keys: object) -> None:
    for key in keys:
        listener._on_press(key)  # type: ignore[arg-type]


def _release(listener: HotkeyListener, *keys: object) -> None:
    for key in keys:
        listener._on_release(key)  # type: ignore[arg-type]


# ------------------------------------------------------------- name handling


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("f5", "F5"), ("  F5  ", "F5"), ("F5", "F5"), ("esc", "ESC")],
)
def test_normalise_key_name_trims_and_uppercases(raw: str, expected: str) -> None:
    assert _normalise_key_name(raw) == expected


def test_registration_is_case_insensitive(listener: HotkeyListener) -> None:
    """A chord stored as ``f5`` must still match a real F5 press."""
    fired: list[str] = []
    listener.register(Hotkey(key="f5"), lambda: fired.append("hit"))

    _press(listener, Key.f5)

    assert fired == ["hit"]


# ---------------------------------------------------------------- dispatch


def test_press_fires_the_registered_callback(listener: HotkeyListener) -> None:
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))

    _press(listener, Key.f5)

    assert fired == ["clip"]


def test_a_bare_key_does_not_fire_a_modified_binding(listener: HotkeyListener) -> None:
    """Ctrl+F6 must not trigger on F6 alone, or the chord would be pointless."""
    fired: list[str] = []
    listener.register(Hotkey(key="F6", ctrl=True), lambda: fired.append("record"))

    _press(listener, Key.f6)

    assert fired == []


def test_a_modified_key_does_not_fire_a_bare_binding(listener: HotkeyListener) -> None:
    """The reverse guard: F5 alone should not fire while Ctrl is held."""
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))

    _press(listener, Key.ctrl, Key.f5)

    assert fired == []


def test_chord_fires_once_its_modifier_is_held(listener: HotkeyListener) -> None:
    fired: list[str] = []
    listener.register(Hotkey(key="F6", ctrl=True), lambda: fired.append("record"))

    _press(listener, Key.ctrl, Key.f6)

    assert fired == ["record"]


def test_releasing_a_modifier_clears_it(listener: HotkeyListener) -> None:
    """A stale modifier would silently break every bare binding afterwards."""
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))

    _press(listener, Key.ctrl)
    _release(listener, Key.ctrl)
    _press(listener, Key.f5)

    assert fired == ["clip"]


@pytest.mark.parametrize("ctrl_variant", [Key.ctrl, Key.ctrl_l, Key.ctrl_r])
def test_left_and_right_control_are_equivalent(
    listener: HotkeyListener,
    ctrl_variant: Key,
) -> None:
    """Windows reports the two Ctrl keys separately; users do not care which."""
    fired: list[str] = []
    listener.register(Hotkey(key="F6", ctrl=True), lambda: fired.append("record"))

    _press(listener, ctrl_variant, Key.f6)

    assert fired == ["record"]


def test_alt_gr_counts_as_alt(listener: HotkeyListener) -> None:
    """Non-US layouts send AltGr; it should satisfy an Alt binding."""
    alt_gr = getattr(Key, "alt_gr", None)
    if alt_gr is None:  # pragma: no cover - platform without AltGr
        pytest.skip("this platform has no alt_gr key")
    fired: list[str] = []
    listener.register(Hotkey(key="F7", alt=True), lambda: fired.append("alt"))

    _press(listener, alt_gr, Key.f7)

    assert fired == ["alt"]


def test_printable_keys_resolve_to_their_uppercase_character(
    listener: HotkeyListener,
) -> None:
    fired: list[str] = []
    listener.register(Hotkey(key="A", ctrl=True), lambda: fired.append("a"))

    _press(listener, Key.ctrl, KeyCode.from_char("a"))

    assert fired == ["a"]


def test_unknown_keys_are_ignored(listener: HotkeyListener) -> None:
    """A press we cannot name must not raise, just do nothing."""
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))

    _press(listener, None, KeyCode.from_char(" "))

    assert fired == []


def test_a_raising_callback_does_not_kill_the_listener(listener: HotkeyListener) -> None:
    """A buggy handler must not take the listener thread down with it."""
    fired: list[str] = []

    def explode() -> None:
        raise RuntimeError("handler is broken")

    listener.register(Hotkey(key="F5"), explode)
    listener.register(Hotkey(key="F6"), lambda: fired.append("still alive"))

    _press(listener, Key.f5)  # must not propagate
    _press(listener, Key.f6)

    assert fired == ["still alive"]


# ------------------------------------------------------------- registration


def test_re_registering_a_chord_replaces_the_callback(listener: HotkeyListener) -> None:
    """Saving settings re-registers; the old callback must not also fire."""
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("first"))
    listener.register(Hotkey(key="F5"), lambda: fired.append("second"))

    _press(listener, Key.f5)

    assert fired == ["second"]


def test_unregister_removes_the_binding(listener: HotkeyListener) -> None:
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))
    listener.unregister(Hotkey(key="F5"))

    _press(listener, Key.f5)

    assert fired == []


def test_unregister_is_idempotent(listener: HotkeyListener) -> None:
    """Callers tidy up speculatively, so an unknown chord must not raise."""
    listener.unregister(Hotkey(key="F9"))
    listener.unregister(Hotkey(key="F9"))


def test_unregister_matches_regardless_of_case(listener: HotkeyListener) -> None:
    fired: list[str] = []
    listener.register(Hotkey(key="F5"), lambda: fired.append("clip"))
    listener.unregister(Hotkey(key="f5"))

    _press(listener, Key.f5)

    assert fired == []


# -------------------------------------------------------------- key mapping


def test_build_key_map_covers_the_function_keys(listener: HotkeyListener) -> None:
    assert listener._key_map["F5"] is Key.f5
    assert listener._key_map["F12"] is Key.f12


def test_build_key_map_aliases_named_keys(listener: HotkeyListener) -> None:
    """``ENTER`` and ``RETURN`` should both reach the same pynput key."""
    assert listener._key_map["ENTER"] is listener._key_map["RETURN"]
    assert listener._key_map["ESC"] is listener._key_map["ESCAPE"]


# ------------------------------------------------------------- lifecycle


def test_start_installs_a_listener_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeListener.instances.clear()
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)
    instance = HotkeyListener()

    instance.start()
    instance.start()  # idempotent: must not spawn a second hook

    assert len(_FakeListener.instances) == 1
    assert _FakeListener.instances[0].started is True
    assert _FakeListener.instances[0].daemon is True

    instance.stop()
    assert _FakeListener.instances[0].stopped is True


def test_start_survives_a_backend_that_refuses_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine without a usable input backend must still get a working app.

    Global hotkeys are a convenience; the GUI controls do the same jobs. A
    listener that cannot start therefore logs and stands down rather than
    taking the bootstrap with it.
    """

    def refuse(**_kwargs: Any) -> None:
        raise OSError("no input backend on this display")

    monkeypatch.setattr(keyboard, "Listener", refuse)
    instance = HotkeyListener()

    instance.start()  # must not raise

    assert instance._listener is None
    assert instance._started is False


def test_stop_without_start_is_safe() -> None:
    HotkeyListener().stop()


def test_stop_survives_a_listener_that_refuses_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must complete even if the backend misbehaves on the way out."""

    class _StubbornListener(_FakeListener):
        def stop(self) -> None:
            raise OSError("backend wedged")

    monkeypatch.setattr(keyboard, "Listener", _StubbornListener)
    instance = HotkeyListener()
    instance.start()

    instance.stop()  # must not raise

    assert instance._listener is None
    assert instance._started is False


def test_releasing_a_non_modifier_is_ignored(listener: HotkeyListener) -> None:
    """Only modifiers are tracked; releasing anything else is a no-op."""
    listener._on_press(Key.ctrl)

    listener._on_release(Key.f5)

    assert listener._pressed_modifiers == {"ctrl"}


def test_named_keys_outside_the_map_fall_back_to_their_pynput_name(
    listener: HotkeyListener,
) -> None:
    """Power users can bind keys we do not ship in the default map.

    ``_build_key_map`` only lists the keys worth offering as defaults, so
    anything else resolves through the key's own pynput name. That is what
    makes an unusual binding from settings.json work.
    """
    assert "CAPS_LOCK" not in listener._key_map
    fired: list[str] = []
    listener.register(Hotkey(key="CAPS_LOCK"), lambda: fired.append("caps"))

    _press(listener, Key.caps_lock)

    assert fired == ["caps"]


def test_stop_clears_held_modifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modifiers held across a restart would corrupt the next chord match."""
    _FakeListener.instances.clear()
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)
    instance = HotkeyListener()
    instance.start()

    instance._on_press(Key.ctrl)
    assert instance._pressed_modifiers == {"ctrl"}

    instance.stop()

    assert instance._pressed_modifiers == set()
