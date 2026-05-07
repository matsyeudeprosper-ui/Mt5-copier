import sqlite3
import json
import logging
from datetime import datetime

DB_PATH = "/tmp/trades.db"
logger = logging.getLogger(__name__)

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    # Original trades table (legacy)
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT, magic TEXT, ticket TEXT, symbol TEXT, type TEXT,
        volume REAL, open_price REAL, sl REAL, tp REAL, close_profit REAL,
        comment TEXT, timestamp TEXT, received_at TEXT)''')
    # New messages table for sequence‑based event log
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seq INTEGER UNIQUE,
        action TEXT,
        symbol TEXT,
        magic TEXT,
        data TEXT,
        timestamp TEXT,
        created_at TEXT
    )''')
    # Licenses table
    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
        license_key TEXT PRIMARY KEY,
        telegram_chat_id TEXT,
        bound_account TEXT,
        activated_at TEXT,
        expires_at TEXT,
        is_master INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        is_trial INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS license_activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT,
        mt5_account TEXT,
        action TEXT,
        created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        plan TEXT,
        license_key TEXT,
        telegram_chat_id TEXT,
        status TEXT,
        created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        telegram_chat_id TEXT PRIMARY KEY,
        starting_balance REAL,
        base_currency TEXT DEFAULT 'USD',
        deposits TEXT DEFAULT '[]',
        pip_value REAL,
        currency TEXT,
        hard_stop_hit INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0,
        auto_report_enabled INTEGER DEFAULT 0,
        report_hour INTEGER DEFAULT 20,
        report_frequency TEXT DEFAULT 'daily'
    )''')

    # --- Migrations: add missing columns to user_settings ---
    c.execute("PRAGMA table_info(user_settings)")
    existing_cols = [col[1] for col in c.fetchall()]
    if "hard_stop_hit" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN hard_stop_hit INTEGER DEFAULT 0")
    if "trial_used" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN trial_used INTEGER DEFAULT 0")
    if "auto_report_enabled" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN auto_report_enabled INTEGER DEFAULT 0")
    if "report_hour" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN report_hour INTEGER DEFAULT 20")
    if "report_frequency" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN report_frequency TEXT DEFAULT 'daily'")
    if "pip_value" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN pip_value REAL")
    if "currency" not in existing_cols:
        c.execute("ALTER TABLE user_settings ADD COLUMN currency TEXT")

    conn.commit()
    conn.close()
    logger.info("Database ready")

def store_message(seq, action, symbol, magic, data, timestamp):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO messages (seq, action, symbol, magic, data, timestamp, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, action, symbol, str(magic), json.dumps(data), timestamp, datetime.now().isoformat())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
    finally:
        conn.close()

def store_legacy_trade(action, data, timestamp):
    conn = get_conn()
    c = conn.cursor()
    try:
        if action == "open":
            c.execute('''INSERT INTO trades (action, magic, ticket, symbol, type, volume, open_price, sl, tp, comment, timestamp, received_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      ("open", str(data.get("magic", "")), str(data.get("ticket", "")), data.get("symbol", ""),
                       data.get("type", ""), data.get("volume"), data.get("open_price"),
                       data.get("sl"), data.get("tp"), data.get("comment", ""),
                       timestamp, datetime.now().isoformat()))
        elif action == "close":
            c.execute('''INSERT INTO trades (action, magic, ticket, close_profit, timestamp, received_at)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      ("close", str(data.get("magic", "")), str(data.get("ticket", "")), data.get("profit", 0),
                       timestamp, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to store legacy trade: {e}")
    finally:
        conn.close()

def get_messages_since_seq(since_seq, symbol=None):
    conn = get_conn()
    c = conn.cursor()
    if symbol:
        c.execute("SELECT seq, action, symbol, magic, data, timestamp FROM messages WHERE symbol = ? AND seq > ? ORDER BY seq ASC",
                  (symbol, since_seq))
    else:
        c.execute("SELECT seq, action, symbol, magic, data, timestamp FROM messages WHERE seq > ? ORDER BY seq ASC",
                  (since_seq,))
    rows = c.fetchall()
    c.execute("SELECT MAX(seq) FROM messages")
    current_seq = c.fetchone()[0] or 0
    conn.close()
    messages = []
    for row in rows:
        seq, action, sym, magic, data_str, ts = row
        messages.append({
            "seq": seq,
            "action": action,
            "symbol": sym,
            "data": json.loads(data_str)
        })
    return messages, current_seq

def get_all_legacy_trades():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_settings(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT starting_balance, base_currency, deposits, pip_value, currency, hard_stop_hit, trial_used, auto_report_enabled, report_hour, report_frequency FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    if row:
        start, currency, deposits_json, pip_value, pip_currency, hard_stop_hit, trial_used, auto_report, hour, freq = row
        deposits = json.loads(deposits_json) if deposits_json else []
        total_deposits = sum(deposits) if deposits else 0
        effective_start = start + total_deposits if start else 0
        return {
            "starting_balance": start,
            "base_currency": currency,
            "deposits": deposits,
            "effective_start": effective_start,
            "pip_value": pip_value,
            "pip_currency": pip_currency,
            "hard_stop_hit": bool(hard_stop_hit),
            "trial_used": bool(trial_used),
            "auto_report_enabled": bool(auto_report),
            "report_hour": hour,
            "report_frequency": freq
        }
    return {
        "starting_balance": None,
        "base_currency": "USD",
        "deposits": [],
        "effective_start": 0,
        "pip_value": None,
        "pip_currency": None,
        "hard_stop_hit": False,
        "trial_used": False,
        "auto_report_enabled": False,
        "report_hour": 20,
        "report_frequency": "daily"
    }

def set_user_settings(chat_id, **kwargs):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    exists = c.fetchone()
    if exists:
        updates = []
        params = []
        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            params.append(value)
        params.append(str(chat_id))
        c.execute(f"UPDATE user_settings SET {', '.join(updates)} WHERE telegram_chat_id = ?", params)
    else:
        cmd = "INSERT INTO user_settings (telegram_chat_id, starting_balance, base_currency, deposits, pip_value, currency, hard_stop_hit, trial_used, auto_report_enabled, report_hour, report_frequency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        c.execute(cmd, (str(chat_id), kwargs.get('starting_balance', 0), kwargs.get('base_currency', 'USD'), '[]', kwargs.get('pip_value'), kwargs.get('pip_currency'), kwargs.get('hard_stop_hit', 0), kwargs.get('trial_used', 0), kwargs.get('auto_report_enabled', 0), kwargs.get('report_hour', 20), kwargs.get('report_frequency', 'daily')))
    conn.commit()
    conn.close()

def set_pip_calibration(chat_id, pip_value, currency):
    set_user_settings(chat_id, pip_value=pip_value, pip_currency=currency)

def add_deposit(chat_id, amount):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT deposits FROM user_settings WHERE telegram_chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    deposits = json.loads(row[0]) if row and row[0] else []
    deposits.append(amount)
    c.execute("UPDATE user_settings SET deposits = ? WHERE telegram_chat_id = ?", (json.dumps(deposits), str(chat_id)))
    conn.commit()
    conn.close()

def set_hard_stop(chat_id, hard_stop_hit):
    set_user_settings(chat_id, hard_stop_hit=1 if hard_stop_hit else 0)

def mark_trial_used(chat_id):
    set_user_settings(chat_id, trial_used=1)

def get_license_by_key(license_key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT telegram_chat_id, expires_at, is_active, bound_account FROM licenses WHERE license_key = ? AND is_master = 0", (license_key,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"chat_id": row[0], "expires_at": row[1], "is_active": row[2], "bound_account": row[3]}
    return None

def update_license_binding(license_key, mt5_account):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE licenses SET bound_account = ? WHERE license_key = ?", (mt5_account, license_key))
    conn.commit()
    conn.close()

def get_master_license():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT license_key FROM licenses WHERE is_master = 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def sync_master_key(MASTER_KEY):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT license_key FROM licenses WHERE is_master = 1")
    existing = c.fetchone()
    if existing:
        if existing[0] != MASTER_KEY:
            c.execute("DELETE FROM licenses WHERE is_master = 1")
            c.execute("INSERT INTO licenses (license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (MASTER_KEY, "master", None, datetime.now().isoformat(), None, 1, 1, 0))
    else:
        c.execute("INSERT INTO licenses (license_key, telegram_chat_id, bound_account, activated_at, expires_at, is_master, is_active, is_trial) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (MASTER_KEY, "master", None, datetime.now().isoformat(), None, 1, 1, 0))
    conn.commit()
    conn.close()