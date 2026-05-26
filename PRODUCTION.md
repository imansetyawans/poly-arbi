# Production Runbook

This runbook keeps the current profitable JetFadil strategy logic unchanged and hardens only the execution boundary, audit trail, and launch process.

## What Changed

- `run-paper` now writes `run_manifest.json` beside the ledger so every result folder captures the exact runtime, risk, and strategy config.
- Every proposed order execution attempt is written to `orders.csv` or the SQLite `orders` table with mode, status, limit, notional, and reason.
- `run-live` is available, but it is double-armed: it requires `POLYBOT_ENABLE_LIVE_TRADING=YES` and `--i-understand-live-risk`.
- Live execution uses the official `py-clob-client-v2` SDK instead of hand-rolled signing.
- The strategy classes and their decision logic are unchanged.

## Install

```powershell
pip install -r requirements.txt
```

## Live Environment

Live trading needs credentials. They are intentionally not stored in code.

Copy `.env.example` to `.env.live`, then fill the private key. The file also contains the profitable v4 strategy/runtime config, so you do not need to keep the long command in your shell history.

```powershell
Copy-Item .env.example .env.live
notepad .env.live
```

Or set this in the shell that starts the bot:

```powershell
$env:POLYBOT_ENABLE_LIVE_TRADING="YES"
$env:POLYMARKET_PRIVATE_KEY="..."
```

`CLOB_API_KEY`, `CLOB_SECRET`, and `CLOB_PASS_PHRASE` are optional cached L2 API credentials. If they are not supplied, the bot uses `py-clob-client-v2` to derive them from `POLYMARKET_PRIVATE_KEY` at startup. If you do supply cached L2 credentials, provide all three.

Optional proxy/safe wallet settings:

```powershell
$env:POLYMARKET_SIGNATURE_TYPE="1"
$env:POLYMARKET_FUNDER_ADDRESS="0x..."
$env:POLYMARKET_CHAIN_ID="137"
```

Run the preflight before live trading:

```powershell
python -m polymarket_tilt_bot preflight-live --env-file .env.live --verbose
```

## Paper Production Rehearsal

Use the same env-backed profitable v4 shape first:

```powershell
python -m polymarket_tilt_bot run-paper --env-file .env.live --db paper_trades_jetfadil_prod_rehearsal --cycles 2
```

## Live Command

Use the env-backed live command:

```powershell
python -m polymarket_tilt_bot run-live --env-file .env.live --i-understand-live-risk
```

Default live order type is `FAK`: fill available liquidity immediately and cancel the rest. It uses a small live tick buffer but never submits above the strategy's own limit price.

```powershell
POLYBOT_LIVE_ORDER_TYPE=FAK
POLYBOT_LIVE_TICK_SIZE=0.01
POLYBOT_LIVE_PRICE_TICKS=1
```

Live account logs use the authenticated CLOB balance endpoint:

```text
live account clob_balance=42.50 clob_allowance=100.00 local_ledger_baseline=300.00 ...
```

`clob_balance` is your real Polymarket collateral balance. `local_ledger_baseline` comes from `POLYBOT_BALANCE` and is only for local PnL bookkeeping.

## Before Leaving It Running

1. Confirm `preflight-live` says `Ready: True`.
2. Run at least one short `run-paper --cycles 2` rehearsal with the exact flags.
3. Confirm the result folder contains `run_manifest.json`, `orders.csv`, `fills.csv`, and `resolutions.csv`.
4. Start live with a short cycle count first, for example `--cycles 2`.
5. Inspect `orders.csv` and `fills.csv` before switching to `--cycles 0`.

## Kill Switch

Stop the process with `Ctrl+C`. To prevent a restart from live trading, remove the live arming env var:

```powershell
Remove-Item Env:\POLYBOT_ENABLE_LIVE_TRADING
```
