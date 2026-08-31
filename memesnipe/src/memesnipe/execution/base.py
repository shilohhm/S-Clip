"""The venue contract, shared by paper and live execution."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Fill, Market


@runtime_checkable
class Venue(Protocol):
    paper: bool

    async def buy(self, market: Market, usd: float) -> Fill | None:
        """Spend `usd` on the token. None means the order did not fill."""
        ...

    async def sell(self, market: Market, qty: float) -> Fill | None:
        """Sell `qty` tokens. None means the order did not fill - which on a
        real venue is the normal outcome for a rugged pool, and the reason the
        paper venue simulates it too."""
        ...

    async def aclose(self) -> None: ...
