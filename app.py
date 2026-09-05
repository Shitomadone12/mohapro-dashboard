"""
MOHA PRO ULTIMATE - Cloud Dashboard Backend (v2)
------------------------------------------------
Isbeddelka v2:
  - Qaybta SYMBOLS: server-ka ayaa symbol-ada ka soo saara trades-ka bootka
    diray, sidaas awgeed .mq4 / .mq5 WAX LOOMA BEDDELIN.
  - Command QUEUE: amarro badan oo isku mar la sugayo (hore mid ayaa kaliya
    la kaydin jiray, kii labaadna wuu tirtiri jiray kii hore).
  - /diag: sharraxaad cad oo ah SABABTA dashboard-ku uu DEMO u muujinayo.
  - VALID_COMMANDS: POC lagu daray (badhanka POC hore 400 ayuu soo celin jiray)
    iyo SYMBOL_ON / SYMBOL_OFF.
  - dashboard.html waxaa laga akhriyaa faylka isku galka ku jira (base64 maaha),
    sidaas ayaad si fudud u tafatiri kartaa.

Endpoints:
  POST /update           - bootka MT4/MT5 -> xogta
  GET  /state            - dashboard <- xogta
  GET  /symbols          - dashboard <- liiska symbol-ada
  GET  /api/commands     - bootka <- amarka soo socda
  POST /admin/command    - dashboard -> dir amar
  GET  /signals          - Twelve Data signals (ikhtiyaari)
  GET  /diag             - caafimaadka xiriirka (debug)
  GET  /health           - health check
"""
import os
import time
import json
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard.html")

# ---------------- CONFIG ----------------
# MUHIIM: AUTH_TOKEN waa in uu SAX AHAAN la mid yahay Cloud_Auth_Token ee EA-ga.
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MohaPro_Live_2026_MySecret")
MASTER_TOKEN = AUTH_TOKEN

MAX_HISTORY = 120        # immisa qiimo equity ah oo la kaydiyo
STALE_SECONDS = 120      # ka dib intaas xogtu waa duugoobtay -> DEMO

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
        "updated": None, "equity_history": [],
    }

STATES = {}         # token -> state
COMMANDS = {}       # token -> [amarro sugaya]
SYMBOL_FLAGS = {}   # token -> {"XAUUSD": True, "US30": False, ...}
SEEN = {}           # token -> {"first": ts, "count": n, "ip": str}

MAX_QUEUE = 20


def get_token(req):
    """Token ka soo qaado: Bearer header, JSON body {token}, ama ?token="""
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

    data = request.get_json(silent=True) or {}
    st = STATES.setdefault(tok, blank_state())

    for k in ["balance", "equity", "profit", "winrate", "drawdown",
              "opentrades", "symbol", "trades", "journal"]:
        if k in data:
            st[k] = data[k]

    st["updated"] = int(time.time())

    if st.get("equity") is not None:
        try:
            st["equity_history"].append(float(st["equity"]))
            if len(st["equity_history"]) > MAX_HISTORY:
                del st["equity_history"][0:len(st["equity_history"]) - MAX_HISTORY]
        except (TypeError, ValueError):
            pass

    seen = SEEN.setdefault(tok, {"first": int(time.time()), "count": 0, "ip": ""})
    seen["count"] += 1
    seen["ip"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")

    return jsonify({"ok": True, "queued_commands": len(COMMANDS.get(tok, []))})


# ============ 2) Dashboard <- Server ============
def live_info(st):
    """Sheeg haddii xogtu nool tahay iyo SABABTA haddii ay nolol la'dahay."""
    if not st:
        return False, "no_data", None
    updated = st.get("updated") or 0
    if not updated:
        return False, "no_data", None
    age = int(time.time() - updated)
    if age >= STALE_SECONDS:
        return False, "stale", age
    return True, "live", age


@app.route("/state", methods=["GET"])
def get_state():
    tok = request.args.get("token") or MASTER_TOKEN
    st = STATES.get(tok)
    if not st:
        out = blank_state()
        out["live"] = False
        out["reason"] = "no_data"
        out["age"] = None
        out["symbols"] = []
        return jsonify(out)

    out = dict(st)
    live, reason, age = live_info(st)
    out["live"] = live
    out["reason"] = reason
    out["age"] = age
    out["symbols"] = build_symbols(tok, st)
    return jsonify(out)


# ============ 3) SYMBOLS ============
def build_symbols(tok, st):
    """
    Symbol-ada waxaa laga soo saaraa trades-ka bootku diray.
    EA-ga wax looma beddelin: `trades` array-ga horeba wuu leeyahay `sym`.
    Haddii EA-gu si toos ah u diro `symbols`, taas ayaa mudnaan leh.
    """
    explicit = st.get("symbols")
    if isinstance(explicit, list) and explicit:
        return explicit

    trades = st.get("trades") or []
    flags = SYMBOL_FLAGS.setdefault(tok, {})
    agg = {}

    for t in trades:
        sym = (t.get("sym") or "").upper()
        if not sym:
            continue
        row = agg.setdefault(sym, {
            "symbol": sym, "open": 0, "closed": 0,
            "open_pnl": 0.0, "closed_pnl": 0.0,
            "strategies": [], "enabled": flags.get(sym, True),
        })
        try:
            pnl = float(t.get("profit") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if (t.get("st") or "").upper() == "OPEN":
            row["open"] += 1
            row["open_pnl"] += pnl
        else:
            row["closed"] += 1
            row["closed_pnl"] += pnl
        strat = t.get("strat")
        if strat and strat not in row["strategies"]:
            row["strategies"].append(strat)

    # symbol-ada la damiyay oo aan trade furan lahayn wali ha muuqdaan
    for sym, on in flags.items():
        if sym not in agg:
            agg[sym] = {"symbol": sym, "open": 0, "closed": 0,
                        "open_pnl": 0.0, "closed_pnl": 0.0,
                        "strategies": [], "enabled": on}

    rows = list(agg.values())
    for r in rows:
        r["open_pnl"] = round(r["open_pnl"], 2)
        r["closed_pnl"] = round(r["closed_pnl"], 2)
    rows.sort(key=lambda r: (-r["open"], -abs(r["open_pnl"]), r["symbol"]))
    return rows


@app.route("/symbols", methods=["GET"])
def symbols_endpoint():
    tok = request.args.get("token") or MASTER_TOKEN
    st = STATES.get(tok)
    return jsonify({
        "symbols": build_symbols(tok, st) if st else [],
        "flags": SYMBOL_FLAGS.get(tok, {}),
    })


# ============ 4) Bootka <- Server: amarrada ============
# Bootku wuxuu raadiyaa `"token":"<token>"` iyo `"command":"START"` — meel bannaan
# la'aan. Sidaas darteed JSON compact ah ayaa loo diraa.
@app.route("/api/commands", methods=["GET"])
def commands():
    tok = get_token(request)
    if not tok:
        return jsonify({"error": "no token"}), 401
    q = COMMANDS.get(tok) or []
    cmd = q.pop(0) if q else ""
    payload = json.dumps({"token": tok, "command": cmd}, separators=(",", ":"))
    return Response(payload, mimetype="application/json")


# ============ 5) Admin -> Server: dir amar ============
SIMPLE_COMMANDS = {"START", "STOP", "PAUSE", "RESUME", "CLOSE_ALL", "CLOSE_PROFIT"}
STRATEGIES = {"SR", "BB", "EMA", "SMC", "VSA", "RSI", "POC", "SCALP", "GRID"}


def validate(cmd):
    """Soo celi (ok, fariin). Amarrada symbol-ka ee cusub halkan ayey ku jiraan."""
    if cmd in SIMPLE_COMMANDS:
        return True, ""
    if cmd.startswith("STRATEGY:"):
        s = cmd.split(":", 1)[1]
        if s in STRATEGIES:
            return True, ""
        return False, "strategy aan la aqoon: " + s
    if cmd.startswith("SYMBOL_ON:") or cmd.startswith("SYMBOL_OFF:"):
        s = cmd.split(":", 1)[1]
        if s and s.replace(".", "").replace("_", "").isalnum() and len(s) <= 16:
            return True, ""
        return False, "symbol aan sax ahayn: " + s
    return False, "amar aan la aqoon"


@app.route("/admin/command", methods=["POST", "OPTIONS"])
def set_command():
    if request.method == "OPTIONS":
        return ("", 204)

    tok = get_token(request) or MASTER_TOKEN
    data = request.get_json(silent=True) or request.form
    cmd = (data.get("command") or "").strip().upper()

    ok, msg = validate(cmd)
    if not ok:
        return jsonify({
            "error": msg,
            "allowed": sorted(SIMPLE_COMMANDS)
                       + ["STRATEGY:" + s for s in sorted(STRATEGIES)]
                       + ["SYMBOL_ON:<SYM>", "SYMBOL_OFF:<SYM>"],
        }), 400

    # xaaladda symbol-ka isla markiiba badal si dashboard-ku uga jawaabo dhaqso
    if cmd.startswith("SYMBOL_ON:"):
        SYMBOL_FLAGS.setdefault(tok, {})[cmd.split(":", 1)[1]] = True
    elif cmd.startswith("SYMBOL_OFF:"):
        SYMBOL_FLAGS.setdefault(tok, {})[cmd.split(":", 1)[1]] = False

    q = COMMANDS.setdefault(tok, [])
    q.append(cmd)
    if len(q) > MAX_QUEUE:
        del q[0:len(q) - MAX_QUEUE]

    return jsonify({"ok": True, "queued": cmd, "pending": len(q)})


# ============ 6) DIAG — sababta DEMO ============
@app.route("/diag", methods=["GET"])
def diag():
    """
    Fur: https://<app>.onrender.com/diag
    Wuxuu kuu sheegayaa MEESHA xiriirku ka jabay.
    """
    now = int(time.time())
    tenants = []
    for tok, st in STATES.items():
        live, reason, age = live_info(st)
        seen = SEEN.get(tok, {})
        tenants.append({
            "token_preview": tok[:6] + "..." + tok[-4:] if len(tok) > 12 else tok,
            "token_matches_env": tok == MASTER_TOKEN,
            "live": live, "reason": reason, "age_seconds": age,
            "updates_received": seen.get("count", 0),
            "last_ip": seen.get("ip", ""),
            "symbols_found": len(build_symbols(tok, st)),
            "pending_commands": len(COMMANDS.get(tok, [])),
        })

    if not tenants:
        hint = ("Boot NA soo gaarin. Hubi: (1) URL-ka /update ee EA-ga, "
                "(2) URL-ka ku jira liiska 'Allow WebRequest' ee MT4/MT5, "
                "(3) in EA-gu shaqeynayo oo AutoTrading la furay.")
    elif not any(t["token_matches_env"] for t in tenants):
        hint = ("Boot wuu soo gaaray LAAKIIN token-kiisu kama mid aha AUTH_TOKEN "
                "ee Render. Taasi waa sababta DEMO. Xal: Cloud_Auth_Token ee EA-ga "
                "ka dhig sida AUTH_TOKEN, AMA dashboard-ka ku fur ?token=<token-ka EA-ga>.")
    elif not any(t["live"] for t in tenants):
        hint = ("Xog hore ayaa timid laakiin way duugowday (>%ds). Hubi in EA-gu "
                "wali shaqeynayo, ama in Render free tier uu hurday." % STALE_SECONDS)
    else:
        hint = "Wax walba way shaqeynayaan."

    return jsonify({
        "server_time": now,
        "env_auth_token_preview": (MASTER_TOKEN[:6] + "..." + MASTER_TOKEN[-4:]
                                   if len(MASTER_TOKEN) > 12 else MASTER_TOKEN),
        "env_auth_token_is_default": MASTER_TOKEN == "MohaPro_Live_2026_MySecret",
        "stale_after_seconds": STALE_SECONDS,
        "tenants": tenants,
        "diagnosis": hint,
    })


# ============ 7) SIGNALS ============
def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _fetch_closes(symbol, interval, size=60):
    if not TWELVEDATA_KEY:
        return None, None, "no_key"
    q = urllib.parse.urlencode({
        "symbol": symbol, "interval": interval,
        "outputsize": size, "apikey": TWELVEDATA_KEY, "format": "JSON",
    })
    try:
        with urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return None, None, "fetch_error: " + str(e)
    if isinstance(data, dict) and data.get("status") == "error":
        return None, None, data.get("message", "api_error")
    vals = data.get("values") or []
    if not vals:
        return None, None, "no_data"
    closes = [float(x["close"]) for x in vals]
    closes.reverse()
    return closes, vals[0].get("datetime"), None


def _compute_signal(closes):
    e9, e21 = _ema(closes, 9), _ema(closes, 21)
    r = _rsi(closes, 14)
    mom = closes[-1] - closes[-4] if len(closes) >= 4 else 0.0
    score, reasons = 0.0, []
    if e9 is not None and e21 is not None:
        if e9 > e21:
            score += 1; reasons.append("EMA up")
        else:
            score -= 1; reasons.append("EMA down")
    if r is not None:
        score += (1 if r > 50 else -1) * min(abs(r - 50) / 20.0, 1.0)
        reasons.append("RSI %.0f" % r)
    if mom > 0:
        score += 1; reasons.append("Momentum up")
    elif mom < 0:
        score -= 1; reasons.append("Momentum down")
    direction = "UP" if score > 0.5 else ("DOWN" if score < -0.5 else "NEUTRAL")
    return {"direction": direction,
            "confidence": int(min(abs(score) / 3.0 * 100, 99)),
            "rsi": round(r, 1) if r is not None else None,
            "reasons": reasons}


@app.route("/signals", methods=["GET"])
def signals():
    if not TWELVEDATA_KEY:
        return jsonify({"error": "no_api_key",
                        "hint": "Geli TWELVEDATA_KEY env var (bilaash: twelvedata.com)"}), 200
    out, now = [], time.time()
    for sym in SIGNAL_PAIRS:
        cached = _signal_cache.get(sym)
        if cached and (now - cached[0] < SIGNAL_CACHE_SEC):
            out.append(cached[1]); continue
        closes, last_dt, err = _fetch_closes(sym, SIGNAL_INTERVAL)
        if err or not closes or len(closes) < 22:
            item = {"symbol": sym, "direction": "N/A", "confidence": 0,
                    "rsi": None, "reasons": [err or "insufficient"], "time": last_dt}
        else:
            item = _compute_signal(closes)
            item["symbol"], item["time"] = sym, last_dt
        _signal_cache[sym] = (now, item)
        out.append(item)
    return jsonify({"pairs": out, "interval": SIGNAL_INTERVAL, "generated": int(now)})


# ============ 8) Pages ============
_dashboard_cache = {"mtime": 0, "html": None}


def dashboard_html():
    try:
        mtime = os.path.getmtime(DASHBOARD_FILE)
        if _dashboard_cache["html"] is None or mtime != _dashboard_cache["mtime"]:
            with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
                _dashboard_cache["html"] = f.read()
            _dashboard_cache["mtime"] = mtime
        return _dashboard_cache["html"]
    except OSError:
        return ("<h1 style='font-family:sans-serif;padding:2rem'>dashboard.html lama helin</h1>"
                "<p style='font-family:sans-serif;padding:0 2rem'>Dhig dashboard.html "
                "isla galka app.py.</p>")


@app.route("/")
@app.route("/admin")
def index():
    return Response(dashboard_html(), mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "tenants": len(STATES)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
