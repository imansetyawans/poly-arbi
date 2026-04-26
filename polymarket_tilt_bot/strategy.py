from __future__ import annotations

import math
from typing import Literal

from .config import RiskConfig, StrategyConfig
from .models import CryptoTick, Market, OrderBook, OrderIntent, Position, Signal
from .price_window import PriceWindow


class HedgedTiltStrategy:
    """JetFadil-inspired paper strategy.

    The strategy tries to reproduce the observed shape: buy both outcomes in small
    clips, but keep the larger unpaired exposure on the side that the live candle
    currently favors.
    """

    def __init__(self, strategy: StrategyConfig, risk: RiskConfig) -> None:
        self.strategy = strategy
        self.risk = risk
        self._open_prices: dict[str, float] = {}

    def update_open_price(self, market: Market, tick: CryptoTick) -> float:
        if market.price_to_beat:
            self._open_prices[market.slug] = market.price_to_beat
        return self._open_prices.setdefault(market.slug, tick.price)

    def build_signal(self, market: Market, tick: CryptoTick, window: PriceWindow) -> Signal:
        open_price = self.update_open_price(market, tick)
        seconds_elapsed = max(0.0, tick.timestamp - market.start_ts)
        seconds_remaining = max(0.0, market.end_ts - tick.timestamp)
        distance_bps = ((tick.price / open_price) - 1.0) * 10_000 if open_price else 0.0
        momentum_bps = window.momentum_bps(market.asset, self.strategy.momentum_window_seconds)
        volatility_bps = max(window.volatility_bps(market.asset), self.strategy.volatility_floor_bps)

        z_score = (distance_bps + 0.35 * momentum_bps) / volatility_bps
        probability_up = 1.0 / (1.0 + math.exp(-z_score))
        probability_up = min(0.97, max(0.03, probability_up))
        confidence = abs(probability_up - 0.5) * 2.0
        direction: Literal["Up", "Down"] = "Up" if probability_up >= 0.5 else "Down"
        return Signal(
            market_slug=market.slug,
            probability_up=probability_up,
            confidence=confidence,
            direction=direction,
            seconds_elapsed=seconds_elapsed,
            seconds_remaining=seconds_remaining,
            distance_bps=distance_bps,
            momentum_bps=momentum_bps,
            reason=(
                f"distance={distance_bps:.2f}bps momentum={momentum_bps:.2f}bps "
                f"vol={volatility_bps:.2f}bps"
            ),
        )

    def propose_orders(
        self,
        market: Market,
        position: Position,
        signal: Signal,
        books: dict[Literal["Up", "Down"], OrderBook],
    ) -> list[OrderIntent]:
        if signal.seconds_remaining < self.risk.min_seconds_left:
            return []
        if signal.seconds_elapsed > self.risk.no_new_market_after_seconds:
            return []
        if position.total_cost >= self.risk.max_market_notional:
            return []

        up_ask = books["Up"].best_ask
        down_ask = books["Down"].best_ask
        if up_ask is None or down_ask is None:
            return []

        # Avoid paying too much for a synthetic 1 USDC payout pair.
        pair_cost = up_ask + down_ask
        allow_hedge = pair_cost <= self.strategy.max_pair_cost

        target_cost = self._target_costs(signal)
        intents: list[OrderIntent] = []
        for outcome in ("Up", "Down"):
            ask = books[outcome].best_ask
            if ask is None:
                continue
            wanted = target_cost[outcome] - position.cost[outcome]
            remaining_budget = self.risk.max_market_notional - position.total_cost
            notional = min(wanted, remaining_budget, self.risk.max_single_fill_notional)
            if notional < market.min_order_size:
                continue

            is_tilt_side = outcome == signal.direction and signal.confidence >= self.strategy.min_confidence_to_tilt
            is_hedge_side = outcome != signal.direction
            if is_hedge_side and not allow_hedge:
                continue
            if is_hedge_side and ask > self.strategy.max_hedge_price:
                continue

            fair = signal.probability_up if outcome == "Up" else 1.0 - signal.probability_up
            max_price = self.strategy.max_tilt_price if is_tilt_side else self.strategy.max_hedge_price
            if is_tilt_side:
                max_price = min(max_price, fair + self.strategy.price_edge_buffer)
            if ask > max_price:
                continue

            intents.append(
                OrderIntent(
                    market_slug=market.slug,
                    outcome=outcome,
                    token_id=market.up_token if outcome == "Up" else market.down_token,
                    max_notional=notional,
                    limit_price=round(max_price, 3),
                    reason=f"{'tilt' if is_tilt_side else 'hedge'} {signal.reason}",
                )
            )
        return intents

    def _target_costs(self, signal: Signal) -> dict[Literal["Up", "Down"], float]:
        confidence = signal.confidence
        hedge = self.strategy.base_hedge_notional * max(0.35, 1.0 - 0.45 * confidence)
        tilt_room = max(0.0, self.risk.max_market_notional - 2.0 * hedge)
        tilt = tilt_room * min(1.0, confidence / max(self.strategy.strong_confidence, 0.01))
        if confidence < self.strategy.min_confidence_to_tilt:
            tilt = 0.0
        target = {"Up": hedge, "Down": hedge}
        target[signal.direction] += tilt
        return target
