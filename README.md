# Poly Arbi: Polymarket CSV Paper Bot

This is a paper-first Python bot based on the JetFadil account analysis in `jetfadil_polymarket_report.md`.

The default strategy is now adaptive two-sided inventory:

1. Watch live BTC/ETH 5-minute Up/Down markets.
2. Track the live Binance WebSocket trade price versus the market's opening/reference price.
3. Buy both outcomes only when the pair cost is acceptable.
4. Complete the missing side first and keep directional bias small.
5. Cap one-sided starter exposure and rebalance late instead of stacking unfinished positions.
6. Hold through resolution and decompose PnL into paired hedge PnL and unpaired directional PnL.

This repository intentionally ships in paper mode. It does not place real orders.
The live execution boundary exists only as a disabled placeholder.

## Install

```powershell
pip install -r requirements.txt
```

## Scan Current Markets

```powershell
python -m polymarket_tilt_bot scan-once --assets BTC,ETH
```

## Run Paper Trading

```powershell
python -m polymarket_tilt_bot run-paper --assets BTC,ETH --cycles 120 --poll-seconds 5 --db paper_trades_csv --balance 1000 --max-market-notional 100 --max-single-fill 10
```

Use `--cycles 0` to run continuously until you stop it with `Ctrl+C`:

```powershell
python -m polymarket_tilt_bot run-paper --assets BTC,ETH --cycles 0 --poll-seconds 5 --db paper_trades_csv --balance 1000 --max-market-notional 100 --max-single-fill 10
```

Useful strategy/storage flags:

```text
--strategy-mode hedged-mm   # default, adaptive two-sided inventory with small bias
--strategy-mode pair-only   # stricter, targets equal Up/Down cost
--strategy-mode current     # old hedged-tilt behavior
--storage csv               # default, writes separate CSV files in the --db folder
--storage sqlite            # legacy SQLite support
```

One-sided risk controls are enabled by default:

```text
--max-unpaired-notional             # defaults to min(max single fill, 25% of market cap)
--min-order-notional 1              # reject paper orders/fills below Polymarket's $1 minimum
--starter-entry-cutoff-seconds 90   # stop opening fresh flat positions after this elapsed time
--completion-pair-cost-mid 1.05     # missing-side repair tolerance after 120s
--completion-pair-cost-late 1.08    # missing-side repair tolerance after 180s
--bad-regime-window 20              # recent resolved markets to inspect
--bad-regime-min-completion-rate .5 # pause new entries below this completion rate
--disable-bad-regime-guard          # keep completion rules but disable new-entry pause
```

## Resolution And Daily PnL

The bot resolves paper positions from real Polymarket market outcomes via Gamma after the 5-minute market has ended. In CSV mode it records the winning outcome and PnL decomposition in `resolutions.csv`.

Print a report for all resolved markets:

```powershell
python -m polymarket_tilt_bot daily-report --db paper_trades_csv
```

Print a UTC-date filtered report:

```powershell
python -m polymarket_tilt_bot daily-report --db paper_trades_csv --date 2026-04-26
```

The report includes total cost, total PnL, ROI, winner, paired hedge PnL, and unpaired tilt PnL per market.
It also prints both-sided completion rate, both-sided PnL, one-sided PnL, failed one-sided markets, and PnL buckets by last-fill timing and total cost.

Every cycle, the bot logs the current Polymarket odds and your paper position:

```text
state btc-updown-5m-... odds up_bid=0.520 up_ask=0.540 down_bid=0.460 down_ask=0.480 signal=Up p_up=0.631 pos_up=10.00 pos_down=8.00 cost=9.50 unpaired=Up 2.00
```

It also logs paper account state every cycle:

```text
account balance=940.00 equity=1003.24 realized_pnl=10.34 unrealized_pnl=-7.10 reserved=70.34 open_positions=1
```

Definitions:

- `balance`: starting simulation balance + realized PnL - capital reserved in unresolved paper positions.
- `equity`: starting simulation balance + realized PnL + mark-to-market unrealized PnL.
- `reserved`: paper cost still locked in unresolved markets.
- `unrealized_pnl`: estimated open-position value using current Polymarket best bids.

The bot records:

- `markets`
- `signals`
- `orderbook_snapshots`
- `fills`
- `resolutions`

into the SQLite database you pass with `--db`.
into CSV files in the folder you pass with `--db`.

## Strategy Summary

The default `hedged-mm` strategy implements the lesson from paper testing:

```text
complete both sides
avoid one-sided inventory
rebalance late
use momentum only as a small inventory bias
```

It skips flat markets when `Up ask + Down ask` is too expensive, prioritizes the missing side after the first fill, blocks repeated same-side stacking above the one-sided cap, and avoids chasing very expensive odds unless needed to complete inventory.

## Project Layout

```text
polymarket_tilt_bot/
  clients.py          # Polymarket Gamma/CLOB and public price clients
  market_scanner.py   # Finds active BTC/ETH 5-minute markets
  strategy.py         # Hedged MM / pair-only / legacy strategy logic
  paper_broker.py     # Conservative simulated fills against live asks
  ledger.py           # CSV-first market/signal/fill/resolution logging
  resolution.py       # Reads resolved winners from Gamma
  live_executor.py    # Disabled live-trading boundary
  runner.py           # CLI
tests/
```

## Important Data Warning

Polymarket states these markets resolve from Chainlink BTC/USD or ETH/USD data streams. The default price client now uses Binance public market-data WebSocket trade streams at `wss://data-stream.binance.vision`, which are faster and do not require API keys, but still are not the exact Chainlink resolution feed. Before live trading, replace or validate this against the exact resolution feed or a professional low-latency source aligned to Chainlink.

No `.env` file is required for paper mode.

## Safety Defaults

The defaults are deliberately small:

- max market notional: `$100`
- max single simulated fill: `$10`
- no new entries in the final 15 seconds
- live execution is not implemented

For small-balance paper tests, `$1-$2` single fills are supported when the order would still meet Polymarket's minimum order notional and share size. For example, `$2` at `0.20` odds buys about `10` shares, while `$2` at `0.80` odds buys only `2.5` shares and may be skipped. Any order below `$1` is skipped even when the share count would be large enough.

## Tests

```powershell
pytest -q
```
