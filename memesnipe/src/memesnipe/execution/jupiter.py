"""Live Solana execution through Jupiter.

This is the only module in the project that can lose real money, so it is
deliberately the most conservative one:

* It refuses to construct without `solders`, an RPC URL and a wallet key. There
  is no degraded mode that silently does nothing - a live run that quietly
  no-ops is worse than one that will not start.
* It quotes before every swap and rechecks the quoted price impact against the
  configured cap, even though the risk stage already checked an estimate. The
  estimate uses reported pool liquidity; the quote uses the actual route.
* It confirms the transaction and reports the *executed* amounts from the
  quote, not the requested ones.

The wallet key is read from `MEMESNIPE_WALLET_KEY` (base58 secret key) and is
never logged, echoed, or written to the journal. Use a dedicated wallet funded
with only what you intend to risk; this process holds a hot key.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx

from ..config import ExecutionConfig
from ..models import Fill, Market, Side, now_ms

log = logging.getLogger(__name__)

WALLET_ENV = "MEMESNIPE_WALLET_KEY"
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterVenue:
    paper = False

    def __init__(self, config: ExecutionConfig) -> None:
        try:
            from solders.keypair import Keypair
        except ImportError as error:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "live trading needs the 'solana' extra: pip install 'memesnipe[solana]'"
            ) from error

        secret = os.environ.get(WALLET_ENV)
        if not secret:
            raise RuntimeError(f"{WALLET_ENV} is not set")
        if not config.rpc_url:
            raise RuntimeError("execution.rpc_url is not set")

        self._config = config
        self._keypair = Keypair.from_base58_string(secret)
        self._pubkey = str(self._keypair.pubkey())
        self._quote_client = httpx.AsyncClient(
            base_url=config.jupiter_base, timeout=httpx.Timeout(5.0)
        )
        self._rpc_client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._sol_price_usd = 0.0
        self._sol_price_at_ms = 0
        log.info("live venue armed for wallet %s", self._pubkey)

    # -- pricing ---------------------------------------------------------

    async def _sol_price(self) -> float:
        """SOL/USD, cached briefly. Used only to convert USD sizes to lamports.

        A stale SOL price mis-sizes an order; it does not decide direction, so
        a few seconds of cache is an acceptable trade for not adding a network
        round trip to the hot path.
        """
        if self._sol_price_usd > 0 and now_ms() - self._sol_price_at_ms < 30_000:
            return self._sol_price_usd
        try:
            response = await self._quote_client.get(
                "/price/v2", params={"ids": WSOL_MINT}, timeout=3.0
            )
            response.raise_for_status()
            price = float(response.json()["data"][WSOL_MINT]["price"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            log.warning("SOL price lookup failed: %s", error)
            return self._sol_price_usd  # 0.0 on the first failure, which blocks the trade
        self._sol_price_usd = price
        self._sol_price_at_ms = now_ms()
        return price

    # -- swap plumbing ---------------------------------------------------

    async def _quote(self, input_mint: str, output_mint: str, amount: int) -> dict | None:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(int(self._config.slippage_bps)),
            # Direct routes only. A multi-hop route through a fresh memecoin is
            # slower to land and more likely to fail mid-route.
            "onlyDirectRoutes": "true",
        }
        try:
            response = await self._quote_client.get("/v6/quote", params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("jupiter quote failed (%s -> %s): %s", input_mint, output_mint, error)
            return None

    async def _swap(self, quote: dict) -> str | None:
        from solders.transaction import VersionedTransaction

        try:
            response = await self._quote_client.post(
                "/v6/swap",
                json={
                    "quoteResponse": quote,
                    "userPublicKey": self._pubkey,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": self._config.priority_fee_lamports,
                },
            )
            response.raise_for_status()
            encoded = response.json()["swapTransaction"]
        except (httpx.HTTPError, KeyError, ValueError) as error:
            log.error("jupiter swap build failed: %s", error)
            return None

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(encoded))
        signed = VersionedTransaction(unsigned.message, [self._keypair])
        payload = base64.b64encode(bytes(signed)).decode("ascii")
        try:
            response = await self._rpc_client.post(
                self._config.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        payload,
                        {
                            "encoding": "base64",
                            # Preflight costs a round trip we cannot afford on
                            # entry, and reverts here are expected rather than
                            # exceptional.
                            "skipPreflight": True,
                            "maxRetries": 3,
                        },
                    ],
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.error("sendTransaction failed: %s", error)
            return None
        if "error" in body:
            log.error("sendTransaction rejected: %s", body["error"])
            return None
        return str(body.get("result", "")) or None

    async def _confirm(self, signature: str, timeout_seconds: float = 30.0) -> bool:
        deadline = now_ms() + int(timeout_seconds * 1000)
        while now_ms() < deadline:
            try:
                response = await self._rpc_client.post(
                    self._config.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[signature], {"searchTransactionHistory": False}],
                    },
                )
                response.raise_for_status()
                value = (response.json().get("result") or {}).get("value") or [None]
                status = value[0]
                if status:
                    if status.get("err"):
                        log.error("transaction %s reverted: %s", signature, status["err"])
                        return False
                    if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                        return True
            except (httpx.HTTPError, ValueError, IndexError) as error:
                log.debug("confirmation poll failed: %s", error)
            await asyncio.sleep(0.5)
        log.error("transaction %s not confirmed in %.0fs", signature, timeout_seconds)
        return False

    def _check_quote_impact(self, quote: dict) -> bool:
        try:
            impact_bps = float(quote.get("priceImpactPct", 0.0)) * 10_000.0
        except (TypeError, ValueError):
            return False
        if impact_bps > self._config.slippage_bps:
            log.warning(
                "aborting: routed price impact %.0fbps exceeds the %.0fbps cap",
                impact_bps,
                self._config.slippage_bps,
            )
            return False
        return True

    # -- venue interface -------------------------------------------------

    async def buy(self, market: Market, usd: float) -> Fill | None:
        sol_price = await self._sol_price()
        if sol_price <= 0:
            log.error("refusing to buy %s without a SOL price", market.symbol)
            return None
        lamports = int(usd / sol_price * LAMPORTS_PER_SOL)
        if lamports <= 0:
            return None
        quote = await self._quote(WSOL_MINT, market.mint, lamports)
        if quote is None or not self._check_quote_impact(quote):
            return None
        signature = await self._swap(quote)
        if signature is None or not await self._confirm(signature):
            return None

        decimals = int(market.extra.get("decimals", 6))
        qty = int(quote.get("outAmount", 0)) / (10**decimals)
        if qty <= 0:
            return None
        price = usd / qty
        return Fill(
            mint=market.mint,
            symbol=market.symbol,
            side=Side.BUY,
            qty=qty,
            price_usd=price,
            usd=usd,
            fee_usd=self._config.priority_fee_lamports / LAMPORTS_PER_SOL * sol_price,
            slippage_bps=float(quote.get("priceImpactPct", 0.0)) * 10_000.0,
            ts_ms=now_ms(),
            tx_ref=signature,
            paper=False,
        )

    async def sell(self, market: Market, qty: float) -> Fill | None:
        sol_price = await self._sol_price()
        if sol_price <= 0:
            return None
        decimals = int(market.extra.get("decimals", 6))
        amount = int(qty * (10**decimals))
        if amount <= 0:
            return None
        quote = await self._quote(market.mint, WSOL_MINT, amount)
        if quote is None:
            return None
        # No impact check on the way out. Refusing to sell because the exit is
        # expensive is how a bad position becomes a total one.
        signature = await self._swap(quote)
        if signature is None or not await self._confirm(signature):
            return None

        lamports_out = int(quote.get("outAmount", 0))
        usd_out = lamports_out / LAMPORTS_PER_SOL * sol_price
        return Fill(
            mint=market.mint,
            symbol=market.symbol,
            side=Side.SELL,
            qty=qty,
            price_usd=usd_out / qty if qty else 0.0,
            usd=usd_out,
            fee_usd=self._config.priority_fee_lamports / LAMPORTS_PER_SOL * sol_price,
            slippage_bps=float(quote.get("priceImpactPct", 0.0)) * 10_000.0,
            ts_ms=now_ms(),
            tx_ref=signature,
            paper=False,
        )

    async def aclose(self) -> None:
        await self._quote_client.aclose()
        await self._rpc_client.aclose()
