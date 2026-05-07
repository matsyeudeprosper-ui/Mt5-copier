import os
import threading
import time
import logging
from flask import Flask, request, jsonify
import requests
import db
from trade_endpoints import trade_bp
from telegram_bot import bot_bp, set_bot_token
from scheduler import start_scheduler

# Environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
MASTER_KEY = os.environ.get("MASTER_KEY", "YourMasterKeyHere123!")

app = Flask(__name__)
app.secret_key = os.urandom(24)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize database and sync master key
db.init_db()
db.sync_master_key(MASTER_KEY)

# Set bot token for telegram module
set_bot_token(TELEGRAM_BOT_TOKEN)

# Register blueprints
app.register_blueprint(trade_bp)
app.register_blueprint(bot_bp)

# Keep-alive thread
def keep_alive():
    while True:
        time.sleep(240)
        try:
            requests.get(f"http://localhost:{os.environ.get('PORT', 5000)}/health", timeout=10)
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")

if not app.debug:
    threading.Thread(target=keep_alive, daemon=True).start()
    logger.info("Keep-alive thread started")

# Start scheduler
scheduler = start_scheduler()

# Additional endpoints
@app.route("/health", methods=["GET"])
def health():
    return {"status": "alive"}

@app.route("/buy", methods=["GET"])
def payment_page():
    return "<h1>Use Telegram bot to purchase</h1>"

@app.route("/payment_success", methods=["GET"])
def payment_success():
    return "<h1>Payment successful! Your license key will be sent via Telegram.</h1>"

@app.route("/payment_cancel", methods=["GET"])
def payment_cancel():
    return "<h1>Payment cancelled. No license issued.</h1>"

@app.route("/test", methods=["GET"])
def test_dashboard():
    return "<h1>Test dashboard available</h1><p>Add templates/test.html</p>"

@app.route("/licenses", methods=["GET"])
def list_licenses():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    conn = db.get_conn()
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)