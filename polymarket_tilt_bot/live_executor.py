from __future__ import annotations

from .models import Market, OrderBook, OrderIntent


class LiveTradingDisabled(RuntimeError):
    pass


class LiveExecutor:
    """Placeholder boundary for real CLOB order placement.

    Real trading requires Polymarket CLOB authentication, API keys, wallet
    signing, allowances, and jurisdiction checks. This class exists so the rest
    of the bot has a clean execution boundary, but it deliberately refuses to
    trade until those pieces are implemented and reviewed.
    """

    def execute(self, market: Market, intent: OrderIntent, book: OrderBook) -> None:
        raise LiveTradingDisabled(
            "Live trading is intentionally disabled. Use PaperBroker until API keys, "
            "wallet signing, allowances, and risk controls are explicitly implemented."
        )
