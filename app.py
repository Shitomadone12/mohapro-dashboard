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
  - HAL FAYL: dashboard-ka wuxuu ku dhex jiraa faylkan (EMBEDDED_HTML). Haddii
    aad dhigto dashboard.html isla galka, kaas ayaa mudnaan leh — haddii kale
    nuqulka gudaha ayaa la isticmaalaa. Marnaba ma jabi karo.

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
EMBEDDED_HTML = """<!DOCTYPE html>
<html lang="so">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<meta name="theme-color" content="#0a0a0f">
<title>MOHA PRO — Bot Control</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='78' font-size='78'>&#9889;</text></svg>">
<style>
:root{
  --bg:#0a0a0f; --surface:#15151e; --surface-2:#1c1c27; --line:#282833; --line-2:#34343f;
  --ink:#fff; --ink-2:#a6a6ba; --muted:#6f6f85;
  --orange:#f0872a; --orange-2:#ff9a3c; --orange-d:#c9661a;
  --good:#22b455; --good-ink:#3ad46e; --bad:#e0524f; --bad-ink:#f0736f; --info:#3987e5;
  --font:system-ui,-apple-system,"Segoe UI",sans-serif; --mono:ui-monospace,"Roboto Mono",monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--font);padding-bottom:80px;-webkit-font-smoothing:antialiased}
.num{font-variant-numeric:tabular-nums;font-family:var(--mono)}
.wrap{max-width:820px;margin:0 auto;padding:0 15px}
button{font-family:inherit;cursor:pointer}
:focus-visible{outline:2px solid var(--orange);outline-offset:2px}

/* ===== HERO ===== */
.hero{position:relative;width:100%;overflow:hidden;min-height:210px;background:#120a06}
.hero img{width:100%;height:auto;display:block;min-height:210px;object-fit:cover}
.hero-grad{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,10,15,.3) 0%,rgba(10,10,15,0) 35%,rgba(10,10,15,.55) 70%,var(--bg) 100%)}
.hero-top{position:absolute;top:14px;left:0;right:0;padding:0 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:7px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.5);backdrop-filter:blur(6px);color:#fff}
.chip:active{opacity:.6}
.st-pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;padding:7px 13px;border-radius:999px;border:1px solid rgba(255,255,255,.15);background:rgba(0,0,0,.55);backdrop-filter:blur(6px);letter-spacing:.4px}
.st-pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.st-pill.on{color:var(--good-ink)} .st-pill.on .dot{background:var(--good-ink);animation:pulse 2s infinite}
.st-pill.demo{color:var(--orange-2)} .st-pill.demo .dot{background:var(--orange-2)}
.st-pill.off{color:var(--bad-ink)} .st-pill.off .dot{background:var(--bad-ink)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,180,85,.5)}70%{box-shadow:0 0 0 7px rgba(34,180,85,0)}100%{box-shadow:0 0 0 0 rgba(34,180,85,0)}}
.hero-title{position:absolute;left:18px;bottom:16px;right:18px}
.hero-title h1{font-size:36px;font-weight:800;letter-spacing:-1px;line-height:.95;text-shadow:0 3px 16px rgba(0,0,0,.8)}
.hero-title h1 .v{color:var(--orange)}
.hero-title p{font-size:12px;color:#e0bfa0;margin-top:6px;font-weight:500;text-shadow:0 2px 8px rgba(0,0,0,.9)}

/* ===== BOT LINE ===== */
.botline{display:flex;align-items:center;gap:9px;margin:16px 2px 12px;font-size:14px;font-weight:600}
.botline .bd{width:10px;height:10px;border-radius:50%;background:var(--bad)}
.botline.run .bd{background:var(--good);box-shadow:0 0 0 4px rgba(34,180,85,.2)}
.botline .clock{margin-left:auto;font-family:var(--mono);font-size:12.5px;color:var(--muted);font-weight:500}

/* ===== ACTIONS ===== */
.actions{display:grid;grid-template-columns:1fr 1fr 1fr;background:linear-gradient(135deg,var(--orange-2),var(--orange-d));border-radius:16px;overflow:hidden;margin-bottom:14px}
.actions button{border:none;background:transparent;color:#1a0e00;padding:15px 6px;display:flex;flex-direction:column;align-items:center;gap:5px;position:relative;transition:background .15s}
.actions button:not(:last-child){border-right:1px solid rgba(0,0,0,.15)}
.actions button:active{background:rgba(0,0,0,.12)}
.actions button svg{width:23px;height:23px;stroke:#1a0e00;fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.actions button .al{font-size:12.5px;font-weight:700;letter-spacing:.4px}
.cmd-note{text-align:center;font-size:12px;color:var(--muted);min-height:16px;margin-bottom:14px}

/* ===== BANNER ===== */
.banner{border-radius:12px;padding:11px 14px;font-size:12.5px;font-weight:600;text-align:center;margin-bottom:15px;line-height:1.5}
.banner.demo{background:rgba(240,135,42,.13);border:1px solid rgba(240,135,42,.4);color:var(--orange-2)}
.banner.live{background:rgba(34,180,85,.13);border:1px solid rgba(34,180,85,.45);color:var(--good-ink)}
.banner a{color:inherit}

/* ===== TABS ===== */
.tabs{display:flex;background:var(--surface);border:1px solid var(--line);border-radius:11px;padding:3px;gap:3px;margin-bottom:16px}
.tabs div{flex:1;text-align:center;font-size:13px;font-weight:600;color:var(--ink-2);padding:9px 0;border-radius:8px;cursor:pointer;transition:.15s}
.tabs div.on{background:var(--orange);color:#0a0a0f}
.pane{display:none} .pane.on{display:block}

/* ===== SECTIONS ===== */
.sec-h{font-size:13px;color:var(--orange);font-weight:700;margin:0 2px 12px;display:flex;align-items:center;gap:8px}
.sec-h::before{content:"";width:4px;height:14px;border-radius:2px;background:var(--orange)}
.sec-h .rt{margin-left:auto;font-size:11.5px;color:var(--muted);font-weight:500}
.block{margin-bottom:18px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:15px;padding:14px}

/* ===== KPI ===== */
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.kpi .lbl{font-size:11px;color:var(--muted);font-weight:600}
.kpi .val{font-size:19px;font-weight:700;margin-top:6px;letter-spacing:-.4px}
.up{color:var(--good-ink)} .down{color:var(--bad-ink)} .or{color:var(--orange)}
.kpi.accent{background:linear-gradient(135deg,rgba(240,135,42,.12),var(--surface));border-color:rgba(240,135,42,.3)}

/* ===== SYMBOLS ===== */
.symrow{display:flex;align-items:center;gap:12px;padding:13px 2px;border-bottom:1px solid var(--line)}
.symrow:last-child{border-bottom:none}
.symrow .si{flex:1;min-width:0}
.symrow .sn{font-size:15px;font-weight:700;letter-spacing:-.2px}
.symrow .sm{font-size:12px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.symrow .sm b{font-weight:600}
.sw{width:44px;height:26px;border-radius:13px;background:var(--line-2);border:none;padding:0;position:relative;flex-shrink:0;transition:background .18s}
.sw::after{content:"";position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .18s}
.sw.on{background:var(--good)}
.sw.on::after{transform:translateX(18px)}
.sym-empty{color:var(--muted);text-align:center;padding:26px 10px;font-size:13px;line-height:1.6}
.addrow{display:flex;gap:8px;margin-top:14px}
.addrow input{flex:1;background:var(--surface-2);border:1px solid var(--line);color:var(--ink);border-radius:9px;padding:10px 12px;font-family:var(--mono);font-size:13px;text-transform:uppercase}
.addrow input::placeholder{color:var(--muted);text-transform:none;font-family:var(--font)}
.addrow button{background:var(--orange);color:#0a0a0f;border:none;border-radius:9px;padding:0 18px;font-weight:700;font-size:13px}

/* ===== STRATEGY ===== */
.strats{display:flex;gap:9px;flex-wrap:wrap}
.strat{padding:9px 17px;border-radius:999px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink-2);font-size:13px;font-weight:600;transition:all .15s}
.strat.active{background:linear-gradient(135deg,var(--orange-2),var(--orange-d));color:#1a0e00;border-color:var(--orange)}

/* ===== TABLE ===== */
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{padding:10px;border-bottom:1px solid rgba(255,255,255,.05);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl tbody tr:last-child td{border-bottom:none}
.tbl .sym{font-weight:700}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-size:10.5px;font-weight:700}
.badge.buy{color:var(--good-ink);background:rgba(34,180,85,.14)}
.badge.sell{color:var(--bad-ink);background:rgba(224,82,79,.14)}
.badge.strat{color:var(--orange);background:rgba(240,135,42,.14)}
.badge.op{color:var(--info);background:rgba(57,135,229,.16)}
.badge.cl{color:var(--muted);background:rgba(119,119,140,.16)}
.pl-pos{color:var(--good-ink);font-weight:700} .pl-neg{color:var(--bad-ink);font-weight:700}
.trow{cursor:pointer} .trow:active{background:rgba(240,135,42,.08)}
.carcell{width:20px;padding-right:0!important} .car{color:var(--orange);font-size:12px}
.detrow td{padding:0!important;border:none!important}
.tdet{background:var(--surface-2);border-radius:0 0 10px 10px;padding:12px 14px!important}
.det-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.dc{background:#13131c;border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.dc span{display:block;font-size:10px;color:var(--muted);margin-bottom:4px;font-weight:600}
.dc b{font-size:14px;font-variant-numeric:tabular-nums}
@media(max-width:640px){.det-grid{grid-template-columns:repeat(2,1fr)}}
.ot-empty{color:var(--muted);text-align:center;padding:20px;font-size:13px}

/* ===== JOURNAL ===== */
.jhero{display:flex;gap:10px;margin-bottom:13px}
.jhero .jg{flex:1;background:linear-gradient(135deg,rgba(34,227,122,.13),var(--surface));border:1px solid rgba(34,227,122,.5);border-radius:13px;padding:12px 14px}
.jhero .jg.d{background:linear-gradient(135deg,rgba(240,135,42,.12),var(--surface));border-color:var(--orange)}
.jhero .jl{font-size:11px;color:var(--ink-2);font-weight:600}
.jhero .jv{font-size:21px;font-weight:800;margin-top:3px;color:var(--good-ink)}
.jhero .jv.or{color:var(--orange)} .jhero .jv.neg{color:var(--bad)}
.jhero .js{font-size:11px;color:var(--muted);margin-top:2px}
.jmon-h{font-size:12px;color:var(--ink-2);font-weight:600;margin:6px 0 8px}
#j_bars svg{display:block;width:100%;height:130px}
.jmrow{display:flex;justify-content:space-between;font-size:9.5px;color:var(--muted);margin-top:5px;padding:0 2px}
.jgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px;margin-top:14px}
.jst{background:#0f0f17;border:1px solid var(--line);border-radius:11px;padding:10px 11px}
.jst .jl2{font-size:10px;color:var(--ink-2);font-weight:600}
.jst .jv2{font-size:15px;font-weight:700;margin-top:3px}
.jst .jv2.g{color:var(--good-ink)} .jst .jv2.r{color:var(--bad-ink)} .jst .jv2.o{color:var(--orange)}

/* ===== NAV ===== */
.navbar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;justify-content:space-around;background:rgba(14,14,22,.96);border-top:1px solid var(--line);padding:7px 4px calc(7px + env(safe-area-inset-bottom));backdrop-filter:blur(10px)}
.navbar button{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:5px 2px;background:none;border:none;color:var(--muted);font-size:10.5px;font-weight:600}
.navbar button svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.navbar button.active{color:var(--orange)}
.foot{text-align:center;color:var(--muted);font-size:10.5px;padding:8px 0 14px}
</style>
</head>
<body>

<div class="hero" id="top">
  <img id="banner-img" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='800' height='300'%3E%3Crect width='800' height='300' fill='%23120a06'/%3E%3C/svg%3E" alt="MOHA PRO">
  <input type="file" id="img-input" accept="image/*" style="display:none">
  <div class="hero-grad"></div>
  <div class="hero-top">
    <span id="status" class="st-pill off"><span class="dot"></span>OFFLINE</span>
    <button class="chip" id="change-btn">Beddel sawirka</button>
  </div>
  <div class="hero-title">
    <h1>MOHA PRO <span class="v">v56</span></h1>
    <p>MT4 / MT5 · Bot control</p>
  </div>
</div>

<div class="wrap">

  <div class="botline" id="botline"><span class="bd"></span><span>Bot <span id="botstate">Stopped</span></span><span class="clock" id="clock">--:--:--</span></div>

  <div class="actions">
    <button onclick="sendCmd('START')"><svg viewBox="0 0 24 24"><polygon points="6 4 20 12 6 20 6 4"/></svg><span class="al">START</span></button>
    <button onclick="sendCmd('STOP')"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg><span class="al">STOP</span></button>
    <button onclick="if(confirm('Xir dhammaan trade-yada?'))sendCmd('CLOSE_ALL')"><svg viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg><span class="al">CLOSE</span></button>
  </div>
  <div class="cmd-note" id="cmdNote"></div>

  <div class="banner demo" id="dataMode">Xogta lama helin weli</div>

  <div class="tabs" id="tabs">
    <div data-t="overview" class="on">Guud</div>
    <div data-t="symbols">Symbols</div>
    <div data-t="chart">Chart</div>
    <div data-t="journal">Journal</div>
  </div>

  <!-- ===== OVERVIEW ===== -->
  <section class="pane on" data-p="overview">
    <div class="block">
      <h2 class="sec-h">Akoonka <span class="rt" id="symbol">—</span></h2>
      <div class="kpis">
        <div class="card kpi accent"><div class="lbl">Balance</div><div class="val num" id="k_balance">$0.00</div></div>
        <div class="card kpi"><div class="lbl">Equity</div><div class="val num" id="k_equity">$0.00</div></div>
        <div class="card kpi"><div class="lbl">Faa'iido</div><div class="val num up" id="k_profit">+$0.00</div></div>
        <div class="card kpi"><div class="lbl">Win rate</div><div class="val num or" id="k_wr">0.0%</div></div>
        <div class="card kpi"><div class="lbl">Drawdown</div><div class="val num down" id="k_dd">0.0%</div></div>
        <div class="card kpi"><div class="lbl">Furan</div><div class="val num" id="k_open">0</div></div>
      </div>
    </div>

    <div class="card block">
      <h2 class="sec-h">Xeeladda <span class="rt">guji si aad u beddesho</span></h2>
      <div class="strats" id="strats">
        <button class="strat" data-s="SR">SR</button><button class="strat" data-s="BB">BB</button>
        <button class="strat" data-s="EMA">EMA</button><button class="strat" data-s="SMC">SMC</button>
        <button class="strat" data-s="VSA">VSA</button><button class="strat" data-s="POC">POC</button>
      </div>
    </div>

    <div class="card block">
      <h2 class="sec-h">Ganacsiyada <span class="rt" id="trades-count">0</span></h2>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th></th><th>Lammaane</th><th>Nooc</th><th>Xeelad</th><th>P&amp;L</th><th>Xaalad</th></tr></thead>
          <tbody id="tradesBody"><tr><td colspan="6" class="ot-empty">Trade ma jiro</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- ===== SYMBOLS ===== -->
  <section class="pane" data-p="symbols">
    <div class="card block">
      <h2 class="sec-h">Symbols <span class="rt" id="sym-count">0 firfircoon</span></h2>
      <div id="symList"><div class="sym-empty">Symbol lama helin weli.<br>Marka bootku trade furo, halkan ayuu ka soo muuqan doonaa.</div></div>
      <div class="addrow">
        <input id="symInput" placeholder="Ku dar symbol, tusaale XAUUSD" maxlength="16">
        <button onclick="addSymbol()">Ku dar</button>
      </div>
    </div>
    <div class="card block">
      <h2 class="sec-h">Fiiro gaar ah</h2>
      <p style="font-size:13px;color:var(--ink-2);line-height:1.7">
        Damintu waxay amar u dirtaa bootka. EA-gu wuxuu qaataa markuu xiga poll-ka
        (3–10 sekan). Trade-yada horeba u furan ma xirmaan — isticmaal CLOSE.
      </p>
    </div>
  </section>

  <!-- ===== CHART ===== -->
  <section class="pane" data-p="chart">
    <div class="card block">
      <h2 class="sec-h">Chart <span class="rt" id="chartsym">GBPUSD · M5</span></h2>
      <div id="tvchart" style="height:340px;border-radius:10px;overflow:hidden"></div>
    </div>
  </section>

  <!-- ===== JOURNAL ===== -->
  <section class="pane" data-p="journal">
    <div class="card block">
      <h2 class="sec-h">Journal <span class="rt">ilaa 1 sano</span></h2>
      <div class="tabs" id="jseg" style="margin-bottom:13px">
        <div data-p="today">Maanta</div><div data-p="week" class="on">Toddobaad</div>
        <div data-p="month">Bishii</div><div data-p="year">Guud</div>
      </div>
      <div class="jhero">
        <div class="jg"><div class="jl">Faa'iido</div><div class="jv" id="j_gain">—</div><div class="js" id="j_gainabs">—</div></div>
        <div class="jg d"><div class="jl">Balance</div><div class="jv or" id="j_bal">—</div><div class="js" id="j_eq">—</div></div>
      </div>
      <div class="jmon-h">Waxqabadka bille</div>
      <div id="j_bars"><svg viewBox="0 0 420 130" preserveAspectRatio="none" id="j_svg"></svg>
        <div class="jmrow"><span>J</span><span>F</span><span>M</span><span>A</span><span>M</span><span>J</span><span>J</span><span>A</span><span>S</span><span>O</span><span>N</span><span>D</span></div>
      </div>
      <div class="jgrid" id="j_grid"></div>
    </div>
  </section>

  <div class="foot">MOHA PRO v56 · Bot Control</div>
</div>

<nav class="navbar" id="nav">
  <button data-t="overview" class="active"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg>Guud</button>
  <button data-t="symbols"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/></svg>Symbols</button>
  <button data-t="chart"><svg viewBox="0 0 24 24"><path d="M3 15l5-5 4 4 8-8"/></svg>Chart</button>
  <button data-t="journal"><svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><line x1="8" y1="9" x2="16" y2="9"/><line x1="8" y1="13" x2="16" y2="13"/></svg>Journal</button>
  <button id="nav-img"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.6"/><polyline points="21 15 16 10 5 21"/></svg>Sawir</button>
</nav>

<script>
const $=id=>document.getElementById(id);
const TOKEN=new URLSearchParams(location.search).get('token')||"MohaPro_Live_2026_MySecret";
const POLL_MS=5000;
const money=n=>{const v=+n||0;return (v<0?'-$':'$')+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});};

/* clock */
function tick(){$('clock').textContent=new Date().toLocaleTimeString('en-GB');}
setInterval(tick,1000);tick();

/* ===== TABS ===== */
function showTab(t){
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.dataset.p===t));
  document.querySelectorAll('#tabs div').forEach(d=>d.classList.toggle('on',d.dataset.t===t));
  document.querySelectorAll('#nav button[data-t]').forEach(b=>b.classList.toggle('active',b.dataset.t===t));
  if(t==='chart')initChart(LAST_SYMBOL);
  window.scrollTo({top:0,behavior:'smooth'});
}
document.querySelectorAll('#tabs div').forEach(d=>d.addEventListener('click',()=>showTab(d.dataset.t)));
document.querySelectorAll('#nav button[data-t]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.t)));

/* ===== CHART ===== */
let tvStarted=false,tvSym='',LAST_SYMBOL='GBPUSD';
function tvSymbolFor(raw){let s=(raw||'GBPUSD').toUpperCase().replace(/[^A-Z].*$/,'');if(s.length<6)s='GBPUSD';return 'FX:'+s.slice(0,6);}
function loadTV(cb){if(window.TradingView){cb();return;}if(!tvStarted){tvStarted=true;const s=document.createElement('script');s.src='https://s3.tradingview.com/tv.js';s.onload=cb;document.head.appendChild(s);}else{setTimeout(()=>loadTV(cb),300);}}
function initChart(raw){
  const sym=tvSymbolFor(raw);if(sym===tvSym)return;tvSym=sym;
  $('chartsym').textContent=sym.replace('FX:','')+' · M5';
  loadTV(()=>{const el=$('tvchart');if(!el||!window.TradingView)return;el.innerHTML='';
    new TradingView.widget({container_id:'tvchart',autosize:true,symbol:sym,interval:'5',timezone:'Etc/UTC',theme:'dark',style:'1',locale:'en',toolbar_bg:'#15151e',hide_side_toolbar:true,allow_symbol_change:true});});
}

/* ===== BANNER ===== */
(function(){const img=$('banner-img'),inp=$('img-input');
  try{const s=localStorage.getItem('moha_banner');if(s)img.src=s;}catch(e){}
  const open=()=>inp.click();
  $('change-btn').addEventListener('click',open);$('nav-img').addEventListener('click',open);
  inp.addEventListener('change',e=>{const f=e.target.files&&e.target.files[0];if(!f)return;const r=new FileReader();
    r.onload=ev=>{img.src=ev.target.result;try{localStorage.setItem('moha_banner',ev.target.result);}catch(x){}};r.readAsDataURL(f);});
})();

/* ===== STRATEGY ===== */
document.querySelectorAll('.strat').forEach(el=>el.addEventListener('click',()=>{
  sendCmd('STRATEGY:'+el.dataset.s);
  document.querySelectorAll('.strat').forEach(x=>x.classList.remove('active'));el.classList.add('active');}));

/* ===== COMMANDS ===== */
async function sendCmd(cmd){
  const note=$('cmdNote');note.style.color='var(--muted)';note.textContent='Diraya '+cmd+'…';
  try{
    const r=await fetch('/admin/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,command:cmd})});
    const d=await r.json();
    if(d.ok){note.style.color='var(--good-ink)';note.textContent=cmd+' waa la diray — bootku ~3s gudahood ayuu qaadanayaa';}
    else{note.style.color='var(--bad-ink)';note.textContent=d.error||'Amarka lama aqbalin';}
  }catch(e){note.style.color='var(--bad-ink)';note.textContent='Server-ka lama gaari karin';}
}

/* ===== SYMBOLS ===== */
let SYMS=[];
function renderSymbols(list){
  SYMS=list||[];const box=$('symList');
  const on=SYMS.filter(s=>s.enabled!==false).length;
  $('sym-count').textContent=on+' firfircoon';
  if(!SYMS.length){box.innerHTML='<div class="sym-empty">Symbol lama helin weli.<br>Marka bootku trade furo, halkan ayuu ka soo muuqan doonaa.</div>';return;}
  box.innerHTML='';
  SYMS.forEach(s=>{
    const row=document.createElement('div');row.className='symrow';
    const pnl=+s.open_pnl||0;
    const meta=s.open>0
      ? s.open+' furan · <b class="'+(pnl>=0?'pl-pos':'pl-neg')+'">'+(pnl>=0?'+':'')+money(Math.abs(pnl))+'</b>'
      : (s.enabled===false?'Damisan':'Bannaan');
    const strat=s.strategies&&s.strategies.length?' · '+s.strategies.join(', '):'';
    row.innerHTML='<div class="si"><div class="sn">'+esc(s.symbol)+'</div><div class="sm">'+meta+strat+'</div></div>';
    const sw=document.createElement('button');
    sw.className='sw'+(s.enabled===false?'':' on');
    sw.setAttribute('aria-label',(s.enabled===false?'Fur ':'Dami ')+s.symbol);
    sw.addEventListener('click',()=>{
      const turnOn=!sw.classList.contains('on');
      sw.classList.toggle('on',turnOn);
      sendCmd((turnOn?'SYMBOL_ON:':'SYMBOL_OFF:')+s.symbol);
    });
    row.appendChild(sw);box.appendChild(row);
  });
}
function addSymbol(){
  const v=($('symInput').value||'').trim().toUpperCase();
  if(!v){$('symInput').focus();return;}
  sendCmd('SYMBOL_ON:'+v);$('symInput').value='';
  setTimeout(poll,600);
}

/* ===== TRADES ===== */
function esc(s){const d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
function f5(v){return (+v||0).toFixed(5);}
function detHTML(t,open){
  return '<div class="det-grid">'+
    '<div class="dc"><span>Entry</span><b>'+f5(t.entry)+'</b></div>'+
    '<div class="dc"><span>'+(open?'Hadda':'Close')+'</span><b>'+f5(t.cur)+'</b></div>'+
    '<div class="dc"><span>Stop loss</span><b class="pl-neg">'+f5(t.sl)+'</b></div>'+
    '<div class="dc"><span>Take profit</span><b class="pl-pos">'+f5(t.tp)+'</b></div>'+
    '<div class="dc"><span>Lot</span><b>'+(+t.lot||0).toFixed(2)+'</b></div>'+
    '<div class="dc"><span>P&L</span><b class="'+((+t.profit||0)>=0?'pl-pos':'pl-neg')+'">'+money(+t.profit||0)+'</b></div>'+
  '</div>';
}
function renderTrades(trades){
  const tb=$('tradesBody'),cnt=$('trades-count');
  if(!trades||!trades.length){tb.innerHTML='<tr><td colspan="6" class="ot-empty">Trade ma jiro weli</td></tr>';cnt.textContent='0';return;}
  cnt.textContent=trades.length;tb.innerHTML='';
  trades.slice(0,20).forEach((t,i)=>{
    const p=+t.profit||0,buy=(t.type||'').toUpperCase()==='BUY',open=(t.st||'')==='OPEN';
    const tr=document.createElement('tr');tr.className='trow';
    tr.innerHTML='<td class="carcell"><span class="car" id="car'+i+'">&#9656;</span></td>'+
      '<td class="sym">'+esc(t.sym)+'</td>'+
      '<td><span class="badge '+(buy?'buy':'sell')+'">'+esc(t.type)+'</span></td>'+
      '<td><span class="badge strat">'+esc(t.strat)+'</span></td>'+
      '<td class="'+(p>=0?'pl-pos':'pl-neg')+'">'+(p>=0?'+':'')+money(p)+'</td>'+
      '<td><span class="badge '+(open?'op':'cl')+'">'+(open?'FURAN':'XIRAN')+'</span></td>';
    tr.addEventListener('click',()=>{const d=$('det'+i),c=$('car'+i);const sh=d.style.display==='none';
      d.style.display=sh?'table-row':'none';if(c)c.innerHTML=sh?'&#9662;':'&#9656;';});
    tb.appendChild(tr);
    const dr=document.createElement('tr');dr.id='det'+i;dr.style.display='none';dr.className='detrow';
    dr.innerHTML='<td colspan="6" class="tdet">'+detHTML(t,open)+'</td>';
    tb.appendChild(dr);
  });
}

/* ===== JOURNAL ===== */
let J_DATA=null,J_PERIOD='week';
function renderJournal(j){
  if(!j)return;
  const per={today:['Maanta',j.today],week:['Toddobaad',j.week],month:['Bishii',j.month],year:['Guud',j.year]};
  const sel=per[J_PERIOD]||per.week;const pnl=+sel[1]||0;
  const gv=$('j_gain'),ga=$('j_gainabs');
  gv.textContent=J_PERIOD==='year'?((j.gainPct>=0?'+':'')+(+j.gainPct).toFixed(1)+'%'):((pnl>=0?'+':'')+money(Math.abs(pnl)));
  gv.className='jv'+(pnl<0?' neg':'');
  ga.textContent=sel[0]+': '+(pnl>=0?'+':'')+money(Math.abs(pnl));
  $('j_bal').textContent=money(j.balance);$('j_eq').textContent='Equity '+money(j.equity);
  const m=Array.isArray(j.monthly)?j.monthly:new Array(12).fill(0);
  const mx=Math.max(1,...m.map(v=>Math.abs(+v||0)));
  const W=420,H=130,mid=H/2,bw=24,gap=(W-bw*12)/13;
  let svg='<line x1="0" y1="'+mid+'" x2="'+W+'" y2="'+mid+'" stroke="#282833" stroke-width="1"/>';
  m.forEach((v,i)=>{const val=+v||0,h=Math.max(3,Math.abs(val)/mx*(mid-8)),x=gap+i*(bw+gap);
    svg+='<rect x="'+x.toFixed(1)+'" y="'+(val>=0?mid-h:mid).toFixed(1)+'" width="'+bw+'" height="'+h.toFixed(1)+'" rx="2" fill="'+(val>=0?'#22e37a':'#e0524f')+'"/>';});
  $('j_svg').innerHTML=svg;
  const g=[['Win rate',(+j.winRate).toFixed(1)+'%','g'],['Profit factor',(+j.pf).toFixed(2),'o'],['Trades',(+j.trades).toLocaleString(),''],
    ['Pips',(j.pips>=0?'+':'')+Math.round(j.pips).toLocaleString(),j.pips>=0?'g':'r'],['Avg win','+'+money(Math.abs(j.avgWin)),'g'],
    ['Avg loss','-'+money(Math.abs(j.avgLoss)),'r'],['Drawdown',(+j.dd).toFixed(1)+'%','r'],
    ['Best','+'+money(Math.abs(j.best)),'g'],['Worst','-'+money(Math.abs(j.worst)),'r']];
  $('j_grid').innerHTML=g.map(x=>'<div class="jst"><div class="jl2">'+x[0]+'</div><div class="jv2 '+x[2]+'">'+x[1]+'</div></div>').join('');
}
document.querySelectorAll('#jseg div').forEach(el=>el.addEventListener('click',()=>{
  J_PERIOD=el.dataset.p;document.querySelectorAll('#jseg div').forEach(x=>x.classList.remove('on'));el.classList.add('on');
  if(J_DATA)renderJournal(J_DATA);}));

/* ===== STATE ===== */
const DEMO={balance:10482.55,equity:10531.20,profit:182.55,winrate:76.2,drawdown:3.10,opentrades:2,symbol:"GBPUSD",
  trades:[
    {sym:"GBPUSD",type:"BUY",strat:"VSA",profit:64.20,st:"OPEN",entry:1.27140,cur:1.27204,sl:1.26950,tp:1.27520,lot:0.20},
    {sym:"USDJPY",type:"SELL",strat:"SR",profit:16.93,st:"OPEN",entry:157.320,cur:157.280,sl:157.520,tp:156.920,lot:0.15},
    {sym:"GBPUSD",type:"BUY",strat:"VSA",profit:88.30,st:"CLOSED",entry:1.26980,cur:1.27290,sl:1.26800,tp:1.27340,lot:0.20},
    {sym:"AUDUSD",type:"SELL",strat:"VSA",profit:137.09,st:"CLOSED",entry:0.66420,cur:0.66150,sl:0.66600,tp:0.66060,lot:0.30},
    {sym:"USDCAD",type:"SELL",strat:"SR",profit:-48.34,st:"CLOSED",entry:1.36540,cur:1.36680,sl:1.36700,tp:1.36180,lot:0.15}
  ],
  symbols:[
    {symbol:"GBPUSD",open:1,open_pnl:64.20,strategies:["VSA"],enabled:true},
    {symbol:"USDJPY",open:1,open_pnl:16.93,strategies:["SR"],enabled:true},
    {symbol:"AUDUSD",open:0,open_pnl:0,strategies:["VSA"],enabled:true},
    {symbol:"USDCAD",open:0,open_pnl:0,strategies:["SR"],enabled:false}
  ],
  journal:{gainPct:18.4,balance:11842.55,equity:11905.20,today:82.55,week:1842.55,month:1842.55,year:1842.55,
    trades:1078,winRate:76.2,pf:1.62,pips:4820,avgWin:34,avgLoss:-21,best:137,worst:-50,dd:3.1,
    monthly:[320,540,-180,410,730,-90,560,880,210,-210,640,780]}};

const REASONS={
  no_data:"Bootku weli xog ma soo dirin. Fur /diag si aad u aragto sababta.",
  stale:"Xogtii ugu dambeysay way duugowday. Bootku ma shaqaynayo ama server-ku wuu hurday.",
  offline:"Server-ka lama gaari karin."
};

function setStatus(s,reason,age){
  const el=$('status'),bl=$('botline'),bs=$('botstate'),dm=$('dataMode');
  el.className='st-pill '+s;
  if(s==='on'){
    el.innerHTML='<span class="dot"></span>LIVE';bl.classList.add('run');bs.textContent='Shaqeynaya';
    dm.className='banner live';dm.textContent='Xog dhab ah — la cusboonaysiiyay '+(age!=null?age+'s ka hor':'hadda');
  }else{
    el.innerHTML='<span class="dot"></span>'+(s==='demo'?'DEMO':'OFFLINE');
    bl.classList.remove('run');bs.textContent='Joogsan';
    dm.className='banner demo';
    dm.innerHTML='Xog tusaale ah — lacagtaadu maaha.<br>'+(REASONS[reason]||REASONS.no_data)+
      ' <a href="/diag" target="_blank">Fur /diag</a>';
  }
}

function applyState(d){
  if(d.balance!=null)$('k_balance').textContent=money(d.balance);
  if(d.equity!=null)$('k_equity').textContent=money(d.equity);
  if(d.profit!=null){const p=+d.profit,el=$('k_profit');el.textContent=(p>=0?'+':'')+money(Math.abs(p));el.className='val num '+(p>=0?'up':'down');}
  if(d.winrate!=null)$('k_wr').textContent=(+d.winrate).toFixed(1)+'%';
  if(d.drawdown!=null)$('k_dd').textContent=(+d.drawdown).toFixed(2)+'%';
  if(d.opentrades!=null)$('k_open').textContent=d.opentrades;
  if(d.symbol){$('symbol').textContent=d.symbol;LAST_SYMBOL=d.symbol;}
  renderTrades(d.trades);
  renderSymbols(d.symbols);
  if(d.journal){J_DATA=d.journal;renderJournal(d.journal);}
}

function showDemo(reason){applyState(DEMO);setStatus('demo',reason||'no_data');}

async function poll(){
  try{
    const r=await fetch('/state?token='+encodeURIComponent(TOKEN),{cache:'no-store'});
    if(!r.ok)throw 0;
    const d=await r.json();
    if(d.live){applyState(d);setStatus('on','live',d.age);}
    else{showDemo(d.reason);}
  }catch(e){showDemo('offline');}
}
showDemo();poll();setInterval(poll,POLL_MS);
</script>
</body>
</html>
"""


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
        # Faylka dibadda ah lama helin -> isticmaal nuqulka ku dhex jira faylkan.
        return EMBEDDED_HTML


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
