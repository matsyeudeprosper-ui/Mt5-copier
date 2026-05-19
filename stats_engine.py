"""
Statistics engine for public performance report.
Aggregates trade data from Supabase and returns metrics.
"""

import logging

logger = logging.getLogger(__name__)

def get_public_stats(supabase_client):
    """Return aggregated trading statistics for public report using the provided Supabase client."""
    if not supabase_client:
        return {"error": "Supabase client not available"}

    try:
        # Exclude master licenses
        master_licenses = supabase_client.table('licenses').select('license_key').eq('is_master', True).execute()
        master_keys = [row['license_key'] for row in master_licenses.data] if master_licenses.data else []
        # Fetch all trades
        trades_data = supabase_client.table('trades').select('profit, equity_before, equity_after, closed_at, license_key').execute()
        trades = trades_data.data
        # Filter out master trades
        if master_keys:
            trades = [t for t in trades if t.get('license_key') not in master_keys]
    except Exception as e:
        logger.error(f"API stats error: {e}")
        return {"error": str(e)}

    if not trades:
        return {"error": "No trades found"}

    # Compute metrics
    profits = [t['profit'] for t in trades]
    total_profit = sum(profits)
    num_trades = len(trades)
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0

    # Equity curve & max drawdown
    equity_curve = []
    equity = 0
    for t in trades:
        if t.get('equity_before') is not None:
            equity = t['equity_after']
        else:
            equity += t['profit']
        equity_curve.append(equity)
    max_drawdown = 0
    if equity_curve:
        peak = equity_curve[0]
        for val in equity_curve:
            if val > peak:
                peak = val
            drawdown = peak - val
            if drawdown > max_drawdown:
                max_drawdown = drawdown

    # Equity growth
    start_equity = None
    for t in trades:
        if t.get('equity_before') is not None:
            start_equity = t['equity_before']
            break
    if start_equity is None:
        start_equity = 0
    end_equity = start_equity + total_profit
    growth = (total_profit / start_equity * 100) if start_equity > 0 else 0
    recovery_factor = total_profit / max_drawdown if max_drawdown > 0 else 0

    # Prepare chart data
    trades_sorted = sorted(trades, key=lambda x: x['closed_at'])
    dates = [t['closed_at'][:10] for t in trades_sorted]
    cumulative = []
    cum = 0
    for t in trades_sorted:
        cum += t['profit']
        cumulative.append(cum)

    return {
        "total_profit": round(total_profit, 2),
        "num_trades": num_trades,
        "win_rate": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_drawdown, 2),
        "recovery_factor": round(recovery_factor, 2),
        "growth": round(growth, 1),
        "start_equity": round(start_equity, 2),
        "end_equity": round(end_equity, 2),
        "dates": dates,
        "cumulative_profit": cumulative
    }