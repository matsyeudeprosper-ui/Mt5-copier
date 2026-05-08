import logging
from datetime import datetime, timedelta
from telegram_bot import send_telegram
import db

logger = logging.getLogger(__name__)

def send_expiry_reminder(chat_id, license_key, expires_at, days_left):
    """Send a license expiry reminder to a user (only if not sent in last 24 hours)."""
    if not chat_id:
        return
    # Check last reminder time
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