"""Value types passed between stages of the pipeline.

Every stage takes one of these and produces the next. Keeping them frozen
dataclasses means a decision can be journalled verbatim and replayed later,
which is the only way to tell whether a change to the scoring actually helped
or just fit the last week of noise.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Simulation clock. At scale 1.0 this is the wall clock and costs one
# `time.time()` call. Replay runs it faster so a fifteen-minute hold rule can
# be exercised in fifteen seconds - and critically, *everything* scales
# together: the feed pacing, the simulated price path, the hold timeout and the
# guard windows. A scaled clock that only some of those honour would quietly
# test a strategy nobody is running.
_scale = 1.0
_origin_real_ms = 0
_origin_virtual_ms = 0


def now_ms() -> int:
    """Milliseconds on the simulation clock."""
    real = int(time.time() * 1000)
    if _scale == 1.0:
        return real
    return _origin_virtual_ms + int((real - _origin_real_ms) * _scale)


def set_time_scale(scale: float) -> None:
    """Run the clock `scale` times faster. 1.0 restores the wall clock."""
    global _scale, _origin_real_ms, _origin_virtual_ms  # noqa: PLW0603 - one clock, by design
    if scale <= 0:
        raise ValueError("time scale must be positive")
    _origin_virtual_ms = now_ms()
    _origin_real_ms = int(time.time() * 1000)
    _scale = float(scale)


def time_scale() -> float:
    return _scale


async def sleep_virtual(seconds: float) -> None:
    """Sleep `seconds` of simulation time, which is less real time when the
    clock is accelerated."""
    if seconds > 0:
        await asyncio.sleep(seconds / _scale)


class Chain(StrEnum):
    SOLANA = "solana"
    BASE = "base"
    ETHEREUM = "ethereum"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExitReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    HARD_STOP = "hard_stop"
    MAX_HOLD = "max_hold"
    LIQUIDITY_PULLED = "liquidity_pulled"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Signal:
    """A post from a watched account that might reference a token."""

    source: str
    author: str
    post_id: str
    text: str
    created_at_ms: int
    received_at_ms: int = field(default_factory=now_ms)
    url: str = ""
    author_weight: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def feed_lag_ms(self) -> int:
        """How stale the post was by the time we saw it.

        This is the number that decides whether the whole strategy is viable.
        If it is routinely above a couple of seconds you are buying other
        bots' exits, and no amount of downstream tuning fixes that.
        """
        return max(0, self.received_at_ms - self.created_at_ms)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A token reference extracted from a signal, before any market lookup."""

    chain: Chain
    signal: Signal
    mint: str | None = None
    symbol: str | None = None
    # How the reference was found: an explicit address is far stronger evidence
    # than a bare $TICKER, which is ambiguous and trivially front-run.
    kind: str = "unknown"
    confidence: float = 0.5

    @property
    def key(self) -> str:
        return self.mint or f"${(self.symbol or '').upper()}"


@dataclass(frozen=True, slots=True)
class Market:
    """A market snapshot for a candidate, from whichever discovery source won."""

    mint: str
    chain: Chain
    symbol: str
    name: str
    price_usd: float
    liquidity_usd: float
    fdv_usd: float
    volume_5m_usd: float
    buys_5m: int
    sells_5m: int
    pair_address: str
    dex: str
    pair_created_at_ms: int
    fetched_at_ms: int = field(default_factory=now_ms)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return max(0.0, (self.fetched_at_ms - self.pair_created_at_ms) / 1000.0)


@dataclass(frozen=True, slots=True)
class RiskReport:
    """The outcome of every safety check, pass or fail."""

    passed: bool
    score: float
    checks: dict[str, bool]
    reasons: list[str]

    @classmethod
    def rejected(cls, reason: str) -> RiskReport:
        return cls(passed=False, score=0.0, checks={}, reasons=[reason])


@dataclass(frozen=True, slots=True)
class Decision:
    """What the engine chose to do, and everything it based that on."""

    candidate: Candidate
    market: Market | None
    risk: RiskReport
    size_usd: float
    conviction: float
    act: bool
    reason: str
    latency_ms: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Fill:
    """An executed trade, real or simulated."""

    mint: str
    symbol: str
    side: Side
    qty: float
    price_usd: float
    usd: float
    fee_usd: float
    slippage_bps: float
    ts_ms: int
    tx_ref: str
    paper: bool


@dataclass(slots=True)
class Position:
    """An open (or closed) position. Mutable: it is updated on every fill."""

    mint: str
    symbol: str
    chain: Chain
    qty: float = 0.0
    cost_usd: float = 0.0
    realized_usd: float = 0.0
    fees_usd: float = 0.0
    opened_at_ms: int = field(default_factory=now_ms)
    closed_at_ms: int | None = None
    peak_price_usd: float = 0.0
    entry_price_usd: float = 0.0
    entry_liquidity_usd: float = 0.0
    # Which rungs of the take-profit ladder have already been sold, so a
    # position that gaps through two rungs at once does not sell both twice.
    rungs_taken: set[int] = field(default_factory=set)
    exit_reason: ExitReason | None = None

    @property
    def is_open(self) -> bool:
        return self.qty > 0 and self.closed_at_ms is None

    def unrealized_usd(self, price_usd: float) -> float:
        return self.qty * price_usd - self.cost_usd

    def pnl_usd(self, price_usd: float) -> float:
        return self.realized_usd + self.unrealized_usd(price_usd)

    def multiple(self, price_usd: float) -> float:
        if self.entry_price_usd <= 0:
            return 1.0
        return price_usd / self.entry_price_usd

    def apply(self, fill: Fill) -> None:
        self.fees_usd += fill.fee_usd
        if fill.side is Side.BUY:
            self.qty += fill.qty
            self.cost_usd += fill.usd
            if self.entry_price_usd == 0.0:
                self.entry_price_usd = fill.price_usd
                self.peak_price_usd = fill.price_usd
        else:
            # Realise proportionally so a partial exit leaves the remaining
            # cost basis intact rather than booking the whole position.
            sold = min(fill.qty, self.qty)
            share = sold / self.qty if self.qty else 0.0
            basis = self.cost_usd * share
            self.realized_usd += fill.usd - basis
            self.cost_usd -= basis
            self.qty -= sold
            if self.qty <= 1e-9:
                self.qty = 0.0
                self.closed_at_ms = fill.ts_ms
