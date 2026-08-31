from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from memesnipe.models import Chain, Market, Signal, now_ms


@pytest.fixture
def signal() -> Signal:
    created = now_ms()
    return Signal(
        source="x",
        author="tester",
        post_id="1",
        text="hello",
        created_at_ms=created,
        received_at_ms=created + 500,
    )


def make_market(**overrides: object) -> Market:
    defaults: dict[str, object] = {
        "mint": "MintAddress1111111111111111111111111111111",
        "chain": Chain.SOLANA,
        "symbol": "TEST",
        "name": "Test",
        "price_usd": 0.001,
        "liquidity_usd": 40_000.0,
        "fdv_usd": 400_000.0,
        "volume_5m_usd": 20_000.0,
        "buys_5m": 60,
        "sells_5m": 30,
        "pair_address": "pair",
        "dex": "raydium",
        "pair_created_at_ms": now_ms() - 120_000,
    }
    defaults.update(overrides)
    return Market(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def market() -> Market:
    return make_market()
