"""Reusable presentational widgets for S-Clip.

Each widget in this package is a pure Qt component -- it knows about colour,
layout and signals, but nothing about the capture engine or settings store.
Pages compose these primitives into screens; tests can instantiate them in
isolation with no engine running.

The re-exports below let pages import widgets with ``from sclip.ui.widgets
import Card`` rather than having to remember each module path.
"""

from __future__ import annotations

from sclip.ui.widgets.card import Card
from sclip.ui.widgets.hotkey_edit import HotkeyEdit
from sclip.ui.widgets.icon_button import IconButton
from sclip.ui.widgets.record_orb import RecordOrb
from sclip.ui.widgets.segmented_control import SegmentedControl
from sclip.ui.widgets.sidebar import Sidebar, SidebarItem
from sclip.ui.widgets.status_pill import StatusPill
from sclip.ui.widgets.title_bar import TITLE_BAR_HEIGHT, TitleBar

__all__ = [
    "TITLE_BAR_HEIGHT",
    "Card",
    "HotkeyEdit",
    "IconButton",
    "RecordOrb",
    "SegmentedControl",
    "Sidebar",
    "SidebarItem",
    "StatusPill",
    "TitleBar",
]
