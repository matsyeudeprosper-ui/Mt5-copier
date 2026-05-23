import math
from datetime import datetime

# Constants (match EA defaults)
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
    Now uses correct distance to hardstop.
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
    # First trade: distance to hardstop
    distance = abs(first_entry_price - hardstop)
    ticks = distance / tick_size
    loss = ticks * tick_value * min_lot
    total_loss += loss
    
    # Additions: distance from each addition price to hardstop
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
    Replicates CalculateDynamicLotSize from EA with CORRECT worst_distance.
    """
    unlocked_surplus = max(0, equity - protected_floor)
    if unlocked_surplus <= 0.01:
        lot = BASE_LOT_SIZE
        lot = math.floor(lot / lot_step) * lot_step
        lot = max(lot, min_lot)
        lot = min(lot, max_lot)
        min_allowed = min_lot * 2.0
        if lot < min_allowed - 1e-8:
            lot = min_allowed
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
    
    # Determine the farthest grid level from entry (the last planned addition)
    # For buys, the farthest level is the lowest price (most negative)
    # For sells, the farthest level is the highest price
    # We need the distance from that farthest level to the hardstop, not just the grid spacing.
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    
    # Collect all planned addition prices (including estimated if grid_levels missing)
    addition_prices = []
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh'] and level['price'] < first_entry_price:
                addition_prices.append(level['price'])
            elif not is_buy and level['isHigh'] and level['price'] > first_entry_price:
                addition_prices.append(level['price'])
    # Ensure we have at least max_grid_levels-1 addition prices
    levels_needed = max_grid_levels - 1
    if len(addition_prices) < levels_needed:
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        last_price = addition_prices[-1] if addition_prices else first_entry_price
        for i in range(len(addition_prices), levels_needed):
            if is_buy:
                last_price -= grid_spacing
            else:
                last_price += grid_spacing
            addition_prices.append(last_price)
    
    # Find farthest addition price (lowest for buys, highest for sells)
    farthest_price = min(addition_prices) if is_buy else max(addition_prices)
    worst_distance = abs(farthest_price - hardstop)
    if worst_distance <= 0:
        # fallback: use grid_spacing * (max_grid_levels-1) + first_entry * max_expected_move_percent/100
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        worst_distance = grid_spacing * (max_grid_levels - 1) + first_entry_price * (max_expected_move_percent / 100.0)
    
    ticks = worst_distance / tick_size
    loss_per_lot = ticks * tick_value
    if loss_per_lot <= 0:
        lot = min_lot
    else:
        reserved_positions = max_grid_levels  # all positions including first
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
        lot = math.floor(lot / lot_step) * lot_step
        if lot < min_lot:
            lot = min_lot
        if lot > max_lot:
            lot = max_lot
    
    return lot


# ----------------------------------------------------------------------
# New convex curve functions (using corrected base lot)
# ----------------------------------------------------------------------

def compute_projected_loss_for_curve(curve, is_buy, first_entry_price,
                                      max_expected_move_percent, grid_levels,
                                      min_entry_spacing_percent, tick_value, tick_size):
    """Compute projected loss for a curve of lot sizes (list, length = max_grid_levels)."""
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    total_loss = 0.0
    # First trade uses curve[0]
    distance = abs(first_entry_price - hardstop)
    ticks = distance / tick_size
    loss = ticks * tick_value * curve[0]
    total_loss += loss
    # Build addition prices (same as before)
    addition_prices = []
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh'] and level['price'] < first_entry_price:
                addition_prices.append(level['price'])
            elif not is_buy and level['isHigh'] and level['price'] > first_entry_price:
                addition_prices.append(level['price'])
    num_needed = len(curve) - 1
    while len(addition_prices) < num_needed:
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        last_price = addition_prices[-1] if addition_prices else first_entry_price
        if is_buy:
            last_price -= grid_spacing
        else:
            last_price += grid_spacing
        addition_prices.append(last_price)
    for i, price in enumerate(addition_prices[:num_needed]):
        distance = abs(price - hardstop)
        ticks = distance / tick_size
        loss = ticks * tick_value * curve[i+1]
        total_loss += loss
    return total_loss

def calculate_convex_lot_curve(equity, protected_floor, initial_account_equity,
                               direction, first_entry_price, free_margin,
                               max_expected_move_percent, max_grid_levels,
                               max_recovery_additions, min_operational_additions,
                               min_entry_spacing_percent, mor_safety_multiplier,
                               growth_participation_percent, grid_levels,
                               tick_value, tick_size, min_lot, max_lot, lot_step, symbol,
                               curve_acceleration=1.35, protected_early_levels=2,
                               min_partial_capable_lot=0.02):
    """
    Returns a list of lot sizes for each grid level (0..max_grid_levels-1)
    following a convex (late-loading) distribution while preserving total
    projected hardstop loss equal to the flat distribution.
    """
    # Compute flat lot size using corrected function
    flat_lot = calculate_dynamic_lot_size(equity, protected_floor, initial_account_equity,
                                          direction, first_entry_price, free_margin,
                                          max_expected_move_percent, max_grid_levels,
                                          max_recovery_additions, min_operational_additions,
                                          min_entry_spacing_percent, mor_safety_multiplier,
                                          growth_participation_percent, grid_levels,
                                          tick_value, tick_size, min_lot, max_lot, lot_step, symbol)
    total_lot_budget = flat_lot * max_grid_levels
    
    # Generate convex weights
    weights = [math.pow(curve_acceleration, i) for i in range(max_grid_levels)]
    sum_weights = sum(weights)
    curve = [total_lot_budget * w / sum_weights for w in weights]
    
    # Round and enforce min for protected early levels
    for i in range(max_grid_levels):
        curve[i] = math.floor(curve[i] / lot_step) * lot_step
        if i < protected_early_levels and curve[i] < min_partial_capable_lot - 1e-8:
            curve[i] = min_partial_capable_lot
            curve[i] = math.floor(curve[i] / lot_step) * lot_step
    
    # Adjust for early floor: renormalize later levels
    used_budget = sum(curve[:protected_early_levels])
    remaining_budget = total_lot_budget - used_budget
    if remaining_budget < 0:
        # Scale down early levels (should not happen)
        scale = total_lot_budget / used_budget
        for i in range(protected_early_levels):
            curve[i] = math.floor(curve[i] * scale / lot_step) * lot_step
        used_budget = sum(curve[:protected_early_levels])
        remaining_budget = total_lot_budget - used_budget
    
    if remaining_budget > 0 and max_grid_levels > protected_early_levels:
        later_weights = weights[protected_early_levels:]
        later_sum = sum(later_weights)
        for i in range(protected_early_levels, max_grid_levels):
            if later_sum > 0:
                curve[i] = remaining_budget * later_weights[i - protected_early_levels] / later_sum
            else:
                curve[i] = remaining_budget / (max_grid_levels - protected_early_levels)
            curve[i] = math.floor(curve[i] / lot_step) * lot_step
            if curve[i] < min_lot:
                curve[i] = min_lot
    
    # Final min/max enforcement
    for i in range(max_grid_levels):
        if curve[i] < min_lot:
            curve[i] = min_lot
        if curve[i] > max_lot:
            curve[i] = max_lot
    
    # Verify projected loss against flat baseline
    is_buy = (direction == "buy")
    flat_loss = compute_projected_loss_fixed_lot(flat_lot, max_grid_levels, is_buy,
                                                 first_entry_price, max_expected_move_percent,
                                                 grid_levels, min_entry_spacing_percent,
                                                 tick_value, tick_size, min_lot)
    curve_loss = compute_projected_loss_for_curve(curve, is_buy,
                                                  first_entry_price, max_expected_move_percent,
                                                  grid_levels, min_entry_spacing_percent,
                                                  tick_value, tick_size)
    if flat_loss > 0 and abs(curve_loss - flat_loss) / flat_loss > 0.01:
        # Scale the entire curve proportionally
        scale = flat_loss / curve_loss
        for i in range(max_grid_levels):
            curve[i] = max(min_lot, math.floor(curve[i] * scale / lot_step) * lot_step)
            if curve[i] > max_lot:
                curve[i] = max_lot
    
    return curve


def compute_projected_loss_fixed_lot(lot_size, num_levels, is_buy, first_entry_price,
                                      max_expected_move_percent, grid_levels,
                                      min_entry_spacing_percent, tick_value, tick_size, min_lot):
    """Compute projected loss at hardstop for a fixed lot size across all levels."""
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    total_loss = 0.0
    # First trade
    distance = abs(first_entry_price - hardstop)
    ticks = distance / tick_size
    loss = ticks * tick_value * lot_size
    total_loss += loss
    # Build addition prices
    addition_prices = []
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh'] and level['price'] < first_entry_price:
                addition_prices.append(level['price'])
            elif not is_buy and level['isHigh'] and level['price'] > first_entry_price:
                addition_prices.append(level['price'])
    # Ensure at least num_levels-1 additions
    while len(addition_prices) < num_levels - 1:
        grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
        last_price = addition_prices[-1] if addition_prices else first_entry_price
        if is_buy:
            last_price -= grid_spacing
        else:
            last_price += grid_spacing
        addition_prices.append(last_price)
    for price in addition_prices[:num_levels-1]:
        distance = abs(price - hardstop)
        ticks = distance / tick_size
        loss = ticks * tick_value * lot_size
        total_loss += loss
    return total_loss


def can_add_position(projected_loss, allowed_loss):
    """Simple check: addition allowed if projected loss <= allowed loss."""
    return projected_loss <= allowed_loss