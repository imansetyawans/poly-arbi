from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Literal

from .clients import ApiError, BinanceWebSocketPriceClient, ClobClient, GammaClient, HttpClient
from .config import BotConfig, RiskConfig, RuntimeConfig, StrategyConfig
from .ledger import CsvLedger, PositionBook, SQLiteLedger
from .market_scanner import MarketScanner
from .models import Market
from .paper_broker import PaperBroker
from .price_window import PriceWindow
from .resolution import ResolutionClient
from .strategy import HedgedMarketMakerStrategy, HedgedTiltStrategy


LOGGER = logging.getLogger("polymarket_tilt_bot")


class PaperTradingBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.http = HttpClient(config.runtime)
        self.gamma = GammaClient(self.http)
        self.clob = ClobClient(self.http)
        self.prices = BinanceWebSocketPriceClient(config.runtime.assets)
        self.scanner = MarketScanner(self.gamma)
        self.resolver = ResolutionClient(self.gamma)
        if config.strategy.strategy_mode in {"hedged-mm", "pair-only"}:
            self.strategy = HedgedMarketMakerStrategy(config.strategy, config.risk)
        else:
            self.strategy = HedgedTiltStrategy(config.strategy, config.risk)
        self.broker = PaperBroker(slippage_bps=5.0, min_order_notional=config.risk.min_order_notional)
        self.positions = PositionBook()
        self.ledger = CsvLedger(config.runtime.database_path) if config.runtime.storage_mode == "csv" else SQLiteLedger(config.runtime.database_path)
        self.window = PriceWindow()

    def close(self) -> None:
        self.ledger.close()

    def run(self) -> None:
        LOGGER.info("starting paper bot: assets=%s cycles=%s db=%s", self.config.runtime.assets, self.config.runtime.cycles, self.config.runtime.database_path)
        try:
            cycle = 0
            while self.config.runtime.cycles <= 0 or cycle < self.config.runtime.cycles:
                try:
                    self.step()
                except ApiError as exc:
                    LOGGER.warning("cycle skipped after recoverable API error: %s", exc)
                except Exception:
                    LOGGER.exception("cycle skipped after unexpected error")
                cycle += 1
                total = "unlimited" if self.config.runtime.cycles <= 0 else str(self.config.runtime.cycles)
                LOGGER.info("cycle %s/%s complete", cycle, total)
                if self.config.runtime.cycles <= 0 or cycle < self.config.runtime.cycles:
                    time.sleep(self.config.runtime.poll_seconds)
        except KeyboardInterrupt:
            LOGGER.info("stopped by user")
        finally:
            self._try_resolve_finished_positions()
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
        self._log_account_state()

    def _trade_market(self, market: Market, timestamp: float) -> None:
        tick = self.window.latest(market.asset)
        if tick is None:
            return
        books = self.clob.get_books_for_market(market)
        position = self.positions.get(market)
        signal = self.strategy.build_signal(market, tick, self.window)
        self.ledger.record_signal(signal, timestamp)
        self.ledger.record_orderbooks(market, books, timestamp)
        self._log_market_state(market, books, position, signal)
        starter_allowed = not self._bad_regime_pause() if position.total_cost <= 0 else True
        intents = self.strategy.propose_orders(market, position, signal, books, allow_starter=starter_allowed)
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

    def _bad_regime_pause(self) -> bool:
        strategy = self.config.strategy
        if not strategy.bad_regime_guard_enabled:
            return False
        resolved, completion_rate = self.ledger.recent_completion_rate(strategy.bad_regime_window)
        return resolved >= strategy.bad_regime_window and completion_rate < strategy.bad_regime_min_completion_rate

    def _try_resolve_finished_positions(self) -> None:
        now = time.time()
        seen = {position.market_slug: position for position in self.positions.all_positions()}
        for stored_position in self.ledger.get_unresolved_positions():
            seen.setdefault(stored_position.market_slug, stored_position)
        for position in seen.values():
            if position.total_cost <= 0:
                continue
            if self.ledger.is_resolved(position.market_slug):
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

    def _log_market_state(self, market: Market, books, position, signal) -> None:
        up_book = books["Up"]
        down_book = books["Down"]
        LOGGER.info(
            "state %s odds up_bid=%s up_ask=%s down_bid=%s down_ask=%s signal=%s p_up=%.3f pos_up=%.2f pos_down=%.2f cost=%.2f unpaired=%s %.2f",
            market.slug,
            _fmt(up_book.best_bid),
            _fmt(up_book.best_ask),
            _fmt(down_book.best_bid),
            _fmt(down_book.best_ask),
            signal.direction,
            signal.probability_up,
            position.shares["Up"],
            position.shares["Down"],
            position.total_cost,
            position.unpaired_side,
            position.unpaired_shares,
        )

    def _log_account_state(self) -> None:
        positions = self._open_positions()
        reserved = sum(position.total_cost for position in positions)
        unrealized = self._estimate_unrealized_pnl(positions)
        realized = self.ledger.realized_pnl()
        balance = self.config.risk.starting_balance + realized - reserved
        equity = self.config.risk.starting_balance + realized + unrealized
        LOGGER.info(
            "account balance=%.2f equity=%.2f realized_pnl=%.2f unrealized_pnl=%.2f reserved=%.2f open_positions=%s",
            balance,
            equity,
            realized,
            unrealized,
            reserved,
            len(positions),
        )

    def _open_positions(self):
        open_positions = []
        seen = {position.market_slug: position for position in self.positions.all_positions()}
        for stored_position in self.ledger.get_unresolved_positions():
            seen.setdefault(stored_position.market_slug, stored_position)
        for position in seen.values():
            if position.total_cost > 0 and not self.ledger.is_resolved(position.market_slug):
                open_positions.append(position)
        return open_positions

    def _estimate_unrealized_pnl(self, positions) -> float:
        total = 0.0
        for position in positions:
            market = self._market_from_position(position.market_slug)
            if market is None:
                total -= position.total_cost
                continue
            try:
                books = self.clob.get_books_for_market(market)
            except ApiError:
                total -= position.total_cost
                continue
            up_bid = books["Up"].best_bid or 0.0
            down_bid = books["Down"].best_bid or 0.0
            mark_value = position.shares["Up"] * up_bid + position.shares["Down"] * down_bid
            total += mark_value - position.total_cost
        return total


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
        storage_mode=args.storage,
    )
    risk = RiskConfig(
        starting_balance=args.balance,
        max_market_notional=args.max_market_notional,
        max_single_fill_notional=args.max_single_fill,
        max_unpaired_notional=args.max_unpaired_notional,
        min_order_notional=args.min_order_notional,
    )
    strategy = StrategyConfig(
        strategy_mode=args.strategy_mode,
        starter_entry_cutoff_seconds=args.starter_entry_cutoff_seconds,
        completion_pair_cost_mid=args.completion_pair_cost_mid,
        completion_pair_cost_late=args.completion_pair_cost_late,
        profit_expansion_pair_cost=args.profit_expansion_pair_cost,
        bad_regime_window=args.bad_regime_window,
        bad_regime_min_completion_rate=args.bad_regime_min_completion_rate,
        bad_regime_guard_enabled=not args.disable_bad_regime_guard,
    )
    return BotConfig(runtime=runtime, risk=risk, strategy=strategy)


def _fmt(value: float | None) -> str:
    return "None" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-first Polymarket hedged-tilt bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-paper", help="Run paper trading against live markets")
    run.add_argument("--assets", type=parse_assets, default=("BTC", "ETH"), help="BTC, ETH, or BTC,ETH")
    run.add_argument("--cycles", type=int, default=12, help="Number of polling cycles. Use 0 to run until Ctrl+C.")
    run.add_argument("--poll-seconds", type=float, default=5.0)
    run.add_argument("--db", default="paper_trades.sqlite")
    run.add_argument("--storage", choices=["csv", "sqlite"], default="csv")
    run.add_argument("--strategy-mode", choices=["current", "hedged-mm", "pair-only"], default="hedged-mm")
    run.add_argument("--balance", type=float, default=1_000.0)
    run.add_argument("--max-market-notional", type=float, default=100.0)
    run.add_argument("--max-single-fill", type=float, default=10.0)
    run.add_argument("--max-unpaired-notional", type=float, default=None)
    run.add_argument("--min-order-notional", type=float, default=1.0)
    run.add_argument("--starter-entry-cutoff-seconds", type=float, default=90.0)
    run.add_argument("--completion-pair-cost-mid", type=float, default=1.05)
    run.add_argument("--completion-pair-cost-late", type=float, default=1.08)
    run.add_argument("--profit-expansion-pair-cost", type=float, default=1.00)
    run.add_argument("--bad-regime-window", type=int, default=20)
    run.add_argument("--bad-regime-min-completion-rate", type=float, default=0.50)
    run.add_argument("--disable-bad-regime-guard", action="store_true")

    report = sub.add_parser("daily-report", help="Print resolved paper PnL report from SQLite")
    report.add_argument("--db", default="paper_trades.sqlite")
    report.add_argument("--storage", choices=["csv", "sqlite"], default="csv")
    report.add_argument("--date", default=None, help="UTC date prefix, e.g. 2026-04-26. Omit for all resolved markets.")

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

    if args.command == "daily-report":
        ledger = CsvLedger(args.db) if args.storage == "csv" else SQLiteLedger(args.db)
        try:
            report_data = ledger.daily_report(args.date)
        finally:
            ledger.close()
        summary = report_data["summary"]
        print(f"Resolved markets: {summary['resolved_markets']}")
        print(f"Wins/Losses: {summary['wins']}/{summary['losses']}")
        print(f"Total cost: ${summary['total_cost']:.2f}")
        print(f"Total PnL: ${summary['total_pnl']:.2f}")
        print(f"ROI: {summary['roi'] * 100:.2f}%")
        print(f"Both-sided completion rate: {summary['both_sided_completion_rate'] * 100:.2f}%")
        print(f"Both-sided PnL: ${summary['both_sided_pnl']:.2f}")
        print(f"One-sided PnL: ${summary['one_sided_pnl']:.2f}")
        print(f"Failed one-sided markets: {summary['failed_one_sided_markets']}")
        print("")
        print("PnL by last-fill timing bucket:")
        for label, data in summary["timing_buckets"].items():
            print(f"{label}: markets={data['markets']} pnl=${data['pnl']:.2f} roi={data['roi'] * 100:.2f}%")
        print("")
        print("PnL by cost bucket:")
        for label, data in summary["cost_buckets"].items():
            print(f"{label}: markets={data['markets']} pnl=${data['pnl']:.2f} roi={data['roi'] * 100:.2f}%")
        print("")
        print("market,winner,pnl,cost,paired_pnl,unpaired_side,unpaired_pnl,up_shares,down_shares")
        for row in report_data["markets"]:
            print(
                f"{row['market_slug']},{row['winner']},{row['pnl']:.4f},{row['cost']:.4f},"
                f"{row['paired_pnl']:.4f},{row['unpaired_side']},{row['unpaired_pnl']:.4f},"
                f"{row['up_shares']:.4f},{row['down_shares']:.4f}"
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
