# entry_models.py
from dataclasses import dataclass
from typing import List, Literal

@dataclass
class PositionInfo:
    price: float
    isBuy: bool
    volume: float

@dataclass
class EntryDecisionRequest:
    mode: Literal["initial_entry", "grid_addition"]

    symbol: str

    bid: float
    ask: float
    point: float

    # Exact trigger level (EA determined)
    trigger_level_price: float
    trigger_level_is_high: bool
    trigger_level_index: int

    direction_locked: bool
    current_direction_is_buy: bool

    lowest_buy_entry: float = 0.0
    highest_sell_entry: float = 0.0

    last_buy_addition_price: float = 0.0
    last_sell_addition_price: float = 0.0

    required_spacing: float = 0.0

    positions: List[PositionInfo]


@dataclass
class EntryDecisionResponse:
    success: bool
    decision: Literal["execute_buy", "execute_sell", "none"]
    reason: str = ""
    level_price: float = 0.0
    level_index: int = -1