import json
import sqlite3
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

DB_PATH = "/tmp/trades.db"
logger = logging.getLogger(__name__)

import reporting
from notifications import send_expiry_reminder

def send_expiry_reminders():
    """Send Telegram reminders to users whose license expires in 3 days."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    reminder_date = now + timedelta(days=3)
    c.execute("SELECT license_key, telegram_chat_id, expires_at FROM licenses WHERE expires_at IS NOT NULL AND is_master = 0 AND is_active = 1")
    rows = c.fetchall()
    conn.close()
    for license_key, chat_id, expires_at_str in rows:
        if not chat_id:
            continue
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at <= reminder_date and expires_at > now:
            days_left = (expires_at - now).days
            send_expiry_reminder(chat_id, license_key, expires_at_str, days_left)

def get_users_for_report():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_chat_id, report_hour, report_frequency FROM user_settings WHERE auto_report_enabled = 1")
    rows = c.fetchall()
    conn.close()
    return rows

def send_report_to_user(chat_id, period, lang='en'):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT activated_at FROM licenses WHERE telegram_chat_id = ? AND is_master = 0 ORDER BY activated_at ASC LIMIT 1", (str(chat_id),))
    row = c.fetchone()
    if not row:
        return
    purchase_date = datetime.fromisoformat(row[0])
    trades = reporting.get_trades_from_date(purchase_date)
    now = datetime.now()
    if period == 'daily':
        cutoff = now - timedelta(days=1)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    elif period == 'weekly':
        cutoff = now - timedelta(days=7)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    elif period == 'monthly':
        cutoff = now - timedelta(days=30)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    else:
        return

    c.execute("SELECT starting_balance, base_currency, deposits, pip_value, currency FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    row2 = c.fetchone()
    conn.close()

    start_balance = None
    currency = "USD"
    pip_value = None
    pip_currency = None
    if row2:
        start, curr, deposits_json, pv, pc = row2
        deposits = json.loads(deposits_json) if deposits_json else []
        total_deposits = sum(deposits) if deposits else 0
        effective_start = start + total_deposits if start else 0
        start_balance = effective_start if effective_start > 0 else None
        currency = curr
        pip_value = pv
        pip_currency = pc

    with open("config.json", "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
    MESSAGES = CONFIG["messages"]

    stats = reporting.calculate_stats(trades, pip_value=pip_value)
    if not stats:
        return
    if pip_value is not None:
        net_profit_str = f"{stats['net_profit']:.2f} {pip_currency}"
        avg_win_str = f"{stats['avg_win']:.2f} {pip_currency}"
        avg_loss_str = f"{stats['avg_loss']:.2f} {pip_currency}"
        max_dd_str = f"{stats['max_drawdown']:.2f} {pip_currency}"
        currency_symbol = pip_currency
    else:
        net_profit_str = f"{stats['net_profit']:.2f} pips"
        avg_win_str = f"{stats['avg_win']:.2f} pips"
        avg_loss_str = f"{stats['avg_loss']:.2f} pips"
        max_dd_str = f"{stats['max_drawdown']:.2f} pips"
        currency_symbol = "pips"

    msg = MESSAGES[f"report_{period}"][lang] + "\n\n"
    msg += MESSAGES["report_trades"][lang].format(
        total=stats['total_trades'],
        win_rate=stats['win_rate'],
        wins=stats['wins'],
        losses=stats['losses'],
        profit_factor=stats['profit_factor'],
        net_profit=net_profit_str,
        currency=currency_symbol,
        avg_win=avg_win_str,
        avg_loss=avg_loss_str,
        max_drawdown=max_dd_str
    )
    if start_balance is not None and start_balance > 0 and pip_value is not None:
        final_balance = start_balance + stats['net_profit']
        growth = (stats['net_profit'] / start_balance) * 100
        msg += "\n\n" + MESSAGES["report_equity"][lang].format(
            start=round(start_balance, 2),
            final=round(final_balance, 2),
            growth=round(growth, 2),
            currency=currency_symbol
        )

    try:
        from telegram_bot import send_telegram
        send_telegram(chat_id, msg)
    except ImportError:
        logging.error("Could not import send_telegram from app")

def check_and_send_reports():
    users = get_users_for_report()
    now = datetime.now()
    current_hour = now.hour
    is_monday = now.weekday() == 0
    is_first_of_month = now.day == 1

    for chat_id, hour, freq in users:
        if hour != current_hour:
            continue
        if freq == 'daily':
            send_report_to_user(chat_id, 'daily')
        elif freq == 'weekly' and is_monday:
            send_report_to_user(chat_id, 'weekly')
        elif freq == 'monthly' and is_first_of_month:
            send_report_to_user(chat_id, 'monthly')

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_and_send_reports, trigger=CronTrigger(minute=0, hour='*'), id='hourly_report_check')
    scheduler.add_job(func=send_expiry_reminders, trigger=CronTrigger(hour=10, minute=0), id='expiry_reminder')
    scheduler.start()
    logging.info("Scheduler started (report check + expiry reminders)")
    return scheduler