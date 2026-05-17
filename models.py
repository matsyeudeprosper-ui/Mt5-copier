# models.py
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

@dataclass
class PositionInfo:
    ticket: int
    profit: float
    volume: float
    entry: float
    is_buy: bool

@dataclass
class Candidate:
    loser_idx: int
    winner_idx: List[int]
    total_winner_profit: float
    loser_loss: float
    net_ratio: float
    efficiency: float
    volume_released: float
    risk_score: float = 0.0
    clean_score: float = 0.0
    future_score: float = 0.0
    score: float = 0.0
    valid: bool = True

@dataclass
class BasketHealth:
    total_lots: float = 0.0
    directional_imbalance: float = 0.0
    weighted_avg_dist_to_be: float = 0.0
    floating_dd: float = 0.0
    realized_gains: float = 0.0
    hedge_ratio: float = 0.0
    margin_usage: float = 0.0
    num_losers: int = 0
    exposure_concentration: float = 0.0

@dataclass
class WinnerAction:
    ticket: int
    close_type: str          # "full" or "partial"
    close_volume: float
    expected_profit: float

@dataclass
class PairingDecision:
    execute_now: bool
    reason: str
    loser_ticket: int
    winner_actions: List[WinnerAction]
    required_winner_profit: float
    expected_net_profit: float
    required_price: float
    score: float
    net_ratio: float
    future_safe: bool
    basket_health_before: Dict[str, Any]
    basket_health_after: Dict[str, Any]

@dataclass
class PairingConfig:
    target_net_profit_r: float = 0.3
    min_residual_volume: float = 0.01
    min_remaining_winners: int = 1
    max_future_atr_distance: float = 5.0
    pairing_cooldown_seconds: int = 60
    enable_pairing_engine: bool = True
    execution_ratio_threshold: float = 1.5

@dataclass
class PairingEngineState:
    last_pairing_time: int = 0
    pairing_visual_locked: bool = False
    pairing_visual_price: float = 0.0
    pairing_visual_loser_ticket: int = 0
    pairing_visual_winner_ticket: int = 0
    pairing_net_profit: float = 0.0
    pairing_immediately_possible: bool = False
    pairing_in_progress: bool = False