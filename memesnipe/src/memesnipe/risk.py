"""Pre-trade safety checks.

Two categories, and the distinction is the point of this module:

* **Soft checks** are quality filters - liquidity depth, buy/sell balance,
  age, valuation. The `degen` profile relaxes these, because "I will take rug
  risk for the upside" is a legitimate position to hold, and pretending
  otherwise just means the thresholds get edited until they permit everything.
* **Hard checks** never relax in any profile: the blocklists, and the price
  impact of your own order. Buying a pool so thin that your own trade moves it
  hundreds of basis points is not a risk appetite, it is a guaranteed loss on
  entry followed by no exit at all.

Every check is recorded pass or fail even when an earlier one already failed,
so the journal shows *why* a signal was skipped rather than just that it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig
from .models import Market, RiskReport


@dataclass(frozen=True, slots=True)
class Thresholds:
    min_liquidity_usd: float
    max_liquidity_usd: float
    min_pair_age_seconds: float
    max_pair_age_seconds: float
    min_buys_5m: int
    require_sells: bool
    max_fdv_usd: float


def effective_thresholds(config: RiskConfig) -> Thresholds:
    """Apply the profile to the configured soft thresholds."""
    if config.profile == "degen":
        # Accept thinner, younger, quieter pools - the earliest and most
        # dangerous part of a launch. The hard checks still apply, and the
        # exit rules matter far more here than the entry filter does.
        return Thresholds(
            min_liquidity_usd=config.min_liquidity_usd * 0.25,
            max_liquidity_usd=config.max_liquidity_usd,
            min_pair_age_seconds=min(config.min_pair_age_seconds, 5.0),
            max_pair_age_seconds=config.max_pair_age_seconds * 2,
            min_buys_5m=max(1, config.min_buys_5m // 5),
            require_sells=False,
            max_fdv_usd=config.max_fdv_usd * 5,
        )
    return Thresholds(
        min_liquidity_usd=config.min_liquidity_usd,
        max_liquidity_usd=config.max_liquidity_usd,
        min_pair_age_seconds=config.min_pair_age_seconds,
        max_pair_age_seconds=config.max_pair_age_seconds,
        min_buys_5m=config.min_buys_5m,
        require_sells=config.require_sells,
        max_fdv_usd=config.max_fdv_usd,
    )


def price_impact_bps(size_usd: float, liquidity_usd: float) -> float:
    """Constant-product estimate of what your own order costs you.

    A Uniswap-style pool holds roughly half its reported liquidity in the quote
    asset, and buying `size` against reserve `R` moves the price by
    `size / (R + size)`. This is an approximation - concentrated liquidity and
    bonding curves differ - but it is the right shape, and it is pessimistic in
    the thin pools where being wrong is expensive.
    """
    if liquidity_usd <= 0:
        return 10_000.0
    quote_reserve = liquidity_usd / 2.0
    return 10_000.0 * size_usd / (quote_reserve + size_usd)


def _quality_score(market: Market, thresholds: Thresholds) -> float:
    """0..1 quality of the setup, used later for position sizing."""
    depth = min(1.0, market.liquidity_usd / max(1.0, thresholds.min_liquidity_usd * 4))
    total_txns = market.buys_5m + market.sells_5m
    # Balanced flow beats one-sided flow: all-buys is either a honeypot or a
    # top, and neither is something to size up into.
    balance = 0.5 if total_txns == 0 else 1.0 - abs(market.buys_5m - market.sells_5m) / total_txns
    momentum = min(1.0, market.volume_5m_usd / max(1.0, market.liquidity_usd))
    age = market.age_seconds
    if age <= 0:
        freshness = 0.0
    else:
        span = max(1.0, thresholds.max_pair_age_seconds)
        freshness = max(0.0, 1.0 - age / span)
    return round(0.35 * depth + 0.20 * balance + 0.20 * momentum + 0.25 * freshness, 4)


def assess(
    market: Market | None,
    config: RiskConfig,
    *,
    size_usd: float,
    max_price_impact_bps: float | None = None,
) -> RiskReport:
    if market is None:
        return RiskReport.rejected("no market found")

    thresholds = effective_thresholds(config)
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    def check(name: str, ok: bool, reason: str) -> None:
        checks[name] = ok
        if not ok:
            reasons.append(reason)

    # -- hard checks, identical in every profile --------------------------
    check(
        "not_blocked",
        market.mint not in set(config.blocked_mints),
        f"{market.mint} is on the mint blocklist",
    )
    deployer = str(market.extra.get("deployer", ""))
    check(
        "deployer_not_blocked",
        not deployer or deployer not in set(config.blocked_deployers),
        f"deployer {deployer} is blocklisted",
    )
    impact = price_impact_bps(size_usd, market.liquidity_usd)
    impact_limit = (
        max_price_impact_bps if max_price_impact_bps is not None else config.max_price_impact_bps
    )
    check(
        "price_impact",
        impact <= impact_limit,
        f"own-order impact {impact:.0f}bps over the {impact_limit:.0f}bps limit",
    )
    check("has_price", market.price_usd > 0, "no usable price")

    # -- soft checks, relaxed by the degen profile ------------------------
    check(
        "min_liquidity",
        market.liquidity_usd >= thresholds.min_liquidity_usd,
        f"liquidity ${market.liquidity_usd:,.0f} under ${thresholds.min_liquidity_usd:,.0f}",
    )
    check(
        "max_liquidity",
        market.liquidity_usd <= thresholds.max_liquidity_usd,
        f"liquidity ${market.liquidity_usd:,.0f} over ${thresholds.max_liquidity_usd:,.0f}",
    )
    # Age 0 means Dexscreener gave us no creation time. Treated as a failure:
    # an unknown-age pool is not evidence of a fresh one.
    age_known = market.pair_created_at_ms > 0
    check("age_known", age_known, "pair creation time unknown")
    check(
        "min_age",
        age_known and market.age_seconds >= thresholds.min_pair_age_seconds,
        f"pair is {market.age_seconds:.0f}s old, under the "
        f"{thresholds.min_pair_age_seconds:.0f}s floor",
    )
    check(
        "max_age",
        age_known and market.age_seconds <= thresholds.max_pair_age_seconds,
        f"pair is {market.age_seconds:.0f}s old, past the "
        f"{thresholds.max_pair_age_seconds:.0f}s window",
    )
    check(
        "min_buys",
        market.buys_5m >= thresholds.min_buys_5m,
        f"{market.buys_5m} buys in 5m, under {thresholds.min_buys_5m}",
    )
    check(
        "has_sells",
        not thresholds.require_sells or market.sells_5m > 0,
        "no sells in 5m - possible honeypot",
    )
    check(
        "max_fdv",
        market.fdv_usd <= thresholds.max_fdv_usd or market.fdv_usd == 0,
        f"FDV ${market.fdv_usd:,.0f} over ${thresholds.max_fdv_usd:,.0f}",
    )

    passed = all(checks.values())
    return RiskReport(
        passed=passed,
        score=_quality_score(market, thresholds) if passed else 0.0,
        checks=checks,
        reasons=reasons,
    )
