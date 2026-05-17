import os
import threading
import time
import logging
import json
import traceback
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
import requests
import db
from trade_endpoints import trade_bp
from telegram_bot import bot_bp, set_bot_token
from scheduler import start_scheduler
from risk_engine import calculate_dynamic_lot_size, can_add_position
from pairing_engine import get_best_pairing_decision, PairingConfig, PairingEngineState
from supabase import create_client, Client

# Environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change_me_admin")
MASTER_KEY = os.environ.get("MASTER_KEY", "YourMasterKeyHere123!")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

app = Flask(__name__)
app.secret_key = os.urandom(24)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Supabase client (with graceful fallback)
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized")
    except Exception as e:
        logger.error(f"Supabase initialization failed: {e}")
else:
    logger.warning("Supabase credentials missing – using JSON fallback")

# Initialize local database
db.init_db()
db.sync_master_key(MASTER_KEY)
set_bot_token(TELEGRAM_BOT_TOKEN)

# Register blueprints
app.register_blueprint(trade_bp)
app.register_blueprint(bot_bp)

# ---------- JSON file storage for licenses (fallback) and config ----------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
LICENSES_JSON_BACKUP = os.path.join(DATA_DIR, "licenses_backup.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.log")

DEFAULT_CONFIG = {
    "participation_percent": 5.0,
    "mor_safety_multiplier": 1.15,
    "max_recovery_additions": 5,
    "hardstop_percent": 5.0,
    "min_entry_spacing_percent": 0.1,
    "max_grid_levels": 6,
    "enable_flip_engine": False,
    "use_extreme_tracking": False
}

def load_licenses():
    if supabase:
        try:
            result = supabase.table('licenses').select('*').execute()
            if result.data:
                licenses_dict = {}
                for row in result.data:
                    licenses_dict[row['license_key']] = row
                return licenses_dict
        except Exception as e:
            logger.error(f"Supabase load failed: {e}")
    if not os.path.exists(LICENSES_JSON_BACKUP):
        return {}
    try:
        with open(LICENSES_JSON_BACKUP, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"JSON load failed: {e}")
        return {}

def save_licenses(licenses_dict):
    try:
        with open(LICENSES_JSON_BACKUP, "w") as f:
            json.dump(licenses_dict, f, indent=2)
    except Exception as e:
        logger.error(f"JSON backup save failed: {e}")
    if supabase:
        try:
            for lic in licenses_dict.values():
                supabase.table('licenses').upsert(lic).execute()
        except Exception as e:
            logger.error(f"Supabase upsert failed: {e}")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        config = DEFAULT_CONFIG.copy()
        config.update(saved)
        return config
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

def log_heartbeat(data):
    try:
        with open(HEARTBEAT_FILE, "a") as f:
            f.write(json.dumps(data) + "\n")
    except Exception as e:
        logger.error(f"Failed to log heartbeat: {e}")

# ---------- Idempotency cache ----------
processed_requests = {}
REQUEST_CACHE_TTL_SECONDS = 60

# ---------- ENDPOINTS ----------
@app.route("/validate-license", methods=["POST"])
def validate_license():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    license_key = data.get("license", "").strip()
    account = data.get("account")
    broker = data.get("broker", "")

    if not license_key or account is None:
        return jsonify({"error": "Missing license or account"}), 400

    try:
        if supabase:
            result = supabase.table('licenses').select('*').eq('license_key', license_key).execute()
            if not result.data:
                return jsonify({"valid": False, "reason": "License not found"}), 200
            lic = result.data[0]
        else:
            licenses = load_licenses()
            lic = licenses.get(license_key)
            if not lic:
                return jsonify({"valid": False, "reason": "License not found"}), 200

        expires_at = lic.get('expires_at')
        if expires_at:
            try:
                exp_date = datetime.fromisoformat(expires_at)
                if datetime.now() > exp_date:
                    return jsonify({"valid": False, "reason": "License expired"}), 200
            except:
                pass

        bound_account = lic.get('bound_account')
        if bound_account is not None and bound_account != account:
            return jsonify({"valid": False, "reason": "Account not bound"}), 200

        if not lic.get('is_active', True):
            return jsonify({"valid": False, "reason": "License inactive"}), 200

        return jsonify({
            "valid": True,
            "expires": lic.get('expires_at', '2099-12-31'),
            "min_version": lic.get('min_version', '1.0.0')
        }), 200
    except Exception as e:
        logger.error(f"validate_license error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/get-config", methods=["GET"])
def get_config():
    config = load_config()
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
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing data"}), 400
    data["server_time"] = datetime.now().isoformat()
    log_heartbeat(data)
    return jsonify({"status": "ok"}), 200

@app.route("/calculate-lot", methods=["POST"])
def calculate_lot():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON"}), 400

    required = ["equity", "protected_floor", "initial_account_equity", "direction",
                "first_entry_price", "free_margin", "max_expected_move_percent",
                "max_grid_levels", "max_recovery_additions", "min_operational_additions",
                "min_entry_spacing_percent", "mor_safety_multiplier",
                "growth_participation_percent", "grid_levels", "tick_value",
                "tick_size", "min_lot", "max_lot", "lot_step", "symbol"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing {field}"}), 400

    try:
        lot = calculate_dynamic_lot_size(
            equity=data["equity"],
            protected_floor=data["protected_floor"],
            initial_account_equity=data["initial_account_equity"],
            direction=data["direction"],
            first_entry_price=data["first_entry_price"],
            free_margin=data["free_margin"],
            max_expected_move_percent=data["max_expected_move_percent"],
            max_grid_levels=data["max_grid_levels"],
            max_recovery_additions=data["max_recovery_additions"],
            min_operational_additions=data["min_operational_additions"],
            min_entry_spacing_percent=data["min_entry_spacing_percent"],
            mor_safety_multiplier=data["mor_safety_multiplier"],
            growth_participation_percent=data["growth_participation_percent"],
            grid_levels=data["grid_levels"],
            tick_value=data["tick_value"],
            tick_size=data["tick_size"],
            min_lot=data["min_lot"],
            max_lot=data["max_lot"],
            lot_step=data["lot_step"],
            symbol=data["symbol"]
        )
        return jsonify({"lot": lot, "allowed": lot > 0}), 200
    except Exception as e:
        logger.error(f"Lot calculation error: {e}")
        return jsonify({"error": "Internal server error", "lot": -1}), 500

@app.route("/can-add-position", methods=["POST"])
def can_add():
    data = request.get_json()
    projected = data.get("projected_loss", 0)
    allowed = data.get("allowed_loss", 0)
    allowed_bool = can_add_position(projected, allowed)
    return jsonify({"allowed": allowed_bool}), 200

# ---------- PAIRING DECISION ENDPOINT ----------
@app.route("/pairing-decision", methods=["POST"])
def pairing_decision():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON"}), 400

    request_id = data.get("request_id")
    if not request_id:
        return jsonify({"error": "Missing request_id"}), 400

    # Idempotency
    if request_id in processed_requests:
        cached = processed_requests[request_id]
        if datetime.now() < cached["expires_at"]:
            return jsonify(cached["response"]), 200
        else:
            del processed_requests[request_id]

    basket_hash = data.get("basket_hash", "")

    try:
        from models import PairingConfig, PairingEngineState, PositionInfo
        from pairing_engine import get_best_pairing_decision as engine_decision
        from dataclasses import asdict

        # Safe config unpacking
        cfg = data.get("config", {})
        config = PairingConfig(**{k: cfg.get(k) for k in PairingConfig.__dataclass_fields__.keys()})

        # Safe state unpacking
        st = data.get("state", {})
        state = PairingEngineState(**{k: st.get(k) for k in PairingEngineState.__dataclass_fields__.keys()})

        # Build positions with tolerant is_buy
        positions = []
        for p in data.get("positions", []):
            is_buy = p.get("is_buy", p.get("isBuy", False))
            positions.append(PositionInfo(
                ticket=p["ticket"],
                profit=p["profit"],
                volume=p["volume"],
                entry=p["entry"],
                is_buy=is_buy
            ))

        symbol_info = data.get("symbol_info", {})
        direction_locked = data.get("g_direction_locked", False)
        current_direction_is_buy = data.get("g_current_direction_is_buy", True)
        current_time = data.get("current_time", int(time.time()))
        atr_h4 = data.get("atr_h4", 0.001)
        active_flip_ticket = data.get("active_flip_ticket", None)

        decision = engine_decision(
            positions=positions,
            direction_locked=direction_locked,
            current_direction_is_buy=current_direction_is_buy,
            symbol_info=symbol_info,
            config=config,
            state=state,
            current_time=current_time,
            atr_h4=atr_h4,
            active_flip_ticket=active_flip_ticket
        )

        expires_at = int(time.time()) + 5
        response = {
            "request_id": request_id,
            "expires_at": expires_at,
            "basket_hash": basket_hash,
            "decision": asdict(decision) if decision else None
        }

        # Cache response
        processed_requests[request_id] = {
            "expires_at": datetime.now() + timedelta(seconds=REQUEST_CACHE_TTL_SECONDS),
            "response": response
        }

        return jsonify(response), 200

    except Exception as e:
        # Log full traceback and return a 500 with the error message (for debugging)
        error_trace = traceback.format_exc()
        logger.error(f"Pairing decision error: {e}\n{error_trace}")
        return jsonify({"error": str(e), "trace": error_trace}), 500

# ---------- ADMIN ENDPOINTS ----------
@app.route("/admin/set-config", methods=["POST"])
def admin_set_config():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    new_config = request.get_json()
    if not new_config:
        return jsonify({"error": "Missing config"}), 400

    current = load_config()
    for key, value in new_config.items():
        if key in DEFAULT_CONFIG:
            current[key] = value
        else:
            logger.warning(f"Unknown config key: {key}")

    save_config(current)
    return jsonify({"status": "updated", "config": current}), 200

@app.route("/admin/create-license", methods=["POST"])
def admin_create_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "license_key" not in data:
        return jsonify({"error": "Missing license_key"}), 400

    license_key = data["license_key"].strip()
    bound_account = data.get("bound_account")
    expires_at = data.get("expires_at", "2099-12-31")
    min_version = data.get("min_version", "1.0.0")
    telegram_chat_id = data.get("telegram_chat_id")
    is_trial = data.get("is_trial", False)

    new_license = {
        "license_key": license_key,
        "bound_account": bound_account,
        "expires_at": expires_at,
        "min_version": min_version,
        "created_at": datetime.now().isoformat(),
        "is_active": True,
        "is_trial": is_trial,
        "is_master": False,
        "telegram_chat_id": telegram_chat_id
    }

    try:
        if supabase:
            existing = supabase.table('licenses').select('license_key').eq('license_key', license_key).execute()
            if existing.data:
                return jsonify({"error": "License already exists"}), 400
            supabase.table('licenses').insert(new_license).execute()
        else:
            licenses = load_licenses()
            if license_key in licenses:
                return jsonify({"error": "License already exists"}), 400
            licenses[license_key] = new_license
            save_licenses(licenses)
        return jsonify({"status": "created", "license": license_key}), 201
    except Exception as e:
        logger.error(f"create_license error: {e}")
        return jsonify({"error": "Database error"}), 500

@app.route("/admin/disable-license", methods=["POST"])
def admin_disable_license():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "license_key" not in data:
        return jsonify({"error": "Missing license_key"}), 400

    license_key = data["license_key"].strip()

    try:
        if supabase:
            result = supabase.table('licenses').update({
                "is_active": False,
                "expires_at": datetime.now().isoformat()
            }).eq('license_key', license_key).execute()
            if not result.data:
                return jsonify({"error": "License not found"}), 404
        else:
            licenses = load_licenses()
            if license_key not in licenses:
                return jsonify({"error": "License not found"}), 404
            licenses[license_key]["is_active"] = False
            licenses[license_key]["expires_at"] = datetime.now().isoformat()
            save_licenses(licenses)
        return jsonify({"status": "disabled"}), 200
    except Exception as e:
        logger.error(f"disable_license error: {e}")
        return jsonify({"error": "Database error"}), 500

@app.route("/licenses", methods=["GET"])
def list_licenses():
    if request.headers.get("X-Admin-Token") != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        if supabase:
            result = supabase.table('licenses').select('*').execute()
            return jsonify(result.data), 200
        else:
            licenses = load_licenses()
            return jsonify(list(licenses.values())), 200
    except Exception as e:
        logger.error(f"list_licenses error: {e}")
        return jsonify({"error": "Database error"}), 500

# ---------- KEEP-ALIVE THREAD ----------
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

# Start scheduler
scheduler = start_scheduler()

# ---------- EXISTING ENDPOINTS ----------
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)