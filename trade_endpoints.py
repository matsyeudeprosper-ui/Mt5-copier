import os
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
import db
from config import MESSAGES
from notifications import send_validation_failure_notification

trade_bp = Blueprint('trade', __name__)
logger = logging.getLogger(__name__)

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

    db.store_message(seq, action, symbol, magic, data, timestamp)
    if action in ("open", "close"):
        db.store_legacy_trade(action, data, timestamp)

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
        settings = db.get_user_settings(lic["chat_id"])
        hard_stop_hit = settings["hard_stop_hit"]
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

    settings = db.get_user_settings(chat_id)
    if settings["pip_value"] is not None:
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
    db.set_hard_stop(chat_id, True)
    from telegram_bot import send_telegram
    send_telegram(chat_id, "⚠️ Your EA has hit the hard stop. Trading is paused. Use /resume to continue.")
    return jsonify({"success": True}), 200