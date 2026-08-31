# memesnipe

A signal-driven memecoin trading bot: watch specific X accounts, extract the
token they are referencing, check it, size it, buy it, and manage the exit.

It runs in four escalating modes - `replay` (offline, synthetic markets),
`paper` (real signals, simulated fills), `record` (save signals, trade
nothing), and `live` (real funds, behind an explicit arm). Everything it does,
including every trade it declines and why, goes into a SQLite journal.

## Read this before you build on it

Latency is the entire strategy, and you are not going to win on latency.

The bots you are competing with sit on filtered-stream or scraped feeds, run
next to a validator, and submit through private transaction relays. Between
X's own indexing delay, your network hop, the token not being indexed yet, and
transaction landing time, a realistic end-to-end reaction is **1.5-4 seconds**.
The people you are trying to front-run are inside 300ms. On a signal everyone
reads at once, that gap is not a small disadvantage; it is the whole trade.

So the honest expectation is that a Twitter-signal memecoin bot is
**negative-EV by default**. The edge, if you have one, is not speed - it is
having a *source* the crowd is not reading, and exit discipline the crowd does
not have. This project is built accordingly: the paper broker models the costs
that actually kill you, and the exit rules get more attention than the entry
filter.

"I'll buy the rugs too" is not a strategy on its own. Buying a token that will
be rugged is only profitable if you sell before the pull, which is the hard
half. The rug guard here (see **Exits**) is the closest thing to an answer, and
it still loses on any pull faster than one poll interval.

**Run it in `paper` for at least two weeks before funding anything, and read
`mean_feed_lag_ms` in the summary first.** If that number is over ~3s, the
strategy is not viable at your feed quality and no amount of threshold tuning
changes it.

## Install

```bash
cd memesnipe
python -m pip install -e ".[dev]"          # paper and replay
python -m pip install -e ".[dev,solana]"   # adds live execution
```

Python 3.11+.

## The four modes, in the order to use them

```bash
cp config.example.toml config.toml

memesnipe --config config.toml check                          # validate, no network
memesnipe --config config.toml replay --file fixtures/sample_signals.jsonl
memesnipe --config config.toml paper                          # needs MEMESNIPE_X_BEARER
memesnipe --config config.toml record --out signals.jsonl     # save signals, trade nothing
memesnipe --config config.toml live                           # real funds
memesnipe --config config.toml report                         # read the journal
```

Each step adds exactly one source of risk. `replay` adds none - it needs no API
key and no network at all. `paper` adds a real feed. `live` adds your money.

### replay

Runs the entire pipeline offline against recorded signals and a **synthetic**
market simulator. `--speed` accelerates the whole simulation clock together -
feed pacing, price paths, hold timeouts and guard windows - so a 15-minute hold
rule can be exercised in 15 seconds:

```bash
memesnipe --config config.toml replay --file fixtures/sample_signals.jsonl --speed 120
```

This is **not a backtest**. The price paths are invented from a seed, weighted
towards the outcomes that dominate this market (35% rug, 30% bleed out, 20%
chop, 12% runner, 3% moonshot). It tells you nothing about whether your signal
sources are profitable. What it does test - and what is otherwise very hard to
test - is whether your exit rules behave the way you think when a token 10xes
in ninety seconds or has its liquidity pulled underneath you.

Add `--real-markets` to replay recorded signals against live market data
instead. Fills stay simulated regardless: a replay can never send an order.

### paper

Real signals from X, simulated fills. This is the mode that tells you something
real, because the feed lag is your actual feed lag.

```bash
export MEMESNIPE_X_BEARER="..."
memesnipe --config config.toml paper
```

### live

```bash
export MEMESNIPE_X_BEARER="..."
export MEMESNIPE_WALLET_KEY="<base58 secret key>"
export MEMESNIPE_ARM="I_UNDERSTAND_I_CAN_LOSE_EVERYTHING"
memesnipe --config config.toml live
```

Live mode refuses to start without all three, plus `execution.rpc_url`. The arm
variable is deliberately awkward and is checked against that exact string; a
config flag alone is too easy to flip by restoring the wrong file.

The process holds a hot wallet key in memory. **Use a dedicated wallet funded
with only what you intend to risk.** The key is never logged or journalled.

## How it works

```
X account  ->  extract  ->  resolve  ->  risk  ->  size  ->  execute  ->  manage exit
  watcher      mint/         market      checks    convict-   paper or     ladder, trail,
               ticker        + depth               ion         Jupiter      stop, rug guard
```

Signals are handled concurrently - one task each, because the entry path is a
race - while a separate loop manages open positions on a timer, and keeps
running when no signals are arriving.

### Extraction

A pump.fun/Dexscreener/Birdeye link, or a bare mint address, identifies exactly
one token and is trusted (confidence 0.9-1.0). A bare `$TICKER` is a guess and
scores 0.35, because launching a colliding ticker the moment an influencer
posts one is a standard play.

Address candidates are **base58-decoded and length-checked to 32 bytes**, not
just regex-matched - plenty of ordinary words fall in the 32-44 character
range, and chasing one wastes the only seconds that matter. Majors and program
IDs (wSOL, USDC, the token program) are filtered out.

### Resolution

Mints resolve exactly, to their deepest pool, retried while a brand-new pair is
still indexing. Tickers resolve by search and are **refused unless the top pair
leads the runner-up by `ticker_liquidity_ratio` (default 3x)**. Returning
nothing is always better than returning a guess: the wrong mint is a certain
loss, a missed signal is only a missed signal.

### Risk checks

Soft checks (liquidity, pair age, buy/sell balance, FDV) are relaxed by the
`degen` profile. Hard checks are not relaxed by anything:

- the mint and deployer blocklists
- **your own order's price impact**, estimated constant-product. Buying a pool
  so thin that your entry moves it hundreds of bps is not a risk appetite, it
  is a loss on entry and no exit afterwards.

Every check is recorded pass or fail even after the first failure, so the
journal shows *why* a signal was skipped.

### Sizing

Conviction (reference quality x market quality x freshness x your author
weight) scales the **base** size, never the account. A 3x-conviction trade is
three base units, so a losing run shrinks in absolute terms instead of
compounding into the floor. Signals older than 2s are sized *down*, not merely
accepted.

### Exits

Checked in this order on every cycle:

1. **Liquidity pulled** - the pool has lost more than
   `liquidity_drawdown_exit_pct` of the liquidity it had at entry. Sell
   everything, at any price, immediately. This is the rug guard, and it is
   first because it is the only rule where seconds change the outcome from
   "bad trade" to "zero".
2. **Hard stop** - down past `hard_stop_pct` from entry.
3. **Take-profit ladder** - sell a fraction at each multiple, each rung once.
   A gap through two rungs takes both, in order, not twice.
4. **Trailing stop** - armed only once a rung has been taken, so launch
   volatility does not close a position before it has made anything.
5. **Max hold** - time out and free the capital.

An exit that does not fill leaves the position open for the next cycle. A
position is never abandoned.

### Guards

Hard stops that apply in paper and live alike - a limit you have never watched
fire is a limit you have not tested. Max open positions, daily spend cap, daily
loss halt, trades per hour, and one-entry-per-mint.

Entries **reserve** their slot at check time rather than at fill time. Without
that, two signals arriving in the same instant both see the position count from
before either of them filled, and the limits are quietly exceeded by however
many tasks are in flight.

## Paper-fill realism

The paper broker exists to *not* flatter the strategy. It models:

| Cost | Default | Why |
|---|---|---|
| Latency drift | 12 bps / 100ms | The price moves against you while you land |
| Competition | 150 bps | Other bots clear ahead of you |
| Your price impact | constant-product | Depth you consume, both directions |
| Swap fee | 100 bps | Pool fee |
| Fixed cost | $0.12 | Priority fee and gas |
| **Failed sells** | when liquidity is 0 | **Exiting is the hard half** |

It also *declines* entries whose total slippage would exceed your cap, because
a real swap with that cap reverts rather than filling.

These defaults are deliberately pessimistic. Calibrate them from your own live
fills - and if you find yourself tuning them downward because the equity curve
looks nicer, the curve is what is wrong.

## The journal

Every signal, decision and fill lands in SQLite, including declined trades.
Without the declines you cannot answer the only question that matters after a
losing week - whether the filters were too tight or too loose - because the
trades you did not take leave no other trace.

`memesnipe report` prints the summary and the rejection histogram. If one check
accounts for nearly every rejection, that check *is* your strategy, whether you
meant it to be or not.

## Configuration

See `config.example.toml`; every field is commented. Secrets are never in the
file - they come from `MEMESNIPE_X_BEARER`, `MEMESNIPE_WALLET_KEY` and
`MEMESNIPE_ARM`.

## Extending it

The four interfaces are `Watcher`, `Resolver`, `Venue` and the exit rules.

- **Another signal source** (Telegram, Discord, on-chain new-pool events):
  implement `Watcher` - an async iterator of `Signal`. A watcher must never
  raise on a transient upstream failure; losing the feed briefly is survivable,
  crashing with positions open is not, because nothing is left to run the exits.
- **Another chain**: implement `Resolver` and `Venue`. `Chain` already has
  `base` and `ethereum`, and extraction handles EVM addresses.
- **Better safety checks**: a holder-concentration, mint-authority or LP-burn
  check belongs in `risk.py` behind the same `check()` calls, so it shows up in
  the journal like everything else.

## Tests

```bash
python -m pytest tests/ -q
```

65 tests, no network. They cover the parts where being wrong is expensive:
base58 validation, ticker-collision refusal, exit-rule ordering and the
liquidity-pull override, partial-sell cost basis, the guard reservation race,
the live-mode arm gate, and the paper broker's refusal to fill an exit into an
empty pool.

## Legal and practical

Automated trading of unregulated assets, at your own risk, with your own funds.
Check what applies where you live - in some jurisdictions this is a regulated
activity, and trading on material non-public information is illegal regardless
of the asset class. Respect the X API terms for whichever tier you are on.
Nothing here is financial advice.
