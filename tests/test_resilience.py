import requests

from polymarket_tilt_bot.clients import ApiError, HttpClient
from polymarket_tilt_bot.config import RuntimeConfig
from polymarket_tilt_bot.models import Position
from polymarket_tilt_bot.runner import PaperTradingBot


class TimeoutSession:
    def headers(self):
        return {}

    def get(self, url, params=None, timeout=None):
        raise requests.exceptions.ReadTimeout("boom")


def test_http_client_wraps_request_timeouts_as_api_error(monkeypatch) -> None:
    client = HttpClient(RuntimeConfig())
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("boom")))

    try:
        client.get_json("https://example.test")
    except ApiError as exc:
        assert "failed" in str(exc)
    else:
        raise AssertionError("expected ApiError")


def test_unrealized_pnl_fallback_does_not_raise_on_clob_timeout(tmp_path) -> None:
    bot = PaperTradingBot.__new__(PaperTradingBot)
    bot.config = type("Config", (), {"risk": type("Risk", (), {"starting_balance": 300})()})()
    bot.clob = type("Clob", (), {"get_books_for_market": lambda self, market: (_ for _ in ()).throw(ApiError("timeout"))})()
    bot._market_from_position = lambda slug: object()

    position = Position("m1", "0x1")
    position.cost["Up"] = 2.0
    position.shares["Up"] = 10.0

    assert bot._estimate_unrealized_pnl([position]) == -2.0
