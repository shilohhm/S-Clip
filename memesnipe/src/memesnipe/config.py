"""Configuration: a TOML file for behaviour, environment variables for secrets.

Secrets never live in the TOML, so the config file can be committed and diffed
alongside the results it produced. That matters more than it sounds: without it
you cannot tell whether last week's numbers came from these thresholds or the
ones you edited on Tuesday.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .models import Chain

# Live trading is gated on this exact value in the environment. A config flag
# alone is too easy to flip by editing the wrong file or restoring a backup.
ARM_ENV = "MEMESNIPE_ARM"
ARM_VALUE = "I_UNDERSTAND_I_CAN_LOSE_EVERYTHING"


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class WatchAccount:
    """One X account to watch, and how much its posts are worth."""

    handle: str
    weight: float = 1.0
    # Only act on posts matching one of these (case-insensitive substrings or
    # /regex/). Empty means every post from the account is a candidate.
    must_match: list[str] = field(default_factory=list)
    poll_seconds: float = 2.0


@dataclass(slots=True)
class TwitterConfig:
    # "poll" works on any paid X tier; "stream" needs filtered-stream access
    # but is meaningfully faster because there is no polling interval to lose.
    mode: str = "poll"
    accounts: list[WatchAccount] = field(default_factory=list)
    # Fixtures for `replay` mode; ignored elsewhere.
    replay_path: str = ""
    replay_speed: float = 1.0
    # Discard anything already older than this when it reaches us. A post we
    # see 30s late is not an edge, it is a bag.
    max_feed_lag_ms: int = 8_000


@dataclass(slots=True)
class DiscoveryConfig:
    chain: Chain = Chain.SOLANA
    dexscreener_base: str = "https://api.dexscreener.com"
    # Give up on resolving a token rather than arriving last.
    timeout_ms: int = 1_500
    # Retry a just-launched token that has not been indexed yet.
    retries: int = 3
    retry_delay_ms: int = 400
    # A bare $TICKER can match many pairs; require this much of a liquidity
    # lead over the runner-up before trusting the match.
    ticker_liquidity_ratio: float = 3.0


@dataclass(slots=True)
class RiskConfig:
    # "strict" and "degen" only change the soft checks. The hard caps below
    # apply in both, because they are what stops one bad night ending the run.
    profile: str = "strict"
    min_liquidity_usd: float = 8_000.0
    max_liquidity_usd: float = 5_000_000.0
    min_pair_age_seconds: float = 20.0
    max_pair_age_seconds: float = 1_800.0
    min_buys_5m: int = 15
    # A pool with buys and no sells is the classic honeypot shape.
    require_sells: bool = True
    max_fdv_usd: float = 20_000_000.0
    # Refuse a trade whose own size would move the price more than this.
    max_price_impact_bps: float = 400.0
    blocked_mints: list[str] = field(default_factory=list)
    blocked_deployers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SizingConfig:
    base_size_usd: float = 25.0
    max_size_usd: float = 100.0
    # Conviction multiplies the base size; this bounds how far it can.
    max_conviction_multiple: float = 3.0


@dataclass(slots=True)
class ExitConfig:
    # (multiple_of_entry, fraction_of_remaining_to_sell)
    take_profit_ladder: list[tuple[float, float]] = field(
        default_factory=lambda: [(2.0, 0.5), (4.0, 0.5), (10.0, 0.5)]
    )
    # Trail from the peak once the first rung is hit.
    trailing_stop_pct: float = 0.35
    hard_stop_pct: float = 0.55
    max_hold_seconds: float = 900.0
    # The rug guard: liquidity falling this far below entry means leave now,
    # at any price. It is the difference between a bad trade and a zero.
    liquidity_drawdown_exit_pct: float = 0.40
    poll_seconds: float = 3.0


@dataclass(slots=True)
class ExecutionConfig:
    # "paper" simulates fills; "live" sends real transactions.
    mode: str = "paper"
    venue: str = "jupiter"
    slippage_bps: float = 1_500.0
    priority_fee_lamports: int = 200_000
    rpc_url: str = ""
    jupiter_base: str = "https://quote-api.jup.ag"
    # Paper-fill realism knobs. Defaults are deliberately pessimistic; tune
    # them from your own live fills rather than downward because the paper
    # equity curve looks nicer.
    paper_latency_ms: int = 900
    paper_price_drift_bps_per_100ms: float = 12.0
    paper_competition_bps: float = 150.0
    paper_swap_fee_bps: float = 100.0
    paper_fixed_cost_usd: float = 0.12


@dataclass(slots=True)
class GuardConfig:
    """Hard stops. These apply in every profile and every mode."""

    max_open_positions: int = 5
    max_daily_loss_usd: float = 250.0
    max_daily_spend_usd: float = 1_000.0
    max_trades_per_hour: int = 30
    # Never buy the same mint twice in one run, however good it looks.
    dedupe_mints: bool = True


@dataclass(slots=True)
class Config:
    twitter: TwitterConfig = field(default_factory=TwitterConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    guards: GuardConfig = field(default_factory=GuardConfig)
    db_path: str = "memesnipe.sqlite3"
    log_level: str = "INFO"

    @property
    def is_live(self) -> bool:
        return self.execution.mode == "live"

    def require_arm(self) -> None:
        """Refuse to trade real money without the explicit environment arm.

        Called once at startup, before any watcher connects, so an
        unintentionally live config fails loudly and immediately.
        """
        if not self.is_live:
            return
        if os.environ.get(ARM_ENV) != ARM_VALUE:
            raise ConfigError(
                f"execution.mode is 'live' but {ARM_ENV} is not set to {ARM_VALUE!r}. "
                "Refusing to trade real funds."
            )
        if not self.execution.rpc_url:
            raise ConfigError("live mode needs execution.rpc_url")
        if not os.environ.get("MEMESNIPE_WALLET_KEY"):
            raise ConfigError("live mode needs MEMESNIPE_WALLET_KEY in the environment")


def _build(cls: Any, data: dict[str, Any]) -> Any:
    """Build one config section from a TOML table.

    Conversions are explicit rather than driven by the dataclass field types:
    `from __future__ import annotations` stores those types as strings, so
    reflecting over them silently does nothing - which is exactly how a
    `chain = "solana"` string ends up compared against `Chain.SOLANA` and
    failing a validation it should pass.
    """
    known = {field.name for field in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown keys in [{cls.__name__}]: {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for name, raw in data.items():
        if name == "chain":
            try:
                kwargs[name] = Chain(str(raw).lower())
            except ValueError as error:
                supported = [chain.value for chain in Chain]
                raise ConfigError(f"unsupported chain {raw!r}; supported: {supported}") from error
        elif name == "accounts":
            kwargs[name] = [_build(WatchAccount, item) for item in raw]
        elif name == "take_profit_ladder":
            kwargs[name] = [(float(multiple), float(fraction)) for multiple, fraction in raw]
        else:
            kwargs[name] = raw
    return cls(**kwargs)


_SECTIONS = {
    "twitter": TwitterConfig,
    "discovery": DiscoveryConfig,
    "risk": RiskConfig,
    "sizing": SizingConfig,
    "exits": ExitConfig,
    "execution": ExecutionConfig,
    "guards": GuardConfig,
}


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cfg = Config()
    for key, value in raw.items():
        if key in _SECTIONS:
            setattr(cfg, key, _build(_SECTIONS[key], value))
        elif key in {"db_path", "log_level"}:
            setattr(cfg, key, value)
        else:
            raise ConfigError(f"unknown config section: {key}")
    if cfg.discovery.chain is not Chain.SOLANA and cfg.execution.venue == "jupiter":
        raise ConfigError("the jupiter venue only serves solana; set execution.venue")
    return cfg
