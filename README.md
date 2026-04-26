# Poly Arbi: Polymarket Hedged-Tilt Paper Bot

This is a paper-first Python bot based on the JetFadil account analysis in `jetfadil_polymarket_report.md`.

The inferred strategy is a hedged 5-minute crypto candle tilt:

1. Watch live BTC/ETH 5-minute Up/Down markets.
2. Track the live Binance WebSocket trade price versus the market's opening/reference price.
3. Buy both outcomes in small clips when the pair cost is acceptable.
4. Keep the larger unpaired exposure on the side the live candle favors.
5. Hold through resolution and decompose PnL into paired hedge PnL and unpaired directional PnL.

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
python -m polymarket_tilt_bot run-paper --assets BTC,ETH --cycles 120 --poll-seconds 5 --db paper_trades.sqlite --balance 1000 --max-market-notional 100 --max-single-fill 10
```

Use `--cycles 0` to run continuously until you stop it with `Ctrl+C`:

```powershell
python -m polymarket_tilt_bot run-paper --assets BTC,ETH --cycles 0 --poll-seconds 5 --db paper_trades.sqlite --balance 1000 --max-market-notional 100 --max-single-fill 10
```

Every cycle, the bot logs the current Polymarket odds and your paper position:

```text
state btc-updown-5m-... odds up_bid=0.520 up_ask=0.540 down_bid=0.460 down_ask=0.480 signal=Up p_up=0.631 pos_up=10.00 pos_down=8.00 cost=9.50 unpaired=Up 2.00
```

The bot records:

- `markets`
- `signals`
- `orderbook_snapshots`
- `fills`
- `resolutions`

into the SQLite database you pass with `--db`.

## Strategy Summary

The bot implements the observed JetFadil-style pattern:

```text
paired hedge = min(Up shares, Down shares)
unpaired tilt = abs(Up shares - Down shares)
profit target = make the unpaired side correct often enough to overcome hedge cost
```

It avoids a hedge if `Up ask + Down ask` is too expensive, and it avoids new entries near market close by default.

## Project Layout

```text
polymarket_tilt_bot/
  clients.py          # Polymarket Gamma/CLOB and public price clients
  market_scanner.py   # Finds active BTC/ETH 5-minute markets
  strategy.py         # Hedged tilt signal and order-intent logic
  paper_broker.py     # Conservative simulated fills against live asks
  ledger.py           # SQLite market/signal/fill/resolution logging
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

## Tests

```powershell
pytest -q
```
