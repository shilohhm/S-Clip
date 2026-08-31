"""Replay recorded posts from a JSONL file.

This is how you test the pipeline without an API key and without risking
anything: record a session with `memesnipe record`, or hand-write fixtures, then
replay them at any speed. Every stage downstream behaves exactly as it does
live, which makes a replay run a real regression test of the strategy rather
than a smoke test of the plumbing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from ..models import Signal, now_ms, sleep_virtual


class ReplayWatcher:
    def __init__(self, path: str | Path, speed: float = 1.0) -> None:
        self._path = Path(path)
        # Kept for the record; the acceleration itself lives in the global
        # simulation clock so every stage sees the same time.
        self._speed = speed

    def _load(self) -> list[Signal]:
        signals: list[Signal] = []
        with self._path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                record = json.loads(line)
                signals.append(
                    Signal(
                        source=record.get("source", "replay"),
                        author=record["author"],
                        post_id=str(record["post_id"]),
                        text=record["text"],
                        created_at_ms=int(record["created_at_ms"]),
                        received_at_ms=int(record.get("received_at_ms", record["created_at_ms"])),
                        url=record.get("url", ""),
                        author_weight=float(record.get("author_weight", 1.0)),
                        extra=record.get("extra", {}),
                    )
                )
        signals.sort(key=lambda signal: signal.created_at_ms)
        return signals

    async def __aiter__(self) -> AsyncIterator[Signal]:
        signals = self._load()
        if not signals:
            return
        origin = signals[0].created_at_ms
        started_ms = now_ms()
        for signal in signals:
            # Preserve the original spacing between posts so bursts stay
            # bursts; a strategy that only works when signals arrive one at a
            # time should fail here rather than in production. The spacing is
            # in simulation time, so an accelerated clock compresses the whole
            # session uniformly rather than only the gaps.
            wait_ms = (signal.created_at_ms - origin) - (now_ms() - started_ms)
            if wait_ms > 0:
                await sleep_virtual(wait_ms / 1000.0)
            # Restamp arrival so replayed lag is the recorded lag, not the age
            # of the fixture file.
            yield Signal(
                source=signal.source,
                author=signal.author,
                post_id=signal.post_id,
                text=signal.text,
                created_at_ms=signal.created_at_ms,
                received_at_ms=signal.created_at_ms
                + (signal.received_at_ms - signal.created_at_ms),
                url=signal.url,
                author_weight=signal.author_weight,
                extra=signal.extra,
            )

    async def aclose(self) -> None:
        return None
