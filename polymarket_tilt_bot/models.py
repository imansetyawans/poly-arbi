from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


Outcome = Literal["Up", "Down"]


@dataclass(frozen=True)
class Token:
    outcome: Outcome
    token_id: str


@dataclass(frozen=True)
class Market:
    asset: Literal["BTC", "ETH"]
    slug: str
    condition_id: str
    title: str
    start_ts: int
    end_ts: int
    up_token: str
    down_token: str
    accepting_orders: bool
    closed: bool
    resolution_source: str = ""
    min_order_size: float = 5.0
    price_to_beat: float | None = None

    @property
    def tokens(self) -> list[Token]:
        return [Token("Up", self.up_token), Token("Down", self.down_token)]


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    timestamp_ms: int | None = None

    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)

    @property
    def ask_depth(self) -> float:
        return sum(level.size for level in self.asks)


@dataclass(frozen=True)
class CryptoTick:
    asset: Literal["BTC", "ETH"]
    price: float
    timestamp: float
    source: str


@dataclass(frozen=True)
class Signal:
    market_slug: str
    probability_up: float
    confidence: float
    direction: Outcome
    seconds_elapsed: float
    seconds_remaining: float
    distance_bps: float
    momentum_bps: float
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    market_slug: str
    outcome: Outcome
    token_id: str
    max_notional: float
    limit_price: float
    reason: str


@dataclass(frozen=True)
class Fill:
    market_slug: str
    condition_id: str
    outcome: Outcome
    token_id: str
    price: float
    size: float
    notional: float
    timestamp: float
    simulated: bool = True
    reason: str = ""


@dataclass
class Position:
    market_slug: str
    condition_id: str
    shares: dict[Outcome, float] = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})
    cost: dict[Outcome, float] = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})

    def add_fill(self, fill: Fill) -> None:
        self.shares[fill.outcome] += fill.size
        self.cost[fill.outcome] += fill.notional

    def avg_price(self, outcome: Outcome) -> float:
        shares = self.shares[outcome]
        return self.cost[outcome] / shares if shares else 0.0

    @property
    def total_cost(self) -> float:
        return self.cost["Up"] + self.cost["Down"]

    @property
    def paired_shares(self) -> float:
        return min(self.shares["Up"], self.shares["Down"])

    @property
    def unpaired_side(self) -> Outcome | None:
        if self.shares["Up"] > self.shares["Down"]:
            return "Up"
        if self.shares["Down"] > self.shares["Up"]:
            return "Down"
        return None

    @property
    def unpaired_shares(self) -> float:
        return abs(self.shares["Up"] - self.shares["Down"])

    def resolve(self, winner: Outcome) -> dict[str, float | str | None]:
        payout = self.shares[winner]
        pnl = payout - self.total_cost
        up_avg = self.avg_price("Up")
        down_avg = self.avg_price("Down")
        paired_pnl = self.paired_shares * (1.0 - up_avg - down_avg)
        side = self.unpaired_side
        if side is None:
            unpaired_pnl = 0.0
        else:
            side_avg = self.avg_price(side)
            unpaired_pnl = self.unpaired_shares * ((1.0 - side_avg) if side == winner else -side_avg)
        return {
            "winner": winner,
            "payout": payout,
            "pnl": pnl,
            "total_cost": self.total_cost,
            "paired_shares": self.paired_shares,
            "paired_pnl": paired_pnl,
            "unpaired_side": side,
            "unpaired_shares": self.unpaired_shares,
            "unpaired_pnl": unpaired_pnl,
            "up_shares": self.shares["Up"],
            "down_shares": self.shares["Down"],
            "up_avg": up_avg,
            "down_avg": down_avg,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
