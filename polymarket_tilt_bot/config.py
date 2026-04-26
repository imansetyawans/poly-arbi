from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class RiskConfig:
    starting_balance: float = 1_000.0
    max_market_notional: float = 100.0
    max_single_fill_notional: float = 10.0
    max_daily_loss: float = 150.0
    max_consecutive_losses: int = 4
    reserve_fraction: float = 0.50
    min_seconds_left: float = 15.0
    no_new_market_after_seconds: float = 285.0


@dataclass(frozen=True)
class StrategyConfig:
    base_hedge_notional: float = 12.0
    max_pair_cost: float = 1.08
    min_confidence_to_tilt: float = 0.12
    strong_confidence: float = 0.55
    price_edge_buffer: float = 0.015
    max_hedge_price: float = 0.62
    max_tilt_price: float = 0.90
    volatility_floor_bps: float = 3.0
    momentum_window_seconds: float = 20.0


@dataclass(frozen=True)
class RuntimeConfig:
    assets: tuple[Literal["BTC", "ETH"], ...] = ("BTC", "ETH")
    poll_seconds: float = 5.0
    cycles: int = 12
    database_path: str = "paper_trades.sqlite"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    request_timeout: float = 10.0
    user_agent: str = "polymarket-hedged-tilt-paper-bot/0.1"


@dataclass(frozen=True)
class BotConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
