"""Account-level hard stops.

These are not strategy parameters. They exist so that a bug, a bad market, or a
spoofed signal costs a bounded amount instead of the account. Every one of them
applies in both paper and live mode - a limit you have never seen fire in paper
is a limit you have not actually tested.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from .config import GuardConfig
from .models import now_ms

log = logging.getLogger(__name__)

_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000


@dataclass(slots=True)
class Guards:
    config: GuardConfig
    open_positions: int = 0
    spent_today_usd: float = 0.0
    realized_today_usd: float = 0.0
    traded_mints: set[str] = field(default_factory=set)
    # Entries that have passed the checks but whose order has not come back
    # yet. Without this, two signals arriving in the same instant both see the
    # position count from before either of them filled, and the limits are
    # quietly exceeded by however many tasks are in flight.
    pending: dict[str, float] = field(default_factory=dict)
    _trade_times: deque[int] = field(default_factory=deque)
    _day_started_ms: int = field(default_factory=now_ms)
    halted_reason: str | None = None

    def _roll_day(self) -> None:
        if now_ms() - self._day_started_ms >= _DAY_MS:
            self._day_started_ms = now_ms()
            self.spent_today_usd = 0.0
            self.realized_today_usd = 0.0
            # A halt from yesterday's loss limit does not carry over; a halt
            # from anything else is deliberate and stays until restart.
            if self.halted_reason == "daily loss limit reached":
                self.halted_reason = None
                log.info("daily loss limit reset")

    def _recent_trades(self) -> int:
        cutoff = now_ms() - _HOUR_MS
        while self._trade_times and self._trade_times[0] < cutoff:
            self._trade_times.popleft()
        return len(self._trade_times)

    def blocked_reason(self, mint: str, size_usd: float) -> str | None:  # noqa: PLR0911
        """Why this entry may not proceed, or None if it may.

        Counts in-flight entries as if they had already filled, so the limits
        hold under concurrency rather than only when signals arrive one at a
        time.
        """
        self._roll_day()
        if self.halted_reason:
            return f"halted: {self.halted_reason}"
        if mint in self.pending:
            return "an entry for this mint is already in flight"
        if self.config.dedupe_mints and mint in self.traded_mints:
            return "already traded this mint in this run"
        if self.open_positions + len(self.pending) >= self.config.max_open_positions:
            return f"at the {self.config.max_open_positions} open position limit"
        committed = self.spent_today_usd + sum(self.pending.values())
        if committed + size_usd > self.config.max_daily_spend_usd:
            return f"daily spend cap ${self.config.max_daily_spend_usd:,.0f} would be exceeded"
        if -self.realized_today_usd >= self.config.max_daily_loss_usd:
            self.halted_reason = "daily loss limit reached"
            return f"halted: {self.halted_reason}"
        if self._recent_trades() + len(self.pending) >= self.config.max_trades_per_hour:
            return f"at the {self.config.max_trades_per_hour} trades/hour limit"
        return None

    def reserve(self, mint: str, size_usd: float) -> str | None:
        """Check the limits and hold a slot in the same step.

        There is no await between the check and the reservation, so a slot
        cannot be handed to two signals at once. Every reservation must be
        followed by exactly one `record_entry` or `release`.
        """
        blocked = self.blocked_reason(mint, size_usd)
        if blocked is not None:
            return blocked
        self.pending[mint] = size_usd
        return None

    def release(self, mint: str) -> None:
        """Give back a reservation whose order did not fill."""
        self.pending.pop(mint, None)

    def record_entry(self, mint: str, size_usd: float) -> None:
        self.pending.pop(mint, None)
        self.traded_mints.add(mint)
        self.spent_today_usd += size_usd
        self.open_positions += 1
        self._trade_times.append(now_ms())

    def record_exit(self, realized_usd: float, *, closed: bool) -> None:
        self.realized_today_usd += realized_usd
        if closed:
            self.open_positions = max(0, self.open_positions - 1)
        if -self.realized_today_usd >= self.config.max_daily_loss_usd and not self.halted_reason:
            self.halted_reason = "daily loss limit reached"
            log.error(
                "HALTED: down $%.2f today, at the $%.2f limit. No new entries until reset.",
                -self.realized_today_usd,
                self.config.max_daily_loss_usd,
            )

    def halt(self, reason: str) -> None:
        self.halted_reason = reason
        log.error("HALTED: %s", reason)
