from polymarket_tilt_bot.models import BookLevel, Market, OrderBook, OrderIntent
from polymarket_tilt_bot.paper_broker import PaperBroker


def test_paper_broker_fills_against_asks_up_to_limit() -> None:
    market = Market("BTC", "slug", "0x1", "title", 1000, 1300, "up", "down", True, False)
    book = OrderBook("up", bids=[], asks=[BookLevel(0.50, 10), BookLevel(0.52, 10)])
    intent = OrderIntent("slug", "Up", "up", max_notional=7.6, limit_price=0.52, reason="test")

    fill = PaperBroker().execute(market, intent, book, now=1234)

    assert fill is not None
    assert round(fill.notional, 6) == 7.6
    assert round(fill.size, 6) == round(10 + (2.6 / 0.52), 6)
    assert fill.price > 0.50


def test_paper_broker_rejects_when_ask_above_limit() -> None:
    market = Market("BTC", "slug", "0x1", "title", 1000, 1300, "up", "down", True, False)
    book = OrderBook("up", bids=[], asks=[BookLevel(0.55, 10)])
    intent = OrderIntent("slug", "Up", "up", max_notional=5, limit_price=0.54, reason="test")

    assert PaperBroker().execute(market, intent, book, now=1234) is None
