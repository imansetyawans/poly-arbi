from __future__ import annotations

import time
from typing import Literal

from .clients import GammaClient
from .models import Market


PREFIX_BY_ASSET: dict[Literal["BTC", "ETH"], str] = {
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
}


class MarketScanner:
    def __init__(self, gamma: GammaClient) -> None:
        self.gamma = gamma

    def current_epoch(self, now: float | None = None) -> int:
        ts = int(now if now is not None else time.time())
        return ts - (ts % 300)

    def candidate_slugs(self, asset: Literal["BTC", "ETH"], now: float | None = None) -> list[str]:
        epoch = self.current_epoch(now)
        prefix = PREFIX_BY_ASSET[asset]
        return [f"{prefix}-{epoch + offset}" for offset in (-300, 0, 300)]

    def scan(self, assets: tuple[Literal["BTC", "ETH"], ...], now: float | None = None) -> list[Market]:
        markets: list[Market] = []
        for asset in assets:
            for slug in self.candidate_slugs(asset, now):
                event = self.gamma.get_event_by_slug(slug)
                if not event:
                    continue
                market = self.gamma.parse_crypto_market(event, asset)
                now_ts = now if now is not None else time.time()
                is_live_window = market and market.start_ts <= now_ts < market.end_ts
                if market and is_live_window and market.accepting_orders and not market.closed:
                    markets.append(market)
        return sorted(markets, key=lambda market: (market.end_ts, market.asset))
