import os
import json
import sqlite3
import logging
import secrets
import hmac
import hashlib
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, session

# ==================== CONFIGURATION ====================
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # Admin chat ID
MASTER_KEY = os.environ.get("MASTER_KEY", "YourMasterKeyHere123!")

DB_PATH = "/tmp/trades.db"
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # for session
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Temporary storage for verification codes (in memory, lost on restart)
telegram_verifications = {}  # chat_id -> {"code": "1234", "expires": datetime}

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
        email TEXT,
        activated_at TEXT,
        expires_at TEXT,
        is_master INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        plan TEXT,
        license_key TEXT,
        telegram_chat_id TEXT,
        status TEXT,
        created_at TEXT)''')
    # Insert master key if not exists
    c.execute("SELECT * FROM licenses WHERE license_key = ?", (MASTER_KEY,))
    if not c.fetchone():
        c.execute("INSERT INTO licenses (license_key, email, activated_at, expires_at, is_master, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                  (MASTER_KEY, "master@system", datetime.now().isoformat(), None, 1, 1))
    conn.commit()
    conn.close()
    logger.info("Database ready")

init_db()

# ==================== HELPERS ====================
def generate_license_key():
    return secrets.token_hex(16).upper()

def send_telegram(chat_id, message):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False

def notify_admin(message):
    if TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN:
        send_telegram(TELEGRAM_CHAT_ID, message)

def activate_license_internal(telegram_chat_id, expires_days):
    license_key = generate_license_key()
    activated_at = datetime.now().isoformat()
    expires_at = None if expires_days is None else (datetime.now() + timedelta(days=expires_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    email = f"telegram_{telegram_chat_id}" if telegram_chat_id else f"anon_{secrets.token_hex(4)}"
    c.execute('''INSERT INTO licenses (license_key, email, activated_at, expires_at, is_master, is_active)
                 VALUES (?, ?, ?, ?, 0, 1)''', (license_key, email, activated_at, expires_at))
    conn.commit()
    conn.close()
    return license_key, expires_at

def is_license_valid(license_key, require_master=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if require_master:
        c.execute("SELECT expires_at, is_active, is_master FROM licenses WHERE license_key = ?", (license_key,))
    else:
        c.execute("SELECT expires_at, is_active FROM licenses WHERE license_key = ? AND is_master = 0", (license_key,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    if require_master:
        expires_at, is_active, is_master = row
        if not is_active or is_master != 1:
            return False
    else:
        expires_at, is_active = row
        if not is_active:
            return False
    if expires_at and datetime.now() > datetime.fromisoformat(expires_at):
        return False
    return True

# ==================== SENDER ENDPOINT ====================
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

# ==================== RECEIVER ENDPOINT ====================
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

# ==================== CSV EXPORT ====================
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

# ==================== TELEGRAM VERIFICATION ====================
@app.route("/verify_telegram", methods=["POST"])
def verify_telegram():
    data = request.get_json()
    chat_id = data.get("chat_id")
    if not chat_id:
        return jsonify({"ok": False, "error": "No chat_id provided"}), 400
    # Generate 4-digit code
    code = str(secrets.randbelow(9000) + 1000)
    expires = datetime.now() + timedelta(minutes=5)
    telegram_verifications[chat_id] = {"code": code, "expires": expires}
    # Send code
    if not send_telegram(chat_id, f"🔐 Your verification code is: <b>{code}</b>\n\nEnter this code on the payment page to confirm your Telegram."):
        return jsonify({"ok": False, "error": "Could not send message. Make sure you have started a chat with the bot and entered the correct Chat ID."}), 400
    return jsonify({"ok": True}), 200

# ==================== PAYMENT & LICENSE CREATION ====================
@app.route("/create_order", methods=["POST"])
def create_order():
    data = request.get_json()
    plan = data.get("plan")
    telegram_chat_id = data.get("telegram_chat_id", "").strip()
    verification_code = data.get("verification_code", "").strip()

    if not plan:
        return jsonify({"error": "Plan required"}), 400
    if not telegram_chat_id:
        return jsonify({"error": "Telegram Chat ID required"}), 400
    if not verification_code:
        return jsonify({"error": "Verification code required"}), 400

    # Verify the code
    stored = telegram_verifications.get(telegram_chat_id)
    if not stored or stored["expires"] < datetime.now():
        return jsonify({"error": "Verification code expired. Please request a new code."}), 400
    if stored["code"] != verification_code:
        return jsonify({"error": "Invalid verification code."}), 400
    # Code valid – remove it to prevent reuse
    del telegram_verifications[telegram_chat_id]

    plan_map = {"1month": 30, "1year": 365, "lifetime": None}
    price_map = {"1month": 29.99, "1year": 99.99, "lifetime": 299.99}
    if plan not in plan_map:
        return jsonify({"error": "Invalid plan"}), 400
    price = price_map[plan]
    days = plan_map[plan]

    np_url = "https://api.nowpayments.io/v1/invoice"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    order_id = secrets.token_hex(8)
    payload = {
        "price_amount": price,
        "price_currency": "USD",
        "pay_currency": "USDT",
        "order_id": order_id,
        "order_description": f"MT5 Copier License - {plan}",
        "ipn_callback_url": "https://mt5-copier-vu1t.onrender.com/webhook/nowpayments",
        "success_url": f"https://mt5-copier-vu1t.onrender.com/payment_success?order_id={order_id}",
        "cancel_url": "https://mt5-copier-vu1t.onrender.com/payment_cancel"
    }
    try:
        resp = requests.post(np_url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        inv = resp.json()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO orders (order_id, plan, license_key, telegram_chat_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (order_id, plan, "", telegram_chat_id, "pending", datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"order_id": order_id, "payment_url": inv["invoice_url"]})
    except Exception as e:
        logger.error(f"NowPayments error: {e}")
        return jsonify({"error": "Payment gateway error"}), 500

@app.route("/payment_success", methods=["GET"])
def payment_success():
    order_id = request.args.get("order_id")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, plan, status FROM orders WHERE order_id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return "Order not found", 404
    license_key, plan, status = row
    if status != "completed":
        return "Payment not yet confirmed. Please wait a few minutes and refresh.", 200
    return render_template("success.html", license_key=license_key)

@app.route("/payment_cancel", methods=["GET"])
def payment_cancel():
    return render_template("cancel.html")

@app.route("/webhook/nowpayments", methods=["POST"])
def nowpayments_webhook():
    signature = request.headers.get("x-nowpayments-sig")
    if not signature or not NOWPAYMENTS_IPN_SECRET:
        return "Signature missing", 401
    payload = request.get_data()
    computed = hmac.new(NOWPAYMENTS_IPN_SECRET.encode(), payload, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(computed, signature):
        return "Invalid signature", 401
    data = request.get_json()
    payment_status = data.get("payment_status")
    order_id = data.get("order_id")
    if payment_status == "finished" and order_id:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT plan, telegram_chat_id FROM orders WHERE order_id = ?", (order_id,))
        row = c.fetchone()
        if row:
            plan, telegram_chat_id = row
            days_map = {"1month": 30, "1year": 365, "lifetime": None}
            days = days_map.get(plan, 30)
            license_key, expires_at = activate_license_internal(telegram_chat_id, days)
            c.execute("UPDATE orders SET license_key = ?, status = 'completed' WHERE order_id = ?", (license_key, order_id))
            conn.commit()
            expiry_str = expires_at if expires_at else "Never"

            # Admin notification
            admin_msg = f"🎉 <b>New License Sold!</b>\n\nPlan: {plan}\nLicense Key: <code>{license_key}</code>\nExpires: {expiry_str}\nTelegram: `{telegram_chat_id}`"
            notify_admin(admin_msg)

            # Send key to customer
            if telegram_chat_id:
                customer_msg = f"✅ <b>MT5 Trade Copier License</b>\n\nYour license key:\n<code>{license_key}</code>\n\nPlan: {plan}\nExpires: {expiry_str}\n\nEnter this key in your EA's <code>CopierSecretKey</code> (or LicenseKey) input."
                send_telegram(telegram_chat_id, customer_msg)
        conn.close()
    return jsonify({"status": "ok"}), 200

@app.route("/buy", methods=["GET"])
def payment_page():
    return render_template("buy.html")

@app.route("/activate_license", methods=["POST"])
def admin_activate_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    expires_days = data.get("expires_days")
    telegram_chat_id = data.get("telegram_chat_id")
    if not telegram_chat_id:
        return jsonify({"error": "telegram_chat_id required"}), 400
    license_key, expires_at = activate_license_internal(telegram_chat_id, expires_days)
    return jsonify({"license_key": license_key, "expires_at": expires_at}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "alive"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)