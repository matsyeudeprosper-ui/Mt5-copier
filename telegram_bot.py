import os
import json
import secrets
import logging
import requests
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
import db
from config import PLANS, MESSAGES
import reporting

bot_bp = Blueprint('bot', __name__)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Initialize Supabase client (required, will crash if missing)
if not SUPABASE_URL or not SUPABASE_KEY:
    raise Exception("SUPABASE_URL and SUPABASE_KEY must be set in environment variables")

from supabase import create_client, Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Supabase client initialized for bot")

# No JSON fallback – all operations go directly to Supabase

def set_bot_token(token):
    global TELEGRAM_BOT_TOKEN
    TELEGRAM_BOT_TOKEN = token

def send_telegram(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

def notify_admin(message):
    if TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN:
        send_telegram(TELEGRAM_CHAT_ID, message)

# ----- Helper functions (direct Supabase queries) -----
def get_user_license(chat_id):
    """Return first license (dict) for this user, or None."""
    try:
        result = supabase.table('licenses').select('*').eq('telegram_chat_id', str(chat_id)).eq('is_master', False).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"get_user_license error: {e}")
        return None

def get_main_menu_markup(chat_id, lang):
    settings = db.get_user_settings(chat_id)
    has_license = get_user_license(chat_id) is not None
    show_trial = (not has_license) and (not settings["trial_used"])
    buttons = []
    if show_trial:
        buttons.append([{"text": MESSAGES["trial_button"][lang], "callback_data": "menu_trial"}])
    buttons.append([{"text": MESSAGES["buy_license"][lang], "callback_data": "menu_buy"}])
    buttons.append([{"text": MESSAGES["my_status"][lang], "callback_data": "menu_status"}])
    buttons.append([{"text": MESSAGES["unbind_account"][lang], "callback_data": "menu_unbind"}])
    return {"inline_keyboard": buttons}

def plan_selection_markup(lang):
    buttons = []
    for plan_id, plan_data in PLANS.items():
        buttons.append([{"text": plan_data["display"][lang], "callback_data": f"plan_{plan_id}"}])
    return {"inline_keyboard": buttons}

def answer_callback(callback_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_id})
    except Exception:
        pass

def check_license_status(chat_id, lang):
    lic = get_user_license(chat_id)
    if not lic:
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    license_key = lic['license_key']
    bound_account = lic.get('bound_account')
    expires_at = lic.get('expires_at')
    is_trial = lic.get('is_trial', False)
    expiry_str = expires_at if expires_at else MESSAGES["never"][lang]
    bound_str = bound_account if bound_account else MESSAGES["not_bound"][lang]
    trial_str = " (Trial)" if is_trial else ""
    settings = db.get_user_settings(chat_id)
    hard_stop_str = "Yes" if settings["hard_stop_hit"] else "No"
    msg = MESSAGES["license_status"][lang].format(key=license_key, bound=bound_str, expires=expiry_str, hard_stop=hard_stop_str) + trial_str
    send_telegram(chat_id, msg)

def unbind_license(chat_id, lang):
    lic = get_user_license(chat_id)
    if not lic:
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    bound_account = lic.get('bound_account')
    if bound_account is None:
        send_telegram(chat_id, MESSAGES["unbind_none"][lang])
        return
    try:
        supabase.table('licenses').update({'bound_account': None}).eq('license_key', lic['license_key']).execute()
        send_telegram(chat_id, MESSAGES["unbind_success"][lang].format(account=bound_account))
        admin_msg = MESSAGES["admin_unbind"][lang].format(user=chat_id, key=lic['license_key'], account=bound_account)
        notify_admin(admin_msg)
    except Exception as e:
        logger.error(f"unbind error: {e}")
        send_telegram(chat_id, "Error unbinding license.")

def handle_trial(chat_id, lang):
    if get_user_license(chat_id) is not None:
        send_telegram(chat_id, MESSAGES["trial_already_exists"][lang])
        return
    settings = db.get_user_settings(chat_id)
    if settings["trial_used"]:
        send_telegram(chat_id, MESSAGES["trial_not_available"][lang])
        return
    trial_days = 7
    license_key, expires_at = create_or_extend_license(chat_id, trial_days, is_trial=True)
    if not license_key:
        send_telegram(chat_id, "Error creating trial license. Please try later.")
        return
    expiry_str = expires_at if expires_at else "7 days"
    db.mark_trial_used(chat_id)
    msg = MESSAGES["trial_success"][lang].format(key=license_key, expires=expiry_str)
    send_telegram(chat_id, msg)
    notify_admin(f"🧪 New trial license\nUser: {chat_id}\nKey: {license_key}\nExpires: {expiry_str}")

def handle_resume(chat_id, lang):
    lic = get_user_license(chat_id)
    if not lic:
        send_telegram(chat_id, MESSAGES["resume_not_needed"][lang])
        return
    settings = db.get_user_settings(chat_id)
    if not settings["hard_stop_hit"]:
        send_telegram(chat_id, MESSAGES["no_hard_stop"][lang])
        return
    db.set_hard_stop(chat_id, False)
    send_telegram(chat_id, MESSAGES["resume_success"][lang])

def show_report_menu(chat_id, lang):
    reply_markup = {
        "inline_keyboard": [
            [{"text": MESSAGES["report_daily_btn"][lang], "callback_data": "report_daily"}],
            [{"text": MESSAGES["report_weekly_btn"][lang], "callback_data": "report_weekly"}],
            [{"text": MESSAGES["report_monthly_btn"][lang], "callback_data": "report_monthly"}],
            [{"text": MESSAGES["report_alltime_btn"][lang], "callback_data": "report_alltime"}]
        ]
    }
    send_telegram(chat_id, MESSAGES["report_menu_title"][lang], reply_markup)

def show_settings_menu(chat_id, lang):
    settings = db.get_user_settings(chat_id)
    freq_display = MESSAGES["current_frequency"][lang].format(freq=settings["report_frequency"].capitalize())
    hour = settings["report_hour"]
    reply_markup = {
        "inline_keyboard": [
            [{"text": MESSAGES["auto_report_on"][lang] if settings["auto_report_enabled"] else MESSAGES["auto_report_off"][lang], "callback_data": "settings_auto_toggle"}],
            [{"text": MESSAGES["freq_daily_btn"][lang], "callback_data": "settings_freq_daily"},
             {"text": MESSAGES["freq_weekly_btn"][lang], "callback_data": "settings_freq_weekly"},
             {"text": MESSAGES["freq_monthly_btn"][lang], "callback_data": "settings_freq_monthly"}],
            [{"text": MESSAGES["report_hour"][lang].format(hour=hour), "callback_data": "settings_hour"}]
        ]
    }
    menu_text = MESSAGES["settings_title"][lang] + "\n\n" + freq_display
    send_telegram(chat_id, menu_text, reply_markup)

def toggle_auto_report(chat_id, lang):
    settings = db.get_user_settings(chat_id)
    new_state = not settings["auto_report_enabled"]
    db.set_user_settings(chat_id, auto_report_enabled=1 if new_state else 0)
    if new_state:
        send_telegram(chat_id, MESSAGES["auto_report_notify_on"][lang].format(hour=settings["report_hour"]))
    else:
        send_telegram(chat_id, MESSAGES["auto_report_notify_off"][lang])
    show_settings_menu(chat_id, lang)

def handle_report(chat_id, period, lang):
    if not get_user_license(chat_id):
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    purchase_date = reporting.get_license_purchase_date(str(chat_id))
    if not purchase_date:
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    trades = reporting.get_trades_from_date(purchase_date)
    now = datetime.now()
    if period == "daily":
        cutoff = now - timedelta(days=1)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    elif period == "weekly":
        cutoff = now - timedelta(days=7)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    elif period == "monthly":
        cutoff = now - timedelta(days=30)
        trades = [t for t in trades if datetime.fromisoformat(t[12]) >= cutoff]
    elif period == "alltime":
        trades = trades
    else:
        send_telegram(chat_id, "Invalid period.")
        return

    settings = db.get_user_settings(chat_id)
    pip_value = settings["pip_value"]
    pip_currency = settings["pip_currency"] or "USD"
    stats = reporting.calculate_stats(trades, pip_value=pip_value)
    if not stats:
        send_telegram(chat_id, MESSAGES["report_no_trades"][lang])
        return

    start_balance = settings["effective_start"] if settings["effective_start"] > 0 else None

    msg = MESSAGES[f"report_{period}"][lang] + "\n\n"
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
    send_telegram(chat_id, msg)

def create_payment_invoice(chat_id, plan_id, lang):
    if plan_id not in PLANS:
        send_telegram(chat_id, MESSAGES["invalid_plan"][lang])
        return
    plan = PLANS[plan_id]
    price = plan["price"]
    days = plan["days"]

    np_url = "https://api.nowpayments.io/v1/invoice"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    order_id = secrets.token_hex(8)
    payload = {
        "price_amount": price,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": f"MT5 Copier License - {plan_id}",
        "ipn_callback_url": "https://mt5-copier-vu1t.onrender.com/webhook/nowpayments",
        "success_url": f"https://mt5-copier-vu1t.onrender.com/payment_success?order_id={order_id}",
        "cancel_url": "https://mt5-copier-vu1t.onrender.com/payment_cancel"
    }

    logger.info(f"NowPayments request: {json.dumps(payload)}")
    try:
        resp = requests.post(np_url, json=payload, headers=headers, timeout=15)
        logger.info(f"NowPayments response status: {resp.status_code}")
        logger.info(f"NowPayments response body: {resp.text}")
        resp.raise_for_status()
        inv = resp.json()
        payment_url = inv.get("invoice_url")
        if not payment_url:
            send_telegram(chat_id, MESSAGES["payment_error"][lang])
            return

        conn = db.get_conn()
        c = conn.cursor()
        c.execute("INSERT INTO orders (order_id, plan, license_key, telegram_chat_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (order_id, plan_id, "", str(chat_id), "pending", datetime.now().isoformat()))
        conn.commit()
        conn.close()
        send_telegram(chat_id, MESSAGES["payment_link"][lang].format(url=payment_url))
    except Exception as e:
        logger.error(f"NowPayments error: {str(e)}")
        send_telegram(chat_id, MESSAGES["payment_error"][lang])

def create_or_extend_license(telegram_chat_id, expires_days, is_trial=False):
    """Create a new license or extend existing one in Supabase."""
    try:
        now = datetime.now()
        # Check existing license for this user
        existing = supabase.table('licenses').select('*').eq('telegram_chat_id', str(telegram_chat_id)).eq('is_master', False).execute()
        if existing.data:
            lic = existing.data[0]
            license_key = lic['license_key']
            old_expiry = lic.get('expires_at')
            if expires_days is None:
                new_expiry = None
            else:
                if old_expiry:
                    old_date = datetime.fromisoformat(old_expiry)
                    if old_date > now:
                        new_expiry = (old_date + timedelta(days=expires_days)).isoformat()
                    else:
                        new_expiry = (now + timedelta(days=expires_days)).isoformat()
                else:
                    new_expiry = (now + timedelta(days=expires_days)).isoformat()
            # Update
            update_data = {'expires_at': new_expiry}
            if not is_trial and lic.get('is_trial', False):
                update_data['is_trial'] = False
            supabase.table('licenses').update(update_data).eq('license_key', license_key).execute()
            return license_key, new_expiry
        else:
            # Create new license
            license_key = secrets.token_hex(16).upper()
            expires_at = None if expires_days is None else (now + timedelta(days=expires_days)).isoformat()
            new_license = {
                "license_key": license_key,
                "telegram_chat_id": str(telegram_chat_id),
                "bound_account": None,
                "activated_at": now.isoformat(),
                "expires_at": expires_at,
                "is_master": False,
                "is_active": True,
                "is_trial": is_trial
            }
            supabase.table('licenses').insert(new_license).execute()
            return license_key, expires_at
    except Exception as e:
        logger.error(f"create_or_extend_license error: {e}")
        return None, None

# ========== Telegram webhook endpoint ==========
@bot_bp.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    if not TELEGRAM_BOT_TOKEN:
        return "Bot token not set", 500
    update = request.get_json()
    if not update:
        return "No update", 400
    logger.info(f"Telegram update: {json.dumps(update)}")

    lang = "en"
    if "message" in update and "from" in update["message"]:
        lang = update["message"]["from"].get("language_code", "en")
        if lang not in ("en", "fr"):
            lang = "en"
    elif "callback_query" in update and "from" in update["callback_query"]:
        lang = update["callback_query"]["from"].get("language_code", "en")
        if lang not in ("en", "fr"):
            lang = "en"

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "").lower()
        if text == "/start":
            send_telegram(chat_id, MESSAGES["welcome"][lang], reply_markup=get_main_menu_markup(chat_id, lang))
        elif text == "/buy":
            send_telegram(chat_id, MESSAGES["choose_plan"][lang], reply_markup=plan_selection_markup(lang))
        elif text == "/trial":
            handle_trial(chat_id, lang)
        elif text == "/status":
            check_license_status(chat_id, lang)
        elif text == "/unbind":
            unbind_license(chat_id, lang)
        elif text == "/help":
            send_telegram(chat_id, MESSAGES["help"][lang], reply_markup=get_main_menu_markup(chat_id, lang))
        elif text == "/report":
            show_report_menu(chat_id, lang)
        elif text == "/about":
            send_telegram(chat_id, MESSAGES["about"][lang])
        elif text == "/resume":
            handle_resume(chat_id, lang)
        elif text == "/settings":
            show_settings_menu(chat_id, lang)
        elif text.startswith("/sethour"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    hour = int(parts[1])
                    if 0 <= hour <= 23:
                        db.set_user_settings(chat_id, report_hour=hour)
                        send_telegram(chat_id, MESSAGES["set_hour_success"][lang].format(hour=hour))
                    else:
                        send_telegram(chat_id, MESSAGES["set_hour_invalid"][lang])
                except:
                    send_telegram(chat_id, MESSAGES["set_hour_invalid"][lang])
            else:
                send_telegram(chat_id, MESSAGES["set_hour_prompt"][lang])
        else:
            send_telegram(chat_id, MESSAGES["main_menu"][lang], reply_markup=get_main_menu_markup(chat_id, lang))
    elif "callback_query" in update:
        query = update["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        data = query["data"]
        if data == "menu_buy":
            send_telegram(chat_id, MESSAGES["choose_plan"][lang], reply_markup=plan_selection_markup(lang))
        elif data == "menu_trial":
            handle_trial(chat_id, lang)
        elif data == "menu_status":
            check_license_status(chat_id, lang)
        elif data == "menu_unbind":
            unbind_license(chat_id, lang)
        elif data.startswith("plan_"):
            plan_id = data.split("_")[1]
            create_payment_invoice(chat_id, plan_id, lang)
        elif data.startswith("report_"):
            period = data.replace("report_", "")
            handle_report(chat_id, period, lang)
        elif data == "settings_auto_toggle":
            toggle_auto_report(chat_id, lang)
        elif data == "settings_freq_daily":
            db.set_user_settings(chat_id, report_frequency='daily')
            show_settings_menu(chat_id, lang)
        elif data == "settings_freq_weekly":
            db.set_user_settings(chat_id, report_frequency='weekly')
            show_settings_menu(chat_id, lang)
        elif data == "settings_freq_monthly":
            db.set_user_settings(chat_id, report_frequency='monthly')
            show_settings_menu(chat_id, lang)
        elif data == "settings_hour":
            send_telegram(chat_id, MESSAGES["set_hour_prompt"][lang])
        else:
            send_telegram(chat_id, MESSAGES["invalid_option"][lang])
        answer_callback(query["id"])
    return "OK", 200