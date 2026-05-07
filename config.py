import json

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

PLANS = CONFIG["plans"]
MESSAGES = CONFIG["messages"]