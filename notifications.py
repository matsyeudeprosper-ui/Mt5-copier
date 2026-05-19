import logging
from datetime import datetime, timedelta
from telegram_bot import send_telegram
import db
from supabase import create_client
import os

logger = logging.getLogger(__name__)

# Supabase client (reuse same as in app.py)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized for notifications")
    except Exception as e:
        logger.error(f"Supabase initialization failed: {e}")

def send_expiry_reminder(chat_id, license_key, expires_at, days_left):
    """Send a license expiry reminder to a user (only if not sent in last 24 hours)."""
    if not chat_id:
        return
    # Check last reminder time from local db (fallback)
    conn = db.get_conn()
    c = conn.cursor()
    c.execute("SELECT last_expiry_reminder FROM licenses WHERE license_key = ?", (license_key,))
    row = c.fetchone()
    conn.close()
    last = row[0] if row else None
    if last:
        last_date = datetime.fromisoformat(last)
        if datetime.now() - last_date < timedelta(hours=24):
            logger.debug(f"Expiry reminder already sent in last 24h for {license_key}")
            return
    msg = (f"⚠️ Your license for MT5 Trade Copier will expire in {days_left} day(s). "
           f"Please renew via /buy to continue copying trades.")
    send_telegram(chat_id, msg)
    db.update_last_expiry_reminder(license_key)
    logger.info(f"Expiry reminder sent to {chat_id} for key {license_key}")

def send_validation_failure_notification(license_key, reason):
    """
    Send a notification when license validation fails (cooldown: once per hour).
    reason can be "expired" or "wrong_account".
    """
    lic = db.get_license_by_key(license_key)
    if not lic or not lic["chat_id"]:
        return
    chat_id = lic["chat_id"]
    # Check last failure notification time
    conn = db.get_conn()
    c = conn.cursor()
    c.execute("SELECT last_failure_notification FROM licenses WHERE license_key = ?", (license_key,))
    row = c.fetchone()
    conn.close()
    last = row[0] if row else None
    if last:
        last_date = datetime.fromisoformat(last)
        if datetime.now() - last_date < timedelta(hours=1):
            logger.debug(f"Failure notification already sent in last hour for {license_key}")
            return
    if reason == "expired":
        msg = "❌ Your license has expired. Please renew via /buy."
    elif reason == "wrong_account":
        msg = "❌ The MT5 account number does not match the bound account. Use /unbind on Telegram to reset."
    else:
        msg = f"❌ License validation failed: {reason}"
    send_telegram(chat_id, msg)
    db.update_last_failure_notification(license_key)
    logger.info(f"Validation failure notification sent to {chat_id} for key {license_key}")

def send_general_notification(chat_id, message):
    """Send any general message to a user (no cooldown)."""
    if chat_id:
        send_telegram(chat_id, message)
        logger.info(f"General notification sent to {chat_id}")

def send_trade_close_notification(chat_id, symbol, profit, trade_type, num_positions):
    """Send a Telegram notification when a trade (single or basket) closes."""
    if not chat_id:
        return
    # Try to get user's base currency from Supabase
    currency = "USD"
    if supabase:
        try:
            result = supabase.table('user_settings').select('base_currency').eq('telegram_chat_id', str(chat_id)).execute()
            if result.data and result.data[0].get('base_currency'):
                currency = result.data[0]['base_currency']
        except Exception as e:
            logger.error(f"Failed to fetch user currency: {e}")
    if trade_type == "single":
        msg = f"✅ Trade closed on {symbol}\nProfit: {profit:.2f} {currency}"
    else:
        msg = f"✅ Basket closed on {symbol} ({num_positions} positions)\nNet Profit: {profit:.2f} {currency}"
    send_telegram(chat_id, msg)

def send_basket_close_report(chat_id, start_equity, final_equity, total_profit, num_trades, symbol, currency="USD"):
    """
    Send a report when a basket is fully closed.
    Uses Supabase to get user's preferred currency from settings if available.
    """
    if not chat_id:
        return
    # Try to get user's base currency from Supabase
    base_currency = currency
    if supabase:
        try:
            result = supabase.table('user_settings').select('base_currency').eq('telegram_chat_id', str(chat_id)).execute()
            if result.data and result.data[0].get('base_currency'):
                base_currency = result.data[0]['base_currency']
        except Exception as e:
            logger.error(f"Failed to fetch user currency: {e}")
    profit_pct = (total_profit / start_equity) * 100 if start_equity > 0 else 0
    msg = (
        f"📊 <b>Basket Closed</b> ({symbol})\n"
        f"└ Start Equity: {start_equity:.2f} {base_currency}\n"
        f"└ Final Equity: {final_equity:.2f} {base_currency}\n"
        f"└ Total Profit: {total_profit:.2f} {base_currency} ({profit_pct:.2f}%)\n"
        f"└ Number of Trades: {num_trades}\n"
        f"✅ Full exit completed."
    )
    send_telegram(chat_id, msg)
    logger.info(f"Basket close report sent to {chat_id}")