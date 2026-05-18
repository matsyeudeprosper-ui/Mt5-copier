# entry_models.py
from dataclasses import dataclass
from typing import List

@dataclass
class PositionInfo:
    price: float
    isBuy: bool
    volume: float

@dataclass
class EntryDecisionRequest:
    mode: str                         # "initial_entry" or "grid_addition"
    symbol: str
    bid: float
    ask: float
    point: float
    trigger_level_price: float
    trigger_level_is_high: bool
    trigger_level_index: int
    direction_locked: bool
    current_direction_is_buy: bool
    positions: List[PositionInfo]

    lowest_buy_entry: float = 0.0
    highest_sell_entry: float = 0.0
    last_buy_addition_price: float = 0.0
    last_sell_addition_price: float = 0.0
    required_spacing: float = 0.0

@dataclass
class EntryDecisionResponse:
    success: bool
    decision: str                     # "execute_buy", "execute_sell", or "none"
    reason: str = ""
    level_price: float = 0.0
    level_index: int = -1