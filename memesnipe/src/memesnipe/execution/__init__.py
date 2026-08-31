"""Execution venues. Paper and live share one interface on purpose."""

from .base import Venue
from .paper import PaperVenue

__all__ = ["PaperVenue", "Venue", "build_venue"]


def build_venue(config):  # type: ignore[no-untyped-def]
    """Pick a venue from `config.execution.mode`.

    `Config.require_arm()` must already have passed before this is called with
    a live mode; it is the single gate on spending real money.
    """
    if config.execution.mode == "paper":
        return PaperVenue(config.execution)
    if config.execution.mode == "live":
        if config.execution.venue != "jupiter":
            raise ValueError(f"unknown live venue: {config.execution.venue!r}")
        from .jupiter import JupiterVenue

        return JupiterVenue(config.execution)
    raise ValueError(f"unknown execution.mode: {config.execution.mode!r}")
