# engines/basket_analyzer.py
from enum import Enum
from typing import List
from models import PositionInfo, BasketHealth
from pairing_engine import analyze_basket

class BasketMode(Enum):
    HEALTHY = 1      # low stress, good recovery fuel
    BUILDING = 2     # neutral, allow expansions
    STRESSED = 3     # significant drawdown, need pairing
    CRITICAL = 4     # near hardstop, emergency only
    RECOVERY = 5     # after pairing, stabilising

def classify_basket_mode(
    positions: List[PositionInfo],
    direction_locked: bool,
    current_direction_is_buy: bool,
    current_price: float,
    margin: float,
    free_margin: float,
    equity: float,
    protected_floor: float,
    initial_equity: float,
    atr_h4: float,
    mor: float
) -> BasketMode:
    # 1. Compute basket health
    bh = analyze_basket(
        positions, direction_locked, current_direction_is_buy,
        current_price, margin, free_margin, equity, protected_floor, initial_equity
    )
    
    # 2. Recovery fuel ratio (remaining winner profit / remaining loser loss)
    losers = [p for p in positions if p.profit < 0]
    winners = [p for p in positions if p.profit > 0]
    winner_profit = sum(w.profit for w in winners)
    loser_loss = sum(-l.profit for l in losers) if losers else 0
    fuel_ratio = winner_profit / loser_loss if loser_loss > 0 else 999.0
    
    # 3. Hardstop proximity
    # (simplified: if floating DD > 80% of max allowed)
    max_allowed_dd = max(initial_equity * 0.10, 500.0) if initial_equity > 0 else 1000.0
    dd_ratio = bh.floating_dd / max_allowed_dd if max_allowed_dd > 0 else 0
    
    # 4. Margin stress
    margin_stress = bh.margin_usage > 0.8
    
    # 5. Decision logic
    if dd_ratio > 0.9 or bh.floating_dd > max_allowed_dd * 0.9:
        return BasketMode.CRITICAL
    
    if bh.pressure_score > 0.7 or margin_stress or fuel_ratio < 0.3:
        return BasketMode.STRESSED
    
    if fuel_ratio < 0.6 or bh.num_losers > 2:
        return BasketMode.RECOVERY
    
    if bh.pressure_score > 0.4:
        return BasketMode.BUILDING
    
    return BasketMode.HEALTHY