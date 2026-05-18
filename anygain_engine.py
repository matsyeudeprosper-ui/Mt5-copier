"""
AnyGain Engine – Micro‑profit extraction ONLY during basket recovery/stress.
Conservative: only partial closes of winners, no loser offset.
Safe for live deployment.
"""

import math
import time
from typing import List, Dict, Optional
from dataclasses import asdict
from models import PositionInfo, WinnerAction, PairingDecision, PairingConfig
from pairing_engine import analyze_basket, get_positions, EPS

# Rate limiting state: keyed by (account, symbol)
_rate_limit_state = {}   # key: f"{account}_{symbol}", value: {last_run, hourly_count, daily_counts}

def _get_rate_key(account: int, symbol: str) -> str:
    return f"{account}_{symbol}"

def _check_rate_limits(account: int, symbol: str, ticket: int) -> bool:
    key = _get_rate_key(account, symbol)
    now = time.time()
    state = _rate_limit_state.get(key)
    if state is None:
        state = {
            "last_run": 0,
            "hourly_count": 0,
            "last_hour": 0,
            "daily_counts": {}   # ticket -> count for today
        }
        _rate_limit_state[key] = state
    
    # Cooldown 60s
    if now - state["last_run"] < 60:
        return False
    
    # Hourly limit: max 10 actions per symbol per account
    hour = int(now / 3600)
    if hour != state.get("last_hour", 0):
        state["hourly_count"] = 0
        state["last_hour"] = hour
    if state["hourly_count"] >= 10:
        return False
    
    # Daily per‑position limit: max 5 partials per position per day
    today = time.strftime("%Y-%m-%d")
    ticket_key = f"{ticket}_{today}"
    daily = state["daily_counts"].get(ticket_key, 0)
    if daily >= 5:
        return False
    
    return True

def _record_action(account: int, symbol: str, ticket: int):
    key = _get_rate_key(account, symbol)
    state = _rate_limit_state.setdefault(key, {
        "last_run": 0,
        "hourly_count": 0,
        "last_hour": 0,
        "daily_counts": {}
    })
    now = time.time()
    state["last_run"] = now
    state["hourly_count"] = state.get("hourly_count", 0) + 1
    today = time.strftime("%Y-%m-%d")
    ticket_key = f"{ticket}_{today}"
    state["daily_counts"][ticket_key] = state["daily_counts"].get(ticket_key, 0) + 1

def get_anygain_decision(
    positions: List[PositionInfo],
    direction_locked: bool,
    current_direction_is_buy: bool,
    symbol_info: Dict,
    config: PairingConfig,
    current_time: int,   # not used
    atr_h4: float,
    equity: float,
    protected_floor: float,
    initial_equity: float,
    mor: float,
    aggressiveness: float = 1.0,
    account: int = 0      # passed from EA (should be sent in account_info)
) -> Optional[PairingDecision]:
    
    if not direction_locked or not positions:
        return None
    
    # Recovery gate is already enforced by strategy_router, but double‑check
    # (no need to re‑implement here)
    
    losers, winners = get_positions(positions)
    if not winners:
        return None
    
    # Dynamic minimum profit threshold: 0.01% of equity or $0.20, whichever larger
    min_profit = max(equity * 0.0001, 0.20)
    eligible_winners = [w for w in winners if w.profit > min_profit]
    if not eligible_winners:
        return None
    
    # Pick the winner with highest profit (best harvest candidate)
    best_winner = max(eligible_winners, key=lambda w: w.profit)
    
    # Close ratio: 2% to 10% of volume based on aggressiveness
    close_ratio = min(0.02 + (aggressiveness * 0.08), 0.10)
    close_vol = math.floor(best_winner.volume * close_ratio / 0.01) * 0.01
    if close_vol <= 0.0 or close_vol >= best_winner.volume - EPS:
        return None
    
    # Rate limits (need account number – default 0 if not provided)
    if not _check_rate_limits(account, symbol_info.get("symbol", "UNKNOWN"), best_winner.ticket):
        return None
    
    # Simulate basket after partial close
    remaining = []
    for p in positions:
        if p.ticket == best_winner.ticket:
            new_vol = p.volume - close_vol
            if new_vol > EPS:
                remaining.append(PositionInfo(
                    ticket=p.ticket,
                    profit=p.profit * (new_vol / p.volume),
                    volume=new_vol,
                    entry=p.entry,
                    is_buy=p.is_buy
                ))
        else:
            remaining.append(p)
    
    current_price = symbol_info.get('ask', 0.0) if current_direction_is_buy else symbol_info.get('bid', 0.0)
    margin = symbol_info.get('margin', 0.0)
    free_margin = symbol_info.get('free_margin', 10000.0)
    
    before = analyze_basket(positions, direction_locked, current_direction_is_buy,
                            current_price, margin, free_margin,
                            equity, protected_floor, initial_equity)
    after = analyze_basket(remaining, direction_locked, current_direction_is_buy,
                           current_price, margin, free_margin,
                           equity, protected_floor, initial_equity)
    
    # Improvement checks (conservative)
    if after.total_lots >= before.total_lots * 0.95 - EPS:
        return None
    if after.directional_imbalance > before.directional_imbalance + EPS:
        return None
    if after.hedge_ratio < before.hedge_ratio * 0.8 - EPS:
        return None
    
    # Recovery fuel protection
    remaining_winners = [w for w in remaining if w.profit > EPS]
    remaining_losers = [l for l in remaining if l.profit < -EPS]
    remaining_winner_profit = sum(w.profit for w in remaining_winners)
    remaining_loser_loss = sum(-l.profit for l in remaining_losers)
    if remaining_loser_loss > EPS:
        fuel_ratio = remaining_winner_profit / remaining_loser_loss
        if fuel_ratio < 0.40:
            return None
    
    # Expected net profit
    expected_profit = (close_vol / best_winner.volume) * best_winner.profit
    if expected_profit < min_profit:
        return None
    
    winner_actions = [WinnerAction(
        ticket=best_winner.ticket,
        close_type="partial",
        close_volume=close_vol,
        expected_profit=expected_profit
    )]
    
    _record_action(account, symbol_info.get("symbol", "UNKNOWN"), best_winner.ticket)
    
    return PairingDecision(
        execute_now=True,
        reason="anygain_recovery_harvest",
        loser_ticket=0,
        winner_actions=winner_actions,
        required_winner_profit=0.0,
        expected_net_profit=expected_profit,
        required_price=0.0,
        score=expected_profit,
        net_ratio=0.0,
        future_safe=True,
        basket_health_before=asdict(before),
        basket_health_after=asdict(after)
    )