"""
DEPRECATED - not called by the EA anymore.

Pairing decisions moved fully client-side as of the EA's pairing-engine
redesign: PairingEngine.mqh now does a simple exhaustive search (no pressure
score, no stress tiers, no scoring model) and executes locally, no server
round-trip. This module (and the /pairing-decision route in app.py that
serves it) is kept temporarily, unremoved, while the new local engine is
verified across several live recovery cycles - safe to delete once confirmed.
Basket-close reporting (Telegram, Supabase) is unaffected by this deprecation;
it goes through /copier in trade_endpoints.py, which never depended on this.

Original docstring, for reference:
PAIRING ENGINE MODE:
- Generates deterministic pairing decisions
- NEVER executes broker orders
- EA remains sole execution authority
- Engine only simulates and scores candidates
- Designed to mirror MT5 EA pairing logic
- Now implements pressure-based gating and dynamic thresholds
- MODIFIED: Enforces net_profit >= 0 (no negative exits)
"""

import math
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import asdict
from models import (
    PositionInfo, Candidate, BasketHealth, WinnerAction,
    PairingDecision, PairingConfig, PairingEngineState
)

EPS = 1e-8

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def normalize_volume(volume: float, lot_step: float, min_lot: float) -> float:
    if volume <= 0.0:
        return 0.0
    norm = math.floor(volume / lot_step + EPS) * lot_step
    if norm < min_lot - EPS:
        return 0.0
    return norm

def one_r_value(symbol_info: Dict, min_entry_spacing_percent: float, fixed_lot_size: float) -> float:
    price = symbol_info.get('bid', 0.0)
    grid_spacing = price * (min_entry_spacing_percent / 100.0)
    tick_value = symbol_info.get('tick_value', 1.0)
    tick_size = symbol_info.get('tick_size', symbol_info.get('point', 0.00001))
    if tick_value <= 0 or tick_size <= 0:
        tick_value = 1.0
        tick_size = symbol_info.get('point', 0.00001)
    return (grid_spacing / tick_size) * tick_value * fixed_lot_size

def get_positions(positions: List[PositionInfo]) -> Tuple[List[PositionInfo], List[PositionInfo]]:
    losers = [p for p in positions if p.profit < -EPS]
    winners = [p for p in positions if p.profit > EPS]
    losers.sort(key=lambda x: x.profit)
    winners.sort(key=lambda x: x.profit, reverse=True)
    return losers, winners

def generate_candidates(losers: List[PositionInfo], winners: List[PositionInfo]) -> List[Candidate]:
    candidates = []
    max_l = min(len(losers), 5)
    max_w = min(len(winners), 5)
    if max_l == 0 or max_w == 0:
        return candidates

    # Single winner
    for l in range(max_l):
        loser_loss = -losers[l].profit
        if loser_loss <= EPS:
            continue
        for w in range(max_w):
            total_profit = winners[w].profit
            net_ratio = total_profit / loser_loss
            if net_ratio < 0.75 - EPS:
                continue
            cand = Candidate(
                loser_idx=l,
                winner_idx=[w, -1],
                total_winner_profit=total_profit,
                loser_loss=loser_loss,
                net_ratio=net_ratio,
                efficiency=net_ratio - 1.0,
                volume_released=losers[l].volume + winners[w].volume,
                valid=True
            )
            candidates.append(cand)

    # Two winners
    for l in range(max_l):
        loser_loss = -losers[l].profit
        if loser_loss <= EPS:
            continue
        for w1 in range(max_w - 1):
            for w2 in range(w1 + 1, max_w):
                total_profit = winners[w1].profit + winners[w2].profit
                net_ratio = total_profit / loser_loss
                if net_ratio < 0.75 - EPS:
                    continue
                cand = Candidate(
                    loser_idx=l,
                    winner_idx=[w1, w2],
                    total_winner_profit=total_profit,
                    loser_loss=loser_loss,
                    net_ratio=net_ratio,
                    efficiency=net_ratio - 1.0,
                    volume_released=losers[l].volume + winners[w1].volume + winners[w2].volume,
                    valid=True
                )
                candidates.append(cand)
    return candidates

def compute_clean_score(losers: List[PositionInfo], winners: List[PositionInfo],
                        cand: Candidate, min_residual_volume: float) -> float:
    penalty = 0.0
    remaining_to_cover = cand.loser_loss
    for idx in cand.winner_idx:
        if idx < 0 or remaining_to_cover <= EPS:
            break
        w = winners[idx]
        if w.profit <= EPS or w.volume <= EPS:
            continue
        need = min(w.profit, remaining_to_cover)
        close_vol = (need / w.profit) * w.volume
        remaining_vol = w.volume - close_vol
        if remaining_vol > EPS and remaining_vol < min_residual_volume - EPS:
            penalty += 1.0
        remaining_to_cover -= need
    return 1.0 / (1.0 + penalty)

def compute_future_score(losers: List[PositionInfo], winners: List[PositionInfo],
                         cand: Candidate) -> float:
    remaining_loser_loss = 0.0
    remaining_winner_profit = 0.0
    used_loser = [False] * len(losers)
    used_winner = [False] * len(winners)
    used_loser[cand.loser_idx] = True
    for idx in cand.winner_idx:
        if idx >= 0:
            used_winner[idx] = True
    for i, l in enumerate(losers):
        if not used_loser[i] and l.profit < -EPS:
            remaining_loser_loss += -l.profit
    for i, w in enumerate(winners):
        if not used_winner[i] and w.profit > EPS:
            remaining_winner_profit += w.profit
    if remaining_loser_loss <= EPS:
        return 1.0
    ratio = remaining_winner_profit / remaining_loser_loss
    if ratio > 1.5:
        ratio = 1.5
    return ratio / 1.5

def score_candidates(candidates: List[Candidate], losers: List[PositionInfo],
                     winners: List[PositionInfo], min_residual_volume: float):
    if not candidates:
        return
    max_loss = max(c.loser_loss for c in candidates)
    if max_loss <= EPS:
        max_loss = 1.0
    max_volume = max(c.volume_released for c in candidates)
    if max_volume <= EPS:
        max_volume = 1.0
    for c in candidates:
        volume_score = c.volume_released / max_volume
        risk_score = c.loser_loss / max_loss
        c.risk_score = risk_score
        c.clean_score = compute_clean_score(losers, winners, c, min_residual_volume)
        c.future_score = compute_future_score(losers, winners, c)
        c.score = (0.30 * c.net_ratio +
                   0.20 * volume_score +
                   0.10 * c.efficiency +
                   0.15 * c.clean_score +
                   0.10 * c.future_score +
                   0.15 * risk_score)

def compute_required_winner_profit(loser_loss: float, one_r: float,
                                   spread_cost: float, target_r: float) -> float:
    target_net = max(one_r * target_r, spread_cost)
    return loser_loss + target_net

def compute_required_price_for_candidate(losers: List[PositionInfo], winners: List[PositionInfo],
                                         cand: Candidate, direction_is_buy: bool,
                                         symbol_info: Dict, target_ratio: float) -> float:
    if not cand.valid:
        return 0.0
    required_winners_profit = cand.loser_loss * target_ratio
    current_winners_profit = cand.total_winner_profit
    if current_winners_profit >= required_winners_profit - EPS:
        if direction_is_buy:
            return symbol_info.get('ask', 0.0)
        else:
            return symbol_info.get('bid', 0.0)
    additional_needed = required_winners_profit - current_winners_profit
    first_idx = cand.winner_idx[0]
    if first_idx < 0:
        return 0.0
    w = winners[first_idx]
    if w.volume <= EPS:
        return 0.0
    tick_value = symbol_info.get('tick_value', 1.0)
    tick_size = symbol_info.get('tick_size', symbol_info.get('point', 0.00001))
    price_move = (additional_needed * tick_size) / (tick_value * w.volume)
    if price_move <= EPS:
        return 0.0
    point = symbol_info.get('point', 0.00001)
    cap = 10000.0 * point
    if price_move > cap:
        price_move = cap
    if w.is_buy:
        required_price = w.entry + price_move
    else:
        required_price = w.entry - price_move
    if required_price <= point * 10.0:
        return 0.0
    return required_price

def analyze_basket(positions: List[PositionInfo], direction_locked: bool,
                   current_direction_is_buy: bool, current_price: float,
                   margin: float, free_margin: float,
                   equity: float = 0.0, protected_floor: float = 0.0,
                   initial_equity: float = 0.0) -> BasketHealth:
    bh = BasketHealth()
    buy_lots = 0.0
    sell_lots = 0.0
    sum_dist = 0.0
    max_lot = 0.0
    for p in positions:
        bh.total_lots += p.volume
        if p.is_buy:
            buy_lots += p.volume
        else:
            sell_lots += p.volume
        if p.profit < -EPS:
            bh.num_losers += 1
        bh.floating_dd += p.profit
        dist = (p.entry - current_price) if p.is_buy else (current_price - p.entry)
        if dist < 0:
            dist = -dist
        sum_dist += dist * p.volume
        if p.volume > max_lot:
            max_lot = p.volume
    bh.floating_dd = -bh.floating_dd
    if bh.floating_dd < 0:
        bh.floating_dd = 0.0

    bh.directional_imbalance = abs(buy_lots - sell_lots)
    if bh.total_lots > EPS:
        bh.weighted_avg_dist_to_be = sum_dist / bh.total_lots
        bh.exposure_concentration = max_lot / bh.total_lots
    total_margin = margin + free_margin
    if total_margin > EPS:
        bh.margin_usage = margin / total_margin
    if direction_locked:
        main_lots = buy_lots if current_direction_is_buy else sell_lots
        opp_lots = sell_lots if current_direction_is_buy else buy_lots
        bh.hedge_ratio = opp_lots / main_lots if main_lots > EPS else 0.0

    # Pressure score (0..1, higher = more stressed)
    max_allowed_dd_usd = max(initial_equity * 0.10, 500.0) if initial_equity > 0 else 1000.0
    dd_pressure = min(bh.floating_dd / max_allowed_dd_usd, 1.0) if max_allowed_dd_usd > 0 else 0.0
    margin_pressure = min(bh.margin_usage, 1.0)
    max_imbalance_lots = max(bh.total_lots, 0.5)
    imbalance_pressure = min(bh.directional_imbalance / max_imbalance_lots, 1.0)
    exposure_pressure = bh.exposure_concentration
    bh.pressure_score = (
        0.35 * dd_pressure +
        0.25 * margin_pressure +
        0.20 * imbalance_pressure +
        0.20 * exposure_pressure
    )
    bh.pressure_score = max(0.0, min(1.0, bh.pressure_score))
    return bh

def would_remaining_basket_improve(before: BasketHealth, after: BasketHealth,
                                   cand: Candidate, losers: List[PositionInfo],
                                   winners: List[PositionInfo]) -> bool:
    if after.total_lots >= before.total_lots * 0.95 - EPS:
        return False
    if after.directional_imbalance > before.directional_imbalance + EPS:
        return False
    if after.hedge_ratio < before.hedge_ratio * 0.8 - EPS:
        return False
    return True

def future_safety_check(losers: List[PositionInfo], winners: List[PositionInfo],
                        cand: Candidate, winner_actions: List[WinnerAction],
                        config: PairingConfig, atr_h4: float,
                        current_price: float, direction_is_buy: bool) -> bool:
    min_residual = config.min_residual_volume
    for i, l in enumerate(losers):
        if i == cand.loser_idx:
            continue
        if l.volume > EPS and l.volume < min_residual - EPS:
            return False
    for i, w in enumerate(winners):
        if i in cand.winner_idx:
            continue
        if w.volume > EPS and w.volume < min_residual - EPS:
            return False

    for action in winner_actions:
        pos = next((w for w in winners if w.ticket == action.ticket), None)
        if pos:
            remaining = pos.volume - action.close_volume
            if remaining > EPS and remaining < min_residual - EPS:
                return False

    total_lots = 0.0
    sum_price = 0.0
    for i, l in enumerate(losers):
        if i == cand.loser_idx:
            continue
        total_lots += l.volume
        sum_price += l.entry * l.volume
    for i, w in enumerate(winners):
        if i in cand.winner_idx:
            continue
        total_lots += w.volume
        sum_price += w.entry * w.volume
    if total_lots > EPS:
        new_be = sum_price / total_lots
        distance = abs(current_price - new_be)
        if atr_h4 <= EPS:
            atr_h4 = 100.0 * 0.00001
        if distance > config.max_future_atr_distance * atr_h4 + EPS:
            return False

    remaining_winners = 0
    for i, w in enumerate(winners):
        if i not in cand.winner_idx and w.profit > EPS:
            remaining_winners += 1
    remaining_losers = 0
    for i, l in enumerate(losers):
        if i != cand.loser_idx and l.profit < -EPS:
            remaining_losers += 1
    if remaining_winners < config.min_remaining_winners and remaining_losers > 0:
        return False
    return True

def simulate_candidate_execution(losers: List[PositionInfo], winners: List[PositionInfo],
                                 cand: Candidate, config: PairingConfig,
                                 symbol_info: Dict, one_r: float,
                                 fixed_lot_size: float,
                                 active_flip_ticket: Optional[int] = None) -> Tuple[float, float, float, List[WinnerAction], bool]:
    loser = losers[cand.loser_idx]
    loser_loss = -loser.profit
    spread = symbol_info.get('spread', 0) * symbol_info.get('point', 0.00001)
    tick_value = symbol_info.get('tick_value', 1.0)
    spread_cost = spread * tick_value * fixed_lot_size * 2.0
    required_profit = compute_required_winner_profit(loser_loss, one_r, spread_cost, config.target_net_profit_r)

    realised = 0.0
    winner_actions = []
    for idx in cand.winner_idx:
        if idx < 0 or realised >= required_profit - EPS:
            break
        w = winners[idx]
        need = min(w.profit, required_profit - realised)
        close_vol = (need / w.profit) * w.volume
        lot_step = symbol_info.get('lot_step', 0.01)
        min_lot = symbol_info.get('min_lot', 0.01)
        close_vol = normalize_volume(close_vol, lot_step, min_lot)
        if close_vol <= EPS:
            continue
        remaining = w.volume - close_vol
        close_type = "full" if remaining <= EPS else "partial"

        if w.ticket == active_flip_ticket:
            if close_type == "partial" and 0 < remaining < config.min_residual_volume - EPS:
                close_type = "full"
                close_vol = w.volume
                need = w.profit

        winner_actions.append(WinnerAction(ticket=w.ticket, close_type=close_type,
                                           close_volume=close_vol, expected_profit=need))
        realised += need

    if realised <= EPS:
        return 0.0, 0.0, 0.0, [], False

    net_profit = realised - loser_loss
    return realised, required_profit, net_profit, winner_actions, True

# ======================================================================
# MAIN DECISION FUNCTION – returns None if no pairing should happen
# ======================================================================
def get_best_pairing_decision(
    positions: List[PositionInfo],
    direction_locked: bool,
    current_direction_is_buy: bool,
    symbol_info: Dict,
    config: PairingConfig,
    state: PairingEngineState,
    current_time: int,
    atr_h4: float,
    active_flip_ticket: Optional[int] = None,
    equity: float = 0.0,
    protected_floor: float = 0.0,
    initial_equity: float = 0.0
) -> Optional[PairingDecision]:

    # ---------------------------------------------------------
    # PRECHECKS
    # ---------------------------------------------------------
    if not config.enable_pairing_engine:
        return None
    if not direction_locked:
        return None
    if current_time - state.last_pairing_time < config.pairing_cooldown_seconds:
        return None
    if state.pairing_in_progress:
        return None

    losers, winners = get_positions(positions)
    if not losers or not winners:
        return None

    # ---------------------------------------------------------
    # BASKET ANALYSIS
    # ---------------------------------------------------------
    current_price = symbol_info.get('ask', 0.0) if current_direction_is_buy else symbol_info.get('bid', 0.0)
    margin = symbol_info.get('margin', 0.0)
    free_margin = symbol_info.get('free_margin', 10000.0)

    before = analyze_basket(
        positions,
        direction_locked,
        current_direction_is_buy,
        current_price,
        margin,
        free_margin,
        equity,
        protected_floor,
        initial_equity
    )

    # ---------------------------------------------------------
    # PRESSURE GATING (healthy basket → no pairing)
    # ---------------------------------------------------------
    if before.pressure_score < 0.35:
        return None

    # ---------------------------------------------------------
    # DYNAMIC THRESHOLD based on stress level
    # ---------------------------------------------------------
    if before.pressure_score < 0.4:
        dynamic_threshold = 2.0
    elif before.pressure_score < 0.7:
        dynamic_threshold = 1.5
    else:
        dynamic_threshold = 1.1

    # ---------------------------------------------------------
    # GENERATE AND SELECT CANDIDATE
    # ---------------------------------------------------------
    candidates = generate_candidates(losers, winners)
    if not candidates:
        return None

    # Immediate candidate if net_ratio meets dynamic_threshold
    immediate = None
    best_ratio = -1.0
    for c in candidates:
        if c.net_ratio >= dynamic_threshold - EPS and c.net_ratio > best_ratio:
            best_ratio = c.net_ratio
            immediate = c

    if immediate:
        best = immediate
    else:
        score_candidates(candidates, losers, winners, config.min_residual_volume)
        best = max(candidates, key=lambda x: (x.score, x.net_ratio, x.volume_released))

    if best.net_ratio < dynamic_threshold - EPS:
        return None

    # ---------------------------------------------------------
    # SIMULATE EXECUTION
    # ---------------------------------------------------------
    one_r = one_r_value(
        symbol_info,
        symbol_info.get('min_entry_spacing_percent', 0.1),
        symbol_info.get('fixed_lot_size', 0.02)
    )

    realised, required_profit, net_profit, winner_actions, exec_ok = simulate_candidate_execution(
        losers, winners, best, config, symbol_info, one_r,
        symbol_info.get('fixed_lot_size', 0.02), active_flip_ticket
    )

    if not exec_ok or not winner_actions:
        return None

    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    # CRITICAL FIX: Ensure net profit is never negative
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    if net_profit < -EPS:
        return None

    # ---------------------------------------------------------
    # SIMULATE REMAINING BASKET AFTER PARTIAL CLOSES
    # ---------------------------------------------------------
    loser_ticket = losers[best.loser_idx].ticket
    remaining = []
    for p in positions:
        if p.ticket == loser_ticket:
            continue
        action = next((a for a in winner_actions if a.ticket == p.ticket), None)
        if action:
            if action.close_type == "full":
                continue
            else:
                new_vol = p.volume - action.close_volume
                if new_vol > EPS:
                    remaining.append(PositionInfo(
                        ticket=p.ticket,
                        profit=p.profit * (new_vol / p.volume),
                        volume=new_vol, entry=p.entry, is_buy=p.is_buy
                    ))
        else:
            remaining.append(p)

    after = analyze_basket(
        remaining,
        direction_locked,
        current_direction_is_buy,
        current_price,
        margin,
        free_margin,
        equity,
        protected_floor,
        initial_equity
    )

    # ---------------------------------------------------------
    # IMPROVEMENT CHECK
    # ---------------------------------------------------------
    if not would_remaining_basket_improve(before, after, best, losers, winners):
        return None

    # ---------------------------------------------------------
    # FUTURE SAFETY CHECK
    # ---------------------------------------------------------
    if not future_safety_check(losers, winners, best, winner_actions,
                               config, atr_h4, current_price, current_direction_is_buy):
        return None

    # ---------------------------------------------------------
    # REQUIRED PRICE FOR LIMIT ORDER (if needed)
    # ---------------------------------------------------------
    required_price = compute_required_price_for_candidate(losers, winners, best,
                                                          current_direction_is_buy,
                                                          symbol_info,
                                                          dynamic_threshold)

    # ---------------------------------------------------------
    # SUCCESS – RETURN EXECUTABLE DECISION
    # ---------------------------------------------------------
    return PairingDecision(
        execute_now=True,
        reason="ok",
        loser_ticket=loser_ticket,
        winner_actions=winner_actions,
        required_winner_profit=required_profit,
        expected_net_profit=net_profit,
        required_price=required_price,
        score=best.score,
        net_ratio=best.net_ratio,
        future_safe=True,
        basket_health_before=asdict(before),
        basket_health_after=asdict(after)
    )