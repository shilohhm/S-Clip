"""The resolver contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Candidate, Market


@runtime_checkable
class Resolver(Protocol):
    async def resolve(self, candidate: Candidate) -> Market | None:
        """Return the best market for a candidate, or None if it cannot be
        identified confidently. Returning None is always preferable to
        returning a guess: the wrong mint is a guaranteed loss, a missed
        signal is only a missed signal."""
        ...

    async def snapshot(self, mint: str) -> Market | None:
        """Re-read a market that is already held, for exit management."""
        ...

    async def aclose(self) -> None: ...
