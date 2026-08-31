"""The watcher contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..models import Signal


@runtime_checkable
class Watcher(Protocol):
    """Yields signals until cancelled or the source is exhausted.

    A watcher must never raise on a transient upstream failure. Losing the feed
    for a few seconds is survivable; crashing the process while positions are
    open is not, because nothing is left to run the exit rules.
    """

    def __aiter__(self) -> AsyncIterator[Signal]: ...

    async def aclose(self) -> None: ...
