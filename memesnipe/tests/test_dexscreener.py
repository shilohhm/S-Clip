from __future__ import annotations

import httpx

from memesnipe.config import DiscoveryConfig
from memesnipe.discovery.dexscreener import DexscreenerResolver
from memesnipe.models import Candidate, Chain, Signal, now_ms

MINT = "9tUZEz8VjYBqXTLDKZdQY1L8LbDWTZ823VZsmzaZqVVz"


def pair(symbol: str, liquidity: float, *, chain: str = "solana", address: str = MINT) -> dict:
    return {
        "chainId": chain,
        "dexId": "raydium",
        "pairAddress": f"pair-{symbol}-{liquidity:.0f}",
        "baseToken": {"address": address, "symbol": symbol, "name": symbol},
        "priceUsd": "0.001",
        "liquidity": {"usd": liquidity},
        "fdv": liquidity * 10,
        "volume": {"m5": 5_000},
        "txns": {"m5": {"buys": 40, "sells": 20}},
        "pairCreatedAt": now_ms() - 60_000,
    }


def resolver_for(handler, **overrides) -> DexscreenerResolver:
    config = DiscoveryConfig(**overrides)
    client = httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    return DexscreenerResolver(config, client=client)


def candidate(*, mint: str | None = None, symbol: str | None = None) -> Candidate:
    signal = Signal(source="x", author="a", post_id="1", text="", created_at_ms=now_ms())
    return Candidate(
        chain=Chain.SOLANA,
        signal=signal,
        mint=mint,
        symbol=symbol,
        kind="mint_address" if mint else "ticker",
    )


async def test_mint_lookup_takes_the_deepest_pool():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [pair("TEST", 5_000), pair("TEST", 90_000)]})

    market = await resolver_for(handler).resolve(candidate(mint=MINT))
    assert market is not None
    assert market.liquidity_usd == 90_000


async def test_pairs_on_other_chains_are_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [pair("TEST", 90_000, chain="base")]})

    assert await resolver_for(handler).resolve(candidate(mint=MINT)) is None


async def test_a_missing_pair_is_retried_while_it_indexes():
    # The window this bot trades is exactly the window where a brand new pair
    # is not in the index yet, so a first miss must not end the attempt.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"pairs": []})
        return httpx.Response(200, json={"pairs": [pair("TEST", 20_000)]})

    market = await resolver_for(handler, retries=3, retry_delay_ms=1).resolve(candidate(mint=MINT))
    assert market is not None
    assert calls["n"] == 3


async def test_a_transport_error_is_not_raised_at_the_caller():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    assert await resolver_for(handler, retries=1).resolve(candidate(mint=MINT)) is None


async def test_a_clear_ticker_winner_resolves():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [pair("MOON", 100_000), pair("MOON", 2_000)]})

    market = await resolver_for(handler).resolve(candidate(symbol="MOON"))
    assert market is not None
    assert market.liquidity_usd == 100_000


async def test_an_ambiguous_ticker_is_refused():
    # Two pools of comparable depth sharing a ticker is the shape of a
    # deliberate collision. Guessing here buys the scammer's token.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [pair("MOON", 100_000), pair("MOON", 80_000)]})

    assert await resolver_for(handler).resolve(candidate(symbol="MOON")) is None


async def test_ticker_match_must_be_exact():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [pair("MOONBOY", 100_000)]})

    assert await resolver_for(handler).resolve(candidate(symbol="MOON")) is None


async def test_malformed_payloads_do_not_crash_the_resolver():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": [{"chainId": "solana"}, None, "junk"]})

    assert await resolver_for(handler, retries=1).resolve(candidate(mint=MINT)) is None
