"""Page-level screens for the S-Clip main window.

Each module in this package owns one top-level destination in the navigation
shell: capture, library, settings, and about. The main window wires them into
a stack and routes its navigation signals here, so the pages remain agnostic
to the chrome that hosts them.
"""

from __future__ import annotations

from sclip.ui.pages.about_page import AboutPage
from sclip.ui.pages.capture_page import CapturePage
from sclip.ui.pages.library_page import LibraryPage
from sclip.ui.pages.settings_page import SettingsPage

__all__ = [
    "AboutPage",
    "CapturePage",
    "LibraryPage",
    "SettingsPage",
]
