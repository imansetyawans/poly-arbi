import csv
import argparse

import pytest

from polymarket_tilt_bot.config import BotConfig, RiskConfig, RuntimeConfig, StrategyConfig
from polymarket_tilt_bot.ledger import CsvLedger, PositionBook
from polymarket_tilt_bot.live_executor import LiveExecutor, LiveTradingDisabled
from polymarket_tilt_bot.models import BookLevel, CryptoTick, Market, OrderBook, OrderIntent
from polymarket_tilt_bot.paper_broker import PaperBroker
from polymarket_tilt_bot.price_window import PriceWindow
from polymarket_tilt_bot.runner import PaperTradingBot, add_trading_arguments, load_env_file, main, validate_config, write_run_manifest
from polymarket_tilt_bot.strategy import JetFadilStrategy


def market() -> Market:
    return Market(
        asset="BTC",
        slug="btc-updown-5m-1000",
        condition_id="0xabc",
        title="Bitcoin Up or Down",
        start_ts=1000,
        end_ts=1300,
        up_token="up",
        down_token="down",
        accepting_orders=True,
        closed=False,
        price_to_beat=100.0,
    )


def book(token: str, ask: float) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(max(0.01, ask - 0.02), 1000)], asks=[BookLevel(ask, 1000)])


def test_config_validation_rejects_impossible_single_fill_cap() -> None:
    config = BotConfig(
        runtime=RuntimeConfig(poll_seconds=1, cycles=1),
        risk=RiskConfig(max_market_notional=3, max_single_fill_notional=4),
        strategy=StrategyConfig(strategy_mode="jetfadil"),
    )

    with pytest.raises(ValueError, match="max-single-fill"):
        validate_config(config)


def test_live_executor_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(LiveExecutor, "preflight", classmethod(lambda cls: object()))

    with pytest.raises(LiveTradingDisabled, match="i-understand-live-risk"):
        LiveExecutor.from_env(RuntimeConfig(), confirmed=False)


def test_live_preflight_allows_missing_cached_l2_credentials(monkeypatch) -> None:
    monkeypatch.setenv("POLYBOT_ENABLE_LIVE_TRADING", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.delenv("CLOB_API_KEY", raising=False)
    monkeypatch.delenv("CLOB_SECRET", raising=False)
    monkeypatch.delenv("CLOB_PASS_PHRASE", raising=False)

    result = LiveExecutor.preflight()

    assert result.ready is True
    assert result.missing_env == ()


def test_live_preflight_rejects_partial_cached_l2_credentials(monkeypatch) -> None:
    monkeypatch.setenv("POLYBOT_ENABLE_LIVE_TRADING", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.delenv("CLOB_SECRET", raising=False)
    monkeypatch.delenv("CLOB_PASS_PHRASE", raising=False)

    result = LiveExecutor.preflight()

    assert result.ready is False
    assert "CLOB_SECRET" in result.missing_env
    assert "CLOB_PASS_PHRASE" in result.missing_env


def test_live_executor_derives_l2_credentials_when_not_supplied(monkeypatch) -> None:
    class FakeCreds:
        pass

    class FakeSdk:
        @staticmethod
        def ApiCreds(**kwargs):
            return kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.set_creds = None

        def create_or_derive_api_key(self):
            return FakeCreds()

        def set_api_creds(self, creds):
            self.set_creds = creds

    FakeSdk.ClobClient = FakeClient
    monkeypatch.setenv("POLYBOT_ENABLE_LIVE_TRADING", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.delenv("CLOB_API_KEY", raising=False)
    monkeypatch.delenv("CLOB_SECRET", raising=False)
    monkeypatch.delenv("CLOB_PASS_PHRASE", raising=False)
    monkeypatch.setattr("polymarket_tilt_bot.live_executor.LiveExecutor.preflight", classmethod(lambda cls: type("Result", (), {"ready": True, "missing_env": (), "sdk_available": True})()))
    monkeypatch.setitem(__import__("sys").modules, "py_clob_client_v2", FakeSdk)

    executor = LiveExecutor.from_env(RuntimeConfig(), confirmed=True)

    assert "creds" not in executor.client.kwargs
    assert isinstance(executor.client.set_creds, FakeCreds)


def test_live_executor_uses_cached_l2_credentials_when_supplied(monkeypatch) -> None:
    class FakeSdk:
        @staticmethod
        def ApiCreds(**kwargs):
            return kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.set_creds = None

        def create_or_derive_api_key(self):
            raise AssertionError("should not derive when cached creds are present")

        def set_api_creds(self, creds):
            self.set_creds = creds

    FakeSdk.ClobClient = FakeClient
    monkeypatch.setenv("POLYBOT_ENABLE_LIVE_TRADING", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.setenv("CLOB_SECRET", "secret")
    monkeypatch.setenv("CLOB_PASS_PHRASE", "pass")
    monkeypatch.setattr("polymarket_tilt_bot.live_executor.LiveExecutor.preflight", classmethod(lambda cls: type("Result", (), {"ready": True, "missing_env": (), "sdk_available": True})()))
    monkeypatch.setitem(__import__("sys").modules, "py_clob_client_v2", FakeSdk)

    executor = LiveExecutor.from_env(RuntimeConfig(), confirmed=True)

    assert executor.client.kwargs["creds"]["api_key"] == "key"
    assert executor.client.set_creds is None


def test_env_file_loader_sets_missing_values_without_overriding_existing(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.live"
    env_file.write_text("POLYBOT_ENABLE_LIVE_TRADING=YES\nCLOB_API_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("CLOB_API_KEY", "existing-value")
    monkeypatch.delenv("POLYBOT_ENABLE_LIVE_TRADING", raising=False)

    load_env_file(str(env_file))

    assert __import__("os").environ["POLYBOT_ENABLE_LIVE_TRADING"] == "YES"
    assert __import__("os").environ["CLOB_API_KEY"] == "existing-value"


def test_trading_arguments_default_from_env(monkeypatch) -> None:
    monkeypatch.setenv("POLYBOT_ASSETS", "BTC")
    monkeypatch.setenv("POLYBOT_CYCLES", "0")
    monkeypatch.setenv("POLYBOT_DB", "live_trades_jetfadil_prod")
    monkeypatch.setenv("POLYBOT_STRATEGY_MODE", "jetfadil")
    monkeypatch.setenv("POLYBOT_MAX_MARKET_NOTIONAL", "18")
    monkeypatch.setenv("POLYBOT_JETFADIL_ENTRY_PAIR_COST", "0.98")
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)

    args = parser.parse_args([])

    assert args.assets == ("BTC",)
    assert args.cycles == 0
    assert args.db == "live_trades_jetfadil_prod"
    assert args.strategy_mode == "jetfadil"
    assert args.max_market_notional == 18
    assert args.jetfadil_entry_pair_cost == 0.98


def test_main_loads_env_file_before_building_command_defaults(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.live"
    env_file.write_text("POLYBOT_CYCLES=7\nPOLYBOT_ASSETS=BTC\n", encoding="utf-8")
    captured = {}

    class FakeBot:
        def __init__(self, config, executor=None, execution_mode="paper"):
            captured["config"] = config
            captured["executor"] = executor
            captured["execution_mode"] = execution_mode

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("polymarket_tilt_bot.runner.LiveExecutor.from_env", lambda runtime, confirmed, order_type="FAK", tick_size="0.01", price_ticks=1: object())
    monkeypatch.setattr("polymarket_tilt_bot.runner.PaperTradingBot", FakeBot)

    assert main(["run-live", "--env-file", str(env_file), "--i-understand-live-risk"]) == 0
    assert captured["config"].runtime.cycles == 7
    assert captured["config"].runtime.assets == ("BTC",)
    assert captured["execution_mode"] == "live"
    assert captured["ran"] is True


def test_run_paper_accepts_env_file_for_production_rehearsal(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.live"
    env_file.write_text("POLYBOT_CYCLES=0\nPOLYBOT_ASSETS=BTC\nPOLYBOT_STRATEGY_MODE=jetfadil\n", encoding="utf-8")
    captured = {}

    class FakeBot:
        def __init__(self, config, executor=None, execution_mode="paper"):
            captured["config"] = config
            captured["executor"] = executor
            captured["execution_mode"] = execution_mode

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("polymarket_tilt_bot.runner.PaperTradingBot", FakeBot)

    assert main(["run-paper", "--env-file", str(env_file), "--cycles", "2"]) == 0
    assert captured["config"].runtime.cycles == 2
    assert captured["config"].runtime.assets == ("BTC",)
    assert captured["config"].strategy.strategy_mode == "jetfadil"
    assert captured["executor"] is None
    assert captured["execution_mode"] == "paper"
    assert captured["ran"] is True


def test_live_executor_maps_existing_intent_to_signed_buy_order() -> None:
    class FakeOrderType:
        FOK = "FOK"

    class FakeSide:
        BUY = "BUY"

    class FakeOrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSdk:
        OrderType = FakeOrderType
        Side = FakeSide
        OrderArgs = FakeOrderArgs
        PartialCreateOrderOptions = FakeOptions

    class FakeClient:
        def __init__(self):
            self.calls = []

        def create_and_post_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"success": True}

    client = FakeClient()
    executor = LiveExecutor(client, FakeSdk, order_type="FOK")
    m = market()
    intent = OrderIntent(m.slug, "Up", m.up_token, max_notional=3.0, limit_price=0.60, reason="test")

    fill = executor.execute(m, intent, book("up", 0.59), now=1234)

    assert fill is not None
    assert fill.simulated is False
    assert fill.notional == 3.0
    assert fill.size == 5.0
    assert client.calls[0]["order_type"] == "FOK"
    assert client.calls[0]["order_args"].kwargs["side"] == "BUY"
    assert client.calls[0]["order_args"].kwargs["token_id"] == m.up_token


def test_live_executor_uses_fak_one_tick_without_exceeding_strategy_limit() -> None:
    class FakeOrderType:
        FAK = "FAK"

    class FakeSide:
        BUY = "BUY"

    class FakeOrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSdk:
        OrderType = FakeOrderType
        Side = FakeSide
        OrderArgs = FakeOrderArgs
        PartialCreateOrderOptions = FakeOptions

    class FakeClient:
        def __init__(self):
            self.calls = []

        def create_and_post_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"success": True}

    client = FakeClient()
    executor = LiveExecutor(client, FakeSdk, order_type="FAK", tick_size="0.01", price_ticks=1)
    m = market()
    intent = OrderIntent(m.slug, "Up", m.up_token, max_notional=3.0, limit_price=0.555, reason="test")

    fill = executor.execute(m, intent, book("up", 0.54), now=1234)

    assert fill is not None
    assert client.calls[0]["order_type"] == "FAK"
    assert client.calls[0]["order_args"].kwargs["price"] == 0.55
    assert client.calls[0]["order_args"].kwargs["price"] <= intent.limit_price


def test_live_executor_allows_two_ticks_only_when_strategy_limit_allows_it() -> None:
    class FakeOrderType:
        FAK = "FAK"

    class FakeSide:
        BUY = "BUY"

    class FakeOrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSdk:
        OrderType = FakeOrderType
        Side = FakeSide
        OrderArgs = FakeOrderArgs
        PartialCreateOrderOptions = FakeOptions

    class FakeClient:
        def __init__(self):
            self.calls = []

        def create_and_post_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"success": True}

    client = FakeClient()
    executor = LiveExecutor(client, FakeSdk, order_type="FAK", tick_size="0.01", price_ticks=2)
    m = market()
    intent = OrderIntent(m.slug, "Up", m.up_token, max_notional=3.0, limit_price=0.60, reason="test")

    executor.execute(m, intent, book("up", 0.54), now=1234)

    assert client.calls[0]["order_args"].kwargs["price"] == 0.56


def test_live_executor_rejects_more_than_two_live_price_ticks() -> None:
    with pytest.raises(Exception, match="price ticks"):
        LiveExecutor(object(), object(), price_ticks=3)


def test_runner_marks_partial_live_fill_status(tmp_path) -> None:
    bot = PaperTradingBot.__new__(PaperTradingBot)
    bot.execution_mode = "live"
    bot.ledger = CsvLedger(str(tmp_path / "ledger"))

    class PartialExecutor:
        def execute(self, m, intent, order_book, timestamp):
            return __import__("polymarket_tilt_bot.models", fromlist=["Fill"]).Fill(
                m.slug,
                m.condition_id,
                intent.outcome,
                intent.token_id,
                price=0.55,
                size=2,
                notional=1.1,
                timestamp=timestamp,
                simulated=False,
            )

    bot.executor = PartialExecutor()
    m = market()
    intent = OrderIntent(m.slug, "Up", m.up_token, max_notional=3.0, limit_price=0.55, reason="test")

    bot._execute_intent(m, intent, book("up", 0.54), 1234)

    with (tmp_path / "ledger" / "orders.csv").open(newline="", encoding="utf-8") as handle:
        orders = list(csv.DictReader(handle))
    assert orders[-1]["status"] == "partial_filled"


def test_offline_end_to_end_paper_step_records_manifest_orders_and_fills(tmp_path) -> None:
    config = BotConfig(
        runtime=RuntimeConfig(assets=("BTC",), cycles=1, poll_seconds=1, database_path=str(tmp_path / "paper"), storage_mode="csv"),
        risk=RiskConfig(starting_balance=300, max_market_notional=18, max_single_fill_notional=3, max_unpaired_notional=2),
        strategy=StrategyConfig(
            strategy_mode="jetfadil",
            jetfadil_entry_pair_cost=0.98,
            jetfadil_min_confidence=0.0,
            jetfadil_min_entry_seconds=0,
        ),
    )
    bot = PaperTradingBot.__new__(PaperTradingBot)
    bot.config = config
    bot.execution_mode = "paper"
    bot.strategy = JetFadilStrategy(config.strategy, config.risk)
    bot.broker = PaperBroker(slippage_bps=0.0, min_order_notional=config.risk.min_order_notional)
    bot.executor = bot.broker
    bot.positions = PositionBook()
    bot.ledger = CsvLedger(config.runtime.database_path)
    write_run_manifest(bot.ledger, config, "paper")
    bot.window = PriceWindow()
    bot._bad_regime_pause = lambda: False
    bot.clob = type("Clob", (), {"get_books_for_market": lambda self, m: {"Up": book("up", 0.45), "Down": book("down", 0.50)}})()
    bot.window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    bot.window.add(CryptoTick("BTC", 100.2, 1060, "test"))

    bot._trade_market(market(), 1060)

    with (tmp_path / "paper" / "fills.csv").open(newline="", encoding="utf-8") as handle:
        fills = list(csv.DictReader(handle))
    with (tmp_path / "paper" / "orders.csv").open(newline="", encoding="utf-8") as handle:
        orders = list(csv.DictReader(handle))

    assert (tmp_path / "paper" / "run_manifest.json").exists()
    assert {row["outcome"] for row in fills} == {"Up", "Down"}
    assert len(orders) == 2
    assert {row["status"] for row in orders} == {"filled"}
    assert all(float(row["notional"]) <= 3.0 for row in fills)
