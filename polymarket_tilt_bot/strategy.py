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
        allow_starter: bool = True,
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
        planned_notional = 0.0
        for outcome in ("Up", "Down"):
            ask = books[outcome].best_ask
            if ask is None:
                continue
            wanted = target_cost[outcome] - position.cost[outcome]
            remaining_budget = self.risk.max_market_notional - position.total_cost - planned_notional
            notional = min(wanted, remaining_budget, self.risk.max_single_fill_notional)
            if notional < self.risk.min_order_notional:
                continue
            estimated_shares = notional / ask if ask else 0.0
            if estimated_shares < market.min_order_size:
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
            planned_notional += notional
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


class HedgedMarketMakerStrategy(HedgedTiltStrategy):
    """Two-sided inventory strategy with small late directional bias."""

    def propose_orders(
        self,
        market: Market,
        position: Position,
        signal: Signal,
        books: dict[Literal["Up", "Down"], OrderBook],
        allow_starter: bool = True,
    ) -> list[OrderIntent]:
        if signal.seconds_remaining < self.risk.min_seconds_left:
            return []
        if signal.seconds_elapsed > self.risk.no_new_market_after_seconds:
            return []
        if position.total_cost >= self.risk.max_market_notional:
            return []
        if self._is_flat(position) and signal.seconds_elapsed > self.strategy.starter_entry_cutoff_seconds:
            return []
        if self._is_flat(position) and not allow_starter:
            return []

        up_ask = books["Up"].best_ask
        down_ask = books["Down"].best_ask
        if up_ask is None or down_ask is None:
            return []
        if self._is_flat(position) and up_ask + down_ask > self.strategy.hedged_mm_max_pair_cost:
            return []

        target_cost = self._target_costs(signal)
        if self.strategy.strategy_mode == "pair-only":
            target_cost = {"Up": self.risk.max_market_notional / 2, "Down": self.risk.max_market_notional / 2}

        intents: list[OrderIntent] = []
        planned_notional = 0.0
        planned_cost = dict(position.cost)
        planned_shares = dict(position.shares)
        for outcome in self._order_priority(position, signal):
            ask = books[outcome].best_ask
            if ask is None:
                continue
            wanted = target_cost[outcome] - planned_cost[outcome]
            remaining_budget = self.risk.max_market_notional - position.total_cost - planned_notional
            notional = min(wanted, remaining_budget, self.risk.max_single_fill_notional)
            other = "Down" if outcome == "Up" else "Up"
            completes_planned_hedge = planned_cost[other] > 0 and planned_cost[outcome] <= 0
            if notional > 0 and not completes_planned_hedge and not self._is_completion_side(outcome, position):
                notional = min(notional, self._imbalance_room_for(outcome, planned_cost))
            if notional <= 0:
                continue
            if notional < self.risk.min_order_notional:
                continue
            if not self._price_is_allowed(outcome, ask, notional, position, planned_cost, planned_shares, signal, up_ask, down_ask):
                continue
            if notional / ask < market.min_order_size:
                continue
            if self._is_chasing(outcome, ask, position, signal):
                continue
            intents.append(
                OrderIntent(
                    market_slug=market.slug,
                    outcome=outcome,
                    token_id=market.up_token if outcome == "Up" else market.down_token,
                    max_notional=notional,
                    limit_price=round(min(0.999, ask + self.strategy.price_edge_buffer), 3),
                    reason=f"hedged-mm {signal.reason}",
                )
            )
            planned_notional += notional
            planned_cost[outcome] += notional
            planned_shares[outcome] += notional / ask
        return intents

    def _target_costs(self, signal: Signal) -> dict[Literal["Up", "Down"], float]:
        half = self.risk.max_market_notional / 2.0
        if self.strategy.strategy_mode == "pair-only":
            return {"Up": half, "Down": half}
        bias = min(self.strategy.max_directional_bias, signal.confidence * self.strategy.max_directional_bias)
        if signal.seconds_elapsed < self.strategy.rebalance_start_seconds:
            bias *= 0.35
        target = {"Up": half, "Down": half}
        target[signal.direction] = half * (1.0 + bias)
        target["Down" if signal.direction == "Up" else "Up"] = half * (1.0 - bias)
        return target

    def _order_priority(self, position: Position, signal: Signal) -> list[Literal["Up", "Down"]]:
        if position.cost["Up"] <= 0 and position.cost["Down"] > 0:
            return ["Up", "Down"]
        if position.cost["Down"] <= 0 and position.cost["Up"] > 0:
            return ["Down", "Up"]
        if position.cost["Up"] > 0 and position.cost["Down"] > 0:
            if position.cost["Up"] + self.risk.max_single_fill_notional < position.cost["Down"]:
                return ["Up", "Down"]
            if position.cost["Down"] + self.risk.max_single_fill_notional < position.cost["Up"]:
                return ["Down", "Up"]
        if signal.seconds_elapsed >= self.strategy.rebalance_start_seconds:
            return [signal.direction, "Down" if signal.direction == "Up" else "Up"]
        return ["Up", "Down"]

    def _is_chasing(self, outcome: Literal["Up", "Down"], ask: float, position: Position, signal: Signal) -> bool:
        if ask <= self.strategy.avoid_chase_price:
            return False
        needs_completion = self._is_completion_side(outcome, position) or position.cost[outcome] < self.risk.max_single_fill_notional
        return not needs_completion

    def _is_flat(self, position: Position) -> bool:
        return position.cost["Up"] <= 0 and position.cost["Down"] <= 0

    def _is_completion_side(self, outcome: Literal["Up", "Down"], position: Position) -> bool:
        other = "Down" if outcome == "Up" else "Up"
        return position.cost[other] > 0 and position.cost[outcome] <= 0

    def _imbalance_room_for(self, outcome: Literal["Up", "Down"], cost: dict[Literal["Up", "Down"], float]) -> float:
        cap = self._max_unpaired_notional()
        other = "Down" if outcome == "Up" else "Up"
        return max(0.0, cost[other] + cap - cost[outcome])

    def _max_unpaired_notional(self) -> float:
        if self.risk.max_unpaired_notional is not None:
            return self.risk.max_unpaired_notional
        return min(self.risk.max_single_fill_notional, self.risk.max_market_notional * 0.25)

    def _price_is_allowed(
        self,
        outcome: Literal["Up", "Down"],
        ask: float,
        notional: float,
        position: Position,
        planned_cost: dict[Literal["Up", "Down"], float],
        planned_shares: dict[Literal["Up", "Down"], float],
        signal: Signal,
        up_ask: float,
        down_ask: float,
    ) -> bool:
        if self._is_flat(position):
            return up_ask + down_ask <= self.strategy.hedged_mm_max_pair_cost
        if not self._is_completion_side(outcome, position):
            if planned_cost["Up"] <= 0 or planned_cost["Down"] <= 0:
                return True
            return self._projected_pair_cost(planned_cost, planned_shares, outcome, ask, notional) <= self.strategy.profit_expansion_pair_cost

        held_side = "Down" if outcome == "Up" else "Up"
        held_avg = position.avg_price(held_side)
        completion_pair_cost = held_avg + ask
        return completion_pair_cost <= self._completion_pair_cost_limit(signal)

    def _projected_pair_cost(
        self,
        cost: dict[Literal["Up", "Down"], float],
        shares: dict[Literal["Up", "Down"], float],
        outcome: Literal["Up", "Down"],
        ask: float,
        notional: float,
    ) -> float:
        projected: dict[Literal["Up", "Down"], float] = {}
        for side in ("Up", "Down"):
            projected_cost = cost[side]
            projected_shares = shares[side]
            if side == outcome:
                projected_cost += notional
                projected_shares += notional / ask
            projected[side] = projected_cost / projected_shares if projected_shares else 0.0
        return projected["Up"] + projected["Down"]

    def _completion_pair_cost_limit(self, signal: Signal) -> float:
        if signal.seconds_elapsed >= self.strategy.rebalance_start_seconds:
            return self.strategy.completion_pair_cost_late
        if signal.seconds_elapsed >= self.strategy.hedge_completion_seconds:
            return self.strategy.completion_pair_cost_mid
        return self.strategy.hedged_mm_max_pair_cost


class JetFadilStrategy(HedgedMarketMakerStrategy):
    """More aggressive hedged-tilt mode inspired by JetFadil account behavior."""

    def propose_orders(
        self,
        market: Market,
        position: Position,
        signal: Signal,
        books: dict[Literal["Up", "Down"], OrderBook],
        allow_starter: bool = True,
    ) -> list[OrderIntent]:
        if signal.seconds_remaining < self.risk.min_seconds_left:
            return []
        if signal.seconds_elapsed > self.risk.no_new_market_after_seconds:
            return []
        if position.total_cost >= self.risk.max_market_notional:
            return []
        if self._is_flat(position) and not allow_starter:
            return []

        up_ask = books["Up"].best_ask
        down_ask = books["Down"].best_ask
        if up_ask is None or down_ask is None:
            return []
        if self._is_flat(position) and up_ask + down_ask > self.strategy.jetfadil_entry_pair_cost:
            return []

        target_cost = self._target_costs(signal)
        intents: list[OrderIntent] = []
        planned_notional = 0.0
        planned_cost = dict(position.cost)
        planned_shares = dict(position.shares)
        for outcome in self._order_priority(position, signal):
            ask = books[outcome].best_ask
            if ask is None:
                continue
            wanted = target_cost[outcome] - planned_cost[outcome]
            remaining_budget = self.risk.max_market_notional - position.total_cost - planned_notional
            notional = min(wanted, remaining_budget, self.risk.max_single_fill_notional)
            if notional <= 0:
                continue

            other = "Down" if outcome == "Up" else "Up"
            is_completion = planned_cost[other] > 0 and planned_cost[outcome] <= 0
            if planned_cost["Up"] > 0 and planned_cost["Down"] > 0 and not is_completion:
                notional = min(notional, self._imbalance_room_for(outcome, planned_cost))
            if notional < self.risk.min_order_notional:
                continue
            if not self._price_is_allowed(outcome, ask, notional, position, planned_cost, planned_shares, signal, up_ask, down_ask):
                continue
            if notional / ask < market.min_order_size:
                continue

            intents.append(
                OrderIntent(
                    market_slug=market.slug,
                    outcome=outcome,
                    token_id=market.up_token if outcome == "Up" else market.down_token,
                    max_notional=notional,
                    limit_price=round(min(0.999, ask + self.strategy.price_edge_buffer), 3),
                    reason=f"jetfadil {signal.reason}",
                )
            )
            planned_notional += notional
            planned_cost[outcome] += notional
            planned_shares[outcome] += notional / ask

        if self._is_flat(position) and {intent.outcome for intent in intents} != {"Up", "Down"}:
            return []
        return intents

    def _target_costs(self, signal: Signal) -> dict[Literal["Up", "Down"], float]:
        half = self.risk.max_market_notional / 2.0
        bias = min(self.strategy.jetfadil_max_directional_bias, signal.confidence * self.strategy.jetfadil_max_directional_bias * 1.8)
        target = {"Up": half, "Down": half}
        target[signal.direction] = half * (1.0 + bias)
        target["Down" if signal.direction == "Up" else "Up"] = half * (1.0 - bias)
        return target

    def _order_priority(self, position: Position, signal: Signal) -> list[Literal["Up", "Down"]]:
        if position.cost["Up"] <= 0 and position.cost["Down"] > 0:
            return ["Up", "Down"]
        if position.cost["Down"] <= 0 and position.cost["Up"] > 0:
            return ["Down", "Up"]
        if position.cost["Up"] > 0 and position.cost["Down"] > 0:
            if position.cost["Up"] > position.cost["Down"] + self._max_unpaired_notional():
                return ["Down", "Up"]
            if position.cost["Down"] > position.cost["Up"] + self._max_unpaired_notional():
                return ["Up", "Down"]
        return [signal.direction, "Down" if signal.direction == "Up" else "Up"]

    def _price_is_allowed(
        self,
        outcome: Literal["Up", "Down"],
        ask: float,
        notional: float,
        position: Position,
        planned_cost: dict[Literal["Up", "Down"], float],
        planned_shares: dict[Literal["Up", "Down"], float],
        signal: Signal,
        up_ask: float,
        down_ask: float,
    ) -> bool:
        if self._is_flat(position):
            return up_ask + down_ask <= self.strategy.jetfadil_entry_pair_cost
        if planned_cost["Up"] <= 0 or planned_cost["Down"] <= 0:
            held_side = "Down" if outcome == "Up" else "Up"
            held_avg = position.avg_price(held_side)
            return held_avg + ask <= self._completion_pair_cost_limit(signal)
        return self._projected_pair_cost(planned_cost, planned_shares, outcome, ask, notional) <= self.strategy.profit_expansion_pair_cost
