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


def test_strategy_rejects_sub_minimum_notional_even_when_share_size_is_met() -> None:
    strategy = HedgedTiltStrategy(StrategyConfig(), RiskConfig(max_market_notional=12, max_single_fill_notional=0.5, min_order_notional=1.0))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.1, 1050, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.05), "Down": book("down", 0.05)})

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
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.70), "Down": book("down", 0.30)})

    down_intent = next(intent for intent in intents if intent.outcome == "Down")
    assert down_intent.limit_price > 0.30


def test_hedged_mm_blocks_repeated_same_side_after_unpaired_cap() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm"), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.shares["Down"] = 10
    position.cost["Down"] = 2

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.90), "Down": book("down", 0.20)})

    assert not intents


def test_hedged_mm_allows_missing_side_completion_first() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm"), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.shares["Down"] = 10
    position.cost["Down"] = 2

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.40), "Down": book("down", 0.20)})

    assert intents
    assert intents[0].outcome == "Up"


def test_hedged_mm_blocks_flat_starter_after_cutoff() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm", starter_entry_cutoff_seconds=90), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.40), "Down": book("down", 0.40)})

    assert not intents


def test_hedged_mm_still_allows_late_completion() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="hedged-mm", completion_pair_cost_late=1.08),
        RiskConfig(max_market_notional=12, max_single_fill_notional=5),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.shares["Up"] = 4
    position.cost["Up"] = 2

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.50), "Down": book("down", 0.56)})

    assert intents
    assert intents[0].outcome == "Down"


def test_hedged_mm_preserves_normal_two_sided_entry_when_pair_is_favorable() -> None:
    strategy = HedgedMarketMakerStrategy(StrategyConfig(strategy_mode="hedged-mm"), RiskConfig(max_market_notional=12, max_single_fill_notional=2))
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, Position(m.slug, m.condition_id), signal, {"Up": book("up", 0.40), "Down": book("down", 0.40)})

    assert {intent.outcome for intent in intents} == {"Up", "Down"}


def test_hedged_mm_blocks_expensive_profit_expansion_after_hedge_exists() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="hedged-mm", profit_expansion_pair_cost=1.00),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.cost["Up"] = 2
    position.shares["Up"] = 2 / 0.55
    position.cost["Down"] = 2
    position.shares["Down"] = 2 / 0.52

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.55), "Down": book("down", 0.52)})

    assert not intents


def test_hedged_mm_allows_high_quality_profit_expansion_after_hedge_exists() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="hedged-mm", profit_expansion_pair_cost=1.00),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.cost["Up"] = 2
    position.shares["Up"] = 2 / 0.40
    position.cost["Down"] = 2
    position.shares["Down"] = 2 / 0.40

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.40), "Down": book("down", 0.40)})

    assert intents
    assert sum(intent.max_notional for intent in intents) <= 4


def test_hedged_mm_reserves_remaining_budget_across_batch() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="pair-only", profit_expansion_pair_cost=1.00),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2, min_order_notional=1),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.cost["Up"] = 5
    position.shares["Up"] = 5 / 0.20
    position.cost["Down"] = 5
    position.shares["Down"] = 5 / 0.20

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.20), "Down": book("down", 0.20)})

    assert sum(intent.max_notional for intent in intents) == 2


def test_hedged_mm_allows_lower_side_to_rebalance_at_imbalance_cap() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="pair-only", profit_expansion_pair_cost=1.00),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2, max_unpaired_notional=2),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.cost["Up"] = 6
    position.shares["Up"] = 6 / 0.20
    position.cost["Down"] = 4
    position.shares["Down"] = 4 / 0.20

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.20), "Down": book("down", 0.20)})

    assert intents
    assert intents[0].outcome == "Down"
    assert intents[0].max_notional == 2


def test_hedged_mm_blocks_heavier_side_when_imbalance_is_at_cap() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="hedged-mm", profit_expansion_pair_cost=1.00),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2, max_unpaired_notional=2),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1200, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.cost["Up"] = 6
    position.shares["Up"] = 6 / 0.20
    position.cost["Down"] = 4
    position.shares["Down"] = 4 / 0.20

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.20), "Down": book("down", 0.20)})

    assert all(intent.outcome != "Up" for intent in intents)


def test_hedged_mm_rejects_heavier_side_when_unpaired_room_is_sub_minimum() -> None:
    strategy = HedgedMarketMakerStrategy(
        StrategyConfig(strategy_mode="hedged-mm"),
        RiskConfig(max_market_notional=12, max_single_fill_notional=2, max_unpaired_notional=2, min_order_notional=1),
    )
    window = PriceWindow()
    m = market()
    window.add(CryptoTick("BTC", 100.0, 1000, "test"))
    tick = CryptoTick("BTC", 100.2, 1050, "test")
    window.add(tick)
    position = Position(m.slug, m.condition_id)
    position.shares["Up"] = 10
    position.cost["Up"] = 2.0
    position.shares["Down"] = 10
    position.cost["Down"] = 0.5

    signal = strategy.build_signal(m, tick, window)
    intents = strategy.propose_orders(m, position, signal, {"Up": book("up", 0.05), "Down": book("down", 0.05)})

    assert all(intent.outcome != "Up" for intent in intents)
