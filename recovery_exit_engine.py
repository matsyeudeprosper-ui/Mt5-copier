"""
Recovery Exit Engine (ARBE) – Adaptive Recovery Basket Exit.
Closes the entire basket when equity recovers above starting equity
by an adaptive percentage based on pressure score.
Now works for ANY basket with >=2 positions, regardless of stress history.
ADDED: strict check that expected net profit is positive.
"""

from typing import List, Optional, Dict
from dataclasses import asdict
from models import PositionInfo, WinnerAction, PairingDecision, BasketHealth
from pairing_engine import EPS

def get_recovery_exit_decision(
    positions: List[PositionInfo],
    direction_locked: bool,
    current_direction_is_buy: bool,
    symbol_info: Dict,
    current_price: float,
    margin: float,
    free_margin: float,
    equity: float,
    protected_floor: float,
    initial_equity: float,
    basket_start_equity: float,
    pressure_score: float
) -> Optional[PairingDecision]:
    """
    Returns a PairingDecision to close all positions if the basket
    has recovered to a profit above starting equity,
    with adaptive threshold based on pressure_score.
    No pressure gate – works for any basket with >=2 positions.
    """
    if not direction_locked:
        return None
    if len(positions) < 2:
        return None
    if basket_start_equity <= 0:
        return None

    # Adaptive target based on pressure score
    if pressure_score < 0.5:
        target_percent = 0.20
    elif pressure_score < 0.7:
        target_percent = 0.12
    elif pressure_score < 0.85:
        target_percent = 0.08
    else:
        target_percent = 0.05

    target_equity = basket_start_equity * (1.0 + target_percent / 100.0)

    # Minimum absolute profit floor to avoid spread eating everything
    min_absolute_profit = max(0.50, initial_equity * 0.0005)   # $0.50 or 0.05% of initial equity
    net_profit = equity - basket_start_equity

    # Reject if net profit is not positive
    if net_profit <= 0:
        return None

    if net_profit < min_absolute_profit and equity < target_equity:
        return None

    if equity < target_equity:
        return None

    # Build winner_actions: close all positions fully
    winner_actions = []
    for p in positions:
        winner_actions.append(WinnerAction(
            ticket=p.ticket,
            close_type="full",
            close_volume=p.volume,
            expected_profit=p.profit
        ))

    # Create dummy basket health (for logging, not used by EA for execution)
    before = BasketHealth()
    after = BasketHealth()

    return PairingDecision(
        execute_now=True,
        reason=f"recovery_exit_pressure_{pressure_score:.2f}_target_{target_percent:.2f}%",
        loser_ticket=0,
        winner_actions=winner_actions,
        required_winner_profit=0.0,
        expected_net_profit=net_profit,
        required_price=0.0,
        score=0.0,
        net_ratio=0.0,
        future_safe=True,
        basket_health_before=asdict(before),
        basket_health_after=asdict(after)
    )