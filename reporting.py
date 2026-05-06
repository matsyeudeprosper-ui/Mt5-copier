import sqlite3
from datetime import datetime, timedelta
import numpy as np

DB_PATH = "/tmp/trades.db"

def get_license_purchase_date(telegram_chat_id):
    """Return the activation date (activated_at) of the user's first license."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT activated_at FROM licenses WHERE telegram_chat_id = ? AND is_master = 0 ORDER BY activated_at ASC LIMIT 1", (telegram_chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row[0])
    return None

def get_trades_from_date(start_date):
    """Retrieve all closed trades (action='close') from a given date onward."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE action = 'close' AND timestamp >= ? ORDER BY timestamp ASC", (start_date.isoformat(),))
    rows = c.fetchall()
    conn.close()
    return rows

def get_pip_value_and_currency(telegram_chat_id):
    """Return (pip_value, currency) from user_settings, or (None, None) if not set."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT pip_value, base_currency FROM user_settings WHERE telegram_chat_id = ?", (telegram_chat_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None:
        return row[0], row[1]
    return None, "USD"

def calculate_stats(trades, pip_value=None):
    """
    Compute statistics from list of close trades.
    Each trade row: ... close_profit (pips).
    If pip_value is given, multiply pips by pip_value to get profit in currency.
    """
    if not trades:
        return None
    profits = [row[9] for row in trades]  # close_profit (in pips)
    if pip_value is not None:
        profits = [p * pip_value for p in profits]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    total_trades = len(profits)
    wins_count = len(wins)
    losses_count = len(losses)
    win_rate = (wins_count / total_trades * 100) if total_trades > 0 else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net_profit = gross_profit - gross_loss
    avg_win = gross_profit / wins_count if wins_count > 0 else 0
    avg_loss = gross_loss / losses_count if losses_count > 0 else 0
    # Drawdown based on cumulative profits (in pips or currency)
    cumulative = np.cumsum(profits)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = cumulative - running_max
    max_drawdown = abs(np.min(drawdown)) if len(drawdown) > 0 else 0
    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "wins": wins_count,
        "losses": losses_count,
        "profit_factor": round(profit_factor, 2),
        "net_profit": round(net_profit, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_drawdown, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2)
    }