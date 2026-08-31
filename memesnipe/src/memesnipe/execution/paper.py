"""Simulated fills.

The point of this module is to *not* flatter the strategy. A paper broker that
fills at the quoted price will show a profitable curve for almost any signal
rule, and the first live week then deletes the account. So every cost that
actually shows up in a real memecoin snipe is modelled here:

1. **Latency drift.** Between the post and your transaction landing, the price
   moves - and on a signal everyone else is also reading, it moves against you.
2. **Competition.** Other bots raise the clearing price ahead of you.
3. **Your own price impact.** A constant-product pool charges you for depth you
   consume, in both directions.
4. **Fees.** Swap fee plus the fixed on-chain cost of the transaction.
5. **Failure to sell.** If liquidity is gone, the sell does not fill. Paper
   trading that always lets you exit is the single most misleading thing you
   can build here, because exiting is the hard half.

Defaults are pessimistic on purpose. Calibrate them from your own live fills;
if you find yourself tuning them down to make the equity curve look better, the
curve is the thing that is wrong.
"""

from __future__ import annotations

import logging

from ..config import ExecutionConfig
from ..models import Fill, Market, Side, now_ms
from ..risk import price_impact_bps

log = logging.getLogger(__name__)


class PaperVenue:
    paper = True

    def __init__(self, config: ExecutionConfig) -> None:
        self._config = config
        self.fills: list[Fill] = []

    def _drift_bps(self) -> float:
        """Adverse price movement over the execution delay, plus competition."""
        intervals = self._config.paper_latency_ms / 100.0
        return intervals * self._config.paper_price_drift_bps_per_100ms + (
            self._config.paper_competition_bps
        )

    async def buy(self, market: Market, usd: float) -> Fill | None:
        if usd <= 0 or market.price_usd <= 0:
            return None
        drift = self._drift_bps()
        impact = price_impact_bps(usd, market.liquidity_usd)
        slippage = drift + impact
        if slippage > self._config.slippage_bps:
            # A real swap with a slippage cap set this way reverts instead of
            # filling, so the paper venue declines too. Rejected entries are
            # part of the strategy's return profile, not an error.
            log.info(
                "paper buy rejected on %s: %.0fbps slippage over the %.0fbps cap",
                market.symbol,
                slippage,
                self._config.slippage_bps,
            )
            return None
        fill_price = market.price_usd * (1.0 + slippage / 10_000.0)
        fee = usd * self._config.paper_swap_fee_bps / 10_000.0 + self._config.paper_fixed_cost_usd
        qty = (usd - fee) / fill_price
        fill = Fill(
            mint=market.mint,
            symbol=market.symbol,
            side=Side.BUY,
            qty=qty,
            price_usd=fill_price,
            usd=usd,
            fee_usd=fee,
            slippage_bps=slippage,
            ts_ms=now_ms() + self._config.paper_latency_ms,
            tx_ref=f"paper-buy-{market.mint[:8]}-{now_ms()}",
            paper=True,
        )
        self.fills.append(fill)
        return fill

    async def sell(self, market: Market, qty: float) -> Fill | None:
        if qty <= 0 or market.price_usd <= 0:
            return None
        notional = qty * market.price_usd
        if market.liquidity_usd <= 0:
            log.warning("paper sell on %s failed: no liquidity left", market.symbol)
            return None
        # Selling pays the same depth cost as buying, in the opposite
        # direction, and an exit is usually larger relative to what is left in
        # the pool than the entry was.
        impact = price_impact_bps(notional, market.liquidity_usd)
        slippage = impact + self._config.paper_competition_bps
        fill_price = market.price_usd * max(0.0, 1.0 - slippage / 10_000.0)
        gross = qty * fill_price
        fee = gross * self._config.paper_swap_fee_bps / 10_000.0 + self._config.paper_fixed_cost_usd
        fill = Fill(
            mint=market.mint,
            symbol=market.symbol,
            side=Side.SELL,
            qty=qty,
            price_usd=fill_price,
            usd=max(0.0, gross - fee),
            fee_usd=fee,
            slippage_bps=slippage,
            ts_ms=now_ms() + self._config.paper_latency_ms,
            tx_ref=f"paper-sell-{market.mint[:8]}-{now_ms()}",
            paper=True,
        )
        self.fills.append(fill)
        return fill

    async def aclose(self) -> None:
        return None
