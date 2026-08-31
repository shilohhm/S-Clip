"""Command line entry point.

    memesnipe check    --config config.toml     validate, no network, no orders
    memesnipe replay   --config config.toml     offline dry run from fixtures
    memesnipe paper    --config config.toml     live signals, simulated fills
    memesnipe record   --config config.toml     save live signals for replay
    memesnipe live     --config config.toml     real money, gated behind an arm
    memesnipe report   --config config.toml     read the journal

The order above is the order to use them in. Each step adds exactly one source
of risk: `replay` adds nothing, `paper` adds a real feed, `record` adds
nothing, `live` adds your money. Skipping straight to the end is how people
find out their feed lag was nine seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal as signal_module
import sys
from pathlib import Path

from .config import ARM_ENV, ARM_VALUE, Config, ConfigError, load_config
from .discovery import build_resolver
from .engine import Engine
from .execution import build_venue
from .models import ExitReason, set_time_scale
from .risk import effective_thresholds
from .storage import Journal
from .watchers import build_watcher

log = logging.getLogger("memesnipe")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run_engine(config: Config, *, simulated_markets: bool) -> int:
    config.require_arm()
    journal = Journal(config.db_path)
    resolver = build_resolver(config, simulated=simulated_markets)
    venue = build_venue(config)
    watcher = build_watcher(config)
    engine = Engine(config, watcher=watcher, resolver=resolver, venue=venue, journal=journal)
    # Carry forward mints already traded so a restart does not re-enter
    # everything the previous run already decided on.
    engine.guards.traded_mints |= journal.traded_mints()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    banner = "PAPER" if venue.paper else "*** LIVE - REAL FUNDS ***"
    log.info(
        "%s | %s profile | %d accounts | base $%.2f, max $%.2f",
        banner,
        config.risk.profile,
        len(config.twitter.accounts),
        config.sizing.base_size_usd,
        config.sizing.max_size_usd,
    )

    runner = asyncio.create_task(engine.run())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
        if not runner.done():
            log.info("shutting down: flattening open positions")
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        # Never leave a position open with nothing watching it.
        with contextlib.suppress(Exception):
            await engine.liquidate_all(ExitReason.MANUAL)
        await resolver.aclose()
        await venue.aclose()
        _print_summary(journal)
        journal.close()
    return 0


async def _record(config: Config, out_path: Path) -> int:
    """Save live signals to JSONL without evaluating or trading anything."""
    watcher = build_watcher(config)
    count = 0
    log.info("recording to %s - ctrl-c to stop", out_path)
    try:
        with out_path.open("a", encoding="utf-8") as handle:
            async for sig in watcher:  # type: ignore[attr-defined]
                handle.write(
                    json.dumps(
                        {
                            "source": sig.source,
                            "author": sig.author,
                            "post_id": sig.post_id,
                            "text": sig.text,
                            "created_at_ms": sig.created_at_ms,
                            "received_at_ms": sig.received_at_ms,
                            "url": sig.url,
                            "author_weight": sig.author_weight,
                        }
                    )
                    + "\n"
                )
                handle.flush()
                count += 1
                log.info("recorded %d: @%s (%dms lag)", count, sig.author, sig.feed_lag_ms)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await watcher.aclose()  # type: ignore[attr-defined]
    log.info("recorded %d signals", count)
    return 0


def _print_summary(journal: Journal) -> None:
    summary = journal.summary()
    print("\n" + "=" * 62)
    print("RUN SUMMARY")
    print("=" * 62)
    for key, value in summary.items():
        print(f"  {key.replace('_', ' '):<26} {value}")
    rejections = journal.rejection_reasons()
    if rejections:
        print("\n  why signals were skipped")
        for reason, hits in rejections:
            print(f"    {hits:>4}x  {reason}")
    print("=" * 62)
    lag = summary.get("mean_feed_lag_ms", 0)
    if isinstance(lag, (int, float)) and lag > 3_000:
        print(
            f"\n  WARNING: mean feed lag is {lag:.0f}ms. At that delay you are\n"
            "  buying after the bots that saw the post first. Fix the feed\n"
            "  before funding anything.\n"
        )


def _cmd_check(config: Config) -> int:
    thresholds = effective_thresholds(config.risk)
    print(f"config OK | chain={config.discovery.chain.value} profile={config.risk.profile}")
    print(f"  execution      {config.execution.mode} via {config.execution.venue}")
    handles = [account.handle for account in config.twitter.accounts]
    print(f"  accounts       {handles or 'NONE CONFIGURED'}")
    print(f"  sizing         base ${config.sizing.base_size_usd} max ${config.sizing.max_size_usd}")
    print(
        f"  guards         {config.guards.max_open_positions} open, "
        f"${config.guards.max_daily_spend_usd:,.0f}/day spend, "
        f"${config.guards.max_daily_loss_usd:,.0f}/day loss halt"
    )
    print(f"  entry filter   {thresholds}")
    print(
        f"  exits          ladder {config.exits.take_profit_ladder}, "
        f"trail {config.exits.trailing_stop_pct:.0%}, stop {config.exits.hard_stop_pct:.0%}, "
        f"timeout {config.exits.max_hold_seconds:.0f}s"
    )
    if config.is_live:
        import os

        armed = os.environ.get(ARM_ENV) == ARM_VALUE
        print(f"  LIVE MODE      arm environment variable {'set' if armed else 'NOT SET'}")
    if not config.twitter.accounts and config.twitter.mode != "replay":
        print("\n  no [[twitter.accounts]] configured - nothing would be watched")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memesnipe", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="path to the TOML config")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate the config and print what it would do")

    replay = sub.add_parser("replay", help="offline dry run against recorded or fixture signals")
    replay.add_argument("--file", help="JSONL of recorded signals (overrides the config)")
    replay.add_argument(
        "--speed",
        type=float,
        default=60.0,
        help="simulation speed multiplier; scales the feed, prices and exit timers together",
    )
    replay.add_argument(
        "--real-markets",
        action="store_true",
        help="resolve against live market data instead of the simulator",
    )

    sub.add_parser("paper", help="live signals, simulated fills")

    record = sub.add_parser("record", help="save live signals to JSONL without trading")
    record.add_argument("--out", default="signals.jsonl")

    sub.add_parser("live", help="real funds - requires the arm environment variable")
    sub.add_parser("report", help="summarise the journal")

    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    except ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2

    _setup_logging(config.log_level)

    if args.command == "check":
        return _cmd_check(config)

    if args.command == "report":
        journal = Journal(config.db_path)
        _print_summary(journal)
        for fill in journal.recent_fills():
            print(
                f"  {fill['side']:<4} {fill['symbol']:<10} "
                f"${fill['usd']:>9.2f} @ {fill['price_usd']:.10f} "
                f"{fill['slippage_bps']:>6.0f}bps {'paper' if fill['paper'] else 'LIVE'}"
            )
        journal.close()
        return 0

    simulated_markets = False
    if args.command == "replay":
        config.twitter.mode = "replay"
        if args.file:
            config.twitter.replay_path = args.file
        if args.speed <= 0:
            print("--speed must be positive", file=sys.stderr)
            return 2
        config.twitter.replay_speed = args.speed
        set_time_scale(args.speed)
        # A replay must never be able to send an order, whatever the config says.
        config.execution.mode = "paper"
        simulated_markets = not args.real_markets
        if not config.twitter.replay_path:
            print("replay needs --file or twitter.replay_path", file=sys.stderr)
            return 2
    elif args.command == "paper":
        config.execution.mode = "paper"
    elif args.command == "live":
        config.execution.mode = "live"

    if args.command == "record":
        try:
            return asyncio.run(_record(config, Path(args.out)))
        except KeyboardInterrupt:
            return 0

    try:
        return asyncio.run(_run_engine(config, simulated_markets=simulated_markets))
    except ConfigError as error:
        print(f"refusing to start: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
