# entry_engine.py
import math
from typing import List
from .entry_models import (
    PositionInfo, EntryDecisionRequest, EntryDecisionResponse
)

EPS = 1e-8
LEVEL_MATCH_TOLERANCE_POINTS = 10


def is_level_free(level_price: float, is_buy_side: bool, positions: List[PositionInfo], point: float) -> bool:
    tolerance = LEVEL_MATCH_TOLERANCE_POINTS * point
    for pos in positions:
        if pos.isBuy != is_buy_side:
            continue
        if abs(pos.price - level_price) <= tolerance:
            return False
    return True


def validate_initial_entry(req: EntryDecisionRequest) -> EntryDecisionResponse:
    if req.direction_locked:
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="direction already locked"
        )

    is_buy_setup = not req.trigger_level_is_high

    if not is_level_free(req.trigger_level_price, is_buy_setup, req.positions, req.point):
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="level already occupied"
        )

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

    if direction:
        if req.trigger_level_is_high:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="buy addition requires support level (isHigh=false)"
            )
        if req.trigger_level_price >= req.lowest_buy_entry - EPS:
            return EntryDecisionResponse(
                success=True,
                decision="none",
                reason="level not below lowest buy entry"
            )
    else:
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

    if not is_level_free(req.trigger_level_price, direction, req.positions, req.point):
        return EntryDecisionResponse(
            success=True,
            decision="none",
            reason="level already occupied"
        )

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
    # Defensive checks
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
    if not (math.isfinite(request.bid) and math.isfinite(request.ask) and
            math.isfinite(request.point) and math.isfinite(request.trigger_level_price) and
            math.isfinite(request.required_spacing)):
        return EntryDecisionResponse(success=False, decision="none", reason="non-finite value")

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