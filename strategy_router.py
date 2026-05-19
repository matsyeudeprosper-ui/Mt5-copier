# strategy_router.py
from typing import Optional, Dict, List
from models import PairingDecision, PairingConfig, PairingEngineState, PositionInfo
from basket_analyzer import classify_basket_mode, BasketMode
from anygain_engine import get_anygain_decision
from pairing_engine import get_best_pairing_decision
from recovery_exit_engine import get_recovery_exit_decision

def is_recovery_mode(positions: List[PositionInfo]) -> bool:
    """
    Simplified gate: any basket with at least 2 positions is in recovery mode.
    Single trades (even losing) are NOT harvested.
    """
    return len(positions) >= 2

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
    user_profile: Dict,
    basket_start_equity: float = 0.0
) -> Optional[PairingDecision]:
    
    if not positions:
        return None
    
    margin = symbol_info.get('margin', 0.0)
    free_margin = symbol_info.get('free_margin', 10000.0)
    total_margin = margin + free_margin
    margin_usage = margin / total_margin if total_margin > 0 else 0.0
    
    # Simplified recovery gate: only position count matters
    if not is_recovery_mode(positions):
        return None
    
    # Compute current price and basket health (needed for pressure score)
    current_price = symbol_info.get('ask', 0.0) if current_direction_is_buy else symbol_info.get('bid', 0.0)
    
    # Import analyze_basket from pairing_engine to compute pressure_score
    from pairing_engine import analyze_basket
    bh = analyze_basket(
        positions, direction_locked, current_direction_is_buy,
        current_price, margin, free_margin,
        equity, protected_floor, initial_equity
    )
    pressure_score = bh.pressure_score
    
    # ========== 1. ARBE – Adaptive Recovery Basket Exit (HIGHEST PRIORITY) ==========
    arbe_decision = get_recovery_exit_decision(
        positions=positions,
        direction_locked=direction_locked,
        current_direction_is_buy=current_direction_is_buy,
        symbol_info=symbol_info,
        current_price=current_price,
        margin=margin,
        free_margin=free_margin,
        equity=equity,
        protected_floor=protected_floor,
        initial_equity=initial_equity,
        basket_start_equity=basket_start_equity,
        pressure_score=pressure_score
    )
    if arbe_decision is not None:
        return arbe_decision
    
    # ========== 2. Basket mode classification (for AnyGain / Pairing) ==========
    mode = classify_basket_mode(
        positions, direction_locked, current_direction_is_buy,
        current_price, margin, free_margin,
        equity, protected_floor, initial_equity,
        atr_h4, mor
    )
    
    anygain_aggressiveness = user_profile.get('anygain_aggressiveness', 1.0)
    account = user_profile.get('account', 0)
    
    # ========== 3. AnyGain (micro harvesting) ==========
    if mode == BasketMode.CRITICAL:
        # CRITICAL: only Pairing, no AnyGain
        decision = get_best_pairing_decision(
            positions, direction_locked, current_direction_is_buy,
            symbol_info, config, state, current_time, atr_h4,
            active_flip_ticket, equity, protected_floor, initial_equity
        )
        if decision:
            decision.reason = f"critical_mode_{decision.reason}"
        return decision
    
    elif mode == BasketMode.STRESSED:
        # STRESSED: try Pairing first, then AnyGain
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
        # RECOVERY: try Pairing first, then AnyGain with reduced aggressiveness
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
    
    # BUILDING or HEALTHY should not reach here due to recovery gate, but fallback
    return None