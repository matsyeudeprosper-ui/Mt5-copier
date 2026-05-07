import os
import json
import sqlite3
import logging
import secrets
import hmac
import hashlib
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template

# ==================== LOAD CONFIGURATION ====================
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

PLANS = CONFIG["plans"]
MESSAGES = CONFIG["messages"]

# Environment variables
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MASTER_KEY = os.environ.get("MASTER_KEY", "YourMasterKeyHere123!")
ADMIN_STARTING_BALANCE = float(os.environ.get("ADMIN_STARTING_BALANCE", "0"))

DB_PATH = "/tmp/trades.db"
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== DATABASE INIT ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT, magic TEXT, ticket TEXT, symbol TEXT, type TEXT,
        volume REAL, open_price REAL, sl REAL, tp REAL, close_profit REAL,
        comment TEXT, timestamp TEXT, received_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
        license_key TEXT PRIMARY KEY,
        telegram_chat_id TEXT,
        bound_account TEXT,
        activated_at TEXT,
        expires_at TEXT,
        is_master INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        is_trial INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS license_activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT,
        mt5_account TEXT,
        action TEXT,
        created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        plan TEXT,
        license_key TEXT,
        telegram_chat_id TEXT,
        status TEXT,
        created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        telegram_chat_id TEXT PRIMARY KEY,
        starting_balance REAL,
        base_currency TEXT DEFAULT 'USD',
        deposits TEXT DEFAULT '[]',
        pip_value REAL,
        currency TEXT,
        hard_stop_hit INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0,
        auto_report_enabled INTEGER DEFAULT 0,
        report_hour INTEGER DEFAULT 20,
        report_frequency TEXT DEFAULT 'daily'
    )''')

    # Master key sync
    c.execute("SELECT license_key FROM licenses WHERE is_master = 1")
    existing = c.fetchone()
    if existing:
        if existing[0] != MASTER_KEY:
            c.execute("DELETE FROM licenses WHERE is_master = 1")
            c.execute("INSERT INTO licenses (license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (MASTER_KEY, "master", None, datetime.now().isoformat(), None, 1, 1, 0))
            logger.info(f"Master key updated to {MASTER_KEY}")
    else:
        c.execute("INSERT INTO licenses (license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (MASTER_KEY, "master", None, datetime.now().isoformat(), None, 1, 1, 0))

    conn.commit()
    conn.close()
    logger.info("Database ready")

init_db()

# ==================== HELPER FUNCTIONS ====================
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

def generate_license_key():
    return secrets.token_hex(16).upper()

def activate_license(telegram_chat_id, expires_days, is_trial=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, expires_at, is_trial FROM licenses WHERE telegram_chat_id = ? AND is_master = 0 ORDER BY expires_at DESC LIMIT 1", (telegram_chat_id,))
    row = c.fetchone()
    now = datetime.now()
    if row:
        license_key = row[0]
        old_expiry = row[1]
        old_is_trial = row[2]
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
                new_expiry = None
        if is_trial == False and old_is_trial == 1:
            c.execute("UPDATE licenses SET expires_at = ?, is_trial = 0 WHERE license_key = ?", (new_expiry, license_key))
        else:
            c.execute("UPDATE licenses SET expires_at = ? WHERE license_key = ?", (new_expiry, license_key))
        conn.commit()
        conn.close()
        return license_key, new_expiry
    else:
        license_key = generate_license_key()
        activated_at = now.isoformat()
        expires_at = None if expires_days is None else (now + timedelta(days=expires_days)).isoformat()
        c.execute('''INSERT INTO licenses (license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial)
                     VALUES (?, ?, ?, ?, ?, 0, 1, ?)''', (license_key, telegram_chat_id, None, activated_at, expires_at, 1 if is_trial else 0))
        conn.commit()
        conn.close()
        return license_key, expires_at

def is_license_valid(license_key, require_master=False, mt5_account=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if require_master:
        c.execute("SELECT expires_at, is_active, is_master FROM licenses WHERE license_key = ?", (license_key,))
        row = c.fetchone()
        conn.close()
        if not row:
            return False
        expires_at, is_active, is_master = row
        if not is_active or is_master != 1:
            return False
        if expires_at and datetime.now() > datetime.fromisoformat(expires_at):
            return False
        return True
    else:
        c.execute("SELECT expires_at, is_active, bound_account, is_trial FROM licenses WHERE license_key = ? AND is_master = 0", (license_key,))
        row = c.fetchone()
        if not row:
            conn.close()
            return False
        expires_at, is_active, bound_account, is_trial = row
        if not is_active:
            conn.close()
            return False
        if expires_at and datetime.now() > datetime.fromisoformat(expires_at):
            conn.close()
            return False
        if mt5_account is not None:
            if bound_account is None:
                c.execute("UPDATE licenses SET bound_account = ? WHERE license_key = ?", (mt5_account, license_key))
                c.execute("INSERT INTO license_activations (license_key, mt5_account, action, created_at) VALUES (?, ?, ?, ?)",
                          (license_key, mt5_account, "bind", datetime.now().isoformat()))
                conn.commit()
                conn.close()
                return True
            elif bound_account == mt5_account:
                conn.close()
                return True
            else:
                conn.close()
                return False
        conn.close()
        return True

# ==================== TRADE COPYING ENDPOINTS ====================
@app.route("/copier", methods=["POST"])
def receive_trade():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not is_license_valid(license_key, require_master=True):
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    action = data.get("action")
    if action not in ("open", "close", "modify"):
        return jsonify({"error": "Invalid action"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO trades (action, magic, ticket, symbol, type, volume, open_price, sl, tp, close_profit, comment, timestamp, received_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (action, str(data.get("magic","")), str(data.get("ticket","")), data.get("symbol",""),
               data.get("type",""), data.get("volume"), data.get("open_price"), data.get("sl"), data.get("tp"),
               data.get("close_profit"), data.get("comment",""), data.get("timestamp",""), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    logger.info(f"Trade stored from master key")
    return jsonify({"status": "ok"}), 200

@app.route("/trades", methods=["GET"])
def get_trades():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not is_license_valid(license_key, require_master=False):
        return jsonify({"error": "Invalid or expired license"}), 401

    since_id = request.args.get("since_id", default=0, type=int)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, action, magic, ticket, symbol, type, volume, open_price, sl, tp, close_profit, comment, timestamp, received_at FROM trades WHERE id > ? ORDER BY id ASC", (since_id,))
    rows = c.fetchall()
    conn.close()
    cols = ["id","action","magic","ticket","symbol","type","volume","open_price","sl","tp","close_profit","comment","timestamp","received_at"]
    trades = [dict(zip(cols, row)) for row in rows]
    return jsonify({"since_id": since_id, "trades": trades}), 200

@app.route("/export", methods=["GET"])
def export_csv():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not (is_license_valid(license_key, require_master=True) or is_license_valid(license_key, require_master=False)):
        return "Unauthorized", 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "No trades", 404
    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","action","magic","ticket","symbol","type","volume","open_price","sl","tp","close_profit","comment","timestamp","received_at"])
    writer.writerows(rows)
    return output.getvalue(), 200, {"Content-Type":"text/csv","Content-Disposition":"attachment;filename=trades.csv"}

# ==================== USER SETTINGS ====================
def get_user_settings(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT starting_balance, base_currency, deposits, pip_value, currency, hard_stop_hit, trial_used, auto_report_enabled, report_hour, report_frequency FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    if row:
        start, currency, deposits_json, pip_value, pip_currency, hard_stop_hit, trial_used, auto_report, hour, freq = row
        deposits = json.loads(deposits_json) if deposits_json else []
        total_deposits = sum(deposits) if deposits else 0
        effective_start = start + total_deposits if start else 0
        return {
            "starting_balance": start,
            "base_currency": currency,
            "deposits": deposits,
            "effective_start": effective_start,
            "pip_value": pip_value,
            "pip_currency": pip_currency,
            "hard_stop_hit": bool(hard_stop_hit),
            "trial_used": bool(trial_used),
            "auto_report_enabled": bool(auto_report),
            "report_hour": hour,
            "report_frequency": freq
        }
    return {
        "starting_balance": None,
        "base_currency": "USD",
        "deposits": [],
        "effective_start": 0,
        "pip_value": None,
        "pip_currency": None,
        "hard_stop_hit": False,
        "trial_used": False,
        "auto_report_enabled": False,
        "report_hour": 20,
        "report_frequency": "daily"
    }

def set_user_settings(chat_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    exists = c.fetchone()
    if exists:
        updates = []
        params = []
        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            params.append(value)
        params.append(str(chat_id))
        c.execute(f"UPDATE user_settings SET {', '.join(updates)} WHERE telegram_chat_id = ?", params)
    else:
        # Insert with defaults
        cmd = "INSERT INTO user_settings (telegram_chat_id, starting_balance, base_currency, deposits, pip_value, currency, hard_stop_hit, trial_used, auto_report_enabled, report_hour, report_frequency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        c.execute(cmd, (str(chat_id), kwargs.get('starting_balance', 0), kwargs.get('base_currency', 'USD'), '[]', kwargs.get('pip_value'), kwargs.get('pip_currency'), kwargs.get('hard_stop_hit', 0), kwargs.get('trial_used', 0), kwargs.get('auto_report_enabled', 0), kwargs.get('report_hour', 20), kwargs.get('report_frequency', 'daily')))
    conn.commit()
    conn.close()

def set_pip_calibration(chat_id, pip_value, currency):
    set_user_settings(chat_id, pip_value=pip_value, pip_currency=currency)

def add_deposit(chat_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT deposits FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    deposits = json.loads(row[0]) if row and row[0] else []
    deposits.append(amount)
    c.execute("UPDATE user_settings SET deposits = ? WHERE telegram_chat_id = ?", (json.dumps(deposits), str(chat_id)))
    conn.commit()
    conn.close()

def set_hard_stop(chat_id, hard_stop_hit):
    set_user_settings(chat_id, hard_stop_hit=1 if hard_stop_hit else 0)

def mark_trial_used(chat_id):
    set_user_settings(chat_id, trial_used=1)

# ==================== CALIBRATION & HARD STOP ENDPOINTS ====================
@app.route("/calibrate_pip", methods=["POST"])
def calibrate_pip():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    license_key = data.get("license_key")
    master_ticket = data.get("master_ticket")
    closed_pips = data.get("closed_pips")
    real_profit = data.get("real_profit")
    currency = data.get("currency", "USD").upper()

    if not all([license_key, master_ticket, closed_pips, real_profit]):
        return jsonify({"error": "Missing required fields"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_chat_id FROM licenses WHERE license_key = ? AND is_master = 0", (license_key,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Invalid license key"}), 401
    chat_id = row[0]

    settings = get_user_settings(chat_id)
    if settings["pip_value"] is not None:
        conn.close()
        return jsonify({"error": "Calibration already exists"}), 400

    c.execute("SELECT close_profit FROM trades WHERE action = 'close' AND ticket = ?", (str(master_ticket),))
    trade_row = c.fetchone()
    conn.close()

    if not trade_row:
        msg = MESSAGES["calibration_no_trade"]["en"].format(ticket=master_ticket)
        return jsonify({"error": msg}), 404

    master_pips = trade_row[0]
    if master_pips == 0:
        return jsonify({"error": "Master trade pip difference is zero"}), 400

    pip_value = real_profit / closed_pips
    if pip_value <= 0:
        return jsonify({"error": "Invalid pip value"}), 400

    set_pip_calibration(chat_id, pip_value, currency)
    msg = MESSAGES["calibration_success"]["en"].format(currency=currency, pip_value=pip_value)
    send_telegram(chat_id, msg)
    return jsonify({"success": True, "pip_value": pip_value, "currency": currency}), 200

@app.route("/hard_stop", methods=["POST"])
def report_hard_stop():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    license_key = data.get("license_key")
    if not license_key:
        return jsonify({"error": "Missing license_key"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_chat_id FROM licenses WHERE license_key = ? AND is_master = 0", (license_key,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Invalid license key"}), 401
    chat_id = row[0]
    set_hard_stop(chat_id, True)
    conn.close()
    send_telegram(chat_id, "⚠️ Your EA has hit the hard stop. Trading is paused. Use /resume to continue.")
    return jsonify({"success": True}), 200

# ==================== REPORTING ====================
import reporting

def format_report_text(stats, period_key, currency="USD", start_balance=None, lang="en", pip_value=None, pip_currency=None):
    if not stats:
        return MESSAGES["report_no_trades"][lang]
    msg = MESSAGES[f"report_{period_key}"][lang] + "\n\n"
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
    return msg

def handle_report(chat_id, period, lang):
    purchase_date = reporting.get_license_purchase_date(str(chat_id))
    if not purchase_date:
        send_telegram(chat_id, "You don't have an active license.")
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

    settings = get_user_settings(chat_id)
    pip_value = settings["pip_value"]
    pip_currency = settings["pip_currency"] or "USD"
    stats = reporting.calculate_stats(trades, pip_value=pip_value)
    start_balance = settings["effective_start"] if settings["effective_start"] > 0 else None
    msg = format_report_text(stats, period, pip_currency, start_balance, lang, pip_value, pip_currency)
    send_telegram(chat_id, msg)

def handle_admin_report(chat_id, lang):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        send_telegram(chat_id, "Unauthorized.")
        return
    if ADMIN_STARTING_BALANCE <= 0:
        send_telegram(chat_id, MESSAGES["admin_report_not_set"][lang])
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE action = 'close' ORDER BY timestamp ASC")
    rows = c.fetchall()
    conn.close()
    stats = reporting.calculate_stats(rows, pip_value=None)
    msg = format_report_text(stats, "admin", "USD", ADMIN_STARTING_BALANCE, lang, pip_value=None, pip_currency="pips")
    send_telegram(chat_id, msg)

# ==================== TELEGRAM BOT ====================
def get_main_menu_markup(chat_id, lang):
    settings = get_user_settings(chat_id)
    has_license = False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key FROM licenses WHERE telegram_chat_id = ? AND is_master = 0", (str(chat_id),))
    if c.fetchone():
        has_license = True
    conn.close()
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

@app.route("/webhook/telegram", methods=["POST"])
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
        elif text == "/adminreport":
            handle_admin_report(chat_id, lang)
        elif text == "/settings":
            show_settings_menu(chat_id, lang)
        elif text.startswith("/sethour"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    hour = int(parts[1])
                    if 0 <= hour <= 23:
                        set_user_settings(chat_id, report_hour=hour)
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
            set_user_settings(chat_id, report_frequency='daily')
            show_settings_menu(chat_id, lang)
        elif data == "settings_freq_weekly":
            set_user_settings(chat_id, report_frequency='weekly')
            show_settings_menu(chat_id, lang)
        elif data == "settings_freq_monthly":
            set_user_settings(chat_id, report_frequency='monthly')
            show_settings_menu(chat_id, lang)
        elif data == "settings_hour":
            send_telegram(chat_id, MESSAGES["set_hour_prompt"][lang])
        else:
            send_telegram(chat_id, MESSAGES["invalid_option"][lang])
        answer_callback(query["id"])
    return "OK", 200

def show_settings_menu(chat_id, lang):
    settings = get_user_settings(chat_id)
    auto_status = "ON" if settings["auto_report_enabled"] else "OFF"
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
    settings = get_user_settings(chat_id)
    new_state = not settings["auto_report_enabled"]
    set_user_settings(chat_id, auto_report_enabled=1 if new_state else 0)
    if new_state:
        send_telegram(chat_id, MESSAGES["auto_report_notify_on"][lang].format(hour=settings["report_hour"]))
    else:
        send_telegram(chat_id, MESSAGES["auto_report_notify_off"][lang])
    show_settings_menu(chat_id, lang)

def check_license_status(chat_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, bound_account, expires_at, is_trial FROM licenses WHERE telegram_chat_id = ? AND is_master = 0", (str(chat_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    license_key, bound_account, expires_at, is_trial = row
    expiry_str = expires_at if expires_at else MESSAGES["never"][lang]
    bound_str = bound_account if bound_account else MESSAGES["not_bound"][lang]
    trial_str = " (Trial)" if is_trial else ""
    settings = get_user_settings(chat_id)
    hard_stop_str = "Yes" if settings["hard_stop_hit"] else "No"
    msg = MESSAGES["license_status"][lang].format(key=license_key, bound=bound_str, expires=expiry_str, hard_stop=hard_stop_str) + trial_str
    send_telegram(chat_id, msg)
    conn.close()

def unbind_license(chat_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, bound_account FROM licenses WHERE telegram_chat_id = ? AND is_master = 0", (str(chat_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        send_telegram(chat_id, MESSAGES["no_license"][lang])
        return
    license_key, bound_account = row
    if bound_account is None:
        send_telegram(chat_id, MESSAGES["unbind_none"][lang])
        conn.close()
        return
    c.execute("UPDATE licenses SET bound_account = NULL WHERE license_key = ?", (license_key,))
    c.execute("INSERT INTO license_activations (license_key, mt5_account, action, created_at) VALUES (?, ?, ?, ?)",
              (license_key, bound_account, "unbind", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    send_telegram(chat_id, MESSAGES["unbind_success"][lang].format(account=bound_account))
    admin_msg = MESSAGES["admin_unbind"][lang].format(user=chat_id, key=license_key, account=bound_account)
    notify_admin(admin_msg)

def handle_trial(chat_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key FROM licenses WHERE telegram_chat_id = ? AND is_master = 0", (str(chat_id),))
    if c.fetchone():
        conn.close()
        send_telegram(chat_id, MESSAGES["trial_already_exists"][lang])
        return
    conn.close()
    settings = get_user_settings(chat_id)
    if settings["trial_used"]:
        send_telegram(chat_id, MESSAGES["trial_not_available"][lang])
        return
    trial_days = 7
    license_key, expires_at = activate_license(str(chat_id), trial_days, is_trial=True)
    expiry_str = expires_at if expires_at else "7 days"
    mark_trial_used(chat_id)
    msg = MESSAGES["trial_success"][lang].format(key=license_key, expires=expiry_str)
    send_telegram(chat_id, msg)
    notify_admin(f"🧪 New trial license\nUser: {chat_id}\nKey: {license_key}\nExpires: {expiry_str}")

def handle_resume(chat_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key FROM licenses WHERE telegram_chat_id = ? AND is_master = 0", (str(chat_id),))
    if not c.fetchone():
        conn.close()
        send_telegram(chat_id, MESSAGES["resume_not_needed"][lang])
        return
    conn.close()
    settings = get_user_settings(chat_id)
    if not settings["hard_stop_hit"]:
        send_telegram(chat_id, MESSAGES["no_hard_stop"][lang])
        return
    set_hard_stop(chat_id, False)
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

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO orders (order_id, plan, license_key, telegram_chat_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (order_id, plan_id, "", str(chat_id), "pending", datetime.now().isoformat()))
        conn.commit()
        conn.close()
        send_telegram(chat_id, MESSAGES["payment_link"][lang].format(url=payment_url))
    except Exception as e:
        logger.error(f"NowPayments error: {str(e)}")
        send_telegram(chat_id, MESSAGES["payment_error"][lang])

# ==================== NOWPAYMENTS WEBHOOK ====================
@app.route("/webhook/nowpayments", methods=["POST"])
def nowpayments_webhook():
    signature = request.headers.get("x-nowpayments-sig")
    if not signature or not NOWPAYMENTS_IPN_SECRET:
        logger.warning("Missing signature or IPN secret")
        return "Signature missing", 401

    payload_raw = request.get_data()
    logger.info(f"NowPayments webhook raw payload: {payload_raw.decode('utf-8')}")

    try:
        data = request.get_json()
        def sort_dict(d):
            return {k: sort_dict(v) if isinstance(v, dict) else v for k, v in sorted(d.items())}
        sorted_data = sort_dict(data)
        sorted_json = json.dumps(sorted_data, separators=(',', ':'))
        computed = hmac.new(NOWPAYMENTS_IPN_SECRET.encode(), sorted_json.encode(), hashlib.sha512).hexdigest()
        if not hmac.compare_digest(computed, signature):
            logger.error(f"HMAC mismatch. Computed: {computed}, Received: {signature}")
            return "Invalid signature", 401
    except Exception as e:
        logger.error(f"Signature verification error: {e}")
        return "Invalid signature", 401

    payment_status = data.get("payment_status")
    order_id = data.get("order_id")
    logger.info(f"Webhook: payment_status={payment_status}, order_id={order_id}")

    if payment_status == "finished" and order_id:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT plan, telegram_chat_id FROM orders WHERE order_id = ?", (order_id,))
        row = c.fetchone()
        if row:
            plan_id, chat_id = row
            days = PLANS[plan_id]["days"]
            license_key, expires_at = activate_license(str(chat_id), days, is_trial=False)
            c.execute("UPDATE orders SET license_key = ?, status = 'completed' WHERE order_id = ?", (license_key, order_id))
            conn.commit()
            expiry_str = expires_at if expires_at else MESSAGES["never"]["en"]
            plan_name = PLANS[plan_id]["name"]["en"]
            msg = MESSAGES["payment_confirmed"]["en"].format(key=license_key, plan=plan_name, expires=expiry_str)
            send_telegram(chat_id, msg)
            admin_msg = MESSAGES["admin_sale"]["en"].format(user=chat_id, plan=plan_name, key=license_key, expires=expiry_str)
            notify_admin(admin_msg)
        else:
            logger.warning(f"Order not found: {order_id}")
        conn.close()
    return jsonify({"status": "ok"}), 200

# ==================== OTHER ENDPOINTS ====================
@app.route("/buy", methods=["GET"])
def payment_page():
    return render_template("buy.html") if os.path.exists("templates/buy.html") else "<h1>Use Telegram bot to purchase</h1>"

@app.route("/payment_success", methods=["GET"])
def payment_success():
    return "<h1>Payment successful! Your license key will be sent via Telegram.</h1>"

@app.route("/payment_cancel", methods=["GET"])
def payment_cancel():
    return "<h1>Payment cancelled. No license issued.</h1>"

@app.route("/validate", methods=["POST"])
def validate_license():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    license_key = data.get("license_key")
    mt5_account = str(data.get("mt5_account", ""))
    if not license_key:
        return jsonify({"allowed": False, "message": "License key missing"}), 200
    if not mt5_account:
        return jsonify({"allowed": False, "message": "MT5 account missing"}), 200

    valid = is_license_valid(license_key, require_master=False, mt5_account=mt5_account)
    if not valid:
        return jsonify({"allowed": False, "message": "Invalid or expired license, or wrong MT5 account (use Unbind button on Telegram to reset)"}), 200

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_chat_id FROM licenses WHERE license_key = ?", (license_key,))
    row = c.fetchone()
    hard_stop_hit = False
    if row:
        settings = get_user_settings(row[0])
        hard_stop_hit = settings["hard_stop_hit"]
    c.execute("SELECT expires_at FROM licenses WHERE license_key = ?", (license_key,))
    row2 = c.fetchone()
    expires_at = row2[0] if row2 else None
    conn.close()
    return jsonify({"allowed": True, "expires_at": expires_at, "hard_stop_hit": hard_stop_hit, "message": "Valid"}), 200

# ==================== TEST DASHBOARD & ADMIN ====================
@app.route("/test", methods=["GET"])
def test_dashboard():
    return render_template("test.html") if os.path.exists("templates/test.html") else "<h1>Test dashboard available</h1><p>Add templates/test.html</p>"

@app.route("/licenses", methods=["GET"])
def list_licenses():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial FROM licenses")
    rows = c.fetchall()
    conn.close()
    licenses = []
    for row in rows:
        licenses.append({
            "license_key": row[0],
            "telegram_chat_id": row[1],
            "bound_account": row[2],
            "activated_at": row[3],
            "expires_at": row[4],
            "is_master": bool(row[5]),
            "is_active": bool(row[6]),
            "is_trial": bool(row[7])
        })
    return jsonify(licenses), 200

@app.route("/test_activate_license", methods=["POST"])
def test_activate_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    telegram_chat_id = data.get("telegram_chat_id")
    plan_id = data.get("plan")
    if not telegram_chat_id or not plan_id:
        return jsonify({"error": "telegram_chat_id and plan required"}), 400
    if plan_id not in PLANS:
        return jsonify({"error": "Invalid plan"}), 400
    days = PLANS[plan_id]["days"]
    license_key, expires_at = activate_license(str(telegram_chat_id), days, is_trial=False)
    expiry_str = expires_at if expires_at else "Never"
    plan_name = PLANS[plan_id]["name"]["en"]
    msg = MESSAGES["payment_confirmed"]["en"].format(key=license_key, plan=plan_name, expires=expiry_str)
    send_telegram(telegram_chat_id, msg)
    notify_admin(f"🧪 Test license activated (no payment)\nUser: {telegram_chat_id}\nPlan: {plan_id}\nKey: {license_key}\nExpires: {expiry_str}")
    return jsonify({"success": True, "license_key": license_key, "expires_at": expires_at}), 200

@app.route("/activate_license", methods=["POST"])
def admin_activate_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    expires_days = data.get("expires_days")
    telegram_chat_id = data.get("telegram_chat_id")
    if not telegram_chat_id:
        return jsonify({"error": "telegram_chat_id required"}), 400
    license_key, expires_at = activate_license(telegram_chat_id, expires_days, is_trial=False)
    return jsonify({"license_key": license_key, "expires_at": expires_at}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "alive"})

# ==================== START SCHEDULER ====================
from scheduler import start_scheduler
scheduler = start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)