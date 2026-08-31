from __future__ import annotations

from collections.abc import AsyncIterator

from conftest import make_market

from memesnipe.config import Config
from memesnipe.engine import Engine
from memesnipe.execution.paper import PaperVenue
from memesnipe.models import Candidate, ExitReason, Market, Signal, now_ms
from memesnipe.storage import Journal

MINT = "9tUZEz8VjYBqXTLDKZdQY1L8LbDWTZ823VZsmzaZqVVz"


class FakeWatcher:
    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    async def __aiter__(self) -> AsyncIterator[Signal]:
        for signal in self._signals:
            yield signal

    async def aclose(self) -> None:
        return None


class FakeResolver:
    """Resolves to whatever the test hands it, and lets the test move the
    market underneath an open position."""

    def __init__(self, market: Market | None) -> None:
        self.market = market
        self.snapshots = 0

    async def resolve(self, candidate: Candidate) -> Market | None:
        return self.market

    async def snapshot(self, mint: str) -> Market | None:
        self.snapshots += 1
        return self.market

    async def aclose(self) -> None:
        return None


def make_engine(tmp_path, market: Market | None, **config_overrides):
    config = Config()
    config.db_path = str(tmp_path / "j.sqlite3")
    for key, value in config_overrides.items():
        section, _, field = key.partition("__")
        setattr(getattr(config, section), field, value)
    resolver = FakeResolver(market)
    journal = Journal(config.db_path)
    engine = Engine(
        config,
        watcher=FakeWatcher([]),
        resolver=resolver,
        venue=PaperVenue(config.execution),
        journal=journal,
    )
    return engine, resolver, journal


def signal_for(text: str, lag_ms: int = 500) -> Signal:
    created = now_ms()
    return Signal(
        source="x",
        author="tester",
        post_id=str(now_ms()),
        text=text,
        created_at_ms=created,
        received_at_ms=created + lag_ms,
    )


async def test_a_clean_signal_becomes_a_position(tmp_path):
    engine, _, journal = make_engine(tmp_path, make_market(mint=MINT))
    decisions = await engine.handle_signal(signal_for(f"https://pump.fun/coin/{MINT}"))

    assert len(decisions) == 1
    assert decisions[0].act
    assert engine.portfolio.get(MINT) is not None
    assert journal.summary()["signals_acted"] == 1
    # Latency is journalled per stage, not just in aggregate.
    assert "resolve" in decisions[0].latency_ms
    journal.close()


async def test_stale_signals_are_journalled_rather_than_dropped_silently(tmp_path):
    engine, _, journal = make_engine(tmp_path, make_market(mint=MINT))
    decisions = await engine.handle_signal(
        signal_for(f"https://pump.fun/coin/{MINT}", lag_ms=30_000)
    )

    assert decisions and not decisions[0].act
    assert "stale" in decisions[0].reason
    assert engine.portfolio.open_positions == []
    # It must show up in the rejection stats: "we were too slow" is the most
    # actionable rejection reason there is.
    reasons = journal.rejection_reasons()
    assert reasons and "stale" in reasons[0][0]
    journal.close()


async def test_an_unresolvable_token_is_recorded_and_not_bought(tmp_path):
    engine, _, journal = make_engine(tmp_path, None)
    decisions = await engine.handle_signal(signal_for(f"CA {MINT}"))
    assert not decisions[0].act
    assert decisions[0].reason == "could not resolve a market"
    assert engine.portfolio.open_positions == []
    journal.close()


async def test_a_failed_risk_check_blocks_the_trade(tmp_path):
    engine, _, journal = make_engine(tmp_path, make_market(mint=MINT, liquidity_usd=100.0))
    decisions = await engine.handle_signal(signal_for(f"CA {MINT}"))
    assert not decisions[0].act
    assert engine.portfolio.open_positions == []
    journal.close()


async def test_only_one_position_is_taken_per_post(tmp_path):
    # A post naming several tokens is nearly always noise around one call.
    engine, _, journal = make_engine(tmp_path, make_market(mint=MINT))
    text = f"{MINT} and $ALPHA and $BETA"
    decisions = await engine.handle_signal(signal_for(text))
    assert sum(1 for decision in decisions if decision.act) == 1
    journal.close()


async def test_the_position_limit_is_enforced_across_signals(tmp_path):
    engine, _, journal = make_engine(tmp_path, make_market(mint=MINT), guards__max_open_positions=1)
    await engine.handle_signal(signal_for(f"CA {MINT}"))
    second = await engine.handle_signal(signal_for("$OTHER"))
    assert not any(decision.act for decision in second)
    journal.close()


async def test_a_liquidity_pull_closes_the_position_on_the_next_cycle(tmp_path):
    engine, resolver, journal = make_engine(tmp_path, make_market(mint=MINT, liquidity_usd=40_000))
    await engine.handle_signal(signal_for(f"CA {MINT}"))
    position = engine.portfolio.get(MINT)
    assert position is not None and position.is_open

    # The pool is drained between cycles, which is what a rug looks like from
    # here: the price still quotes, but the liquidity is gone.
    resolver.market = make_market(mint=MINT, liquidity_usd=800.0, price_usd=0.00002)
    await engine.manage_positions_once()

    assert not position.is_open
    assert position.exit_reason is ExitReason.LIQUIDITY_PULLED
    assert engine.guards.open_positions == 0
    journal.close()


async def test_an_unfillable_exit_leaves_the_position_open_for_a_retry(tmp_path):
    engine, resolver, journal = make_engine(tmp_path, make_market(mint=MINT))
    await engine.handle_signal(signal_for(f"CA {MINT}"))
    position = engine.portfolio.get(MINT)
    assert position is not None

    # Liquidity at zero means the sell does not fill. Dropping the position
    # here would leave it unmanaged forever.
    resolver.market = make_market(mint=MINT, liquidity_usd=0.0, price_usd=0.0001)
    await engine.manage_positions_once()
    assert position.is_open
    journal.close()


async def test_run_consumes_the_feed_and_flattens_nothing_it_did_not_open(tmp_path):
    config = Config()
    config.db_path = str(tmp_path / "j.sqlite3")
    config.exits.max_hold_seconds = 0.0  # every position times out immediately
    config.exits.poll_seconds = 0.01
    journal = Journal(config.db_path)
    engine = Engine(
        config,
        watcher=FakeWatcher([signal_for(f"CA {MINT}")]),
        resolver=FakeResolver(make_market(mint=MINT)),
        venue=PaperVenue(config.execution),
        journal=journal,
    )
    await engine.run()
    # The run does not end while a position is still open.
    assert engine.portfolio.open_positions == []
    assert engine.portfolio.all_positions[0].exit_reason is ExitReason.MAX_HOLD
    journal.close()
