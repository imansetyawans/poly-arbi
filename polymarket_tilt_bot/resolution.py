from __future__ import annotations

from typing import Literal

from .clients import GammaClient
from .models import Market


class ResolutionClient:
    def __init__(self, gamma: GammaClient) -> None:
        self.gamma = gamma

    def get_winner(self, market: Market) -> Literal["Up", "Down"] | None:
        event = self.gamma.get_event_by_slug(market.slug)
        if not event:
            return None
        row = (event.get("markets") or [{}])[0]
        prices_raw = row.get("outcomePrices")
        outcomes_raw = row.get("outcomes")
        if not prices_raw or not outcomes_raw:
            return None
        import json

        outcomes = json.loads(outcomes_raw)
        prices = [float(x) for x in json.loads(prices_raw)]
        if len(outcomes) != len(prices):
            return None
        if max(prices) < 0.99:
            return None
        return outcomes[prices.index(max(prices))]
