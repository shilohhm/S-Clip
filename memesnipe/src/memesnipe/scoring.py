"""Conviction and position sizing.

Sizing is where a signal bot either survives a bad streak or does not. The rule
here: conviction scales the base size, never the account. A 3x-conviction trade
is three base units, not three times whatever is left, so a losing run shrinks
in absolute terms instead of compounding into the floor.
"""

from __future__ import annotations

from .config import SizingConfig
from .models import Candidate, Market, RiskReport

# How much to trust each kind of token reference. A launchpad link names the
# mint; a bare ticker is a guess that scammers actively poison.
_KIND_WEIGHT = {
    "pumpfun_link": 1.0,
    "dexscreener_link": 0.95,
    "birdeye_link": 0.95,
    "mint_address": 0.9,
    "contract_address": 0.9,
    "ticker": 0.4,
    "unknown": 0.2,
}


def conviction(candidate: Candidate, market: Market | None, risk: RiskReport) -> float:
    """0..1 confidence that this specific trade is worth taking."""
    if market is None or not risk.passed:
        return 0.0
    reference = _KIND_WEIGHT.get(candidate.kind, 0.3)
    # Author weight is the operator's own prior on the account. It is allowed
    # to boost above 1.0, but the product is clamped so no single dial can
    # push a weak setup to full size on its own.
    author = min(2.0, max(0.0, candidate.signal.author_weight))
    freshness = 1.0
    lag = candidate.signal.feed_lag_ms
    if lag > 2_000:
        # Every second of staleness is a second of other bots buying ahead of
        # you, so late signals get smaller, not merely accepted.
        freshness = max(0.2, 1.0 - (lag - 2_000) / 10_000.0)
    return round(min(1.0, reference * risk.score * freshness * (author / 1.0) * 1.0), 4)


def size_position(conviction_score: float, config: SizingConfig) -> float:
    """Dollar size for a trade at this conviction."""
    if conviction_score <= 0:
        return 0.0
    multiple = 1.0 + conviction_score * (config.max_conviction_multiple - 1.0)
    return round(min(config.base_size_usd * multiple, config.max_size_usd), 2)
