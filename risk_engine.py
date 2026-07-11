import math

DEFAULT_MIN_LOT = 0.01
DEFAULT_MAX_LOT = 0.50
DEFAULT_LOT_STEP = 0.01
BASE_LOT_SIZE = 0.02
MAX_MARGIN_USAGE_PERCENT = 20.0


def build_addition_prices(is_buy, first_entry_price, grid_levels, min_entry_spacing_percent, count_needed):
    """
    Build up to `count_needed` addition prices in the adverse direction from
    first_entry_price: real structural levels first (nearest-to-entry order),
    then synthetic fallback prices continuing outward from the last known
    price (real or synthetic) using min_entry_spacing_percent for any
    remaining slots. Used consistently by every loss/sizing calculation below
    so they all agree on where the grid's addition levels actually sit.
    """
    real_prices = []
    if grid_levels:
        for level in grid_levels:
            if is_buy and not level['isHigh'] and level['price'] < first_entry_price:
                real_prices.append(level['price'])
            elif not is_buy and level['isHigh'] and level['price'] > first_entry_price:
                real_prices.append(level['price'])

    # Nearest-to-entry first
    if is_buy:
        real_prices.sort(reverse=True)
    else:
        real_prices.sort()

    addition_prices = real_prices[:count_needed]

    grid_spacing = first_entry_price * (min_entry_spacing_percent / 100.0)
    last_price = addition_prices[-1] if addition_prices else first_entry_price
    while len(addition_prices) < count_needed:
        last_price = (last_price - grid_spacing) if is_buy else (last_price + grid_spacing)
        addition_prices.append(last_price)

    return addition_prices


def loss_distance(entry_price, hardstop_price, is_buy):
    """
    Distance (>=0) from entry_price to hardstop_price in the adverse
    direction. Mirrors the MQL5 PositionLossAtHardstopPrice clamp: if
    entry_price sits beyond the hardstop, the basket would already have
    closed before price ever reached it, so that position could never
    actually open — its loss contribution is 0, not a phantom distance.
    """
    distance = (entry_price - hardstop_price) if is_buy else (hardstop_price - entry_price)
    return max(distance, 0.0)


def compute_minimum_operational_reserve(is_buy, max_expected_move_percent, min_entry_spacing_percent,
                                        grid_levels, min_operational_additions, mor_safety_multiplier,
                                        first_entry_price, tick_value, tick_size, symbol):
    min_lot = DEFAULT_MIN_LOT
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)

    addition_prices = build_addition_prices(is_buy, first_entry_price, grid_levels,
                                            min_entry_spacing_percent, min_operational_additions)

    total_loss = 0.0
    # First trade
    distance = loss_distance(first_entry_price, hardstop, is_buy)
    ticks = distance / tick_size if tick_size > 0 else 0
    loss = ticks * tick_value * min_lot
    total_loss += loss

    for price in addition_prices:
        distance = loss_distance(price, hardstop, is_buy)
        ticks = distance / tick_size if tick_size > 0 else 0
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
    # Guard against zero tick_value (e.g., market closed or invalid symbol)
    if tick_value <= 0 or tick_size <= 0:
        return min_lot

    unlocked_surplus = max(0, equity - protected_floor)

    # Compute total allowed loss (always, not only when surplus > 0.01)
    is_buy = (direction == "buy")
    mor = compute_minimum_operational_reserve(is_buy, max_expected_move_percent,
                                              min_entry_spacing_percent, grid_levels,
                                              min_operational_additions, mor_safety_multiplier,
                                              first_entry_price, tick_value, tick_size, symbol)

    locked_profit = max(0, protected_floor - initial_account_equity)
    growth_participation = locked_profit * (growth_participation_percent / 100.0)
    total_allowed_loss = mor + unlocked_surplus + growth_participation

    # Worst distance calculation. Use the real farthest structural addition
    # level when available (grid_levels) instead of always assuming the
    # synthetic min-spacing ladder — real S/R levels can sit much farther
    # from first entry than a few min_entry_spacing_percent hops, and sizing
    # against too-close an assumption overstates the affordable lot.
    hardstop_distance = first_entry_price * (max_expected_move_percent / 100.0)
    addition_prices = build_addition_prices(is_buy, first_entry_price, grid_levels,
                                            min_entry_spacing_percent, max_grid_levels - 1)
    farthest_grid_distance = abs(addition_prices[-1] - first_entry_price) if addition_prices else 0.0
    worst_distance = hardstop_distance + farthest_grid_distance

    ticks = worst_distance / tick_size
    loss_per_lot = ticks * tick_value
    if loss_per_lot <= 0:
        lot = min_lot
    else:
        max_lot_possible = total_allowed_loss / (loss_per_lot * max_grid_levels)
        lot = min(max_lot_possible, max_lot)
        lot = math.floor(lot / lot_step) * lot_step
        lot = max(lot, min_lot)

    # Cap for small accounts (optional, but keep)
    if lot > 0.50 and equity < 10000:
        lot = 0.50

    # Ensure lot can be partially closed (at least 2 * min_lot) but only if risk allows
    min_allowed = min_lot * 2.0
    if lot < min_allowed - 1e-8:
        # Instead of forcing min_allowed unconditionally, cap it to available risk
        potential_lot = min(min_allowed, max_lot_possible if loss_per_lot > 0 else max_lot)
        if potential_lot >= min_lot:
            lot = potential_lot
            lot = math.floor(lot / lot_step) * lot_step
            if lot < min_lot:
                lot = min_lot
            if lot > max_lot:
                lot = max_lot
        # else keep original lot (already minimal)

    return lot


def compute_projected_loss_fixed_lot(lot_size, num_levels, is_buy, first_entry_price,
                                      max_expected_move_percent, grid_levels,
                                      min_entry_spacing_percent, tick_value, tick_size, min_lot):
    if tick_value <= 0 or tick_size <= 0:
        return 0.0
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    total_loss = 0.0
    distance = loss_distance(first_entry_price, hardstop, is_buy)
    ticks = distance / tick_size
    total_loss += ticks * tick_value * lot_size

    addition_prices = build_addition_prices(is_buy, first_entry_price, grid_levels,
                                            min_entry_spacing_percent, num_levels - 1)
    for price in addition_prices:
        distance = loss_distance(price, hardstop, is_buy)
        ticks = distance / tick_size
        total_loss += ticks * tick_value * lot_size
    return total_loss


def compute_projected_loss_for_curve(curve, is_buy, first_entry_price,
                                      max_expected_move_percent, grid_levels,
                                      min_entry_spacing_percent, tick_value, tick_size):
    if tick_value <= 0 or tick_size <= 0:
        return 0.0
    hardstop = first_entry_price * (1.0 - max_expected_move_percent / 100.0) if is_buy else first_entry_price * (1.0 + max_expected_move_percent / 100.0)
    total_loss = 0.0
    distance = loss_distance(first_entry_price, hardstop, is_buy)
    ticks = distance / tick_size
    total_loss += ticks * tick_value * curve[0]

    addition_prices = build_addition_prices(is_buy, first_entry_price, grid_levels,
                                            min_entry_spacing_percent, len(curve) - 1)
    for i, price in enumerate(addition_prices):
        distance = loss_distance(price, hardstop, is_buy)
        ticks = distance / tick_size
        total_loss += ticks * tick_value * curve[i + 1]
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
    # Guard zero tick_value
    if tick_value <= 0 or tick_size <= 0:
        return [min_lot] * max_grid_levels

    flat_lot = calculate_dynamic_lot_size(equity, protected_floor, initial_account_equity,
                                          direction, first_entry_price, free_margin,
                                          max_expected_move_percent, max_grid_levels,
                                          max_recovery_additions, min_operational_additions,
                                          min_entry_spacing_percent, mor_safety_multiplier,
                                          growth_participation_percent, grid_levels,
                                          tick_value, tick_size, min_lot, max_lot, lot_step, symbol)
    total_lot_budget = flat_lot * max_grid_levels

    weights = [math.pow(curve_acceleration, i) for i in range(max_grid_levels)]
    sum_weights = sum(weights)
    curve = [total_lot_budget * w / sum_weights for w in weights]

    # Round and enforce early minimum
    for i in range(max_grid_levels):
        curve[i] = math.floor(curve[i] / lot_step) * lot_step
        if i < protected_early_levels and curve[i] < min_partial_capable_lot - 1e-8:
            curve[i] = min_partial_capable_lot
            curve[i] = math.floor(curve[i] / lot_step) * lot_step

    used_budget = sum(curve[:protected_early_levels])
    remaining_budget = total_lot_budget - used_budget
    if remaining_budget < 0:
        scale = total_lot_budget / used_budget if used_budget > 0 else 1.0
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

    for i in range(max_grid_levels):
        if curve[i] < min_lot:
            curve[i] = min_lot
        if curve[i] > max_lot:
            curve[i] = max_lot

    # Verify risk parity – adjust curve to match flat loss, but only if flat_loss > 0
    is_buy = (direction == "buy")
    flat_loss = compute_projected_loss_fixed_lot(flat_lot, max_grid_levels, is_buy,
                                                 first_entry_price, max_expected_move_percent,
                                                 grid_levels, min_entry_spacing_percent,
                                                 tick_value, tick_size, min_lot)
    curve_loss = compute_projected_loss_for_curve(curve, is_buy,
                                                  first_entry_price, max_expected_move_percent,
                                                  grid_levels, min_entry_spacing_percent,
                                                  tick_value, tick_size)
    if flat_loss > 0 and curve_loss > 0 and abs(curve_loss - flat_loss) / flat_loss > 0.01:
        scale = flat_loss / curve_loss
        for i in range(max_grid_levels):
            curve[i] = max(min_lot, math.floor(curve[i] * scale / lot_step) * lot_step)
            if curve[i] > max_lot:
                curve[i] = max_lot

    return curve


def can_add_position(projected_loss, allowed_loss):
    """
    Determine if a new grid level can be added based on remaining loss budget.
    """
    return projected_loss <= allowed_loss
