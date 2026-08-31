"""The pipeline: signal in, position out, exits managed until flat.

Two concurrent halves, and they must not block each other. The entry path is a
race - every millisecond between the post and the fill is price you pay - so
each signal is handled in its own task. The exit path is a loop over open
positions, and it keeps running even when no signals are arriving, because a
position with nothing watching it is the one that goes to zero.

Every stage stamps its latency. The `latency` field on a decision is the
honest record of where the time went, and it is the first thing to read when
the fills are worse than the paper run predicted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .config import Config
from .discovery.base import Resolver
from .execution.base import Venue
from .extract import extract_candidates
from .guards import Guards
from .models import Candidate, Decision, ExitReason, RiskReport, Signal, now_ms, time_scale
from .portfolio import Portfolio, plan_exit
from .risk import assess
from .scoring import conviction, size_position
from .storage import Journal

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        config: Config,
        *,
        watcher: object,
        resolver: Resolver,
        venue: Venue,
        journal: Journal,
        max_concurrent_signals: int = 8,
    ) -> None:
        self.config = config
        self.watcher = watcher
        self.resolver = resolver
        self.venue = venue
        self.journal = journal
        self.portfolio = Portfolio()
        self.guards = Guards(config.guards)
        self._semaphore = asyncio.Semaphore(max_concurrent_signals)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    # -- entry path ------------------------------------------------------

    async def handle_signal(self, signal: Signal) -> list[Decision]:
        self.journal.record_signal(signal)
        log.info(
            "signal @%s (%dms lag): %s",
            signal.author,
            signal.feed_lag_ms,
            signal.text[:120].replace("\n", " "),
        )

        candidates = extract_candidates(signal, self.config.discovery.chain)
        if not candidates:
            return []

        # Journal the staleness drop rather than returning early and silently.
        # Lag is the single most useful rejection statistic there is: if most
        # skips are "too slow", no amount of threshold tuning is the fix.
        if signal.feed_lag_ms > self.config.twitter.max_feed_lag_ms:
            reason = (
                f"stale signal: {signal.feed_lag_ms}ms lag over the "
                f"{self.config.twitter.max_feed_lag_ms}ms limit"
            )
            log.info("dropping %s", reason)
            stale = [
                Decision(
                    candidate=candidate,
                    market=None,
                    risk=RiskReport.rejected(reason),
                    size_usd=0.0,
                    conviction=0.0,
                    act=False,
                    reason=reason,
                    latency_ms={"feed_lag": signal.feed_lag_ms},
                )
                for candidate in candidates[:1]
            ]
            for decision in stale:
                self.journal.record_decision(decision)
            return stale

        decisions: list[Decision] = []
        for candidate in candidates:
            decision = await self._evaluate(candidate)
            self.journal.record_decision(decision)
            decisions.append(decision)
            if decision.act:
                # One position per signal. A post naming three tokens is
                # almost always noise around one real call, and buying all
                # three triples the exposure for the same information.
                break
        return decisions

    async def _evaluate(self, candidate: Candidate) -> Decision:
        latency: dict[str, int] = {"feed_lag": candidate.signal.feed_lag_ms}
        started = now_ms()

        market = await self.resolver.resolve(candidate)
        latency["resolve"] = now_ms() - started
        if market is None:
            return Decision(
                candidate=candidate,
                market=None,
                risk=RiskReport.rejected("could not resolve a market"),
                size_usd=0.0,
                conviction=0.0,
                act=False,
                reason="could not resolve a market",
                latency_ms=latency,
            )

        # Size is needed to judge price impact, and price impact feeds back
        # into whether the trade is worth taking, so the first pass sizes at
        # the base amount and the second re-checks at the size actually used.
        provisional = assess(market, self.config.risk, size_usd=self.config.sizing.base_size_usd)
        score = conviction(candidate, market, provisional)
        size_usd = size_position(score, self.config.sizing)
        risk = assess(market, self.config.risk, size_usd=size_usd) if size_usd > 0 else provisional
        latency["risk"] = now_ms() - started

        if not risk.passed:
            reason = risk.reasons[0] if risk.reasons else "failed risk checks"
            log.info("skip %s: %s", market.symbol or candidate.key, reason)
            return Decision(
                candidate=candidate,
                market=market,
                risk=risk,
                size_usd=0.0,
                conviction=score,
                act=False,
                reason=reason,
                latency_ms=latency,
            )

        blocked = self.guards.reserve(market.mint, size_usd)
        if blocked:
            log.info("skip %s: %s", market.symbol, blocked)
            return Decision(
                candidate=candidate,
                market=market,
                risk=risk,
                size_usd=0.0,
                conviction=score,
                act=False,
                reason=blocked,
                latency_ms=latency,
            )

        try:
            fill = await self.venue.buy(market, size_usd)
        except Exception:
            self.guards.release(market.mint)
            raise
        latency["execute"] = now_ms() - started
        if fill is None:
            self.guards.release(market.mint)
            return Decision(
                candidate=candidate,
                market=market,
                risk=risk,
                size_usd=size_usd,
                conviction=score,
                act=False,
                reason="order did not fill",
                latency_ms=latency,
            )

        position = self.portfolio.open(fill, market)
        self.guards.record_entry(market.mint, fill.usd)
        self.journal.record_fill(fill)
        self.journal.upsert_position(position)
        log.info(
            "BOUGHT %s $%.2f @ %.10f (%.0fbps slippage, %dms end to end)%s",
            fill.symbol,
            fill.usd,
            fill.price_usd,
            fill.slippage_bps,
            latency["execute"] + latency["feed_lag"],
            " [paper]" if fill.paper else "",
        )
        return Decision(
            candidate=candidate,
            market=market,
            risk=risk,
            size_usd=size_usd,
            conviction=score,
            act=True,
            reason="entered",
            latency_ms=latency,
        )

    # -- exit path -------------------------------------------------------

    async def manage_positions_once(self) -> None:
        for position in self.portfolio.open_positions:
            market = await self.resolver.snapshot(position.mint)
            if market is None:
                # No quote is itself information: a pair that stops resolving
                # has usually had its liquidity removed. Do not silently hold.
                log.warning("%s: no market data on refresh", position.symbol)
                continue
            plan = plan_exit(position, market, self.config.exits)
            if plan is None:
                continue
            qty = self.portfolio.sell_qty(position, plan)
            fill = await self.venue.sell(market, qty)
            if fill is None:
                log.error(
                    "%s: exit (%s) did not fill - retrying next cycle",
                    position.symbol,
                    plan.reason.value,
                )
                continue
            before = position.realized_usd
            self.portfolio.apply_exit(position, fill, plan.reason)
            self.journal.record_fill(fill)
            self.journal.upsert_position(position)
            self.guards.record_exit(position.realized_usd - before, closed=not position.is_open)
            log.info(
                "SOLD %s %.4f @ %.10f (%s) realised $%.2f",
                fill.symbol,
                fill.qty,
                fill.price_usd,
                plan.reason.value,
                position.realized_usd,
            )

    async def _exit_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.manage_positions_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("exit loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.config.exits.poll_seconds / time_scale(),
                )

    # -- lifecycle -------------------------------------------------------

    def _spawn(self, signal: Signal) -> None:
        async def run() -> None:
            async with self._semaphore:
                try:
                    await self.handle_signal(signal)
                except Exception:
                    log.exception("failed handling signal %s", signal.post_id)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        exit_task = asyncio.create_task(self._exit_loop())
        try:
            async for signal in self.watcher:  # type: ignore[attr-defined]
                self._spawn(signal)
        finally:
            # Let in-flight entries finish so nothing is bought without being
            # recorded.
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # A finished feed is not a finished run. Keep the exit loop alive
            # until every position has closed on its own rules, so a replay
            # reports what the strategy actually returned rather than what it
            # happened to be holding when the fixtures ran out.
            await self._drain_positions()
            self._stopping.set()
            exit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exit_task

    async def _drain_positions(self) -> None:
        """Wait for open positions to exit on their own rules, then give up.

        Bounded by the hold timeout plus a margin: every rule that can close a
        position fires within that, so anything still open afterwards is stuck
        and gets flattened by the caller instead.
        """
        deadline = now_ms() + int(self.config.exits.max_hold_seconds * 1000) + 30_000
        while self.portfolio.open_positions and now_ms() < deadline:
            if self._stopping.is_set():
                return
            await asyncio.sleep(self.config.exits.poll_seconds / time_scale())
        if self.portfolio.open_positions:
            log.warning(
                "%d position(s) still open after the drain window",
                len(self.portfolio.open_positions),
            )

    async def liquidate_all(self, reason: ExitReason = ExitReason.MANUAL) -> None:
        """Close every open position. Used on shutdown and by `--flatten`."""
        for position in self.portfolio.open_positions:
            market = await self.resolver.snapshot(position.mint)
            if market is None:
                log.error("%s: cannot flatten, no market data", position.symbol)
                continue
            fill = await self.venue.sell(market, position.qty)
            if fill is None:
                log.error("%s: flatten did not fill", position.symbol)
                continue
            before = position.realized_usd
            self.portfolio.apply_exit(position, fill, reason)
            self.journal.record_fill(fill)
            self.journal.upsert_position(position)
            self.guards.record_exit(position.realized_usd - before, closed=not position.is_open)
            log.info("FLATTENED %s realised $%.2f", fill.symbol, position.realized_usd)
