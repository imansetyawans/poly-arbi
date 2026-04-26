from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Literal

from .clients import ApiError, ClobClient, CoinGeckoPriceClient, GammaClient, HttpClient
from .config import BotConfig, RiskConfig, RuntimeConfig, StrategyConfig
from .ledger import PositionBook, SQLiteLedger
from .market_scanner import MarketScanner
from .models import Market
from .paper_broker import PaperBroker
from .price_window import PriceWindow
from .resolution import ResolutionClient
from .strategy import HedgedTiltStrategy


LOGGER = logging.getLogger("polymarket_tilt_bot")


class PaperTradingBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.http = HttpClient(config.runtime)
        self.gamma = GammaClient(self.http)
        self.clob = ClobClient(self.http)
        self.prices = CoinGeckoPriceClient(self.http)
        self.scanner = MarketScanner(self.gamma)
        self.resolver = ResolutionClient(self.gamma)
        self.strategy = HedgedTiltStrategy(config.strategy, config.risk)
        self.broker = PaperBroker(slippage_bps=5.0)
        self.positions = PositionBook()
        self.ledger = SQLiteLedger(config.runtime.database_path)
        self.window = PriceWindow()

    def close(self) -> None:
        self.ledger.close()

    def run(self) -> None:
        LOGGER.info("starting paper bot: assets=%s cycles=%s db=%s", self.config.runtime.assets, self.config.runtime.cycles, self.config.runtime.database_path)
        try:
            for cycle in range(self.config.runtime.cycles):
                self.step()
                LOGGER.info("cycle %s/%s complete", cycle + 1, self.config.runtime.cycles)
                if cycle + 1 < self.config.runtime.cycles:
                    time.sleep(self.config.runtime.poll_seconds)
        finally:
            self.close()

    def step(self) -> None:
        now = time.time()
        markets = self.scanner.scan(self.config.runtime.assets, now)
        for asset in self.config.runtime.assets:
            try:
                tick = self.prices.get_tick(asset)
                self.window.add(tick)
            except Exception as exc:
                LOGGER.warning("price tick failed for %s: %s", asset, exc)

        for market in markets:
            tick = self.window.latest(market.asset)
            if tick is None:
                continue
            self.ledger.record_market(market, now)
            try:
                self._trade_market(market, tick.timestamp)
            except ApiError as exc:
                LOGGER.warning("market %s skipped: %s", market.slug, exc)

        self._try_resolve_finished_positions()

    def _trade_market(self, market: Market, timestamp: float) -> None:
        tick = self.window.latest(market.asset)
        if tick is None:
            return
        books = self.clob.get_books_for_market(market)
        position = self.positions.get(market)
        signal = self.strategy.build_signal(market, tick, self.window)
        self.ledger.record_signal(signal, timestamp)
        intents = self.strategy.propose_orders(market, position, signal, books)
        for intent in intents:
            fill = self.broker.execute(market, intent, books[intent.outcome], timestamp)
            if fill is None:
                continue
            self.positions.add_fill(fill)
            self.ledger.record_fill(fill)
            LOGGER.info(
                "paper fill %s %s %.2f shares @ %.3f notional %.2f",
                market.slug,
                fill.outcome,
                fill.size,
                fill.price,
                fill.notional,
            )

    def _try_resolve_finished_positions(self) -> None:
        now = time.time()
        for position in self.positions.all_positions():
            if position.total_cost <= 0:
                continue
            market = self._market_from_position(position.market_slug)
            if market is None or now < market.end_ts + 20:
                continue
            winner = self.resolver.get_winner(market)
            if winner is None:
                continue
            result = self.ledger.record_resolution(position, winner, now)
            LOGGER.info("resolved %s winner=%s pnl=%.2f", position.market_slug, winner, result["pnl"])

    def _market_from_position(self, slug: str) -> Market | None:
        asset: Literal["BTC", "ETH"] = "ETH" if slug.startswith("eth-") else "BTC"
        event = self.gamma.get_event_by_slug(slug)
        return self.gamma.parse_crypto_market(event, asset) if event else None


def parse_assets(raw: str) -> tuple[Literal["BTC", "ETH"], ...]:
    assets: list[Literal["BTC", "ETH"]] = []
    for part in raw.split(","):
        value = part.strip().upper()
        if value not in {"BTC", "ETH"}:
            raise argparse.ArgumentTypeError("assets must be BTC, ETH, or BTC,ETH")
        assets.append(value)  # type: ignore[arg-type]
    return tuple(assets)


def build_config(args: argparse.Namespace) -> BotConfig:
    runtime = RuntimeConfig(
        assets=args.assets,
        poll_seconds=args.poll_seconds,
        cycles=args.cycles,
        database_path=args.db,
    )
    risk = RiskConfig(
        starting_balance=args.balance,
        max_market_notional=args.max_market_notional,
        max_single_fill_notional=args.max_single_fill,
    )
    strategy = StrategyConfig()
    return BotConfig(runtime=runtime, risk=risk, strategy=strategy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-first Polymarket hedged-tilt bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-paper", help="Run paper trading against live markets")
    run.add_argument("--assets", type=parse_assets, default=("BTC", "ETH"), help="BTC, ETH, or BTC,ETH")
    run.add_argument("--cycles", type=int, default=12)
    run.add_argument("--poll-seconds", type=float, default=5.0)
    run.add_argument("--db", default="paper_trades.sqlite")
    run.add_argument("--balance", type=float, default=1_000.0)
    run.add_argument("--max-market-notional", type=float, default=100.0)
    run.add_argument("--max-single-fill", type=float, default=10.0)

    scan = sub.add_parser("scan-once", help="Print currently tradable crypto 5m markets")
    scan.add_argument("--assets", type=parse_assets, default=("BTC", "ETH"), help="BTC, ETH, or BTC,ETH")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "scan-once":
        runtime = RuntimeConfig(assets=args.assets)
        http = HttpClient(runtime)
        scanner = MarketScanner(GammaClient(http))
        for market in scanner.scan(args.assets):
            print(f"{market.asset} {market.slug} orders={market.accepting_orders} up={market.up_token} down={market.down_token}")
        return 0

    if args.command == "run-paper":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True) if Path(args.db).parent != Path(".") else None
        bot = PaperTradingBot(build_config(args))
        bot.run()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
