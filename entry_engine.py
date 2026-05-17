# entry_engine.py
"""
Entry & addition validation engine.
EA determines the exact trigger level; server only validates permission.
"""

import math
from typing import List
from .entry_models import (
    PositionInfo, EntryDecisionRequest, EntryDecisionResponse
)

EPS = 1e-8
LEVEL_MATCH_TOLERANCE_POINTS = 10


def is_level_free(level_price: float, is_buy_side: bool, positions: List[PositionInfo], point: float) -> bool:
    """Check if no position on the same side exists within tolerance of the given level."""
    tolerance = LEVEL_MATCH_TOLERANCE_POINTS * point
    for pos in positions:
        if pos.isBuy != is_buy_side:
            continue          # ignore opposite side positions (hedges)
        if abs(pos.price - level_price) <= tolerance:
            return False
    return True


def validate_initial_entry(req: EntryDecisionRequest) -> EntryDecisionResponse:
    # Basic business checks
    if req.direction_locked:
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="direction already locked"
        )

    # Determine side from trigger level
    is_buy_setup = not req.trigger_level_is_high   # support -> buy

    # Check if level is free (no existing position at same price on same side)
    if not is_level_free(req.trigger_level_price, is_buy_setup, req.positions, req.point):
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="level already occupied"
        )

    # Valid: allow execution
    if is_buy_setup:
        return EntryDecisionResponse(
            success=True,
            decision="execute_buy",
            reason="valid support level",
            level_price=req.trigger_level_price,
            level_index=req.trigger_level_index
        )
    else:
        return EntryDecisionResponse(
            success=True,
            decision="execute_sell",
            reason="valid resistance level",
            level_price=req.trigger_level_price,
            level_index=req.trigger_level_index
        )


def validate_grid_addition(req: EntryDecisionRequest) -> EntryDecisionResponse:
    if not req.direction_locked:
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="direction not locked"
        )

    direction = req.current_direction_is_buy

    # Validate that the trigger level type matches the basket direction
    if direction:
        # Buy basket: only support levels (isHigh == False) are allowed
        if req.trigger_level_is_high:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="buy addition requires support level (isHigh=false)"
            )
        # Check that level is below the lowest buy entry
        if req.trigger_level_price >= req.lowest_buy_entry - EPS:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="level not below lowest buy entry"
            )
    else:
        # Sell basket: only resistance levels (isHigh == True) are allowed
        if not req.trigger_level_is_high:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="sell addition requires resistance level (isHigh=true)"
            )
        if req.trigger_level_price <= req.highest_sell_entry + EPS:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="level not above highest sell entry"
            )

    # Check if level is free (no same‑side position at that price)
    if not is_level_free(req.trigger_level_price, direction, req.positions, req.point):
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="level already occupied"
        )

    # Spacing check
    if direction:
        last_price = req.last_buy_addition_price if req.last_buy_addition_price > 0 else req.lowest_buy_entry
        if abs(req.ask - last_price) < req.required_spacing - EPS:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="spacing too small"
            )
    else:
        last_price = req.last_sell_addition_price if req.last_sell_addition_price > 0 else req.highest_sell_entry
        if abs(req.bid - last_price) < req.required_spacing - EPS:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="spacing too small"
            )

    # All checks passed
    if direction:
        return EntryDecisionResponse(
            success=True,
            decision="execute_buy",
            reason="addition valid",
            level_price=req.trigger_level_price,
            level_index=req.trigger_level_index
        )
    else:
        return EntryDecisionResponse(
            success=True,
            decision="execute_sell",
            reason="addition valid",
            level_price=req.trigger_level_price,
            level_index=req.trigger_level_index
        )


def get_entry_decision(request: EntryDecisionRequest) -> EntryDecisionResponse:
    # Defensive input validation
    if request.point <= 0:
        return EntryDecisionResponse(success=False, decision="none", reason="invalid point")
    if request.required_spacing < 0:
        return EntryDecisionResponse(success=False, decision="none", reason="negative spacing")
    if request.bid <= 0 or request.ask <= 0:
        return EntryDecisionResponse(success=False, decision="none", reason="invalid bid/ask")
    if request.ask < request.bid:
        return EntryDecisionResponse(success=False, decision="none", reason="ask below bid")
    if request.trigger_level_index < 0:
        return EntryDecisionResponse(success=False, decision="none", reason="invalid trigger index")
    # Check for NaN / infinite values (Pydantic already prevents non‑numeric, but double‑check)
    if not (math.isfinite(request.bid) and math.isfinite(request.ask) and math.isfinite(request.point) and
            math.isfinite(request.trigger_level_price) and math.isfinite(request.required_spacing)):
        return EntryDecisionResponse(success=False, decision="none", reason="non‑finite value detected")

    if request.mode == "initial_entry":
        return validate_initial_entry(request)
    elif request.mode == "grid_addition":
        return validate_grid_addition(request)
    else:
        return EntryDecisionResponse(
            success=False,
            decision="none",
            reason=f"unknown mode: {request.mode}"
        )