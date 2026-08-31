"""Pull token references out of post text.

The precision of this module sets the ceiling on everything downstream. A bare
`$TICKER` matches dozens of pairs and is the reference scammers deliberately
create collisions for, so it is emitted with low confidence and the resolver is
told to demand a clear liquidity winner. An explicit mint address, or a
pump.fun/Dexscreener link, identifies exactly one token and is trusted.
"""

from __future__ import annotations

import re

from .models import Candidate, Chain, Signal

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58_ALPHABET)}

# Deliberately loose; every hit is verified by decoding it below.
_B58_CANDIDATE = re.compile(rf"\b[{_B58_ALPHABET}]{{32,44}}\b")
_EVM_ADDRESS = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_TICKER = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,14})\b")
_PUMPFUN_LINK = re.compile(rf"pump\.fun/(?:coin/)?([{_B58_ALPHABET}]{{32,44}})", re.IGNORECASE)
_DEXSCREENER_LINK = re.compile(
    r"dexscreener\.com/(?P<chain>[a-z]+)/(?P<address>[A-Za-z0-9]{32,64})", re.IGNORECASE
)
_BIRDEYE_LINK = re.compile(rf"birdeye\.so/token/([{_B58_ALPHABET}]{{32,44}})", re.IGNORECASE)

# Base58 strings that are valid addresses but never a tradeable memecoin mint.
_KNOWN_NON_TARGETS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "11111111111111111111111111111111",  # system program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL token program
}

# Words that look like tickers but are market chatter, not a call.
_TICKER_STOPWORDS = {"SOL", "ETH", "BTC", "USD", "USDC", "USDT", "BNB", "SPX"}


def is_base58_address(text: str) -> bool:
    """True when the string decodes to a 32-byte value, i.e. a real pubkey.

    Length alone is not enough: plenty of ordinary words and hashes fall in the
    32-44 character range, and acting on one wastes the only thing that matters
    here, which is the first two seconds.
    """
    if not 32 <= len(text) <= 44:
        return False
    number = 0
    for char in text:
        index = _B58_INDEX.get(char)
        if index is None:
            return False
        number = number * 58 + index
    leading_zeros = len(text) - len(text.lstrip("1"))
    decoded_length = leading_zeros + (number.bit_length() + 7) // 8
    return decoded_length == 32


def _chain_from_slug(slug: str) -> Chain | None:
    try:
        return Chain(slug.lower())
    except ValueError:
        return None


def extract_candidates(signal: Signal, chain: Chain = Chain.SOLANA) -> list[Candidate]:
    """Every distinct token reference in the post, best evidence first."""
    text = signal.text
    found: list[Candidate] = []
    seen: set[str] = set()

    def add(
        *,
        mint: str | None = None,
        symbol: str | None = None,
        kind: str,
        confidence: float,
        candidate_chain: Chain = chain,
    ) -> None:
        key = mint or f"${(symbol or '').upper()}"
        if key in seen or key == "$":
            return
        seen.add(key)
        found.append(
            Candidate(
                chain=candidate_chain,
                signal=signal,
                mint=mint,
                symbol=symbol.upper() if symbol else None,
                kind=kind,
                confidence=confidence,
            )
        )

    # A launchpad link is the strongest possible reference: it names the mint
    # and tells us the venue in one go.
    for mint in _PUMPFUN_LINK.findall(text):
        if is_base58_address(mint):
            add(mint=mint, kind="pumpfun_link", confidence=1.0)

    for match in _DEXSCREENER_LINK.finditer(text):
        link_chain = _chain_from_slug(match.group("chain")) or chain
        add(
            mint=match.group("address"),
            kind="dexscreener_link",
            confidence=0.95,
            candidate_chain=link_chain,
        )

    for mint in _BIRDEYE_LINK.findall(text):
        if is_base58_address(mint):
            add(mint=mint, kind="birdeye_link", confidence=0.95)

    if chain is Chain.SOLANA:
        for token in _B58_CANDIDATE.findall(text):
            if token in _KNOWN_NON_TARGETS or not is_base58_address(token):
                continue
            add(mint=token, kind="mint_address", confidence=0.9)
    else:
        for address in _EVM_ADDRESS.findall(text):
            add(mint=address.lower(), kind="contract_address", confidence=0.9)

    for symbol in _TICKER.findall(text):
        if symbol.upper() in _TICKER_STOPWORDS:
            continue
        add(symbol=symbol, kind="ticker", confidence=0.35)

    found.sort(key=lambda candidate: candidate.confidence, reverse=True)
    return found


def matches_filters(text: str, patterns: list[str]) -> bool:
    """Whether the post passes an account's `must_match` list.

    `/.../` is treated as a regex, anything else as a case-insensitive
    substring. An empty list matches everything.
    """
    if not patterns:
        return True
    lowered = text.lower()
    for pattern in patterns:
        if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
            if re.search(pattern[1:-1], text, re.IGNORECASE):
                return True
        elif pattern.lower() in lowered:
            return True
    return False
