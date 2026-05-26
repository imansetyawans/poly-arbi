# Poly Arbi: Polymarket JetFadil Bot

Python bot for Polymarket crypto 5-minute Up/Down markets. The current production setup keeps the profitable JetFadil strategy config in environment variables, supports paper rehearsal, and has a guarded live CLOB execution path.

The strategy logic is still the same profitable v4 shape. The production changes are around launch safety, env configuration, order execution, and audit logs.

## Quick Start

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create your local production env file:

```powershell
Copy-Item .env.example .env.live
notepad .env.live
```

Fill at least this value before live trading:

```text
POLYMARKET_PRIVATE_KEY=your_private_key_here
```

Then check the live setup:

```powershell
python -m polymarket_tilt_bot preflight-live --env-file .env.live --verbose
```

Run a short paper rehearsal using the same env-backed config:

```powershell
python -m polymarket_tilt_bot run-paper --env-file .env.live --db paper_trades_jetfadil_prod_rehearsal --cycles 2
```

Run a short live smoke test:

```powershell
python -m polymarket_tilt_bot run-live --env-file .env.live --i-understand-live-risk --cycles 2
```

Run live continuously after the smoke test looks correct:

```powershell
python -m polymarket_tilt_bot run-live --env-file .env.live --i-understand-live-risk
```

Stop the bot with `Ctrl+C`.

## Recommended Live Env

`.env.example` already contains the latest JetFadil v4 production config from the profitable paper run:

```text
POLYBOT_ASSETS=BTC
POLYBOT_CYCLES=0
POLYBOT_POLL_SECONDS=3
POLYBOT_DB=live_trades_jetfadil_prod
POLYBOT_STORAGE=csv
POLYBOT_STRATEGY_MODE=jetfadil
POLYBOT_BALANCE=300
POLYBOT_MAX_MARKET_NOTIONAL=18
POLYBOT_MAX_SINGLE_FILL=3
POLYBOT_MAX_UNPAIRED_NOTIONAL=2
POLYBOT_PROFIT_EXPANSION_PAIR_COST=0.98
POLYBOT_JETFADIL_ENTRY_PAIR_COST=0.98
POLYBOT_JETFADIL_EARLY_ENTRY_PAIR_COST=0.95
POLYBOT_JETFADIL_DEEP_VALUE_PAIR_COST=0.95
POLYBOT_JETFADIL_MIN_ENTRY_SECONDS=60
POLYBOT_JETFADIL_MIN_CONFIDENCE=0.05
POLYBOT_JETFADIL_STRONG_TILT_CONFIDENCE=0.50
POLYBOT_JETFADIL_MAX_DIRECTIONAL_BIAS=0.10
POLYBOT_JETFADIL_STARTER_ENTRY_CUTOFF_SECONDS=240
POLYBOT_JETFADIL_CORE_PAIR_FRACTION=0.80
POLYBOT_JETFADIL_PRE_LATE_EXPANSION_PAIR_COST=0.85
POLYBOT_JETFADIL_LATE_EXPANSION_SECONDS=240
POLYBOT_LIVE_ORDER_TYPE=FAK
POLYBOT_LIVE_TICK_SIZE=0.01
POLYBOT_LIVE_PRICE_TICKS=1
```

Important notes:

- `POLYBOT_BALANCE` is the bot's local accounting baseline. Your real spend is controlled by the wallet funds plus `POLYBOT_MAX_MARKET_NOTIONAL`, `POLYBOT_MAX_SINGLE_FILL`, and `POLYBOT_MAX_UNPAIRED_NOTIONAL`.
- Command-line flags override env values, so `--cycles 2` is useful for smoke tests even when `POLYBOT_CYCLES=0`.
- `CLOB_API_KEY`, `CLOB_SECRET`, and `CLOB_PASS_PHRASE` are optional cached Polymarket L2 credentials. If they are not set, the bot derives them from `POLYMARKET_PRIVATE_KEY` at startup.
- `.env.live` should stay local. Do not commit it.

## Live Order Behavior

Live mode uses Polymarket CLOB orders through `py-clob-client-v2`.

Default production execution:

```text
POLYBOT_LIVE_ORDER_TYPE=FAK
POLYBOT_LIVE_TICK_SIZE=0.01
POLYBOT_LIVE_PRICE_TICKS=1
```

That means the bot tries to fill available liquidity immediately and cancels anything unfilled. It may add a small 1-tick buffer to the current ask for better matching, but it never submits a price above the strategy's own limit price. The code rejects live tick buffers above 2.

If volatility moves the book above the strategy limit, the order is skipped instead of chasing.

## Safety Gates

Live trading is double-armed:

1. `.env.live` must contain `POLYBOT_ENABLE_LIVE_TRADING=YES`.
2. The command must include `--i-understand-live-risk`.

Before leaving the bot running:

1. Run `preflight-live` and confirm `Ready: True`.
2. Run paper with `--cycles 2`.
3. Run live with `--cycles 2`.
4. Inspect `orders.csv`, `fills.csv`, `resolutions.csv`, and `run_manifest.json`.
5. Start continuous live only after the audit files look correct.

To disarm live trading in the current shell:

```powershell
Remove-Item Env:\POLYBOT_ENABLE_LIVE_TRADING
```

## Useful Commands

Scan current BTC and ETH markets:

```powershell
python -m polymarket_tilt_bot scan-once --assets BTC,ETH
```

Run paper without live keys:

```powershell
python -m polymarket_tilt_bot run-paper --assets BTC --cycles 0 --poll-seconds 3 --db paper_trades_jetfadil_v4 --storage csv --strategy-mode jetfadil --balance 300 --max-market-notional 18 --max-single-fill 3 --max-unpaired-notional 2 --profit-expansion-pair-cost 0.98 --jetfadil-entry-pair-cost 0.98 --jetfadil-early-entry-pair-cost 0.95 --jetfadil-deep-value-pair-cost 0.95 --jetfadil-min-entry-seconds 60 --jetfadil-min-confidence 0.05 --jetfadil-strong-tilt-confidence 0.50 --jetfadil-max-directional-bias 0.10 --jetfadil-starter-entry-cutoff-seconds 240 --jetfadil-core-pair-fraction 0.80 --jetfadil-pre-late-expansion-pair-cost 0.85 --jetfadil-late-expansion-seconds 240
```

Backfill missing market resolutions:

```powershell
python -m polymarket_tilt_bot resolve-missing --db paper_trades_jetfadil_v4 --storage csv
```

Print the PnL report:

```powershell
python -m polymarket_tilt_bot daily-report --db paper_trades_jetfadil_v4 --storage csv
```

## Output Files

CSV storage writes these files inside the folder named by `POLYBOT_DB` or `--db`:

```text
run_manifest.json
markets.csv
signals.csv
orderbook_snapshots.csv
orders.csv
fills.csv
resolutions.csv
```

`orders.csv` records every live or paper execution attempt, including status, limit price, notional, and reason. `fills.csv` records matched fills. `run_manifest.json` captures the runtime, risk, and strategy config used for that run.

## Strategy Modes

```text
--strategy-mode jetfadil   # production JetFadil-style paired bursts with controlled tilt
--strategy-mode hedged-mm  # adaptive two-sided inventory with small bias
--strategy-mode pair-only  # stricter equal Up/Down cost targeting
--strategy-mode current    # old hedged-tilt behavior
```

The production env uses `jetfadil`.

## Project Layout

```text
polymarket_tilt_bot/
  clients.py          # Polymarket Gamma/CLOB and public price clients
  market_scanner.py   # Finds active BTC/ETH 5-minute markets
  strategy.py         # Hedged MM, pair-only, legacy, and JetFadil strategy logic
  paper_broker.py     # Conservative simulated fills against live asks
  ledger.py           # CSV/SQLite market, signal, order, fill, and resolution logging
  resolution.py       # Reads resolved winners from Gamma
  live_executor.py    # Guarded signed live CLOB execution
  runner.py           # CLI and env-backed runtime config
tests/
```

## Data Warning

Polymarket crypto 5-minute markets resolve from Chainlink BTC/USD or ETH/USD data streams. The default signal client uses Binance public market-data WebSocket trade streams, which are fast and require no API keys, but they are not the exact Chainlink resolution feed. Validate this before increasing live size.

## More Detail

Read `PRODUCTION.md` for the full launch checklist and kill-switch notes.

## Tests

```powershell
pytest -q
python -m compileall -q polymarket_tilt_bot tests
```
