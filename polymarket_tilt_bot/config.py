from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


StrategyMode = Literal["current", "hedged-mm", "pair-only", "jetfadil"]
StorageMode = Literal["csv", "sqlite"]


@dataclass(frozen=True)
class RiskConfig:
    starting_balance: float = 1_000.0
    max_market_notional: float = 100.0
    max_single_fill_notional: float = 10.0
    max_unpaired_notional: float | None = None
    min_order_notional: float = 1.0
    max_daily_loss: float = 150.0
    max_consecutive_losses: int = 4
    reserve_fraction: float = 0.50
    min_seconds_left: float = 15.0
    no_new_market_after_seconds: float = 285.0


@dataclass(frozen=True)
class StrategyConfig:
    base_hedge_notional: float = 12.0
    max_pair_cost: float = 1.08
    hedged_mm_max_pair_cost: float = 1.03
    min_confidence_to_tilt: float = 0.12
    strong_confidence: float = 0.55
    price_edge_buffer: float = 0.015
    max_hedge_price: float = 0.62
    max_tilt_price: float = 0.90
    volatility_floor_bps: float = 3.0
    momentum_window_seconds: float = 20.0
    max_directional_bias: float = 0.10
    hedge_completion_seconds: float = 120.0
    rebalance_start_seconds: float = 180.0
    starter_entry_cutoff_seconds: float = 90.0
    completion_pair_cost_mid: float = 1.05
    completion_pair_cost_late: float = 1.08
    profit_expansion_pair_cost: float = 1.00
    jetfadil_entry_pair_cost: float = 1.00
    jetfadil_early_entry_pair_cost: float = 0.98
    jetfadil_deep_value_pair_cost: float = 0.98
    jetfadil_min_entry_seconds: float = 5.0
    jetfadil_min_confidence: float = 0.05
    jetfadil_strong_tilt_confidence: float = 0.35
    jetfadil_max_directional_bias: float = 0.20
    jetfadil_starter_entry_cutoff_seconds: float = 240.0
    jetfadil_core_pair_fraction: float = 0.80
    jetfadil_pre_late_expansion_pair_cost: float = 0.85
    jetfadil_late_expansion_seconds: float = 240.0
    bad_regime_window: int = 20
    bad_regime_min_completion_rate: float = 0.50
    bad_regime_guard_enabled: bool = True
    avoid_chase_price: float = 0.85
    strategy_mode: StrategyMode = "hedged-mm"


@dataclass(frozen=True)
class RuntimeConfig:
    assets: tuple[Literal["BTC", "ETH"], ...] = ("BTC", "ETH")
    poll_seconds: float = 5.0
    cycles: int = 12
    database_path: str = "paper_trades"
    storage_mode: StorageMode = "csv"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    request_timeout: float = 10.0
    user_agent: str = "polymarket-hedged-tilt-paper-bot/0.1"
    resolution_grace_seconds: float = 20.0
    resolution_poll_seconds: float = 30.0


@dataclass(frozen=True)
class BotConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
