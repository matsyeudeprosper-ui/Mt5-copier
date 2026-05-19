import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from supabase import create_client
import os

logger = logging.getLogger(__name__)

# Supabase client
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized in scheduler")
    except Exception as e:
        logger.error(f"Supabase initialization failed: {e}")

def send_expiry_reminders():
    """Send Telegram reminders to users whose license expires in 3 days."""
    if not supabase:
        return
    now = datetime.now()
    reminder_date = now + timedelta(days=3)
    try:
        # Get all non-master active licenses with expiry and telegram chat id
        result = supabase.table('licenses').select('license_key, telegram_chat_id, expires_at').eq('is_master', False).eq('is_active', True).not_.is_('expires_at', 'null').execute()
        for lic in result.data:
            chat_id = lic.get('telegram_chat_id')
            if not chat_id:
                continue
            expires_at = datetime.fromisoformat(lic['expires_at'])
            if expires_at <= reminder_date and expires_at > now:
                days_left = (expires_at - now).days
                # Import inside to avoid circular import
                from notifications import send_expiry_reminder
                send_expiry_reminder(chat_id, lic['license_key'], lic['expires_at'], days_left)
    except Exception as e:
        logger.error(f"send_expiry_reminders error: {e}")

def get_users_for_report():
    """Return list of (telegram_chat_id, report_hour, report_frequency) for users with auto_report enabled."""
    if not supabase:
        return []
    try:
        result = supabase.table('user_settings').select('telegram_chat_id, report_hour, report_frequency').eq('auto_report_enabled', True).execute()
        return [(row['telegram_chat_id'], row['report_hour'], row['report_frequency']) for row in result.data]
    except Exception as e:
        logger.error(f"get_users_for_report error: {e}")
        return []

def send_report_to_user(chat_id, period, lang='en'):
    """Send a report to a specific user by calling handle_report (defined in telegram_bot)."""
    # Import inside to avoid circular import
    from telegram_bot import handle_report
    handle_report(chat_id, period, lang)

def check_and_send_reports():
    """Check each hour which users need a report and send it."""
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