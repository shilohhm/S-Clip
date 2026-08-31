from __future__ import annotations

from conftest import make_market

from memesnipe.config import ExecutionConfig
from memesnipe.execution.paper import PaperVenue
from memesnipe.models import Side


def config(**overrides: object) -> ExecutionConfig:
    base = ExecutionConfig()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


async def test_buy_fills_above_the_quoted_price():
    venue = PaperVenue(config())
    market = make_market(price_usd=0.001)
    fill = await venue.buy(market, 50.0)
    assert fill is not None
    # Latency drift, competition and depth all push the fill against you. A
    # paper broker that fills at the quote is the thing this exists to avoid.
    assert fill.price_usd > market.price_usd
    assert fill.slippage_bps > 0
    assert fill.fee_usd > 0
    assert fill.qty * fill.price_usd < fill.usd


async def test_buy_is_rejected_when_slippage_exceeds_the_cap():
    # A real swap with this cap reverts rather than filling.
    venue = PaperVenue(config(slippage_bps=50.0))
    assert await venue.buy(make_market(), 50.0) is None


async def test_thin_pools_cost_more_than_deep_ones():
    venue = PaperVenue(config(slippage_bps=100_000.0))
    deep = await venue.buy(make_market(liquidity_usd=500_000.0), 100.0)
    thin = await venue.buy(make_market(liquidity_usd=5_000.0), 100.0)
    assert deep is not None and thin is not None
    assert thin.slippage_bps > deep.slippage_bps


async def test_sell_fills_below_the_quoted_price():
    venue = PaperVenue(config())
    market = make_market(price_usd=0.002)
    fill = await venue.sell(market, 10_000)
    assert fill is not None
    assert fill.side is Side.SELL
    assert fill.price_usd < market.price_usd


async def test_sell_does_not_fill_when_liquidity_is_gone():
    # The most important simulated failure there is: paper trading that always
    # lets you exit hides the entire risk of the strategy.
    venue = PaperVenue(config())
    assert await venue.sell(make_market(liquidity_usd=0.0), 10_000) is None


async def test_higher_latency_produces_a_worse_entry():
    fast = PaperVenue(config(paper_latency_ms=200, slippage_bps=100_000.0))
    slow = PaperVenue(config(paper_latency_ms=5_000, slippage_bps=100_000.0))
    quick = await fast.buy(make_market(), 50.0)
    late = await slow.buy(make_market(), 50.0)
    assert quick is not None and late is not None
    assert late.price_usd > quick.price_usd
