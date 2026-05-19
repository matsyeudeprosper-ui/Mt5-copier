import os
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from supabase import create_client
import db
from config import MESSAGES
from notifications import send_validation_failure_notification, send_trade_close_notification

trade_bp = Blueprint('trade', __name__)
logger = logging.getLogger(__name__)

# Supabase client (reuse from app)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized in trade_endpoints")
    except Exception as e:
        logger.error(f"Supabase initialization failed: {e}")

def is_license_valid(license_key, require_master=False, mt5_account=None):
    if license_key is None:
        return False
    if require_master:
        master = db.get_master_license()
        if master != license_key:
            return False
        return True
    else:
        lic = db.get_license_by_key(license_key)
        if not lic:
            return False
        if not lic["is_active"]:
            return False
        if lic["expires_at"]:
            if datetime.now() > datetime.fromisoformat(lic["expires_at"]):
                return False
        if mt5_account is not None:
            if lic["bound_account"] is None:
                db.update_license_binding(license_key, mt5_account)
            elif lic["bound_account"] != mt5_account:
                return False
        return True

@trade_bp.route("/copier", methods=["POST"])
def receive_trade():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not is_license_valid(license_key, require_master=True):
        return jsonify({"error": "Unauthorized"}), 401
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    action = data.get("action")
    allowed_actions = ("open", "close", "partial_close", "modify", "pending_open", "pending_cancel", "pending_modify")
    if action not in allowed_actions:
        return jsonify({"error": "Invalid action"}), 400

    seq = data.get("seq")
    if seq is None:
        return jsonify({"error": "Missing seq"}), 400

    symbol = data.get("symbol", "")
    magic = data.get("magic", "")
    timestamp = data.get("timestamp", datetime.now().isoformat())

    # Store raw message in local DB (legacy)
    db.store_message(seq, action, symbol, magic, data, timestamp)

    # If it's a close, store trade in Supabase and send notification
    if action == "close" and supabase:
        try:
            profit = data.get("profit", 0.0)
            equity_before = data.get("equity_before")
            equity_after = data.get("equity_after")
            trade_type = data.get("trade_type", "single")  # 'single' or 'basket'
            num_positions = data.get("num_positions", 1)
            reason = data.get("reason", "normal_close")
            user_license_key = data.get("user_license_key")  # from EA

            # Get chat_id from user license key (if provided)
            chat_id = None
            if user_license_key:
                try:
                    lic_res = supabase.table('licenses').select('telegram_chat_id').eq('license_key', user_license_key).execute()
                    if lic_res.data:
                        chat_id = lic_res.data[0]['telegram_chat_id']
                except Exception as e:
                    logger.error(f"Failed to find chat_id for user license {user_license_key}: {e}")

            # Insert trade record
            supabase.table('trades').insert({
                "license_key": user_license_key or license_key,
                "symbol": symbol,
                "closed_at": datetime.now().isoformat(),
                "profit": profit,
                "equity_before": equity_before,
                "equity_after": equity_after,
                "trade_type": trade_type,
                "num_positions": num_positions,
                "reason": reason
            }).execute()
            logger.info(f"Trade stored in Supabase: {symbol} profit={profit}")

            # Send Telegram notification
            if chat_id:
                send_trade_close_notification(chat_id, symbol, profit, trade_type, num_positions)
        except Exception as e:
            logger.error(f"Failed to store trade in Supabase: {e}")

    return jsonify({"status": "ok", "seq": seq}), 200

@trade_bp.route("/trades", methods=["GET"])
def get_trades():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not is_license_valid(license_key, require_master=False):
        return jsonify({"error": "Invalid or expired license"}), 401

    since_seq = request.args.get("since_seq", default=0, type=int)
    symbol = request.args.get("symbol", "")
    messages, current_seq = db.get_messages_since_seq(since_seq, symbol if symbol else None)

    return jsonify({
        "since_seq": since_seq,
        "current_seq": current_seq,
        "messages": messages
    }), 200

@trade_bp.route("/export", methods=["GET"])
def export_csv():
    license_key = request.headers.get("X-Auth-Token")
    if not license_key or not (is_license_valid(license_key, require_master=True) or is_license_valid(license_key, require_master=False)):
        return "Unauthorized", 401
    rows = db.get_all_legacy_trades()
    if not rows:
        return "No trades", 404
    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","action","magic","ticket","symbol","type","volume","open_price","sl","tp","close_profit","comment","timestamp","received_at"])
    writer.writerows(rows)
    return output.getvalue(), 200, {"Content-Type":"text/csv","Content-Disposition":"attachment;filename=trades.csv"}

@trade_bp.route("/validate", methods=["POST"])
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
        lic = db.get_license_by_key(license_key)
        if lic:
            if lic["expires_at"] and datetime.now() > datetime.fromisoformat(lic["expires_at"]):
                send_validation_failure_notification(license_key, "expired")
            else:
                send_validation_failure_notification(license_key, "wrong_account")
        return jsonify({"allowed": False, "message": "Invalid or expired license, or wrong MT5 account (use Unbind button on Telegram to reset)"}), 200

    lic = db.get_license_by_key(license_key)
    hard_stop_hit = False
    if lic and lic["chat_id"]:
        # We'll use the updated settings (soon to be migrated)
        try:
            if supabase:
                res = supabase.table('user_settings').select('hard_stop_hit').eq('telegram_chat_id', lic["chat_id"]).execute()
                if res.data and res.data[0].get('hard_stop_hit'):
                    hard_stop_hit = res.data[0]['hard_stop_hit']
        except:
            pass
    return jsonify({"allowed": True, "expires_at": lic["expires_at"], "hard_stop_hit": hard_stop_hit, "message": "Valid"}), 200

@trade_bp.route("/calibrate_pip", methods=["POST"])
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

    lic = db.get_license_by_key(license_key)
    if not lic:
        return jsonify({"error": "Invalid license key"}), 401
    chat_id = lic["chat_id"]

    # Get current pip value from Supabase if available
    pip_value = None
    if supabase:
        res = supabase.table('user_settings').select('pip_value').eq('telegram_chat_id', str(chat_id)).execute()
        if res.data and res.data[0].get('pip_value'):
            return jsonify({"error": "Calibration already exists"}), 400

    conn = db.get_conn()
    c = conn.cursor()
    c.execute("SELECT close_profit FROM trades WHERE action = 'close' AND ticket = ?", (str(master_ticket),))
    trade_row = c.fetchone()
    conn.close()
    if not trade_row:
        return jsonify({"error": "Master trade not found"}), 404
    master_pips = trade_row[0]
    if master_pips == 0:
        return jsonify({"error": "Master trade pip difference is zero"}), 400

    pip_value = real_profit / closed_pips
    if pip_value <= 0:
        return jsonify({"error": "Invalid pip value"}), 400

    # Store in Supabase
    if supabase:
        set_user_settings(chat_id, pip_value=pip_value, pip_currency=currency)
    else:
        db.set_pip_calibration(chat_id, pip_value, currency)

    from telegram_bot import send_telegram
    send_telegram(chat_id, MESSAGES["calibration_success"]["en"].format(currency=currency, pip_value=pip_value))
    return jsonify({"success": True, "pip_value": pip_value, "currency": currency}), 200

@trade_bp.route("/hard_stop", methods=["POST"])
def report_hard_stop():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json()
    license_key = data.get("license_key")
    if not license_key:
        return jsonify({"error": "Missing license_key"}), 400
    lic = db.get_license_by_key(license_key)
    if not lic:
        return jsonify({"error": "Invalid license key"}), 401
    chat_id = lic["chat_id"]
    # Update Supabase
    if supabase:
        set_hard_stop(chat_id, True)
    else:
        db.set_hard_stop(chat_id, True)
    from telegram_bot import send_telegram
    send_telegram(chat_id, "⚠️ Your EA has hit the hard stop. Trading is paused. Use /resume to continue.")
    return jsonify({"success": True}), 200

# Helper to update user settings (simple version; full version in telegram_bot)
def set_user_settings(chat_id, **kwargs):
    if supabase:
        supabase.table('user_settings').update(kwargs).eq('telegram_chat_id', str(chat_id)).execute()
def set_hard_stop(chat_id, value):
    set_user_settings(chat_id, hard_stop_hit=value)