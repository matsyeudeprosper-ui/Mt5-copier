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

# ==================== CONFIGURATION ====================
SECRET_KEY = os.environ.get("COP_SECRET_KEY", "change_me_trade")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = "/tmp/trades.db"
app = Flask(__name__)
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
        email TEXT,
        bound_mt5_account TEXT,
        activated_at TEXT,
        expires_at TEXT,
        max_accounts INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS license_activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT,
        mt5_account TEXT,
        last_validated TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        email TEXT,
        plan TEXT,
        license_key TEXT,
        status TEXT,
        created_at TEXT)''')
    conn.commit()
    conn.close()
    logger.info("Database ready")

init_db()

# ==================== HELPERS ====================
def generate_license_key():
    return secrets.token_hex(16).upper()

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def activate_license_internal(email, expires_days, max_accounts=1):
    license_key = generate_license_key()
    activated_at = datetime.now().isoformat()
    expires_at = None
    if expires_days:
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO licenses (license_key, email, bound_mt5_account, activated_at, expires_at, max_accounts, is_active)
                 VALUES (?, ?, ?, ?, ?, ?, 1)''',
              (license_key, email, None, activated_at, expires_at, max_accounts))
    conn.commit()
    conn.close()
    return license_key, expires_at

# ==================== TRADE ENDPOINTS ====================
def is_trade_auth():
    return request.headers.get("X-Auth-Token") == SECRET_KEY

def save_trade(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO trades (action, magic, ticket, symbol, type, volume, open_price, sl, tp, close_profit, comment, timestamp, received_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (data.get("action"), str(data.get("magic","")), str(data.get("ticket","")), data.get("symbol",""),
               data.get("type",""), data.get("volume"), data.get("open_price"), data.get("sl"), data.get("tp"),
               data.get("close_profit"), data.get("comment",""), data.get("timestamp",""), datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_trades_since(since_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE id > ? ORDER BY id ASC", (since_id,))
    rows = c.fetchall()
    conn.close()
    cols = ["id","action","magic","ticket","symbol","type","volume","open_price","sl","tp","close_profit","comment","timestamp","received_at"]
    return [dict(zip(cols, row)) for row in rows]

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "alive"})

@app.route("/copier", methods=["POST"])
def receive_trade():
    if not is_trade_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    if data.get("action") not in ("open","close","modify"):
        return jsonify({"error": "Invalid action"}), 400
    save_trade(data)
    return jsonify({"status": "ok"}), 200

@app.route("/trades", methods=["GET"])
def get_trades():
    if not is_trade_auth():
        return jsonify({"error": "Unauthorized"}), 401
    since_id = request.args.get("since_id", 0, int)
    return jsonify({"since_id": since_id, "trades": get_trades_since(since_id)})

@app.route("/export", methods=["GET"])
def export_csv():
    if not is_trade_auth():
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
    return output.getvalue(), 200, {"Content-Type":"text/csv", "Content-Disposition":"attachment;filename=trades.csv"}

# ==================== LICENSE VALIDATION (EA) ====================
@app.route("/validate", methods=["POST"])
def validate_license():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    license_key = data.get("license_key")
    mt5_account = str(data.get("mt5_account", ""))
    if not license_key:
        return jsonify({"allowed": False, "message": "License key missing"}), 200
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,))
    lic = c.fetchone()
    if not lic:
        conn.close()
        return jsonify({"allowed": False, "message": "Invalid license"}), 200
    bound_account = lic[2]
    expires_at = lic[4]
    is_active = lic[6]
    now = datetime.now()
    if not is_active:
        conn.close()
        return jsonify({"allowed": False, "message": "License deactivated"}), 200
    if expires_at and datetime.fromisoformat(expires_at) < now:
        conn.close()
        return jsonify({"allowed": False, "message": "License expired"}), 200
    if bound_account is None:
        c.execute("UPDATE licenses SET bound_mt5_account = ? WHERE license_key = ?", (mt5_account, license_key))
        conn.commit()
    elif bound_account != mt5_account:
        conn.close()
        return jsonify({"allowed": False, "message": "License used on another MT5 account"}), 200
    c.execute("INSERT INTO license_activations (license_key, mt5_account, last_validated) VALUES (?, ?, ?)",
              (license_key, mt5_account, now.isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"allowed": True, "expires_at": expires_at, "message": "Valid"}), 200

# ==================== PAYMENT & LICENSE AUTOMATION ====================
@app.route("/create_order", methods=["POST"])
def create_order():
    data = request.get_json()
    email = data.get("email")
    plan = data.get("plan")
    if not email or not plan:
        return jsonify({"error": "Email and plan required"}), 400
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
        c.execute("INSERT INTO orders (order_id, email, plan, license_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (order_id, email, plan, "", "pending", datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"order_id": order_id, "payment_url": inv["invoice_url"]})
    except Exception as e:
        logger.error(f"NowPayments error: {e}")
        return jsonify({"error": "Payment gateway error"}), 500

@app.route("/payment_success", methods=["GET"])
def payment_success():
    order_id = request.args.get("order_id")
    if not order_id:
        return "Missing order_id", 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, email, plan, status FROM orders WHERE order_id = ?", (order_id,))
    row = c.fetchone()
    if not row:
        return "Order not found", 404
    license_key, email, plan, status = row
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
        c.execute("SELECT email, plan FROM orders WHERE order_id = ?", (order_id,))
        row = c.fetchone()
        if row:
            email, plan = row
            days_map = {"1month": 30, "1year": 365, "lifetime": None}
            days = days_map.get(plan, 30)
            license_key, expires_at = activate_license_internal(email, days)
            c.execute("UPDATE orders SET license_key = ?, status = 'completed' WHERE order_id = ?", (license_key, order_id))
            conn.commit()
            expiry_str = expires_at if expires_at else "Never"
            msg = f"🎉 <b>New License Sold!</b>\n\nEmail: {email}\nPlan: {plan}\nLicense Key: <code>{license_key}</code>\nExpires: {expiry_str}"
            send_telegram(msg)
            logger.info(f"License {license_key} activated for {email}")
        conn.close()
    return jsonify({"status": "ok"}), 200

@app.route("/buy", methods=["GET"])
def payment_page():
    return render_template("buy.html")

# ==================== ADMIN BACKUP ====================
@app.route("/activate_license", methods=["POST"])
def admin_activate_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    email = data.get("email")
    expires_days = data.get("expires_days")
    max_accounts = data.get("max_accounts", 1)
    if not email:
        return jsonify({"error": "Email required"}), 400
    license_key, expires_at = activate_license_internal(email, expires_days, max_accounts)
    return jsonify({"license_key": license_key, "expires_at": expires_at}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)