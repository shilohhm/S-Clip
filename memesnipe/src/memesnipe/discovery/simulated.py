"""A synthetic resolver, for running the whole loop with no network at all.

This is **not** a backtest. It invents price paths from a seed; it tells you
nothing about whether your signal sources are profitable. What it does test -
and what is otherwise very hard to test - is whether the exit rules behave the
way you think when a token 10xes in ninety seconds, chops sideways, bleeds out,
or has its liquidity pulled from under you.

Archetypes are drawn per mint from a deterministic seed, so a replay run is
reproducible and a change to the exit config can be compared against the same
market conditions rather than a fresh set of random ones.
"""

from __future__ import annotations

import hashlib
import math

from ..models import Candidate, Chain, Market, now_ms

ARCHETYPES = ("runner", "chop", "bleed", "rug", "moonshot")

# How often each archetype is drawn. Weighted towards the outcomes that
# actually dominate this market: most launches bleed out or rug.
_WEIGHTS = (("rug", 35), ("bleed", 30), ("chop", 20), ("runner", 12), ("moonshot", 3))


class SimulatedResolver:
    def __init__(self, seed: str = "memesnipe", chain: Chain = Chain.SOLANA) -> None:
        self._seed = seed
        self._chain = chain
        self._born: dict[str, int] = {}

    def _hash(self, mint: str, salt: str = "") -> float:
        digest = hashlib.sha256(f"{self._seed}:{salt}:{mint}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def archetype(self, mint: str) -> str:
        roll = self._hash(mint, "archetype") * sum(weight for _, weight in _WEIGHTS)
        cumulative = 0.0
        for name, weight in _WEIGHTS:
            cumulative += weight
            if roll < cumulative:
                return name
        return "bleed"

    def _path(self, mint: str, elapsed_seconds: float) -> tuple[float, float]:
        """(price multiple, liquidity multiple) at `elapsed_seconds` after entry."""
        kind = self.archetype(mint)
        jitter = 1.0 + (self._hash(mint, f"j{int(elapsed_seconds / 5)}") - 0.5) * 0.18
        minutes = elapsed_seconds / 60.0
        if kind == "moonshot":
            return (1.0 + 25.0 * (1 - math.exp(-minutes / 3.0))) * jitter, 1.0 + minutes * 0.4
        if kind == "runner":
            peak = 4.5
            # Up fast, then give most of it back - the shape that punishes
            # holding for the top and rewards a ladder.
            value = 1.0 + peak * math.sin(min(math.pi, minutes / 6.0 * math.pi))
            return max(0.15, value) * jitter, max(0.3, 1.0 + minutes * 0.15 - minutes**2 * 0.01)
        if kind == "chop":
            return (1.0 + 0.25 * math.sin(minutes * 2.2)) * jitter, 1.0
        if kind == "rug":
            pull_at = 1.0 + self._hash(mint, "pull") * 6.0
            if minutes < pull_at:
                return (1.0 + 0.6 * minutes / pull_at) * jitter, 1.0 + minutes * 0.2
            # Liquidity gone, price follows. The engine's job is to be out
            # inside the poll interval that follows.
            return 0.02, 0.03
        return max(0.05, 1.0 - 0.10 * minutes) * jitter, max(0.1, 1.0 - 0.08 * minutes)

    def _market(self, mint: str, symbol: str) -> Market:
        born = self._born.setdefault(mint, now_ms())
        elapsed = (now_ms() - born) / 1000.0
        price_multiple, liquidity_multiple = self._path(mint, elapsed)
        base_price = 0.000001 * (1 + self._hash(mint, "price") * 40)
        base_liquidity = 12_000 + self._hash(mint, "liq") * 90_000
        liquidity = base_liquidity * liquidity_multiple
        return Market(
            mint=mint,
            chain=self._chain,
            symbol=symbol,
            name=f"Simulated {symbol}",
            price_usd=base_price * price_multiple,
            liquidity_usd=liquidity,
            fdv_usd=liquidity * 12,
            volume_5m_usd=liquidity * (0.4 + self._hash(mint, "vol")),
            buys_5m=int(20 + self._hash(mint, "buys") * 200),
            sells_5m=int(10 + self._hash(mint, "sells") * 120),
            pair_address=f"sim-{mint[:8]}",
            dex="simulated",
            # Old enough to clear the minimum-age floor, young enough to stay
            # inside the window the strategy targets.
            pair_created_at_ms=now_ms() - int(45_000 + self._hash(mint, "age") * 120_000),
            extra={"simulated": True, "archetype": self.archetype(mint)},
        )

    async def resolve(self, candidate: Candidate) -> Market | None:
        mint = candidate.mint or f"sim{candidate.symbol}"
        symbol = candidate.symbol or mint[:6].upper()
        return self._market(mint, symbol)

    async def snapshot(self, mint: str) -> Market | None:
        if mint not in self._born:
            return None
        symbol = mint[:6].upper()
        return self._market(mint, symbol)

    async def aclose(self) -> None:
        return None
