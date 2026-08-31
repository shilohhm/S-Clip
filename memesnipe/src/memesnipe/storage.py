"""SQLite journal of every signal, decision and fill.

Everything the engine does is written down, including the trades it declined
and why. Without the declines you cannot answer the only question that matters
after a losing week - whether the filters were too tight or too loose - because
the trades you did not take leave no other trace.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Decision, Fill, Position, Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    source TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT,
    created_at_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    feed_lag_ms INTEGER NOT NULL,
    UNIQUE(source, post_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    mint TEXT,
    symbol TEXT,
    kind TEXT NOT NULL,
    acted INTEGER NOT NULL,
    size_usd REAL NOT NULL,
    conviction REAL NOT NULL,
    risk_score REAL NOT NULL,
    risk_passed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    market_json TEXT,
    latency_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price_usd REAL NOT NULL,
    usd REAL NOT NULL,
    fee_usd REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    ts_ms INTEGER NOT NULL,
    tx_ref TEXT NOT NULL,
    paper INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    mint TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    cost_usd REAL NOT NULL,
    realized_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    entry_price_usd REAL NOT NULL,
    peak_price_usd REAL NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    closed_at_ms INTEGER,
    exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_mint ON fills(mint);
CREATE INDEX IF NOT EXISTS idx_decisions_post ON decisions(post_id);
"""


class Journal:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        # WAL keeps a `report` run readable while the engine is still trading.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)

    def record_signal(self, signal: Signal) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO signals "
            "(post_id, source, author, text, url, created_at_ms, received_at_ms, feed_lag_ms) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                signal.post_id,
                signal.source,
                signal.author,
                signal.text,
                signal.url,
                signal.created_at_ms,
                signal.received_at_ms,
                signal.feed_lag_ms,
            ),
        )

    def record_decision(self, decision: Decision) -> None:
        market = decision.market
        self._connection.execute(
            "INSERT INTO decisions "
            "(post_id, mint, symbol, kind, acted, size_usd, conviction, risk_score, "
            " risk_passed, reason, checks_json, market_json, latency_json, created_at_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision.candidate.signal.post_id,
                decision.candidate.mint,
                market.symbol if market else decision.candidate.symbol,
                decision.candidate.kind,
                int(decision.act),
                decision.size_usd,
                decision.conviction,
                decision.risk.score,
                int(decision.risk.passed),
                decision.reason,
                json.dumps(decision.risk.checks),
                json.dumps(_market_row(market)) if market else None,
                json.dumps(decision.latency_ms),
                decision.candidate.signal.received_at_ms,
            ),
        )

    def record_fill(self, fill: Fill) -> None:
        self._connection.execute(
            "INSERT INTO fills "
            "(mint, symbol, side, qty, price_usd, usd, fee_usd, slippage_bps, ts_ms, tx_ref, paper)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill.mint,
                fill.symbol,
                fill.side.value,
                fill.qty,
                fill.price_usd,
                fill.usd,
                fill.fee_usd,
                fill.slippage_bps,
                fill.ts_ms,
                fill.tx_ref,
                int(fill.paper),
            ),
        )

    def upsert_position(self, position: Position) -> None:
        self._connection.execute(
            "INSERT INTO positions "
            "(mint, symbol, qty, cost_usd, realized_usd, fees_usd, entry_price_usd, "
            " peak_price_usd, opened_at_ms, closed_at_ms, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET "
            " qty=excluded.qty, cost_usd=excluded.cost_usd, realized_usd=excluded.realized_usd, "
            " fees_usd=excluded.fees_usd, peak_price_usd=excluded.peak_price_usd, "
            " closed_at_ms=excluded.closed_at_ms, exit_reason=excluded.exit_reason",
            (
                position.mint,
                position.symbol,
                position.qty,
                position.cost_usd,
                position.realized_usd,
                position.fees_usd,
                position.entry_price_usd,
                position.peak_price_usd,
                position.opened_at_ms,
                position.closed_at_ms,
                position.exit_reason.value if position.exit_reason else None,
            ),
        )

    def traded_mints(self) -> set[str]:
        rows = self._connection.execute("SELECT DISTINCT mint FROM positions").fetchall()
        return {row["mint"] for row in rows}

    def summary(self) -> dict[str, object]:
        """Aggregate performance. Reads only closed, realised numbers."""
        rows = self._connection.execute(
            "SELECT symbol, mint, cost_usd, realized_usd, fees_usd, qty, exit_reason, "
            "       opened_at_ms, closed_at_ms FROM positions"
        ).fetchall()
        closed = [row for row in rows if row["closed_at_ms"] is not None]
        realized = sum(row["realized_usd"] for row in rows)
        fees = sum(row["fees_usd"] for row in rows)
        wins = [row for row in closed if row["realized_usd"] > 0]
        decisions = self._connection.execute(
            "SELECT COUNT(*) AS total, SUM(acted) AS acted FROM decisions"
        ).fetchone()
        lag = self._connection.execute(
            "SELECT AVG(feed_lag_ms) AS mean, MAX(feed_lag_ms) AS worst FROM signals"
        ).fetchone()
        slippage = self._connection.execute(
            "SELECT AVG(slippage_bps) AS mean FROM fills WHERE side='buy'"
        ).fetchone()
        return {
            "positions": len(rows),
            "closed": len(closed),
            "open": len(rows) - len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "realized_usd": round(realized, 2),
            "fees_usd": round(fees, 2),
            "signals_evaluated": decisions["total"] or 0,
            "signals_acted": decisions["acted"] or 0,
            "mean_feed_lag_ms": round(lag["mean"] or 0.0, 1),
            "worst_feed_lag_ms": lag["worst"] or 0,
            "mean_entry_slippage_bps": round(slippage["mean"] or 0.0, 1),
        }

    def rejection_reasons(self, limit: int = 15) -> list[tuple[str, int]]:
        """Why signals were skipped, most common first. This is the tuning
        dial: if one check accounts for every rejection, that check is your
        strategy whether you meant it to be or not."""
        rows = self._connection.execute(
            "SELECT reason, COUNT(*) AS hits FROM decisions WHERE acted=0 "
            "GROUP BY reason ORDER BY hits DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(row["reason"], row["hits"]) for row in rows]

    def recent_fills(self, limit: int = 20) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM fills ORDER BY ts_ms DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._connection.close()


def _market_row(market: object) -> dict[str, object]:
    row = asdict(market)  # type: ignore[call-overload]
    row.pop("extra", None)
    row["chain"] = str(row["chain"])
    return row
