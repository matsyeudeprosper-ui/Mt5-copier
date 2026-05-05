import os
import json
import sqlite3
import logging
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import io
import csv

# ==================== CONFIGURATION ====================
SECRET_KEY = os.environ.get("COP_SECRET_KEY", "change_me_trade")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
DB_PATH = "/tmp/trades.db"   # SQLite file (ephemeral on free Render)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Trades table (existing)
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            magic TEXT,
            ticket TEXT,
            symbol TEXT,
            type TEXT,
            volume REAL,
            open_price REAL,
            sl REAL,
            tp REAL,
            close_profit REAL,
            comment TEXT,
            timestamp TEXT,
            received_at TEXT
        )
    ''')
    # Licenses table (new)
    c.execute('''
        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            email TEXT,
            bound_mt5_account TEXT,
            activated_at TEXT,
            expires_at TEXT,
            max_accounts INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1
        )
    ''')
    # Activations log (optional)
    c.execute('''
        CREATE TABLE IF NOT EXISTS license_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT,
            mt5_account TEXT,
            last_validated TEXT,
            FOREIGN KEY(license_key) REFERENCES licenses(license_key)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)

init_db()

# ==================== HELPER FUNCTIONS ====================
def save_trade(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO trades (
            action, magic, ticket, symbol, type, volume, open_price, sl, tp,
            close_profit, comment, timestamp, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get("action"),
        str(data.get("magic", "")),
        str(data.get("ticket", "")),
        data.get("symbol", ""),
        data.get("type", ""),
        data.get("volume"),
        data.get("open_price"),
        data.get("sl"),
        data.get("tp"),
        data.get("close_profit"),
        data.get("comment", ""),
        data.get("timestamp", ""),
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()

def get_trades_since(since_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE id > ? ORDER BY id ASC", (since_id,))
    rows = c.fetchall()
    conn.close()
    columns = ["id","action","magic","ticket","symbol","type","volume","open_price",
               "sl","tp","close_profit","comment","timestamp","received_at"]
    return [dict(zip(columns, row)) for row in rows]

def is_trade_auth():
    return request.headers.get("X-Auth-Token") == SECRET_KEY

def is_admin_auth():
    return request.headers.get("X-Admin-Token") == ADMIN_SECRET

def generate_license_key():
    return secrets.token_hex(16).upper()

# ==================== TRADE COPYING ENDPOINTS ====================
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive", "db": DB_PATH})

@app.route("/copier", methods=["POST"])
def receive_trade():
    if not is_trade_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    logger.info("Received trade: %s", json.dumps(data))

    action = data.get("action")
    if action not in ("open", "close", "modify"):
        return jsonify({"error": "Invalid action"}), 400

    save_trade(data)
    return jsonify({"status": "ok", "stored": True}), 200

@app.route("/trades", methods=["GET"])
def get_new_trades():
    if not is_trade_auth():
        return jsonify({"error": "Unauthorized"}), 401
    since_id = request.args.get("since_id", default=0, type=int)
    trades = get_trades_since(since_id)
    return jsonify({"since_id": since_id, "trades": trades}), 200

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
        return "No trades yet", 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","action","magic","ticket","symbol","type","volume","open_price",
                     "sl","tp","close_profit","comment","timestamp","received_at"])
    writer.writerows(rows)
    return output.getvalue(), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment;filename=trades.csv"
    }

# ==================== LICENSE MANAGEMENT ENDPOINTS ====================
@app.route("/validate", methods=["POST"])
def validate_license():
    """
    Called by the EA to check if a license key is valid.
    Expects JSON: { "license_key": "...", "mt5_account": "123456" }
    Returns: { "allowed": bool, "expires_at": "...", "message": "..." }
    """
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
        return jsonify({"allowed": False, "message": "Invalid license key"}), 200

    # lic order: license_key, email, bound_mt5_account, activated_at, expires_at, max_accounts, is_active
    bound_account = lic[2]
    expires_at = lic[4]
    is_active = lic[6]
    now = datetime.now()

    if not is_active:
        conn.close()
        return jsonify({"allowed": False, "message": "License is deactivated"}), 200

    if expires_at:
        exp_date = datetime.fromisoformat(expires_at)
        if now > exp_date:
            conn.close()
            return jsonify({"allowed": False, "message": "License expired"}), 200

    # Binding logic: if no account bound yet, bind now
    if bound_account is None:
        c.execute("UPDATE licenses SET bound_mt5_account = ? WHERE license_key = ?",
                  (mt5_account, license_key))
        conn.commit()
        bound_account = mt5_account
    elif bound_account != mt5_account:
        conn.close()
        return jsonify({"allowed": False, "message": "License already used on another MT5 account"}), 200

    # Log validation
    c.execute("INSERT INTO license_activations (license_key, mt5_account, last_validated) VALUES (?, ?, ?)",
              (license_key, mt5_account, now.isoformat()))
    conn.commit()
    conn.close()

    return jsonify({
        "allowed": True,
        "expires_at": expires_at,
        "message": "License valid"
    }), 200

@app.route("/activate_license", methods=["POST"])
def activate_license():
    """
    Admin endpoint to manually activate a license.
    Requires X-Admin-Token header.
    JSON: { "email": "user@example.com", "expires_days": 365, "max_accounts": 1 }
    Returns: { "license_key": "...", "message": "..." }
    """
    if not is_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    email = data.get("email")
    expires_days = data.get("expires_days")  # null = never expires
    max_accounts = data.get("max_accounts", 1)

    if not email:
        return jsonify({"error": "Email required"}), 400

    license_key = generate_license_key()
    activated_at = datetime.now().isoformat()
    expires_at = None
    if expires_days:
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO licenses (license_key, email, bound_mt5_account, activated_at, expires_at, max_accounts, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
    ''', (license_key, email, None, activated_at, expires_at, max_accounts))
    conn.commit()
    conn.close()

    logger.info("Activated license %s for email %s", license_key, email)
    return jsonify({
        "license_key": license_key,
        "expires_at": expires_at,
        "message": "License activated successfully"
    }), 200

@app.route("/licenses", methods=["GET"])
def list_licenses():
    """Admin endpoint to list all licenses (use with admin token)"""
    if not is_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT license_key, email, bound_mt5_account, activated_at, expires_at, is_active FROM licenses")
    rows = c.fetchall()
    conn.close()
    licenses = []
    for row in rows:
        licenses.append({
            "license_key": row[0],
            "email": row[1],
            "bound_mt5_account": row[2],
            "activated_at": row[3],
            "expires_at": row[4],
            "is_active": bool(row[5])
        })
    return jsonify(licenses), 200

# ==================== RUN (for local testing) ====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)