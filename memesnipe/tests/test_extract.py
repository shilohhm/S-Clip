from __future__ import annotations

from memesnipe.extract import extract_candidates, is_base58_address, matches_filters
from memesnipe.models import Chain, Signal, now_ms

REAL_MINT = "9tUZEz8VjYBqXTLDKZdQY1L8LbDWTZ823VZsmzaZqVVz"


def make_signal(text: str) -> Signal:
    return Signal(source="x", author="a", post_id="1", text=text, created_at_ms=now_ms())


def test_base58_validation_rejects_lookalike_words():
    # The right length and alphabet, but it does not decode to 32 bytes. Acting
    # on one of these wastes the only two seconds that matter.
    assert not is_base58_address("abcdefghijklmnopqrstuvwxyzabcdefg")
    assert not is_base58_address("short")
    assert is_base58_address(REAL_MINT)


def test_pumpfun_link_beats_bare_ticker():
    candidates = extract_candidates(
        make_signal(f"aping https://pump.fun/coin/{REAL_MINT} and also $DOGE")
    )
    assert candidates[0].kind == "pumpfun_link"
    assert candidates[0].mint == REAL_MINT
    assert candidates[0].confidence > candidates[-1].confidence


def test_mint_is_not_emitted_twice_from_link_and_bare_address():
    candidates = extract_candidates(make_signal(f"{REAL_MINT} https://pump.fun/coin/{REAL_MINT}"))
    assert [c.mint for c in candidates] == [REAL_MINT]


def test_majors_and_program_ids_are_ignored():
    text = (
        "swap on So11111111111111111111111111111111111111112 "
        "for EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v $SOL $BTC"
    )
    assert extract_candidates(make_signal(text)) == []


def test_evm_addresses_only_on_evm_chains():
    text = "buy 0x1234567890abcdef1234567890ABCDEF12345678 now"
    assert extract_candidates(make_signal(text), Chain.SOLANA) == []
    on_base = extract_candidates(make_signal(text), Chain.BASE)
    assert on_base[0].mint == "0x1234567890abcdef1234567890abcdef12345678"


def test_dexscreener_link_carries_its_own_chain():
    candidates = extract_candidates(
        make_signal("https://dexscreener.com/base/0xabc123def4567890abcdef1234567890abcdef12")
    )
    assert candidates[0].chain is Chain.BASE


def test_filters_accept_substrings_and_regex():
    assert matches_filters("new CA below", [])
    assert matches_filters("check pump.fun/coin/x", ["pump.fun"])
    assert matches_filters("the CA is here", [r"/\bCA\b/"])
    assert not matches_filters("nothing here", ["$", "pump.fun"])
