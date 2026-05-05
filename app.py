import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify

# === CONFIGURATION ===
# Read the secret key from environment variables (set on Render)
SECRET_KEY = os.environ.get("COP_SECRET_KEY", "change_this_in_production")

app = Flask(__name__)

# Configure logging (Render will show these logs)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === HELPER: Validate auth header ===
def is_authorized():
    auth = request.headers.get("X-Auth-Token")
    if not auth or auth != SECRET_KEY:
        logger.warning(f"Unauthorized attempt from {request.remote_addr}")
        return False
    return True

# === HEALTH CHECK (GET) ===
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "alive", "message": "Trade copier server is running"})

# === MAIN ENDPOINT FOR TRADE EVENTS ===
@app.route("/copier", methods=["POST"])
def handle_trade_event():
    # 1. Authentication
    if not is_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    # 2. Parse JSON body
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json()
    logger.info(f"Received: {json.dumps(data)}")

    # 3. Basic validation
    if "action" not in data:
        return jsonify({"error": "Missing 'action' field"}), 400

    action = data["action"]

    # 4. Process based on action (you can add your own logic here)
    try:
        if action == "open":
            # Expected fields: magic, symbol, ticket, type, volume, open_price, sl, tp, comment, timestamp
            required = ["magic", "symbol", "ticket", "type", "volume", "open_price", "timestamp"]
            for field in required:
                if field not in data:
                    return jsonify({"error": f"Missing '{field}' for open action"}), 400
            logger.info(f"OPEN POSITION: {data['symbol']} {data['type']} {data['volume']} @ {data['open_price']}")

        elif action == "close":
            # Expected: magic, ticket, close_profit, timestamp
            if "ticket" not in data:
                return jsonify({"error": "Missing 'ticket' for close action"}), 400
            logger.info(f"CLOSE POSITION: ticket {data['ticket']}, profit {data.get('close_profit',0)}")

        elif action == "modify":
            # Expected: magic, ticket, sl, tp, timestamp
            if "ticket" not in data:
                return jsonify({"error": "Missing 'ticket' for modify action"}), 400
            logger.info(f"MODIFY POSITION: ticket {data['ticket']}, new SL={data.get('sl')}, new TP={data.get('tp')}")

        else:
            return jsonify({"error": f"Unknown action '{action}'"}), 400

        # 5. Respond success
        return jsonify({"status": "ok", "received": True}), 200

    except Exception as e:
        logger.exception("Error processing request")
        return jsonify({"error": "Internal server error"}), 500

# === RUN (only when executed directly, not used by gunicorn) ===
if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=5000, debug=True)