"""Turning a token reference into a tradeable market."""

from .base import Resolver
from .simulated import SimulatedResolver

__all__ = ["Resolver", "SimulatedResolver", "build_resolver"]


def build_resolver(config, *, simulated: bool = False):  # type: ignore[no-untyped-def]
    """Pick a resolver. Imported lazily so an offline replay run needs no
    HTTP client installed at all."""
    if simulated:
        return SimulatedResolver(chain=config.discovery.chain)
    from .dexscreener import DexscreenerResolver

    return DexscreenerResolver(config.discovery)
