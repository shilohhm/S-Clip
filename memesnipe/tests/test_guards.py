from __future__ import annotations

from memesnipe.config import GuardConfig
from memesnipe.guards import Guards


def test_reservations_stop_concurrent_signals_exceeding_the_position_limit():
    guards = Guards(GuardConfig(max_open_positions=2, max_daily_spend_usd=10_000))
    assert guards.reserve("a", 25) is None
    assert guards.reserve("b", 25) is None
    # Nothing has filled yet, but both slots are spoken for. Without the
    # reservation this third signal would see zero open positions and pass.
    assert guards.reserve("c", 25) is not None


def test_a_released_reservation_frees_its_slot():
    guards = Guards(GuardConfig(max_open_positions=1))
    assert guards.reserve("a", 25) is None
    assert guards.reserve("b", 25) is not None
    guards.release("a")
    assert guards.reserve("b", 25) is None


def test_the_same_mint_cannot_be_entered_twice_concurrently():
    guards = Guards(GuardConfig(max_open_positions=5))
    assert guards.reserve("a", 25) is None
    assert "in flight" in (guards.reserve("a", 25) or "")


def test_dedupe_blocks_a_mint_already_traded():
    guards = Guards(GuardConfig())
    guards.reserve("a", 25)
    guards.record_entry("a", 25)
    guards.record_exit(-5.0, closed=True)
    assert "already traded" in (guards.reserve("a", 25) or "")


def test_daily_spend_cap_counts_reserved_size():
    guards = Guards(GuardConfig(max_daily_spend_usd=100, max_open_positions=10))
    assert guards.reserve("a", 60) is None
    assert "daily spend cap" in (guards.reserve("b", 60) or "")


def test_daily_loss_halts_all_new_entries():
    guards = Guards(GuardConfig(max_daily_loss_usd=50))
    guards.reserve("a", 25)
    guards.record_entry("a", 25)
    guards.record_exit(-60.0, closed=True)
    assert guards.halted_reason == "daily loss limit reached"
    assert "halted" in (guards.reserve("b", 25) or "")


def test_trades_per_hour_limit():
    guards = Guards(GuardConfig(max_trades_per_hour=2, max_open_positions=10))
    for mint in ("a", "b"):
        guards.reserve(mint, 10)
        guards.record_entry(mint, 10)
        guards.record_exit(1.0, closed=True)
    assert "trades/hour" in (guards.reserve("c", 10) or "")
