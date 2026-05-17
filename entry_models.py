# entry_models.py
from pydantic import BaseModel
from typing import List, Literal

class PositionInfo(BaseModel):
    price: float
    isBuy: bool
    volume: float

class EntryDecisionRequest(BaseModel):
    mode: Literal["initial_entry", "grid_addition"]

    symbol: str

    bid: float
    ask: float
    point: float

    # Exact trigger level (EA determined)
    trigger_level_price: float
    trigger_level_is_high: bool
    trigger_level_index: int   # must be >= 0

    direction_locked: bool
    current_direction_is_buy: bool

    lowest_buy_entry: float = 0.0
    highest_sell_entry: float = 0.0

    last_buy_addition_price: float = 0.0
    last_sell_addition_price: float = 0.0

    required_spacing: float = 0.0

    positions: List[PositionInfo]


class EntryDecisionResponse(BaseModel):
    success: bool          # false only on internal error
    decision: Literal["execute_buy", "execute_sell", "none"]
    reason: str = ""
    level_price: float = 0.0
    level_index: int = -1