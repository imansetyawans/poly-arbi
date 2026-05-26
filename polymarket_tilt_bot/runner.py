from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .clients import ApiError, BinanceWebSocketPriceClient, ClobClient, GammaClient, HttpClient
from .config import BotConfig, RiskConfig, RuntimeConfig, StrategyConfig
from .ledger import CsvLedger, PositionBook, SQLiteLedger
from .live_executor import LiveExecutor, LiveTradingDisabled
from .market_scanner import MarketScanner
from .models import Market
from .paper_broker import PaperBroker
from .price_window import PriceWindow
from .resolution import ResolutionClient
from .strategy import HedgedMarketMakerStrategy, HedgedTiltStrategy, JetFadilStrategy


LOGGER = logging.getLogger("polymarket_tilt_bot")


class PaperTradingBot:
    def __init__(self, config: BotConfig, executor=None, execution_mode: str = "paper") -> None:
        validate_config(config, live=execution_mode == "live")
        self.config = config
        self.execution_mode = execution_mode
        self.http = HttpClient(config.runtime)
        self.gamma = GammaClient(self.http)
        self.clob = ClobClient(self.http)
        self.prices = BinanceWebSocketPriceClient(config.runtime.assets)
        self.scanner = MarketScanner(self.gamma)
        self.resolver = ResolutionClient(self.gamma)
        if config.strategy.strategy_mode == "jetfadil":
            self.strategy = JetFadilStrategy(config.strategy, config.risk)
        elif config.strategy.strategy_mode in {"hedged-mm", "pair-only"}:
            self.strategy = HedgedMarketMakerStrategy(config.strategy, config.risk)
        else:
            self.strategy = HedgedTiltStrategy(config.strategy, config.risk)
        self.broker = PaperBroker(slippage_bps=5.0, min_order_notional=config.risk.min_order_notional)
        self.executor = executor or self.broker
        self.positions = PositionBook()
        self.ledger = CsvLedger(config.runtime.database_path) if config.runtime.storage_mode == "csv" else SQLiteLedger(config.runtime.database_path)
        write_run_manifest(self.ledger, config, execution_mode)
        self.window = PriceWindow()
        self._last_resolution_check = 0.0

    def close(self) -> None:
        self.ledger.close()

    def run(self) -> None:
        execution_mode = getattr(self, "execution_mode", "paper")
        LOGGER.info("starting %s bot: assets=%s cycles=%s db=%s", execution_mode, self.config.runtime.assets, self.config.runtime.cycles, self.config.runtime.database_path)
        try:
            self._try_resolve_finished_positions(force=True)
            cycle = 0
            while self.config.runtime.cycles <= 0 or cycle < self.config.runtime.cycles:
                try:
                    self.step()
                except ApiError as exc:
                    LOGGER.warning("cycle skipped after recoverable API error: %s", exc)
                except Exception:
                    LOGGER.exception("cycle skipped after unexpected error")
                finally:
                    self._try_resolve_finished_positions()
                cycle += 1
                total = "unlimited" if self.config.runtime.cycles <= 0 else str(self.config.runtime.cycles)
                LOGGER.info("cycle %s/%s complete", cycle, total)
                if self.config.runtime.cycles <= 0 or cycle < self.config.runtime.cycles:
                    time.sleep(self.config.runtime.poll_seconds)
        except KeyboardInterrupt:
            LOGGER.info("stopped by user")
        finally:
            self._try_resolve_finished_positions(force=True)
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
            fill = self._execute_intent(market, intent, books[intent.outcome], timestamp)
            if fill is None:
                continue
            self.positions.add_fill(fill)
            self.ledger.record_fill(fill)
            LOGGER.info(
                "%s fill %s %s %.2f shares @ %.3f notional %.2f",
                self.execution_mode,
                market.slug,
                fill.outcome,
                fill.size,
                fill.price,
                fill.notional,
            )

    def _execute_intent(self, market: Market, intent, book, timestamp: float):
        try:
            fill = self.executor.execute(market, intent, book, timestamp)
        except Exception as exc:
            self.ledger.record_order_attempt(intent, timestamp, self.execution_mode, "error", str(exc))
            LOGGER.exception("%s order failed %s %s %.2f @ %.3f", self.execution_mode, market.slug, intent.outcome, intent.max_notional, intent.limit_price)
            return None
        status = "filled" if fill is not None else "not_filled"
        if fill is not None and fill.notional + 1e-9 < intent.max_notional:
            status = "partial_filled"
        self.ledger.record_order_attempt(intent, timestamp, self.execution_mode, status)
        return fill

    def _bad_regime_pause(self) -> bool:
        strategy = self.config.strategy
        if not strategy.bad_regime_guard_enabled:
            return False
        resolved, completion_rate = self.ledger.recent_completion_rate(strategy.bad_regime_window)
        return resolved >= strategy.bad_regime_window and completion_rate < strategy.bad_regime_min_completion_rate

    def _try_resolve_finished_positions(self, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_resolution_check < self.config.runtime.resolution_poll_seconds:
            return 0
        self._last_resolution_check = now
        return resolve_missing_positions(
            ledger=self.ledger,
            gamma=self.gamma,
            resolver=self.resolver,
            grace_seconds=self.config.runtime.resolution_grace_seconds,
            now=now,
        )

    def _market_from_position(self, slug: str) -> Market | None:
        return market_from_ledger_or_gamma(self.ledger, self.gamma, slug)

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
        if getattr(self, "execution_mode", "paper") == "live":
            self._log_live_account_state(reserved, unrealized, realized, len(positions))
            return
        balance = self.config.risk.starting_balance + realized - reserved
        equity = self.config.risk.starting_balance + realized + unrealized
        LOGGER.info(
            "paper account balance=%.2f equity=%.2f realized_pnl=%.2f unrealized_pnl=%.2f reserved=%.2f open_positions=%s",
            balance,
            equity,
            realized,
            unrealized,
            reserved,
            len(positions),
        )

    def _log_live_account_state(self, reserved: float, unrealized: float, realized: float, open_positions: int) -> None:
        try:
            balance = self.executor.collateral_balance_allowance()
        except Exception as exc:
            LOGGER.warning(
                "live account balance unavailable: %s; local_ledger baseline=%.2f realized_pnl=%.2f unrealized_pnl=%.2f reserved=%.2f open_positions=%s",
                exc,
                self.config.risk.starting_balance,
                realized,
                unrealized,
                reserved,
                open_positions,
            )
            return
        LOGGER.info(
            "live account clob_balance=%.2f clob_allowance=%.2f local_ledger_baseline=%.2f realized_pnl=%.2f unrealized_pnl=%.2f reserved=%.2f open_positions=%s",
            balance.balance,
            balance.allowance,
            self.config.risk.starting_balance,
            realized,
            unrealized,
            reserved,
            open_positions,
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


def market_from_ledger_or_gamma(ledger, gamma: GammaClient, slug: str) -> Market | None:
    market = ledger.get_market(slug)
    if market is not None:
        return market
    asset: Literal["BTC", "ETH"] = "ETH" if slug.startswith("eth-") else "BTC"
    event = gamma.get_event_by_slug(slug)
    return gamma.parse_crypto_market(event, asset) if event else None


def resolve_missing_positions(
    ledger,
    gamma: GammaClient,
    resolver: ResolutionClient,
    grace_seconds: float,
    now: float | None = None,
) -> int:
    timestamp = time.time() if now is None else now
    resolved_count = 0
    for position in ledger.get_unresolved_positions():
        if position.total_cost <= 0 or ledger.is_resolved(position.market_slug):
            continue
        try:
            market = market_from_ledger_or_gamma(ledger, gamma, position.market_slug)
            if market is None:
                LOGGER.warning("resolution pending %s: market metadata unavailable", position.market_slug)
                continue
            if timestamp < market.end_ts + grace_seconds:
                continue
            winner = resolver.get_winner(market)
            if winner is None:
                LOGGER.info("resolution pending %s: winner not posted yet", position.market_slug)
                continue
            result = ledger.record_resolution(position, winner, timestamp)
            resolved_count += 1
            LOGGER.info(
                "resolved %s winner=%s pnl=%.2f paired_pnl=%.2f unpaired_pnl=%.2f",
                position.market_slug,
                winner,
                result["pnl"],
                result["paired_pnl"],
                result["unpaired_pnl"],
            )
        except Exception as exc:
            LOGGER.warning("resolution failed for %s: %s", position.market_slug, exc)
    return resolved_count


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
        resolution_grace_seconds=args.resolution_grace_seconds,
        resolution_poll_seconds=args.resolution_poll_seconds,
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
        jetfadil_entry_pair_cost=args.jetfadil_entry_pair_cost,
        jetfadil_early_entry_pair_cost=args.jetfadil_early_entry_pair_cost,
        jetfadil_deep_value_pair_cost=args.jetfadil_deep_value_pair_cost,
        jetfadil_min_entry_seconds=args.jetfadil_min_entry_seconds,
        jetfadil_min_confidence=args.jetfadil_min_confidence,
        jetfadil_strong_tilt_confidence=args.jetfadil_strong_tilt_confidence,
        jetfadil_max_directional_bias=args.jetfadil_max_directional_bias,
        jetfadil_starter_entry_cutoff_seconds=args.jetfadil_starter_entry_cutoff_seconds,
        jetfadil_core_pair_fraction=args.jetfadil_core_pair_fraction,
        jetfadil_pre_late_expansion_pair_cost=args.jetfadil_pre_late_expansion_pair_cost,
        jetfadil_late_expansion_seconds=args.jetfadil_late_expansion_seconds,
        bad_regime_window=args.bad_regime_window,
        bad_regime_min_completion_rate=args.bad_regime_min_completion_rate,
        bad_regime_guard_enabled=not args.disable_bad_regime_guard,
    )
    return BotConfig(runtime=runtime, risk=risk, strategy=strategy)


def validate_config(config: BotConfig, live: bool = False) -> None:
    risk = config.risk
    runtime = config.runtime
    strategy = config.strategy
    if not runtime.assets:
        raise ValueError("at least one asset is required")
    if runtime.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    if runtime.cycles < 0:
        raise ValueError("--cycles must be 0 or greater")
    if risk.starting_balance <= 0:
        raise ValueError("--balance must be positive")
    if risk.max_market_notional <= 0:
        raise ValueError("--max-market-notional must be positive")
    if risk.max_single_fill_notional <= 0:
        raise ValueError("--max-single-fill must be positive")
    if risk.max_single_fill_notional > risk.max_market_notional:
        raise ValueError("--max-single-fill cannot exceed --max-market-notional")
    if risk.max_unpaired_notional is not None and risk.max_unpaired_notional < 0:
        raise ValueError("--max-unpaired-notional cannot be negative")
    if risk.min_order_notional <= 0:
        raise ValueError("--min-order-notional must be positive")
    if not 0 <= risk.reserve_fraction < 1:
        raise ValueError("reserve_fraction must be between 0 and 1")
    if strategy.bad_regime_window <= 0:
        raise ValueError("--bad-regime-window must be positive")
    if not 0 <= strategy.bad_regime_min_completion_rate <= 1:
        raise ValueError("--bad-regime-min-completion-rate must be between 0 and 1")
    if live and runtime.cycles == 0:
        LOGGER.warning("live mode is configured to run indefinitely; stop it explicitly when finished")


def write_run_manifest(ledger, config: BotConfig, execution_mode: str) -> None:
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_mode": execution_mode,
        "runtime": asdict(config.runtime),
        "risk": asdict(config.risk),
        "strategy": asdict(config.strategy),
    }
    if isinstance(ledger, CsvLedger):
        path = ledger.path / "run_manifest.json"
    else:
        path = ledger.path.with_suffix(".run_manifest.json")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        raise ValueError(f"env file not found: {path}")
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid env file line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            raise ValueError(f"invalid env file line {line_number}: empty key")
        if key not in os.environ:
            os.environ[key] = value


def _env_default(name: str, default, caster):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return caster(raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _extract_env_file(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--env-file" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--env-file="):
            return arg.split("=", 1)[1]
    return None


def _fmt(value: float | None) -> str:
    return "None" if value is None else f"{value:.3f}"


def add_trading_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--assets", type=parse_assets, default=_env_default("POLYBOT_ASSETS", ("BTC", "ETH"), parse_assets), help="BTC, ETH, or BTC,ETH")
    command.add_argument("--cycles", type=int, default=_env_default("POLYBOT_CYCLES", 12, int), help="Number of polling cycles. Use 0 to run until Ctrl+C.")
    command.add_argument("--poll-seconds", type=float, default=_env_default("POLYBOT_POLL_SECONDS", 5.0, float))
    command.add_argument("--db", default=_env_default("POLYBOT_DB", "paper_trades.sqlite", str))
    command.add_argument("--storage", choices=["csv", "sqlite"], default=_env_default("POLYBOT_STORAGE", "csv", str))
    command.add_argument("--strategy-mode", choices=["current", "hedged-mm", "pair-only", "jetfadil"], default=_env_default("POLYBOT_STRATEGY_MODE", "hedged-mm", str))
    command.add_argument("--balance", type=float, default=_env_default("POLYBOT_BALANCE", 1_000.0, float))
    command.add_argument("--max-market-notional", type=float, default=_env_default("POLYBOT_MAX_MARKET_NOTIONAL", 100.0, float))
    command.add_argument("--max-single-fill", type=float, default=_env_default("POLYBOT_MAX_SINGLE_FILL", 10.0, float))
    command.add_argument("--max-unpaired-notional", type=float, default=_env_default("POLYBOT_MAX_UNPAIRED_NOTIONAL", None, float))
    command.add_argument("--min-order-notional", type=float, default=_env_default("POLYBOT_MIN_ORDER_NOTIONAL", 1.0, float))
    command.add_argument("--starter-entry-cutoff-seconds", type=float, default=_env_default("POLYBOT_STARTER_ENTRY_CUTOFF_SECONDS", 90.0, float))
    command.add_argument("--completion-pair-cost-mid", type=float, default=_env_default("POLYBOT_COMPLETION_PAIR_COST_MID", 1.05, float))
    command.add_argument("--completion-pair-cost-late", type=float, default=_env_default("POLYBOT_COMPLETION_PAIR_COST_LATE", 1.08, float))
    command.add_argument("--profit-expansion-pair-cost", type=float, default=_env_default("POLYBOT_PROFIT_EXPANSION_PAIR_COST", 1.00, float))
    command.add_argument("--jetfadil-entry-pair-cost", type=float, default=_env_default("POLYBOT_JETFADIL_ENTRY_PAIR_COST", 1.00, float))
    command.add_argument("--jetfadil-early-entry-pair-cost", type=float, default=_env_default("POLYBOT_JETFADIL_EARLY_ENTRY_PAIR_COST", 0.98, float))
    command.add_argument("--jetfadil-deep-value-pair-cost", type=float, default=_env_default("POLYBOT_JETFADIL_DEEP_VALUE_PAIR_COST", 0.98, float))
    command.add_argument("--jetfadil-min-entry-seconds", type=float, default=_env_default("POLYBOT_JETFADIL_MIN_ENTRY_SECONDS", 5.0, float))
    command.add_argument("--jetfadil-min-confidence", type=float, default=_env_default("POLYBOT_JETFADIL_MIN_CONFIDENCE", 0.05, float))
    command.add_argument("--jetfadil-strong-tilt-confidence", type=float, default=_env_default("POLYBOT_JETFADIL_STRONG_TILT_CONFIDENCE", 0.35, float))
    command.add_argument("--jetfadil-max-directional-bias", type=float, default=_env_default("POLYBOT_JETFADIL_MAX_DIRECTIONAL_BIAS", 0.20, float))
    command.add_argument("--jetfadil-starter-entry-cutoff-seconds", type=float, default=_env_default("POLYBOT_JETFADIL_STARTER_ENTRY_CUTOFF_SECONDS", 240.0, float))
    command.add_argument("--jetfadil-core-pair-fraction", type=float, default=_env_default("POLYBOT_JETFADIL_CORE_PAIR_FRACTION", 0.80, float))
    command.add_argument("--jetfadil-pre-late-expansion-pair-cost", type=float, default=_env_default("POLYBOT_JETFADIL_PRE_LATE_EXPANSION_PAIR_COST", 0.85, float))
    command.add_argument("--jetfadil-late-expansion-seconds", type=float, default=_env_default("POLYBOT_JETFADIL_LATE_EXPANSION_SECONDS", 240.0, float))
    command.add_argument("--resolution-grace-seconds", type=float, default=_env_default("POLYBOT_RESOLUTION_GRACE_SECONDS", 20.0, float))
    command.add_argument("--resolution-poll-seconds", type=float, default=_env_default("POLYBOT_RESOLUTION_POLL_SECONDS", 30.0, float))
    command.add_argument("--bad-regime-window", type=int, default=_env_default("POLYBOT_BAD_REGIME_WINDOW", 20, int))
    command.add_argument("--bad-regime-min-completion-rate", type=float, default=_env_default("POLYBOT_BAD_REGIME_MIN_COMPLETION_RATE", 0.50, float))
    command.add_argument("--disable-bad-regime-guard", action="store_true", default=_env_bool("POLYBOT_DISABLE_BAD_REGIME_GUARD", False))


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    env_file = _extract_env_file(raw_argv)
    if env_file:
        try:
            load_env_file(env_file)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from exc

    parser = argparse.ArgumentParser(description="Paper-first Polymarket hedged-tilt bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-paper", help="Run paper trading against live markets")
    add_trading_arguments(run)
    run.add_argument("--env-file", default=None, help="Optional KEY=VALUE env file for config")

    live = sub.add_parser("run-live", help="Run the current strategy with signed live CLOB execution")
    add_trading_arguments(live)
    live.add_argument("--env-file", default=None, help="Optional KEY=VALUE env file for live credentials")
    live.add_argument("--i-understand-live-risk", action="store_true", help="Required live-trading arming flag")
    live.add_argument("--live-order-type", choices=["FOK", "FAK", "GTC", "GTD"], default=_env_default("POLYBOT_LIVE_ORDER_TYPE", "FAK", str))
    live.add_argument("--live-tick-size", default=_env_default("POLYBOT_LIVE_TICK_SIZE", "0.01", str))
    live.add_argument("--live-price-ticks", type=int, default=_env_default("POLYBOT_LIVE_PRICE_TICKS", 1, int))

    preflight = sub.add_parser("preflight-live", help="Check live-trading SDK and environment variables")
    preflight.add_argument("--env-file", default=None, help="Optional KEY=VALUE env file for live credentials")
    preflight.add_argument("--verbose", action="store_true")

    report = sub.add_parser("daily-report", help="Print resolved paper PnL report from SQLite")
    report.add_argument("--db", default="paper_trades.sqlite")
    report.add_argument("--storage", choices=["csv", "sqlite"], default="csv")
    report.add_argument("--date", default=None, help="UTC date prefix, e.g. 2026-04-26. Omit for all resolved markets.")

    scan = sub.add_parser("scan-once", help="Print currently tradable crypto 5m markets")
    scan.add_argument("--assets", type=parse_assets, default=("BTC", "ETH"), help="BTC, ETH, or BTC,ETH")

    resolve = sub.add_parser("resolve-missing", help="Fetch and record missing paper resolutions")
    resolve.add_argument("--db", default="paper_trades.sqlite")
    resolve.add_argument("--storage", choices=["csv", "sqlite"], default="csv")
    resolve.add_argument("--resolution-grace-seconds", type=float, default=20.0)

    args = parser.parse_args(raw_argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "scan-once":
        runtime = RuntimeConfig(assets=args.assets)
        http = HttpClient(runtime)
        scanner = MarketScanner(GammaClient(http))
        for market in scanner.scan(args.assets):
            print(f"{market.asset} {market.slug} orders={market.accepting_orders} up={market.up_token} down={market.down_token}")
        return 0

    if args.command == "preflight-live":
        result = LiveExecutor.preflight()
        print(f"SDK available: {result.sdk_available}")
        print(f"Live armed: {result.armed}")
        print(f"Ready: {result.ready}")
        if result.missing_env:
            print("Missing env: " + ", ".join(result.missing_env))
        elif args.verbose:
            print("Missing env: none")
        return 0 if result.ready else 2

    if args.command in {"run-paper", "run-live"}:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True) if Path(args.db).parent != Path(".") else None
        config = build_config(args)
        try:
            validate_config(config, live=args.command == "run-live")
        except ValueError as exc:
            parser.error(str(exc))
        if args.command == "run-live":
            try:
                executor = LiveExecutor.from_env(
                    config.runtime,
                    confirmed=args.i_understand_live_risk,
                    order_type=args.live_order_type,
                    tick_size=args.live_tick_size,
                    price_ticks=args.live_price_ticks,
                )
            except LiveTradingDisabled as exc:
                parser.error(str(exc))
            bot = PaperTradingBot(config, executor=executor, execution_mode="live")
        else:
            bot = PaperTradingBot(config)
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
        print(f"One-sided average loss: ${summary['one_sided_average_loss']:.2f}")
        print(f"Scaled both-sided markets: {summary['scaled_both_sided_markets']}")
        print(f"Scaled both-sided PnL: ${summary['scaled_both_sided_pnl']:.2f}")
        print(f"Positive pair-quality markets: {summary['positive_pair_quality_markets']}")
        print(f"Expensive both-sided markets: {summary['expensive_both_sided_markets']}")
        print(f"Expensive both-sided PnL: ${summary['expensive_both_sided_pnl']:.2f}")
        print(f"Max drawdown: ${summary['max_drawdown']:.2f}")
        print("")
        print("PnL by last-fill timing bucket:")
        for label, data in summary["timing_buckets"].items():
            print(f"{label}: markets={data['markets']} pnl=${data['pnl']:.2f} roi={data['roi'] * 100:.2f}%")
        print("")
        print("PnL by cost bucket:")
        for label, data in summary["cost_buckets"].items():
            print(f"{label}: markets={data['markets']} pnl=${data['pnl']:.2f} roi={data['roi'] * 100:.2f}%")
        print("")
        print("market,winner,pnl,cost,pair_cost,paired_pnl,unpaired_side,unpaired_pnl,up_shares,down_shares")
        for row in report_data["markets"]:
            print(
                f"{row['market_slug']},{row['winner']},{row['pnl']:.4f},{row['cost']:.4f},{row['pair_cost'] or ''},"
                f"{row['paired_pnl']:.4f},{row['unpaired_side']},{row['unpaired_pnl']:.4f},"
                f"{row['up_shares']:.4f},{row['down_shares']:.4f}"
            )
        return 0

    if args.command == "resolve-missing":
        runtime = RuntimeConfig(
            database_path=args.db,
            storage_mode=args.storage,
            resolution_grace_seconds=args.resolution_grace_seconds,
        )
        http = HttpClient(runtime)
        gamma = GammaClient(http)
        ledger = CsvLedger(args.db) if args.storage == "csv" else SQLiteLedger(args.db)
        try:
            resolved_count = resolve_missing_positions(
                ledger=ledger,
                gamma=gamma,
                resolver=ResolutionClient(gamma),
                grace_seconds=args.resolution_grace_seconds,
            )
        finally:
            ledger.close()
        print(f"Resolved missing markets: {resolved_count}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
