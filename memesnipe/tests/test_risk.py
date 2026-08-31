from __future__ import annotations

from conftest import make_market

from memesnipe.config import RiskConfig
from memesnipe.models import now_ms
from memesnipe.risk import assess, effective_thresholds, price_impact_bps


def test_price_impact_grows_as_the_pool_thins():
    assert price_impact_bps(50, 100_000) < price_impact_bps(50, 10_000)
    assert price_impact_bps(50, 1_000) > 500
    # No pool at all is total impact, not a divide by zero.
    assert price_impact_bps(50, 0) == 10_000.0


def test_clean_market_passes():
    report = assess(make_market(), RiskConfig(), size_usd=25)
    assert report.passed
    assert report.score > 0


def test_honeypot_shape_is_rejected_in_strict_and_allowed_in_degen():
    market = make_market(buys_5m=200, sells_5m=0)
    strict = assess(market, RiskConfig(profile="strict"), size_usd=25)
    assert not strict.passed
    assert strict.checks["has_sells"] is False

    degen = assess(market, RiskConfig(profile="degen"), size_usd=25)
    assert degen.passed


def test_price_impact_cap_holds_even_in_degen():
    # The degen profile buys thinner pools, but never one where the order
    # itself moves the price past the cap.
    market = make_market(liquidity_usd=1_200.0)
    report = assess(market, RiskConfig(profile="degen"), size_usd=200)
    assert not report.passed
    assert report.checks["price_impact"] is False


def test_blocklist_is_absolute():
    market = make_market()
    config = RiskConfig(profile="degen", blocked_mints=[market.mint])
    report = assess(market, config, size_usd=25)
    assert not report.passed
    assert report.checks["not_blocked"] is False


def test_unknown_pair_age_is_treated_as_a_failure_not_as_fresh():
    report = assess(make_market(pair_created_at_ms=0), RiskConfig(), size_usd=25)
    assert not report.passed
    assert report.checks["age_known"] is False


def test_pair_older_than_the_window_is_rejected():
    stale = make_market(pair_created_at_ms=now_ms() - 7_200_000)
    assert not assess(stale, RiskConfig(), size_usd=25).passed


def test_no_market_is_a_rejection_not_a_crash():
    report = assess(None, RiskConfig(), size_usd=25)
    assert not report.passed
    assert report.reasons == ["no market found"]


def test_every_check_is_recorded_even_after_the_first_failure():
    report = assess(make_market(liquidity_usd=1.0), RiskConfig(), size_usd=25)
    assert not report.passed
    # The journal needs the whole picture, not just the first thing that broke.
    assert len(report.checks) > 5
    assert report.checks["min_liquidity"] is False


def test_degen_relaxes_soft_thresholds_only():
    strict = effective_thresholds(RiskConfig(profile="strict"))
    degen = effective_thresholds(RiskConfig(profile="degen"))
    assert degen.min_liquidity_usd < strict.min_liquidity_usd
    assert degen.min_buys_5m < strict.min_buys_5m
    assert degen.require_sells is False
