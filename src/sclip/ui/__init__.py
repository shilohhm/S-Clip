"""Presentation layer for S-Clip.

This package owns the visual identity of the application: design tokens, the
Qt stylesheet, and the small library of reusable widgets that the pages stitch
together into screens. Nothing in here imports from :mod:`sclip.core` -- the
widgets are deliberately framework-only so they can be unit-tested without a
running capture engine.
"""

from __future__ import annotations
