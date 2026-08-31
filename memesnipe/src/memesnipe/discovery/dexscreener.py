"""Resolve candidates against Dexscreener's public API.

Two very different jobs share this class. Resolving a *mint* is exact: one
address, whichever of its pairs is deepest. Resolving a bare *$TICKER* is a
search, and a search over memecoin tickers is adversarial by construction -
launching a colliding ticker the moment an influencer posts one is a standard
play. So a ticker match is only accepted when the top pair leads the runner-up
by `ticker_liquidity_ratio`; otherwise the candidate is dropped.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import DiscoveryConfig
from ..models import Candidate, Chain, Market, now_ms

log = logging.getLogger(__name__)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def market_from_pair(pair: dict[str, object], chain: Chain) -> Market | None:
    base = pair.get("baseToken") or {}
    if not isinstance(base, dict) or not base.get("address"):
        return None
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") if isinstance(txns, dict) else {}
    m5 = m5 if isinstance(m5, dict) else {}
    return Market(
        mint=str(base["address"]),
        chain=chain,
        symbol=str(base.get("symbol", "")).upper(),
        name=str(base.get("name", "")),
        price_usd=_as_float(pair.get("priceUsd")),
        liquidity_usd=_as_float(liquidity.get("usd") if isinstance(liquidity, dict) else 0),
        fdv_usd=_as_float(pair.get("fdv")),
        volume_5m_usd=_as_float(volume.get("m5") if isinstance(volume, dict) else 0),
        buys_5m=_as_int(m5.get("buys")),
        sells_5m=_as_int(m5.get("sells")),
        pair_address=str(pair.get("pairAddress", "")),
        dex=str(pair.get("dexId", "")),
        # Missing creation time means "unknown age", and the risk stage treats
        # unknown age as too old rather than brand new.
        pair_created_at_ms=_as_int(pair.get("pairCreatedAt"), default=0),
        extra={"url": pair.get("url", "")},
    )


class DexscreenerResolver:
    def __init__(self, config: DiscoveryConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.dexscreener_base,
            timeout=httpx.Timeout(config.timeout_ms / 1000.0),
            headers={"accept": "application/json"},
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _pairs_on_chain(self, payload: dict[str, object], chain: Chain) -> list[dict[str, object]]:
        pairs = payload.get("pairs") or []
        if not isinstance(pairs, list):
            return []
        return [
            pair
            for pair in pairs
            if isinstance(pair, dict) and str(pair.get("chainId", "")).lower() == chain.value
        ]

    async def _by_mint(self, mint: str, chain: Chain) -> Market | None:
        """Deepest pool for an exact address, retried while the pair indexes.

        A token minted seconds ago is often not in the index yet, and that is
        precisely the window this bot exists to trade, so a miss is retried
        rather than treated as "does not exist".
        """
        for attempt in range(self._config.retries):
            try:
                payload = await self._get(f"/latest/dex/tokens/{mint}")
            except (httpx.HTTPError, ValueError) as error:
                log.debug("dexscreener mint lookup failed (%s): %s", mint, error)
                payload = {}
            pairs = self._pairs_on_chain(payload, chain)
            if pairs:
                best = max(
                    pairs,
                    key=lambda pair: _as_float((pair.get("liquidity") or {}).get("usd")),
                )
                return market_from_pair(best, chain)
            if attempt + 1 < self._config.retries:
                await asyncio.sleep(self._config.retry_delay_ms / 1000.0)
        return None

    async def _by_ticker(self, symbol: str, chain: Chain) -> Market | None:
        try:
            payload = await self._get("/latest/dex/search", params={"q": f"${symbol}"})
        except (httpx.HTTPError, ValueError) as error:
            log.debug("dexscreener search failed (%s): %s", symbol, error)
            return None
        exact = [
            pair
            for pair in self._pairs_on_chain(payload, chain)
            if str((pair.get("baseToken") or {}).get("symbol", "")).upper() == symbol.upper()
        ]
        if not exact:
            return None
        exact.sort(
            key=lambda pair: _as_float((pair.get("liquidity") or {}).get("usd")), reverse=True
        )
        top = _as_float((exact[0].get("liquidity") or {}).get("usd"))
        runner_up = (
            _as_float((exact[1].get("liquidity") or {}).get("usd")) if len(exact) > 1 else 0.0
        )
        if runner_up > 0 and top < runner_up * self._config.ticker_liquidity_ratio:
            log.info(
                "ambiguous ticker $%s: top liquidity %.0f vs runner-up %.0f, skipping",
                symbol,
                top,
                runner_up,
            )
            return None
        return market_from_pair(exact[0], chain)

    async def resolve(self, candidate: Candidate) -> Market | None:
        chain = candidate.chain
        if candidate.mint:
            return await self._by_mint(candidate.mint, chain)
        if candidate.symbol:
            return await self._by_ticker(candidate.symbol, chain)
        return None

    async def snapshot(self, mint: str) -> Market | None:
        try:
            payload = await self._get(f"/latest/dex/tokens/{mint}")
        except (httpx.HTTPError, ValueError):
            return None
        pairs = self._pairs_on_chain(payload, self._config.chain)
        if not pairs:
            return None
        best = max(pairs, key=lambda pair: _as_float((pair.get("liquidity") or {}).get("usd")))
        market = market_from_pair(best, self._config.chain)
        if market is not None:
            object.__setattr__(market, "fetched_at_ms", now_ms())
        return market

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
