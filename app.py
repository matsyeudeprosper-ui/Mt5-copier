import os
import json
import sqlite3
import logging
from datetime import datetime
from flask import Flask, request, jsonify, send_file
import io
import csv

# === CONFIGURATION ===
SECRET_KEY = os.environ.get("COP_SECRET_KEY", "change_me_in_render")
DB_PATH = "/tmp/trades.db"   # Ephemeral on Render free tier, but fast

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE INIT ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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
    conn.commit()
    conn.close()
    logger.info("Database ready at %s", DB_PATH)

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
    # Convert to list of dicts
    columns = ["id","action","magic","ticket","symbol","type","volume","open_price",
               "sl","tp","close_profit","comment","timestamp","received_at"]
    trades = []
    for row in rows:
        trades.append(dict(zip(columns, row)))
    return trades

# === AUTHENTICATION ===
def is_authorized():
    auth = request.headers.get("X-Auth-Token")
    return auth == SECRET_KEY

# === ENDPOINTS ===
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive", "db": DB_PATH})

@app.route("/copier", methods=["POST"])
def receive_trade():
    """Called by your MT5 sender EA"""
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    logger.info("Received: %s", json.dumps(data))

    action = data.get("action")
    if action not in ("open", "close", "modify"):
        return jsonify({"error": "Invalid action"}), 400

    save_trade(data)
    return jsonify({"status": "ok", "stored": True}), 200

@app.route("/trades", methods=["GET"])
def get_new_trades():
    """Called by your MT5 receiver EA to fetch pending trades"""
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    since_id = request.args.get("since_id", default=0, type=int)
    trades = get_trades_since(since_id)
    return jsonify({"since_id": since_id, "trades": trades}), 200

@app.route("/export", methods=["GET"])
def export_csv():
    """Download all trades as CSV for later analysis"""
    if not is_authorized():
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

# === START ===
init_db()

if __name__ == "__main__":
    # For local testing only – Render uses gunicorn
    app.run(host="0.0.0.0", port=5000, debug=False)