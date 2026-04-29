from polymarket_tilt_bot.config import RiskConfig, StrategyConfig
from polymarket_tilt_bot.models import BookLevel, CryptoTick, Market, OrderBook, Position
from polymarket_tilt_bot.price_window import PriceWindow
from polymarket_tilt_bot.strategy import HedgedMarketMakerStrategy, HedgedTiltStrategy


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


def test_strategy_tilts_toward_up_when_price_above_open() -> None:
    strategy = HedgedTiltStrategy(StrategyConfig(), RiskConfig(max_market_notional=100, max_single_fill_notional=10))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.4, 1160, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.55), "Down": book("down", 0.45)})

    assert signal.direction == "Up"
    assert any(intent.outcome == "Up" for intent in intents)


def test_strategy_blocks_expensive_pair_hedge() -> None:
    strategy = HedgedTiltStrategy(StrategyConfig(max_pair_cost=1.02), RiskConfig(max_market_notional=100, max_single_fill_notional=10))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.3, 1160, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.55), "Down": book("down", 0.60)})

    assert all(intent.outcome != "Down" for intent in intents)


def test_strategy_allows_small_notional_when_minimum_share_size_is_met() -> None:
    strategy = HedgedTiltStrategy(StrategyConfig(), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.1, 1160, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.8), "Down": book("down", 0.2)})

    assert any(intent.outcome == "Down" and intent.max_notional == 2 for intent in intents)


def test_strategy_rejects_small_notional_when_minimum_share_size_is_not_met() -> None:
    strategy = HedgedTiltStrategy(StrategyConfig(), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.1, 1160, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.8), "Down": book("down", 0.7)})

    assert not intents


def test_hedged_mm_prioritizes_missing_side_completion() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm"), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.shares["Down"] = 20
    position.cost["Down"] = 2

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.40), "Down": book("down", 0.40)})

    assert intents
    assert intents[0].outcome == "Up"


def test_hedged_mm_requires_cheap_pair() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm", hedged_mm_max_pair_cost=1.02), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.55), "Down": book("down", 0.55)})

    assert not intents


def test_hedged_mm_limit_includes_slippage_buffer() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm"), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.70), "Down": book("down", 0.30)})

    down_intent = next(intent for intent in intents if intent.outcome == "Down")
    assert down_intent.limit_price > 0.30
