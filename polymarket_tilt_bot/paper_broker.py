from __future__ import annotations

import time
from typing import Literal

from .models import Fill, Market, OrderBook, OrderIntent


class PaperBroker:
    def __init__(self, slippage_bps: float = 0.0, min_order_notional: float = 1.0) -> None:
        self.slippage_bps = slippage_bps
        self.min_order_notional = min_order_notional

    def execute(
        self,
        market: Market,
        intent: OrderIntent,
        book: OrderBook,
        now: float | None = None,
    ) -> Fill | None:
        if intent.max_notional < self.min_order_notional:
            return None
        remaining_notional = intent.max_notional
        filled_size = 0.0
        spent = 0.0
        slip_multiplier = 1.0 + self.slippage_bps / 10_000
        for level in sorted(book.asks, key=lambda item: item.price):
            price = level.price * slip_multiplier
            if price > intent.limit_price:
                break
            level_notional = level.size * price
            take_notional = min(remaining_notional, level_notional)
            if take_notional <= 0:
                break
            take_size = take_notional / price
            filled_size += take_size
            spent += take_notional
            remaining_notional -= take_notional
            if remaining_notional <= 1e-9:
                break
        if spent < self.min_order_notional or filled_size <= 0:
            return None
        avg_price = spent / filled_size
        return Fill(
            market_slug=market.slug,
            condition_id=market.condition_id,
            outcome=intent.outcome,
            token_id=intent.token_id,
            price=avg_price,
            size=filled_size,
            notional=spent,
            timestamp=now if now is not None else time.time(),
            simulated=True,
            reason=intent.reason,
        )
