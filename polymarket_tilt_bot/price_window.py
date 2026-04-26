from __future__ import annotations

import statistics
from collections import deque
from typing import Literal

from .models import CryptoTick


class PriceWindow:
    def __init__(self, max_seconds: float = 360.0) -> None:
        self.max_seconds = max_seconds
        self._ticks: dict[Literal["BTC", "ETH"], deque[CryptoTick]] = {"BTC": deque(), "ETH": deque()}

    def add(self, tick: CryptoTick) -> None:
        ticks = self._ticks[tick.asset]
        ticks.append(tick)
        cutoff = tick.timestamp - self.max_seconds
        while ticks and ticks[0].timestamp < cutoff:
            ticks.popleft()

    def latest(self, asset: Literal["BTC", "ETH"]) -> CryptoTick | None:
        ticks = self._ticks[asset]
        return ticks[-1] if ticks else None

    def price_at_or_after(self, asset: Literal["BTC", "ETH"], timestamp: float) -> float | None:
        for tick in self._ticks[asset]:
            if tick.timestamp >= timestamp:
                return tick.price
        return None

    def momentum_bps(self, asset: Literal["BTC", "ETH"], window_seconds: float) -> float:
        ticks = self._ticks[asset]
        if len(ticks) < 2:
            return 0.0
        latest = ticks[-1]
        target = latest.timestamp - window_seconds
        previous = ticks[0]
        for tick in reversed(ticks):
            if tick.timestamp <= target:
                previous = tick
                break
        return ((latest.price / previous.price) - 1.0) * 10_000 if previous.price else 0.0

    def volatility_bps(self, asset: Literal["BTC", "ETH"]) -> float:
        ticks = self._ticks[asset]
        if len(ticks) < 3:
            return 0.0
        returns = []
        prev = ticks[0]
        for tick in list(ticks)[1:]:
            if prev.price:
                returns.append(((tick.price / prev.price) - 1.0) * 10_000)
            prev = tick
        return statistics.pstdev(returns) if len(returns) > 1 else 0.0
