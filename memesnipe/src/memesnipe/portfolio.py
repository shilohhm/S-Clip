"""Open positions and the exit rules that manage them.

Exits, not entries, decide whether this strategy makes money. The entry filter
only chooses which lottery tickets to buy; these rules decide whether a 6x is
booked at 6x or ridden back to zero, and whether a rug costs a position or an
account.

Rules, in the order they are checked:

1. **Liquidity pulled** - the pool has lost more than the configured fraction
   of the liquidity it had at entry. Sell everything immediately, at any price.
   This is the rug guard, and it is checked first because it is the only one
   where a few seconds changes the outcome from "bad trade" to "zero".
2. **Hard stop** - down past the stop from entry. Sell everything.
3. **Take-profit ladder** - sell a fraction at each multiple, once per rung.
4. **Trailing stop** - once a rung has been taken, protect the peak.
5. **Max hold** - time out. A memecoin that has not moved in fifteen minutes is
   not going to, and the capital is better used on the next signal.
"""

from __future__ import annotations

import logging

from .config import ExitConfig
from .models import ExitReason, Fill, Market, Position, Side, now_ms

log = logging.getLogger(__name__)


class ExitPlan:
    """A decision to sell part or all of a position."""

    __slots__ = ("fraction", "reason")

    def __init__(self, fraction: float, reason: ExitReason) -> None:
        self.fraction = min(1.0, max(0.0, fraction))
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ExitPlan({self.fraction:.2f}, {self.reason.value})"


def plan_exit(position: Position, market: Market, config: ExitConfig) -> ExitPlan | None:
    """What, if anything, to sell right now."""
    if not position.is_open or market.price_usd <= 0:
        return None

    position.peak_price_usd = max(position.peak_price_usd, market.price_usd)

    # 1. Liquidity pulled. Checked before price because a rugged pool can still
    # quote a price that no size will actually clear at.
    if position.entry_liquidity_usd > 0:
        remaining = market.liquidity_usd / position.entry_liquidity_usd
        if remaining < (1.0 - config.liquidity_drawdown_exit_pct):
            log.warning(
                "%s: liquidity at %.0f%% of entry, exiting now",
                position.symbol,
                remaining * 100,
            )
            return ExitPlan(1.0, ExitReason.LIQUIDITY_PULLED)

    multiple = position.multiple(market.price_usd)

    # 2. Hard stop.
    if multiple <= (1.0 - config.hard_stop_pct):
        return ExitPlan(1.0, ExitReason.HARD_STOP)

    # 3. Take-profit ladder. Rungs are indexed so a gap through two of them
    # takes both, in order, rather than double-selling the same one.
    for index, (target_multiple, fraction) in enumerate(config.take_profit_ladder):
        if index in position.rungs_taken:
            continue
        if multiple >= target_multiple:
            position.rungs_taken.add(index)
            return ExitPlan(fraction, ExitReason.TAKE_PROFIT)

    # 4. Trailing stop, armed only after the first rung so a position is not
    # stopped out of normal launch volatility before it has made anything.
    if position.rungs_taken and position.peak_price_usd > 0:
        drawdown = 1.0 - market.price_usd / position.peak_price_usd
        if drawdown >= config.trailing_stop_pct:
            return ExitPlan(1.0, ExitReason.TRAILING_STOP)

    # 5. Time out.
    held_seconds = (now_ms() - position.opened_at_ms) / 1000.0
    if held_seconds >= config.max_hold_seconds:
        return ExitPlan(1.0, ExitReason.MAX_HOLD)

    return None


class Portfolio:
    """Every position the run has taken, open and closed."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def __contains__(self, mint: str) -> bool:
        return mint in self._positions

    def get(self, mint: str) -> Position | None:
        return self._positions.get(mint)

    @property
    def open_positions(self) -> list[Position]:
        return [position for position in self._positions.values() if position.is_open]

    @property
    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def open(self, fill: Fill, market: Market) -> Position:
        position = self._positions.get(fill.mint)
        if position is None:
            position = Position(
                mint=fill.mint,
                symbol=fill.symbol or market.symbol,
                chain=market.chain,
                entry_liquidity_usd=market.liquidity_usd,
                opened_at_ms=fill.ts_ms,
            )
            self._positions[fill.mint] = position
        position.apply(fill)
        return position

    def apply_exit(self, position: Position, fill: Fill, reason: ExitReason) -> None:
        position.apply(fill)
        if not position.is_open:
            position.exit_reason = reason

    def equity(self, prices: dict[str, float]) -> float:
        """Realised plus marked-to-market unrealised, across everything."""
        total = 0.0
        for position in self._positions.values():
            price = prices.get(position.mint, position.entry_price_usd)
            total += position.pnl_usd(price)
        return round(total, 2)

    @staticmethod
    def sell_qty(position: Position, plan: ExitPlan) -> float:
        return position.qty if plan.fraction >= 1.0 else position.qty * plan.fraction


__all__ = ["ExitPlan", "Portfolio", "Side", "plan_exit"]
