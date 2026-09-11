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
if st.get("equity") is not None:
        try:
            st["equity_history"].append(float(st["equity"]))
            if len(st["equity_history"]) > MAX_HISTORY:
                del st["equity_history"][0:len(st["equity_history"]) - MAX_HISTORY]
        except (TypeError, ValueError):
            pass

    seen = SEEN.setdefault(tok, {}).setdefault(bot, {"count": 0, "ip": ""})
    seen["count"] += 1
    seen["ip"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")

    return jsonify({"ok": True, "bot": bot,
                    "queued_commands": len(COMMANDS.get(tok, {}).get(bot, []))})


# ============ 2) Dashboard <- Server ============
def live_info(st):
    if not st:
        return False, "no_data", None
    updated = st.get("updated") or 0
    if not updated:
        return False, "no_data", None
    age = int(time.time() - updated)
    if age >= STALE_SECONDS:
        return False, "stale", age
    return True, "live", age


def bot_summary(tok, bot, st):
    live, reason, age = live_info(st)
    return {
        "bot": bot, "live": live, "reason": reason, "age": age,
        "balance": st.get("balance"), "equity": st.get("equity"),
        "profit": st.get("profit"), "opentrades": st.get("opentrades"),
        "symbol": st.get("symbol"), "winrate": st.get("winrate"),
        "drawdown": st.get("drawdown"), "account": st.get("account"),
        "broker": st.get("broker"), "trading": st.get("trading"),
        "symbols": build_symbols(tok, bot, st),
        "pending": len(COMMANDS.get(tok, {}).get(bot, [])),
    }


@app.route("/state", methods=["GET"])
def get_state():
    tok = request.args.get("token") or MASTER_TOKEN
    want = request.args.get("bot")
    bots = STATES.get(tok, {})

    # --- liiska botyada (had iyo jeer la diraa)
    now = time.time()
    summaries = [bot_summary(tok, b, s) for b, s in sorted(bots.items())
                 if (now - (s.get("updated") or 0)) < FORGET_SECONDS]

    if not bots:
        out = blank_state()
        out.update({"live": False, "reason": "no_data", "age": None,
                    "symbols": [], "bots": [], "bot": None})
        return jsonify(out)

    # --- hal bot oo keliya: muuqaalka buuxa ayaa la tusayaa, ma aha isu-geynta
    if not want and len(bots) == 1:
        want = list(bots.keys())[0]

    # --- bot gaar ah
    if want:
        bot = clean_bot(want)
        st = bots.get(bot)
        if not st:
            out = blank_state()
            out.update({"live": False, "reason": "no_data", "age": None,
                        "symbols": [], "bots": summaries, "bot": bot})
            return jsonify(out)
        out = dict(st)
        live, reason, age = live_info(st)
        out.update({"live": live, "reason": reason, "age": age, "bot": bot,
                    "symbols": build_symbols(tok, bot, st), "bots": summaries})
        return jsonify(out)

    # --- "Dhammaan": ma isku darno balance-ka (waxay noqon kartaa isku akoon
    #     ama laba akoon oo kala duwan). Waxaa la diraa liiska botyada,
    #     iyo kaliya waxa si sax ah loo isku daro: profit + trades furan.
    out = blank_state()
    live_any = any(b["live"] for b in summaries)
    prof = sum((b["profit"] or 0) for b in summaries if b["live"])
    opens = sum((b["opentrades"] or 0) for b in summaries if b["live"])
    syms = []
    for b in summaries:
        for s in b["symbols"]:
            row = dict(s)
            row["bot"] = b["bot"]
            syms.append(row)
    trades = []
    for b, s in sorted(bots.items()):
        for t in (s.get("trades") or []):
            row = dict(t)
            row["bot"] = b
            trades.append(row)

    out.update({
        "live": live_any,
        "reason": "live" if live_any else (summaries[0]["reason"] if summaries else "no_data"),
        "age": min([b["age"] for b in summaries if b["age"] is not None], default=None),
        "bot": None, "bots": summaries, "symbols": syms,
        "profit": round(prof, 2), "opentrades": opens,
        "trades": trades, "aggregate": True,
    })
    return jsonify(out)
# ============ 3) SYMBOLS ============
def build_symbols(tok, bot, st):
    if not st:
        return []
    explicit = st.get("symbols")
    if isinstance(explicit, list) and explicit:
        return explicit

    trades = st.get("trades") or []
    flags = SYMBOL_FLAGS.setdefault(tok, {}).setdefault(bot, {})
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
    bot = clean_bot(request.args.get("bot"))
    st = STATES.get(tok, {}).get(bot)
    return jsonify({"bot": bot,
                    "symbols": build_symbols(tok, bot, st),
                    "flags": SYMBOL_FLAGS.get(tok, {}).get(bot, {})})


# ============ 4) Bootka <- Server: amarrada ============
@app.route("/api/commands", methods=["GET"])
def commands():
    tok = get_token(request)
    if not tok:
        return jsonify({"error": "no token"}), 401
    bot = get_bot(request)
    q = COMMANDS.get(tok, {}).get(bot) or []
    cmd = q.pop(0) if q else ""
    payload = json.dumps({"token": tok, "bot": bot, "command": cmd},
                         separators=(",", ":"))
    return Response(payload, mimetype="application/json")


# ============ 5) Admin -> Server ============
SIMPLE_COMMANDS = {"START", "STOP", "PAUSE", "RESUME", "CLOSE_ALL", "CLOSE_PROFIT"}
STRATEGIES = {"SR", "BB", "EMA", "SMC", "VSA", "RSI", "POC", "SCALP", "GRID"}


def validate(cmd):
    if cmd in SIMPLE_COMMANDS:
        return True, ""
    if cmd.startswith("STRATEGY:"):
        s = cmd.split(":", 1)[1]
        return (True, "") if s in STRATEGIES else (False, "strategy aan la aqoon: " + s)
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

    # bot: mid gaar ah, ama "*" = dhammaan botyada la yaqaan
    raw = (data.get("bot") or "").strip()
    if raw == "*":
        targets = sorted(STATES.get(tok, {}).keys())
        if not targets:
            return jsonify({"error": "bot lama helin"}), 404
    else:
        targets = [clean_bot(raw)]
sent = []
    for bot in targets:
        if cmd.startswith("SYMBOL_ON:"):
            SYMBOL_FLAGS.setdefault(tok, {}).setdefault(bot, {})[cmd.split(":", 1)[1]] = True
        elif cmd.startswith("SYMBOL_OFF:"):
            SYMBOL_FLAGS.setdefault(tok, {}).setdefault(bot, {})[cmd.split(":", 1)[1]] = False

        q = COMMANDS.setdefault(tok, {}).setdefault(bot, [])
        q.append(cmd)
        if len(q) > MAX_QUEUE:
            del q[0:len(q) - MAX_QUEUE]
        sent.append(bot)

    return jsonify({"ok": True, "queued": cmd, "bots": sent})


# ============ 6) DIAG ============
@app.route("/admin/forget_bot", methods=["POST", "OPTIONS"])
def forget_bot():
    """Bot duug ah liiska ka saar. Haddii uu wali wax dirayo, wuu soo laaban doonaa."""
    if request.method == "OPTIONS":
        return ("", 204)
    tok = get_token(request) or MASTER_TOKEN
    data = request.get_json(silent=True) or request.form
    bot = clean_bot(data.get("bot"))
    removed = STATES.get(tok, {}).pop(bot, None) is not None
    COMMANDS.get(tok, {}).pop(bot, None)
    SYMBOL_FLAGS.get(tok, {}).pop(bot, None)
    SEEN.get(tok, {}).pop(bot, None)
    return jsonify({"ok": True, "removed": removed, "bot": bot})


@app.route("/diag", methods=["GET"])
def diag():
    rows = []
    for tok, bots in STATES.items():
        for bot, st in sorted(bots.items()):
            live, reason, age = live_info(st)
            seen = SEEN.get(tok, {}).get(bot, {})
            rows.append({
                "bot": bot,
                "token_preview": (tok[:6] + "..." + tok[-4:]) if len(tok) > 12 else tok,
                "token_matches_env": tok == MASTER_TOKEN,
                "live": live, "reason": reason, "age_seconds": age,
                "updates_received": seen.get("count", 0),
                "last_ip": seen.get("ip", ""),
                "symbols_found": len(build_symbols(tok, bot, st)),
                "pending_commands": len(COMMANDS.get(tok, {}).get(bot, [])),
            })

    default_named = [r for r in rows if r["bot"] == DEFAULT_BOT]

    if not rows:
        hint = ("Boot NA soo gaarin. Hubi: (1) URL-ka /update ee EA-ga, "
                "(2) URL-ka ku jira liiska 'Allow WebRequest' ee MT4/MT5, "
                "(3) in EA-gu shaqeynayo oo AutoTrading la furay.")
    elif not any(r["token_matches_env"] for r in rows):
        hint = ("Boot wuu soo gaaray laakiin token-kiisu kama mid aha AUTH_TOKEN "
                "ee Render. Taasi waa sababta DEMO.")
    elif not any(r["live"] for r in rows):
        hint = ("Xog hore ayaa timid laakiin way duugowday (>%ds)." % STALE_SECONDS)
    elif len(rows) == 1 and default_named:
        hint = ("Hal bot ayaa soo gaaraya, magacna ma laha. Geli InpCloudBotName "
                "EA kasta si ay dashboard-ka ugu kala muuqdaan.")
    else:
        hint = "Wax walba way shaqeynayaan. Botyada la helay: %d" % len(rows)

    return jsonify({
        "build": BUILD,
        "server_time": int(time.time()),
        "env_auth_token_preview": (MASTER_TOKEN[:6] + "..." + MASTER_TOKEN[-4:]
                                   if len(MASTER_TOKEN) > 12 else MASTER_TOKEN),
        "stale_after_seconds": STALE_SECONDS,
        "bots": rows,
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
        ag = (ag * (period - 1) + (d if d > 0 else 0.0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)
def _fetch_closes(symbol, interval, size=60):
    if not TWELVEDATA_KEY:
        return None, None, "no_key"
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                "outputsize": size, "apikey": TWELVEDATA_KEY,
                                "format": "JSON"})
    try:
        with urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q,
                                    timeout=8) as r:
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
    return {"direction": "UP" if score > 0.5 else ("DOWN" if score < -0.5 else "NEUTRAL"),
            "confidence": int(min(abs(score) / 3.0 * 100, 99)),
            "rsi": round(r, 1) if r is not None else None,
            "reasons": reasons}


@app.route("/signals", methods=["GET"])
def signals():
    if not TWELVEDATA_KEY:
        return jsonify({"error": "no_api_key",
                        "hint": "Geli TWELVEDATA_KEY env var"}), 200
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



# ============ 7b) BINARY SIGNALS (on-demand + honest tracking) ============
#
#  Falsafada qaybtan: signal-ka la bixiyo LA CABBIRAA. Signal kasta natiijadiisa
#  si toos ah ayaa la hubiyaa marka muddadu dhammaato, saxnaanta dhabta ahna
#  waxaa la barbar dhigaa break-even-ka payout-kaaga. Ma jirto lambar la
#  qurxiyay - haddii uu edge-gu maqan yahay, si cad ayaa loo tusayaa.

SIGNALS = {}            # token -> [record]
MAX_SIGNALS = 300
SIGNAL_SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "EUR/JPY"]
EXPIRY_CHOICES = {5: "5min", 15: "15min", 60: "1h"}
_price_cache = {}       # symbol -> (ts, price)


def _spot(symbol):
    """Qiimaha hadda. 20 sekan ayaa la kaydiyaa (rate limit)."""
    now = time.time()
    c = _price_cache.get(symbol)
    if c and now - c[0] < 20:
        return c[1], None
    closes, _, err = _fetch_closes(symbol, "1min", 5)
    if err or not closes:
        return None, (err or "no_data")
    _price_cache[symbol] = (now, closes[-1])
    return closes[-1], None


def verify_pending(tok):
    """Signal-adii muddadoodu dhammaatay natiijadooda hubi."""
recs = SIGNALS.get(tok) or []
    now = time.time()
    checked = 0
    for r in recs:
        if r["status"] != "pending" or r["expires_at"] > now:
            continue
        if checked >= 3:          # rate limit: saddex hubin call kasta
            break
        checked += 1
        price, err = _spot(r["symbol"])
        if price is None:
            continue
        r["exit_price"] = price
        move = price - r["entry_price"]
        if move == 0:
            r["status"] = "void"          # isku qiime = broker-ku badanaa waa refund
        elif (move > 0) == (r["direction"] == "BUY"):
            r["status"] = "correct"
        else:
            r["status"] = "wrong"


def signal_stats(tok, payout):
    recs = [r for r in (SIGNALS.get(tok) or []) if r["status"] in ("correct", "wrong")]
    total = len(recs)
    wins = sum(1 for r in recs if r["status"] == "correct")
    acc = (wins / total * 100.0) if total else None
    breakeven = 100.0 / (100.0 + float(payout))* 100.0
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "accuracy": round(acc, 1) if acc is not None else None,
        "breakeven": round(breakeven, 1),
        "payout": float(payout),
        "above_breakeven": (acc is not None and acc >= breakeven),
        "pending": sum(1 for r in (SIGNALS.get(tok) or []) if r["status"] == "pending"),
    }


@app.route("/signal/request", methods=["POST", "OPTIONS"])
def signal_request():
    if request.method == "OPTIONS":
        return ("", 204)
    if not TWELVEDATA_KEY:
        return jsonify({"error": "no_api_key",
                        "hint": "Geli TWELVEDATA_KEY env var ee Render (bilaash: twelvedata.com)"}), 200

    tok = get_token(request) or MASTER_TOKEN
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "EUR/USD").upper()
    if symbol not in SIGNAL_SYMBOLS:
        return jsonify({"error": "symbol aan la aqoon"}), 400
    try:
        expiry = int(data.get("expiry") or 5)
    except (TypeError, ValueError):
        expiry = 5
    if expiry not in EXPIRY_CHOICES:
        expiry = 5

    # Suuqa forex-ku wuu xiran yahay Sabtida iyo Axadda -> xog cusub ma jirto.
    wd = time.gmtime().tm_wday          # 0=Isniin ... 5=Sabti, 6=Axad
    hr = time.gmtime().tm_hour
    closed = (wd == 5) or (wd == 6 and hr < 21) or (wd == 4 and hr >= 21)
    if closed:
        return jsonify({"error": "market_closed",
                        "hint": "Suuqa forex-ku hadda wuu xiran yahay (Sabti/Axad). "
                                "Signal lama bixin karo ilaa suuqu furmo Axada 21:00 GMT."}), 200

    closes, last_dt, err = _fetch_closes(symbol, EXPIRY_CHOICES[expiry], 60)
    if err or not closes or len(closes) < 22:
        return jsonify({"error": err or "xog kuma filna",
                        "hint": "Xogta qiimaha lama helin: " + str(err or "kuma filna")}), 200

    sig = _compute_signal(closes)
    if sig["direction"] == "NEUTRAL":
        return jsonify({"neutral": True, "symbol": symbol, "expiry": expiry,
                        "reasons": sig["reasons"],
                        "message": "Suuqu isku dheelitiran yahay - signal lama bixinayo."}), 200

    now = time.time()
    rec = {
        "id": int(now * 1000) % 10**10,
        "symbol": symbol, "expiry": expiry,
        "direction": "BUY" if sig["direction"] == "UP" else "SELL",
        "score": sig["confidence"], "reasons": sig["reasons"], "rsi": sig["rsi"],
        "entry_price": closes[-1], "exit_price": None,
        "created": int(now), "expires_at": now + expiry * 60,
        "status": "pending", "bar_time": last_dt,
    }
    q = SIGNALS.setdefault(tok, [])
    q.insert(0, rec)
    del q[MAX_SIGNALS:]
    return jsonify({"signal": rec})


@app.route("/signal/history", methods=["GET"])

