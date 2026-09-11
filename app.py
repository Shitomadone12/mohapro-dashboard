"""
MOHA PRO - Cloud Dashboard Backend (v3, multi-bot)
--------------------------------------------------
Isbeddelka v3:
  - LABA BOT AMA KA BADAN: bot kastaa wuxuu diraa "bot":"<magac>". Xogtoodu
    gebi ahaanba way kala go'an tahay - state, amarro, symbols.
  - Dashboard-ku wuxuu leeyahay bot switcher: "Dhammaan" ama mid gaar ah.
  - Amarku wuxuu u socdaa BOOTKA la doortay oo keliya.

Endpoints:
  POST /update                        - bootka -> xogta   (body: token, bot, ...)
  GET  /state?token=&bot=             - dashboard <- xogta bot gaar ah
  GET  /state?token=                  - dashboard <- liiska botyada oo dhan
  GET  /api/commands?token=&bot=      - bootka <- amarka soo socda
  POST /admin/command                 - dashboard -> dir amar (token, bot, command)
  GET  /diag                          - sababta DEMO
  GET  /signals, /health
"""
import os
import time
import json
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, Response

app = Flask(name)

BASE_DIR = os.path.dirname(os.path.abspath(file))
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard.html")

# ---------------- CONFIG ----------------
# MUHIIM: AUTH_TOKEN waa in uu SAX AHAAN la mid yahay InpCloudToken ee EA-yada.
# LABADA BOT waxay isticmaalaan ISKU TOKEN - waxa kala saara bot magaca.
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MohaPro_Live_2026_MySecret")
MASTER_TOKEN = AUTH_TOKEN

BUILD = "v4.0-2026-09-08"
DEFAULT_BOT = "default"
MAX_HISTORY = 120
STALE_SECONDS = 120
FORGET_SECONDS = 1800   # bot aan wax dirin 30 daqiiqo -> liiska laga saaro
MAX_QUEUE = 20

# ---------------- SIGNALS (ikhtiyaari) ----------------
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY", "")
SIGNAL_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "EUR/JPY"]
SIGNAL_INTERVAL = os.environ.get("SIGNAL_INTERVAL", "5min")
SIGNAL_CACHE_SEC = 60
_signal_cache = {}


# ---------------- STATE ----------------
def blank_state():
    return {
        "balance": None, "equity": None, "profit": None,
        "winrate": None, "drawdown": None, "opentrades": None,
        "symbol": None, "trades": None, "journal": None,
        "account": None, "broker": None, "trading": None,
        "updated": None, "equity_history": [],
    }


# token -> bot -> state
STATES = {}
# token -> bot -> [amarro]
COMMANDS = {}
# token -> bot -> {"XAUUSD": True}
SYMBOL_FLAGS = {}
# token -> bot -> {"count": n, "ip": str}
SEEN = {}


def clean_bot(name):
    n = (name or "").strip()
    if not n:
        return DEFAULT_BOT
    return n[:40]


def get_token(req):
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        t = auth[7:].strip()
        if t:
            return t
    data = req.get_json(silent=True) or {}
    if data.get("token"):
        return str(data.get("token"))
    if req.args.get("token"):
        return req.args.get("token")
    return None


def get_bot(req):
    data = req.get_json(silent=True) or {}
    return clean_bot(data.get("bot") or req.args.get("bot"))


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ============ 1) Bootka -> Server ============
@app.route("/update", methods=["POST", "OPTIONS"])
def update():
    if request.method == "OPTIONS":
        return ("", 204)
    tok = get_token(request)
    if not tok:
        return jsonify({"error": "no token"}), 401

    bot = get_bot(request)
    data = request.get_json(silent=True) or {}
    st = STATES.setdefault(tok, {}).setdefault(bot, blank_state())

    for k in ["balance", "equity", "profit", "winrate", "drawdown", "opentrades",
              "symbol", "trades", "journal", "account", "broker", "trading"]:
        if k in data:
            st[k] = data[k]

    st["updated"] = int(time.time())