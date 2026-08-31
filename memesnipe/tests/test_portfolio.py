from __future__ import annotations

from conftest import make_market

from memesnipe.config import ExitConfig
from memesnipe.models import ExitReason, Fill, Position, Side, now_ms
from memesnipe.portfolio import Portfolio, plan_exit


def buy_fill(price: float = 0.001, usd: float = 100.0) -> Fill:
    return Fill(
        mint="M",
        symbol="TEST",
        side=Side.BUY,
        qty=usd / price,
        price_usd=price,
        usd=usd,
        fee_usd=1.0,
        slippage_bps=100,
        ts_ms=now_ms(),
        tx_ref="t",
        paper=True,
    )


def opened(entry_liquidity: float = 40_000.0) -> Position:
    portfolio = Portfolio()
    position = portfolio.open(buy_fill(), make_market(liquidity_usd=entry_liquidity))
    return position


def test_partial_sell_keeps_the_remaining_cost_basis():
    position = opened()
    half = position.qty / 2
    position.apply(
        Fill(
            mint="M",
            symbol="TEST",
            side=Side.SELL,
            qty=half,
            price_usd=0.002,
            usd=half * 0.002,
            fee_usd=0.5,
            slippage_bps=50,
            ts_ms=now_ms(),
            tx_ref="s",
            paper=True,
        )
    )
    assert position.is_open
    assert position.qty == half
    # Half the position sold at 2x: half the basis realised, half still held.
    assert round(position.realized_usd, 2) == 50.0
    assert round(position.cost_usd, 2) == 50.0


def test_liquidity_pull_wins_over_every_other_rule():
    position = opened(entry_liquidity=40_000.0)
    # Price is up 5x, which would normally take a profit rung - but the pool
    # has been drained, so the only correct action is out, all of it.
    market = make_market(price_usd=0.005, liquidity_usd=2_000.0)
    plan = plan_exit(position, market, ExitConfig())
    assert plan is not None
    assert plan.reason is ExitReason.LIQUIDITY_PULLED
    assert plan.fraction == 1.0


def test_hard_stop_sells_everything():
    position = opened()
    plan = plan_exit(position, make_market(price_usd=0.0004), ExitConfig())
    assert plan is not None
    assert plan.reason is ExitReason.HARD_STOP
    assert plan.fraction == 1.0


def test_ladder_rungs_fire_once_each_and_in_order():
    position = opened()
    config = ExitConfig()

    first = plan_exit(position, make_market(price_usd=0.002), config)
    assert first is not None and first.reason is ExitReason.TAKE_PROFIT
    # Same price again: the rung is spent, so nothing more is sold here.
    assert plan_exit(position, make_market(price_usd=0.002), config) is None

    second = plan_exit(position, make_market(price_usd=0.0041), config)
    assert second is not None and second.reason is ExitReason.TAKE_PROFIT
    assert position.rungs_taken == {0, 1}


def test_a_gap_through_two_rungs_takes_them_one_at_a_time():
    position = opened()
    config = ExitConfig()
    market = make_market(price_usd=0.005)
    assert plan_exit(position, market, config) is not None
    assert position.rungs_taken == {0}
    assert plan_exit(position, market, config) is not None
    assert position.rungs_taken == {0, 1}


def test_trailing_stop_is_armed_only_after_a_rung_is_taken():
    position = opened()
    config = ExitConfig()
    # Down 40% from a peak it never profited from: not a trailing stop, because
    # launch volatility would otherwise close every position immediately.
    position.peak_price_usd = 0.0015
    assert plan_exit(position, make_market(price_usd=0.0009), config) is None

    plan_exit(position, make_market(price_usd=0.002), config)  # take rung 0
    stopped = plan_exit(position, make_market(price_usd=0.0012), config)
    assert stopped is not None
    assert stopped.reason is ExitReason.TRAILING_STOP


def test_max_hold_times_a_position_out():
    position = opened()
    config = ExitConfig(max_hold_seconds=60.0)
    position.opened_at_ms = now_ms() - 61_000
    plan = plan_exit(position, make_market(price_usd=0.00105), config)
    assert plan is not None
    assert plan.reason is ExitReason.MAX_HOLD


def test_closed_position_is_not_re_exited():
    position = opened()
    position.apply(
        Fill(
            mint="M",
            symbol="TEST",
            side=Side.SELL,
            qty=position.qty,
            price_usd=0.002,
            usd=position.qty * 0.002,
            fee_usd=1.0,
            slippage_bps=50,
            ts_ms=now_ms(),
            tx_ref="s",
            paper=True,
        )
    )
    assert not position.is_open
    assert plan_exit(position, make_market(price_usd=0.0001), ExitConfig()) is None
