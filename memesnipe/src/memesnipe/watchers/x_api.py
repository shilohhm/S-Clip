"""X (Twitter) API v2 watcher, in polling and filtered-stream flavours.

Speed is the whole product here, so the two modes are not equivalent:

* `stream` holds one long-lived connection and receives a post as X pushes it,
  typically a few hundred milliseconds after it is created. It needs
  filtered-stream access on your X plan.
* `poll` hits each watched account's timeline on a timer. Its floor is half the
  poll interval on average and the full interval at worst, on top of X's own
  indexing delay. It works on cheaper tiers, and it is measurably slower.

Either way the watcher records `created_at` from X and stamps arrival locally,
so `Signal.feed_lag_ms` tells you honestly how far behind the post you are. Read
that number before you fund anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import httpx

from ..config import TwitterConfig
from ..extract import matches_filters
from ..models import Signal, now_ms

log = logging.getLogger(__name__)

BEARER_ENV = "MEMESNIPE_X_BEARER"
API_BASE = "https://api.x.com/2"


def _parse_created_at(value: str | None) -> int:
    if not value:
        return now_ms()
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return now_ms()


class XApiWatcher:
    def __init__(
        self,
        config: TwitterConfig,
        *,
        mode: str = "poll",
        on_error: Callable[[str, Exception], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        token = os.environ.get(BEARER_ENV)
        if not token and client is None:
            raise RuntimeError(f"{BEARER_ENV} is not set; needed for twitter.mode={mode!r}")
        self._config = config
        self._mode = mode
        self._on_error = on_error
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=90.0),
        )
        self._accounts = {
            account.handle.lstrip("@").lower(): account for account in config.accounts
        }
        self._queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=1_000)
        self._tasks: list[asyncio.Task[None]] = []
        self._seen: set[str] = set()

    # -- helpers ---------------------------------------------------------

    def _report(self, where: str, error: Exception) -> None:
        log.warning("x_api %s: %s", where, error)
        if self._on_error is not None:
            self._on_error(where, error)

    def _emit(self, handle: str, payload: dict[str, object]) -> None:
        post_id = str(payload.get("id", ""))
        if not post_id or post_id in self._seen:
            return
        self._seen.add(post_id)
        text = str(payload.get("text", ""))
        account = self._accounts.get(handle.lower())
        if account is not None and not matches_filters(text, account.must_match):
            return
        signal = Signal(
            source="x",
            author=handle,
            post_id=post_id,
            text=text,
            created_at_ms=_parse_created_at(payload.get("created_at")),  # type: ignore[arg-type]
            url=f"https://x.com/{handle}/status/{post_id}",
            author_weight=account.weight if account else 1.0,
        )
        try:
            self._queue.put_nowait(signal)
        except asyncio.QueueFull:
            # Dropping the oldest is right: in a burst the newest post is the
            # only one still tradeable.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(signal)

    async def resolve_user_ids(self) -> dict[str, str]:
        """Map handles to numeric ids, which the timeline endpoints need."""
        handles = list(self._accounts)
        resolved: dict[str, str] = {}
        for start in range(0, len(handles), 100):
            batch = handles[start : start + 100]
            response = await self._client.get("/users/by", params={"usernames": ",".join(batch)})
            response.raise_for_status()
            for user in response.json().get("data", []):
                resolved[user["username"].lower()] = user["id"]
        missing = set(handles) - set(resolved)
        if missing:
            log.warning("could not resolve handles: %s", sorted(missing))
        return resolved

    # -- poll mode -------------------------------------------------------

    async def _poll_account(self, handle: str, user_id: str) -> None:
        account = self._accounts[handle]
        since_id: str | None = None
        backoff = 1.0
        # Stagger starts so N accounts do not fire their requests in lockstep
        # and burn the rate limit in one instant of each interval.
        await asyncio.sleep(hash(handle) % 1000 / 1000.0 * account.poll_seconds)
        while True:
            try:
                params: dict[str, str | int] = {
                    "max_results": 5,
                    "tweet.fields": "created_at",
                    "exclude": "replies,retweets",
                }
                if since_id:
                    params["since_id"] = since_id
                response = await self._client.get(f"/users/{user_id}/tweets", params=params)
                if response.status_code == 429:
                    reset = response.headers.get("x-rate-limit-reset")
                    wait = 60.0
                    if reset and reset.isdigit():
                        wait = max(1.0, int(reset) - now_ms() / 1000.0)
                    log.warning("rate limited on @%s, sleeping %.1fs", handle, wait)
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                backoff = 1.0
                posts = response.json().get("data") or []
                # Oldest first, so `since_id` advances monotonically.
                for post in reversed(posts):
                    since_id = str(post["id"])
                    self._emit(handle, post)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report(f"poll @{handle}", error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            await asyncio.sleep(account.poll_seconds)

    # -- stream mode -----------------------------------------------------

    async def sync_stream_rules(self) -> None:
        """Make the server-side rules match the configured accounts exactly."""
        response = await self._client.get("/tweets/search/stream/rules")
        response.raise_for_status()
        existing = response.json().get("data") or []
        wanted = {f"from:{handle} -is:retweet -is:reply" for handle in self._accounts}
        stale = [rule["id"] for rule in existing if rule["value"] not in wanted]
        current = {rule["value"] for rule in existing}
        if stale:
            await self._client.post("/tweets/search/stream/rules", json={"delete": {"ids": stale}})
        missing = wanted - current
        if missing:
            # Tag rules with the handle so the stream payload identifies the
            # author without a second lookup per post.
            add = [
                {"value": value, "tag": value.split(":", 1)[1].split(" ", 1)[0]}
                for value in sorted(missing)
            ]
            result = await self._client.post("/tweets/search/stream/rules", json={"add": add})
            result.raise_for_status()

    async def _run_stream(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.sync_stream_rules()
                params = {
                    "tweet.fields": "created_at",
                    "expansions": "author_id",
                    "user.fields": "username",
                }
                async with self._client.stream(
                    "GET", "/tweets/search/stream", params=params
                ) as response:
                    response.raise_for_status()
                    backoff = 1.0
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue  # keep-alive heartbeat
                        payload = json.loads(line)
                        data = payload.get("data")
                        if not data:
                            continue
                        users = payload.get("includes", {}).get("users", [])
                        handle = users[0]["username"] if users else ""
                        if not handle:
                            tags = [
                                rule.get("tag", "") for rule in payload.get("matching_rules", [])
                            ]
                            handle = tags[0] if tags else "unknown"
                        self._emit(handle, data)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report("stream", error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # -- iteration -------------------------------------------------------

    async def __aiter__(self) -> AsyncIterator[Signal]:
        if self._mode == "stream":
            self._tasks.append(asyncio.create_task(self._run_stream()))
        else:
            user_ids = await self.resolve_user_ids()
            for handle, user_id in user_ids.items():
                self._tasks.append(asyncio.create_task(self._poll_account(handle, user_id)))
        if not self._tasks:
            raise RuntimeError("no accounts to watch; add [[twitter.accounts]] entries")
        try:
            while True:
                yield await self._queue.get()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._owns_client:
            await self._client.aclose()
