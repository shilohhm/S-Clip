"""Design tokens for the S-Clip interface.

The :class:`Theme` dataclass is the single source of truth for every colour,
spacing step, radius and type-ramp value the UI consumes. Both Python widgets
(for paint-time work like the recording dot) and the QSS file pull from this
module, so a palette change only ever needs editing in one place.

The QSS file uses Python's ``%(name)s`` placeholder syntax for tokens. Calling
:func:`load_stylesheet` reads the file from disk and applies the substitution
using the live :data:`THEME` instance, which means a future "theme picker"
feature could swap the dataclass at runtime and reskin the whole app without a
line of QSS being touched by hand.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# -- Spacing scale ---------------------------------------------------------
# Exposed as module-level constants because layouts use them as raw integers,
# not as QSS tokens. They are mirrored onto the Theme dataclass below so the
# stylesheet can reach the same numbers.
SPACING_XXS: int = 4
SPACING_XS: int = 8
SPACING_SM: int = 12
SPACING_MD: int = 16
SPACING_LG: int = 24
SPACING_XL: int = 32
SPACING_XXL: int = 48


# -- Corner radius scale ---------------------------------------------------
RADIUS_SM: int = 8      # buttons, inputs, small chips
RADIUS_MD: int = 12     # nested panels, popups
RADIUS_LG: int = 16     # cards
RADIUS_PILL: int = 999  # full pill (status indicator) -- any large value works


@dataclass(frozen=True, slots=True)
class TypeRamp:
    """One step on the type ramp.

    Bundling size, weight and an optional family override into a single object
    keeps QSS substitutions tidy: ``%(display_size)s`` and ``%(display_weight)s``
    rather than dozens of loose variables.
    """

    size_px: int
    weight: int
    family: str = "'Inter', 'Segoe UI Variable', 'Segoe UI', system-ui, sans-serif"


@dataclass(frozen=True, slots=True)
class Theme:
    """Every design token the UI needs, in one immutable bundle.

    The dataclass is frozen because the UI assumes tokens never change once
    the application has started painting; a theme swap should build a fresh
    Theme and replace :data:`THEME`, never mutate the existing instance.

    The palette is a quiet, near-black dark scheme. Surfaces climb a gentle
    elevation ladder — ``bg`` for the window, ``surface`` for cards, ``inset``
    for recessed wells like input fields — so panels read as distinct planes
    rather than one flat sheet of grey. Violet is the single accent, used
    sparingly: a focus ring here, one primary button there, never a wall of
    colour.
    """

    # -- Surfaces, low to high ------------------------------------------
    bg: str = "#0B0D12"               # window background, deepest plane
    bg_alt: str = "#0D0F15"           # sidebar — a hair lifted off the window
    surface: str = "#15181F"          # cards
    surface_elevated: str = "#1B1F29"  # hovered cards, popups, raised segments
    inset: str = "#0A0B10"            # recessed wells: inputs, thumbnails
    border: str = "#23262F"           # subtle 1px outlines
    border_strong: str = "#343845"    # outlines that need to be noticed

    # -- Text, brightest to dimmest -------------------------------------
    text_primary: str = "#ECEDF0"
    text_secondary: str = "#969BA8"
    text_muted: str = "#5C6170"       # timestamps, hints, the quietest text
    text_disabled: str = "#454957"

    # -- Accent / status palette ----------------------------------------
    accent_primary: str = "#7C5CFF"    # electric violet -- CTAs, focus rings
    accent_primary_hover: str = "#9079FF"
    accent_primary_pressed: str = "#6A4DEB"
    accent_text: str = "#AD9CFF"       # violet legible as text on a dark surface
    accent_secondary: str = "#36D2E6"  # cyan -- info, the buffering state

    success: str = "#41D17F"
    warning: str = "#F5A623"
    danger: str = "#FF4D5A"            # recording dot, the Stop button
    danger_hover: str = "#FF6670"
    danger_pressed: str = "#E63B48"

    # -- Type ramp ------------------------------------------------------
    # Default factories are used because a frozen dataclass cannot assign to
    # its own fields in __post_init__; the ramp objects are built once at
    # construction and never mutated thereafter.
    display: TypeRamp = field(default_factory=lambda: TypeRamp(size_px=24, weight=700))
    title: TypeRamp = field(default_factory=lambda: TypeRamp(size_px=15, weight=600))
    body: TypeRamp = field(default_factory=lambda: TypeRamp(size_px=14, weight=400))
    label: TypeRamp = field(default_factory=lambda: TypeRamp(size_px=13, weight=500))
    caption: TypeRamp = field(default_factory=lambda: TypeRamp(size_px=12, weight=400))
    mono: TypeRamp = field(
        default_factory=lambda: TypeRamp(
            size_px=13,
            weight=600,
            # Cascadia Mono ships with modern Windows; Consolas is the
            # universal fallback on older releases.
            family="'Cascadia Mono', 'Consolas', 'Courier New', monospace",
        )
    )

    # -- Spacing / radius (mirrored so QSS can substitute them) ---------
    space_xxs: int = SPACING_XXS
    space_xs: int = SPACING_XS
    space_sm: int = SPACING_SM
    space_md: int = SPACING_MD
    space_lg: int = SPACING_LG
    space_xl: int = SPACING_XL
    space_xxl: int = SPACING_XXL

    radius_sm: int = RADIUS_SM
    radius_md: int = RADIUS_MD
    radius_lg: int = RADIUS_LG
    radius_pill: int = RADIUS_PILL


# The live singleton. Replace this attribute (in tests or a future theme
# picker) to reskin the app -- everything that needs a token reads from here.
THEME: Theme = Theme()


def _flatten_for_qss(theme: Theme) -> dict[str, str | int]:
    """Turn a :class:`Theme` into a flat dict suitable for ``qss % mapping``.

    ``asdict`` returns nested dicts for the :class:`TypeRamp` fields, which is
    no use for ``%(display_size)s`` style substitutions. Each ramp is exploded
    into ``<name>_size``, ``<name>_weight`` and ``<name>_family`` keys so the
    QSS can reference them flatly.
    """
    raw = asdict(theme)
    flat: dict[str, str | int] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and {"size_px", "weight", "family"} <= value.keys():
            flat[f"{key}_size"] = value["size_px"]
            flat[f"{key}_weight"] = value["weight"]
            flat[f"{key}_family"] = value["family"]
        else:
            flat[key] = value
    return flat


def _stylesheet_path() -> Path:
    """Resolve the QSS file shipped alongside this module.

    Deliberately avoids importing :mod:`sclip.paths`: the theme system needs
    none of the platformdirs machinery, and co-locating the QSS with this file
    means a packaged wheel keeps the two in sync automatically.
    """
    return Path(__file__).resolve().parent / "styles.qss"


def load_stylesheet(theme: Theme | None = None) -> str:
    """Return the application stylesheet with theme tokens substituted in.

    The QSS source uses ``%(token_name)s`` placeholders. We read it, perform
    the substitution against the supplied theme (defaulting to the live
    :data:`THEME` singleton), and hand the result back to the caller, who
    passes it straight to ``QApplication.setStyleSheet``.

    Any I/O or substitution failure is logged with full context and re-raised:
    a missing or malformed stylesheet is a packaging fault, not something the
    app should paper over with an empty string.
    """
    active = theme if theme is not None else THEME
    qss_path = _stylesheet_path()
    try:
        template = qss_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read stylesheet at %s: %s", qss_path, exc)
        raise

    mapping = _flatten_for_qss(active)
    # The QSS references a couple of small SVG assets (the dropdown chevrons)
    # by absolute path. Inject the assets directory as a posix-style string so
    # ``url(...)`` resolves regardless of the process's working directory.
    mapping["assets"] = (qss_path.parent / "assets").as_posix()
    try:
        return template % mapping
    except KeyError as exc:
        logger.error("Stylesheet references unknown theme token: %s", exc)
        raise
    except (TypeError, ValueError) as exc:
        logger.error("Stylesheet substitution failed: %s", exc)
        raise


__all__ = [
    "RADIUS_LG",
    "RADIUS_MD",
    "RADIUS_PILL",
    "RADIUS_SM",
    "SPACING_LG",
    "SPACING_MD",
    "SPACING_SM",
    "SPACING_XL",
    "SPACING_XS",
    "SPACING_XXL",
    "SPACING_XXS",
    "THEME",
    "Theme",
    "TypeRamp",
    "load_stylesheet",
]
