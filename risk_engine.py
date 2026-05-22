import math
from datetime import datetime

# Constants (match EA defaults) yes
DEFAULT_MIN_LOT = 0.01
DEFAULT_MAX_LOT = 0.50
DEFAULT_LOT_STEP = 0.01
BASE_LOT_SIZE = 0.02
MAX_MARGIN_USAGE_PERCENT = 20.0

def compute_minimum_operational_reserve(is_buy, max_expected_move_percent, min_entry_spacing_percent,
                                        grid_levels, min_operational_additions, mor_safety_multiplier,
                                        first_entry_price, tick_value, tick_size, symbol):
    """
    Replicates ComputeMinimumOperationalReserve from EA.
    grid_levels: list of dicts with keys 'price' and 'isHigh'
    """
    min_lot = DEFAULT_MIN_LOT
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    
    # Collect addition prices
    addition_prices = []
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh'] and level['price'] < first_entry_price:
                addition_prices.append(level['price'])
            elif not is_buy and level['isHigh'] and level['price'] > first_entry_price:
                addition_prices.append(level['price'])
    
    # If not enough real levels, estimate using spacing
    if len(addition_prices) < min_operational_additions:
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        last_price = first_entry_price
        for i in range(len(addition_prices), min_operational_additions):
            if is_buy:
                last_price -= grid_spacing
            else:
                last_price += grid_spacing
            addition_prices.append(last_price)
    
    if is_buy:
        addition_prices.sort()
    else:
        addition_prices.sort(reverse=True)
    
    total_loss = 0.0
    # First trade
    distance = abs(first_entry_price - hardstop)
    ticks = distance / tick_size
    loss = ticks * tick_value * min_lot
    total_loss += loss
    
    # Additions
    for price in addition_prices[:min_operational_additions]:
        distance = abs(price - hardstop)
        ticks = distance / tick_size
        loss = ticks * tick_value * min_lot
        total_loss += loss
    
    mor = total_loss * mor_safety_multiplier
    return max(mor, 0.01)


def calculate_dynamic_lot_size(equity, protected_floor, initial_account_equity,
                               direction, first_entry_price, free_margin,
                               max_expected_move_percent, max_grid_levels,
                               max_recovery_additions, min_operational_additions,
                               min_entry_spacing_percent, mor_safety_multiplier,
                               growth_participation_percent, grid_levels,
                               tick_value, tick_size, min_lot, max_lot, lot_step, symbol):
    """
    Replicates CalculateDynamicLotSize from EA.
    Returns lot size (float) that is at least 2 * min_lot (to allow partial closes).
    """
    unlocked_surplus = max(0, equity - protected_floor)
    if unlocked_surplus <= 0.01:
        lot = BASE_LOT_SIZE
        lot = math.floor(lot / lot_step) * lot_step
        lot = max(lot, min_lot)
        lot = min(lot, max_lot)
        # Ensure at least 2 * min_lot (unless impossible)
        min_allowed = min_lot * 2.0
        if lot < min_allowed - 1e-8:
            lot = min_allowed
            # Re‑round to lot step
            lot = math.floor(lot / lot_step) * lot_step
            if lot < min_lot:
                lot = min_lot
            if lot > max_lot:
                lot = max_lot
        return lot
    
    is_buy = (direction == "buy")
    mor = compute_minimum_operational_reserve(is_buy, max_expected_move_percent,
                                              min_entry_spacing_percent, grid_levels,
                                              min_operational_additions, mor_safety_multiplier,
                                              first_entry_price, tick_value, tick_size, symbol)
    
    locked_profit = max(0, protected_floor - initial_account_equity)
    growth_participation = locked_profit * (growth_participation_percent / 100.0)
    total_allowed_loss = mor + unlocked_surplus + growth_participation
    
    # Estimate worst-case distance using grid levels
    worst_distance = 0
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh']:
                dist = level['price'] - hardstop
                if dist < 0: dist = 0
                worst_distance = max(worst_distance, dist)
            elif not is_buy and level['isHigh']:
                dist = hardstop - level['price']
                if dist < 0: dist = 0
                worst_distance = max(worst_distance, dist)
    if worst_distance <= 0:
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        worst_distance = grid_spacing * (max_grid_levels - 1)
        if worst_distance <= 0:
            worst_distance = 1.0
    
    ticks = worst_distance / tick_size
    loss_per_lot = ticks * tick_value
    if loss_per_lot <= 0:
        lot = min_lot
    else:
        reserved_positions = 1.0 + min_operational_additions
        max_lot_possible = total_allowed_loss / (loss_per_lot * reserved_positions)
        lot = min(max_lot_possible, max_lot)
        lot = math.floor(lot / lot_step) * lot_step
        lot = max(lot, min_lot)
    
    # Cap for small accounts
    if lot > 0.50 and equity < 10000:
        lot = 0.50
    
    # Ensure lot is at least 2 * min_lot (to allow partial closes)
    min_allowed = min_lot * 2.0
    if lot < min_allowed - 1e-8:
        lot = min_allowed
        # Re‑round to lot step
        lot = math.floor(lot / lot_step) * lot_step
        if lot < min_lot:
            lot = min_lot
        if lot > max_lot:
            lot = max_lot
    
    return lot


def can_add_position(projected_loss, allowed_loss):
    """Simple check: addition allowed if projected loss <= allowed loss."""
    return projected_loss <= allowed_loss