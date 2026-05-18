# strategy_router.py
from typing import Optional, Dict, List
from models import PairingDecision, PairingConfig, PairingEngineState, PositionInfo
from basket_analyzer import classify_basket_mode, BasketMode
from anygain_engine import get_anygain_decision
from pairing_engine import get_best_pairing_decision

def is_recovery_mode(
    positions: List[PositionInfo],
    equity: float,
    protected_floor: float,
    initial_equity: float,
    margin_usage: float
) -> bool:
    """
    Strict gate: return True only if basket is stressed AND has at least 2 positions.
    Single trades (even losing) are NEVER harvested.
    """
    # Require at least 2 positions (basket behavior)
    if len(positions) < 2:
        return False
    
    # Basket underwater (total floating loss)
    floating_pnl = sum(p.profit for p in positions)
    if floating_pnl < 0:
        return True
    
    # High margin usage (>70%)
    if margin_usage > 0.70:
        return True
    
    # Equity drawdown close to protected floor (0.5% below)
    if equity < protected_floor + (initial_equity * 0.005):
        return True
    
    return False

def route_strategy(
    positions: List[PositionInfo],
    direction_locked: bool,
    current_direction_is_buy: bool,
    symbol_info: Dict,
    config: PairingConfig,
    state: PairingEngineState,
    current_time: int,
    atr_h4: float,
    active_flip_ticket: Optional[int],
    equity: float,
    protected_floor: float,
    initial_equity: float,
    mor: float,
    user_profile: Dict
) -> Optional[PairingDecision]:
    
    if not positions:
        return None
    
    margin = symbol_info.get('margin', 0.0)
    free_margin = symbol_info.get('free_margin', 10000.0)
    total_margin = margin + free_margin
    margin_usage = margin / total_margin if total_margin > 0 else 0.0
    
    # Strict recovery gate
    if not is_recovery_mode(positions, equity, protected_floor, initial_equity, margin_usage):
        return None
    
    # Basket classification (only if recovery mode is active)
    current_price = symbol_info.get('ask', 0.0) if current_direction_is_buy else symbol_info.get('bid', 0.0)
    mode = classify_basket_mode(
        positions, direction_locked, current_direction_is_buy,
        current_price, margin, free_margin,
        equity, protected_floor, initial_equity,
        atr_h4, mor
    )
    
    anygain_aggressiveness = user_profile.get('anygain_aggressiveness', 1.0)
    account = user_profile.get('account', 0)   # Extract account for rate limiting
    
    if mode == BasketMode.CRITICAL:
        decision = get_best_pairing_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, state, current_time, atr_h4,
            active_flip_ticket, equity, protected_floor, initial_equity
        )
        if decision:
            decision.reason = f"critical_mode_{decision.reason}"
        return decision
    
    elif mode == BasketMode.STRESSED:
        decision = get_best_pairing_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, state, current_time, atr_h4,
            active_flip_ticket, equity, protected_floor, initial_equity
        )
        if decision:
            decision.reason = f"stressed_mode_{decision.reason}"
            return decision
        return get_anygain_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, current_time, atr_h4,
            equity, protected_floor, initial_equity, mor,
            aggressiveness=anygain_aggressiveness,
            account=account
        )
    
    elif mode == BasketMode.RECOVERY:
        decision = get_best_pairing_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, state, current_time, atr_h4,
            active_flip_ticket, equity, protected_floor, initial_equity
        )
        if decision:
            decision.reason = f"recovery_mode_{decision.reason}"
            return decision
        return get_anygain_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, current_time, atr_h4,
            equity, protected_floor, initial_equity, mor,
            aggressiveness=anygain_aggressiveness * 0.5,
            account=account
        )
    
    # Should not reach here due to recovery gate, but fallback
    return None