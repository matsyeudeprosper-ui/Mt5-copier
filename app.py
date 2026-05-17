import os
import threading
import time
import logging
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
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

# Initialize database and sync master key (keep existing DB logic for other features)
db.init_db()
db.sync_master_key(MASTER_KEY)

# Set bot token for telegram module
set_bot_token(TELEGRAM_BOT_TOKEN)

# Register blueprints
app.register_blueprint(trade_bp)
app.register_blueprint(bot_bp)

# ---------- NEW: JSON file storage for licenses, config, heartbeats ----------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
LICENSES_FILE = os.path.join(DATA_DIR, "licenses.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.log")

# Default configuration (fallback if file missing)
DEFAULT_CONFIG = {
    "participation_percent": 5.0,      # GrowthParticipationPercent
    "mor_safety_multiplier": 1.15,     # MORSafetyMultiplier
    "max_recovery_additions": 5,       # g_maxRecoveryAdditions (unlimited but set high)
    "hardstop_percent": 5.0,           # Max expected move percent
    "min_entry_spacing_percent": 0.1,  # MinEntrySpacingPercent
    "max_grid_levels": 6,
    "enable_flip_engine": False,
    "use_extreme_tracking": False
}

def load_licenses():
    """Load licenses from JSON file. Returns dict: license_key -> license_data"""
    if not os.path.exists(LICENSES_FILE):
        # Create empty licenses file
        with open(LICENSES_FILE, "w") as f:
            json.dump({}, f)
        return {}
    try:
        with open(LICENSES_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load licenses: {e}")
        return {}

def save_licenses(licenses):
    """Save licenses dict to JSON file"""
    try:
        with open(LICENSES_FILE, "w") as f:
            json.dump(licenses, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save licenses: {e}")

def load_config():
    """Load config from JSON file, return dict (with defaults merged)"""
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        # Merge with defaults (in case new keys added later)
        config = DEFAULT_CONFIG.copy()
        config.update(saved)
        return config
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(config):
    """Save config dict to JSON file"""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

def log_heartbeat(data):
    """Append heartbeat data to log file (one JSON line per entry)"""
    try:
        with open(HEARTBEAT_FILE, "a") as f:
            f.write(json.dumps(data) + "\n")
    except Exception as e:
        logger.error(f"Failed to log heartbeat: {e}")

# ---------- NEW ENDPOINTS FOR EA ----------
@app.route("/validate-license", methods=["POST"])
def validate_license():
    """
    Expects JSON: { "license": "ABC123", "account": 123456, "broker": "ICMarkets" }
    Returns: { "valid": bool, "expires": "YYYY-MM-DD", "min_version": "1.0.5" }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    license_key = data.get("license", "").strip()
    account = data.get("account")
    broker = data.get("broker", "")

    if not license_key or account is None:
        return jsonify({"error": "Missing license or account"}), 400

    licenses = load_licenses()
    lic = licenses.get(license_key)
    if not lic:
        return jsonify({"valid": False, "reason": "License not found"}), 200

    # Check if expired
    expires_at = lic.get("expires_at")
    if expires_at:
        try:
            exp_date = datetime.fromisoformat(expires_at)
            if datetime.now() > exp_date:
                return jsonify({"valid": False, "reason": "License expired"}), 200
        except:
            pass

    # Check bound account (if any)
    bound_account = lic.get("bound_account")
    if bound_account is not None and bound_account != account:
        return jsonify({"valid": False, "reason": "Account not bound"}), 200

    # Optional: check broker?
    # (we can skip for now)

    # License is valid
    return jsonify({
        "valid": True,
        "expires": lic.get("expires_at", "2099-12-31"),
        "min_version": lic.get("min_version", "1.0.0")
    }), 200

@app.route("/get-config", methods=["GET"])
def get_config():
    """Return current risk parameters (no auth needed, public for all EAs)"""
    config = load_config()
    # Map to EA's expected variable names (snake_case to match EA's internal naming)
    response = {
        "participation_percent": config.get("participation_percent", 5.0),
        "mor_safety_multiplier": config.get("mor_safety_multiplier", 1.15),
        "max_recovery_additions": config.get("max_recovery_additions", 5),
        "hardstop_percent": config.get("hardstop_percent", 5.0),
        "min_entry_spacing_percent": config.get("min_entry_spacing_percent", 0.1),
        "max_grid_levels": config.get("max_grid_levels", 6),
        "enable_flip_engine": config.get("enable_flip_engine", False),
        "use_extreme_tracking": config.get("use_extreme_tracking", False)
    }
    return jsonify(response), 200

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    """EA sends telemetry: { equity, floating_dd, positions, version, ... }"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing data"}), 400

    # Add server timestamp
    data["server_time"] = datetime.now().isoformat()
    log_heartbeat(data)
    return jsonify({"status": "ok"}), 200

# ---------- ADMIN ENDPOINTS (protected by X-Admin-Token) ----------
@app.route("/admin/set-config", methods=["POST"])
def admin_set_config():
    """Update configuration. Expect JSON with new values. Header: X-Admin-Token: ADMIN_SECRET"""
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    new_config = request.get_json()
    if not new_config:
        return jsonify({"error": "Missing config"}), 400

    current = load_config()
    # Update only provided keys (allow partial updates)
    for key, value in new_config.items():
        if key in DEFAULT_CONFIG:
            current[key] = value
        else:
            logger.warning(f"Unknown config key: {key}")

    save_config(current)
    return jsonify({"status": "updated", "config": current}), 200

@app.route("/admin/create-license", methods=["POST"])
def admin_create_license():
    """Create a new license. Header: X-Admin-Token: ADMIN_SECRET
       Body: { "license_key": "ABC123", "bound_account": 123456 (optional), "expires_at": "2026-12-31", "min_version": "1.0.0" }
    """
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "license_key" not in data:
        return jsonify({"error": "Missing license_key"}), 400

    license_key = data["license_key"].strip()
    bound_account = data.get("bound_account")  # can be None (not bound)
    expires_at = data.get("expires_at", "2099-12-31")
    min_version = data.get("min_version", "1.0.0")

    licenses = load_licenses()
    if license_key in licenses:
        return jsonify({"error": "License already exists"}), 400

    licenses[license_key] = {
        "bound_account": bound_account,
        "expires_at": expires_at,
        "min_version": min_version,
        "created_at": datetime.now().isoformat(),
        "is_active": True
    }
    save_licenses(licenses)
    return jsonify({"status": "created", "license": license_key}), 201

@app.route("/admin/disable-license", methods=["POST"])
def admin_disable_license():
    """Disable a license (soft delete). Header: X-Admin-Token: ADMIN_SECRET
       Body: { "license_key": "ABC123" }
    """
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "license_key" not in data:
        return jsonify({"error": "Missing license_key"}), 400

    license_key = data["license_key"].strip()
    licenses = load_licenses()
    if license_key not in licenses:
        return jsonify({"error": "License not found"}), 404

    # Instead of deleting, we mark as inactive (optional)
    licenses[license_key]["is_active"] = False
    # Also set expiry to now if you want immediate block
    licenses[license_key]["expires_at"] = datetime.now().isoformat()
    save_licenses(licenses)
    return jsonify({"status": "disabled"}), 200

# Keep-alive thread (same as before)
def keep_alive():
    while True:
        time.sleep(60)
        try:
            url = f"http://localhost:{os.environ.get('PORT', 5000)}/health"
            requests.get(url, timeout=5)
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")

if not app.debug:
    threading.Thread(target=keep_alive, daemon=True).start()
    logger.info("Keep-alive thread started (every 60 seconds)")

# Start scheduler (existing)
scheduler = start_scheduler()

# Existing endpoints (health, buy, payment, etc.)
@app.route("/health", methods=["GET"])
def health():
    return {"status": "alive"}

@app.route("/buy", methods=["GET"])
def payment_page():
    return render_template("buy.html")

@app.route("/payment_success", methods=["GET"])
def payment_success():
    return render_template("success.html")

@app.route("/payment_cancel", methods=["GET"])
def payment_cancel():
    return render_template("cancel.html")

@app.route("/test", methods=["GET"])
def test_dashboard():
    return render_template("test.html")

@app.route("/licenses", methods=["GET"])
def list_licenses():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    licenses = load_licenses()
    return jsonify(licenses), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)