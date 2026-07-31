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
RADIUS_SM: int = 8  # buttons, inputs, small chips
RADIUS_MD: int = 12  # nested panels, popups
RADIUS_LG: int = 16  # cards
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
    family: str = "'Figtree', 'Segoe UI Variable', 'Segoe UI', sans-serif"


@dataclass(frozen=True, slots=True)
class Theme:
    """Every design token the UI needs, in one immutable bundle.

    The dataclass is frozen because the UI assumes tokens never change once
    the application has started painting; a theme swap should build a fresh
    Theme and replace :data:`THEME`, never mutate the existing instance.

    The palette is a competition-grade graphite scheme. Surfaces climb a
    restrained elevation ladder while a single high-visibility signal colour
    marks focus, buffering, and primary actions. Recording red remains
    semantic rather than decorative.
    """

    # -- Surfaces, low to high ------------------------------------------
    bg: str = "#0B0D0E"  # window background, deepest plane
    bg_alt: str = "#101315"  # sidebar - a hair lifted off the window
    surface: str = "#161A1D"  # cards and instrument stage
    surface_elevated: str = "#1D2226"  # hovered cards, popups, raised segments
    inset: str = "#080A0B"  # recessed wells: inputs, thumbnails
    border: str = "#292F33"  # subtle 1px outlines
    border_strong: str = "#3D454B"  # outlines that need to be noticed
    border_hover: str = "#515C63"

    # -- Text, brightest to dimmest -------------------------------------
    text_primary: str = "#F1F4F2"
    text_secondary: str = "#A3ACA7"
    text_muted: str = "#6D7771"  # timestamps, hints, the quietest text
    text_disabled: str = "#4E5651"

    # -- Accent / status palette ----------------------------------------
    accent_primary: str = "#D8F45B"  # signal lime -- CTAs, focus, buffering
    accent_primary_hover: str = "#E4FA82"
    accent_primary_pressed: str = "#B8D13F"
    accent_text: str = "#E3F98A"
    accent_secondary: str = "#D8F45B"
    accent_wash: str = "rgba(216, 244, 91, 0.10)"
    accent_wash_strong: str = "rgba(216, 244, 91, 0.18)"

    success: str = "#73DC8C"
    warning: str = "#F0B85C"
    danger: str = "#FF5D66"  # recording dot, the Stop button
    danger_hover: str = "#FF7A81"
    danger_pressed: str = "#DC434D"

    # -- Interaction surfaces / foregrounds -----------------------------
    surface_hover: str = "#242A2E"
    surface_pressed: str = "#121619"
    neutral_wash: str = "rgba(241, 244, 242, 0.05)"
    text_on_accent: str = "#111510"
    text_on_danger: str = "#FFF4F3"

    # -- Type ramp ------------------------------------------------------
    # Default factories are used because a frozen dataclass cannot assign to
    # its own fields in __post_init__; the ramp objects are built once at
    # construction and never mutated thereafter.
    display: TypeRamp = field(
        default_factory=lambda: TypeRamp(
            size_px=26,
            weight=600,
            family="'Unbounded', 'Figtree', 'Segoe UI', sans-serif",
        )
    )
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
