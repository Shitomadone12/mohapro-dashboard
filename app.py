# -*- coding: utf-8 -*-
"""
MOHA PRO — Cloud Dashboard v2 (multi-user, MT5 account login)
=============================================================
Hal fayl. Flask + SQLite/Postgres. Render.com iyo Railway diyaar.

ENV:
  SECRET_KEY      - random long string (session cookie signing). WAAJIB.
  CLOUD_TOKEN     - waa inuu la mid noqdo Cloud_Auth_Token ee EA-da. WAAJIB.
  ADMIN_ACCOUNT   - lambarka MT5 ee milkiilaha (Moha). WAAJIB.
  ADMIN_PASSWORD  - password-ka admin-ka marka ugu horreysa. WAAJIB.
  DB_PATH         - default /var/data/mohapro.db (haddii /var/data jiro) ama ./mohapro.db

EA endpoints (token auth):
  POST /update            <- SendToCloud()
  POST /trades            <- closed-trade push
  GET  /api/commands      <- CheckCloudCommands()

Web (session auth):
  /login /register /logout /dashboard /admin
  GET  /api/state         -> xogta account-ka user-ka
  POST /api/command       -> amar loo diro EA-da
  v12.25: 🪜 GRID STOP (grid · grid_h kv · SET:GR*) · 💱 lamaane fl bit 4 = GRID - EA v71.0
  v12.24: 📏 CABBIR LAMAANE (tick.cb · SET:CBON/CBTF/CBN/CBSQ) · lamaane kasta TICK / BASKET + cabbir (pairs fl · cm) - EA v70.9
  v12.23: wajiga hore (sawir shaashad buuxda · ⏻ MT5 SHID/DAMI · ✕ XIDH · ⋯) · 🎵 player la jiidi karo
  v12.22: 🎯 ZONE YAR (zn) · 💱 LAMAANAHA (pairs · SET:PAIRS=h..) - EA v70.8
  GET/POST /api/music     -> v12.20: 🎵 liiska heesaha (link-yo)
"""

import os, re, json, time, sqlite3, hmac, secrets, logging, threading

# v3.1: Postgres — LABA darawal.
#   pg8000  = Python saafi ah. Compile uma baahna, libpq uma baahna -> weligiis wuu rakibmayaa.
#   psycopg2 = degdeg badan, laakiin wheel u baahan (Python version kasta mid u gaar ah).
# Midkasta oo la helo waa la isticmaalayaa; pg8000 ayaa asal ahaan la rakibayaa.
try:
    import psycopg2
    import psycopg2.extras
except Exception:                      # ImportError, ama libpq maqan
    psycopg2 = None
try:
    import pg8000.dbapi as pg8000
except Exception:
    pg8000 = None
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, request, session, redirect, url_for, jsonify,
                   render_template_string, make_response, Response, g)
import base64 as _b64
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
log = logging.getLogger("mohapro")
logging.basicConfig(level=logging.INFO)

def _db_path():
    p = os.environ.get("DB_PATH")
    if p:
        return p
    if os.path.isdir("/var/data") and os.access("/var/data", os.W_OK):
        return "/var/data/mohapro.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mohapro.db")

DATABASE_URL   = os.environ.get("DATABASE_URL", "").strip()   # v3: Postgres (Neon)
if DATABASE_URL.startswith("postgres://"):        # Neon/Heroku qaab duug ah
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

PG_DRIVER = "psycopg2" if psycopg2 is not None else ("pg8000" if pg8000 is not None else "")
USE_PG = bool(DATABASE_URL) and bool(PG_DRIVER)
if DATABASE_URL and not PG_DRIVER:
    log.error("DATABASE_URL waa la dejiyay laakiin darawal Postgres lama helin "
              "(pg8000 ama psycopg2) -> SQLite ayaa la isticmaalayaa. "
              "Ku dar 'pg8000' requirements.txt.")
elif USE_PG:
    log.info("Postgres: darawalka la isticmaalayo = %s", PG_DRIVER)

DB_PATH        = _db_path()
CLOUD_TOKEN    = os.environ.get("CLOUD_TOKEN", "").strip()
ADMIN_ACCOUNT  = re.sub(r"\D", "", os.environ.get("ADMIN_ACCOUNT", "").strip())
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
EXTRA_USERS    = os.environ.get("EXTRA_USERS", "")   # "acc:magac:pw, acc:magac:pw"
BRAND_IMAGE_URL= os.environ.get("BRAND_IMAGE_URL", "").strip()   # sawir caadi ah (URL)
SECRET_KEY     = os.environ.get("SECRET_KEY", "")

STALE_SECONDS  = 90        # in ka badan = OFFLINE
HISTORY_CAP    = 720       # dhibco taariikheed account kasta
HISTORY_EVERY  = 60        # ugu dhaqsaha badnaan hal dhibic daqiiqaddii

if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    log.warning("SECRET_KEY lama dejin -> mid ku-meel-gaadh ah. Session-nadu way ba'ayaan restart kasta.")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("RENDER", "") != "" or os.environ.get("RAILWAY_ENVIRONMENT", "") != ""
                           or os.environ.get("RAILWAY_ENVIRONMENT_NAME", "") != "" or os.environ.get("FORCE_HTTPS", "") == "1"),   # v12.5.1: Railway
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

VALID_COMMANDS = [
    "START", "STOP", "CLOSE_ALL", "CLOSE_PROFIT",
    "STRATEGY:SR", "STRATEGY:SMC", "STRATEGY:BOTH",   # v12.5: BB/EMA/VSA/POC waa la saaray (EA v68.1)
    "SET:RESET",
    "BASKET:CLOSE", "BASKET:BE",                         # v12.7: GOLD BASKET (EA v69.0)
]

# v5: sitinka App-ka (SET:KEY=VALUE). xad kasta waa la hubinayaa - qiime khaldan lama dirayo.
SET_LIMITS = {          # key: (min, max, noocaa)
    "SL":        (0, 5000, "int"),
    "TP":        (0, 5000, "int"),
    "LOT":       (0, 100,  "float"),
    "STEP":      (1, 5000, "int"),
    "STEPSTART": (1, 5000, "int"),
    "STEPON":    (0, 1,    "int"),
    "BE":        (0, 1,    "int"),
    "LOCKMODE":  (0, 1,    "int"),   # v5.1: 0 = STEP, 1 = BE-ONLY
    "ADAPT":     (0, 1,    "int"),   # v5.2: ATR adaptive SL/TP
    "MGMT":      (0, 1,    "int"),   # v7.1: 1 = maamulku wuu shaqeynayaa, 0 = damman
    "SLTP":      (0, 2,    "int"),   # v9.3: 0 = FIXED, 1 = ATR, 2 = SNIPER
    # v10: sitinka .set-ka ugu muhiimsan -> app-ka (EA v67.4+)
    "RISK":      (0.01, 10, "float"),
    "DLOSS":     (0, 50,   "float"),
    "MAXDD":     (0, 100,  "float"),
    "SNIPER":    (0, 1,    "int"),
    "SNDAY":     (0, 50,   "int"),
    "SNRR":      (1, 10,   "float"),
    "SNSLMAX":   (0, 500,  "int"),
    "NEWS":      (0, 1,    "int"),
    "REV":       (0, 2000000000, "int"),
    # v12.6: maamulka cusub (EA v68.2)
    "STRAT":     (0, 2,    "int"),   # 0 = SR/SD, 1 = SMC, 2 = LABADA
    "EMAF":      (0, 1,    "int"),   # EMA filter
    "STARS":     (1, 3,    "int"),   # ★ ugu yar
    "LOT2":      (10, 100, "int"),   # lot-ka ★★ (%)
    "PROT":      (0, 2,    "int"),   # 0 = DAMMAN, 1 = BREAK-EVEN, 2 = TALLAABO
    "SDPROF":    (0, 2,    "int"),   # v12.8 (EA v69.6): heerka zone-ka 0 TAYO · 1 DHEXE · 2 BADAN (admin)
    # v12.7: GOLD BASKET (EA v69.0) - XAUUSD oo keliya
    "BSK":       (0, 1,    "int"),   # 1 = basket ON
    "BSKSIG":    (0, 3,    "int"),   # 0 = ZONE, 1 = EMA, 2 = LABADA, 3 = DOUBLE (v12.11 · EA v69.9)
    "BSKDTOL":   (0.05, 1, "float"), # DOUBLE: dulqaadka labada sal (x ATR)
    "BSKDNECK":  (0.3, 3,  "float"), # DOUBLE: neckline ugu yar (x ATR)
    "BSKDTP":    (1, 5,    "float"), # DOUBLE: TP (x R)
    "BSKN":      (2, 5,    "int"),   # trade-yada basket-ka
    "BSKENT":    (0, 1,    "int"),   # 0 = LAKAB, 1 = ISKU MAR
    "BSKRISK":   (0.05, 5, "float"), # risk basket-ka oo dhan (%)
    "BSKMAX":    (0, 100000, "int"), # xad $ adag (0 = off)
    "BSKDAY":    (1, 10,   "int"),   # basket maalintii
    "BSKSPR":    (5, 500,  "int"),   # spread ugu badan (sent)
    "BSKTP":     (0, 1,    "int"),   # 0 = JARANJAR, 1 = WADAJIR
    "BSKTGT":    (1, 100000, "int"), # WADAJIR: bartilmaameed $
    "BSKBE":     (0, 1,    "int"),   # TP1 kadib BE
    "BSKLOCK":   (0, 1,    "int"),   # qufulka faa'iidada
    "BSKSES":    (0, 1,    "int"),   # waqtiga dahabka
    "BSKBLK":    (0, 1,    "int"),   # 2 khasaare -> jihada jooji
    # v12.9: ASIA BREAKOUT (EA v69.7) - XAUUSD · admin
    "ASIA":      (0, 1,    "int"),   # 1 = ON
    "ASIADIR":   (0, 2,    "int"),   # 0 = LABADA, 1 = BUY, 2 = SELL
    "ASIATR":    (0, 1,    "int"),   # trend H4 filter
    "ASIAMAXR":  (0, 1000, "int"),   # range ugu weyn $ (0 = off)
    "ASIASL":    (0, 1,    "int"),   # 0 = bartamaha, 1 = dhinaca kale
    "ASIATP":    (0.5, 5,  "float"), # TP = N x R
    "ASIARISK":  (0.05, 5, "float"), # risk %
    "ASIAMAX":   (0, 100000, "int"), # xad $ adag
    "ASIAL":     (1, 3,    "int"),   # lakabyada
    "ASIAWE":    (8, 20,   "int"),   # daaqadda jebinta: dhammaad (GMT)
    "ASIACL":    (13, 23,  "int"),   # xidh (GMT)
    # v12.10: ASIA GRID / MARTINGALE ilaalin leh (EA v69.8)
    "ASG":       (0, 1,    "int"),   # 1 = ON
    "ASGM":      (0, 1,    "int"),   # 0 = GRID, 1 = MARTINGALE
    "ASGL":      (2, 5,    "int"),   # lakabyada oo dhan
    "ASGS":      (0.15, 0.33, "float"), # masaafo (x R)
    "ASGX":      (1, 2,    "float"), # lot kordhin
    # v12.12: ⚡ TICK SCALPER (EA v70.0) - XAUUSD · admin
    "TK":        (0, 1,    "int"),   # 1 = ON
    "TKONLY":    (0, 1,    "int"),   # TICK oo keliya (xeeladaha kale XAUUSD ha furin)
    "TKW":       (3, 50,   "int"),   # tick tracker: W
    "TKK":       (2, 50,   "int"),   # tick tracker: K
    "TKMV":      (0, 20,   "float"), # dhaqdhaqaaq ugu yar ($)
    "TKSEC":     (0, 300,  "int"),   # xawaare (ilbiriqsi · 0 = off)
    "TKLOT":     (0.01, 100, "float"), # lot go'an
    "TKRISK":    (0, 5,    "float"), # risk % (0 = lot go'an)
    "TKVSL":     (0.1, 100, "float"), # virtual SL ($)
    "TKVTP":     (0, 100,  "float"), # virtual TP ($ · 0 = trailing oo keliya)
    "TKBE":      (0, 100,  "float"), # break-even trigger ($)
    "TKBEL":     (0, 50,   "float"), # break-even lock ($)
    "TKTRS":     (0, 100,  "float"), # trailing bilow ($)
    "TKTRD":     (0.05, 50, "float"), # trailing masaafo ($)
    "TKHARD":    (0.5, 200, "float"), # SL adag broker ($)
    "TKHOLD":    (0, 86400, "int"),  # waqtiga ugu dheer (ilbiriqsi)
    "TKHS":      (0, 23,   "int"),   # saacadaha: bilow (server)
    "TKHE":      (1, 24,   "int"),   # saacadaha: dhammaad (server)
    "TKSPR":     (5, 500,  "int"),   # spread ugu badan (sent)
    "TKCD":      (0, 3600, "int"),   # cooldown (ilbiriqsi)
    "TKMAXD":    (1, 1000, "int"),   # trade maalintii
    "TKML":      (0, 20,   "int"),   # khasaare isku xiga -> hakad
    "TKPAUSE":   (1, 1440, "int"),   # hakad (daqiiqo)
    "TKDL":      (0, 50,   "float"), # khasaaraha maalinlaha %
    "TKDT":      (0, 100,  "float"), # bartilmaameed maalinle %
    "TKSLM":     (0, 1,    "int"),   # v12.14 (EA v70.2): 0 = VIRTUAL SL, 1 = BROKER SL
    "TKTPM":     (0, 1,    "int"),   # v12.15 (EA v70.3): 0 = VIRTUAL TP, 1 = BROKER TP
    "TKBON":     (0, 1,    "int"),   # v12.16 (EA v70.4): TICK BASKET (0 = HAL TRADE)
    "TKBN":      (2, 5,    "int"),   # trade-yada basket-ka
    "TKBENT":    (0, 1,    "int"),   # 0 = ISKU MAR, 1 = SIGNAL KASTA
    "TKBEX":     (0, 1,    "int"),   # 0 = TP KASTA, 1 = WADAJIR
    "TKBTGT":    (0, 10000, "float"), # bartilmaameed WADAJIR ($)
    "TKBBE":     (0, 10000, "float"), # BE basket ($ · 0 = off)
    "TKBLP":     (0, 1000000, "int"), # 0.01 lot $X balance kasta (0 = off)
    "TKTRON":    (0, 1,    "int"),   # v12.17 (EA v70.5): TP RAAC (0 = off)
    "TKTRST":    (50, 95,  "float"), # TP RAAC bilow: faa'iido >= X% TP-ga
    "TKTRLK":    (10, 90,  "float"), # TP RAAC qufulka SL: X% TP-ga
    "TKTRSTEP":  (0.1, 50, "float"), # TP RAAC: TP u dheeree $X raac kasta
    "TKTRMAX":   (1, 10,   "int"),   # TP RAAC: raac ugu badan
    "TKDRON":    (0, 1,    "int"),   # v12.19 (EA v70.7): 🧭 JIHADA SUUQA
    "TKDRM":     (0, 2,    "int"),   # JILICSAN 2/4 · DHEXE 3/4 · ADAG 4/4
    "TKDRB":     (3, 20,   "int"),   # shumacyada M1
    "TKDRN":     (2, 20,   "int"),   # inta jiho isku mid ah
    "TKDRS":     (0, 100,  "float"), # xoogga ugu yar %
    "TKDRW":     (0, 240,  "int"),   # khasaare kadib -> sug (daq)
    "ZNON":      (0, 1,    "int"),   # v12.22 (EA v70.8): 🎯 ZONE YAR
    "ZNTF":      (0, 2,    "int"),   # M1 · M5 · LABADA
    "ZNSH":      (0, 1,    "int"),   # chart-ka MT5: muuji / qari
    "ZNT":       (2, 6,    "int"),   # taabasho ugu yar
    "ZNW":       (0.05, 1, "float"), # ballaca ugu badan x ATR
    "ZNMX":      (1, 10,   "int"),   # zone-yada la hayo
    "ZNSL":      (0.1, 20, "float"), # SL zone-ka geeskiisa $
    "ZNTP":      (0, 1,    "int"),   # TP: zone xiga · TICK caadi
    "PRON":      (0, 1,    "int"),   # v12.22 (EA v70.8): 💱 LAMAANAHA
    "PRMAX":     (1, 8,    "int"),   # lamaanaha ugu badan (👑 ku jiro)
    "CBON":      (0, 1,    "int"),   # v12.24 (EA v70.9): 📏 CABBIR LAMAANE
    "CBTF":      (0, 2,    "int"),   # ATR timeframe: M5 · M15 · H1
    "CBN":       (20, 2000, "int"),  # ATR muddo (bar)
    "CBSQ":      (0, 100,  "int"),   # spread > X% SL-ka -> TICK ma galo (0 = off)
    "GRON":      (0, 1,    "int"),   # v12.25 (EA v71.0): 🪜 GRID STOP
    "GRONLY":    (0, 1,    "int"),   # GRID oo keliya
    "GRDIR":     (0, 2,    "int"),   # TREND · BUY · SELL
    "GRSTR":     (0, 100,  "float"), # xoogga jihada %
    "GRSTEP":    (0, 1000, "float"), # masaafo $ (0 = AUTO)
    "GRSATR":    (0.05, 1, "float"), # AUTO x ATR M5
    "GRSPX":     (1, 10,   "float"), # masaafo >= X x spread
    "GRLV":      (2, 30,   "int"),   # lakab ugu badan
    "GRLOT":     (0.01, 100, "float"), # lot lakab kasta
    "GRTP":      (0, 100000, "float"), # TP guud $
    "GRLK":      (0, 100000, "float"), # quful bilow $
    "GRLKP":     (10, 90,  "float"), # quful %
    "GRSL":      (0.1, 5,  "float"), # SL guud % balance
    "GRHL":      (2, 30,   "int"),   # SL broker = X lakab
    "GRCD":      (0, 1440, "int"),   # SL kadib sug (daq)
    "GRMD":      (1, 50,   "int"),   # grid maalintii
    "GRDL":      (0, 50,   "float"), # khasaaraha maalinlaha %
    "GRNW":      (0, 1,    "int"),   # war
    "GRHS":      (0, 23,   "int"),   # saacadaha: bilow
    "GRHE":      (1, 24,   "int"),   # saacadaha: dhammaad
    "GRMM":      (0, 10080, "int"),  # grid ugu dheer (daq)
    "TKSPM":     (0, 10,   "float"), # filter spread: dhaqdhaqaaq >= X x spread (0 = off)
    "TKTR":      (0, 1,    "int"),   # filter trend EMA50 M5
}
BSK_KEYS = ("BSK", "BSKSIG", "BSKN", "BSKENT", "BSKRISK", "BSKMAX", "BSKDAY", "BSKSPR",
            "BSKTP", "BSKTGT", "BSKBE", "BSKLOCK", "BSKSES", "BSKBLK", "BSKDTOL", "BSKDNECK", "BSKDTP")
ASIA_KEYS = ("ASIA", "ASIADIR", "ASIATR", "ASIAMAXR", "ASIASL", "ASIATP", "ASIARISK", "ASIAMAX",
             "ASIAL", "ASIAWE", "ASIACL", "ASG", "ASGM", "ASGL", "ASGS", "ASGX")   # v12.9 · v12.10 GRID
TK_KEYS = ("TK", "TKONLY", "TKW", "TKK", "TKMV", "TKSEC", "TKLOT", "TKRISK", "TKVSL", "TKVTP", "TKBE", "TKBEL",
           "TKTRS", "TKTRD", "TKHARD", "TKHOLD", "TKHS", "TKHE", "TKSPR", "TKCD", "TKMAXD", "TKML", "TKPAUSE", "TKDL", "TKDT", "TKSLM", "TKSPM", "TKTR", "TKTPM",
           "TKBON", "TKBN", "TKBENT", "TKBEX", "TKBTGT", "TKBBE", "TKBLP",
           "TKTRON", "TKTRST", "TKTRLK", "TKTRSTEP", "TKTRMAX",
           "TKDRON", "TKDRM", "TKDRB", "TKDRN", "TKDRS", "TKDRW")   # v12.12 · v12.14 · v12.15 · v12.16 · v12.17 · v12.19
ZN_KEYS = ("ZNON", "ZNTF", "ZNSH", "ZNT", "ZNW", "ZNMX", "ZNSL", "ZNTP")   # v12.22 (EA v70.8): 🎯 ZONE YAR
CB_KEYS = ("CBON", "CBTF", "CBN", "CBSQ")
GR_KEYS = ("GRON", "GRONLY", "GRDIR", "GRSTR", "GRSTEP", "GRSATR", "GRSPX", "GRLV", "GRLOT", "GRTP", "GRLK", "GRLKP",
           "GRSL", "GRHL", "GRCD", "GRMD", "GRDL", "GRNW", "GRHS", "GRHE", "GRMM")   # v12.25 (EA v71.0): 🪜 GRID STOP                                  # v12.24 (EA v70.9): 📏 CABBIR LAMAANE
PR_KEYS = ("PAIRS", "PRON", "PRMAX")                                       # v12.22 (EA v70.8): 💱 LAMAANAHA (admin · macmiil-ka looma gudbiyo)

# v12.18 (EA v70.6): ⚙️ INPUT - dhammaan input-yada EA-ga (p34.py ayaa soo saaray · EA-ga iyo app-ka isku hash)
INP_SCHEMA_JSON = r'''{"h":"20818ced","ver":"71.0","n":369,"groups":[{"name":"0 · TIJAABO (v65.3)","cat":"SYS"},{"name":"0c · ATR ADAPTIVE (v66.5 - SL/TP suuqa ayuu la socdaa)","cat":"MAIN"},{"name":"0a · STEP-LOCK (v66.2 - faa'iido xidhid tallaabo tallaabo)","cat":"MAIN"},{"name":"0b · SNIPER MODE (v66)","cat":"MAIN"},{"name":"🥇 GOLD BASKET (v69.0 · XAUUSD oo keliya)","cat":"GOLD"},{"name":"🥇 ASIA BREAKOUT · XAUUSD (v69.7)","cat":"ASIA"},{"name":"⚡ TICK SCALPER · XAUUSD (v70.0)","cat":"TICK"},{"name":"1 · SHATI & AMMAAN","cat":"SYS"},{"name":"2 · XEELAD - DOORASHO","cat":"MAIN"},{"name":"3 · XADKA TIRADA TRADE-KA (is-dul-saarid)","cat":"PROT"},{"name":"4 · KHATAR & LOT","cat":"PROT"},{"name":"5 · XADKA AMMAANKA (khasaare / faa'iido)","cat":"PROT"},{"name":"6 · SL / TP (aasaaska)","cat":"MAIN"},{"name":"7 · BREAK-EVEN & TRAILING","cat":"MAIN"},{"name":"8 · QAYB-XIRID (Partial / ScaleOut / TP-Ladder)","cat":"MAIN"},{"name":"9 · XIRITAAN HORE","cat":"MAIN"},{"name":"10 · FILTER - XAALADDA SUUQA","cat":"FILT"},{"name":"11 · FILTER - EMA (JIHADA · H1 + H4)","cat":"FILT"},{"name":"12 · FILTER - TREND FOLLOW","cat":"FILT"},{"name":"13 · FILTER - KALE","cat":"FILT"},{"name":"14 · WAQTI & SESSION","cat":"FILT"},{"name":"15 · WARARKA (NEWS)","cat":"PROT"},{"name":"16 · XEELAD: SR (Support / Resistance)","cat":"MAIN"},{"name":"19 · XEELAD: SMC (Smart Money)","cat":"MAIN"},{"name":"19b · ★ SCORING & LOT (v68.1)","cat":"MAIN"},{"name":"22 · HABKA FULINTA","cat":"SYS"},{"name":"23 · PROP FIRM","cat":"PROT"},{"name":"24 · TELEGRAM","cat":"SYS"},{"name":"25 · CLOUD DASHBOARD","cat":"SYS"},{"name":"26 · MUUQAALKA CHART-KA","cat":"SYS"},{"name":"21b · SUPPLY & DEMAND (v61 - mishiin cusub)","cat":"MAIN"},{"name":"27 · DIIWAAN & DEBUG","cat":"SYS"},{"name":"28 · 💱 LAMAANAHA (v70.8)","cat":"SYS"},{"name":"29 · 📏 CABBIR LAMAANE · TICK + BASKET (v70.9)","cat":"TICK"},{"name":"30 · 🪜 GRID STOP · keli (v71.0)","cat":"GRID"}],"items":[{"n":"Disable_All_Management","l":"Disable All Management","g":0,"k":1,"t":"b"},{"n":"Test_Loose_Filters","l":"TRUE = fitarada AAN zone-ka ahayn waa la dabciyay (News/Session/Cooldown/Regime-ADX/MTF/EMA200/Correlation/H1-","g":0,"k":0,"t":"b","x":0,"r":0,"m":0},{"n":"Adaptive_SLTP","l":"TRUE = SL/TP shumac kasta dib ayaa loo xisaabiyaa ATR-ka HADDA (marxaladda suuqa). Hore: hal mar oo keliya fur","g":1,"k":1,"t":"b"},{"n":"Adaptive_TF","l":"TF-ka ATR-ka","g":1,"k":0,"t":"e","x":1,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"Adaptive_SL_Mult","l":"SL = ATR x tan","g":1,"k":0,"t":"d","x":2,"r":0,"m":0},{"n":"Adaptive_TP_Mult","l":"TP = ATR x tan","g":1,"k":0,"t":"d","x":3,"r":0,"m":0},{"n":"Adaptive_Max_R","l":"xadka sare - SL/TP ha ka ballaadhan tan x kii asalka ahaa (0 = xad ma jiro)","g":1,"k":0,"t":"d","x":4,"r":0,"m":0},{"n":"Adaptive_Min_R","l":"xadka hoose - ha ka yaraan tan x kii asalka ahaa (0 = xad ma jiro)","g":1,"k":0,"t":"d","x":5,"r":0,"m":0},{"n":"Adaptive_Log","l":"qor beddel kasta","g":1,"k":0,"t":"b","x":6,"r":0,"m":0},{"n":"Lock_Mode","l":"BE_ONLY = SL wuxuu ku sii jiraa BREAK-EVEN (trade-ku meel bannaan buu helayaa) | STEP = SL tallaabo tallaabo k","g":2,"k":1,"t":"e","o":[[0,"STEP"],[1,"BE_ONLY"]]},{"n":"Lock_BE_Buffer_Pips","l":"BE_ONLY - SL = entry + intan pip (0 = entry sax ah)","g":2,"k":0,"t":"d","x":7,"r":0,"m":0},{"n":"Step_Lock_Enable","l":"TRUE = SL tallaabo tallaabo kor buu u socdaa (faa'iidada waa la xidhaa). Trade WELIGIIS lama xidho - kaliya SL","g":2,"k":1,"t":"b"},{"n":"Step_Lock_Pips","l":"tallaabo kasta (pip). SL-ku wuxuu ka dambeeyaa sicirka intaas","g":2,"k":1,"t":"d"},{"n":"Step_Lock_Start_Pips","l":"faa'iidada ugu horraysa ee SL-ku dhaqaaqo (pip). 15 -> +15 = SL break-even","g":2,"k":1,"t":"d"},{"n":"Step_Lock_Log","l":"qor tallaabo kasta (Experts log)","g":2,"k":0,"t":"b","x":8,"r":0,"m":0},{"n":"Inp_Sniper_Mode","l":"SNIPER - zone WEYN (M30) + xaqiijin yar (CHoCH M5) gudaha zone-ka -> SL cidhiidhi, RR sare. false = mishiinkii","g":3,"k":1,"t":"b"},{"n":"Sniper_LTF","l":"timeframe-ka yar ee xaqiijinta (CHoCH)","g":3,"k":0,"t":"e","x":9,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"Inp_Sniper_Touch_Bars","l":"6 saac - taabashadu waa inay dhowdahay","g":3,"k":0,"t":"i","x":10,"r":0,"m":8},{"n":"Sniper_Swing_Len","l":"xoogga swing-ka LTF (shumac dhinac kasta)","g":3,"k":0,"t":"i","x":11,"r":0,"m":0},{"n":"Sniper_SL_Buffer_Pips","l":"SL = hooseynta/sarreynta sweep-ka + inta pip","g":3,"k":0,"t":"d","x":12,"r":0,"m":0},{"n":"Sniper_SL_Min_Pips","l":"SL ugu yar (pip) - ka yar -> waa la ballaadhinayaa ilaa tan","g":3,"k":0,"t":"d","x":13,"r":0,"m":0},{"n":"Sniper_SL_ATR_TF","l":"ATR-ka SL-ka sniper-ka (JPY/GBP ayuu la qabsadaa)","g":3,"k":0,"t":"e","x":14,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"Sniper_SL_Min_ATR","l":"SL ugu yar = N x ATR (0 = off, pip-ka oo keliya)","g":3,"k":0,"t":"d","x":15,"r":0,"m":0},{"n":"Sniper_SL_Buf_ATR","l":"buffer-ka gadaasha sweep-ka = max(pip, N x ATR)","g":3,"k":0,"t":"d","x":16,"r":0,"m":0},{"n":"Sniper_SL_Spread_Mult","l":"SL ugu yar = N x spread","g":3,"k":0,"t":"d","x":17,"r":0,"m":0},{"n":"Sniper_SL_Max_ATR","l":"SL ugu weyn = max(SNSLMAX, N x ATR) (0 = SNSLMAX oo keliya)","g":3,"k":0,"t":"d","x":18,"r":0,"m":0},{"n":"Inp_Sniper_SL_Max_Pips","l":"Sniper SL Max Pips","g":3,"k":1,"t":"d"},{"n":"Inp_Sniper_RR","l":"TP = SL x tan","g":3,"k":1,"t":"d"},{"n":"Inp_Sniper_Max_Trades_Day","l":"trade ugu badan MAALINTII - DHAMMAAN lammaanayaasha/chart-yada (magic-ga MOHA). 0 = xad ma jiro","g":3,"k":1,"t":"i"},{"n":"Sniper_Symbols","l":"lammaanayaasha la ogol yahay (madhan = dhammaan). XAUUSD pip-kiisu waa ka duwan yahay - gooni u tijaabi","g":3,"k":0,"t":"s","x":19,"r":0,"m":0},{"n":"Sniper_Session_Only","l":"kaliya London + New York","g":3,"k":0,"t":"b","x":20,"r":0,"m":0},{"n":"Sniper_GMT_Start","l":"London ka hor","g":3,"k":0,"t":"i","x":21,"r":0,"m":0},{"n":"Sniper_GMT_End","l":"NY dhammaadka","g":3,"k":0,"t":"i","x":22,"r":0,"m":0},{"n":"Inp_BSK_On","l":"default ON (DOUBLE) · v69.7: OFF (ASIA ayaa beddelay) · v69.0 GOLD BASKET: XAUUSD signal kasta -> basket (lamm","g":4,"k":1,"t":"b"},{"n":"Inp_BSK_Signal","l":"signal-ka dahabka: ZONE (demand/supply) · EMA pullback · LABADA · DOUBLE (v69.9: top/bottom salka + EMA)","g":4,"k":1,"t":"e","o":[[0,"ZONE"],[1,"EMA"],[2,"BOTH"],[3,"DBL"]]},{"n":"Inp_BSK_Trades","l":"trade-yada basket-ka (2-5)","g":4,"k":1,"t":"i"},{"n":"Inp_BSK_Entry","l":"LAKAB: #1 hadda, inta kale zone-ka gudihiisa · ISKU MAR: dhammaan hadda","g":4,"k":1,"t":"e","o":[[0,"LAKAB"],[1,"ISKUMAR"]]},{"n":"Inp_BSK_Risk_Pct","l":"risk basket-ka OO DHAN (% balance) - trade-yada ayaa loo qaybiyaa, ma labanlaabmo (v69.5: 0.5-1%)","g":4,"k":1,"t":"d"},{"n":"Inp_BSK_MaxLoss_USD","l":"xad adag ($) SAQAF: xadka dhabta ah = MIN(kan, 1.2 x risk $) · 0 = auto (1.2 x risk) oo keliya","g":4,"k":1,"t":"d"},{"n":"Inp_BSK_Max_Day","l":"basket maalintii (ugu badnaan) · v69.5: 2 (3 hore)","g":4,"k":1,"t":"i"},{"n":"Inp_BSK_Max_Spread","l":"spread ugu badan (sent: 35 = $0.35)","g":4,"k":1,"t":"i"},{"n":"Inp_BSK_TP_Mode","l":"TP: JARANJAR (1R · 2R · 3R) · WADAJIR ($ bartilmaameed -> xidh dhammaan)","g":4,"k":1,"t":"e","o":[[0,"JARANJAR"],[1,"WADAJIR"]]},{"n":"Inp_BSK_Target_USD","l":"WADAJIR: bartilmaameedka faa'iidada ($)","g":4,"k":1,"t":"d"},{"n":"Inp_BSK_BE_TP1","l":"TP1 kadib -> inta kale SL = break-even (basket-ku ma khasaari karo)","g":4,"k":1,"t":"b"},{"n":"Inp_BSK_Lock","l":"qufulka faa'iidada: faa'iidada ugu sarreysa 50% ha lumin","g":4,"k":1,"t":"b"},{"n":"Inp_BSK_Session","l":"waqtiga dahabka oo keliya (London + NY, GMT hoose)","g":4,"k":1,"t":"b"},{"n":"Inp_BSK_Dir_Block","l":"2 basket oo isku jiho ah oo khasaara -> jihadaas maanta waa la joojiyaa","g":4,"k":1,"t":"b"},{"n":"BSK_GMT_Start","l":"waqtiga dahabka: bilow (GMT)","g":4,"k":0,"t":"i","x":23,"r":0,"m":0},{"n":"BSK_GMT_End","l":"waqtiga dahabka: dhammaad (GMT)","g":4,"k":0,"t":"i","x":24,"r":0,"m":0},{"n":"BSK_Layer_Expire_Min","l":"LAKAB: lakabyada aan buuxsamin X daqiiqo kadib waa la tirtiraa","g":4,"k":0,"t":"i","x":25,"r":0,"m":0},{"n":"BSK_Cooldown_Min","l":"basket xidhmay kadib - sug (daqiiqo)","g":4,"k":0,"t":"i","x":26,"r":0,"m":0},{"n":"BSK_Lock_Start_Pct","l":"qufulku wuxuu bilaabmaa marka faa'iidadu = X% risk-ka ($)","g":4,"k":0,"t":"d","x":27,"r":0,"m":0},{"n":"BSK_Lock_Keep_Pct","l":"... kadib X% faa'iidada ugu sarreysa waa la ilaaliyaa","g":4,"k":0,"t":"d","x":28,"r":0,"m":0},{"n":"BSK_MaxLoss_Mult","l":"v69.5 · xadka khasaaraha = N x risk-ga $ ee basket-ka (1.05-3)","g":4,"k":0,"t":"d","x":29,"r":0,"m":0},{"n":"BSK_Step_Lock","l":"v69.5 · TP2 kadib -> trade-yada hadhay SL = TP1 (ugu yaraan +2R)","g":4,"k":0,"t":"b","x":30,"r":0,"m":0},{"n":"BSK_EMA_Fast","l":"EMA pullback: EMA degdeg (M5)","g":4,"k":0,"t":"i","x":31,"r":1,"m":0},{"n":"BSK_EMA_Mid","l":"EMA pullback: EMA dhexe (M5 + trend TF)","g":4,"k":0,"t":"i","x":32,"r":1,"m":0},{"n":"BSK_EMA_Slow","l":"EMA pullback: EMA gaabis (trend TF)","g":4,"k":0,"t":"i","x":33,"r":1,"m":0},{"n":"BSK_Trend_TF","l":"EMA pullback: timeframe-ka trend-ka","g":4,"k":0,"t":"e","x":34,"r":1,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"BSK_Entry_TF","l":"EMA pullback: timeframe-ka gelitaanka","g":4,"k":0,"t":"e","x":35,"r":1,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"BSK_Pull_Tol_ATR","l":"EMA pullback: dulqaadka band-ka EMA (x ATR)","g":4,"k":0,"t":"d","x":36,"r":0,"m":0},{"n":"BSK_Symbols","l":"lammaanayaasha basket-ka (magaca ku jira)","g":4,"k":0,"t":"s","x":37,"r":0,"m":0},{"n":"BSK_Trend_Sep_ATR","l":"v69.4 · trend: EMA50/EMA200 (M15) kala fogaan >= N x ATR (range -> ma ganacsado)","g":4,"k":0,"t":"d","x":38,"r":0,"m":0},{"n":"BSK_Trend_Slope_ATR","l":"v69.4 · trend: EMA50 M15 janjeer (8 shumac) >= N x ATR","g":4,"k":0,"t":"d","x":39,"r":0,"m":0},{"n":"BSK_Chop_Bars","l":"v69.4 · chop: shumacyada M5 ee la eegayo","g":4,"k":0,"t":"i","x":40,"r":0,"m":0},{"n":"BSK_Chop_Max","l":"v69.4 · chop: gudbidda EMA50 ugu badan (ka badan -> ma ganacsado)","g":4,"k":0,"t":"i","x":41,"r":0,"m":0},{"n":"BSK_Need_Structure","l":"v69.4 · qaab-dhismeed M15: BUY = HH/HL · SELL = LH/LL","g":4,"k":0,"t":"b","x":42,"r":0,"m":0},{"n":"BSK_Flip_Hours","l":"v69.4 · basket kadib jihada lidka ah lama furo X saac","g":4,"k":0,"t":"i","x":43,"r":0,"m":0},{"n":"BSK_Dbl_TF","l":"timeframe-ka (M15 ayaa la tijaabiyay · M5 ma shaqeyn)","g":4,"k":0,"t":"e","x":44,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"Inp_BSK_Dbl_Tol","l":"labada sal / top kala fogaanshaha ugu badan (x ATR)","g":4,"k":1,"t":"d"},{"n":"Inp_BSK_Dbl_Neck","l":"neckline ugu yar (x ATR) salka ka sarreeya","g":4,"k":1,"t":"d"},{"n":"Inp_BSK_Dbl_TP_R","l":"TP lakab kasta (x R)","g":4,"k":1,"t":"d"},{"n":"BSK_Dbl_GapMin","l":"shumacyada u dhexeeya labada sal (ugu yar)","g":4,"k":0,"t":"i","x":45,"r":0,"m":0},{"n":"BSK_Dbl_GapMax","l":"... (ugu badan)","g":4,"k":0,"t":"i","x":46,"r":0,"m":0},{"n":"Inp_ASIA_On","l":"default OFF (DOUBLE) · v69.7: Asia range (00-07 GMT) -> jebinta London (07-12) -> 1 trade maalintii","g":5,"k":1,"t":"b"},{"n":"ASIA_Range_Start","l":"Asia range: bilow (saac GMT)","g":5,"k":0,"t":"i","x":47,"r":0,"m":0},{"n":"ASIA_Range_End","l":"Asia range: dhammaad (saac GMT) = bilowga jebinta","g":5,"k":1,"t":"i"},{"n":"Inp_ASIA_Win_End","l":"daaqadda jebinta: dhammaad (GMT) - kadib maanta trade ma furmo","g":5,"k":1,"t":"i"},{"n":"Inp_ASIA_Close_Hour","l":"trade furan -> la xidhaa (GMT) · Jimce: 20 ugu dambeyn","g":5,"k":1,"t":"i"},{"n":"Inp_ASIA_Dir","l":"jihada: LABADA · BUY · SELL","g":5,"k":1,"t":"e","o":[[0,"BOTH"],[1,"BUY"],[2,"SELL"]]},{"n":"Inp_ASIA_Trend","l":"trend H4 (EMA50): jebinta trend-ka raacda oo keliya","g":5,"k":1,"t":"b"},{"n":"Inp_ASIA_MaxRange","l":"range ugu weyn ($ qiime) - ka weyn -> maanta ma ganacsado (0 = off)","g":5,"k":1,"t":"d"},{"n":"Inp_ASIA_SL_Mode","l":"SL: bartamaha range-ka · dhinaca kale","g":5,"k":1,"t":"e","o":[[0,"MID"],[1,"OPP"]]},{"n":"Inp_ASIA_TP_R","l":"TP = N x R (0.5-5)","g":5,"k":1,"t":"d"},{"n":"Inp_ASIA_Risk_Pct","l":"risk (% balance) - lakabyada oo dhan","g":5,"k":1,"t":"d"},{"n":"Inp_ASIA_Max_USD","l":"xad adag $: lot-ka ugu yar khatartiisu ha dhaafin · floating <= -xad -> xidh (0 = off)","g":5,"k":1,"t":"d"},{"n":"Inp_ASIA_Layers","l":"lakabyada: 1 = hal trade · 2-3 = retest heerka la jebiyay (2 saac)","g":5,"k":1,"t":"i"},{"n":"ASIA_Max_Spread","l":"spread ugu badan (sent: 60 = $0.60)","g":5,"k":0,"t":"i","x":48,"r":0,"m":2},{"n":"Inp_ASG_On","l":"v69.8 GRID / MARTINGALE ilaalin leh: lakabyo marka qiimuhu soo laabto · SL wadaag · risk guud = risk %","g":5,"k":1,"t":"b"},{"n":"Inp_ASG_Mode","l":"GRID = lot isku mid · MARTINGALE = lot kordha","g":5,"k":1,"t":"e","o":[[0,"GRID"],[1,"MARTI"]]},{"n":"Inp_ASG_Levels","l":"lakabyada oo dhan (#1 + kuwa kale) 2-5","g":5,"k":1,"t":"i"},{"n":"Inp_ASG_Step_R","l":"masaafada lakabyada (x R = entry -> SL) 0.15-0.33","g":5,"k":1,"t":"d"},{"n":"Inp_ASG_Mult","l":"MARTINGALE: lot kordhin lakab kasta (1.0-2.0)","g":5,"k":1,"t":"d"},{"n":"Inp_TK_On","l":"Momentum tick scalper (tijaabo: \"Every tick based on real ticks\")","g":6,"k":1,"t":"b"},{"n":"Inp_TK_Only","l":"TICK OO KELIYA: marka TICK shidan yahay, main / basket / asia lammaanahan ma furaan","g":6,"k":1,"t":"b"},{"n":"Inp_TK_Window","l":"TICK TRACKER: W = isbeddelada qiimaha ee la eegayo (3-50)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Need","l":"TICK TRACKER: K = inta jiho isku mid ah loo baahan yahay (W ka mid)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_MinMove","l":"dhaqdhaqaaqa ugu yar ee window-ka ($ qiime)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_MaxSec","l":"xawaare: window-ku ha ku dhammaado X ilbiriqsi (0 = off)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Lot","l":"lot go'an (marka Risk = 0)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Risk_Pct","l":"> 0: lot = risk % / Virtual SL (0 = lot go'an)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_VSL","l":"VIRTUAL SL ($ qiime · broker-ka lama tuso)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_VTP","l":"VIRTUAL TP ($ qiime · 0 = trailing oo keliya)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_BE_Trig","l":"BREAK-EVEN: faa'iido $X -> SL = entry + lock (0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_BE_Lock","l":"BREAK-EVEN: lock ($ qiime)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Trail_Start","l":"TRAILING virtual: bilow ($ faa'iido · 0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Trail_Dist","l":"TRAILING virtual: masaafada heerka ugu sarreeya ($)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Hard_SL","l":"SL ADAG broker-ka ($ · ilaalin internet go'a · ugu yaraan VSL + $1)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Max_Hold","l":"trade ugu dheer (ilbiriqsi) -> xidh (0 = off)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Hour_Start","l":"saacadaha (waqtiga SERVER-ka): bilow","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Hour_End","l":"saacadaha (waqtiga SERVER-ka): dhammaad","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Max_Spread","l":"spread ugu badan (sent: 35 = $0.35)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Cooldown","l":"sug X ilbiriqsi kadib trade kasta","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Max_Day","l":"trade maalintii ugu badan","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Max_Losses","l":"khasaare isku xiga -> hakad (0 = off)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Pause_Min","l":"hakadka (daqiiqo)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Day_Loss_Pct","l":"khasaaraha maalinlaha TICK (% balance) -> maanta jooji (0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Day_Target_Pct","l":"faa'iidada maalinlaha (% balance) -> maanta jooji (0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Basket","l":"BASKET (trade-yo badan) · false = HAL TRADE","g":6,"k":1,"t":"b"},{"n":"Inp_TK_Bsk_Trades","l":"trade-yada (2-5)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Bsk_Entry","l":"lakabyada: ISKU MAR · SIGNAL KASTA","g":6,"k":1,"t":"e","o":[[0,"ALL"],[1,"SIG"]]},{"n":"Inp_TK_Bsk_Exit","l":"xidhitaan: TP KASTA · WADAJIR ($)","g":6,"k":1,"t":"e","o":[[0,"EACH"],[1,"SUM"]]},{"n":"Inp_TK_Bsk_Target","l":"bartilmaameed WADAJIR ($ faa'iidada guud)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Bsk_BE","l":"faa'iido guud $X -> SL wadaag = celceliska entry (0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Bsk_LotPer","l":"0.01 lot $X balance kasta (compounding · 0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_TPR_On","l":"TP RAAC - qiimuhu TP-ga marka uu u dhawaado -> TP-ga fogee + SL quful","g":6,"k":1,"t":"b"},{"n":"Inp_TK_TPR_Start","l":"bilow marka faa'iidadu gaadho X% masaafada TP-ga (50-95)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_TPR_Lock","l":"SL-ka ku quful X% masaafada TP-ga (10-90 · < Bilow)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_TPR_Step","l":"TP-ga u dheeree $X (qiime) raac kasta","g":6,"k":1,"t":"d"},{"n":"Inp_TK_TPR_Max","l":"raac ugu badan trade kasta (1-10)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_TP_Mode","l":"TAKE PROFIT: VIRTUAL (bot-ka ayaa xidha) · BROKER (T/P-ga MT5)","g":6,"k":1,"t":"e","o":[[0,"VIRTUAL"],[1,"BROKER"]]},{"n":"Inp_TK_SL_Mode","l":"STOP LOSS: VIRTUAL (bot-ka ayaa xidha) · BROKER (server-ka · slippage yar)","g":6,"k":1,"t":"e","o":[[0,"VIRTUAL"],[1,"BROKER"]]},{"n":"Inp_TK_Spread_Mult","l":"filter spread: dhaqdhaqaaq >= X x spread (0 = off)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Dir_On","l":"🧭 JIHADA SUUQA - tick-yada jihada guud oo keliya (shumacyada M1 · EMA20/50 · 5 daq · EMA50 M5)","g":6,"k":1,"t":"b"},{"n":"Inp_TK_Dir_Mode","l":"JILICSAN 2/4 · DHEXE 3/4 · ADAG 4/4 calaamadood","g":6,"k":1,"t":"e","o":[[0,"SOFT"],[1,"MID"],[2,"HARD"]]},{"n":"Inp_TK_Dir_Bars","l":"shumacyada M1 ee la eegayo (3-20)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Dir_Need","l":"inta shumac ee jiho isku mid ah (Bars ka mid)","g":6,"k":1,"t":"i"},{"n":"Inp_TK_Dir_Str","l":"xoogga ugu yar % (ka yar = RANGE -> ha galin)","g":6,"k":1,"t":"d"},{"n":"Inp_TK_Dir_Wait","l":"khasaare kadib -> jihadaas sug X daqiiqo (0 = off)","g":6,"k":1,"t":"i"},{"n":"Inp_ZN_On","l":"🎯 ZONE YAR - TICK wuxuu ka galaa zone-yada yaryar oo keliya (SELL saqaf · BUY dabaq)","g":6,"k":1,"t":"b"},{"n":"Inp_ZN_TF","l":"timeframe-ka zone-yada · M1 · M5 · LABADA (M5 zone + M1 xaqiijin)","g":6,"k":1,"t":"e","o":[[0,"M1"],[1,"M5"],[2,"BOTH"]]},{"n":"Inp_ZN_Show","l":"chart-ka ku sawir (false = qari · ganacsigu wuu socdaa)","g":6,"k":1,"t":"b"},{"n":"Inp_ZN_Touch","l":"taabasho ugu yar (2-6) · ka yar = lama sawiro","g":6,"k":1,"t":"i"},{"n":"Inp_ZN_W","l":"ballaca ugu badan × ATR timeframe-ka (0.05-1.0)","g":6,"k":1,"t":"d"},{"n":"Inp_ZN_Max","l":"zone-yada la hayo · kuwa qiimaha ugu dhow (1-10)","g":6,"k":1,"t":"i"},{"n":"Inp_ZN_SL","l":"SL = zone-ka geeskiisa ± $X","g":6,"k":1,"t":"d"},{"n":"Inp_ZN_TP","l":"TP = zone-ka xiga · ama TICK caadi","g":6,"k":1,"t":"e","o":[[0,"ZONE"],[1,"TICK"]]},{"n":"Inp_TK_Trend","l":"filter trend EMA50 M5 (BUY kor oo keliya · SELL hoos oo keliya)","g":6,"k":1,"t":"b"},{"n":"Inp_TK_Panel","l":"panel qoraal ah chart-ka (default OFF - xogtu app-ka ayey ku jirtaa · tester-ka mar walba waa la tusaa)","g":6,"k":0,"t":"b","x":49,"r":0,"m":0},{"n":"License_Key","l":"Furaha shatiga (license key)","g":7,"k":2,"t":"s"},{"n":"Enable_AccountLock","l":"Ku xir account gaar ah (on/off)","g":7,"k":2,"t":"b"},{"n":"Licensed_Account","l":"Lambarka account-ka la ogolaaday (0 = mid kasta)","g":7,"k":2,"t":"i"},{"n":"Expiry_Date","l":"Taariikhda uu shatigu dhacayo","g":7,"k":2,"t":"s"},{"n":"Cloud_Auth_Token","l":"Furaha ammaanka - waa la dejiyay (v59.8). HA LA WADAAGIN FAYLKA.","g":7,"k":2,"t":"s"},{"n":"MohaPro_Key","l":"FURAHAAGA BOT-KA (app-ka ka koobi garee: Maamul/Guud). Account-kan OO KELIYA ayuu u shaqeeyaa. Madhan = furihi","g":7,"k":2,"t":"s"},{"n":"Require_Signed_Commands","l":"Require Signed Commands","g":7,"k":2,"t":"b"},{"n":"Panel_Stats_Period","l":"Panel WinRate/PF - muddo (TODAY/WEEK/MONTH/ALL)","g":7,"k":0,"t":"e","x":50,"r":0,"m":0,"o":[[0,"TODAY"],[1,"WEEK"],[2,"MONTH"],[3,"ALL"]]},{"n":"Panel_Stats_This_Symbol_Only","l":"tirakoobku lammaanahan oo keliya (false = account oo dhan)","g":7,"k":0,"t":"b","x":51,"r":0,"m":0},{"n":"Strategy_Mode","l":"Xeeladda: SR/SD · SMC · LABADA (EMA = filter kaliya)","g":8,"k":0,"t":"e","x":52,"r":1,"m":0,"o":[[0,"SRSD"],[1,"SMC"],[2,"BOTH"]]},{"n":"Trade_Timeframe","l":"Timeframe-ka ganacsiga (M1 ama M5)","g":8,"k":0,"t":"e","x":53,"r":0,"m":0,"o":[[1,"M1"],[5,"M5"]]},{"n":"One_Trade_Per_Symbol","l":"Hal trade lammaanahiiba (xeeladuhu ha isku raran)","g":9,"k":0,"t":"b","x":54,"r":0,"m":0},{"n":"Enable_Conflict_Guard","l":"Ka hortag BUY & SELL isku mar","g":9,"k":0,"t":"b","x":55,"r":0,"m":0},{"n":"Max_Open_Trades","l":"One_Trade_Per_Symbol horeba 1 buu ogolaa - hadda waa run","g":9,"k":0,"t":"i","x":56,"r":0,"m":0},{"n":"Inp_Max_Trades_Per_Day","l":"Max Trades Per Day","g":9,"k":1,"t":"i"},{"n":"MinMinutesBetweenTrades","l":"MinMinutesBetweenTrades","g":9,"k":0,"t":"i","x":57,"r":0,"m":0},{"n":"One_Trade_Per_Zone","l":"1 ZONE = 1 TRADE kaliya weligiis (SR/S&D) - marka zone la ganacsado, mar dambe lama isticmaali doono","g":9,"k":0,"t":"b","x":58,"r":0,"m":0},{"n":"Avoid_Reentry_Near_Loss","l":"ka fogow dib-u-gelid meesha khasaaraha ugu dambeeyay (zone-chop/whipsaw ka hortag)","g":9,"k":0,"t":"b","x":59,"r":0,"m":0},{"n":"Reentry_Avoid_ATR","l":"fogaanta (ATR x) khasaaraha ugu dambeeyay ee la ilaalinayo","g":9,"k":0,"t":"d","x":60,"r":0,"m":0},{"n":"Reentry_Avoid_Minutes","l":"Reentry Avoid Minutes","g":9,"k":0,"t":"i","x":61,"r":0,"m":0},{"n":"AllowMultiplePerBar","l":"Ogolow trade badan shumac kasta (false = hal trade shumacii)","g":9,"k":0,"t":"b","x":62,"r":0,"m":0},{"n":"One_Trade_Per_H1_Bar","l":"Hal trade saacaddii (H1)","g":9,"k":0,"t":"b","x":63,"r":0,"m":0},{"n":"Enable_NewBar_Only","l":"Signal kaliya marka shumac cusub furmo","g":9,"k":0,"t":"b","x":64,"r":0,"m":0},{"n":"Inp_Risk_Percent","l":"Khatarta trade kasta (% haraaga)","g":10,"k":1,"t":"d"},{"n":"Auto_Lot","l":"Lot toos ah oo ka yimaada khatarta % (on/off)","g":10,"k":0,"t":"b","x":65,"r":0,"m":0},{"n":"InitialLot","l":"Lot-ka bilowga (marka Auto Lot damman yahay)","g":10,"k":0,"t":"d","x":66,"r":0,"m":0},{"n":"Enable_Dynamic_Risk","l":"Enable Dynamic Risk","g":10,"k":0,"t":"b","x":67,"r":0,"m":0},{"n":"DynRisk_Loss_Trigger","l":"Immisa khasaare kadib ayaa khatarta la yareeyaa","g":10,"k":0,"t":"i","x":68,"r":0,"m":0},{"n":"DynRisk_Reduced_Pct","l":"Khatarta la yareeyay (%)","g":10,"k":0,"t":"d","x":69,"r":0,"m":0},{"n":"MagicNumber","l":"Magic number - aqoonsiga bot-ka","g":10,"k":2,"t":"i"},{"n":"Inp_Daily_Loss_Limit_Percent","l":"Xadka khasaaraha maalinlaha (%)","g":11,"k":1,"t":"d"},{"n":"Max_Losses_Per_Day","l":"Khasaare ugu badan maalintii","g":11,"k":0,"t":"i","x":70,"r":0,"m":0},{"n":"Max_Consecutive_Losses","l":"Khasaare isku xigta oo ugu badan","g":11,"k":0,"t":"i","x":71,"r":0,"m":0},{"n":"Max_True_Consecutive_Losses","l":"shabaqa ammaanka DHABTA ah","g":11,"k":0,"t":"i","x":72,"r":0,"m":0},{"n":"Inp_Max_Total_Drawdown_Pct","l":"Drawdown-ka guud ee ugu badan (%)","g":11,"k":1,"t":"d"},{"n":"Enable_ExtraSafety","l":"Ammaan dheeraad ah (on/off)","g":11,"k":0,"t":"b","x":73,"r":1,"m":0},{"n":"Daily_Profit_Target_Pct","l":"Bartilmaameedka faa'iidada maalinlaha (%)","g":11,"k":0,"t":"d","x":74,"r":0,"m":0},{"n":"Weekly_Profit_Target_Pct","l":"Bartilmaameedka faa'iidada usbuucle (%)","g":11,"k":0,"t":"d","x":75,"r":0,"m":0},{"n":"Enable_Daily_Profit_Lock","l":"Enable Daily Profit Lock","g":11,"k":0,"t":"b","x":76,"r":0,"m":0},{"n":"Enable_Weekly_Profit_Lock","l":"Enable Weekly Profit Lock","g":11,"k":0,"t":"b","x":77,"r":0,"m":0},{"n":"Enable_PortfolioMgmt","l":"Maareynta portfolio (on/off)","g":11,"k":0,"t":"b","x":78,"r":0,"m":0},{"n":"Max_Portfolio_Risk","l":"Khatarta guud ee ugu badan (%)","g":11,"k":0,"t":"d","x":79,"r":0,"m":0},{"n":"Equity_Protection_Pct","l":"Ilaalinta equity-ga (%)","g":11,"k":0,"t":"d","x":80,"r":0,"m":0},{"n":"SLTP_Mode","l":"Halka SL/TP laga qaato: FIXED (pips go'an) / ATR / SMC_PRO","g":12,"k":0,"t":"e","x":81,"r":0,"m":0,"o":[[0,"FIXED"],[1,"ATR"],[2,"SMC_PRO"]]},{"n":"StopLoss_Pips_Fixed","l":"SL: fogaanta (pips) - habka FIXED","g":12,"k":0,"t":"i","x":82,"r":0,"m":0},{"n":"TakeProfit_Pips_Fixed","l":"TP: fogaanta (pips) - habka FIXED","g":12,"k":0,"t":"i","x":83,"r":0,"m":0},{"n":"ATR_Period_Core","l":"Muddada ATR-ka aasaasiga ah","g":12,"k":0,"t":"i","x":84,"r":0,"m":0},{"n":"Safety_Buffer_Pips","l":"Buffer-ka ammaanka (pips)","g":12,"k":0,"t":"i","x":85,"r":0,"m":0},{"n":"Enforce_Min_RR","l":"Khasab ka dhig RR-ga ugu yar (on/off)","g":12,"k":0,"t":"b","x":86,"r":0,"m":0},{"n":"Min_SL_ATR_Floor","l":"SL-ka UGU YAR (x ATR)","g":12,"k":0,"t":"d","x":87,"r":0,"m":0},{"n":"Min_SL_Spread_Mult","l":"SL-ku waa inuu >= N x spread noqdaa","g":12,"k":0,"t":"d","x":88,"r":0,"m":0},{"n":"Debug_SLTP_Log","l":"qor RAAC-RAACA SL/TP talaabo kasta (Experts log)","g":12,"k":0,"t":"b","x":89,"r":0,"m":0},{"n":"Skip_If_SL_Clamped","l":"haddii SL la ballaadhiyo -> KA TAG (halkii la sii wado)","g":12,"k":0,"t":"b","x":90,"r":0,"m":0},{"n":"Zone_SL_Min_ATR","l":"SD-gu SL-kiisa DISTAL+0.35ATR buu leeyahay","g":12,"k":0,"t":"d","x":91,"r":0,"m":0},{"n":"RR_Skip_If_Short","l":"Haddii bartilmaameedku ku filneyn -> KA TAG (halkii TP la riixi lahaa)","g":12,"k":0,"t":"b","x":92,"r":0,"m":0},{"n":"Force_RR_From_SL","l":"TP = SL x Min_RR_Ratio (RR 1.3 KHASAB - xeelad kastaa)","g":12,"k":0,"t":"b","x":93,"r":0,"m":0},{"n":"Min_RR_Ratio","l":"Min RR Ratio","g":12,"k":0,"t":"d","x":94,"r":0,"m":0},{"n":"TargetProfitUSD","l":"Bartilmaameedka faa'iidada ($) - 0 = damman","g":12,"k":0,"t":"d","x":95,"r":0,"m":0},{"n":"Rev_Structural_SL","l":"Rogmasho: SL ku saleysan qaab-dhismeedka (on/off)","g":12,"k":0,"t":"b","x":96,"r":0,"m":0},{"n":"Rev_SL_Lookback","l":"Rogmasho SL: immisa shumac dib loo eegayo","g":12,"k":0,"t":"i","x":97,"r":0,"m":0},{"n":"Rev_SL_Max_ATR","l":"Rogmasho SL: fogaanta ugu badan (x ATR)","g":12,"k":0,"t":"d","x":98,"r":0,"m":0},{"n":"SMC_SL_Max_ATR","l":"SMC: SL-ga ugu fog (x ATR)","g":12,"k":0,"t":"d","x":99,"r":0,"m":0},{"n":"Liquidity_TP_Min_R","l":"Liquidity TP: R-ga ugu yar","g":12,"k":0,"t":"d","x":100,"r":0,"m":0},{"n":"EnableBreakEven","l":"BE - TP-ga ha jarin","g":13,"k":1,"t":"b"},{"n":"BE_Trigger_R","l":"TP=3R. 2.0R = 2/3 jidka. Haddii BE la shido, ka hor ma jarayo.","g":13,"k":0,"t":"d","x":101,"r":0,"m":0},{"n":"BE_Lock_R","l":"BE Lock R","g":13,"k":0,"t":"d","x":102,"r":0,"m":0},{"n":"Enable_FastBE","l":"FAST BE - kaliya TP Ladder la isticmaalo - HA SHIDIN","g":13,"k":0,"t":"b","x":103,"r":0,"m":0},{"n":"FastBE_Trigger_R","l":"Fast BE: immisa R faa'iido kadib","g":13,"k":0,"t":"d","x":104,"r":0,"m":0},{"n":"FastBE_Lock_R","l":"Fast BE: faa'iidada la xidhayo (R)","g":13,"k":0,"t":"d","x":105,"r":0,"m":0},{"n":"Enable_ATR_Trailing","l":"Enable ATR Trailing","g":13,"k":0,"t":"b","x":106,"r":1,"m":0},{"n":"ATR_Trail_Start_Mult","l":"ATR: multiplier-ka bilowga trail-ka","g":13,"k":0,"t":"d","x":107,"r":0,"m":0},{"n":"ATR_Trail_Step_Mult","l":"ATR: multiplier-ka tallaabada trail-ka","g":13,"k":0,"t":"d","x":108,"r":0,"m":0},{"n":"EnableTrailingStop","l":"Trailing stop - SL sicirka raaca (on/off)","g":13,"k":0,"t":"b","x":109,"r":1,"m":0},{"n":"Trail_Start_R","l":"Trail: R-ga bilowga","g":13,"k":0,"t":"d","x":110,"r":0,"m":0},{"n":"Trail_Step_R","l":"Trail: R-ga tallaabada","g":13,"k":0,"t":"d","x":111,"r":0,"m":0},{"n":"TrailingStartPips","l":"Trailing: pips-ka bilowga","g":13,"k":0,"t":"i","x":112,"r":0,"m":0},{"n":"TrailingStepPips","l":"Trailing: pips-ka tallaabada","g":13,"k":0,"t":"i","x":113,"r":0,"m":0},{"n":"EnablePartialClose","l":"Xidh qayb ka mid ah trade-ka (on/off)","g":14,"k":0,"t":"b","x":114,"r":1,"m":0},{"n":"PartialClosePips","l":"Qayb-xidhid: pips","g":14,"k":0,"t":"i","x":115,"r":0,"m":0},{"n":"PartialClosePercent","l":"Qayb-xidhid: boqolkiiba","g":14,"k":0,"t":"i","x":116,"r":0,"m":0},{"n":"Enable_ScaleOut","l":"Enable ScaleOut","g":14,"k":0,"t":"b","x":117,"r":1,"m":0},{"n":"ScaleOut_R1_Pct","l":"Scale out +1R: boqolkiiba la xidhayo","g":14,"k":0,"t":"i","x":118,"r":0,"m":0},{"n":"ScaleOut_R2_Pct","l":"Scale out +2R: boqolkiiba la xidhayo","g":14,"k":0,"t":"i","x":119,"r":0,"m":0},{"n":"Prop_Scale_Out","l":"Prop Scale Out","g":14,"k":0,"t":"b","x":120,"r":0,"m":0},{"n":"Enable_TP_Ladder","l":"TP Ladder - TP qaybsan + SL tallaabo (on/off) - HA SHIDIN","g":14,"k":0,"t":"b","x":121,"r":1,"m":0},{"n":"TPL_TP1_R","l":"TP Ladder: TP1 immisa R (xidh 50%, SL breakeven)","g":14,"k":0,"t":"d","x":122,"r":1,"m":0},{"n":"TPL_TP2_R","l":"TP Ladder: TP2 immisa R (xidh 25%)","g":14,"k":0,"t":"d","x":123,"r":1,"m":0},{"n":"TPL_TP3_R","l":"TP Ladder: TP3 immisa R (xidh inta hadhay)","g":14,"k":0,"t":"d","x":124,"r":1,"m":0},{"n":"TPL_TP4_R","l":"TP Ladder: TP4 immisa R","g":14,"k":0,"t":"d","x":125,"r":1,"m":0},{"n":"TPL_TP1_Pct","l":"TP Ladder: TP1 boqolkiiba","g":14,"k":0,"t":"i","x":126,"r":1,"m":0},{"n":"TPL_TP2_Pct","l":"TP Ladder: TP2 boqolkiiba","g":14,"k":0,"t":"i","x":127,"r":1,"m":0},{"n":"TPL_TP3_Pct","l":"TP Ladder: TP3 boqolkiiba","g":14,"k":0,"t":"i","x":128,"r":1,"m":0},{"n":"TPL_TP4_Pct","l":"TP Ladder: TP4 boqolkiiba","g":14,"k":0,"t":"i","x":129,"r":1,"m":0},{"n":"TPL_Trail_Final","l":"TP Ladder: trail qaybta u dambaysa (on/off)","g":14,"k":0,"t":"b","x":130,"r":0,"m":0},{"n":"TPL_Trail_Step_R","l":"TP Ladder: tallaabada trail-ka (R)","g":14,"k":0,"t":"d","x":131,"r":0,"m":0},{"n":"Enable_USD_ProfitLock","l":"Enable USD ProfitLock","g":14,"k":0,"t":"b","x":132,"r":1,"m":0},{"n":"LockProfit_USD","l":"Faa'iidada ($) ee la xidhayo","g":14,"k":0,"t":"d","x":133,"r":0,"m":0},{"n":"LockProfit_KeepPct","l":"Faa'iidada: boqolkiiba la haynayo","g":14,"k":0,"t":"d","x":134,"r":0,"m":0},{"n":"LockProfit_Trigger_R","l":"Faa'iidada: R-ga shaqaynaya","g":14,"k":0,"t":"d","x":135,"r":0,"m":0},{"n":"Enable_Stagnant_Exit","l":"Xidh trade aan waxba qabanayn N shumac kadib (on/off)","g":15,"k":0,"t":"b","x":136,"r":1,"m":0},{"n":"Stagnant_Bars","l":"Immisa shumac trade-ku furan yahay ka hor hubinta","g":15,"k":0,"t":"i","x":137,"r":0,"m":0},{"n":"Stagnant_Max_R","l":"Xidh haddii faa'iidadu u dhaxayso -R iyo +R (trade taagan)","g":15,"k":0,"t":"d","x":138,"r":0,"m":0},{"n":"Close_Profit_Before_News","l":"Xidh trade faa'iido leh warka ka hor (on/off)","g":15,"k":0,"t":"b","x":139,"r":0,"m":0},{"n":"News_Exit_Minutes","l":"Daqiiqado warka ka hor oo la xidhayo","g":15,"k":0,"t":"i","x":140,"r":0,"m":0},{"n":"News_Exit_Min_Profit_USD","l":"Kaliya xidh haddii faa'iidadu ka badan tahay ($) - 0 = mid kasta","g":15,"k":0,"t":"d","x":141,"r":0,"m":0},{"n":"News_Exit_High_Only","l":"Kaliya wararka WEYN (false = Weyn + Dhexe)","g":15,"k":0,"t":"b","x":142,"r":0,"m":0},{"n":"News_Exit_Close_Losers","l":"Sidoo kale xidh trade khasaare leh (on/off)","g":15,"k":0,"t":"b","x":143,"r":0,"m":0},{"n":"CloseWeekend","l":"Xidh trade-yada dhammaadka usbuuca (on/off)","g":15,"k":0,"t":"b","x":144,"r":0,"m":0},{"n":"Enable_Market_Regime","l":"Kaliya ganacso xaaladda suuqa ee saxda ah (on/off)","g":16,"k":0,"t":"b","x":145,"r":0,"m":0},{"n":"Regime_Lookback","l":"Xaaladda suuqa: immisa shumac dib loo eegayo","g":16,"k":0,"t":"i","x":146,"r":0,"m":0},{"n":"Filter2_ADX_Strong","l":"ADX filter","g":16,"k":0,"t":"b","x":147,"r":0,"m":0},{"n":"Filter2_ADX_MinLevel","l":"ADX ugu yar (20 = trend caadi, sare = adag)","g":16,"k":0,"t":"d","x":148,"r":0,"m":0},{"n":"Filter2_ADX_Period","l":"Muddada ADX-ga filter-ka","g":16,"k":0,"t":"i","x":149,"r":0,"m":0},{"n":"Filter_Low_Volatility","l":"ha ganacsan suuq aan dhaqaaqayn","g":16,"k":0,"t":"b","x":150,"r":0,"m":0},{"n":"Min_ATR_Pips","l":"ATR ugu yar (pips)","g":16,"k":0,"t":"d","x":151,"r":0,"m":0},{"n":"MaxSpread","l":"Spread ugu badan (points)","g":16,"k":0,"t":"i","x":152,"r":0,"m":0},{"n":"Enable_Dynamic_Spread","l":"xadka spread-ka oo ATR raaca","g":16,"k":0,"t":"b","x":153,"r":0,"m":0},{"n":"DynSpread_ATR_Mult","l":"Spread firfircoon: multiplier ATR","g":16,"k":0,"t":"d","x":154,"r":0,"m":0},{"n":"DynSpread_Min_Cap","l":"Spread firfircoon: xadka ugu hooseeya","g":16,"k":0,"t":"i","x":155,"r":0,"m":0},{"n":"DynSpread_Max_Cap","l":"Spread firfircoon: xadka ugu sarreeya","g":16,"k":0,"t":"i","x":156,"r":0,"m":0},{"n":"Enable_Extension_Guard","l":"ha eryin spike (sicirka aad uga fog EMA50)","g":16,"k":0,"t":"b","x":157,"r":0,"m":0},{"n":"Max_Extension_ATR","l":"Fogaanta ugu badan EMA50 (x ATR) - hoos = adag","g":16,"k":0,"t":"d","x":158,"r":0,"m":0},{"n":"EMA_Filter_On","l":"EMA = FILTER jihada (trade ma furo) · on/off","g":17,"k":1,"t":"b"},{"n":"EMA_F_H1_Period","l":"EMA-ga H1 (dahab)","g":17,"k":0,"t":"i","x":159,"r":1,"m":0},{"n":"EMA_F_H4_Period","l":"EMA-ga H4 (buluug)","g":17,"k":0,"t":"i","x":160,"r":1,"m":0},{"n":"EMA_F_Strict","l":"true = EMA50 H1 + EMA200 H4 isku hagaagsan · false = H4 kaliya","g":17,"k":0,"t":"b","x":161,"r":0,"m":0},{"n":"EMA_Range_Guard","l":"RANGE (suuqu isku fadhiyo) -> ha ganacsan","g":17,"k":0,"t":"b","x":162,"r":1,"m":0},{"n":"EMA_Range_Bars","l":"RANGE - H1 shumacyada la eegayo (24 = 1 maalin)","g":17,"k":0,"t":"i","x":163,"r":0,"m":0},{"n":"EMA_Range_Crosses","l":"range adag oo keliya","g":17,"k":0,"t":"i","x":164,"r":0,"m":0},{"n":"EMA_Range_Slope_ATR","l":"EMA50 H1 isbeddelka 6 saac < x ATR H1 -> siman","g":17,"k":0,"t":"d","x":165,"r":0,"m":0},{"n":"Enable_TrendFollow","l":"Raac trendka - albaabka xeeladaha trend (on/off)","g":18,"k":0,"t":"b","x":166,"r":0,"m":0},{"n":"TF_Require_Volume","l":"TF Require Volume","g":18,"k":0,"t":"b","x":167,"r":0,"m":0},{"n":"TF_Volume_Ratio","l":"Trend: saamiga volume-ka loo baahan yahay","g":18,"k":0,"t":"d","x":168,"r":0,"m":0},{"n":"TF_Require_MACD","l":"TF Require MACD","g":18,"k":0,"t":"b","x":169,"r":0,"m":0},{"n":"MACD_Fast","l":"MACD degdeg (fast)","g":18,"k":0,"t":"i","x":170,"r":0,"m":0},{"n":"MACD_Slow","l":"MACD gaabis (slow)","g":18,"k":0,"t":"i","x":171,"r":0,"m":0},{"n":"MACD_Signal","l":"MACD signal","g":18,"k":0,"t":"i","x":172,"r":0,"m":0},{"n":"Max_Same_Currency_Exposure","l":"Trade ugu badan oo isku lacag ku sharad ah (0 = damman)","g":19,"k":0,"t":"i","x":173,"r":0,"m":0},{"n":"Enable_Correlation_Filter","l":"Filter lammaanayaal isku xidhan (on/off)","g":19,"k":0,"t":"b","x":174,"r":0,"m":0},{"n":"Correlation_Groups","l":"Kooxaha lammaanayaasha isku xidhan ( | kala saar kooxaha )","g":19,"k":0,"t":"s","x":175,"r":0,"m":0},{"n":"Max_Correlated_Same_Dir","l":"Trade isku jiho ah oo ugu badan lammaanayaal isku xidhan","g":19,"k":0,"t":"i","x":176,"r":0,"m":0},{"n":"Require_Pattern_Confirm","l":"Kaliya ganacso marka qaab chart uu xaqiijiyo (on/off)","g":19,"k":0,"t":"b","x":177,"r":0,"m":0},{"n":"Pattern_Tol_ATR","l":"Qaab: dulqaadka Double Top / Bottom (x ATR)","g":19,"k":0,"t":"d","x":178,"r":0,"m":0},{"n":"Pattern_Lookback","l":"Qaab: immisa shumac la baadhayo","g":19,"k":0,"t":"i","x":179,"r":0,"m":0},{"n":"Use_Zone_Filter_For_Entry","l":"FILTER MEEL: kaliya gal zone SR (BUY support / SELL resistance) (on/off)","g":19,"k":0,"t":"b","x":180,"r":0,"m":0},{"n":"Enable_Session_Filter","l":"Filter-ka session-ka suuqa (on/off)","g":20,"k":0,"t":"b","x":181,"r":0,"m":0},{"n":"Trade_Asian","l":"Ganacso session-ka Aasiya","g":20,"k":0,"t":"b","x":182,"r":0,"m":0},{"n":"Trade_London","l":"Ganacso session-ka London (on/off)","g":20,"k":0,"t":"b","x":183,"r":0,"m":0},{"n":"Trade_NewYork","l":"Ganacso session-ka New York (on/off)","g":20,"k":0,"t":"b","x":184,"r":0,"m":0},{"n":"Trade_Overlap_Only","l":"Kaliya waqtiga London & New York isku dhacaan (on/off)","g":20,"k":0,"t":"b","x":185,"r":0,"m":0},{"n":"Use_Time_Filter","l":"Kaliya ganacso saacadaha aad dooratay (on/off)","g":20,"k":0,"t":"b","x":186,"r":0,"m":0},{"n":"Start_Hour","l":"Saacadda bilowga (waqtiga broker-ka)","g":20,"k":0,"t":"i","x":187,"r":0,"m":0},{"n":"End_Hour","l":"Saacadda dhammaadka (waqtiga broker-ka)","g":20,"k":0,"t":"i","x":188,"r":0,"m":0},{"n":"Session_Broker_GMT_Offset","l":"GMT+3 (DST) - broker-kaagu GMT+3 ayuu yahay","g":20,"k":0,"t":"i","x":189,"r":0,"m":0},{"n":"Auto_GMT_Offset","l":"farqiga GMT tooska u hel (DST) - la talo siiyay true","g":20,"k":0,"t":"b","x":190,"r":0,"m":0},{"n":"Enable_Candle_Sync","l":"Ku xir shaqada bilowga shumaca cusub (on/off)","g":20,"k":0,"t":"b","x":191,"r":0,"m":0},{"n":"Sync_TF","l":"Timeframe-ka sync-ga shumaca","g":20,"k":0,"t":"e","x":192,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"Inp_EnableNewsFilter","l":"jooji ganacsiga waqtiga wararka (tester kuma shaqeeyo)","g":21,"k":1,"t":"b"},{"n":"News_Calendar_URL","l":"URL-ka kalandarka wararka","g":21,"k":2,"t":"s"},{"n":"News_Broker_GMT_Offset","l":"GMT+3 (DST) - waqtiga wararka","g":21,"k":0,"t":"i","x":193,"r":0,"m":0},{"n":"News_Refresh_Minutes","l":"Daqiiqado la cusboonaysiiyo wararka","g":21,"k":0,"t":"i","x":194,"r":0,"m":0},{"n":"News_Filter_High","l":"Xannib wararka saameyn WEYN leh (on/off)","g":21,"k":0,"t":"b","x":195,"r":0,"m":0},{"n":"News_Filter_Medium","l":"Xannib wararka saameyn DHEXE leh (on/off)","g":21,"k":0,"t":"b","x":196,"r":0,"m":0},{"n":"News_Filter_Low","l":"Xannib wararka saameyn YAR leh (on/off)","g":21,"k":0,"t":"b","x":197,"r":0,"m":0},{"n":"MinutesBeforeNews","l":"Daqiiqado warka ka hor oo la joojinayo","g":21,"k":0,"t":"i","x":198,"r":0,"m":0},{"n":"MinutesAfterNews","l":"Daqiiqado warka ka dib oo la joojinayo","g":21,"k":0,"t":"i","x":199,"r":0,"m":0},{"n":"News_Block_If_Fetch_Fails","l":"Xannib haddii wararka la soo dejin waayo (on/off)","g":21,"k":0,"t":"b","x":200,"r":0,"m":0},{"n":"SR_Zone_TF","l":"SR: timeframe-ka zone-yada","g":22,"k":0,"t":"e","x":201,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"SR_Lookback","l":"SR: immisa shumac dib loo eegayo (HTF)","g":22,"k":0,"t":"i","x":202,"r":0,"m":0},{"n":"SR_Swing_Len","l":"SR: dhererka fractal-ka zone-yada","g":22,"k":0,"t":"i","x":203,"r":0,"m":0},{"n":"SR_Zone_ATR_Mult","l":"SR: ballaca zone-ka (x ATR HTF)","g":22,"k":0,"t":"d","x":204,"r":0,"m":0},{"n":"SR_StrengthBuffer","l":"SR: ballaca ugu yar (pips) - ATR ayaa badiyaa","g":22,"k":0,"t":"d","x":205,"r":0,"m":0},{"n":"SR_MinTouches","l":"SR: taabasho DHAB ah ugu yar","g":22,"k":0,"t":"i","x":206,"r":0,"m":0},{"n":"SR_Min_Zone_Age","l":"SR Min Zone Age","g":22,"k":0,"t":"i","x":207,"r":0,"m":0},{"n":"SR_Mode","l":"SR: habka (BOUNCE / BREAK-RETEST / LABADABA)","g":22,"k":0,"t":"e","x":208,"r":0,"m":0,"o":[[0,"BOUNCE"],[1,"BREAK_RETEST"],[2,"BOTH"]]},{"n":"SR_Use_Flip","l":"SR: zone jabay -> door beddel (R<->S) (on/off)","g":22,"k":0,"t":"b","x":209,"r":0,"m":0},{"n":"SR_Break_Buf_ATR","l":"SR: jabku waa inuu ka fog yahay (x ATR HTF)","g":22,"k":0,"t":"d","x":210,"r":0,"m":0},{"n":"SR_Max_Dist_ATR","l":"SR: fogaanta ugu badan sicir->zone (x ATR)","g":22,"k":0,"t":"d","x":211,"r":0,"m":0},{"n":"SR_Max_Penetration_ATR","l":"SR: intee shumacu zone-ka ka dhex geli karo (x ATR)","g":22,"k":0,"t":"d","x":212,"r":0,"m":0},{"n":"SR_Require_Rejection","l":"SR: u baahan diidmo shumac (rejection) (on/off)","g":22,"k":0,"t":"b","x":213,"r":0,"m":0},{"n":"SR_Rej_Wick_Pct","l":"SR: dabada shumaca / range (0.40 = 40%)","g":22,"k":0,"t":"d","x":214,"r":0,"m":0},{"n":"SR_Rej_Wick_Body","l":"SR: dabada / jidhka shumaca","g":22,"k":0,"t":"d","x":215,"r":0,"m":0},{"n":"SR_Rej_ClosePos","l":"SR: meesha close-ku ku yaal shumaca (0..1)","g":22,"k":0,"t":"d","x":216,"r":0,"m":0},{"n":"SR_Require_Volume","l":"SR: u baahan volume (on/off)","g":22,"k":0,"t":"b","x":217,"r":0,"m":0},{"n":"SR_Vol_Ratio","l":"SR: saamiga volume (marka kor la shido)","g":22,"k":0,"t":"d","x":218,"r":0,"m":0},{"n":"SR_Cooldown_Bars","l":"SR: shumac u dhexeeya laba trade oo isku zone ah","g":22,"k":0,"t":"i","x":219,"r":0,"m":0},{"n":"SR_SL_Buffer_ATR","l":"SL-ku intee buu darafka zone-ka ka baxsanaadaa (x ATR). Hore: 0.25 go'an","g":22,"k":0,"t":"d","x":220,"r":0,"m":0},{"n":"SR_SL_Multiplier","l":"SR: SL (x ATR) - marka zone la waayo","g":22,"k":0,"t":"d","x":221,"r":0,"m":0},{"n":"SR_TP_Multiplier","l":"SR: TP (x ATR) - marka zone la waayo","g":22,"k":0,"t":"d","x":222,"r":0,"m":0},{"n":"SR_Zone_TP","l":"SR: TP = zone-ka ka soo horjeeda (on/off)","g":22,"k":0,"t":"b","x":223,"r":0,"m":0},{"n":"SR_Debug","l":"SR: qor sabab kasta oo diidmo ah","g":22,"k":0,"t":"b","x":224,"r":0,"m":0},{"n":"SMC_Require_CHoCH","l":"CHoCH iyo EMA trend way is diidayeen - BOS OB (trend) ayaa ugu fiican","g":23,"k":0,"t":"b","x":225,"r":0,"m":0},{"n":"SMC_Require_FVG","l":"SMC Require FVG","g":23,"k":0,"t":"b","x":226,"r":0,"m":0},{"n":"SMC_Require_Sweep","l":"SMC Require Sweep","g":23,"k":0,"t":"b","x":227,"r":0,"m":0},{"n":"SMC_Require_PremDisc","l":"BUY discount / SELL premium kaliya - dhexda ma jirto","g":23,"k":0,"t":"b","x":228,"r":0,"m":0},{"n":"SMC_Require_LTF_CHoCH","l":"SMC Require LTF CHoCH","g":23,"k":0,"t":"b","x":229,"r":0,"m":0},{"n":"SMC_Use_HTF_Bias","l":"EMA filter-ka ayaa jihada qabta","g":23,"k":0,"t":"b","x":230,"r":0,"m":0},{"n":"SMC_MinTouches","l":"SMC: immisa jeer heerka la taabtay","g":23,"k":0,"t":"i","x":231,"r":0,"m":0},{"n":"OB_Mitigation_Perc","l":"Order Block: boqolkiiba la buuxiyay (%)","g":23,"k":0,"t":"d","x":232,"r":0,"m":0},{"n":"SMC_Lookback","l":"SMC: immisa shumac dib loo eegayo","g":23,"k":0,"t":"i","x":233,"r":0,"m":0},{"n":"SMC_StrengthBuffer","l":"SMC: xoogga ugu yar (buffer)","g":23,"k":0,"t":"d","x":234,"r":0,"m":0},{"n":"SMC_SL_Multiplier","l":"SMC: SL (x ATR)","g":23,"k":0,"t":"d","x":235,"r":0,"m":0},{"n":"SMC_TP_Multiplier","l":"SMC: TP (x ATR)","g":23,"k":0,"t":"d","x":236,"r":0,"m":0},{"n":"SMC_SwingLen","l":"SMC: dhererka swing-ga","g":23,"k":0,"t":"i","x":237,"r":0,"m":0},{"n":"SMC_Equilibrium_Pct","l":"SMC: dhexda (equilibrium) %","g":23,"k":0,"t":"d","x":238,"r":0,"m":0},{"n":"SMC_Impulse_ATR","l":"SMC Impulse ATR","g":23,"k":0,"t":"d","x":239,"r":0,"m":0},{"n":"SMC_Impulse_MaxBars","l":"SMC: shumac ugu badan OB -> jab","g":23,"k":0,"t":"i","x":240,"r":0,"m":0},{"n":"SMC_BOS_MaxAge","l":"30 shumac M5 = 2.5 saac kaliya -> 8 saac","g":23,"k":0,"t":"i","x":241,"r":0,"m":0},{"n":"SMC_OB_BodyOnly","l":"SMC: zone-ka OB = jidhka kaliya (false = shumaca oo dhan)","g":23,"k":0,"t":"b","x":242,"r":0,"m":0},{"n":"SMC_Sweep_Lookback","l":"SMC: sweep - shumac dib loo eegayo","g":23,"k":0,"t":"i","x":243,"r":0,"m":0},{"n":"SMC_Max_Dist_ATR","l":"SMC Max Dist ATR","g":23,"k":0,"t":"d","x":244,"r":0,"m":0},{"n":"SMC_Entry_Buf_ATR","l":"SMC: dulqaadka gelitaanka (x ATR)","g":23,"k":0,"t":"d","x":245,"r":0,"m":0},{"n":"SMC_Require_Rejection","l":"SMC: u baahan diidmo shumac (on/off)","g":23,"k":0,"t":"b","x":246,"r":0,"m":0},{"n":"SMC_Rej_Wick_Pct","l":"SMC: dabada shumaca / range","g":23,"k":0,"t":"d","x":247,"r":0,"m":0},{"n":"SMC_Cooldown_Bars","l":"SMC: shumac u dhexeeya laba trade oo isku OB ah","g":23,"k":0,"t":"i","x":248,"r":0,"m":0},{"n":"SMC_Zone_TP","l":"SMC: TP = liquidity-ga xiga (on/off)","g":23,"k":0,"t":"b","x":249,"r":0,"m":0},{"n":"SMC_Debug","l":"SMC: qor sabab kasta oo diidmo ah","g":23,"k":0,"t":"b","x":250,"r":0,"m":0},{"n":"SMC_HTF_TF","l":"SMC: timeframe-ka sare","g":23,"k":0,"t":"e","x":251,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"SMC_HTF_EMA","l":"SMC: EMA-ga timeframe-ka sare","g":23,"k":0,"t":"i","x":252,"r":0,"m":0},{"n":"SMC_Structural_SL","l":"SMC: SL ku saleysan qaab-dhismeedka (on/off)","g":23,"k":0,"t":"b","x":253,"r":0,"m":0},{"n":"SMC_LTF","l":"SMC: timeframe-ka hoose","g":23,"k":0,"t":"e","x":254,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"SMC_Sniper_CHoCH","l":"Sniper -> sug CHoCH M5 ka hor gelitaanka (on/off)","g":23,"k":0,"t":"b","x":255,"r":0,"m":0},{"n":"SMC_Touch_Bars","l":"Sniper - OB-ga waa inuu la taabtay shumacyadan gudahood (12 x M5 = 1 saac)","g":23,"k":0,"t":"i","x":256,"r":0,"m":0},{"n":"Inp_Entry_Max_Dist_Pips","l":"Entry Max Dist Pips","g":23,"k":0,"t":"d","x":257,"r":0,"m":8},{"n":"SMC_Extreme_Bars","l":"OB-gu waa inuu yahay salka/dusha shumacyadan (48 x M5 = 4 saac) - 0 = damman","g":23,"k":0,"t":"i","x":258,"r":0,"m":0},{"n":"SMC_Extreme_Tol_ATR","l":"dulqaadka cidhifka OB-ga (x ATR)","g":23,"k":0,"t":"d","x":259,"r":0,"m":0},{"n":"Stars_Enable","l":"★ scoring (on/off)","g":24,"k":0,"t":"b","x":260,"r":0,"m":0},{"n":"Stars_Min","l":"★ ugu yar: 2 = ★ ka tag · 1 = dhammaan","g":24,"k":1,"t":"i"},{"n":"Lot_Pct_2Star","l":"★★ = lot-ka boqolkiisa (★★★ = 100%)","g":24,"k":1,"t":"d"},{"n":"ExecMode","l":"Habka fulinta MARKET order-ka (SMART / MARKET). v66.1: pending order (LIMIT) gebi ahaanba waa la saaray - EXEC","g":25,"k":0,"t":"e","x":261,"r":0,"m":0,"o":[[0,"INSTANT"],[1,"SMART"],[2,"LIMIT"]]},{"n":"Enable_ECN_StopFallback","l":"ECN: SL/TP kadib dir haddii la diido (on/off)","g":25,"k":0,"t":"b","x":262,"r":0,"m":0},{"n":"MaxRetries","l":"Isku day mar kale oo ugu badan","g":25,"k":0,"t":"i","x":263,"r":0,"m":0},{"n":"RetryDelayMs","l":"Daahitaanka isku dayga (ms)","g":25,"k":0,"t":"i","x":264,"r":0,"m":0},{"n":"MaxSlippagePips","l":"Slippage ugu badan (pips)","g":25,"k":0,"t":"i","x":265,"r":0,"m":0},{"n":"UseVirtualOrders","l":"Amaro virtual ah - SL/TP xasuusta ku hay (on/off)","g":25,"k":0,"t":"b","x":266,"r":0,"m":0},{"n":"Enable_Stealth_Mode","l":"Hab qarsoodi - broker-ku SL/TP ha arkin (on/off)","g":25,"k":0,"t":"b","x":267,"r":0,"m":0},{"n":"Loose_Entry_Mode","l":"HAB DEBECSAN: trade badan, filter yar (on/off)","g":25,"k":0,"t":"b","x":268,"r":0,"m":0},{"n":"Simple_Mode","l":"HAB FUDUD: SL/TP go'an, maamul automatic ah ma jiro (on/off)","g":25,"k":0,"t":"b","x":269,"r":0,"m":0},{"n":"PropMode","l":"Habka Prop Firm (NONE = damman)","g":26,"k":0,"t":"e","x":270,"r":0,"m":0,"o":[[0,"NONE"],[1,"FTMO"],[2,"MFF"],[3,"CUSTOM"]]},{"n":"Prop_Max_Daily_DD","l":"Prop: drawdown maalinle ugu badan (%)","g":26,"k":0,"t":"d","x":271,"r":0,"m":0},{"n":"Prop_Max_Total_DD","l":"Prop: drawdown guud ugu badan (%)","g":26,"k":0,"t":"d","x":272,"r":0,"m":0},{"n":"Prop_Min_Trading_Days","l":"Prop: maalmo ganacsi ugu yar","g":26,"k":0,"t":"d","x":273,"r":0,"m":0},{"n":"Prop_No_Weekend","l":"Prop: ha ganacsan dhammaadka usbuuca (on/off)","g":26,"k":0,"t":"b","x":274,"r":0,"m":0},{"n":"Prop_No_News","l":"Prop: ha ganacsan waqtiga wararka (on/off)","g":26,"k":0,"t":"b","x":275,"r":0,"m":0},{"n":"Prop_Consistency_Max","l":"Prop: xeerka is-waafaqidda ugu badan (%)","g":26,"k":0,"t":"d","x":276,"r":0,"m":0},{"n":"EnableTelegram","l":"Dir digniinaha Telegram (on/off)","g":27,"k":0,"t":"b","x":277,"r":0,"m":0},{"n":"TG_BotToken","l":"Telegram: token-ka bot-ka (GELI halkan)","g":27,"k":2,"t":"s"},{"n":"TG_ChatID","l":"Telegram: Chat ID (GELI halkan)","g":27,"k":2,"t":"s"},{"n":"TG_DailySummary","l":"Telegram: soo koobid maalinle (on/off)","g":27,"k":0,"t":"b","x":278,"r":0,"m":0},{"n":"TG_WeeklySummary","l":"Telegram: soo koobid usbuucle (on/off)","g":27,"k":0,"t":"b","x":279,"r":0,"m":0},{"n":"TG_DrawdownAlert","l":"Telegram: digniin drawdown (on/off)","g":27,"k":0,"t":"b","x":280,"r":0,"m":0},{"n":"TG_DrawdownAlertPct","l":"Telegram: boqolkiiba drawdown-ka digniinta","g":27,"k":0,"t":"d","x":281,"r":0,"m":0},{"n":"TG_TradeDetails","l":"Telegram: faahfaahinta trade-ka (on/off)","g":27,"k":0,"t":"b","x":282,"r":0,"m":0},{"n":"TG_ErrorAlerts","l":"Telegram: digniin qalad (on/off)","g":27,"k":0,"t":"b","x":283,"r":0,"m":0},{"n":"TG_TradeCloseAlert","l":"Telegram: digniin xidhitaanka trade (on/off)","g":27,"k":0,"t":"b","x":284,"r":0,"m":0},{"n":"TG_NewsAlert","l":"Telegram: digniin warar (on/off)","g":27,"k":0,"t":"b","x":285,"r":0,"m":0},{"n":"TG_RejectedAlerts","l":"Telegram: digniin trade la diiday (on/off)","g":27,"k":0,"t":"b","x":286,"r":0,"m":0},{"n":"TG_Reject_Cooldown_Min","l":"Telegram: daqiiqado u dhexeeya digniinaha diidmada","g":27,"k":0,"t":"i","x":287,"r":0,"m":0},{"n":"TG_Error_Cooldown_Sec","l":"Telegram: ilbiriqsiyo u dhexeeya digniinaha qaladka","g":27,"k":0,"t":"i","x":288,"r":0,"m":0},{"n":"EnableCloudDashboard","l":"Dashboard-ka cloud-ka (on/off)","g":28,"k":2,"t":"b"},{"n":"CloudDashboardURL","l":"URL-ka dashboard-ka","g":28,"k":2,"t":"s"},{"n":"CloudCommandURL","l":"URL-ka amarada","g":28,"k":2,"t":"s"},{"n":"Cloud_Bot_Name","l":"Magaca bootka ee dashboard-ka (gaar u ah EA kasta)","g":28,"k":2,"t":"s"},{"n":"Enable_Connection_Guard","l":"Ilaali xiriirka internet-ka (on/off)","g":28,"k":2,"t":"b"},{"n":"Enable_Reconnect_Alert","l":"Digniin marka xiriirku dib u soo noqdo (on/off)","g":28,"k":2,"t":"b"},{"n":"Cloud_Push_Seconds","l":"intee ilbiriqsi kasta ayaa xogta la dirayaa (hore 15). Kordhi = bandwidth yar","g":28,"k":2,"t":"i"},{"n":"Cloud_Cmd_Seconds","l":"amarrada intee ilbiriqsi kasta (hore 10)","g":28,"k":2,"t":"i"},{"n":"Cloud_Journal_Max","l":"immisa trade oo xidhan ayaa push kasta la dirayaa (hore 120)","g":28,"k":2,"t":"i"},{"n":"Cloud_Timeout_Ms","l":"Sug xogta guud (ms) - internet gaabis 8000-12000","g":28,"k":2,"t":"i"},{"n":"Journal_Timeout_Ms","l":"Sug jornalka (ms) - Render hurda 20000","g":28,"k":2,"t":"i"},{"n":"Journal_Batch_Max","l":"Trade tiro badan oo hal mar la diro (yaree = dhakhso)","g":28,"k":2,"t":"i"},{"n":"Journal_Only_New","l":"Kaliya kuwa cusub dir (ha dirin mar walba isku mid)","g":28,"k":2,"t":"b"},{"n":"UI_Premium","l":"chart-ka cusub (panel glass + xuduud dahab ah) - false = panel-kii hore","g":29,"k":0,"t":"b","x":289,"r":0,"m":0},{"n":"UI_Scale","l":"cabbirka panel-ka (0 = toos, DPI-ga shaashadda) · 1.0 / 1.25 / 1.5","g":29,"k":0,"t":"d","x":290,"r":0,"m":0},{"n":"UI_Show_EMA","l":"EMA qurxin chart-ka (daruur · glow · cross · qiimaha)","g":29,"k":0,"t":"b","x":291,"r":0,"m":0},{"n":"UI_Show_Zones","l":"zone-yada SD (★) + dhaqaaqa la filayo (ATR D1) chart-ka ku sawir","g":29,"k":0,"t":"b","x":292,"r":0,"m":0},{"n":"BrandName","l":"Magaca panel-ka","g":29,"k":0,"t":"s","x":293,"r":0,"m":0},{"n":"Watermark_Text","l":"Qoraalka watermark-ka","g":29,"k":0,"t":"s","x":294,"r":0,"m":0},{"n":"Watermark_Size","l":"Cabbirka watermark-ka","g":29,"k":0,"t":"i","x":295,"r":0,"m":0},{"n":"Watermark_Color","l":"Midabka watermark-ka","g":29,"k":0,"t":"c","x":296,"r":0,"m":0},{"n":"Show_Trade_Signals","l":"Tus calaamadaha signal-ka (on/off)","g":29,"k":0,"t":"b","x":297,"r":0,"m":0},{"n":"Show_Trade_Markers","l":"Tus calaamadaha trade-ka (on/off)","g":29,"k":0,"t":"b","x":298,"r":0,"m":0},{"n":"Marker_Size","l":"Cabbirka calaamadda","g":29,"k":0,"t":"i","x":299,"r":0,"m":0},{"n":"Buy_Marker_Color","l":"Midabka calaamadda BUY","g":29,"k":0,"t":"c","x":300,"r":0,"m":0},{"n":"Sell_Marker_Color","l":"Midabka calaamadda SELL","g":29,"k":0,"t":"c","x":301,"r":0,"m":0},{"n":"Show_Dynamic_Zones","l":"Tus zone-yada firfircoon (on/off)","g":29,"k":0,"t":"b","x":302,"r":0,"m":0},{"n":"Show_Trade_Panel","l":"panel-ka trade-yada furan (hoose-bidix) SL/TP pip","g":29,"k":0,"t":"b","x":303,"r":0,"m":0},{"n":"Show_Control_Panel","l":"CONTROL PANEL-ka hore (badhamo xeelad/LOT/SL/TP) - shid/dami adigoo doorta, uma baahnid F7 recompile","g":29,"k":0,"t":"b","x":304,"r":0,"m":0},{"n":"CPanel_X","l":"CONTROL PANEL - X (marka Use_New_Left_Panel=true) - waad bedeli kartaa","g":29,"k":0,"t":"i","x":305,"r":0,"m":0},{"n":"CPanel_Y","l":"kor, la simay dashboard-ka bidix: CONTROL PANEL - Y (marka Use_New_Left_Panel=true) - waad bedeli kartaa","g":29,"k":0,"t":"i","x":306,"r":0,"m":0},{"n":"CPanel_Gap_Below_Strat","l":"CILAD-XALI - marka Use_New_Left_Panel=FALSE (STRATEGY PERFORMANCE panel-ku muuqdo), CONTROL PANEL-ku meel bann","g":29,"k":0,"t":"i","x":307,"r":0,"m":0},{"n":"Panel_Always_Show","l":"tus xitaa marka trade furan aan jirin (si aad u hubiso)","g":29,"k":0,"t":"b","x":308,"r":0,"m":0},{"n":"Panel_X","l":"dashboard-ka bidix kuma dul fadhiisto: fogaanta bidixda (panel-kii hore 280 buu ballaadhan yahay)","g":29,"k":0,"t":"i","x":309,"r":0,"m":0},{"n":"Panel_Y_Margin","l":"fogaanta hoose","g":29,"k":0,"t":"i","x":310,"r":0,"m":0},{"n":"Panel_Font_Scale","l":"cabbirka qoraalka (1.0 = caadi, 0.9 = yar)","g":29,"k":0,"t":"d","x":311,"r":0,"m":0},{"n":"Use_New_Left_Panel","l":"panel-ka bidixda ee CUSUB (qiimayaashu MIDIG bay ku toosan yihiin)","g":29,"k":0,"t":"b","x":312,"r":0,"m":0},{"n":"LPanel_X","l":"panel-ka bidixda - X","g":29,"k":0,"t":"i","x":313,"r":0,"m":0},{"n":"LPanel_Y","l":"panel-ka bidixda - Y","g":29,"k":0,"t":"i","x":314,"r":0,"m":0},{"n":"Show_Trend_Lines","l":"Tus xariiqaha trendka (on/off)","g":29,"k":0,"t":"b","x":315,"r":0,"m":0},{"n":"SR_Draw_Zones","l":"SR Draw Zones","g":29,"k":0,"t":"b","x":316,"r":0,"m":0},{"n":"SD_Profile","l":"heerka zone-ka · TAYO = inputs-ka hoose (adag) · DHEXE = isku dheelli · BADAN = trade badan","g":30,"k":1,"t":"e","o":[[0,"TAYO"],[1,"DHEXE"],[2,"BADAN"]]},{"n":"Enable_SD_Engine","l":"isticmaal SUPPLY&DEMAND halkii SR-kii hore (on/off)","g":30,"k":0,"t":"b","x":317,"r":0,"m":0},{"n":"SD_Zone_TF","l":"timeframe-ka zone-yada","g":30,"k":0,"t":"e","x":318,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"SD_Lookback","l":"immisa shumac dib loo eegayo","g":30,"k":0,"t":"i","x":319,"r":0,"m":0},{"n":"SD_Base_Max_Bars","l":"shumac ugu badan oo base ah (1-5)","g":30,"k":0,"t":"i","x":320,"r":0,"m":0},{"n":"SD_Base_Max_Range_ATR","l":"base shumac kasta range-kiisu ka yar (x ATR)","g":30,"k":0,"t":"d","x":321,"r":0,"m":0},{"n":"SD_Base_Max_Body_Pct","l":"base jidhku ka yar","g":30,"k":0,"t":"d","x":322,"r":0,"m":0},{"n":"Inp_SD_Impulse_Min_ATR","l":"SD Impulse Min ATR","g":30,"k":0,"t":"d","x":323,"r":0,"m":8},{"n":"Inp_SD_Impulse_Min_Pips","l":"zone xoog leh","g":30,"k":0,"t":"d","x":324,"r":0,"m":8},{"n":"Inp_SD_Touch_React_Min_Pips","l":"SD Touch React Min Pips","g":30,"k":0,"t":"d","x":325,"r":0,"m":8},{"n":"SD_Swing_Extreme","l":"swing-na sidoo kale","g":30,"k":0,"t":"b","x":326,"r":0,"m":0},{"n":"SD_Impulse_Max_Bars","l":"SD: shumac ugu badan oo baxsasho ah","g":30,"k":0,"t":"i","x":327,"r":0,"m":0},{"n":"SD_Impulse_Body_Pct","l":"shumaca 1aad ee baxsashada jidhkiisu ka badan","g":30,"k":0,"t":"d","x":328,"r":0,"m":0},{"n":"Inp_SD_Max_Touches","l":"zone cusub/nadiif ah","g":30,"k":0,"t":"i","x":329,"r":0,"m":8},{"n":"SD_Min_Zone_Age","l":"SD: shumac ugu yar oo zone-ku jiray","g":30,"k":0,"t":"i","x":330,"r":0,"m":0},{"n":"SD_Max_Zone_Age","l":"zone WEYN wuu duugoobaa - 31 maalmood kuma filna","g":30,"k":0,"t":"i","x":331,"r":0,"m":0},{"n":"SD_Max_Dist_ATR","l":"fogaanta 8 -> 15 ATR","g":30,"k":0,"t":"d","x":332,"r":0,"m":0},{"n":"SD_SL_Buffer_ATR","l":"SD: SL-ku intee buu DISTAL-ka ka baxsanaadaa (x ATR)","g":30,"k":0,"t":"d","x":333,"r":0,"m":0},{"n":"SD_SL_Max_ATR","l":"SD: SL ugu ballaadhan (x ATR) - ka tag haddii ka weyn","g":30,"k":0,"t":"d","x":334,"r":0,"m":0},{"n":"SD_RR","l":"TP = SL x tan","g":30,"k":0,"t":"d","x":335,"r":0,"m":0},{"n":"SD_Require_Confirm","l":"SD: u baahan shumac diidmo (rejection) (on/off)","g":30,"k":0,"t":"b","x":336,"r":0,"m":0},{"n":"SD_Confirm_Wick_Pct","l":"dabada shumaca / range","g":30,"k":0,"t":"d","x":337,"r":0,"m":0},{"n":"SD_Kill_On_Break","l":"SD: close ka baxsan distal -> zone WAA LA TUURAA (on/off)","g":30,"k":0,"t":"b","x":338,"r":0,"m":0},{"n":"SD_Break_Buf_ATR","l":"SD: buffer-ka jabinta (x ATR)","g":30,"k":0,"t":"d","x":339,"r":0,"m":0},{"n":"Inp_SD_Require_BOS","l":"baxsashadu waa inay JABISAA qaab-dhismeedkii hore (BOS)","g":30,"k":0,"t":"b","x":340,"r":0,"m":8},{"n":"SD_BOS_Lookback","l":"SD BOS Lookback","g":30,"k":0,"t":"i","x":341,"r":0,"m":0},{"n":"SD_Swing_Zones","l":"zone ka dhis SWING HIGH/LOW (base looma baahna)","g":30,"k":0,"t":"b","x":342,"r":0,"m":0},{"n":"SD_Swing_Len","l":"shumac dhinac kasta oo swing-ka qeexaya","g":30,"k":0,"t":"i","x":343,"r":0,"m":0},{"n":"SD_Swing_Imp_Bars","l":"immisa shumac kadib ayaa baxsashada la qiyaasayo","g":30,"k":0,"t":"i","x":344,"r":0,"m":0},{"n":"Inp_SD_Extreme_Only","l":"zone waa inuu CIDHIFKA ku yaallaa - dhexda lama qaadanayo","g":30,"k":0,"t":"b","x":345,"r":0,"m":8},{"n":"Inp_SD_Extreme_Window","l":"cidhif dhab ah - dhexda ma jirto","g":30,"k":0,"t":"i","x":346,"r":0,"m":8},{"n":"Inp_SD_Extreme_Tol_ATR","l":"SD Extreme Tol ATR","g":30,"k":0,"t":"d","x":347,"r":0,"m":8},{"n":"SD_Big_TF","l":"TF-ka lagu qiyaaso WEYNIDA baxsashada","g":30,"k":0,"t":"e","x":348,"r":0,"m":0,"o":[[0,"CURRENT"],[1,"M1"],[2,"M2"],[3,"M3"],[4,"M4"],[5,"M5"],[6,"M6"],[10,"M10"],[12,"M12"],[15,"M15"],[20,"M20"],[30,"M30"],[16385,"H1"],[16386,"H2"],[16387,"H3"],[16388,"H4"],[16390,"H6"],[16392,"H8"],[16396,"H12"],[16408,"D1"],[32769,"W1"],[49153,"MN1"]]},{"n":"SD_Big_Weight","l":"dhibcaha weynida (0 = damman)","g":30,"k":0,"t":"d","x":349,"r":0,"m":0},{"n":"SD_Recency_Weight","l":"dhibcaha CUSUBNIMADA (hore 5.0 - zone duug oo weyn wuu guuli waayay)","g":30,"k":0,"t":"d","x":350,"r":0,"m":0},{"n":"SD_Debug","l":"SD: qor sabab kasta oo diidmo ah","g":30,"k":0,"t":"b","x":351,"r":0,"m":0},{"n":"Panel_Show_Selected_Only","l":"Panel: kaliya tus xeeladda la doortay (on/off)","g":30,"k":0,"t":"b","x":352,"r":0,"m":0},{"n":"Show_CurrencyMeter","l":"Tus cabbirka xoogga lacagaha (on/off)","g":30,"k":0,"t":"b","x":353,"r":0,"m":0},{"n":"Show_EMA_Lines","l":"Show EMA Lines","g":30,"k":0,"t":"b","x":354,"r":0,"m":0},{"n":"Viz_EMA_Fast","l":"Muuqaal: EMA degdeg","g":30,"k":0,"t":"i","x":355,"r":0,"m":0},{"n":"Viz_EMA_Slow","l":"Muuqaal: EMA gaabis","g":30,"k":0,"t":"i","x":356,"r":0,"m":0},{"n":"Viz_EMA_Fast_Clr","l":"Muuqaal: midabka EMA degdeg","g":30,"k":0,"t":"c","x":357,"r":0,"m":0},{"n":"Viz_EMA_Slow_Clr","l":"Muuqaal: midabka EMA gaabis","g":30,"k":0,"t":"c","x":358,"r":0,"m":0},{"n":"Viz_EMA_Width","l":"Muuqaal: dhumucda xariiqda EMA","g":30,"k":0,"t":"i","x":359,"r":0,"m":0},{"n":"Viz_EMA_Bars","l":"Muuqaal: immisa shumac EMA la sawirayo","g":30,"k":0,"t":"i","x":360,"r":0,"m":0},{"n":"Enable_Chart_Screenshot","l":"Qaado sawirka chart-ka (on/off)","g":30,"k":0,"t":"b","x":361,"r":0,"m":0},{"n":"Screenshot_Width","l":"Ballaca sawirka","g":30,"k":0,"t":"i","x":362,"r":0,"m":0},{"n":"Screenshot_Height","l":"Dhererka sawirka","g":30,"k":0,"t":"i","x":363,"r":0,"m":0},{"n":"Screenshot_To_App","l":"sawirka trade-ka (furan + xidhan) app-ka u dir (Trade tab)","g":30,"k":0,"t":"b","x":364,"r":0,"m":0},{"n":"Screenshot_Show_Panels","l":"false = panel-yada MOHA PRO waa la qariyaa inta sawirka la qaadayo (chart nadiif ah)","g":30,"k":0,"t":"b","x":365,"r":0,"m":0},{"n":"EnableJournal","l":"Diiwaanka trade-yada CSV (on/off)","g":31,"k":0,"t":"b","x":366,"r":1,"m":0},{"n":"Journal_Filename","l":"Magaca faylka diiwaanka","g":31,"k":0,"t":"s","x":367,"r":1,"m":0},{"n":"Enable_Debug_Log","l":"Diiwaanka debug-ga (on/off)","g":31,"k":0,"t":"b","x":368,"r":0,"m":0},{"n":"Inp_PR_On","l":"💱 LAMAANAHA - app-ka ka dooro · bot-ka 👑 (dahabka) ayaa chart-ka u furaya","g":32,"k":1,"t":"b"},{"n":"Inp_PR_Tpl","l":"template-ka (Charts -> Template -> Save) ee bot-ku ku jiro","g":32,"k":2,"t":"s"},{"n":"Inp_PR_Max","l":"lamaanaha ugu badan · 👑 ku jiro (1-8)","g":32,"k":1,"t":"i"},{"n":"Inp_CB_On","l":"📏 CABBIR LAMAANE - sitinka $ ee TICK / BASKET si toos ah lamaane kasta (ATR) · false = $ dahab","g":33,"k":1,"t":"b"},{"n":"Inp_CB_TF","l":"ATR timeframe-ka (M5 · M15 · H1)","g":33,"k":1,"t":"e","o":[[0,"M5"],[1,"M15"],[2,"H1"]]},{"n":"Inp_CB_Bars","l":"ATR muddo (bar) · M15 384 = 4 maalmood (20-2000)","g":33,"k":1,"t":"i"},{"n":"Inp_CB_Spread_Pct","l":"spread > X% SL-ka (VSL) -> TICK ma galo (0 = off · lamaanaha aan dahabka ahayn)","g":33,"k":1,"t":"i"},{"n":"Inp_CB_Ref","l":"tixraaca dahabka (AUTO = XAUUSD terminal-ka)","g":33,"k":2,"t":"s"},{"n":"Inp_CB_Syms","l":"lamaanayaal kale oo TICK/BASKET (tijaabo · chart gacan) \"XAG,BTC\" · madhan = 💱 LAMAANAHA app-ka oo keliya","g":33,"k":2,"t":"s"},{"n":"Inp_GR_On","l":"🪜 GRID STOP - amarro stop (virtual) jihada trend-ka oo keliya · SL guud · quful · TP guud","g":34,"k":1,"t":"b"},{"n":"Inp_GR_Only","l":"GRID OO KELIYA: shidan -> xeeladaha kale (TICK · BASKET · ASIA · SR/SMC) lammaanahan ma furaan","g":34,"k":1,"t":"b"},{"n":"Inp_GR_Dir","l":"jihada: TREND (🧭 jihada suuqa) · BUY oo keliya · SELL oo keliya","g":34,"k":1,"t":"e","o":[[0,"TREND"],[1,"BUY"],[2,"SELL"]]},{"n":"Inp_GR_Str","l":"🧭 xoogga jihada ugu yar % (ka yar = RANGE -> grid ma jiro)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Step","l":"masaafada lakabyada ($ qiime dahab · 📏 x cabbir) · 0 = AUTO (ATR)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Step_ATR","l":"AUTO: masaafo = X × ATR M5(14) (0.05-1.0)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Spr_X","l":"masaafo >= X × spread (ka yar -> sug) (1-10)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Levels","l":"lakab ugu badan (2-30)","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Lot","l":"lot lakab kasta (isku mid · martingale ma jiro)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_TP_USD","l":"🎯 TP guud: faa'iidada basket-ka $X -> xidh dhammaan (0 = off)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Lock_USD","l":"🔒 QUFUL: faa'iido $X kadib ... (0 = off)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Lock_Pct","l":"🔒 QUFUL: ... X% faa'iidada ugu sarreysa ha lumin (10-90)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_SL_Pct","l":"🛑 SL guud: khasaaraha basket-ka % balance -> xidh dhammaan (0.1-5)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_Hard_Lv","l":"SL broker trade kasta = X lakab (internet go'a) (2-30)","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Cool","l":"SL guud kadib -> sug X daqiiqo (0 = off)","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Max_Day","l":"grid maalintii ugu badan (1-50)","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Day_Loss","l":"khasaaraha maalinlaha GRID % balance -> maanta jooji (0 = off)","g":34,"k":1,"t":"d"},{"n":"Inp_GR_News","l":"war xoog leh (news filter-ka) -> grid cusub / lakab cusub ma jiro","g":34,"k":1,"t":"b"},{"n":"Inp_GR_Hour_Start","l":"saacadaha grid cusub (waqtiga SERVER-ka): bilow","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Hour_End","l":"saacadaha grid cusub: dhammaad","g":34,"k":1,"t":"i"},{"n":"Inp_GR_Max_Min","l":"grid ugu dheer (daqiiqo) -> xidh (0 = off)","g":34,"k":1,"t":"i"}]}'''
INP_SCHEMA = json.loads(INP_SCHEMA_JSON)
INP_GEN = {it["n"]: it for it in INP_SCHEMA["items"] if it.get("k") == 0}


def valid_in(name, raw):
    """v12.18: qiimaha input-ka -> qaab EA-ga (bool 1/0 · int · double · enum · color · string = h+hex). None = khaldan."""
    it = INP_GEN.get(str(name))
    if not it:
        return None
    t = it.get("t"); raw = str(raw).strip()
    if t == "b":
        return {"1": "1", "0": "0", "true": "1", "false": "0"}.get(raw.lower())
    if t == "s":
        if not re.fullmatch(r"h(?:[0-9a-fA-F]{2}){0,400}", raw):
            return None
        try:
            txt = bytes.fromhex(raw[1:]).decode("utf-8")
        except ValueError:
            return None
        if len(txt) > 200 or any(ord(ch) < 32 for ch in txt):
            return None
        return "h" + txt.encode("utf-8").hex()
    try:
        f = float(raw)
    except ValueError:
        return None
    if f != f or abs(f) > 1e12:
        return None
    if t == "d":
        out = ("%.10f" % f).rstrip("0").rstrip(".")
        return "0" if out in ("", "-0") else out
    if f != int(f):
        return None
    v = int(f)
    if t == "i":
        return str(v) if abs(v) <= 2000000000 else None
    if t == "c":
        return str(v) if 0 <= v <= 0xFFFFFF else None
    if t == "e":
        return str(v) if v in [o[0] for o in it.get("o") or []] else None
    return None


def valid_pairs(raw):
    """v12.22: liiska lamaanaha "SYM:on:tf:st:rk;..." (h + hex UTF-8) -> nadiifsan, ama None."""
    raw = str(raw).strip()
    if not re.fullmatch(r"[hH](?:[0-9a-fA-F]{2}){0,700}", raw):
        return None
    try:
        txt = bytes.fromhex(raw[1:]).decode("utf-8")
    except ValueError:
        return None
    out, seen = [], set()
    parts = [p for p in txt.split(";") if p.strip()]
    if len(parts) > 8:
        return None
    for part in parts:
        f = part.split(":")
        if len(f) not in (5, 7):                          # v12.24: + fl (1 TICK · 2 BASKET) · cm (0 AUTO · -1 DAMI · >0 GACAN)
            return None
        sym = f[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9._#+\-]{1,24}", sym) or sym.upper() in seen:
            return None
        seen.add(sym.upper())
        try:
            on, tf, st, rk = int(f[1]), int(f[2]), int(f[3]), float(f[4])
        except ValueError:
            return None
        if on not in (0, 1) or tf not in (1, 5) or st not in (0, 1, 2) or not (0 <= rk <= 5) or rk != rk:
            return None
        fl, cm = 0, 0.0
        if len(f) == 7:
            try:
                fl, cm = int(f[5]), float(f[6])
            except ValueError:
                return None
            if fl not in range(8) or cm != cm or not (cm == 0 or cm == -1 or 0.00001 <= cm <= 100000):
                return None
        cms = "0" if cm == 0 else ("-1" if cm < 0 else (("%.8f" % cm).rstrip("0").rstrip(".") or "0"))
        out.append("%s:%d:%d:%d:%s:%d:%s" % (sym, on, tf, st, ("%.2f" % rk).rstrip("0").rstrip(".") or "0", fl, cms))
    return "h" + ";".join(out).encode("utf-8").hex()


def valid_command(cmd):
    """True + amarka nadiifsan, ama False + sabab."""
    if cmd in VALID_COMMANDS:
        return True, cmd
    if cmd.startswith("SET:PAIRS="):                      # v12.22 (EA v70.8): 💱 LAMAANAHA
        vv = valid_pairs(cmd[10:])
        return (True, "SET:PAIRS=" + vv) if vv is not None else (False, None)
    if cmd.startswith("SET:INDEL:"):                      # v12.18: input-ka MT5-ka ku celi
        return (True, cmd) if cmd[10:] in INP_GEN else (False, None)
    if cmd.startswith("SET:IN:"):                         # v12.18: input kasta (EA v70.6+)
        mm = re.match(r"^SET:IN:([A-Za-z_]\w{0,63})=(.*)$", cmd, re.S)
        if not mm:
            return False, None
        vv = valid_in(mm.group(1), mm.group(2))
        return (True, "SET:IN:%s=%s" % (mm.group(1), vv)) if vv is not None else (False, None)
    m = re.match(r"^SET:([A-Z][A-Z0-9]*)=([0-9]+(?:\.[0-9]+)?)$", cmd)
    if not m:
        return False, None
    key, raw = m.group(1), m.group(2)
    lim = SET_LIMITS.get(key)
    if not lim:
        return False, None
    lo, hi, kind = lim
    try:
        v = float(raw)
    except ValueError:
        return False, None
    if v < lo or v > hi:
        return False, None
    val = str(int(v)) if kind == "int" else ("%.2f" % v)
    return True, "SET:%s=%s" % (key, val)

# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account     TEXT    NOT NULL UNIQUE,
  name        TEXT    NOT NULL DEFAULT '',
  pw          TEXT    NOT NULL,
  role        TEXT    NOT NULL DEFAULT 'user',
  approved    INTEGER NOT NULL DEFAULT 0,
  can_control INTEGER NOT NULL DEFAULT 1,
  pw_self     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL,
  last_login  TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  account     TEXT PRIMARY KEY,
  bot         TEXT NOT NULL DEFAULT '',
  data        TEXT NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  account  TEXT NOT NULL,
  ts       REAL NOT NULL,
  balance  REAL NOT NULL,
  equity   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hist ON history(account, ts);
CREATE TABLE IF NOT EXISTS commands(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account    TEXT NOT NULL,
  cmd        TEXT NOT NULL,
  by_account TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  taken_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_cmd ON commands(account, taken_at);
CREATE TABLE IF NOT EXISTS closed_trades(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  account  TEXT NOT NULL,
  ticket   TEXT NOT NULL,
  data     TEXT NOT NULL,
  ts       REAL NOT NULL,
  UNIQUE(account, ticket)
);
CREATE INDEX IF NOT EXISTS ix_ct ON closed_trades(account, ts);
CREATE TABLE IF NOT EXISTS ctrades(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT NOT NULL,
  ticket  TEXT NOT NULL,
  sym     TEXT NOT NULL DEFAULT '',
  type    TEXT NOT NULL DEFAULT '',
  strat   TEXT NOT NULL DEFAULT '',
  lot     REAL NOT NULL DEFAULT 0,
  points  REAL NOT NULL DEFAULT 0,
  profit  REAL NOT NULL DEFAULT 0,
  ot      INTEGER NOT NULL DEFAULT 0,
  ct      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(account, ticket)
);
CREATE INDEX IF NOT EXISTS ix_ctr  ON ctrades(account, ct);
CREATE INDEX IF NOT EXISTS ix_ctrs ON ctrades(account, sym);
CREATE TABLE IF NOT EXISTS branding(
  account    TEXT PRIMARY KEY,
  img        TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis(
  account TEXT NOT NULL,
  sym     TEXT NOT NULL,
  data    TEXT NOT NULL,
  news    TEXT NOT NULL DEFAULT '[]',
  ts      REAL NOT NULL,
  UNIQUE(account, sym)
);
CREATE INDEX IF NOT EXISTS ix_an ON analysis(account, ts);
CREATE TABLE IF NOT EXISTS cmd_seen(
  cmd_id  INTEGER NOT NULL,
  account TEXT NOT NULL,
  chart   TEXT NOT NULL,
  ts      REAL NOT NULL,
  UNIQUE(cmd_id, chart)
);
CREATE INDEX IF NOT EXISTS ix_cs ON cmd_seen(account, chart);
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread     TEXT NOT NULL,
  sender     TEXT NOT NULL,
  from_admin INTEGER NOT NULL DEFAULT 0,
  body       TEXT NOT NULL DEFAULT '',
  img_id     INTEGER,
  created_at REAL NOT NULL,
  read_at    REAL
);
CREATE INDEX IF NOT EXISTS ix_msg ON messages(thread, id);
CREATE INDEX IF NOT EXISTS ix_msg_unread ON messages(from_admin, read_at);
CREATE TABLE IF NOT EXISTS bot_keys(
  account    TEXT PRIMARY KEY,
  bkey       TEXT NOT NULL UNIQUE,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  last_seen  REAL
);
CREATE TABLE IF NOT EXISTS licenses(
  account    TEXT PRIMARY KEY,
  until      REAL NOT NULL DEFAULT 0,
  perms      TEXT NOT NULL DEFAULT '{}',
  follow     INTEGER NOT NULL DEFAULT 1,
  ovr        TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS levels(
  account TEXT NOT NULL,
  sym     TEXT NOT NULL,
  data    TEXT NOT NULL,
  ts      REAL NOT NULL,
  UNIQUE(account, sym)
);
CREATE TABLE IF NOT EXISTS kv(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ea_config(
  account    TEXT PRIMARY KEY,
  data       TEXT NOT NULL,
  rev        INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_shots(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account    TEXT NOT NULL,
  pos        TEXT NOT NULL DEFAULT '',
  sym        TEXT NOT NULL DEFAULT '',
  tag        TEXT NOT NULL DEFAULT '',
  type       TEXT NOT NULL DEFAULT '',
  lot        REAL NOT NULL DEFAULT 0,
  entry      REAL NOT NULL DEFAULT 0,
  sl         REAL NOT NULL DEFAULT 0,
  tp         REAL NOT NULL DEFAULT 0,
  closep     REAL NOT NULL DEFAULT 0,
  profit     REAL NOT NULL DEFAULT 0,
  ot         REAL NOT NULL DEFAULT 0,
  ct         REAL NOT NULL DEFAULT 0,
  dg         INTEGER NOT NULL DEFAULT 5,
  mime       TEXT NOT NULL DEFAULT '',
  data       TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shots ON trade_shots(account, id);
CREATE TABLE IF NOT EXISTS chat_images(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread     TEXT NOT NULL,
  mime       TEXT NOT NULL,
  data       TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

# --------------------------------------------------------------------------
# v3: LABA DIALECT — SQLite (local/fallback) iyo Postgres (Neon, joogto ah)
# Koodka intiisa kale isku qaab ayuu u qoran yahay: con.execute("... ?", (a,b))
# --------------------------------------------------------------------------
def _db_error_types():
    t = [sqlite3.Error]
    if psycopg2 is not None:
        t.append(psycopg2.Error)
    if pg8000 is not None:
        t.append(pg8000.Error)
    return tuple(t)


DB_ERRORS = _db_error_types()


PG_CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "8"))
PG_CONNECT_TRIES   = int(os.environ.get("PG_CONNECT_TRIES", "3"))
PG_STATEMENT_MS    = int(os.environ.get("PG_STATEMENT_MS", "8000"))


def _pg_harden(raw):
    """Server-side: query weligeed ma istaagayso. Socket-ka timeout wuu leeyahay,
    tanina waxay ilaalinaysaa dhinaca server-ka."""
    try:
        c = raw.cursor()
        c.execute("SET statement_timeout = %d" % PG_STATEMENT_MS)
        c.execute("SET idle_in_transaction_session_timeout = %d" % (PG_STATEMENT_MS * 2))
        raw.commit()
    except Exception:                                        # noqa: BLE001
        pass
    return raw


def _pg_connect_once():
    if PG_DRIVER == "psycopg2":
        return _pg_harden(psycopg2.connect(DATABASE_URL,
                                           connect_timeout=PG_CONNECT_TIMEOUT))
    # pg8000 DSN ma aqbalo -> URL-ka waa la kala jarayaa.
    import ssl as _ssl
    from urllib.parse import urlparse, unquote
    u = urlparse(DATABASE_URL)
    host = u.hostname or "localhost"
    ctx = None
    if host not in ("localhost", "127.0.0.1"):
        ctx = _ssl.create_default_context()      # Neon: SSL waajib, SNI la socda
    return _pg_harden(pg8000.connect(
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        host=host,
        port=int(u.port or 5432),
        database=(u.path or "/postgres").lstrip("/") or "postgres",
        ssl_context=ctx,
        timeout=PG_CONNECT_TIMEOUT,      # socket timeout - AKHRIS KASTA wuu xadidan yahay
        tcp_keepalive=True,
    ))


def _pg_connect():
    """Codsi caadi ah: HAL isku day oo keliya.
    (gunicorn --timeout 60 -> isku dayo badan oo hurdaa worker-ka ayay disayaan.)
    Isku daygga badan wuxuu ku jira ensure_db() oo keliya."""
    return _pg_connect_once()


class _DictCursor:
    """pg8000 tuple ayuu soo celiya — dict u beddel si koodku isku mid u noqdo."""

    __slots__ = ("cur", "cols")

    def __init__(self, cur):
        self.cur = cur
        self.cols = [d[0] for d in (cur.description or [])]

    def fetchone(self):
        r = self.cur.fetchone()
        return None if r is None else dict(zip(self.cols, r))

    def fetchall(self):
        return [dict(zip(self.cols, r)) for r in self.cur.fetchall()]


def _pg_sql(sql):
    """?  ->  %s   (xariiqyada hal-xaraf ah kuma jiraan SQL-kan)."""
    return sql.replace("?", "%s")


def _pg_schema(sql):
    """SQLite DDL -> Postgres DDL."""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    # REAL ee Postgres waa float4 (7 tirooyin) - epoch-ku wuu jabayaa. Isticmaal float8.
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return sql


class _Conn:
    """Duub ku dhow sqlite3.Connection, laakiin Postgres-na wuu qabtaa.

    - execute(sql, params) -> cursor (fetchone/fetchall -> dict-like)
    - `with db() as con:`  -> commit haddii wax khaldan jirin, kadibna XIR.
      (sqlite3 asalkiisa ma xidho -> xidhitaan la'aan = 'database is locked'.)
    """

    __slots__ = ("raw", "pg", "pooled")

    def __init__(self, raw, pg, pooled=False):
        self.raw = raw
        self.pg = pg
        self.pooled = pooled

    def execute(self, sql, params=()):
        if self.pg:
            if PG_DRIVER == "psycopg2":
                cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(_pg_sql(sql), tuple(params))
                return cur
            cur = self.raw.cursor()
            cur.execute(_pg_sql(sql), tuple(params))
            return _DictCursor(cur)
        return self.raw.execute(sql, params)

    def executemany(self, sql, seq):
        if self.pg:
            cur = self.raw.cursor()
            cur.executemany(_pg_sql(sql), [tuple(x) for x in seq])
            return cur
        return self.raw.executemany(sql, seq)

    def script(self, sql):
        """CREATE TABLE ... ; ... — mid mid."""
        if not self.pg:
            self.raw.executescript(sql)
            return
        cur = self.raw.cursor()
        for stmt in _pg_schema(sql).split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)

    def insert_ignore(self, sql, params=(), conflict=""):
        """INSERT ... ON CONFLICT DO NOTHING (labada dialect)."""
        tail = " ON CONFLICT %s DO NOTHING" % conflict if conflict else " ON CONFLICT DO NOTHING"
        return self.execute(sql + tail, params)

    def insert_many_ignore(self, table, cols, rows, conflict="", chunk=100):
        """HAL statement oo saf badan leh — ma aha INSERT kasta safar u gooni.

        120 trade: hore 120 safar oo Ohio ah (~10 ilbiriqsi).
        Hadda 2 statement (~0.2 ilbiriqsi). Taasi waa 50 jeer ka degdeg badan.
        """
        if not rows:
            return 0
        ncol = len(cols)
        tail = (" ON CONFLICT %s DO NOTHING" % conflict) if conflict else " ON CONFLICT DO NOTHING"
        head = "INSERT INTO %s(%s) VALUES " % (table, ",".join(cols))
        done = 0
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            ph = ",".join(["(" + ",".join(["?"] * ncol) + ")"] * len(part))
            flat = []
            for r in part:
                flat.extend(r)
            self.execute(head + ph + tail, flat)
            done += len(part)
        return done

    def commit(self):
        self.raw.commit()

    def close(self):
        """Xidhiidhka la dhawrayo LAMA xidho - dib ayaa loo isticmaalayaa."""
        if self.pooled:
            return
        try:
            self.raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        except Exception:                      # noqa: BLE001
            _pg_pool_drop()                    # xidhiidhku waa xun -> tuur
            return False
        finally:
            if exc_type is not None and self.pooled:
                _pg_pool_drop()                # qalad kadib dib ha loo isticmaalin
                if exc_type is not DbUnavailable and issubclass(exc_type, DB_ERRORS):
                    _cb_lose(exc)              # qaladka query-ga dabka ha tiriyo
            self.close()
        return False


# --------------------------------------------------------------------------
# v3.2: XIDHIIDH LA DHAWRAYO (pool)
# Hore: codsi KASTA xidhiidh cusub oo Neon ah (TLS handshake ~200ms, Ohio).
# MT5-du ilbiriqsi kasta codsi bay dirtaa -> worker-ku wuu buuxsamayaa,
# browser-yaduna way sugayaan ilaa ay ka quustaan.
# Hadda: thread kastaa HAL xidhiidh ayuu haystaa oo dib u isticmaalayaa.
# --------------------------------------------------------------------------
_tl = threading.local()
PG_PING_AFTER   = 20        # ilbiriqsi: intaas kadib 'SELECT 1' hubi
PG_MAX_IDLE     = 150       # ilbiriqsi: intaas kadib xidhiidhka la cusboonaysiiyo
PG_TRIP_FAILS   = 3         # guuldarro isku xigta -> dabku wuu go'ayaa
PG_TRIP_SECONDS = int(os.environ.get("PG_TRIP_SECONDS", "10"))
# 10s: ku filan in thread-yada aan la xannibin, gaaban si app-ku dhakhso u soo kabsado.
# Hal thread oo keliya ayaa tijaabinaya (_probe_lock), sidaas darteed gaaban waa ammaan.

_cb_lock   = threading.Lock()
_probe_lock = threading.Lock()   # xidhiidh cusub: HAL thread oo keliya marka la shakisan yahay
_cb_fails = 0               # guuldarrooyin isku xigta
_cb_until = 0.0             # waqtiga dabku dib u shidmayo
_cb_trips = 0               # immisa jeer uu go'ay (tirakoob)


class DbUnavailable(Exception):
    """Database ma diyaar aha. Degdeg ayaa loo tuurayaa - LAMA sugayo."""


def _cb_ok():
    """Dabku ma shidan yahay?"""
    with _cb_lock:
        return time.time() >= _cb_until


def _cb_win():
    global _cb_fails, _cb_until
    with _cb_lock:
        if _cb_fails or _cb_until:
            _cb_fails = 0
            _cb_until = 0.0


def _cb_lose(err):
    global _cb_fails, _cb_until, _cb_trips
    with _cb_lock:
        _cb_fails += 1
        if _cb_fails >= PG_TRIP_FAILS and time.time() >= _cb_until:
            _cb_until = time.time() + PG_TRIP_SECONDS
            _cb_trips += 1
            log.error("DATABASE: dabku wuu go'ay (%d guuldarro). %ds ma isku dayeyno. %s",
                      _cb_fails, PG_TRIP_SECONDS, str(err)[:160])


def _pg_pool_drop():
    raw = getattr(_tl, "raw", None)
    _tl.raw = None
    _tl.last = 0.0
    if raw is not None:
        try:
            raw.close()
        except Exception:
            pass


def _pg_pooled():
    if not _cb_ok():
        # Dabku wuu go'an yahay -> ISLA MARKIIBA tuur. Thread ma xannibayno,
        # sidaas darteed browser-yadu weligood ma safaysanayaan.
        raise DbUnavailable("database circuit open")

    now  = time.time()
    raw  = getattr(_tl, "raw", None)
    idle = now - getattr(_tl, "last", 0.0)

    if raw is not None and idle > PG_MAX_IDLE:
        _pg_pool_drop()                       # duug - cusub ka fiican
        raw = None
    elif raw is not None and idle > PG_PING_AFTER:
        try:
            c = raw.cursor()
            c.execute("SELECT 1")
            c.fetchone()
        except Exception:                      # noqa: BLE001
            _pg_pool_drop()
            raw = None

    if raw is None:
        # Marka guuldarro dhow jirto, HAL thread oo keliya ayaa isku dayaya.
        # Haddii kale thread kasta PG_CONNECT_TIMEOUT wuu sugayaa -> worker wuu buuxsamayaa.
        suspect = _cb_fails > 0
        if suspect and not _probe_lock.acquire(blocking=False):
            raise DbUnavailable("database probe in progress")
        try:
            raw = _pg_connect()
            _tl.raw = raw
        except Exception as e:                 # noqa: BLE001
            _cb_lose(e)
            raise
        finally:
            if suspect:
                _probe_lock.release()
    _cb_win()
    _tl.last = now
    return raw


def db():
    if USE_PG:
        return _Conn(_pg_pooled(), True, pooled=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=8000")
    return _Conn(con, False)

def init_db():
    with db() as con:
        con.script(_SCHEMA)
        # Migration: DB hore oo aan pw_self lahayn.
        # Postgres: statement fashilma wuxuu burinayaa transaction-ka oo dhan ->
        # IF NOT EXISTS ayaa loo baahan yahay. SQLite taas ma taageerto -> try/except.
        if con.pg:
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pw_self"
                        " INTEGER NOT NULL DEFAULT 0")
        else:
            try:
                con.execute("ALTER TABLE users ADD COLUMN pw_self INTEGER NOT NULL DEFAULT 0")
                log.info("Migration: users.pw_self la daray")
            except DB_ERRORS:
                pass
        if ADMIN_ACCOUNT and ADMIN_PASSWORD:
            row = con.execute("SELECT id FROM users WHERE account=?", (ADMIN_ACCOUNT,)).fetchone()
            pwh = generate_password_hash(ADMIN_PASSWORD)
            if row is None:
                con.execute(
                    "INSERT INTO users(account,name,pw,role,approved,can_control,created_at)"
                    " VALUES(?,?,?,'admin',1,1,?)",
                    (ADMIN_ACCOUNT, "Admin", pwh, _now_iso()))
                log.info("Admin la abuuray: %s", ADMIN_ACCOUNT)
            else:
                # Password-ka had iyo jeer waa laga soo celiyaa ADMIN_PASSWORD.
                # Sidaas darteed beddelka env-ka + redeploy = xal joogto ah.
                con.execute("UPDATE users SET pw=?, role='admin', approved=1, can_control=1"
                            " WHERE account=?", (pwh, ADMIN_ACCOUNT))
                log.info("Admin la cusboonaysiiyay: %s", ADMIN_ACCOUNT)
        else:
            log.warning("ADMIN_ACCOUNT / ADMIN_PASSWORD lama dejin -> admin lama abuurin.")

        # EXTRA_USERS -> boot kasta dib loo dhisayo. Disk la'aan ayay u shaqeysaa.
        for chunk in EXTRA_USERS.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [x.strip() for x in chunk.split(":")]
            if len(parts) < 3:
                log.warning("EXTRA_USERS qayb khaldan (u baahan acc:magac:pw): %r", chunk[:20])
                continue
            uacc = re.sub(r"\D", "", parts[0])
            uname, upw = parts[1], ":".join(parts[2:])
            if len(uacc) < 4 or len(upw) < 8:
                log.warning("EXTRA_USERS la iska dhaafay (acc ama pw gaaban): %s", uacc or "?")
                continue
            if uacc == ADMIN_ACCOUNT:
                continue
            uh = generate_password_hash(upw)
            row = con.execute("SELECT pw_self FROM users WHERE account=?", (uacc,)).fetchone()
            if row is not None:
                if int(row["pw_self"] or 0) == 1:
                    # Qofku password uu isagu doortay wuu leeyahay -> HA TAABAN.
                    con.execute("UPDATE users SET name=?, approved=1 WHERE account=?",
                                (uname, uacc))
                    log.info("EXTRA_USERS: %s (password-kiisa gaarka ah waa la ilaaliyay)", uacc)
                    continue
                con.execute("UPDATE users SET pw=?, name=?, approved=1 WHERE account=?",
                            (uh, uname, uacc))
            else:
                con.execute(
                    "INSERT INTO users(account,name,pw,role,approved,can_control,pw_self,created_at)"
                    " VALUES(?,?,?,'user',1,1,0,?)", (uacc, uname, uh, _now_iso()))
            log.info("EXTRA_USERS: %s diyaar", uacc)

def _col(row, key, default=None):
    """sqlite3.Row iyo RealDictRow labadaba - si ammaan ah tiir u soo qaad."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


CTRADE_COLS = ("account", "ticket", "sym", "type", "strat",
               "lot", "points", "profit", "ot", "ct")


def _save_closed(con, acc, rows):
    """Trade xidhan kasta hal mar ayaa la kaydiyaa (ticket = fure).

    v3.3: DHAMMAAN safafka HAL statement — ma aha mid mid.
    EA-du ilaa 120 trade ayuu soo diraa /update kasta; mid mid oo Postgres
    fog loo diraa = ~10 ilbiriqsi codsi kasta -> worker wuu buuxsamayaa.
    """
    batch = []
    seen = set()
    for t in rows or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("st", "OPEN")).upper() == "OPEN":
            continue
        tk = str(t.get("tk") or t.get("ticket") or "")
        if not tk or tk in seen:
            continue
        ct = int(_num(t.get("ct") or t.get("ot")))
        if ct <= 0:
            continue
        seen.add(tk)
        batch.append((
            acc, tk,
            str(t.get("sym") or t.get("symbol") or "")[:20],
            str(t.get("type") or "")[:8].upper(),
            str(t.get("strat") or t.get("strategy") or "")[:16],
            _num(t.get("lot") or t.get("lots")),
            _num(t.get("points")), _num(t.get("profit")),
            int(_num(t.get("ot"))), ct))
    if not batch:
        return 0
    return con.insert_many_ignore("ctrades", CTRADE_COLS, batch,
                                  conflict="(account, ticket)")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# v3.1: KACITAAN AMMAAN AH
# Hore: init_db() halkan ayaa la wacayay. Haddii database-ku diido
# (Neon hurday, internet gaabis), app-ku GEBI AHAAN wuu dhintay ->
# "ERR_CONNECTION_ABORTED".  Hadda: isku day, haddii diido sii
# wad, oo codsiga xiga dib isku day.
# --------------------------------------------------------------------------
_DB_READY = False
_DB_LAST_ERR = ""
_DB_TRIES = 0


def ensure_db(retry=True):
    """init_db() hal mar oo guulaysta. Weligiis ma tuurayo.

    retry=True  -> kacitaanka (isku day dhowr jeer)
    retry=False -> codsi caadi ah (HAL isku day, sug la'aan)
    """
    global _DB_READY, _DB_LAST_ERR, _DB_TRIES
    if _DB_READY:
        return True
    if USE_PG and not _cb_ok():
        return False                  # dabku go'an - ha sugin
    tries = (2 if USE_PG else 1) if retry else 1
    for i in range(tries):
        _DB_TRIES += 1
        try:
            init_db()
            _DB_READY = True
            _DB_LAST_ERR = ""
            log.info("Database diyaar (%s) - isku day #%d",
                     PG_DRIVER or "sqlite3", _DB_TRIES)
            return True
        except Exception as e:                               # noqa: BLE001
            _DB_LAST_ERR = "%s: %s" % (type(e).__name__, str(e)[:240])
            log.error("init_db fashilmay (isku day #%d): %s", _DB_TRIES, _DB_LAST_ERR)
            if retry and i + 1 < tries:
                time.sleep(2)
    return False


@app.before_request
def _db_gate():
    # Codsiga caadiga ah: HAL isku day, sug la'aan, oo kaliya haddii dabku shidan yahay.
    if not _DB_READY:
        ensure_db(retry=False)


@app.errorhandler(DbUnavailable)
def _db_unavailable(e):
    """Database ma diyaar aha -> 503 degdeg ah. App-ku ma dhimanayo."""
    return jsonify(ok=False, error="Database ma diyaar aha - dib isku day."), 503


@app.errorhandler(500)
def _internal(e):
    return jsonify(ok=False, error="Qalad server-ka."), 500


ensure_db()   # isku day marka la kacayo - laakiin ma dilayo app-ka


# --------------------------------------------------------------------------
# v8: PWA — app-ka waa la "install" gareyn karaa (mobil + PC)
#  /manifest.webmanifest · /sw.js · /icons/* · /favicon.ico · /apple-touch-icon.png
#  Dhammaan waa PUBLIC (login ma u baahna) - browser-ku login ka hor ayuu akhriyaa.
# --------------------------------------------------------------------------
_PWA_ICONS_B64 = {
    "icon-192.png": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAADAFBMVEXz6Lbr26fp1J3j06Dkzpjiy5PcyZfcxIvYwYvVvIPRuYLP"
        "tHvBuJvKsHjIq2+/rX++pnG9oWm0oHGxmGWwi0iomXWohkmmgD6jgD+bmIiag1SPj4R2iI6hfT+jfTqgezmfeTacejybdjWOeU2X"
        "dDaUcDKObjWEbUKOZiSDZC9/XihxcGZzXzlSZFomelkmc1QlaExzWStqWDZpUShfVUBfTzJOUEWONT11PC1ZSS1YQyGFMjp+MDZ2"
        "MDNoLTNTRSxKRDZMPydKOiBDOSVDMiI2PT03Mio3Lx8uLSoyKh0tKB8qKCMqJyAtIyEpJR4oJB8mJSEmIx0mIRoZMjMhIyUhIiIh"
        "ISEiIBwfICAcHyMdHiEgHRsbHSIYHScVHSkcHB0YHCIWHCcTHC0THCwSHCwSHCsaGiIbGhkYGh4WGyMXGh8VGycUGiYVGiMUGSIT"
        "GywTGyoTGykTGikTGigTGiYTGSYTGSQTGSISGywSGysSGyoSGisSGioSGykSGikSGigSGycSGicSGiYSGSkSGSgSGScSGSYSGSUS"
        "GSQRGykRGioRGikRGigRGicRGSkRGSgRGScRGSYRGSUOGScWGBsUGB4TGCESGCUSGCMSGCISFyISFyARGCcRGCYRGCURFyYRGCQR"
        "GCMRFyMRFyIRFyAQGCYQGCUQFyYQFyUQGCQQFyQQGCMQFyMQFyIQFyEUFhkSFh0RFiERFh8RFSERFh4RFRwQFiUQFiMQFiIQFSUQ"
        "FiEQFh8QFR8QFRwUExQTExMTEhMSEhMSERMSERISEREQFB4RERMREREPGCUPFyUPFyMPFyIPFiMPFiIPFiEPFiAPFSIPFSEPFSAP"
        "FR8PFR4OFyYOFSIOFSAOFR8LFyYPFCEPFB8OFB8OEx8PFB4OFB4PEx4PFBwOFB0OFBwOExwPExsOEhoNEx4NExwMEhwLEiAKER0R"
        "EBEQEBEPEBMPEBIPEBEJEBwPDyEPDxIODxIPDxEPDxAODxEIDxwODhENDhENDhAHDhsGDBkCCxoBCBT4fVyFAAAimUlEQVR42u2d"
        "CVhT17bHAzKPClqUMgsoAm0jgoWqzEYCjQwKyBCgvAoyVStD4SqOIF61ggZQUn0otK/1ggKPVMqgcq16361gATs4XC1VxJhS5FJK"
        "iAm8tfdJSEBQiMHhPVc/yck5++zz/+211t4r6ZdzSL+/4kZ6DfAa4DXAa4DXAK8BXgP8HwXo7v0DW2/3KwnQjbWDodfuVw6gG498"
        "98OuSw8fYk90v1oAvUj+w76+xu6+xr6+qUQgTaX8i2cHHg0MnL3YSyD0viIA3YT83q4zj3guStMceLwzF3v7MUL3KwCAgh+N/sOz"
        "XTzfWaQ5b5K0V/D+PIf2dE9FHJGmInr6+robz/EarUkanoODnhqkN0/xGhvRXpQKLzUAEfz9vRfP8HiLpsnZ8h+Eh/846ABbj3jn"
        "piYVSFMQPSj4l8O4F/Lp9Ph4Op37I/jChffoTJdwQup+KQGImR/i5OwlXhAE/4rBZJC/YcOGeHr84Ko5pFm+vK6zKBVkO6WSZBs9"
        "fb0Xz/EGrElKDoOX6Ug9tnj6lygVrBt5f4dUkG0ckWQ2/IR8CH6HaSRrzgM6HZSvX5+RsX49bNDpD/i2ctMWSUypvS8RgFB+X9e5"
        "ARz84YOE/Iz0D8HSMQOdzk95k6SxnPforExTgSTLqfM3XsybaOrMpm/4+OOPM9LT/wNbenrGx8gLyYMrtEmzQnm/nZXhlEqSVfB3"
        "Q/A/siZNcxj8EcvflJ7+yScfYfvkk/T0TbBrA/3yoIMSyXpAllMqSSbRg2Z+VDeQrB9w6fSNIB+GH8n/AAwhgBMAYWM8/QEHQcow"
        "FUjPPnXiuuFPnrc2ac6qwXh68saNoD8jff1HH0VHI4DoaEBIz8jY/PHGjTCzDoZDmK3g9Z/rkkl1QZJJ8Dfyzuij4P+SvnG0fBHC"
        "ekDYBAgb6YW4uojh/SaqLnpfEMBw3QDBv0humq3gQXg8yM8E+ekw5sPyRQgQRhmZgBBPVBfWuLp45jgiPUv0iOoGF1ikvoe6AUXP"
        "WPIxwkcEAmRCchSd+wAWO5lUF6RnC/5uYdG8ajBBKH9U9Ix0giiOkoXVhfezVxekZwn+Xiiau3DR/HX4RnH0jCV/dBxtlKgunmlK"
        "JUk/80sWzfFPlz8CYSNKBagupgmrC+njiCTlzI/qBqJoTuVHoejJRNHz0ZP1IwIxQjJUF4escXVxTvpUmCxA9/DUeYkXOgs+Lg6m"
        "CKfOdBz8HzzVcCqkC1MhYXAFpEIQUV1IlQokqaKHqBvklFDdsHXbli2Zm3cDQPqH/zEh+xCaZuzenblly7at9B8hFeSs+3jnpEwF"
        "khTRg4PfRUnOuodLj98G+neDTVz/MMFmINgWH87h2sJQSJsKJGmCv58HZSUUzfFRSP5mkJ8xCflCBCDATtgWFTWY8CZJezlvQJpU"
        "mDjAH+K6gaGPgv9oOJK/RSh/UvqBQIiwBSHQd6FCW1+iuvhD5gCSwb9oGgT/Axz8aPgz0ic5/KPjiEgFByWiupjkZ07SpIKfqBvk"
        "UNEcLxk9UugnnJC+G3sBUgFVF3IqLjzeua5JpQJpEnXDw7NdA1A3vAl1Q1RW1s6dmXv++tdNf0lHn1zQZxahffJEG9EMXv6y6a9/"
        "3ZO5c2dWVlQCKrShuvhzUtUFaeILb+N53llrOW3PwctRKSB/J5b/F7EUoT0JQbLVCAT4yBxPDz8x6KktN/cM7zxMqRMttEkTk4+L"
        "Zt4iJRT84clo+PfsEcvHwjYJbXyEMVvhndHXBrlcSCv6A0gFJaguJl5okyZRN2jLWV/mR8ULo0dCKRrGTZmZMCnCn02bxiHAUjNR"
        "s5Gt0tOjH6ywnjXL2pObFR/F/xG8PInqgjShqROK5qBZcrNWDaZEZWcBwN49ezahz11CywBdkIjJYGhZztw0fETSMgAS8hW32ibZ"
        "KvqBtTwJDCaHrJSolMFVcKkgKLQnNKWSJjZ19lnLQ9H8Y1RKNpK/d0/mKP1IfsK68PCEBIwwFkEGIT8hISp8XQJGELYC/ST5aWDy"
        "pDe58dkpUd9DdSFv3QvVxQSmVNKThn84+B1UlGy5nKiEbNC/ZZR8Qn9yQmJkZANzTSSIG5MAe2l7wrrINcz2SNRKRBB9zVNefpo8"
        "smlyDkCQnRDF4S9SUnGQSAUpALqJ6EFftq3QkbNOGIyPF8ofET1CAFC2Jmz1IhfGmrB1CdvHAdiyLWFd2BqGy6LVYWvErT4YtJZT"
        "wPrlFeRmcelZWdnZ8fGDqTgVcHXxxEwgja8fhh9/2aYvP2vFYGGUpPyMMZQF+urIyVvHrAKCbY8TiFqtirGWl9PxDRS3ir42SwQw"
        "TV5t/d7NWxBCVOHgilnyRHXRj7596Z4kAF66cN2goOYw+FNUMkre0cEvkgYBBMpskA6XoNVh65IfJ8Bhti5sdZALGmibmFWREERC"
        "AJ1hD8irfbB/8969cKns5KgfB13UFGxQdYGXtckB9IL+7rNQNKspWHO4wuBHoz9GbKChjVzjS9OWV1CQ06F5B4K20QDIAckJkcHe"
        "NFCrIK9N810TiVwAjT548CbsIkxe51r0pk179mAnQCpwbBXUoLo42w0EvZMB6EbjP9DvDU4Mh7pBGD07duzEk6SkZW6BMiAFhtbP"
        "Xh5LsPH3DolMTYGVergpWiB27kxJjQzx9bchWtn7gaPQer4lM/qBg5wSoR/KOS59y84dO/bsJeIIqgsIYO/+AeSD7kkAQOuHXTHW"
        "CjorBouFU+eePZk78Df+I23DhuSUlMTIEG9/fTlFJE3Nwwe0paYkJ4sbb9jwcXJKKlD6eKghAEU5fYSZmJKCG0ESKClh/dr0TPR/"
        "EzLBCXuhPIIptXhwuQ5kVhekQe8kAMABjf2L5BwEneEJSD8R/GOtTuk4NkCau4oCACgqyYO24FFBRGRw5BqglFdCrRRU3AETN4Ik"
        "+DR6lhxwyctpB1yLJjrdlLkHpUL2roTwm4MOcov6G8EFkwQYWKQ9uC4qNXVXdta+vXszMzdvzhjDNsMUmrwOpM2TV1JQUVdQVFR0"
        "pPmGwCyJIlxoIB8ahfnSnOGwgrqKgpL8PH/vNeuSYSqFXqMPHrTVUVHRtqZfixb2uhnCbu8+AEhNjVo3qL1oYHIA4K6+s7xFaocT"
        "E0H/9n17t4wjHy4F2hIjA729pssryetaKYILdKiQx4loJto8DLktGTWi6YADFK10oel0L9QIKFGb6AzutVUBB7mfRov73YwItgNB"
        "YuJhtUW8s33jJME4AN39COAK0p+9Dw1/xjhGzI6+NHs0tnOdQJsSyuM1WFym2AGw0KEMhqO6TnORn+xpvsR8i5tEgxfWR68f6dvM"
        "vfuyEcEVBNDfPQmAXiFAMwLYtnfL+Po3ZyIHoBSWV1bQtLOzU1NUVlSjoDwWxocoyiBNKGoKyoqq0EhdQVmeSOPt20R9r48eIzr3"
        "bkMAzUKA3gkDwLqHQ6glNaWwMHf/jpEzp6TtgDk0MWz1SncQrqDvZOc0T0FZJC47ayc6MxPaZKciyLnyysrgJTsnfQBRc1+5OiwR"
        "ZtId43aeuWN/bmFhSmoLDqHusdeyJwFoI4AsAHjCJXZmp6xD0ztIUrF3cnJy0wECZRc/yONUTABNUJswXz8XOKCg4waN7FUA18Yf"
        "2qRk73zS8OzPzUIA2tIDFBZm7X8CABpclJ1UHUUVBV0PVzc3D3tlZRXFWWg9xsMLhpwEa7DfLAUVZWV7Dzd3Vw9dBRVFItcR5KaP"
        "Px4PYH9WYeEzA4w/RCg68OBi1QspHhSKh5e+goqKIrEeI3U4gMBJQYsUVVQU9L1QG8pCRcTi54tX4x2Z1w5+PF4MTTGAKLr1FVUU"
        "p4M0L6oXxV1TWUVZE+cxrIE7iUoj0IeiDbvV3XETisd0OEFfCLnjU48VYxNMLQCstJABuxLDAiGFwQHzaBQfX18fKs0GhlpxLlEr"
        "ZKPVFDEGzUV7bWhU1IZCmwcugDQODEvclbXz4HvLrk0NwE85hUfzcvdDHTSG7f7s6/25eak4hRVVVdRcqd6rAwNX+3pTdeGtijPO"
        "4115ebtQEeTnqqqiqqxDpfoGBgeu9qa6qqmoKuI0Ts3LPbgEAMa6xF64wNHCnJ9kBbB1i6RlfRCwfXtqUmSwD1VHWQ0Cgro6JCws"
        "JBgKBhUQq0vzCQyLS0tNTUNN/EVQwahNIBWCTg14fIIjk1K3I4CNkl1vnSKAT3d8KraMbwLezj2Qg1LYUUVVFaVkSNi6dZFhwTDh"
        "K8IONL6R65KS1q1FGaysqoqCPjgsEpqEoLRXVVVxRGmclIc9INH1jk+nCGBkBCGAo0QKK8NoenlD1iYmJuIlV1tFTUVzuQ8QgIXA"
        "jul4x8rhJt5e4DVlnMY5/7i2dNm1Pc8jhEbYhwCwD8oIWIU1VdWU5+GkTc1JxYXFQmU1NWXI49UhYKvBJeg9DvnhJvOU1VQ1YTWO"
        "XBsWvmRZ/KoNzxngs2/+HfDO9f9OQikMUtTdaVAYpO46cCAVl526sE/Vmebtuxqy2sdVDRB18cKVeuAAVDdhq2nu6qqIyXudx5Il"
        "77333uL3r2U+T4DPPggIeOftgICAYJh0VNSV9WnwKSYR5py8A2heWumipqamoutFoVJ9qBQviDE1VWJaOgBNdqGFmaavrK4CUJEY"
        "YPGUAEwfF2D3fwW89dbbYJ5Bfo6qoM5eqC43Ny8HR8hcZXV1FRtYs2B1W6iqpk7Ee2IOaoEZ/ezReY5+wQCw5CkemC4tQMuTAED+"
        "O+94rvSbq6KuquPlE7iWWQjqcnMPoAiBtFVVV53u6g7mBptqmrj4TD0ADbIOhi97PwTSWFVdZa6/79MBWqQGaM05VAyjCp8oR9nu"
        "rxHAOwDg566lpq46D/JzTcDWowgg70ASWtwWqqqrq+pD7ek2F23hDE6CAMrNTfk8fAklEtIYuLTc/TyWLiUAtjx2mX3QWfGhnNap"
        "BPC3UdVU14QUjg14m/7lVgwAQRToQ9OFA+oLnZzs4EVVl+oDGZyDYmzfD9dSlrx/MpDmDieq2oS6AcCSKQK4fuhQ8dEjn+XuH217"
        "YA5F+t/xpIJOVX2aT0g+AHxzZN++3CNHi9PABTRn0Kema+eEQRxRjqRBZ7nbDi5btmwJ/IPVGE7VpQHAUgyw87HL5H4GnR06dF1a"
        "AJ2fngqw3FldU1Pd3t93VWTA2wEf0j/DFy3E1cNcVU1NtXlWapqaqnP9UdFQiPraeXDp4sUQ9kvf97dH57p4vItc8CSAn3SkBRjX"
        "A3sB4B0MACrVdb18VsP222+/9c7Xe/FVDyWiAnqGuqb6dPzHnQZ156Fi1NXOg8sWv/fekqXLfH28dCGG5hIA7z3JA1IC2Ou0PhXA"
        "ZTrEiY3/ytVIvxigOC0O5TGMMMQRauAbEpdGdCUEWLI0EJZANYBzfBfH0BMAWnXspQA4BwCjPLBNZFnbRADz1LU00fiuEQL859Ys"
        "sMKcnKQwqFL1EQH2EDggJ6cQHds2DABpPF1TS33esncJgOSs4QuM9oA975xUAON6YL8QwEFXXUsdpfAapJ/wwP79n6EgQl8WuWpp"
        "ov9wBh+Cjj6Dg4QHIIZCQnxo+nC67rvIBQCQ9SQPyBYgN/ebAOyARZpaWpros22kJIAwj6GKBgdpqc9FdTWRwUIARLA0DH2WRudb"
        "4RgCgNznDuAJI6gJARK8NgpHkBCAcEHS2mA/qq66+ozlgTADiRxAAADB0rWwWHjpgoP0cQxNBYBu2+HDx4AAVwiSdiTvOgZwmKE1"
        "XR1SOCRuGGAf0QAuXABzacRyfX2XiOC1SQXQzRF8KOuHpRhgydqkkJX+NurTtWa8iwH+WXhk9HXyoJtjhw+36coYIO/Iget0nMKa"
        "cHk0ReZvRZOoGABfuSApLiSWERsbEpdUIO5GDJAWBmkMQ6A5DxEggLznBID8mrQm2NNFF64+lwZrVMEoAOGlk5LQJ7IkSf1CgMUA"
        "UACrHW0ujAFK4yXvfwdZIkuAv48LkIcBwoKW22pNn66FPtfGMQHgLUkAHETHDhWkJSWlFRw6NhxAwwCLl6z9Wxz6PI36sFrmtiwQ"
        "AeSNC/B32QIcS4sMXrlcX2uGFkrhyKTKrVi/BEAeIig+hq0Y6c8bDRD+N1RweOlCJ294BEfGpR2TNYDjOADYAZGBfi4zZszQQikc"
        "mXZ882gATHAUGED9UQn9kgBpkSiNtWZMn+HkFxiWhGa8cQAcZQmAej0M3keXnoFTOCnnOP0xgNw8QEAQ6G+euAsRwHvh/4A4RGmM"
        "hwHi8PCYl5oSABxBVBRBOIXTDo0FgBDyCPWSHUgCpOE0hm70qSuhm2PPB+DIERxBQc4wdDOcUQofKv7PzfAZ/y34jH9kX+5TTAKg"
        "8BBKY6KfoEBY7IqPHJlagDxkENdFSWv9QudBBOlTacFxBUUQ5t//O+CtD//9zfa8p1n2D1ALYYD/KSwqiAumUfUhhuaF+q1NKoJ8"
        "wW1kBnCztKS87MSXX3xOGA7no8fLSooKIiK8dOG6C0N9I+IKSo6fKN79TcBb9C/3HXm6fR4evmqxR/iqwhNlJQVxEb6hC2EkdL0i"
        "IgqKSsqOE9cQXvCLL0+UlZeU3pQK4PzjAIRBpyVJYacWaenO0KUEBccllUCTz3OvA8DlzydiP1yLX7z82v8QHcUFB1GgI61Fp8KI"
        "jiRNAuC8bAEK4mL1Z+jOmBcTGJFUgFtMAqDwh3AASMHyCpIiAmPmQVf6seDJ5weQ9OtycPsMd6hz8GWhxZcfBuz+rwkBHMEAhVge"
        "DEVIrDt0NWP5rzL3gLP+mACo1yJmsONcGLWwYf1AcH1i+gEgaun7ACAiCANvznUMZhaNeSkEoO8sa4CSAiaH5eLOiitgihvkfj5h"
        "++Efw/qYBXEsdxcWhykeiqkHQASJSezbSQWljx+fhGGBpQVJt9lJiUj/8wEQEpQUpaWVlJQ9ftXJAZwog67S0opKxtA/ZQDEdbHB"
        "RaXXjxSO6OqL5wNAEJzA13w2/UICUVdffPG5jAFul5aeBIKvvhhlX3311QnCYOuLZ7IndvUV6D9ZWnpb5gDEhbF98cz2hJ6mEuC5"
        "mKwBDkiUZKLi8sgz2xg9HZAVwM2XwwM3/7+GkOvL4wHX/68A/6qsrK05/TWsVi/GTnx9uqa2svJf0gIYviwAhtIC/FJVWQsEZSde"
        "kJWB/trKql+kBTB9WQBMXwP8XwEQ1u0jt04jE++VfJVoNiwJtxa2kjh1+H3ZFAKcLjlcVFQEb8vQ1uEStFVTUsRkMovw9unjhw8f"
        "B3Wni9Cr5AnDgopQa2gOnaKDaBufi46Je5oigNO1nVx2T8/Jk+VNpRwum8s5WVN+kvkL+17DPfYvzJPl5Sc5XD7eyeHCwfIT5Sdv"
        "ECecIArmE+VXuD3se/fYbHZVafNJ6O0evOHcLsVtmTfZ9zrgGPT0t6kBKOn0tre3d86vqixod4ct904YsQYvRxubhY7UdmYl87Y7"
        "2llUdBO93jx8vOhGID6h7ngxtkPNcY72yBzdGRfyb3sT264+vzIrSyqZd3wcF9rY2FNYVQWnZQPgZj4CoKyox3nmG2+8QWkvqAg2"
        "nak3cyG7ID924ew3Zs6c+cZsewazgG0zc+bCngIm8VpUw2Q7ztSDE+6WCHtoDTGciZvrWVLzO1yhN/TG0LmdWcuscDQkDtlEVJec"
        "Hglg7iYlwN26uitXmpu++yey70rZrnqmpnrkU7EsZ7SxkBXLIM80nG1pYznbcObCU7GnbGbrLexgMlno9V5pcxXDcja0s+tgNuEe"
        "mq8GmRsaWlpaGhq+YRnEQL1ZWpoazp5NvZPf4Ih7sjI0nGnFqGr+J3HJpuYrV+rq7koN8OtjAIaGhqZeEUGWeoZAEsNw1zPVsw+K"
        "CbKDV3dGjI0e7IyNxa8dzLo7lNmGAGAZWtcsBPAzN5ztRPWymw3NTznrGZp7UJxMDfVcOyq8DA31yP4xoY5wyJld2iQJ8Ku0AJaj"
        "AO4DgLm5nl2o62xTSwQASmdbhcbGxgZZwuVjQskgPDQ01N9qth6ZxWR22OnNXmA5W8+9gxAkBKBRPUC0cwwBQIFTXVmn7OANjREX"
        "i7tkCIlFAJZSA1Q/BmBlpWdJgX8LQHEozdJYz46RX5fPsNMzsvQDAGNzKzBzoGPlVwRZGhk72SGY0mYRgLGRkz/Nydh0tivDGVq7"
        "udmZzjalMmIQcwz0dArt9bstCVAtSwBjKydjYzJ4gWwEAF6gx5lVeoXJctQztqQGkfVMjfTAjFCmxLHcjYwtPZxMjc1pDc3fiQCM"
        "rcgACLgxKJHMYYeFKyM2wspIz+4U80opy83I1Iz660lZAggJEICRsRXFysjM2NyDbGQEABamRk5wWeYpR9BKCyIbmVoQHjAiMyDD"
        "jYztKBQrgCRiCAAsjAlEcwqD4WpkZmpkZGpsRYuIjYFWdgwE4Gpkau5z+6RQv5QA6GdYjRigvr61teX7pqbLly83VbIRAPjf3Ijs"
        "b2cMAH5WJnDZuOq4GNBqFRRKBu/4Qw4sQGoiqOYmZlZksqWJMcwrLdBDyy0/CxMT2GXnGtSeD1LNzO3szM2MyDERDDjVKoZRnc9w"
        "NDKxDL2Kmjc1fd/S2lpfjwEaJ/UzLPRDOOSB9urq+rbWFoIAARibLPCnWeqZUELtjI3tImLsjMwsqTDWXhZmRo6MCLKJMZ6F4BUO"
        "OhmZm8BwG5mZmXn9Ut+EAIIszUxcYxgdPbcqqzpcjc0svYJcTcyNPRinnI3MzSinYhn+VmbG5I7K7wn9La1t9dXV7cgDk/ohHPop"
        "IgJgVRAuaPkeEBCAiQk5guFOdo5hOJqY2DEYVBBn5eHlYWVmZkFjMMiwk5Wffwq9hgZZgQMWgFmaGTuykSQCwI2RX1nZ0lLJdjMx"
        "s6SFxiyAThkwj8E7Ny8K2czchHK/Di73PdKPHFDBwiE0mZ8ioh+DXhpws+LmV4sJmis5CICVz+7h5N9DAB1xDW5mJmbmlmbw170h"
        "rgEJZ1dVdaDXUxQzM3NKDCMmxhVAGG0I4HYoAHiw61pAXiXHHSRHMFgUE5DMqqBYmMAoQNSZOHdUtUjor87nWrkNXJr0r1kv9nsY"
        "U7j381n1V9vaEENrZY+7mTmZXVVXWVfV42xm5thTUdFBWWAB6i3IXh0VFRwIaMeeykr0atfjCHsZ+RVoNrWw8OLUNjU134y1sjCn"
        "9MAm+LPHw8zCilGfzyKbm5Pv57dTybgnK4+GanQ5UN/WdrWelX+fTzH26L84OQBwV1/3GSczsh//QkV9fRtCaL1+Iy4wMKKtrq6y"
        "sr4tNjAwtq2+jXn3FNXD3YN26i4T3kXgnejVL4IVEugXWw9WVx0SGBh3sxKstSo4MDC/raby+PHKmjboLbiqvu5qrB+8tjHvsGgU"
        "dw8q434VpF0rkg9dVVzg+5HNnM6AnEn9IBr/JP2P/lCymWM+v7oCOaGtte3q7fv3O+tqy8vLa+s6wepqa2urqu+zOZz7dVWwXSvc"
        "iQ9ehX8/X6mtqam90tnJ7kRnldf+jHbiTeGb+traK+j1p9orVXV3UE/VdW31zVj+1foKFj/f0Ywc2v/HZH+Sjm8K0NfI43lZWbhz"
        "eiCXcRzBkNRfQf6HCRr0NqONljpkLXi7ltiJj7UI3zQ1tYg2hEeamka8EXXV0lKPemrFhuRXV/Rw3C2svHi8xr7J3hRAeFuGS428"
        "R27QBb+zWhRHLS3NwwpEQh7bI601N6NcEw5/NZtPtbJwe8RrvCTFbRlEN8a4+BvvjJOFXQT/ljCOxkSQmX6x/Ip6foSdhdMZ3m8X"
        "pbsxhujWJA8b/+T5ky2cWNzqaiECzBBTQtAsnHrQ6FdzWU4WZH/en40Ppbw1ifjmMF3nIRUWzPfo4UAqTJ0TxMMPwc/p8Zi/AIL/"
        "fJf0N4eRuD3Ppd94fW7zF/jwOyvEqXAsZ1dxU1PZruIDTU3Fu3bt+kpSzeVd+OCB4hPj6T2xq6wJ2qAzi+FNTnmLeOq8y6ctmO/W"
        "x/vt0rPdnkfiBkkXL0EqzIdUaBAh1AqGhgRNZZ1DQ0M/lgmGBEP8MkJZcVnTgbKbsLsZ/RXtHW1lPUM3y4cE+ExBGRca1oqDP9Zu"
        "vl0M79L5Z75BkuQtqhr7IRXmu96FKbXh6tWrdT22BgYOnZ10AwPr79jWBgYGtvxjMB01lwvYzUOcANiR3Em3NnAg9mIrLx/ePMZ3"
        "MKCzDWwF0IuBtcAB+hKUQrcNMHXedZsPwT/QKJNbVEncJAylAmXBAgqXU8FquFo6NMfAWiNcMGeOp4bBkIOBhrUnB+vrtPUst90O"
        "O23LBQZzbIV7sX5Oz/B2zpABydaTNEfgaa1h4DBkoGHr2VN3tYFVweZSFsynSAT/s94kTPI2bZAKF2F0/Pi/VjSUCgw05swRrNLw"
        "HLLV6Bzy1GALjgmVGXhqCDznzDFILocWtvyyYc3WBkPHxABzYOwNOEM5GgFDAmtoKChtqGjnB9nNd7oIwS/D27T9LnGjvPNdAzFw"
        "BQaflQ8e8NTw7NSwBSn8Hk+N8Jt4eMv5oN12KLwnnOQ5pGHroMEZdgDfwYFfLgYwAP/MEdwM0PDkl4YPWWsIKlhchhMK/q7zMr5R"
        "3u8Styo8/4jnDzME+wHPQMNgzioYfgMNTy4fADprWpAdB9eED3kazEEH58yxFhxvEVqNQFAj2i4EgBo6ALDBhwK2AYQg7wF7eOqU"
        "9a0KR0yp3/IeoQvxV3l6JvXUCQIcwgU1nVGeIp01neEBnS3HPR3iOTVcT8+e6y3DdnyYpaW5MyCAww0P+Ne/ijwT7rJXOXgK+Git"
        "eTSxqVMaAPGUCqlwFrl6SMCqbq/kD7Erb1zv5LYOD3RnT03LTwIBeERyzEdZDYdTW8PuuXHjF/7dCxfuDQ0FQ2iewfKn6HadElPq"
        "w8aHvFC4Xj6XVXGhqrL+1o0b12tbxQRIdM1x9Pf08XH0t7TW1l6/Xl9/49atqgssFjcfRiSU97Dx4VTeMHVEKhDVBQem1Au3bgHB"
        "jevXxQhPN/h0dP36DZB/68IFybpham9ZOyoV+qG6oPLZFRfab2GGiSMg+Vj9rfYLFff5NPKE64ZnBxDftvnSb8Lqop3V/ivY7ds3"
        "b/78808TsJ9/vnnz9m10DpyK6gYU/Oef022bR6RCH8/fbr5bA5fV0I4JJoRAyEf62xtYXKgb7Px5/Y3P78bZkkszkQoLKD0cSYSn"
        "6BfLr+DwUW0iUTc8n1uXDyP04eoCCm0yjX+f1d4+gTgajp72dtZdfhAEfxeuG57zzeMlqovu8128GKf5TrH8BtZT40gielgNfGnq"
        "BtkBSDxA4TwutN3uPy0VRgR/DxvmMH/eo/Mv6gEKowrtRyiY+ZyKBlEcPYaA5Iuip4ItrBu+vfQCH2EhkQpoSj0P00kQ/+54qSA5"
        "dd7lo3X8rLhueFEPERlZaPOIQnvMVBgZ/BVE3XD+4Yt/jMvv4gfpCAttD3YPpMKdOwTEz0IjxN+5g4J/9PcNL/ZBOmMW2myII0mE"
        "YfntLDb+vqEfgr/7ZXmU0ahUQNVFqKCD1X4X2y/YiG3ID0GMtEXz1AKMqC5QgrIgFe6OsgYWv+OZ64apAni80O6BVBgpn43qBgj+"
        "b7tewgeqjfoabwAWKZqAzeroINR3dLDuC/yEdcNL+ki73yUeKvgt8TVejKABJqSODsjdBgHUDU4yqBumFECiukDfaEOhfY/PYjU0"
        "wCdGNvFl2/mX+7GOo7/GQ4U2TKksXDdQHqHgf9kfrDlqSu3CX+PhL9u+le3UOZUA4ofLCgttXDQLv294FR4uO/prPPKIuuFVeLyv"
        "xJTa1/XtAK9v4FLXK/aAZUmES929l17BR1z/Ln7IOLJX8SHjv7/yj3kXfQmGKbqn8CJTCfBc7DXAa4DXAK8BXgO8BngN8CLtfwH+"
        "cx3T1CgQlwAAAABJRU5ErkJggg=="
    ),
    "icon-512.png": (
        "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAMAAADDpiTIAAADAFBMVEX+9L/45rPw26Xm1qbm0Zzkz5nizZjUzrfgypPdyJLcxY7a"
        "wovYwYvXvoe9xMvUvITSuYLQt3/OtHvMsnrLsHa/sZDGqnDCqG+/omeynnOkkGWng0Ojfj6gf0KgfT6hezufezyeejueeTmeeDac"
        "eDibdziZdTaadDSYcjKHel2PcTuUaiWIazeNZSODYy12WSk6dl4meFgmcVMkblBhWkljTyoqYkU4WEOPNj6LND2GMzp/MTdbRyVf"
        "PiV4MDNvMTFEQjxGOiZBNR86MiQ4LyA1LiI2KiEwLywwKyIvKiEuKSAtKB4qJyErJRwpJiAoJSAmJCEnIhslIh4fKikiIiEhISAg"
        "ICEhIBwdHyIdHiIbHSEeHRwaHSIXHSgcHB0ZHCEXHCQTHC0THCwSHCwSHCsaGx0bGhkYGh8XGiAWGyUVGiQWGiEVGR8UGycUGicU"
        "GiUUGSUUGiMUGSITGywTGykTGikTGigTGicTGSgTGyYTGiYTGiUTGSUTGSMSGywSGysSGyoSGisSGioSGykSGikSGigSGycSGicS"
        "GiYSGSkSGSgSGScSGSYSGSQSGSMRGyoRGioRGikRGygRGigRGicRGSkRGSgRGScRGSYRGSURGSQRGSMPGScWGBwUFx4TGCETFx4S"
        "GCUSGCMSFyMSGCISFyARGCcRGCYRGCURGCQRFyURGCMRFyIRFyEQGCcQGCYQGCUQFycQFyUQGCQQFyQQFyMQFyIUFhsSFh4TEhMS"
        "ERERFiARFh8RFh4RFR0RERQREREQFiQQFiMQFiIQFiEQFiAQFh8QFSAQFR4QFR0QFB4QFBsPGCcPFyMPFyIPFiMPFiIPFiEPFiAP"
        "FSIPFSEPFSAPFR8PFR4OFyQOFSIOFSAOFR8KFyUPFCAOFB8PFB4OFB4PFB0OFB0PFBwOFBwOExwNEx8MEh0KEh8SEBEREBIREBEQ"
        "EBIQEBEPEBMPEBIPEBEPEBAJEBwPDyMPDxIQDxEPDxEPDxAODxIODxEIDxwODhANDhENDhAHDhwFCxcM6ELSAAB5F0lEQVR42u29"
        "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"
    ),
    "icon-maskable-512.png": (
        "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAMAAADDpiTIAAADAFBMVEX/+ML457Hu2KLf1bLl0JvkzpfXzq/gypPdyJLcxY7bworY"
        "wYrXvoXIwanTuoLRt37NtX/MsnvLsHbIrnXGqW6+qXi+oWi4mV6rm3aqhUSWkoKlgD+jfj2hfT+hezmefD+eejudeTqedzWcdzib"
        "dzeZdjeYczWaczGWbCeMdEZ1dWyNZiZ5ZkF+XSdvWzVqUSYxc1gmeVgmcFIkb1FYVUxbSSkvYEEzT0SPNT2LND2GMzqAMThcQSNO"
        "PyR3MTNlLy5DQTxEOytBNyU+Mh84MSY3LyA+KCQxMS0yLSMyLCIxKyAuKiIvKB0tKB8rKCArJRspJR8nJSEmJCApIh0lIh8gKSki"
        "IiEhISAhIB0fICEdHyMcHiEeHRwZHSMVHSkbHB8YHCMWHCYTHC0THCsSHCwSHCsaGhwYGh4XGyMXGR0VGyUVGiQWGiEWGR8UGygU"
        "GicUGiUUGSUUGiMUGSITGywTGyoTGikTGygTGigTGicTGScTGiYTGSUTGSMSGywSGysSGyoSGisSGioSGykSGikSGigSGicSGiYS"
        "GSkSGSgSGScSGSYSGSUSGSQRGyoRGioRGikRGigRGSkRGSgRGicRGScRGSYRGSURGSQRGSIPGScWGBwUGB4TGCITFx4SGCUSGCMS"
        "FyMSGCISFyARGCcRGCYRGCURFyURGCQRGCMRFyMRGCIRFyIRFyERFyAQGCcQGCYQGCUQFycQFyUQGCQQFyQQFyMQFyIUFhsSFh0R"
        "FiARFh8RFR0TExUSEhISERIRERISEREQFiQQFiMQFiIQFiEQFh8QFSAQFR4QEyMQFR0QFBwQERMPGCcPFyMPFyIPFiMPFiIPFiEP"
        "FiAPFSIPFSEPFSAPFR8PFR4PFB8PFB4PFBwPExwOFyQOFSIOFSAOFR8OFB8OFB4OFB0OFBwOExwNEx4JFSMKER0PECIPEBMREBEQ"
        "EBIQEBEQEBAPEBIPEBEPEBAJEB0HEB8QDxEPDxIPDxEPDxAODxIODxEHDxwPDhANDhENDhAHDhsECxeSri4BAABWOklEQVR42u2d"
        "CVhUV7bvkeCIIkZFQeMMBmT4klCawUoQLJECAQW0jCVIvClEwRi9SceoN2k1MUbIcGM0nULwSeJTM9x0J9EqIRgTp8jr1y3G2Y7X"
        "ToN0+7jY0JhIsHhr7eHUOTUwRZlqr6/bVJ1hn137/9trrb33OQe3fwhzaXMTTSAAECYAECYAECYAECYAECYAECYAECYAECYAECYA"
        "ECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYA"
        "ECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYA"
        "ECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYAECYA"
        "EAC49K+vpiYAcEnt6+rqfqZWV+e6ELgoANVce8nqqgUArqO+JH+d1Q24KANurtv562Tmugi4HADVkvqQ/d0gVi3LB1wOATeX7P6o"
        "Pih/kxmBgCNQLQBwDflvKkyGgGs5AVcCQJIf1L8FoiMGX5eg9JwBF0TAzcW6P+n9KHfdjRtVVVVfNTR89XVVFWh/E5iwIuA68wJu"
        "Lik/OgGQv6TqVnz0rV++go9VoD0iwAOByzgBNxeSn3l/lB8lLylp+Cronh5B+oavSzgCNBdwpTjg5nLy36TyH/rqdkP4ADewe8Jv"
        "N5QwBFwvFXABAKp/tvH+IPV1kD96OKjv59fDzc07ouH2V4eqWC7gWgi4uUr3r5Z5/+slXzfoUPkB4bWWiKHAwfCZDb+UXLdPBbp/"
        "NujmCvLXKbz/dQj+P4XfA74/6Pua1NSrlRgJ7gkyQBy4LosDLpINdm8Aqp0E/wiQvIdfiiU19ZlnUlNr04MAh17htyEOHFYg4AJx"
        "wM0Fuj8G/1tS8P+lYSYG/6ERlgKQHy31GUs0BoShmArwOCBPBaoFAN1h5M+CvwG7OwT/H1KXc3sm9c8W6hTmNlSVyBBwgTjg1q3l"
        "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"
        "tjsAkAXevHG4IQkAqEAArEFAIuD7U2UQavMuq6hMpgyzuRgfs4BM3wg6TgyA/yVDSIZ83KgaNYG08YRx9L8Bo0Ih5sMuoz6EAnA5"
        "7wT0NLNpDp4IABh0Bq4fBAMyJgQ3Ti4VUiwD4Fx+KQVgotaUj92XpCenvrfuCNCaAM3MTIZWiJE8nZDMCODqw3/VpkwzlHCGyi/X"
        "nzoAc14FAJDUcPjGTcgBuzEALAm4WQ0ATBw3vTEfRCxGF3DixHloD0DgLOloZ8rOnjufV84AKDcWg4Ro0JFNybRVdXgmjPxM6oBx"
        "E9loLmDixHEBKkOmsRh3GUPY6XkQYs4VnUxmHsCoMzG5DbiKbM43gYthSCSXF507i/20DNLQ/HIN3TrrEgwPz5SR9JTuYADMKs0r"
        "NpszS0lFA8ZpyqG0TCPQNC5AsnHjJkWB/ifg153+Hn/bGfIz4ev5EyeOn8SqmvPyG6ePmwgAYHxs5xSgfQHgc4GHGxKgYdT5NXn5"
        "xUgA2HlU+OxZ6gnOXjh3Pr9GNWrixImjVDX55+mec+fOF1/STUQL0JcWnzh/AoZkpiTVpInjmE0K05oyTbDn/AmzKWQcOx1L5ieO"
        "UpdmJgfgnnHwyVR88gQUYjJCvCA764tQaQAAPEBRpWYcOScesThziphiR2K5GU5HKEmBIScRSqPRFBUawGs0cdJUHeh/8jwb69Jf"
        "iDU6j78af31+Xk0+JAvjEhAAGgG6KwBSEnCjqkR9/6hxIdMry/PNcgIYAmcvXLh44pJOizbv4vFzpO3IuPlE3izcOCvvxHH0oUVA"
        "gEkXNTUsLDQsTBWJUzV0sHbuhDmRn06anJ0YozeZ0sgOrcEM/fIciAEamhNjcNMcOJgAgFnIuYv0wNjM83wrRIEy2Y7sE+BcjvOz"
        "Y2PzzoMvAydgMiZEqkiV1Bq9yWg0nzjP0lxJfkl/c3555fSQcaPuV5dgCtjuKUD7AkBiAPg5GAne0odNHDUxLLH+Yr6CAIbAheMH"
        "i87/+LeTf/vxZGHRJx9x++TToh8v/ghW+Oknn+BXfAwPGDCRZ7TwOax8sucT2PMjnP7jjyfo6XjijyeLT/5YnJfHPuV/WvjJJ7TQ"
        "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"
        "AuB4lfD64YZb9DGi8nobAsw1lVHylzx1n1t+XBQAx1OD0kJxmLam0mSV31Ren9RFX/IkAGjprID9QvFlhoCJzvvi0O+Xkq79qKcA"
        "wAYBJwvF7DEiixkQMJstpXTe9zaM/A9384k/FwNAcgK2j5NKjxGZTPKXPHXav+wjALij2aD1DxPGWSzJTc37drVHPQUATcUBxT1j"
        "P7GFYnVTL3nq7t3fNQBw/teIMBUgwd9V5n1dFABbBBTvm6R/0b3b3vElAHA2K0DePl51y6C7deNQVbdf9BUA2CLAlgkPNTRIsb8L"
        "v+ZDANAiU94rgM+SIQTo9G/edL3g74IAyFIBmg7KzDrwdyn5XQwAGwRuWNW3yv9ztWu1iIsBwGcFyOQggYCID+oz+etcTH/XA4A7"
        "AcKAZD+7qPyuCAAiwAX/WSa+S8rvmgDI3YBrq++6AODrBmWev6662lXbwWUBYBQQc+UmcG0AhAkABADCBADCBADCBADCBADCBADC"
        "BADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADC"
        "BADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADC"
        "BADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADC"
        "BADCBADCBADCBADC/vH/AU7D/u9uDDMeAAAAAElFTkSuQmCC"
    ),
    "apple-touch-icon.png": (
        "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAADAFBMVEXu4K3o16Tn0pzh0Z/kzpjiypHfyZPYyZnbw4vXvYTSvYrS"
        "t33NtX/LsXnJrHG+tJO/qnm9pnO6n2iwnG6tjVCknouiiVimgUGlgD2WkXyXg1iHi4tfg4KjfTqgfDyfeTebeTuadTaMeE6ScTiQ"
        "bDGFbUCFaDaEYip8XCdudXtpZ1pyWzJdXVYmeFglclMmaUsrXUFvVCViUTFjSyFcSShVTDk9TkWQNzyKNDyGMzp9MTZXRCRQQypm"
        "NidgKzRJQjRJPihHOyVCNyRBLyI4ODQ4MiU4Lx8vLiszKx0vKR4qKCQtKB4qJR0oJSEnJSImJCAqIR4mIhwlIRsbMTIhIyMjIR8e"
        "ISQiHxsfICAbHyQdHiIfHhsbHSIYHScVHSocHB8ZHCIWHCYTHC0THCwSHCwSHCscGx4YGh8WGyMXGiAVGycUGiUUGSUVGiITGywT"
        "GykTGikTGSgTGiUTGSUSGywSGysSGyoSGisSGioSGykSGikSGigSGicSGiYSGSkSGSgSGScSGSYSGSQRGykRGioRGikRGigRGicR"
        "GSgRGScRGSYRGSURGSQQGikQGSYQGSQQGSANGSgXGBoUGB8TGCESGCUSGCMSFyMSGCISFyARGCcRGCYRGCURGCQRFyURGCMRFyMR"
        "FyIRFyERFyAQGCgQGCYQGCUQFycQFyUQGCQQFyQQFyMQFyIVFRcTFBcSFh4SFBkRFiERFh8RFSIRFh4RFRwRFBsSEhMTEhISEhIS"
        "ERIREhIRERIREREQFiMQFiIQFiEQFh8QFSAQEyMQExgPGCYPFyMPFyIPFiMPFiIPFiEPFiAPFSIPFSEPFSAPFR8PFR4PFB8PFBwP"
        "ExwOFyQOFSIOFSAOFR8OFB8OFB4OFB0OFBwOEx0NFyYNFCANFB8NFB0NEx8NEx4NEx0KFiQMEh4MERwMEhsJER8SEBEQEBEPEBMP"
        "EBIPEBEJEB0PDyIPDxIQDxEPDxEPDxAODxIODxEIDxwODhENDhENDhAHDhsHDRkFDBoDCxsCChgBBhMVF8ZPAAAdbklEQVR42u2d"
        "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"
    ),
    "favicon.ico": (
        "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"
    ),
}

_PWA_ICONS = {k: _b64.b64decode("".join(v)) for k, v in _PWA_ICONS_B64.items()}
PWA_VERSION = "moha-pwa-1"

_PWA_MANIFEST = {
    "name": "MOHA PRO",
    "short_name": "MOHA PRO",
    "description": "MOHA PRO — la socodka iyo maamulka bot-ka MT5",
    "id": "/dashboard",
    "start_url": "/dashboard",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#0f1013",
    "theme_color": "#0f1013",
    "lang": "so",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Service worker: XOGTA LIVE WEELIGEED LAMA KAYDIYO (/api/* = network oo keliya).
# Kaliya icon-nada ayaa la kaydiyaa, iyo bog "Internet ma jiro" marka xiriirku go'o.
_PWA_SW = r"""
const V = "__VER__";
const OFF = '<!doctype html><html lang="so"><head><meta charset="utf-8">'
 + '<meta name="viewport" content="width=device-width,initial-scale=1">'
 + '<meta name="theme-color" content="#0f1013"><title>MOHA PRO</title>'
 + '<style>html,body{height:100%;margin:0;background:#0f1013;color:#eceae6;'
 + 'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}'
 + '.w{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;'
 + 'gap:14px;padding:24px;text-align:center}img{width:112px;height:112px;border-radius:24px}'
 + 'h1{font-size:20px;margin:6px 0 0}p{color:#9a9ea8;font-size:14.5px;line-height:1.5;margin:0;max-width:300px}'
 + 'button{margin-top:10px;padding:13px 26px;border-radius:999px;border:1px solid #3987e5;'
 + 'background:rgba(57,135,229,.18);color:#bcd9fb;font-size:15px;font-weight:700;font-family:inherit}</style>'
 + '</head><body><div class="w"><img src="/icons/icon-192.png" alt="">'
 + '<h1>Internet ma jiro</h1><p>Xogta bot-ka waxay ka timaadaa server-ka. '
 + 'Bot-ku wuu sii shaqeynayaa MT5 gudihiisa — app-ka ayaan hadda xogta arki karin.</p>'
 + '<button onclick="location.reload()">Isku day mar kale</button></div></body></html>';
const PRE = ["/icons/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(PRE)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const ks = await caches.keys();
    await Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;
  if (r.mode === "navigate") {
    e.respondWith(fetch(r).catch(() =>
      new Response(OFF, {headers: {"Content-Type": "text/html; charset=utf-8"}})));
    return;
  }
  if (u.pathname.startsWith("/icons/") || u.pathname === "/favicon.ico" ||
      u.pathname === "/apple-touch-icon.png") {
    e.respondWith(caches.open(V).then(c => c.match(r).then(m => m || fetch(r).then(res => {
      if (res.ok) c.put(r, res.clone());
      return res;
    }))));
  }
  // /api/* iyo wax kasta oo kale: network oo keliya -> xogtu mar walba waa mid cusub
});
""".replace("__VER__", PWA_VERSION)


def _icon_resp(name):
    data = _PWA_ICONS.get(name)
    if data is None:
        return Response("not found", status=404, mimetype="text/plain")
    mt = "image/x-icon" if name.endswith(".ico") else "image/png"
    return Response(data, mimetype=mt,
                    headers={"Cache-Control": "public, max-age=2592000, immutable"})


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return Response(json.dumps(_PWA_MANIFEST, ensure_ascii=False),
                    mimetype="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sw.js")
def pwa_sw():
    return Response(_PWA_SW, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icons/<name>")
def pwa_icon(name):
    return _icon_resp(name)


@app.get("/favicon.ico")
def pwa_favicon():
    return _icon_resp("favicon.ico")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def pwa_apple():
    return _icon_resp("apple-touch-icon.png")

# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
_fails = {}   # (account, ip) -> [count, first_ts]

def _throttled(key):
    rec = _fails.get(key)
    if not rec:
        return False
    cnt, first = rec
    if time.time() - first > 600:
        _fails.pop(key, None)
        return False
    return cnt >= 8

def _fail(key):
    cnt, first = _fails.get(key, (0, time.time()))
    if time.time() - first > 600:
        cnt, first = 0, time.time()
    _fails[key] = (cnt + 1, first)

def current_user():
    acc = session.get("acc")
    if not acc:
        return None
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE account=?", (acc,)).fetchone()
    if r is None or not r["approved"]:
        session.clear()
        return None
    return r

def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if u is None:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="unauthorized"), 401
            return redirect(url_for("login", next=request.path))
        request.user = u
        return fn(*a, **k)
    return w

def admin_required(fn):
    @wraps(fn)
    @login_required
    def w(*a, **k):
        if request.user["role"] != "admin":
            return jsonify(ok=False, error="forbidden"), 403
        return fn(*a, **k)
    return w

# v11: FURAHA QOF KASTA. Fure kasta (MP-XXXX-XXXX-XXXX) hal account oo keliya ayuu furaa.
#  Furaha guud ee hore (CLOUD_TOKEN) wuu sii shaqeeyaa oo keliya account aan weli fure lahayn,
#  si bot-yada hadda socda aysan u go'in. LEGACY_TOKEN=0 (Render) -> gebi ahaanba waa la xidhaa.
LEGACY_TOKEN = os.environ.get("LEGACY_TOKEN", "1").strip() != "0"
_KEY_ALPH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_key():
    return "MP-" + "-".join("".join(secrets.choice(_KEY_ALPH) for _ in range(4)) for _ in range(3))


def _ea_body():
    """v12.8.1: jidhka JSON ee EA-ga. EA v69.6 (iyo ka hor) wuxuu u diri jiray ANSI (cp1252):
    xaraf sida '·' ama '–' -> UTF-8 khaldan -> JSON {} -> account la'aan -> 403 been ah (furaha).
    Hadda: UTF-8 -> cp1252 -> latin-1. Haddii weli la akhriyi waayo: g._ebad=True (400, maaha 403)."""
    cached = getattr(g, "_ebody", None)
    if cached is not None:
        return cached
    d = request.get_json(silent=True, force=True)
    bad = False
    if d is None:
        raw = request.get_data(cache=True) or b""
        if raw.strip():
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    d = json.loads(raw.decode(enc))
                    break
                except (UnicodeDecodeError, ValueError):
                    d = None
            bad = d is None
    if not isinstance(d, dict):
        d = {}
    g._ebody = d
    g._ebad = bad
    if bad:
        log.warning("EA body: JSON lama akhriyi karo (%d bytes) %s", len(request.get_data(cache=True) or b""), request.path)
    return d


def _ea_bad_body():
    return jsonify(ok=False, error="xogta EA-ga (JSON) lama akhriyi karo"), 400


def _presented_token():
    cached = getattr(g, "_ptok", None)          # /update wuu tirtiraa "token" jidhka -> kaydi
    if cached is not None:
        return cached
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip():
        g._ptok = auth[7:].strip()
        return g._ptok
    body = _ea_body()                                      # v12.8.1
    t = str(body.get("token", "") or "") if isinstance(body, dict) else ""
    t = t or request.args.get("token", "")
    g._ptok = t
    return t


def _eq(a, b):
    try:
        return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))
    except (TypeError, ValueError):
        return False


def _is_legacy(t):
    return bool(CLOUD_TOKEN) and len(CLOUD_TOKEN) >= 8 and _eq(t, CLOUD_TOKEN)


def token_ok():
    """EA auth (hordhac): furaha guud ama fure account ah oo shaqeynaya."""
    t = _presented_token()
    if not t:
        return False
    if _is_legacy(t):
        return LEGACY_TOKEN
    if not t.startswith("MP-") or len(t) > 40:
        return False
    with db() as con:
        r = con.execute("SELECT active FROM bot_keys WHERE bkey=?", (t,)).fetchone()
    return r is not None               # v12: xannibaadda ea_bind ayaa sheegta (lic=BLOCKED)


def ea_bind(acc):
    """v11: furaha la keenay ma u shaqeeyaa account-kan? (token_ok kadib)"""
    t = _presented_token()
    now = time.time()
    if _is_legacy(t):
        if not LEGACY_TOKEN:
            return False
        with db() as con:
            has = con.execute("SELECT 1 FROM bot_keys WHERE account=?", (acc,)).fetchone()
        return has is None            # account fure leh -> furaha guud lagama aqbalo
    with db() as con:
        r = con.execute("SELECT account,active,last_seen FROM bot_keys WHERE bkey=?", (t,)).fetchone()
        if not r or r["account"] != acc:
            return False
        if not r["active"]:
            g._lic = "BLOCKED"          # v12: bot-ku trade cusub ma furo
            return False
        if now - float(r["last_seen"] or 0) > 30:
            con.execute("UPDATE bot_keys SET last_seen=? WHERE bkey=?", (now, t))
    return True


def _key_for(con, acc, create=False):
    r = con.execute("SELECT * FROM bot_keys WHERE account=?", (acc,)).fetchone()
    if r is None and create:
        k = _new_key()
        con.execute("INSERT INTO bot_keys(account,bkey,active,created_at) VALUES(?,?,1,?)", (acc, k, time.time()))
        r = con.execute("SELECT * FROM bot_keys WHERE account=?", (acc,)).fetchone()
    return r


BAD_KEY = ("Furaha bot-ka kuma habboona account-kan (ama waa la xannibay). "
           "App-ka ka koobi garee furaha saxda ah oo MT5 -> Inputs -> MohaPro_Key ku dheji.")


def _bad_key():
    lic = getattr(g, "_lic", "")
    body = {"ok": False, "error": BAD_KEY}
    if lic:
        body["lic"] = lic
    return jsonify(body), 403


# --------------------------------------------------------------------------
# v12: LAYSINKA MACMIILKA + OGGOLAANSHAHA
#  licenses: account kasta oo macmiil ah (until, perms, follow, ovr)
#  follow=1 -> sitinka MASTER-ka (account-ka admin-ka) + waxa macmiilka loo oggol yahay
#  Nuqulka macmiilka (EA v67.6 MACMIIL): lic != OK -> trade cusub ma furo
# --------------------------------------------------------------------------
PERM_KEYS = ("run", "close", "risk", "lot", "sltp", "day", "prot")
PERM_DEFAULT = {"run": 1, "close": 1, "risk": 1, "lot": 0, "sltp": 0, "day": 0, "prot": 0,
                "lo": 0.10, "hi": 0.50}
PERM_OF = {"RISK": "risk", "LOT": "lot",
           "SLTP": "sltp", "SL": "sltp", "TP": "sltp", "SNIPER": "sltp", "SNRR": "sltp",
           "SNSLMAX": "sltp", "ADAPT": "sltp",
           "SNDAY": "day",
           "NEWS": "prot", "STEPON": "prot", "STEP": "prot", "STEPSTART": "prot",
           "BE": "prot", "LOCKMODE": "prot", "PROT": "prot"}   # DLOSS, MAXDD, MGMT, STRAT, EMAF, STARS, LOT2 = admin oo keliya


def _jload(v, d=None):
    try:
        x = json.loads(v or "")
        return x if isinstance(x, dict) else (d if d is not None else {})
    except (ValueError, TypeError):
        return d if d is not None else {}


def _perms_of(row):
    p = dict(PERM_DEFAULT)
    if row is not None:
        p.update(_jload(row["perms"]))
    for k in PERM_KEYS:
        p[k] = 1 if int(_num(p.get(k), 0)) else 0
    lo = max(0.01, min(10.0, _num(p.get("lo"), 0.10)))
    hi = max(lo, min(10.0, _num(p.get("hi"), 0.50)))
    p["lo"], p["hi"] = round(lo, 2), round(hi, 2)
    return p


def _lic_row(con, acc):
    return con.execute("SELECT * FROM licenses WHERE account=?", (acc,)).fetchone()


def _lic_info(con, acc, now=None):
    """None = macmiil maaha (account caadi ah / admin)."""
    now = now or time.time()
    r = _lic_row(con, acc)
    if r is None:
        return None
    k = con.execute("SELECT active FROM bot_keys WHERE account=?", (acc,)).fetchone()
    until = float(r["until"] or 0)
    if k is not None and not k["active"]:
        st = "BLOCKED"
    elif until > now:
        st = "OK"
    else:
        st = "EXPIRED"
    days = int((until - now) // 86400) if until > now else 0
    return {"state": st, "until": int(until), "days": days,
            "perms": _perms_of(r), "follow": bool(r["follow"])}


def _master(con):
    r = con.execute("SELECT v FROM kv WHERE k='master'", ()).fetchone()
    return (r["v"] if r else "") or ""


def _cfg_row(con, acc):
    r = con.execute("SELECT data,rev FROM ea_config WHERE account=?", (acc,)).fetchone()
    return (_jload(r["data"]) if r else {}), (int(r["rev"]) if r else 0)


def _lic_effective(con, acc, row):
    """Sitinka dhabta ah ee macmiilka: master (follow) + admin + macmiil (oggol)."""
    p = _perms_of(row)
    ovr = _jload(row["ovr"])
    eff = {}
    if row["follow"]:
        m = _master(con)
        if m and m != acc:
            eff.update({k: v for k, v in _cfg_row(con, m)[0].items() if k not in PR_KEYS})   # v12.22: lamaanaha master-ka macmiil looma gudbiyo
    eff.update({k: v for k, v in (ovr.get("a") or {}).items() if k in CFG_KEYS or str(k).startswith("IN:")})   # v12.18
    for k, v in (ovr.get("c") or {}).items():
        perm = PERM_OF.get(k)
        if k in CFG_KEYS and perm and p.get(perm):
            if k == "RISK":
                v = "%.2f" % max(p["lo"], min(p["hi"], _num(v, p["lo"])))
            eff[k] = v
    return eff


def _cfg_write(con, acc, eff, by, force=False):
    """Sitinka account-ka kaydi + chart kasta u dir. Furayaal la tuuray -> SET:RESET marka hore."""
    old, rev = _cfg_row(con, acc)
    if eff == old and not force:
        return rev, 0
    rev += 1
    now = time.time()
    con.execute(
        "INSERT INTO ea_config(account,data,rev,updated_at) VALUES(?,?,?,?)"
        " ON CONFLICT(account) DO UPDATE SET data=excluded.data, rev=excluded.rev, updated_at=excluded.updated_at",
        (acc, json.dumps(eff), rev, now))
    cmds = (["SET:RESET"] if (set(old) - set(eff)) else []) + _cfg_cmds(eff, rev)
    con.insert_many_ignore("commands", ("account", "cmd", "by_account", "created_at"),
                           [(acc, c, by, now + i * 1e-6) for i, c in enumerate(cmds)])
    return rev, len(cmds)


def _lic_apply(con, acc, by="system", force=False):
    row = _lic_row(con, acc)
    if row is None:
        return None
    return _cfg_write(con, acc, _lic_effective(con, acc, row), by, force)


def _propagate_master(con, by):
    m = _master(con)
    if not m:
        return 0
    n = 0
    for r in con.execute("SELECT account FROM licenses WHERE follow=1", ()).fetchall():
        if r["account"] != m:
            _lic_apply(con, r["account"], by)
            n += 1
    return n


def _user_lic(u, acc):
    """Isticmaale aan admin ahayn oo account-kiisu macmiil yahay -> lic info, haddii kale None."""
    if u["role"] == "admin":
        return None
    with db() as con:
        return _lic_info(con, acc)


# v5.3: nadiifinta safafka duugga ah - 30 codsi mar, ee ma aha codsi kasta
_PRUNE_EVERY = int(os.environ.get("PRUNE_EVERY", "30"))
_prune_n = {}
_prune_lock = threading.Lock()

def _should_prune(key):
    if _PRUNE_EVERY <= 1:
        return True
    with _prune_lock:
        c = _prune_n.get(key, 0) + 1
        if c >= _PRUNE_EVERY:
            _prune_n[key] = 0
            return True
        _prune_n[key] = c
        return False


# v5.4 BANDWIDTH: jawaabaha JSON/HTML gzip ku cadaadi. JSON-ku 80-90% buu yaraadaa.
import gzip as _gzip, io as _io

@app.after_request
def _compress(resp):
    try:
        if resp.direct_passthrough or resp.status_code >= 300:
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        if resp.headers.get("Content-Encoding"):
            return resp
        ctype = (resp.headers.get("Content-Type") or "")
        if not any(t in ctype for t in ("json", "html", "text", "javascript", "xml", "css")):
            return resp
        data = resp.get_data()
        if len(data) < 1024:            # yar - faa'iido ma leh
            return resp
        buf = _io.BytesIO()
        with _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
            f.write(data)
        packed = buf.getvalue()
        if len(packed) >= len(data):
            return resp
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp


def clean_account(v):
    return re.sub(r"\D", "", str(v or ""))[:20]

# --------------------------------------------------------------------------
# EA endpoints
# --------------------------------------------------------------------------
ANALYSIS_TTL = int(os.environ.get("ANALYSIS_TTL", "900"))   # 15 daq -> saf duug ah waa la iska dhaafaa


def _lv_num(v, nd=6):
    try:
        f = float(v)
        return round(f, nd) if f == f and abs(f) < 1e9 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _save_levels(con, acc, d):
    """v12.3 (EA v67.7): heerarka & range - zone-yada ugu dhow, ATR, jihada, shumacyada.
    Shumacyada EA-du mar walba ma dirto (bandwidth) -> kii hore ayaa la sii hayaa."""
    lv = d.get("levels")
    if not isinstance(lv, dict):
        return
    a = d.get("analysis") if isinstance(d.get("analysis"), dict) else {}
    sym = str(a.get("sym") or d.get("symbol") or "")[:16]
    if not sym:
        return
    out = {"tf": str(lv.get("tf") or "")[:6], "px": _lv_num(lv.get("px")),
           "dg": int(_lv_num(lv.get("dg"), 0)) if 0 <= _lv_num(lv.get("dg"), 0) <= 8 else 5,
           "pp": _lv_num(lv.get("pp"), 8), "atr": _lv_num(lv.get("atr"), 1),
           "mtf": (1 if _lv_num(lv.get("mtf")) > 0 else (-1 if _lv_num(lv.get("mtf")) < 0 else 0)),
           "t": int(_lv_num(lv.get("t"), 0))}
    zs = []
    for z in (lv.get("z") or [])[:6]:
        if not isinstance(z, dict):
            continue
        lo, hi = _lv_num(z.get("lo")), _lv_num(z.get("hi"))
        if lo <= 0 or hi <= 0:
            continue
        zs.append({"d": 1 if _lv_num(z.get("d")) else 0, "lo": min(lo, hi), "hi": max(lo, hi),
                   "t": int(_lv_num(z.get("t"), 0)), "ia": _lv_num(z.get("ia"), 2),
                   "ip": _lv_num(z.get("ip"), 1), "r1": _lv_num(z.get("r1"), 1),
                   "dp": _lv_num(z.get("dp"), 1), "u": 1 if _lv_num(z.get("u")) else 0})
    out["z"] = zs
    bars = lv.get("b")
    if isinstance(bars, list) and bars:
        bb = []
        for b in bars[-60:]:
            if isinstance(b, (list, tuple)) and len(b) >= 3:
                bb.append([_lv_num(b[0]), _lv_num(b[1]), _lv_num(b[2])])
        out["b"], out["bt"] = bb, int(_lv_num(lv.get("bt"), 0))
    else:
        r = con.execute("SELECT data FROM levels WHERE account=? AND sym=?", (acc, sym)).fetchone()
        old = _jload(r["data"]) if r else {}
        if old.get("b"):
            out["b"], out["bt"] = old["b"], old.get("bt", 0)
    con.execute(
        "INSERT INTO levels(account,sym,data,ts) VALUES(?,?,?,?)"
        " ON CONFLICT(account, sym) DO UPDATE SET data=excluded.data, ts=excluded.ts",
        (acc, sym, json.dumps(out, separators=(",", ":")), time.time()))


def _bsk_num(v, nd=2, lo=-1e9, hi=1e9):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:
        return 0.0
    return round(max(lo, min(hi, f)), nd)


CFGSET_KEYS = ("BSK", "BSKSIG", "ASIA", "ASG", "ASGM", "TK")   # v12.12: badhanka ⚡ TICK   # v12.7.1 · v12.9.1 (EA v69.7.1): waxa chart-ku beddeli karo (badhamada toolbar-ka)


def _apply_cfgset(con, acc, d):
    """v12.7.1 (EA v69.1): chart-ka ayaa badhanka 🥇 BASKET / SIGNAL beddelay -> sitinka server-ka ku dar
    (MT5 dib u kicin -> isla doorashadii). rev-ka lama kordhiyo (amar cusub lama dirayo).
    Macmiilka (laysin) lagama aqbalo - sitinkiisa admin-ka ayaa maamula."""
    cs = d.get("cfgset")
    if not isinstance(cs, dict) or not cs:
        return
    if _lic_row(con, acc) is not None:
        return
    clean = {}
    for k in CFGSET_KEYS:
        if k in cs:
            ok, c = valid_command("SET:%s=%s" % (k, cs[k]))
            if ok:
                clean[k] = c.split("=", 1)[1]
    if not clean:
        return
    r = con.execute("SELECT data,rev FROM ea_config WHERE account=?", (acc,)).fetchone()
    merged = _jload(r["data"]) if r else {}
    if not isinstance(merged, dict):
        merged = {}
    merged.update(clean)
    con.execute(
        "INSERT INTO ea_config(account,data,rev,updated_at) VALUES(?,?,?,?)"
        " ON CONFLICT(account) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (acc, json.dumps(merged), int(r["rev"]) if r else 0, time.time()))


def _save_basket(con, acc, d):
    """v12.7 (EA v69.0): GOLD BASKET - chart-ka dahabka ayaa diraa. Chart-yada kale ma dirayaan,
    sidaas darteed xaaladda basket-ka gooni ayaa loo kaydiyaa (kv) si aan loo tirtirin."""
    b = d.get("bsk")
    if not isinstance(b, dict) or not b.get("gold"):
        return
    day = b.get("day") if isinstance(b.get("day"), dict) else {}
    out = {"sym": str(b.get("sym") or "")[:16], "on": bool(b.get("on")), "sig": int(_bsk_num(b.get("sig"), 0, 0, 3)),
           "act": bool(b.get("act")), "why": str(b.get("why") or "")[:160],
           "day": {"n": int(_bsk_num(day.get("n"), 0, 0, 99)), "max": int(_bsk_num(day.get("max"), 0, 0, 99)),
                   "pl": _bsk_num(day.get("pl")), "blk": str(day.get("blk") or "")[:12]},
           "ts": time.time()}
    cf = b.get("cfg")                                        # v12.7.1: sitinka basket-ka ee chart-ka dahabka
    if isinstance(cf, dict):
        out["cfg"] = {"on": 1 if _bsk_num(cf.get("on"), 0, 0, 1) else 0, "sig": int(_bsk_num(cf.get("sig"), 0, 0, 3)),
                      "n": int(_bsk_num(cf.get("n"), 0, 2, 5)), "ent": int(_bsk_num(cf.get("ent"), 0, 0, 1)),
                      "risk": _bsk_num(cf.get("risk"), 2, 0.05, 5), "max": int(_bsk_num(cf.get("max"), 0, 0, 100000)),
                      "day": int(_bsk_num(cf.get("day"), 0, 1, 10)), "spr": int(_bsk_num(cf.get("spr"), 0, 5, 500)),
                      "tp": int(_bsk_num(cf.get("tp"), 0, 0, 1)), "tgt": int(_bsk_num(cf.get("tgt"), 0, 1, 100000)),
                      "be": 1 if _bsk_num(cf.get("be"), 0, 0, 1) else 0, "lock": 1 if _bsk_num(cf.get("lock"), 0, 0, 1) else 0,
                      "ses": 1 if _bsk_num(cf.get("ses"), 0, 0, 1) else 0, "blk": 1 if _bsk_num(cf.get("blk"), 0, 0, 1) else 0,
                      "dtol": _bsk_num(cf.get("dtol"), 2, 0.05, 1), "dneck": _bsk_num(cf.get("dneck"), 2, 0.3, 3),
                      "dtp": _bsk_num(cf.get("dtp"), 1, 1, 5)}
    if out["act"]:
        dg = int(_bsk_num(b.get("dg"), 0, 0, 8))
        out.update({"id": int(_bsk_num(b.get("id"), 0, 0, 10**7)), "dir": "SELL" if str(b.get("dir")) == "SELL" else "BUY",
                    "src": str(b.get("src") or "")[:8], "stars": int(_bsk_num(b.get("stars"), 0, 0, 3)),
                    "n": int(_bsk_num(b.get("n"), 0, 0, 5)), "fill": int(_bsk_num(b.get("fill"), 0, 0, 5)),
                    "open": int(_bsk_num(b.get("open"), 0, 0, 5)), "ent": "LAKAB" if str(b.get("ent")) == "LAKAB" else "ISKU MAR",
                    "pl": _bsk_num(b.get("pl")), "peak": _bsk_num(b.get("peak")), "max": _bsk_num(b.get("max")),
                    "risk": _bsk_num(b.get("risk")), "tgt": _bsk_num(b.get("tgt")), "tpm": int(_bsk_num(b.get("tpm"), 0, 0, 1)),
                    "be": bool(b.get("be")), "tp1": bool(b.get("tp1")), "t0": int(_bsk_num(b.get("t0"), 0, 0, 4e9)),
                    "sl": _bsk_num(b.get("sl"), dg, 0, 1e7), "dg": dg})
        rows = []
        for r in (b.get("rows") or [])[:5]:
            if not isinstance(r, dict):
                continue
            st = str(r.get("st") or "")
            rows.append({"k": int(_bsk_num(r.get("k"), 0, 0, 9)), "px": _bsk_num(r.get("px"), dg, 0, 1e7),
                         "lot": _bsk_num(r.get("lot"), 2, 0, 1e4), "pl": _bsk_num(r.get("pl")),
                         "tp": _bsk_num(r.get("tp"), dg, 0, 1e7), "r": _bsk_num(r.get("r"), 1, 0, 50),
                         "st": st if st in ("WAIT", "OPEN", "TP", "SL", "BE", "OFF") else "OFF"})
        out["rows"] = rows
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("bsk:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


def _save_tick(con, acc, d):
    """v12.12 (EA v70.0): ⚡ TICK SCALPER - chart-ka dahabka ayaa diraa -> kv."""
    a = d.get("tick")
    if not isinstance(a, dict) or not a.get("gold"):
        return
    n = _bsk_num
    dg = int(n(a.get("dg"), 0, 0, 8))
    st = str(a.get("st") or "")
    out = {"sym": str(a.get("sym") or "")[:16], "on": bool(a.get("on")),
           "st": st if st in ("OFF", "SEARCH", "PAUSE", "OPEN") else "PAUSE",
           "why": str(a.get("why") or "")[:160], "dg": dg,
           "dd": n(a.get("dd"), 2, 0, 100), "mdd": n(a.get("mdd"), 2, 0, 100), "ts": time.time()}
    dy = a.get("day") if isinstance(a.get("day"), dict) else {}
    out["day"] = {"n": int(n(dy.get("n"), 0, 0, 100000)), "w": int(n(dy.get("w"), 0, 0, 100000)), "pl": n(dy.get("pl")),
                  "cl": int(n(dy.get("cl"), 0, 0, 1000)), "pause": int(n(dy.get("pause"), 0, 0, 4e9))}
    to = a.get("tot") if isinstance(a.get("tot"), dict) else {}
    out["tot"] = {"n": int(n(to.get("n"), 0, 0, 1e9)), "w": int(n(to.get("w"), 0, 0, 1e9)), "pl": n(to.get("pl")), "pf": n(to.get("pf"), 2, 0, 1000),
                  "apk": n(to.get("apk"), 2, -1e5, 1e5), "asl": n(to.get("asl"), 2, -1e5, 1e5), "asp": n(to.get("asp"), 2, 0, 1e5),
                  "sln": int(n(to.get("sln"), 0, 0, 1e9))}   # v12.14
    tt = a.get("tt") if isinstance(a.get("tt"), dict) else {}
    out["tt"] = {"up": int(n(tt.get("up"), 0, 0, 100)), "dn": int(n(tt.get("dn"), 0, 0, 100)), "mv": n(tt.get("mv"), 2, -1e5, 1e5), "need": n(tt.get("need"), 2, 0, 1e5)}
    cf = a.get("cfg")
    if isinstance(cf, dict):
        out["cfg"] = {"on": 1 if n(cf.get("on"), 0, 0, 1) else 0, "only": 1 if n(cf.get("only"), 0, 0, 1) else 0,
                      "w": int(n(cf.get("w"), 0, 3, 50)), "k": int(n(cf.get("k"), 0, 2, 50)), "mv": n(cf.get("mv"), 2, 0, 20),
                      "sec": int(n(cf.get("sec"), 0, 0, 300)), "lot": n(cf.get("lot"), 2, 0.01, 100), "risk": n(cf.get("risk"), 2, 0, 5),
                      "vsl": n(cf.get("vsl"), 2, 0.1, 100), "vtp": n(cf.get("vtp"), 2, 0, 100), "be": n(cf.get("be"), 2, 0, 100),
                      "bel": n(cf.get("bel"), 2, 0, 50), "trs": n(cf.get("trs"), 2, 0, 100), "trd": n(cf.get("trd"), 2, 0.05, 50),
                      "hard": n(cf.get("hard"), 2, 0.5, 200), "hold": int(n(cf.get("hold"), 0, 0, 86400)),
                      "hs": int(n(cf.get("hs"), 0, 0, 23)), "he": int(n(cf.get("he"), 0, 1, 24)), "spr": int(n(cf.get("spr"), 0, 5, 500)),
                      "cd": int(n(cf.get("cd"), 0, 0, 3600)), "maxd": int(n(cf.get("maxd"), 0, 1, 1000)), "ml": int(n(cf.get("ml"), 0, 0, 20)),
                      "pause": int(n(cf.get("pause"), 0, 1, 1440)), "dl": n(cf.get("dl"), 1, 0, 50), "dt": n(cf.get("dt"), 1, 0, 100),
                      "tpm": int(n(cf.get("tpm"), 0, 0, 1)) if "tpm" in cf else None,   # v12.15
                      "bon": (1 if n(cf.get("bon"), 0, 0, 1) else 0) if "bon" in cf else None,   # v12.16 (EA v70.4)
                      "tron": (1 if n(cf.get("tron"), 0, 0, 1) else 0) if "tron" in cf else None,   # v12.17 (EA v70.5): TP RAAC
                      "dron": (1 if n(cf.get("dron"), 0, 0, 1) else 0) if "dron" in cf else None,   # v12.19 (EA v70.7): JIHADA
                      "drm": int(n(cf.get("drm"), 0, 0, 2)), "drb": int(n(cf.get("drb"), 0, 3, 20)), "drn": int(n(cf.get("drn"), 0, 2, 20)),
                      "drs": n(cf.get("drs"), 0, 0, 100), "drw": int(n(cf.get("drw"), 0, 0, 240)),
                      "trst": n(cf.get("trst"), 0, 50, 95), "trlk": n(cf.get("trlk"), 0, 10, 90),
                      "trstep": n(cf.get("trstep"), 2, 0.1, 50), "trmax": int(n(cf.get("trmax"), 0, 1, 10)),
                      "bn": int(n(cf.get("bn"), 0, 2, 5)), "bent": int(n(cf.get("bent"), 0, 0, 1)), "bex": int(n(cf.get("bex"), 0, 0, 1)),
                      "btgt": n(cf.get("btgt"), 2, 0, 10000), "bbe": n(cf.get("bbe"), 2, 0, 10000), "blp": int(n(cf.get("blp"), 0, 0, 1000000)),
                      "slm": int(n(cf.get("slm"), 0, 0, 1)), "spm": n(cf.get("spm"), 1, 0, 10), "tr": 1 if n(cf.get("tr"), 0, 0, 1) else 0}   # v12.14
    if out["st"] == "OPEN":
        out.update({"dir": "SELL" if str(a.get("dir")) == "SELL" else "BUY",
                    "e": n(a.get("e"), dg, 0, 1e7), "vs": n(a.get("vs"), dg, 0, 1e7), "stg": int(n(a.get("stg"), 0, 0, 3)),
                    "fav": n(a.get("fav"), 2, -1e5, 1e5), "lot": n(a.get("lot"), 2, 0, 1e4), "pl": n(a.get("pl")),
                    "t0": int(n(a.get("t0"), 0, 0, 4e9)), "bsl": n(a.get("bsl"), dg, 0, 1e7), "btp": n(a.get("btp"), dg, 0, 1e7)})
        if "rc" in a:   # v12.17 (EA v70.5): TP RAAC - raac-yada · masaafada TP-ga hadda ($)
            out.update({"rc": int(n(a.get("rc"), 0, 0, 10)), "tpd": n(a.get("tpd"), 2, 0, 1000)})
    sq = str(a.get("seq") or "")[:50]                                   # v12.13 (EA v70.1)
    out["seq"] = "".join(ch for ch in sq if ch in "UDN")
    out["srv"] = int(n(a.get("srv"), 0, 0, 4e9))
    out["sp"] = n(a.get("sp"), 2, 0, 1000)
    out["lotp"] = n(a.get("lotp"), 2, 0, 1e4)
    hist = []
    for h in (a.get("hist") or [])[:10]:
        if not isinstance(h, dict):
            continue
        hist.append({"t": int(n(h.get("t"), 0, 0, 4e9)), "d": 1 if n(h.get("d"), 0, -1, 1) > 0 else -1,
                     "x": int(n(h.get("x"), 0, 0, 12)), "pl": n(h.get("pl"))})
        if "k" in h:   # v12.14 (EA v70.2): peak · slippage · spread · SL broker
            hist[-1].update({"k": n(h.get("k"), 2, -1e5, 1e5), "s": n(h.get("s"), 2, -1e5, 1e5), "sp": n(h.get("sp"), 2, 0, 1e5),
                             "b": int(n(h.get("b"), 0, 0, 2))})   # 1 = SL broker · 2 = TP broker (v12.15)
        hist[-1]["n"] = int(n(h.get("n"), 0, 0, 5))   # v12.16: lakabyada basket-ka (0 = hal trade)
    out["hist"] = hist
    jh = a.get("jh")   # v12.19 (EA v70.7): 🧭 JIHADA SUUQA
    if isinstance(jh, dict):
        vv = jh.get("v") if isinstance(jh.get("v"), list) else []
        out["jh"] = {"on": 1 if n(jh.get("on"), 0, 0, 1) else 0, "d": int(n(jh.get("d"), 0, -1, 1)), "s": n(jh.get("s"), 0, -100, 100),
                     "v": [int(n(x, 0, -1, 1)) for x in vv[:4]], "cu": int(n(jh.get("cu"), 0, 0, 50)), "cd": int(n(jh.get("cd"), 0, 0, 50)),
                     "nb": int(n(jh.get("nb"), 0, 0, 50)), "mv": n(jh.get("mv"), 2, -1e5, 1e5), "t0": int(n(jh.get("t0"), 0, 0, 4e9)),
                     "rj": str(jh.get("rj") or "")[:200], "rn": int(n(jh.get("rn"), 0, 0, 1e6)),
                     "bd": int(n(jh.get("bd"), 0, -1, 1)), "bt": int(n(jh.get("bt"), 0, 0, 4e9))}
    bk = a.get("bk")   # v12.16 (EA v70.4): basket-yada maanta + basket socda
    if isinstance(bk, dict):
        out["bk"] = {"n": int(n(bk.get("n"), 0, 0, 100000)), "hit": int(n(bk.get("hit"), 0, 0, 100000)), "pl": n(bk.get("pl"))}
    bt = a.get("bkt")
    if out["st"] == "OPEN" and isinstance(bt, dict):
        lay = []
        for L in (bt.get("lay") or [])[:5]:
            if isinstance(L, dict):
                lay.append({"e": n(L.get("e"), dg, 0, 1e7), "lot": n(L.get("lot"), 2, 0, 1e4), "pl": n(L.get("pl")), "t": int(n(L.get("t"), 0, 0, 4e9)),
                            "rc": int(n(L.get("rc"), 0, 0, 10))})   # v12.17: TP RAAC lakabka
        out["bkt"] = {"dir": "SELL" if str(bt.get("dir")) == "SELL" else "BUY", "pl": n(bt.get("pl")), "pk": n(bt.get("pk")),
                      "sl": n(bt.get("sl"), dg, 0, 1e7), "be": 1 if n(bt.get("be"), 0, 0, 1) else 0, "t0": int(n(bt.get("t0"), 0, 0, 4e9)), "lay": lay}
    cb = a.get("cb")   # v12.24 (EA v70.9): 📏 CABBIR LAMAANE (sitinka)
    if isinstance(cb, dict):
        ref = str(cb.get("ref") or "")[:24]
        out["cb"] = {"on": 1 if n(cb.get("on"), 0, 0, 1) else 0, "tf": int(n(cb.get("tf"), 0, 0, 2)), "n": int(n(cb.get("n"), 0, 20, 2000)),
                     "sq": int(n(cb.get("sq"), 0, 0, 100)), "f": n(cb.get("f"), 10, -1, 100000), "ok": 1 if n(cb.get("ok"), 0, 0, 1) else 0,
                     "ref": ref if _PR_SYM.match(ref) else "", "sy": str(cb.get("sy") or "")[:120]}
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("tick:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


def _save_inp(con, acc, a, d):
    """v12.18 (EA v70.6): {"h": hash, "v": [qiimaha hadda], "b": [MT5 input]} -> kv inp:<acc>."""
    if not isinstance(a, dict):
        return
    v, b = a.get("v"), a.get("b")
    if not isinstance(v, list) or not isinstance(b, list) or len(v) != len(b) or len(v) > 1200:
        return
    ok = lambda x: isinstance(x, (int, float, bool)) or (isinstance(x, str) and len(x) <= 210)
    if not all(ok(x) for x in v) or not all(ok(x) for x in b):
        return
    out = {"h": str(a.get("h") or "")[:16], "v": v, "b": b, "ts": time.time(),
           "ver": str(d.get("ver") or "")[:12], "chart": str(d.get("chart") or "")[:40]}
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("inp:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


def _save_zn(con, acc, z):
    """v12.22 (EA v70.8): 🎯 ZONE YAR - chart-ka dahabka -> kv zn:<acc>."""
    if not isinstance(z, dict) or not z.get("gold"):
        return
    n = _bsk_num
    dg = int(n(z.get("dg"), 0, 0, 8))
    zs = []
    for q in (z.get("z") or [])[:10]:
        if isinstance(q, list) and len(q) >= 3:
            lo, hi = n(q[0], dg, 0, 1e7), n(q[1], dg, 0, 1e7)
            if 0 < lo <= hi:
                zs.append([lo, hi, int(n(q[2], 0, 0, 999))])
    out = {"sym": str(z.get("sym") or "")[:16], "on": 1 if z.get("on") else 0, "tf": int(n(z.get("tf"), 0, 0, 2)),
           "sh": 1 if z.get("sh") else 0, "t": int(n(z.get("t"), 0, 2, 6)), "w": n(z.get("w"), 2, 0.05, 1),
           "mx": int(n(z.get("mx"), 0, 1, 10)), "sl": n(z.get("sl"), 2, 0.1, 20), "tp": int(n(z.get("tp"), 0, 0, 1)),
           "z": zs, "at": int(n(z.get("at"), 0, -1, 9)), "st": int(n(z.get("st"), 0, 0, 4)), "jd": int(n(z.get("jd"), 0, -1, 1)),
           "px": n(z.get("px"), dg, 0, 1e7), "dg": dg, "atr": n(z.get("atr"), 2, 0, 1e5),
           "rj": str(z.get("rj") or "")[:200], "rn": int(n(z.get("rn"), 0, 0, 1e6)), "tk": 1 if z.get("tk") else 0, "ts": time.time()}
    if out["at"] >= len(zs):
        out["at"] = -1
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("zn:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


_PR_SYM = re.compile(r"^[A-Za-z0-9._#+\-]{1,24}$")


def _save_grid(con, acc, g):
    """v12.25 (EA v71.0): 🪜 GRID STOP - xaaladda -> kv grid:<acc> · grid-yada dhammaaday -> kv grid_h:<acc> (1000 ugu dambeeya)."""
    if not isinstance(g, dict):
        return
    n = _bsk_num
    dg = int(n(g.get("dg"), 0, 0, 8))
    st = str(g.get("st") or "")
    sym = str(g.get("sym") or "")
    out = {"on": 1 if g.get("on") else 0, "only": 1 if g.get("only") else 0, "st": st if st in ("OFF", "WAIT", "ARMED", "RUN") else "WAIT",
           "why": str(g.get("why") or "")[:160], "sym": sym if _PR_SYM.match(sym) else "", "dir": "SELL" if g.get("dir") == "SELL" else ("BUY" if g.get("dir") == "BUY" else ""),
           "n": int(n(g.get("n"), 0, 0, 100)), "c": int(n(g.get("c"), 0, 0, 100)), "lv": int(n(g.get("lv"), 0, 0, 100)), "dg": dg,
           "step": n(g.get("step"), dg, 0, 1e7), "a": n(g.get("a"), dg, 0, 1e9), "fl": n(g.get("fl")), "pk": n(g.get("pk")), "lk": n(g.get("lk"), 2, 0, 1e9),
           "tp": n(g.get("tp"), 2, 0, 1e9), "sl": n(g.get("sl"), 2, 0, 1e9), "t0": int(n(g.get("t0"), 0, 0, 4e9)), "js": n(g.get("js"), 0, -100, 100),
           "srv": int(n(g.get("srv"), 0, 0, 4e9)), "ts": time.time()}
    dy = g.get("day") if isinstance(g.get("day"), dict) else {}
    out["day"] = {"n": int(n(dy.get("n"), 0, 0, 1e6)), "w": int(n(dy.get("w"), 0, 0, 1e6)), "pl": n(dy.get("pl"))}
    rj = g.get("rj") if isinstance(g.get("rj"), list) else []
    out["rj"] = [int(n(x, 0, 0, 1e9)) for x in rj[:4]]
    cf = g.get("cfg")
    if isinstance(cf, dict):
        out["cfg"] = {k: n(cf.get(k), 2, lo, hi) for k, (lo, hi) in {
            "on": (0, 1), "only": (0, 1), "dir": (0, 2), "str": (0, 100), "step": (0, 1000), "satr": (0.05, 1), "spx": (1, 10), "lv": (2, 30),
            "lot": (0.01, 100), "tp": (0, 100000), "lk": (0, 100000), "lkp": (10, 90), "sl": (0.1, 5), "hl": (2, 30), "cd": (0, 1440), "md": (1, 50),
            "dl": (0, 50), "nw": (0, 1), "hs": (0, 23), "he": (1, 24), "mm": (0, 10080)}.items()}
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("grid:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))
    hs = [h for h in (g.get("hist") or [])[:20] if isinstance(h, dict)]
    if not hs:
        return
    old = con.execute("SELECT v FROM kv WHERE k=?", ("grid_h:" + acc,)).fetchone()
    lst = (_jload(old["v"]) if old else {}).get("l") or []
    if not isinstance(lst, list):
        lst = []
    seen = {int(r.get("t") or 0) for r in lst if isinstance(r, dict)}
    add = 0
    for h in hs:
        t = int(n(h.get("t"), 0, 0, 4e9))
        if t <= 0 or t in seen:
            continue
        seen.add(t); add += 1
        lst.append({"t": t, "d": 1 if n(h.get("d"), 0, -1, 1) > 0 else -1, "n": int(n(h.get("n"), 0, 0, 100)), "m": int(n(h.get("m"), 0, 0, 1e6)),
                    "x": int(n(h.get("x"), 0, 0, 9)), "pl": n(h.get("pl"))})
    if add:
        lst = sorted(lst, key=lambda r: r["t"])[-1000:]
        con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    ("grid_h:" + acc, json.dumps({"l": lst}, separators=(",", ":"), ensure_ascii=False)))


def _save_pairs(con, acc, p):
    """v12.22 (EA v70.8): 💱 LAMAANAHA - bot-ka 👑 -> kv pairs:<acc> (liiska broker-ka 'av' 15 daq kasta)."""
    if not isinstance(p, dict):
        return
    n = _bsk_num
    old = con.execute("SELECT v FROM kv WHERE k=?", ("pairs:" + acc,)).fetchone()
    oldv = _jload(old["v"]) if old else {}
    lst = []
    for e in (p.get("l") or [])[:9]:
        if not isinstance(e, dict) or not _PR_SYM.match(str(e.get("s") or "")):
            continue
        dg = int(n(e.get("dg"), 0, 0, 8))
        lst.append({"s": str(e["s"]), "on": 1 if e.get("on") else 0, "mm": 1 if e.get("mm") else 0,
                    "tf": 1 if int(n(e.get("tf"), 0, 0, 60)) == 1 else 5, "st": int(n(e.get("st"), 0, 0, 2)),
                    "rk": n(e.get("rk"), 2, 0, 5), "x": int(n(e.get("x"), 0, 0, 4)), "n": int(n(e.get("n"), 0, 0, 1000)),
                    "pl": n(e.get("pl"), 2, -1e9, 1e9), "sp": n(e.get("sp"), dg, 0, 1e6), "dg": dg, "e": str(e.get("e") or "")[:120]})
        if "cf" in e:                                    # v12.24 (EA v70.9): 📏 cabbir · TICK / BASKET
            lst[-1].update({"cf": n(e.get("cf"), 10, -1, 100000), "cs": int(n(e.get("cs"), 0, -1, 12)), "cq": int(n(e.get("cq"), 0, 0, 9999))})
        if "fl" in e:
            lst[-1].update({"fl": int(n(e.get("fl"), 0, -1, 3)), "cm": n(e.get("cm"), 8, -1, 100000)})
    av = p.get("av")
    if isinstance(av, list):
        av = [str(x) for x in av[:1500] if _PR_SYM.match(str(x))]
    else:
        av = (oldv or {}).get("av") or []
    m = str(p.get("m") or "")
    out = {"m": m if _PR_SYM.match(m) else "", "on": 1 if p.get("on") else 0, "mx": int(n(p.get("mx"), 0, 1, 8)),
           "got": 1 if p.get("got") else 0, "tpl": str(p.get("tpl") or "")[:60], "l": lst, "av": av, "ts": time.time()}
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("pairs:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


def _save_asia(con, acc, d):
    """v12.9 (EA v69.7): ASIA BREAKOUT - chart-ka dahabka ayaa diraa -> kv (chart-yada kale ma tirtiraan)."""
    a = d.get("asia")
    if not isinstance(a, dict) or not a.get("gold"):
        return
    dg = int(_bsk_num(a.get("dg"), 0, 0, 8))
    st = str(a.get("st") or "")
    out = {"sym": str(a.get("sym") or "")[:16], "on": bool(a.get("on")),
           "st": st if st in ("OFF", "RANGE", "WAIT", "OPEN", "DONE", "SKIP") else "WAIT",
           "why": str(a.get("why") or "")[:160], "day": int(_bsk_num(a.get("day"), 0, 0, 99999999)), "dg": dg,
           "hi": _bsk_num(a.get("hi"), dg, 0, 1e7), "lo": _bsk_num(a.get("lo"), dg, 0, 1e7), "px": _bsk_num(a.get("px"), dg, 0, 1e7),
           "tr": int(_bsk_num(a.get("tr"), 0, -1, 1)), "rs": int(_bsk_num(a.get("rs"), 0, 0, 23)), "re": int(_bsk_num(a.get("re"), 0, 0, 23)),
           "we": int(_bsk_num(a.get("we"), 0, 0, 23)), "cl": int(_bsk_num(a.get("cl"), 0, 0, 23)),
           "bal": _bsk_num(a.get("bal"), 2, 0, 1e10), "vpp": _bsk_num(a.get("vpp"), 2, 0, 1e7), "mn": _bsk_num(a.get("mn"), 2, 0, 100),
           "ts": time.time()}
    cf = a.get("cfg")
    if isinstance(cf, dict):
        out["cfg"] = {"on": 1 if _bsk_num(cf.get("on"), 0, 0, 1) else 0, "dir": int(_bsk_num(cf.get("dir"), 0, 0, 2)),
                      "tr": 1 if _bsk_num(cf.get("tr"), 0, 0, 1) else 0, "maxr": int(_bsk_num(cf.get("maxr"), 0, 0, 1000)),
                      "sl": int(_bsk_num(cf.get("sl"), 0, 0, 1)), "tp": _bsk_num(cf.get("tp"), 1, 0.5, 5),
                      "risk": _bsk_num(cf.get("risk"), 2, 0.05, 5), "max": int(_bsk_num(cf.get("max"), 0, 0, 100000)),
                      "l": int(_bsk_num(cf.get("l"), 0, 1, 3)), "we": int(_bsk_num(cf.get("we"), 0, 8, 20)),
                      "cl": int(_bsk_num(cf.get("cl"), 0, 13, 23)),
                      "g": 1 if _bsk_num(cf.get("g"), 0, 0, 1) else 0, "gm": int(_bsk_num(cf.get("gm"), 0, 0, 1)),
                      "gl": int(_bsk_num(cf.get("gl"), 0, 2, 5)), "gs": _bsk_num(cf.get("gs"), 2, 0.15, 0.33),
                      "gx": _bsk_num(cf.get("gx"), 2, 1, 2)}
    if out["st"] == "OPEN":
        out.update({"dir": "SELL" if str(a.get("dir")) == "SELL" else "BUY",
                    "e": _bsk_num(a.get("e"), dg, 0, 1e7), "sl": _bsk_num(a.get("sl"), dg, 0, 1e7), "tp": _bsk_num(a.get("tp"), dg, 0, 1e7),
                    "lot": _bsk_num(a.get("lot"), 2, 0, 1e4), "n": int(_bsk_num(a.get("n"), 0, 0, 5)),
                    "fill": int(_bsk_num(a.get("fill"), 0, 0, 5)), "open": int(_bsk_num(a.get("open"), 0, 0, 5)),
                    "lk": [_bsk_num(x, 2, 0, 1e4) for x in (a.get("lk") or [])[:5]],
                    "pl": _bsk_num(a.get("pl")), "risk": _bsk_num(a.get("risk")), "t0": int(_bsk_num(a.get("t0"), 0, 0, 4e9))})
    hist = []
    for h in (a.get("hist") or [])[:7]:
        if not isinstance(h, dict):
            continue
        hist.append({"d": int(_bsk_num(h.get("d"), 0, 0, 99999999)), "dir": int(_bsk_num(h.get("dir"), 0, -1, 1)),
                     "pl": _bsk_num(h.get("pl")), "r": _bsk_num(h.get("r"), 2, -50, 50), "x": int(_bsk_num(h.get("x"), 0, 0, 9))})
    out["hist"] = hist
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("asia:" + acc, json.dumps(out, separators=(",", ":"), ensure_ascii=False)))


def _save_analysis(con, acc, d):
    """v7: EA-du chart kasta wuxuu soo diraa analiiskiisa. Saf kasta = hal symbol."""
    a = d.get("analysis")
    if not isinstance(a, dict):
        return
    sym = str(a.get("sym") or d.get("symbol") or "")[:16]
    if not sym:
        return
    news = d.get("news")
    if not isinstance(news, list):
        news = []
    now = time.time()
    try:
        js = json.dumps(a, ensure_ascii=False)[:4000]
        nj = json.dumps(news[:8], ensure_ascii=False)[:3000]
    except (TypeError, ValueError):
        return
    con.execute(
        "INSERT INTO analysis(account,sym,data,news,ts) VALUES(?,?,?,?,?)"
        " ON CONFLICT(account, sym) DO UPDATE SET data=excluded.data,"
        " news=excluded.news, ts=excluded.ts",
        (acc, sym, js, nj, now))
    if _should_prune("an:" + acc):
        con.execute("DELETE FROM analysis WHERE account=? AND ts < ?",
                    (acc, now - 6 * 3600))


@app.post("/update")
def ea_update():
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    d = _ea_body()                                         # v12.8.1: UTF-8 / cp1252
    if getattr(g, "_ebad", False):
        return _ea_bad_body()
    d.pop("token", None)

    acc = clean_account(d.get("account"))
    bot = str(d.get("bot", ""))[:64]
    if not acc:
        # EA hore (V58.x) oo aan account dirin -> magaca bot-ka ayaa fure
        acc = "bot-" + re.sub(r"[^A-Za-z0-9]", "", bot)[:16] or "bot-unknown"
    if not ea_bind(acc):                                   # v11
        return _bad_key()

    now = time.time()
    d["_server_ts"] = now
    inp = d.pop("inp", None)                             # v12.18 (EA v70.6): qiimaha input-yada -> kv gooni ah
    zn = d.pop("zn", None)                               # v12.22 (EA v70.8): 🎯 ZONE YAR
    prs = d.pop("pairs", None)                           # v12.22 (EA v70.8): 💱 LAMAANAHA (bot-ka 👑)
    grd = d.pop("grid", None)                            # v12.25 (EA v71.0): 🪜 GRID STOP
    with db() as con:
        _save_inp(con, acc, inp, d)
        _save_zn(con, acc, zn)
        _save_pairs(con, acc, prs)
        _save_grid(con, acc, grd)            # v12.25
        con.execute(
            "INSERT INTO snapshots(account,bot,data,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET bot=excluded.bot,"
            " data=excluded.data, updated_at=excluded.updated_at",
            (acc, bot, json.dumps(d, ensure_ascii=False), now))

        _save_closed(con, acc, d.get("trades"))
        _save_basket(con, acc, d)            # v12.7: GOLD BASKET (chart-ka dahabka oo keliya)
        _save_asia(con, acc, d)              # v12.9: ASIA BREAKOUT (chart-ka dahabka oo keliya)
        _save_tick(con, acc, d)              # v12.12: ⚡ TICK SCALPER (chart-ka dahabka oo keliya)
        _apply_cfgset(con, acc, d)           # v12.7.1: badhanka 🥇 BASKET ee chart-ka -> sitinka la kaydiyay
        _save_analysis(con, acc, d)          # v7: analiiska live (chart kasta = saf)
        _save_levels(con, acc, d)            # v12.3: heerarka & range

        last = con.execute("SELECT ts FROM history WHERE account=? ORDER BY ts DESC LIMIT 1",
                           (acc,)).fetchone()
        if last is None or now - last["ts"] >= HISTORY_EVERY:
            try:
                bal = float(d.get("balance") or 0)
                eq  = float(d.get("equity") or 0)
            except (TypeError, ValueError):
                bal = eq = 0.0
            if bal or eq:
                con.execute("INSERT INTO history(account,ts,balance,equity) VALUES(?,?,?,?)",
                            (acc, now, bal, eq))
                if _should_prune("hist:" + acc):      # v5.3: ma aha codsi kasta
                    con.execute(
                        "DELETE FROM history WHERE account=? AND id NOT IN"
                        " (SELECT id FROM history WHERE account=? ORDER BY ts DESC LIMIT ?)",
                        (acc, acc, HISTORY_CAP))
    return jsonify(ok=True, account=acc)


@app.post("/trades")
def ea_trades():
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    d = _ea_body()                                         # v12.8.1
    if getattr(g, "_ebad", False):
        return _ea_bad_body()
    acc = clean_account(d.get("account"))
    if not acc:
        acc = "bot-" + re.sub(r"[^A-Za-z0-9]", "", str(d.get("bot", "")))[:16] or "bot-unknown"
    if not ea_bind(acc):                                   # v11
        return _bad_key()
    rows = d.get("trades") or []
    n = 0
    now = time.time()
    with db() as con:
        _save_closed(con, acc, rows)
        batch = []
        seen = set()
        for t in rows[:200]:
            if not isinstance(t, dict):
                continue
            tk = str(t.get("ticket") or t.get("id") or "")
            if not tk or tk in seen:
                continue
            seen.add(tk)
            batch.append((acc, tk, json.dumps(t, ensure_ascii=False), now))
        if batch:
            # HAL statement, ma aha mid mid (eeg insert_many_ignore)
            n = con.insert_many_ignore("closed_trades",
                                       ("account", "ticket", "data", "ts"),
                                       batch, conflict="(account, ticket)")
        # v5.3 DHAKHSO: nadiifintu waa culus tahay (sub-SELECT + ORDER BY safka oo dhan).
        # Hore codsi KASTA ayay ku socotay -> EA-du 20s+ ayuu sugayay (err 5203).
        # Hadda: 30 codsi mar (ama marka safku aad u buuxo).
        if _should_prune("ct:" + acc):
            con.execute(
                "DELETE FROM closed_trades WHERE account=? AND id NOT IN"
                " (SELECT id FROM closed_trades WHERE account=? ORDER BY ts DESC LIMIT 500)",
                (acc, acc))
    return jsonify(ok=True, saved=n)


CMD_TTL       = int(os.environ.get("CMD_TTL", "600"))        # START/STOP/SET: 10 daqiiqo
CMD_TTL_CLOSE = int(os.environ.get("CMD_TTL_CLOSE", "120"))  # CLOSE_*: 2 daqiiqo oo keliya


def _collapse_cmds(cmds):
    """v8.3: EA-du amarrada si go'an ayay u akhridaa (START ka hor STOP; SET-ka kii UGU HORREEYA).
    Haddii hal jawaab ku jiraan STOP kadib START, ama SET:SL=30 kadib SET:SL=50, kan DAMBE ha guulaysto:
    fure kasta kan ugu dambeeya oo keliya ayaa la diraa."""
    last = {}
    for i, c in enumerate(cmds):
        c = str(c)
        if c in ("START", "STOP"):
            key = "RUN"
        elif c.startswith("SET:INDEL:"):                  # v12.18: IN / INDEL isla input -> kan dambe
            key = "SET:IN:" + c[10:]
        elif c.startswith("SET:") and "=" in c:
            key = c.split("=", 1)[0]
        elif c.startswith("STRATEGY:"):
            key = "STRATEGY"
        else:
            key = "%s#%d" % (c, i)
        last[key] = (i, c)
    return [c for _, c in sorted(last.values())]


# --------------------------------------------------------------------------
# v10: SITINKA BOT-KA (.set la'aan) — app-ka ayaa kaydiya, server-ka ayaa hayaa.
#  POST /api/config        (app)  -> kaydi + chart kasta u dir (SET:... + SET:REV=n)
#  GET  /api/config        (app)  -> sitinka la kaydiyay + rev
#  GET  /api/ea_config     (EA)   -> marka bot-ku kaco: sitinka kaydsan (qaab amar ah)
# --------------------------------------------------------------------------
CFG_KEYS = ("RISK", "DLOSS", "MAXDD", "SNIPER", "SNDAY", "SNRR", "SNSLMAX", "NEWS",
            "SLTP", "SL", "TP", "LOT", "STEPON", "STEP", "STEPSTART", "BE", "LOCKMODE", "ADAPT", "MGMT",
            "STRAT", "EMAF", "STARS", "LOT2", "PROT", "SDPROF") + BSK_KEYS + ASIA_KEYS + TK_KEYS + ZN_KEYS + CB_KEYS + GR_KEYS + PR_KEYS   # v12.6 · v12.7 GOLD BASKET · v12.8 SDPROF · v12.9 ASIA · v12.12 TICK


def _cfg_cmds(vals, rev):
    out = []
    for k in CFG_KEYS:
        if k in vals:
            ok, c = valid_command("SET:%s=%s" % (k, vals[k]))
            if ok:
                out.append(c)
    for k in sorted(vals):                                # v12.18: ⚙️ INPUT
        if str(k).startswith("IN:"):
            ok, c = valid_command("SET:%s=%s" % (k, vals[k]))
            if ok:
                out.append(c)
    out.append("SET:REV=%d" % int(rev))
    return out


@app.get("/api/config")
@login_required
def api_config_get():
    u = request.user
    acc = _visible_account(u)
    with db() as con:
        r = con.execute("SELECT data,rev,updated_at FROM ea_config WHERE account=?", (acc,)).fetchone()
    if not r:
        return jsonify(ok=True, account=acc, values={}, rev=0, saved=None)
    try:
        vals = json.loads(r["data"])
    except ValueError:
        vals = {}
    return jsonify(ok=True, account=acc, values=vals, rev=int(r["rev"]), saved=float(r["updated_at"]))


@app.post("/api/config")
@login_required
def api_config_save():
    u = request.user
    body = request.get_json(silent=True) or {}
    acc = clean_account(body.get("account")) if u["role"] == "admin" else u["account"]
    acc = acc or u["account"]
    lic = _user_lic(u, acc)                                   # v12
    if lic is None and not u["can_control"] and u["role"] != "admin":
        return jsonify(ok=False, error="Amar diritaanka lagaama ogola."), 403
    if lic is not None and lic["state"] != "OK":
        return jsonify(ok=False, error="Laysinkaagu wuu dhacay - admin-ka la xiriir."), 403
    now = time.time()
    if body.get("reset"):
        if lic is not None:
            return jsonify(ok=False, error="Celinta sitinka admin-ka ayaa leh."), 403
        with db() as con:
            lr = _lic_row(con, acc)
            if lr is not None:                                # v12: macmiil -> master-ka ku celi
                con.execute("UPDATE licenses SET ovr='{}', updated_at=? WHERE account=?", (now, acc))
                rev, _ = _lic_apply(con, acc, u["account"], force=True)
                return jsonify(ok=True, account=acc, rev=rev, reset=True)
            con.execute("DELETE FROM ea_config WHERE account=?", (acc,))
            con.execute("INSERT INTO commands(account,cmd,by_account,created_at) VALUES(?,?,?,?)",
                        (acc, "SET:RESET", u["account"], now))
            if acc == _master(con):
                _propagate_master(con, u["account"])
        return jsonify(ok=True, account=acc, rev=0, reset=True)
    raw = body.get("values") or {}
    if not isinstance(raw, dict):
        return jsonify(ok=False, error="values"), 400
    uns = [str(x) for x in (body.get("unset") or []) if isinstance(x, str) and x.startswith("IN:") and x[3:] in INP_GEN][:600]   # v12.18
    clean, bad = {}, []
    for k, v in raw.items():
        if str(k).startswith("IN:"):                      # v12.18: ⚙️ INPUT (magaca sidiisa)
            vv = valid_in(str(k)[3:], v)
            if vv is None:
                bad.append(str(k)); continue
            clean[str(k)] = vv; continue
        k = str(k).upper()
        if k not in CFG_KEYS:
            bad.append(k); continue
        ok, c = valid_command("SET:%s=%s" % (k, v))
        if not ok:
            bad.append(k); continue
        clean[k] = c.split("=", 1)[1]
    if bad:
        return jsonify(ok=False, error="Qiime khaldan: " + ", ".join(bad)), 400
    if lic is not None:                                       # v12: macmiil -> waxa loo oggol yahay oo keliya
        p = lic["perms"]
        keep = {k: v for k, v in clean.items() if PERM_OF.get(k) and p.get(PERM_OF[k])}
        if "RISK" in keep and not (p["lo"] - 1e-9 <= _num(keep["RISK"]) <= p["hi"] + 1e-9):
            return jsonify(ok=False, error="Risk-ku waa inuu u dhexeeyaa %.2f – %.2f%%." % (p["lo"], p["hi"])), 400
        if not keep:
            return jsonify(ok=False, error="Sitinkan admin-ka ayaa maamula."), 403
        clean = keep
        uns = []
    with db() as con:
        lr = _lic_row(con, acc)
        if lr is not None:                                    # v12: macmiil (admin ama isaga) -> ovr
            ovr = _jload(lr["ovr"])
            side = "c" if lic is not None else "a"
            part = dict(ovr.get(side) or {}); part.update(clean)
            for k in uns:                                 # v12.18
                part.pop(k, None)
            ovr[side] = part
            if side == "a":                                   # admin-ku wuu ka adkaadaa macmiilka
                ovr["c"] = {k: v for k, v in (ovr.get("c") or {}).items() if k not in clean}
            con.execute("UPDATE licenses SET ovr=?, updated_at=? WHERE account=?", (json.dumps(ovr), now, acc))
            rev, n = _lic_apply(con, acc, u["account"], force=True)
            vals, _ = _cfg_row(con, acc)
            return jsonify(ok=True, account=acc, rev=rev, values=vals, sent=n,
                           ignored=sorted(set(raw) - set(clean)) if lic is not None else [])
        r = con.execute("SELECT data,rev FROM ea_config WHERE account=?", (acc,)).fetchone()
        merged = {}
        if r:
            try:
                merged = json.loads(r["data"]) or {}
            except ValueError:
                merged = {}
        merged.update(clean)
        for k in uns:                                     # v12.18: ↺ MT5-ka ku celi
            merged.pop(k, None)
        rev = int(r["rev"]) + 1 if r else 1
        con.execute(
            "INSERT INTO ea_config(account,data,rev,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET data=excluded.data, rev=excluded.rev, updated_at=excluded.updated_at",
            (acc, json.dumps(merged), rev, now))
        cmds = ["SET:INDEL:" + k[3:] for k in uns] + _cfg_cmds(clean, rev)   # v12.18
        con.insert_many_ignore("commands", ("account", "cmd", "by_account", "created_at"),
                               [(acc, c, u["account"], now) for c in cmds])
        nf = _propagate_master(con, u["account"]) if acc == _master(con) else 0   # v12
    return jsonify(ok=True, account=acc, rev=rev, values=merged, sent=len(cmds), followers=nf)


def _ea_lic(acc):
    """v12: EA-da u sheeg xaaladda laysinka. NONE = account caadi ah (macmiil maaha)."""
    with db() as con:
        li = _lic_info(con, acc)
    if li is None:
        return {"lic": "NONE", "lic_d": 0}
    return {"lic": li["state"], "lic_d": li["days"]}


@app.get("/api/ea_config")
def ea_config():
    """EA-du marka ay kacdo (MT5 / VPS dib u kicin) sitinka kaydsan ayay halkan ka qaadataa."""
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    acc = clean_account(request.args.get("account"))
    if not ea_bind(acc):                                   # v11
        return _bad_key()
    cmds = []
    if acc:
        with db() as con:
            r = con.execute("SELECT data,rev FROM ea_config WHERE account=?", (acc,)).fetchone()
        if r:
            try:
                cmds = _cfg_cmds(json.loads(r["data"]) or {}, int(r["rev"]))
            except ValueError:
                cmds = []
    payload = {"token": _presented_token(), "account": acc, "commands": cmds, "ts": int(time.time())}
    payload.update(_ea_lic(acc))                              # v12
    resp = make_response(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/api/commands")
def ea_commands():
    """EA-du waxay soo qaadataa amarrada sugaya. Mid kasta hal mar oo keliya."""
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    acc = clean_account(request.args.get("account"))
    bot = request.args.get("bot", "")
    if not acc:
        acc = "bot-" + re.sub(r"[^A-Za-z0-9]", "", bot)[:16] or "bot-unknown"
    if not ea_bind(acc):                                   # v11
        return _bad_key()
    now = time.time()
    chart = re.sub(r"[^A-Za-z0-9_.#-]", "", request.args.get("chart", ""))[:40]
    with db() as con:
        if chart:
            # v8.3: CHART KASTA amarka wuu helayaa (hore kii ugu horreeyay ee weydiiya ayaa
            # "cunay" -> STOP/SET waxay gaadhi jireen hal chart oo keliya, 9-ka kale ma aysan maqlin).
            rows = con.execute(
                "SELECT id,cmd,created_at FROM commands WHERE account=? AND created_at>?"
                " AND id NOT IN (SELECT cmd_id FROM cmd_seen WHERE account=? AND chart=?)"
                " ORDER BY id ASC LIMIT 20",
                (acc, now - CMD_TTL, acc, chart)).fetchall()
            # CLOSE_* waa khatar in chart dib u kacay uu fuliyo -> daaqad gaaban
            rows = [r for r in rows if not (str(r["cmd"]).startswith("CLOSE_")
                                             and now - float(r["created_at"]) > CMD_TTL_CLOSE)]
            if rows:
                ids = [r["id"] for r in rows]
                con.insert_many_ignore("cmd_seen", ("cmd_id", "account", "chart", "ts"),
                                       [(i, acc, chart, now) for i in ids],
                                       conflict="(cmd_id, chart)")
                con.execute("UPDATE commands SET taken_at=? WHERE taken_at IS NULL AND id IN (%s)"
                            % ",".join("?" * len(ids)), [now] + ids)
            if _should_prune("cs:" + acc):
                con.execute("DELETE FROM cmd_seen WHERE account=? AND ts<?", (acc, now - 86400))
        else:
            # EA hore (v67.0 iyo ka hor): sidii hore - hal mar oo keliya
            rows = con.execute(
                "SELECT id,cmd FROM commands WHERE account=? AND taken_at IS NULL"
                " ORDER BY id ASC LIMIT 10", (acc,)).fetchall()
            if rows:
                con.execute("UPDATE commands SET taken_at=? WHERE id IN (%s)"
                            % ",".join("?" * len(rows)),
                            [now] + [r["id"] for r in rows])
    payload = {"token": _presented_token(), "account": acc,
               "commands": _collapse_cmds([r["cmd"] for r in rows]), "ts": int(now)}
    payload.update(_ea_lic(acc))                              # v12
    resp = make_response(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    """Cilad-raadin. Furayaal ma soo bandhigayo - kaliya HAA/MAYA iyo tirooyin."""
    try:
        with db() as con:
            nusers = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            admins = con.execute(
                "SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
            nsnap  = con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        db_ok, db_err = True, None
    except Exception as e:                                   # noqa: BLE001
        nusers, admins, nsnap, db_ok, db_err = 0, 0, 0, False, str(e)[:120]

    problems, warnings = [], []
    if not ADMIN_ACCOUNT:
        problems.append("ADMIN_ACCOUNT lama dejin (ama xaraf ma aha lambar).")
    if not ADMIN_PASSWORD:
        problems.append("ADMIN_PASSWORD lama dejin.")
    elif len(ADMIN_PASSWORD) < 8:
        problems.append("ADMIN_PASSWORD wuu ka gaaban yahay 8 xaraf.")
    if not CLOUD_TOKEN or len(CLOUD_TOKEN) < 8:
        problems.append("CLOUD_TOKEN lama dejin ama wuu gaaban yahay -> EA xog dirayo ma jiro.")
    if not os.environ.get("SECRET_KEY"):
        problems.append("SECRET_KEY lama dejin -> galitaanku wuu ba'ayaa restart kasta.")
    if not db_ok:
        problems.append("Database qalad: " + str(db_err))
    if not _DB_READY:
        problems.append("Database weli lama dhisin: " + (_DB_LAST_ERR or "?"))
    if ADMIN_ACCOUNT and nusers == 0:
        problems.append("Isticmaale lama abuurin. Dib u deploy gareey.")
    if not (USE_PG or DB_PATH.startswith("/var/data")):
        warnings.append("Kayd joogto ah ma jiro -> jirnalka, sawirka iyo isticmaalayaasha "
                        "way tirtirmayaan restart kasta. Ku dar DATABASE_URL (Postgres).")
    if DATABASE_URL and not PG_DRIVER:
        problems.append("DATABASE_URL waa la dejiyay laakiin darawal Postgres lama helin -> "
                        "ku dar 'pg8000' requirements.txt.")

    return jsonify(
        ok=(not problems),
        db_ready=_DB_READY,
        db_error=(_DB_LAST_ERR or None),
        db_init_tries=_DB_TRIES,
        db_circuit=("open" if not _cb_ok() else "closed"),
        db_circuit_trips=_cb_trips,
        db_recent_fails=_cb_fails,
        db_engine=("postgres" if USE_PG else "sqlite"),
        db_driver=(PG_DRIVER if USE_PG else "sqlite3"),
        db_path=("(postgres)" if USE_PG else DB_PATH),
        db_persistent=bool(USE_PG or DB_PATH.startswith("/var/data")),
        admin_account_set=bool(ADMIN_ACCOUNT),
        admin_account=(ADMIN_ACCOUNT[:3] + "***" + ADMIN_ACCOUNT[-2:]) if len(ADMIN_ACCOUNT) > 5 else ("set" if ADMIN_ACCOUNT else ""),
        admin_password_set=bool(ADMIN_PASSWORD),
        admin_password_len=len(ADMIN_PASSWORD),
        secret_key_set=bool(os.environ.get("SECRET_KEY")),
        cloud_token_set=bool(CLOUD_TOKEN),
        cloud_token_len=len(CLOUD_TOKEN),
        users=nusers, admin_count=admins, accounts_with_data=nsnap,
        extra_users_set=bool(EXTRA_USERS),
        problems=problems, warnings=warnings, ts=int(time.time()))

# --------------------------------------------------------------------------
# Web auth
# --------------------------------------------------------------------------
@app.get("/")
def home():
    return redirect(url_for("dashboard") if session.get("acc") else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        acc = clean_account(request.form.get("account"))
        pw  = request.form.get("password", "")
        key = (acc, request.remote_addr or "?")
        if _throttled(key):
            err = "Isku dayo badan. Sug 10 daqiiqo."
        elif not acc or not pw:
            err = "Geli lambarka account-ka iyo password-ka."
        else:
            with db() as con:
                u = con.execute("SELECT * FROM users WHERE account=?", (acc,)).fetchone()
            if u is None:
                _fail(key)
                err = ("Account-kan lama diiwaangelin. Hubi lambarka, "
                       "ama fur /healthz si aad u hubiso habaynta server-ka.")
            elif not check_password_hash(u["pw"], pw):
                _fail(key)
                err = "Password khaldan."
            elif not u["approved"]:
                err = "Account-kaaga weli lama ansixin. Sug ogolaanshaha admin-ka."
            else:
                _fails.pop(key, None)
                session.clear()
                session.permanent = True
                session["acc"] = u["account"]
                with db() as con:
                    con.execute("UPDATE users SET last_login=? WHERE account=?",
                                (_now_iso(), u["account"]))
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
    return render_template_string(T_LOGIN, err=err)


@app.route("/register", methods=["GET", "POST"])
def register():
    err = ok = None
    if request.method == "POST":
        acc  = clean_account(request.form.get("account"))
        name = (request.form.get("name") or "").strip()[:60]
        pw   = request.form.get("password", "")
        pw2  = request.form.get("password2", "")
        if len(acc) < 4:
            err = "Lambarka account-ka MT5 waa khaldan yahay."
        elif len(pw) < 8:
            err = "Password-ku waa inuu ugu yaraan 8 xaraf noqdaa."
        elif pw != pw2:
            err = "Labada password isku mid ma aha."
        else:
            with db() as con:
                if con.execute("SELECT 1 FROM users WHERE account=?", (acc,)).fetchone():
                    err = "Account-kan hore ayaa loo diiwaangeliyay."
                else:
                    con.execute(
                        "INSERT INTO users(account,name,pw,role,approved,can_control,created_at)"
                        " VALUES(?,?,?,'user',0,1,?)",
                        (acc, name, generate_password_hash(pw), _now_iso()))
                    ok = ("Diiwaangelintu way guulaysatay. Admin-ku waa inuu ku ansixiyaa "
                          "ka hor inta aadan gali karin.")
    return render_template_string(T_REGISTER, err=err, ok=ok)


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Qofku password-kiisa isagu ha beddesho. pw_self=1 -> EXTRA_USERS ma tirtirayo."""
    u = request.user
    err = ok = None
    if request.method == "POST":
        cur  = request.form.get("current", "")
        new1 = request.form.get("password", "")
        new2 = request.form.get("password2", "")
        if not check_password_hash(u["pw"], cur):
            err = "Password-kaaga hadda waa khaldan yahay."
        elif len(new1) < 8:
            err = "Password-ka cusub waa inuu ugu yaraan 8 xaraf ahaadaa."
        elif new1 != new2:
            err = "Labada password ma is le'ekaan."
        elif new1 == cur:
            err = "Password-ka cusub waa inuu ka duwanaadaa kii hore."
        else:
            with db() as con:
                con.execute("UPDATE users SET pw=?, pw_self=1 WHERE account=?",
                            (generate_password_hash(new1), u["account"]))
            log.info("Password la beddelay: %s", u["account"])
            ok = "Password-ka waa la beddelay. Mar dambe kan cusub isticmaal."
    return render_template_string(T_PASSWORD, err=err, ok=ok, acc=u["account"])


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/dashboard")
@login_required
def dashboard():
    u = request.user
    accounts = []
    if u["role"] == "admin":
        with db() as con:
            accounts = [r["account"] for r in con.execute(
                "SELECT account FROM snapshots ORDER BY updated_at DESC").fetchall()]
    # v8.1: admin-ka account-kiisa xog ma laha (EA-du account kale ayay u dirtaa) ->
    # default-ku waa account-ka xogta ugu dambaysay leh, maaha "Xog lama helin".
    sel = u["account"] if (u["account"] in accounts or not accounts) else accounts[0]
    bkey = None
    if u["role"] != "admin":                                # v11: isticmaalaha furihiisa
        with db() as con:
            kr = _key_for(con, u["account"])
        if kr is not None:
            bkey = {"key": kr["bkey"], "active": bool(kr["active"])}
    lic = _user_lic(u, u["account"])                          # v12: macmiil
    return render_template_string(
        T_DASH, me=u["account"], sel=sel, name=u["name"] or u["account"],
        is_admin=(u["role"] == "admin"),
        can_control=(bool(u["can_control"]) or lic is not None), accounts=accounts, bkey=bkey,
        lic=lic, perms_json=json.dumps(lic["perms"] if lic else None))


_AN_ORDER = {"SIGNAL": 0, "IN": 1, "NEAR": 2, "NEWS": 3, "WAIT": 4, "NONE": 5}


def _an_dist(z):
    try:
        v = float(z.get("dist"))
        return v if v >= 0 else 1e9
    except (TypeError, ValueError):
        return 1e9


def _visible_account(u):
    """Admin: waxa uu dooran karo. User: kaliya account-kiisa."""
    if u["role"] == "admin":
        a = clean_account(request.args.get("account") or request.form.get("account"))
        return a or u["account"]
    return u["account"]


@app.get("/api/state")
@login_required
def api_state():
    u = request.user
    acc = _visible_account(u)
    now = time.time()
    with db() as con:
        snap = con.execute("SELECT * FROM snapshots WHERE account=?", (acc,)).fetchone()
        hist = con.execute(
            "SELECT ts,balance,equity FROM history WHERE account=? ORDER BY ts ASC",
            (acc,)).fetchall()
        closed = con.execute(
            "SELECT data FROM closed_trades WHERE account=? ORDER BY ts DESC LIMIT 30",
            (acc,)).fetchall()
        pend = con.execute(
            "SELECT cmd FROM commands WHERE account=? AND taken_at IS NULL ORDER BY id",
            (acc,)).fetchall()
        br = con.execute("SELECT img FROM branding WHERE account=?", (acc,)).fetchone()
        anr = con.execute(
            "SELECT sym,data,news,ts FROM analysis WHERE account=? AND ts>? ORDER BY sym",
            (acc, now - ANALYSIS_TTL)).fetchall()
        chat_unread = _chat_unread(con, u)     # v9
        cfgr = con.execute("SELECT rev,updated_at FROM ea_config WHERE account=?", (acc,)).fetchone()   # v10
        kseen = con.execute("SELECT last_seen FROM bot_keys WHERE account=?", (acc,)).fetchone()        # v11
        lic = _lic_info(con, acc, now)                                                                     # v12
        bskr = con.execute("SELECT v FROM kv WHERE k=?", ("bsk:" + acc,)).fetchone()                       # v12.7
        asr = con.execute("SELECT v FROM kv WHERE k=?", ("asia:" + acc,)).fetchone()                       # v12.9
        tkr = con.execute("SELECT v FROM kv WHERE k=?", ("tick:" + acc,)).fetchone()                       # v12.12
        inr = con.execute("SELECT v FROM kv WHERE k=?", ("inp:" + acc,)).fetchone()                        # v12.18
        znr = con.execute("SELECT v FROM kv WHERE k=?", ("zn:" + acc,)).fetchone()                         # v12.22
        prr = con.execute("SELECT v FROM kv WHERE k=?", ("pairs:" + acc,)).fetchone()                      # v12.22
        grr = con.execute("SELECT v FROM kv WHERE k=?", ("grid:" + acc,)).fetchone()                       # v12.25
        ghr = con.execute("SELECT v FROM kv WHERE k=?", ("grid_h:" + acc,)).fetchone()

    data = json.loads(snap["data"]) if snap else {}
    age = (now - snap["updated_at"]) if snap else None
    bsk = _jload(bskr["v"]) if bskr else None                 # v12.7: GOLD BASKET
    if isinstance(bsk, dict):
        bsk["age"] = int(now - float(bsk.get("ts") or 0))
        if bsk["age"] > ANALYSIS_TTL:
            bsk = None
    tick = _jload(tkr["v"]) if tkr else None                  # v12.12: ⚡ TICK SCALPER
    if isinstance(tick, dict):
        tick["age"] = int(now - float(tick.get("ts") or 0))
        if tick["age"] > ANALYSIS_TTL:
            tick = None
    asia = _jload(asr["v"]) if asr else None                  # v12.9: ASIA BREAKOUT
    if isinstance(asia, dict):
        asia["age"] = int(now - float(asia.get("ts") or 0))
        if asia["age"] > ANALYSIS_TTL:
            asia = None
    online = (age is not None and age < STALE_SECONDS)
    zn = _jload(znr["v"]) if znr else None                    # v12.22: 🎯 ZONE YAR
    if isinstance(zn, dict):
        zn["age"] = int(now - float(zn.get("ts") or 0))
        if zn["age"] > ANALYSIS_TTL:
            zn = None
    grd = _jload(grr["v"]) if grr else None                   # v12.25: 🪜 GRID STOP (taariikhda waa la hayaa)
    if isinstance(grd, dict):
        grd["age"] = int(now - float(grd.get("ts") or 0))
        gh = (_jload(ghr["v"]) if ghr else {}).get("l") or []
        grd["gh"] = gh if isinstance(gh, list) else []
    prs = _jload(prr["v"]) if prr else None                   # v12.22: 💱 LAMAANAHA (liiska waa la hayaa · age = 👑 offline)
    if isinstance(prs, dict):
        prs["age"] = int(now - float(prs.get("ts") or 0))
    inp = _jload(inr["v"]) if inr else None                   # v12.18: ⚙️ INPUT (qiimaha EA-ga)
    if isinstance(inp, dict):
        inp["age"] = int(now - float(inp.get("ts") or 0))

    ct = []
    for r in closed:
        try:
            ct.append(json.loads(r["data"]))
        except ValueError:
            pass

    # v7: analiiska live - symbol kasta + wararkiisa
    ana = []
    for r in anr:
        try:
            row = json.loads(r["data"])
            row["_news"] = json.loads(r["news"] or "[]")
            row["_age"] = int(now - r["ts"])
            ana.append(row)
        except ValueError:
            pass
    ana.sort(key=lambda z: (_AN_ORDER.get(str(z.get("st", "")), 9),
                            _an_dist(z), str(z.get("sym", ""))))

    return jsonify(
        ok=True, account=acc, online=online,
        age=None if age is None else int(age),
        data=data,
        history=[{"t": int(h["ts"]), "b": h["balance"], "e": h["equity"]} for h in hist],
        closed=ct,
        pending=[p["cmd"] for p in pend],
        can_control=bool(u["can_control"]),
        is_admin=(u["role"] == "admin"),
        brand=(br["img"] if br else BRAND_IMAGE_URL),
        analysis=ana,
        chat_unread=chat_unread,
        cfg_rev=(int(cfgr["rev"]) if cfgr else 0),
        cfg_saved=(float(cfgr["updated_at"]) if cfgr else None),
        key_seen_age=(int(now - float(kseen["last_seen"])) if (kseen and kseen["last_seen"]) else None),
        lic=lic,
        bsk=bsk,
        asia=asia,
        tick=tick,
        inp=inp,
        zn=zn,
        pairs=prs,
        grid=grd,
    )


# ======================================================================
# v12.20: 🎵 MUUSIG — liiska heesaha qof kasta (YouTube · Spotify · Audiomack LINK-yo) -> kv mus:<account>.
#   Heesaha laftooda server-ka laguma kaydiyo (xuquuqda fanaaniinta) — link + magac + sawir keliya.
#   MP3-yada telefoonka (BASS EQ) telefoonka ayay ku jiraan (IndexedDB) — server-ka ma gaadhaan.
# ======================================================================
import urllib.request as _ureq, urllib.parse as _uparse

MUS_MAX = 200
MUS_CATS = ("SO", "WO", "MY")
_MUS_YT = re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)([A-Za-z0-9_-]{11})"
                     r"|youtu\.be/([A-Za-z0-9_-]{11})")
_MUS_YTL = re.compile(r"[?&]list=([A-Za-z0-9_-]{10,64})")
_MUS_SP = re.compile(r"open\.spotify\.com/(?:intl-[A-Za-z-]{2,8}/)?(?:embed/)?(track|playlist|album|artist|episode|show)/([A-Za-z0-9]{22})")
_MUS_SPU = re.compile(r"^spotify:(track|playlist|album|artist|episode|show):([A-Za-z0-9]{22})$")
_MUS_AM = re.compile(r"audiomack\.com/(?:embed/)?(?:(song|album|playlist)/([A-Za-z0-9_.-]{1,80})/([A-Za-z0-9_.-]{1,120})"
                     r"|([A-Za-z0-9_.-]{1,80})/(song|album|playlist)/([A-Za-z0-9_.-]{1,120}))")
_MUS_ID = re.compile(r"^[a-f0-9]{8}$")
_MUS_IMG = ("i.ytimg.com", "i.scdn.co", "image-cdn-ak.spotifycdn.com", "image-cdn-fa.spotifycdn.com",
            "mosaic.scdn.co", "seeded-session-images.scdn.co", "assets.audiomack.com")


def _mus_parse(url):
    """Link -> (nooc, ref). Nooc: yt (muuqaal) · ytl (playlist) · sp (Spotify) · am (Audiomack). Khalad -> None."""
    u = str(url or "").strip()[:600]
    if not u:
        return None
    low = u.lower()
    m = _MUS_SPU.match(u)
    if m:
        return ("sp", m.group(1) + ":" + m.group(2))
    if "spotify.com" in low:
        m = _MUS_SP.search(u)
        return ("sp", m.group(1) + ":" + m.group(2)) if m else None
    if "audiomack.com" in low:
        m = _MUS_AM.search(u)
        if not m:
            return None
        if m.group(1):
            return ("am", m.group(1) + "/" + m.group(2) + "/" + m.group(3))
        if m.group(4).lower() == "embed":
            return None
        return ("am", m.group(5) + "/" + m.group(4) + "/" + m.group(6))
    if "youtu" in low:
        mv, ml = _MUS_YT.search(u), _MUS_YTL.search(u)
        if ml and (not mv or "/playlist" in low):
            return ("ytl", ml.group(1))
        if mv:
            return ("yt", mv.group(1) or mv.group(2))
    return None


def _mus_canon(k, r):
    if k == "yt":
        return "https://www.youtube.com/watch?v=" + r
    if k == "ytl":
        return "https://www.youtube.com/playlist?list=" + r
    if k == "sp":
        t, i = r.split(":", 1)
        return "https://open.spotify.com/" + t + "/" + i
    if k == "am":
        t, a, s = r.split("/", 2)
        return "https://audiomack.com/" + a + "/" + t + "/" + s
    return ""


def _mus_txt(s, n=120):
    s = re.sub(r"[\x00-\x1f\x7f<>]", "", str(s or "")).strip()
    return s[:n]


def _mus_meta(k, r):
    """Magaca + fanaanka + sawirka (oEmbed, 4s). Internet la'aan -> magac caadi ah."""
    t, a, th = "", "", ""
    if k == "yt":
        th = "https://i.ytimg.com/vi/" + r + "/mqdefault.jpg"
    try:
        api = ("https://www.youtube.com/oembed?format=json&url=" if k in ("yt", "ytl") else
               "https://open.spotify.com/oembed?url=" if k == "sp" else "")
        if api:
            rq = _ureq.Request(api + _uparse.quote(_mus_canon(k, r), safe=""), headers={"User-Agent": "MOHA-PRO/12.20"})
            with _ureq.urlopen(rq, timeout=4) as resp:
                j = json.loads(resp.read(65536).decode("utf-8", "replace"))
            t = _mus_txt(j.get("title"))
            a = _mus_txt(j.get("author_name"), 80)
            tu = str(j.get("thumbnail_url") or "")
            pu = _uparse.urlparse(tu)
            if pu.scheme == "https" and pu.hostname in _MUS_IMG and len(tu) < 400:
                th = tu
    except Exception:
        pass
    if not t:
        if k == "am":
            typ, art, slug = r.split("/", 2)
            nice = lambda x: re.sub(r"[-_.]+", " ", x).strip().title()
            t, a = _mus_txt(nice(slug)), _mus_txt(nice(art), 80)
        else:
            t = {"yt": "Hees · YouTube", "ytl": "Playlist · YouTube"}.get(k) or ("Spotify · " + r.split(":", 1)[0])
    return t, a, th


def _mus_get(con, acc):
    r = con.execute("SELECT v FROM kv WHERE k=?", ("mus:" + acc,)).fetchone()
    try:
        j = json.loads(r["v"]) if r else {}
        it = j.get("items") if isinstance(j, dict) else None
        return it if isinstance(it, list) else []
    except Exception:
        return []


def _mus_put(con, acc, items):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("mus:" + acc, json.dumps({"items": items[:MUS_MAX]}, separators=(",", ":"), ensure_ascii=False)))


@app.get("/api/music")
@login_required
def api_music():
    with db() as con:
        items = _mus_get(con, request.user["account"])
    return jsonify(ok=True, items=items, max=MUS_MAX)


@app.post("/api/music")
@login_required
def api_music_post():
    acc = request.user["account"]
    b = request.get_json(silent=True) or {}
    op = str(b.get("op") or "")
    iid = str(b.get("id") or "")
    if op == "add":
        p = _mus_parse(b.get("url"))
        if not p:
            return jsonify(ok=False, error="Link-gan lama aqoon. YouTube · Spotify · Audiomack keliya."), 400
        c = str(b.get("c") or "MY").upper()
        c = c if c in MUS_CATS else "MY"
        with db() as con:
            items = _mus_get(con, acc)
        if any(x.get("k") == p[0] and x.get("r") == p[1] and x.get("c") == c for x in items):
            return jsonify(ok=False, error="Heestan horey ayay liiskan ugu jirtay.", items=items), 409
        if len(items) >= MUS_MAX:
            return jsonify(ok=False, error="Liisku waa buuxaa (%d). Qaar tirtir." % MUS_MAX, items=items), 400
        t, a, th = _mus_meta(p[0], p[1])          # internet-ka DB-ga ka baxsan (xidhiidh furan ma hayo)
        ut = _mus_txt(b.get("t"))
        it = {"id": secrets.token_hex(4), "k": p[0], "r": p[1], "t": ut or t, "a": a, "th": th, "c": c, "ts": int(time.time())}
        with db() as con:
            items = _mus_get(con, acc)
            items.append(it)
            _mus_put(con, acc, items)
        return jsonify(ok=True, items=items, item=it)
    if not _MUS_ID.match(iid):
        return jsonify(ok=False, error="Hees lama helin."), 400
    with db() as con:
        items = _mus_get(con, acc)
        ix = next((i for i, x in enumerate(items) if x.get("id") == iid), -1)
        if ix < 0:
            return jsonify(ok=False, error="Hees lama helin.", items=items), 404
        if op == "del":
            items.pop(ix)
        elif op == "move":
            d = -1 if int(b.get("d") or 0) < 0 else 1
            c = items[ix].get("c")
            same = [i for i, x in enumerate(items) if x.get("c") == c]
            pos = same.index(ix) + d
            if 0 <= pos < len(same):
                j = same[pos]
                items[ix], items[j] = items[j], items[ix]
        elif op == "cat":
            c = str(b.get("c") or "").upper()
            if c not in MUS_CATS:
                return jsonify(ok=False, error="Qayb khaldan."), 400
            items[ix]["c"] = c
        elif op == "ren":
            t = _mus_txt(b.get("t"))
            if not t:
                return jsonify(ok=False, error="Magac geli."), 400
            items[ix]["t"] = t
        else:
            return jsonify(ok=False, error="Amar khaldan."), 400
        _mus_put(con, acc, items)
    return jsonify(ok=True, items=items)


@app.get("/api/inp_schema")
@login_required
def api_inp_schema():
    """v12.18: liiska input-yada EA-ga (magac · nooc · qayb · doorashooyin) -> app-ka foomka ayuu ka dhisaa."""
    resp = make_response(INP_SCHEMA_JSON)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.get("/api/levels")
@login_required
def api_levels():
    """v12.3: Analiis -> Heerarka & Range (symbol kasta)."""
    u = request.user
    acc = _visible_account(u)
    want = str(request.args.get("sym") or "")[:16]
    now = time.time()
    with db() as con:
        rows = con.execute("SELECT sym,data,ts FROM levels WHERE account=? AND ts>? ORDER BY sym",
                           (acc, now - ANALYSIS_TTL)).fetchall()
    syms = [r["sym"] for r in rows]
    pick = next((r for r in rows if r["sym"] == want), None) or (rows[0] if rows else None)
    lv = _jload(pick["data"]) if pick else None
    return jsonify(ok=True, account=acc, syms=syms, sym=(pick["sym"] if pick else ""),
                   lv=lv, age=(int(now - float(pick["ts"])) if pick else None))


@app.post("/api/command")
@login_required
def api_command():
    u = request.user
    body = request.get_json(silent=True) or {}
    cmd = str(body.get("cmd", "")).strip().upper()
    ok_cmd, cmd = valid_command(cmd)
    if not ok_cmd:
        return jsonify(ok=False, error="Amar aan la aqoon ama qiime xad-dhaaf ah."), 400
    acc = clean_account(body.get("account")) if u["role"] == "admin" else u["account"]
    acc = acc or u["account"]
    lic = _user_lic(u, acc)                                   # v12
    if lic is None and not u["can_control"] and u["role"] != "admin":
        return jsonify(ok=False, error="Amar diritaanka lagaama ogola."), 403
    if lic is not None:
        if lic["state"] != "OK":
            return jsonify(ok=False, error="Laysinkaagu wuu dhacay - admin-ka la xiriir."), 403
        need = ("run" if cmd in ("START", "STOP") else
                "close" if cmd in ("CLOSE_ALL", "CLOSE_PROFIT", "BASKET:CLOSE", "BASKET:BE") else "")
        if not need or not lic["perms"].get(need):
            return jsonify(ok=False, error="Amarkan admin-ka ayaa leh."), 403
    with db() as con:
        n = con.execute("SELECT COUNT(*) c FROM commands WHERE account=? AND taken_at IS NULL",
                        (acc,)).fetchone()["c"]
        if n >= 24:   # v5: sitinka hal mar 7 amar ayuu noqon karaa
            return jsonify(ok=False, error="Amaro badan ayaa safka ku jira."), 429
        con.execute("INSERT INTO commands(account,cmd,by_account,created_at) VALUES(?,?,?,?)",
                    (acc, cmd, u["account"], time.time()))
    return jsonify(ok=True, cmd=cmd, account=acc)

# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
RANGES = {"day": 1, "week": 7, "month": 30, "year": 365, "all": 0}


@app.get("/api/export/trades.csv")
@login_required
def export_trades_csv():
    """v6: trade-yada la xidhay -> CSV (Excel). Kaliya account-ka la arki karo."""
    u = request.user
    acc = _visible_account(u)
    with db() as con:
        rows = con.execute(
            "SELECT sym,type,strat,lot,points,profit,ot,ct FROM ctrades"
            " WHERE account=? ORDER BY ct DESC LIMIT 5000", (acc,)).fetchall()
    out = ["symbol,nooc,xeelad,lot,points,profit,furitaan,xidhitaan"]
    for r in rows:
        def ts(v):
            try:
                return datetime.fromtimestamp(int(v or 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                return ""
        out.append(",".join([
            str(_col(r, "sym") or ""), str(_col(r, "type") or ""),
            '"%s"' % str(_col(r, "strat") or "").replace('"', "'"),
            "%.2f" % _num(_col(r, "lot")), "%.1f" % _num(_col(r, "points")),
            "%.2f" % _num(_col(r, "profit")), ts(_col(r, "ot")), ts(_col(r, "ct")),
        ]))
    body = "\r\n".join(out) + "\r\n"
    resp = make_response(body)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="mohapro_%s_trades.csv"' % acc
    return resp


@app.get("/api/export/snapshot.json")
@login_required
def export_snapshot_json():
    """v6: xogtii ugu dambaysay ee EA-du soo dirtay, sidii ay ahayd."""
    u = request.user
    acc = _visible_account(u)
    with db() as con:
        snap = con.execute("SELECT data,updated_at FROM snapshots WHERE account=?", (acc,)).fetchone()
    if not snap:
        return jsonify(ok=False, error="Weli xog lama helin."), 404
    try:
        data = json.loads(_col(snap, "data") or "{}")
    except ValueError:
        data = {}
    payload = {
        "account": acc,
        "la_helay": datetime.fromtimestamp(float(_col(snap, "updated_at") or 0),
                                           timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "xogta_EA": data,
    }
    resp = make_response(json.dumps(payload, indent=2, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="mohapro_%s_snapshot.json"' % acc
    return resp


# --------------------------------------------------------------------------
# v9: WADA-SHEEKAYSI — isticmaalaha <-> admin (qoraal + sawir)
#  Isticmaale kasta hal wada-sheekaysi ("thread" = account-kiisa) ayuu la leeyahay admin-ka.
#  Isticmaaluhu kaliya kiisa ayuu arkaa; admin-ku dhammaan.
#  Sawirrada: JPEG/PNG/WEBP oo keliya (SVG maya), <= 700 KB, 30 maalmood kadib waa la tirtiraa.
# --------------------------------------------------------------------------
CHAT_IMG_MAX  = int(os.environ.get("CHAT_IMG_MAX_KB", "700")) * 1024
CHAT_IMG_DAYS = int(os.environ.get("CHAT_IMG_DAYS", "30"))
CHAT_TEXT_MAX = 2000
CHAT_RATE     = 40          # fariin 10 daqiiqo gudahood, qof kasta


def _img_kind(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _chat_thread(u, want):
    if u["role"] == "admin":
        return clean_account(want) or None
    return u["account"]


def _chat_unread(con, u):
    if u["role"] == "admin":
        r = con.execute("SELECT COUNT(*) c FROM messages WHERE from_admin=0 AND read_at IS NULL").fetchone()
    else:
        r = con.execute("SELECT COUNT(*) c FROM messages WHERE thread=? AND from_admin=1 AND read_at IS NULL",
                        (u["account"],)).fetchone()
    return int(r["c"] or 0)


def _msg_row(r):
    return {"id": int(r["id"]), "from_admin": bool(r["from_admin"]), "body": r["body"] or "",
            "img_id": (None if r["img_id"] is None else int(r["img_id"])),
            "ts": float(r["created_at"])}


# --------------------------------------------------------------------------
# v9.1: SAWIRRADA TRADE-YADA — EA v67.2 ayaa soo dira (POST /shots) marka trade
#  la furo (OPEN) iyo marka la xidho (CLOSE). App-ka: tab-ka Trade.
# --------------------------------------------------------------------------
SHOT_MAX  = int(os.environ.get("SHOT_MAX_KB", "900")) * 1024
SHOT_DAYS = int(os.environ.get("SHOT_DAYS", "30"))


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


@app.post("/shots")
def ea_shots():
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    d = _ea_body()                                         # v12.8.1
    if getattr(g, "_ebad", False):
        return _ea_bad_body()
    acc = clean_account(d.get("account"))
    if not acc:
        return jsonify(ok=False, error="account"), 400
    if not ea_bind(acc):                                   # v11
        return _bad_key()
    tag = str(d.get("tag") or "").upper()[:8]
    if tag not in ("OPEN", "CLOSE"):
        return jsonify(ok=False, error="tag"), 400
    raw = b""; mime = ""
    img = d.get("img") or ""
    if img:
        if not isinstance(img, str) or ";base64," not in img[:40]:
            return jsonify(ok=False, error="img"), 400
        b64 = re.sub(r"\s+", "", img.split(",", 1)[1])
        if len(b64) > SHOT_MAX * 4 // 3 + 16:
            return jsonify(ok=False, error="weyn"), 413
        try:
            raw = _b64.b64decode(b64, validate=True)
        except (ValueError, TypeError):
            return jsonify(ok=False, error="b64"), 400
        mime = _img_kind(raw) or ""
        if not mime:
            return jsonify(ok=False, error="PNG/JPEG oo keliya"), 400
    now = time.time()
    try:
        dg = max(0, min(8, int(d.get("dg") or 5)))
    except (TypeError, ValueError):
        dg = 5
    with db() as con:
        con.execute(
            "INSERT INTO trade_shots(account,pos,sym,tag,type,lot,entry,sl,tp,closep,profit,ot,ct,dg,mime,data,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (acc, str(d.get("pos") or "")[:24], str(d.get("sym") or "")[:16], tag,
             str(d.get("type") or "")[:4], _f(d.get("lot")), _f(d.get("entry")), _f(d.get("sl")),
             _f(d.get("tp")), _f(d.get("close")), _f(d.get("profit")), _f(d.get("ot")), _f(d.get("ct")),
             dg, mime, (_b64.b64encode(raw).decode("ascii") if raw else ""), now))
        if _should_prune("shots:" + acc):
            con.execute("DELETE FROM trade_shots WHERE account=? AND created_at<?", (acc, now - SHOT_DAYS * 86400))
    return jsonify(ok=True)


@app.get("/api/shots")
@login_required
def api_shots():
    u = request.user
    acc = _visible_account(u)
    with db() as con:
        rows = con.execute(
            "SELECT id,pos,sym,tag,type,lot,entry,sl,tp,closep,profit,ot,ct,dg,mime,created_at"
            " FROM trade_shots WHERE account=? ORDER BY id DESC LIMIT 200", (acc,)).fetchall()
    # trade kasta: OPEN + CLOSE isku xidh (pos; haddii kale symbol + waqtiga furitaanka)
    trades = {}
    order = []
    def key_for(r):
        pos = (r["pos"] or "").strip()
        if pos and pos != "0":
            return "p:" + pos
        return "t:%s:%d" % (r["sym"], int(float(r["ot"] or 0)) // 120)
    for r in reversed(rows):                              # duug -> cusub
        k = key_for(r)
        if r["tag"] == "CLOSE" and k not in trades:       # pos kala duwan -> symbol + waqti ku raadi
            for kk in reversed(order):
                t = trades[kk]
                if t["sym"] == r["sym"] and t.get("close") is None and abs(float(t["ot"] or 0) - float(r["ot"] or 0)) <= 180:
                    k = kk
                    break
        t = trades.get(k)
        if t is None:
            t = {"sym": r["sym"], "type": r["type"], "lot": r["lot"], "entry": r["entry"], "sl": r["sl"],
                 "tp": r["tp"], "ot": r["ot"], "dg": r["dg"], "pos": r["pos"], "open": None, "close": None}
            trades[k] = t
            order.append(k)
        shot = {"id": int(r["id"]), "img": bool(r["mime"]), "ts": float(r["created_at"])}
        if r["tag"] == "OPEN":
            t["open"] = shot
            for f in ("type", "lot", "entry", "sl", "tp", "ot", "dg"):
                if r[f]:
                    t[f] = r[f]
        else:
            t["close"] = shot
            t["closep"] = r["closep"]; t["profit"] = r["profit"]; t["ct"] = r["ct"]
            for f in ("type", "lot", "entry", "sl", "tp", "ot", "dg"):
                if not t.get(f) and r[f]:
                    t[f] = r[f]
    out = sorted(trades.values(), key=lambda t: max(float(t.get("ct") or 0), float(t.get("ot") or 0)),
                 reverse=True)[:60]          # dhaqdhaqaaqa ugu dambeeyay ayaa kor ah
    return jsonify(ok=True, account=acc, trades=out, days=SHOT_DAYS)


@app.get("/api/shots/img/<int:sid>")
@login_required
def api_shot_img(sid):
    u = request.user
    with db() as con:
        r = con.execute("SELECT account,mime,data FROM trade_shots WHERE id=?", (sid,)).fetchone()
    if not r or not r["data"] or (u["role"] != "admin" and r["account"] != u["account"]):
        return Response("not found", status=404, mimetype="text/plain")
    return Response(_b64.b64decode(r["data"]), mimetype=r["mime"] or "image/png",
                    headers={"Cache-Control": "private, max-age=2592000, immutable",
                             "X-Content-Type-Options": "nosniff"})

@app.get("/api/chat")
@login_required
def api_chat():
    u = request.user
    t = _chat_thread(u, request.args.get("thread"))
    if not t:
        return jsonify(ok=False, error="Dooro qof."), 400
    try:
        after = max(0, int(request.args.get("after") or 0))
    except ValueError:
        after = 0
    now = time.time()
    is_admin = (u["role"] == "admin")
    with db() as con:
        rows = con.execute(
            "SELECT id,from_admin,body,img_id,created_at FROM messages WHERE thread=? AND id>?"
            " ORDER BY id DESC LIMIT 120", (t, after)).fetchall()
        # dhinaca kale fariimihiisa -> "la akhriyay"
        con.execute("UPDATE messages SET read_at=? WHERE thread=? AND from_admin=? AND read_at IS NULL",
                    (now, t, 0 if is_admin else 1))
        seen = con.execute("SELECT MAX(id) m FROM messages WHERE thread=? AND from_admin=? AND read_at IS NOT NULL",
                           (t, 1 if is_admin else 0)).fetchone()
        peer = None
        if is_admin:
            pr = con.execute("SELECT name FROM users WHERE account=?", (t,)).fetchone()
            peer = (pr["name"] if pr else "") or t
    return jsonify(ok=True, thread=t, peer=peer or "MOHA PRO",
                   messages=[_msg_row(r) for r in reversed(rows)],
                   seen_upto=int((seen["m"] if seen else 0) or 0))


@app.post("/api/chat")
@login_required
def api_chat_send():
    u = request.user
    body = request.get_json(silent=True) or {}
    t = _chat_thread(u, body.get("thread"))
    if not t:
        return jsonify(ok=False, error="Dooro qof."), 400
    text = str(body.get("text") or "").replace("\r", "").strip()[:CHAT_TEXT_MAX]
    img = body.get("img") or ""
    if not text and not img:
        return jsonify(ok=False, error="Fariin madhan."), 400
    raw = mime = None
    if img:
        if not isinstance(img, str) or not img.startswith("data:image/") or ";base64," not in img[:40]:
            return jsonify(ok=False, error="Sawir aan la aqoon."), 400
        b64 = img.split(",", 1)[1]
        if len(b64) > CHAT_IMG_MAX * 4 // 3 + 16:
            return jsonify(ok=False, error="Sawirku aad buu u weyn yahay."), 413
        try:
            raw = _b64.b64decode(b64, validate=True)
        except (ValueError, TypeError):
            return jsonify(ok=False, error="Sawirku waa khaldan yahay."), 400
        mime = _img_kind(raw)
        if not mime or len(raw) > CHAT_IMG_MAX:
            return jsonify(ok=False, error="JPEG, PNG ama WEBP oo keliya (<= %d KB)." % (CHAT_IMG_MAX // 1024)), 400
    now = time.time()
    is_admin = (u["role"] == "admin")
    with db() as con:
        if is_admin and not con.execute("SELECT 1 FROM users WHERE account=?", (t,)).fetchone():
            return jsonify(ok=False, error="Isticmaalahan lama helin."), 404
        n = con.execute("SELECT COUNT(*) c FROM messages WHERE sender=? AND created_at>?",
                        (u["account"], now - 600)).fetchone()["c"]
        if int(n or 0) >= CHAT_RATE:
            return jsonify(ok=False, error="Fariimo badan. Daqiiqado sug."), 429
        img_id = None
        if raw is not None:
            img_id = con.execute(
                "INSERT INTO chat_images(thread,mime,data,created_at) VALUES(?,?,?,?) RETURNING id",
                (t, mime, _b64.b64encode(raw).decode("ascii"), now)).fetchone()["id"]
        mid = con.execute(
            "INSERT INTO messages(thread,sender,from_admin,body,img_id,created_at) VALUES(?,?,?,?,?,?) RETURNING id",
            (t, u["account"], 1 if is_admin else 0, text, img_id, now)).fetchone()["id"]
        if _should_prune("chat"):
            cut = now - CHAT_IMG_DAYS * 86400
            con.execute("DELETE FROM chat_images WHERE created_at<?", (cut,))
            con.execute("UPDATE messages SET img_id=-1 WHERE img_id>0 AND created_at<?", (cut,))
    return jsonify(ok=True, message={"id": int(mid), "from_admin": is_admin, "body": text,
                                      "img_id": (int(img_id) if img_id is not None else None), "ts": now})


@app.get("/api/chat/img/<int:iid>")
@login_required
def api_chat_img(iid):
    u = request.user
    with db() as con:
        r = con.execute("SELECT thread,mime,data FROM chat_images WHERE id=?", (iid,)).fetchone()
    if not r or (u["role"] != "admin" and r["thread"] != u["account"]):
        return Response("not found", status=404, mimetype="text/plain")
    return Response(_b64.b64decode(r["data"]), mimetype=r["mime"],
                    headers={"Cache-Control": "private, max-age=2592000, immutable",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/api/chat/threads")
@login_required
def api_chat_threads():
    u = request.user
    if u["role"] != "admin":
        return jsonify(ok=False, error="admin"), 403
    with db() as con:
        us = con.execute("SELECT account,name FROM users WHERE role<>'admin' AND approved=1").fetchall()
        agg = con.execute(
            "SELECT thread, MAX(id) last_id,"
            " SUM(CASE WHEN from_admin=0 AND read_at IS NULL THEN 1 ELSE 0 END) unread"
            " FROM messages GROUP BY thread").fetchall()
        a = {r["thread"]: r for r in agg}
        ids = [int(r["last_id"]) for r in agg if r["last_id"]]
        last = {}
        if ids:
            for r in con.execute("SELECT id,thread,from_admin,body,img_id,created_at FROM messages WHERE id IN (%s)"
                                 % ",".join("?" * len(ids)), ids).fetchall():
                last[r["thread"]] = r
    out = []
    for x in us:
        t = x["account"]; l = last.get(t)
        out.append({"thread": t, "name": x["name"] or t,
                    "unread": int((a[t]["unread"] if t in a else 0) or 0),
                    "last": (None if not l else {"body": l["body"] or "", "img": l["img_id"] is not None,
                                                 "from_admin": bool(l["from_admin"]), "ts": float(l["created_at"])})})
    out.sort(key=lambda z: (-(z["last"]["ts"] if z["last"] else 0), z["name"].lower()))
    return jsonify(ok=True, threads=out)

@app.get("/api/journal")
@login_required
def api_journal():
    """Tirakoobka trade-yada la xidhay: symbol, saacad, xeelad, maalin, bil."""
    u   = request.user
    acc = _visible_account(u)
    rng = request.args.get("range", "month")
    if rng not in RANGES:
        rng = "month"
    days = RANGES[rng]

    if days:
        # maalinta waxay ka bilaabmaysaa 00:00 (UTC) maanta
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cut = int(start.timestamp()) - (days - 1) * 86400
    else:
        cut = 0

    with db() as con:
        rows = con.execute(
            "SELECT sym,type,strat,lot,points,profit,ot,ct FROM ctrades"
            " WHERE account=? AND ct>=? ORDER BY ct DESC", (acc, cut)).fetchall()
        first = con.execute("SELECT MIN(ct) m FROM ctrades WHERE account=?",
                            (acc,)).fetchone()
        total_all = con.execute("SELECT COUNT(*) c FROM ctrades WHERE account=?",
                                (acc,)).fetchone()["c"]

    def blank():
        return {"n": 0, "w": 0, "net": 0.0, "gp": 0.0, "gl": 0.0}

    def add(b, p):
        b["n"] += 1
        b["net"] += p
        if p > 0:
            b["w"] += 1; b["gp"] += p
        else:
            b["gl"] += abs(p)

    tot = blank()
    by_sym, by_hour, by_strat, by_day, by_month = {}, {}, {}, {}, {}
    best = worst = None

    for r in rows:
        p = r["profit"]; add(tot, p)
        for key, dic in ((r["sym"] or "?", by_sym),
                         (r["strat"] or "?", by_strat)):
            dic.setdefault(key, blank()); add(dic[key], p)
        dt = datetime.fromtimestamp(r["ct"], timezone.utc)
        for key, dic in ((dt.hour, by_hour),
                         (dt.strftime("%Y-%m-%d"), by_day),
                         (dt.strftime("%Y-%m"), by_month)):
            dic.setdefault(key, blank()); add(dic[key], p)
        item = {"sym": r["sym"], "type": r["type"], "strat": r["strat"],
                "profit": p, "points": r["points"], "ct": r["ct"], "lot": r["lot"]}
        if best  is None or p > best["profit"]:  best = item
        if worst is None or p < worst["profit"]: worst = item

    def pack(dic, keyname, sort_by_key=False):
        out = [dict({keyname: k}, **v) for k, v in dic.items()]
        if sort_by_key:
            out.sort(key=lambda x: x[keyname])
        else:
            out.sort(key=lambda x: x["net"], reverse=True)
        return out

    def pf(b):
        return round(b["gp"] / b["gl"], 2) if b["gl"] > 0 else (99.0 if b["gp"] > 0 else 0.0)

    return jsonify(
        ok=True, account=acc, range=rng,
        summary={"n": tot["n"], "wins": tot["w"], "losses": tot["n"] - tot["w"],
                 "winrate": round(tot["w"] / tot["n"] * 100, 1) if tot["n"] else 0,
                 "net": round(tot["net"], 2), "gp": round(tot["gp"], 2),
                 "gl": round(tot["gl"], 2), "pf": pf(tot),
                 "avg": round(tot["net"] / tot["n"], 2) if tot["n"] else 0},
        best=best, worst=worst,
        by_symbol=pack(by_sym, "sym")[:12],
        by_strategy=pack(by_strat, "strat"),
        by_hour=pack(by_hour, "h", True),
        by_day=pack(by_day, "d", True)[-31:],
        by_month=pack(by_month, "m", True)[-12:],
        stored_total=total_all,
        since=(first["m"] if first and first["m"] else 0),
    )


MAX_IMG_CHARS = 1_400_000        # ~1 MB oo base64 ah

@app.post("/api/branding")
@login_required
def api_branding():
    u = request.user
    acc = _visible_account(u)
    body = request.get_json(silent=True) or {}
    img = str(body.get("img", "") or "")

    if not img:                                   # tirtir
        with db() as con:
            con.execute("DELETE FROM branding WHERE account=?", (acc,))
        return jsonify(ok=True, img="")

    if not img.startswith(("data:image/jpeg;base64,", "data:image/png;base64,",
                           "data:image/webp;base64,")):
        return jsonify(ok=False, error="Nooca sawirka lama aqbali karo (JPEG/PNG/WEBP)."), 400
    if len(img) > MAX_IMG_CHARS:
        return jsonify(ok=False, error="Sawirku aad buu u weyn yahay."), 413

    with db() as con:
        con.execute("INSERT INTO branding(account,img,updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(account) DO UPDATE SET img=excluded.img,"
                    " updated_at=excluded.updated_at", (acc, img, time.time()))
    return jsonify(ok=True)


@app.get("/admin")
@admin_required
def admin():
    with db() as con:
        users = con.execute("SELECT * FROM users ORDER BY approved ASC, id ASC").fetchall()
        snaps = {r["account"]: r["updated_at"] for r in
                 con.execute("SELECT account,updated_at FROM snapshots").fetchall()}
    now = time.time()
    rows = []
    for u in users:
        up = snaps.get(u["account"])
        rows.append(dict(
            account=u["account"], name=u["name"], role=u["role"],
            approved=bool(u["approved"]), can_control=bool(u["can_control"]),
            created_at=u["created_at"], last_login=u["last_login"] or "-",
            pw_self=bool(_col(u, "pw_self", 0)),
            online=(up is not None and now - up < STALE_SECONDS),
            has_data=(up is not None),
        ))
    orphans = sorted(set(snaps) - {u["account"] for u in users})
    # v11: furaha bot-ka - isticmaale kasta + account kasta oo xog soo diray
    with db() as con:
        kr = {r["account"]: r for r in con.execute("SELECT * FROM bot_keys").fetchall()}
        master = _master(con)
        lics = {a: _lic_info(con, a, now) for a in
                [r["account"] for r in con.execute("SELECT account FROM licenses").fetchall()]}
    names = {u["account"]: (u["name"] or "") for u in users}
    keyrows = []
    for a in list(dict.fromkeys([r["account"] for r in rows if r["role"] != "admin"] + sorted(snaps) + sorted(kr))):
        k = kr.get(a)
        seen = float(k["last_seen"]) if (k and k["last_seen"]) else None
        li = lics.get(a)
        keyrows.append(dict(account=a, name=names.get(a, ""), key=(k["bkey"] if k else ""),
                            active=bool(k["active"]) if k else False, has=k is not None,
                            seen=(None if seen is None else int(now - seen)),
                            live=(seen is not None and now - seen < 120),
                            lic=li, is_master=(a == master),
                            until_s=(datetime.fromtimestamp(li["until"], timezone.utc).strftime("%d %b %Y")
                                     if (li and li["until"]) else "")))
    mopts = [a for a in list(dict.fromkeys(sorted(snaps) + ([master] if master else []))) if a not in lics]
    return render_template_string(T_ADMIN, rows=rows, me=request.user["account"],
                                  orphans=orphans, keyrows=keyrows, legacy=LEGACY_TOKEN,
                                  master=master, mopts=mopts, perm_keys=PERM_KEYS,
                                  perm_names=PERM_NAMES)


@app.post("/admin/user")
@admin_required
def admin_user():
    acc    = clean_account(request.form.get("account"))
    action = request.form.get("action", "")
    me     = request.user["account"]
    if not acc:
        return redirect(url_for("admin"))
    with db() as con:
        tgt = con.execute("SELECT * FROM users WHERE account=?", (acc,)).fetchone()
        if tgt is None:
            return redirect(url_for("admin"))
        if acc == me and action in ("revoke", "delete", "demote"):
            return redirect(url_for("admin"))     # naftaada ha xidhin
        if action == "approve":
            con.execute("UPDATE users SET approved=1 WHERE account=?", (acc,))
            _key_for(con, acc, create=True)                # v11: furaha bot-ka isla markiiba
        elif action == "revoke":
            con.execute("UPDATE users SET approved=0 WHERE account=?", (acc,))
        elif action == "control_on":
            con.execute("UPDATE users SET can_control=1 WHERE account=?", (acc,))
        elif action == "control_off":
            con.execute("UPDATE users SET can_control=0 WHERE account=?", (acc,))
        elif action == "promote":
            con.execute("UPDATE users SET role='admin', approved=1 WHERE account=?", (acc,))
        elif action == "demote":
            con.execute("UPDATE users SET role='user' WHERE account=?", (acc,))
        elif action == "delete":
            con.execute("DELETE FROM users WHERE account=?", (acc,))
        elif action == "reset_pw":
            newpw = request.form.get("newpw", "")
            if len(newpw) >= 8:
                # pw_self=1 -> EXTRA_USERS boot-ka xiga ma tirtirayo.
                con.execute("UPDATE users SET pw=?, pw_self=1 WHERE account=?",
                            (generate_password_hash(newpw), acc))
        elif action == "pw_env":
            # Dib ugu celi password-ka Render (EXTRA_USERS) - boot-ka xiga.
            con.execute("UPDATE users SET pw_self=0 WHERE account=?", (acc,))
    return redirect(url_for("admin"))


@app.post("/admin/create")
@admin_required
def admin_create():
    acc  = clean_account(request.form.get("account"))
    name = (request.form.get("name") or "").strip()[:60]
    pw   = request.form.get("password", "")
    if len(acc) >= 4 and len(pw) >= 8:
        with db() as con:
            if not con.execute("SELECT 1 FROM users WHERE account=?", (acc,)).fetchone():
                con.execute(
                    "INSERT INTO users(account,name,pw,role,approved,can_control,created_at)"
                    " VALUES(?,?,?,'user',1,1,?)",
                    (acc, name, generate_password_hash(pw), _now_iso()))
                _key_for(con, acc, create=True)            # v11
    return redirect(url_for("admin"))


PERM_NAMES = {"run": "START / STOP", "close": "Xir dhammaan trade-yada", "risk": "Risk %",
              "lot": "Lot gacanta", "sltp": "Habka SL/TP · Sniper · RR", "day": "Trade maalintii",
              "prot": "Wararka · Step-Lock · BE"}


@app.post("/admin/license")
@admin_required
def admin_license():
    """v12: laysinka macmiilka + oggolaanshaha."""
    acc = clean_account(request.form.get("account"))
    action = request.form.get("action", "")
    me = request.user["account"]
    now = time.time()
    if acc:
        with db() as con:
            r = _lic_row(con, acc)
            if action in ("add30", "add365"):
                days = 30 if action == "add30" else 365
                if r is None:
                    con.execute("INSERT INTO licenses(account,until,perms,follow,ovr,updated_at) VALUES(?,?,?,?,?,?)",
                                (acc, now + days * 86400, json.dumps(PERM_DEFAULT), 1, "{}", now))
                    _key_for(con, acc, create=True)
                    _lic_apply(con, acc, me, force=True)          # sitinka master-ka isla markiiba
                else:
                    base = max(float(r["until"] or 0), now)
                    con.execute("UPDATE licenses SET until=?, updated_at=? WHERE account=?",
                                (base + days * 86400, now, acc))
            elif action == "stop" and r is not None:
                con.execute("UPDATE licenses SET until=?, updated_at=? WHERE account=?", (now - 1, now, acc))
            elif action == "remove" and r is not None:
                con.execute("DELETE FROM licenses WHERE account=?", (acc,))
            elif action == "perms" and r is not None:
                p = {k: (1 if request.form.get("p_" + k) else 0) for k in PERM_KEYS}
                p["lo"] = _num(request.form.get("lo"), 0.10)
                p["hi"] = _num(request.form.get("hi"), 0.50)
                p["lo"] = round(max(0.01, min(10.0, p["lo"])), 2)
                p["hi"] = round(max(p["lo"], min(10.0, p["hi"])), 2)
                follow = 1 if request.form.get("follow") else 0
                con.execute("UPDATE licenses SET perms=?, follow=?, updated_at=? WHERE account=?",
                            (json.dumps(p), follow, now, acc))
                _lic_apply(con, acc, me)
    return redirect(url_for("admin") + "#k" + acc)


@app.post("/admin/master")
@admin_required
def admin_master():
    """v12: account-ka sitinkiisa macaamiisha la siinayo."""
    acc = clean_account(request.form.get("account"))
    with db() as con:
        if acc and _lic_row(con, acc) is None:
            con.execute("INSERT INTO kv(k,v) VALUES('master',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (acc,))
            _propagate_master(con, request.user["account"])
    return redirect(url_for("admin") + "#keys")


@app.post("/admin/key")
@admin_required
def admin_key():
    """v11: fure cusub / xannib / fur - account kasta (isticmaale ama xog soo dirtay)."""
    acc = clean_account(request.form.get("account"))
    action = request.form.get("action", "")
    if acc:
        with db() as con:
            r = _key_for(con, acc)
            if action == "new":
                k = _new_key()
                if r is None:
                    con.execute("INSERT INTO bot_keys(account,bkey,active,created_at) VALUES(?,?,1,?)", (acc, k, time.time()))
                else:
                    con.execute("UPDATE bot_keys SET bkey=?, active=1, created_at=?, last_seen=NULL WHERE account=?",
                                (k, time.time(), acc))
            elif action == "off" and r is not None:
                con.execute("UPDATE bot_keys SET active=0 WHERE account=?", (acc,))
            elif action == "on" and r is not None:
                con.execute("UPDATE bot_keys SET active=1 WHERE account=?", (acc,))
    return redirect(url_for("admin") + "#keys")

# ==========================================================================
# Templates
# ==========================================================================
CSS = """
:root{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --line:#2e2e2c;
  --ink:#ffffff; --ink2:#c3c2b7; --ink3:#8b8a82;
  --s1:#3987e5;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b; --serious:#ec835a;
  --r:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--s1);text-decoration:none}
.wrap{max-width:1180px;margin:0 auto;padding:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px}
.center{min-height:100vh;display:grid;place-items:center;padding:16px}
.auth{width:100%;max-width:400px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 12px;color:var(--ink2);font-weight:600;
   text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--ink3);font-size:13px;margin:0 0 20px}
label{display:block;font-size:13px;color:var(--ink2);margin:14px 0 6px}
input,select{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);
  background:#121211;color:var(--ink);font-size:15px;font-family:inherit}
input:focus,select:focus{outline:2px solid var(--s1);outline-offset:1px;border-color:transparent}
button,.btn{cursor:pointer;border:1px solid var(--line);background:#232322;color:var(--ink);
  padding:10px 14px;border-radius:9px;font-size:14px;font-family:inherit}
button:hover,.btn:hover{background:#2e2e2c}
.btn-pri{background:var(--s1);border-color:var(--s1);color:#fff;width:100%;padding:12px;
  font-weight:600;margin-top:20px}
.btn-pri:hover{filter:brightness(1.1);background:var(--s1)}
.msg{padding:11px 13px;border-radius:9px;font-size:14px;margin:14px 0 0}
.msg.err{background:rgba(208,59,59,.15);border:1px solid var(--crit);color:#ffb3b3}
.msg.ok{background:rgba(12,163,12,.15);border:1px solid var(--good);color:#a9e8a9}
.foot{margin-top:18px;text-align:center;font-size:13px;color:var(--ink3)}
.hero{position:relative;min-height:300px;display:flex;flex-direction:column;justify-content:flex-end;
  padding:18px 20px;background:#0f0f0e;overflow:hidden;border-bottom:1px solid var(--line)}
/* gadaasha: nuqul indho-beel ah oo daboolaya banaanka */
.hero-bg{position:absolute;inset:-32px;background-size:cover;background-position:center;
  background-repeat:no-repeat;filter:blur(26px) saturate(1.15) brightness(.55);
  transform:scale(1.12);transition:opacity .25s;opacity:0}
/* hore: sawirka OO DHAN - waxba lagama jarayo */
.hero-img{position:absolute;top:8px;left:0;right:0;bottom:74px;
  background-size:contain;background-position:center;
  background-repeat:no-repeat;transition:opacity .25s;opacity:0}
.hero-bg.on,.hero-img.on{opacity:1}
.hero-fade{position:absolute;inset:0;pointer-events:none;background:
  linear-gradient(180deg,rgba(13,13,13,.62) 0%,rgba(13,13,13,0) 26%,
                  rgba(13,13,13,0) 52%,rgba(13,13,13,.90) 86%,rgba(13,13,13,.97) 100%)}
.hero-ph{position:absolute;inset:0;background:
  radial-gradient(1100px 380px at 18% -12%,rgba(57,135,229,.30),transparent 62%),
  linear-gradient(135deg,#1b2432 0%,#141413 68%)}
.hero-in{position:relative;z-index:2}
.hero h1{font-size:34px;line-height:1.05;margin:0;letter-spacing:-.01em;
  text-shadow:0 2px 14px rgba(0,0,0,.65)}
.hero h1 b{color:var(--s1);font-weight:800}
.hero .tag-line{color:#d8d7cf;font-size:13.5px;margin-top:5px;
  text-shadow:0 1px 8px rgba(0,0,0,.7)}
.hero-top{position:absolute;top:14px;left:20px;right:20px;z-index:3;
  display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
.glass{background:rgba(18,18,17,.72);backdrop-filter:blur(8px);
  border:1px solid rgba(255,255,255,.14);color:#fff}
.hero-top .btn{font-size:13px;padding:8px 12px;white-space:nowrap}
.hero-btns{display:flex;gap:8px;margin-left:auto;flex:0 0 auto}
@media(max-width:560px){ .hero{min-height:330px;padding:16px}
  .hero-img{bottom:78px}
  .hero-top{left:16px;right:16px}
  .hero-top .pill{font-size:12px;padding:5px 9px}
  .hero-top .btn{font-size:12px;padding:7px 10px}
  .hero h1{font-size:28px} }
#pick{display:none}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:14px 16px;background:var(--surface);border-bottom:1px solid var(--line)}
.brand{font-weight:700;letter-spacing:.04em}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--ink2);
  background:#121211;border:1px solid var(--line);padding:6px 11px;border-radius:999px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ink3)}
.dot.on{background:var(--good)} .dot.off{background:var(--crit)} .dot.warn{background:#e0a040}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}
.tile .k{font-size:12px;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:25px;font-weight:650;margin-top:5px;font-variant-numeric:tabular-nums}
.pos{color:var(--good)} .neg{color:var(--crit)} .neu{color:var(--ink)}
.cols{display:grid;gap:16px;grid-template-columns:1fr;margin-bottom:16px}
@media(min-width:900px){.cols.two{grid-template-columns:3fr 2fr}}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--ink3);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid #232322;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.tag{font-size:11px;padding:2px 7px;border-radius:5px;background:#232322;color:var(--ink2)}
.tag.buy{background:rgba(12,163,12,.18);color:#7fd67f}
.tag.sell{background:rgba(208,59,59,.18);color:#f0a0a0}
.cnt{color:var(--ink3);font-weight:500;letter-spacing:0}
@media(max-width:560px){ #tc th:nth-child(4), #tc td:nth-child(4),
  #tc th:nth-child(6), #tc td:nth-child(6){display:none} }
.empty{color:var(--ink3);font-size:13.5px;padding:18px 0;text-align:center}
.ctl{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.ctl button{flex:1;min-width:104px}
.b-stop{border-color:var(--crit);color:#f0a0a0}
.b-go{border-color:var(--good);color:#7fd67f}
.jr{font-size:13px;padding:7px 0;border-bottom:1px solid #232322;color:var(--ink2)}
.jr:last-child{border:none}
.scroll{max-height:340px;overflow:auto}
.xscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.xscroll table{min-width:420px}
@media(max-width:560px){ #tt th:nth-child(3), #tt td:nth-child(3){display:none}
  .xscroll table{min-width:0}}
@media(max-width:640px){td,th{padding:9px 8px}
  .top{padding:10px 12px;gap:8px} .wrap{padding:12px}}
.chart{width:100%;height:210px;display:block}
.chart .grid-l{stroke:#2e2e2c;stroke-width:1}
.chart .ln{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .ar{fill:var(--s1);opacity:.12}
.chart text{fill:var(--ink3);font-size:11px}
.tip{position:fixed;pointer-events:none;background:#121211;border:1px solid var(--line);
  border-radius:8px;padding:7px 10px;font-size:12.5px;opacity:0;transition:opacity .1s;z-index:9}
.note{font-size:12.5px;color:var(--ink3);margin-top:10px}
"""

T_LOGIN = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1013">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="MOHA PRO">
<script>if("serviceWorker" in navigator){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){});});}</script>
<title>MOHA PRO — Gal</title><style>""" + CSS + """</style></head><body>
<div class="center"><div class="card auth">
  <h1>MOHA PRO</h1>
  <p class="sub">Geli lambarka account-kaaga MT5.</p>
  <form method="post" autocomplete="on">
    <label for="a">Lambarka account-ka MT5</label>
    <input id="a" name="account" inputmode="numeric" pattern="[0-9]*" required
           autocomplete="username" placeholder="tusaale 51234567">
    <label for="p">Password</label>
    <input id="p" name="password" type="password" required autocomplete="current-password">
    {% if err %}<div class="msg err">{{ err }}</div>{% endif %}
    <button class="btn-pri" type="submit">GAL</button>
  </form>
  <p class="foot">Account ma lihid? <a href="/register">Isdiiwaangeli</a></p>
</div></div></body></html>"""

T_REGISTER = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1013">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="MOHA PRO">
<script>if("serviceWorker" in navigator){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){});});}</script>
<title>MOHA PRO — Isdiiwaangeli</title><style>""" + CSS + """</style></head><body>
<div class="center"><div class="card auth">
  <h1>Isdiiwaangeli</h1>
  <p class="sub">Admin-ku waa inuu ku ansixiyaa ka hor inta aadan gali karin.</p>
  <form method="post">
    <label for="a">Lambarka account-ka MT5</label>
    <input id="a" name="account" inputmode="numeric" pattern="[0-9]*" required>
    <label for="n">Magacaaga</label>
    <input id="n" name="name" maxlength="60">
    <label for="p">Password (ugu yaraan 8 xaraf)</label>
    <input id="p" name="password" type="password" minlength="8" required
           autocomplete="new-password">
    <label for="p2">Ku celi password-ka</label>
    <input id="p2" name="password2" type="password" minlength="8" required
           autocomplete="new-password">
    {% if err %}<div class="msg err">{{ err }}</div>{% endif %}
    {% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}
    <button class="btn-pri" type="submit">DIIWAANGELI</button>
  </form>
  <p class="foot"><a href="/login">Dib ugu noqo galitaanka</a></p>
</div></div></body></html>"""

T_PASSWORD = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1013">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="MOHA PRO">
<script>if("serviceWorker" in navigator){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){});});}</script>
<title>MOHA PRO \u2014 Beddel password</title><style>""" + CSS + """</style></head><body>
<div class="center"><div class="card auth">
  <h1>Beddel password-ka</h1>
  <p class="sub">Account: <b>{{ acc }}</b></p>
  <form method="post" autocomplete="off">
    <label for="c">Password-kaaga hadda</label>
    <input id="c" name="current" type="password" required autocomplete="current-password">
    <label for="p">Password cusub (ugu yaraan 8 xaraf)</label>
    <input id="p" name="password" type="password" minlength="8" required
           autocomplete="new-password">
    <label for="p2">Ku celi password-ka cusub</label>
    <input id="p2" name="password2" type="password" minlength="8" required
           autocomplete="new-password">
    {% if err %}<div class="msg err">{{ err }}</div>{% endif %}
    {% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}
    <button class="btn-pri" type="submit">KAYDI</button>
  </form>
  <p class="foot"><a href="/dashboard">Dib ugu noqo dashboard-ka</a></p>
</div></div></body></html>"""

T_DASH = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1013">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="MOHA PRO">
<script>if("serviceWorker" in navigator){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){});});}</script>
<script>/* v12.2: midabka app-ka - ka hor inta aan bogga la sawirin */
(function(){try{var t=JSON.parse(localStorage.getItem("mohaTheme")||"{}");
if(t&&/^#[0-9a-fA-F]{6}$/.test(t.acc||"")&&t.acc.toLowerCase()!=="#3987e5"){var r=document.documentElement;r.classList.add("th");r.style.setProperty("--acc",t.acc);}}catch(e){}})();</script>
<title>MOHA PRO</title><style>""" + CSS + """
body{padding-bottom:calc(72px + env(safe-area-inset-bottom))}

/* ---------- HERO ---------- */
/* v4: sawirka waa HERO-ga - qoraalku dusha ayuu ka muuqdaa */
.hero{position:relative;min-height:420px;display:flex;flex-direction:column;
  align-items:flex-start;justify-content:flex-end;text-align:left;
  padding:60px 18px 20px;background:#0f0f0e;overflow:hidden;
  border-bottom:1px solid var(--line)}
.hero-photo{position:absolute;inset:0;background-size:cover;background-position:center;
  background-repeat:no-repeat;opacity:0;transition:opacity .3s}
.hero-photo.on{opacity:1}
.hero-top{position:absolute;top:12px;left:14px;right:14px;z-index:3;
  display:flex;align-items:center;justify-content:space-between;gap:8px}
.hero-top .chip{background:rgba(10,10,10,.62)}
.hero-btns{display:flex;gap:8px;align-items:center}
/* v4.3: astaanta MOHA PRO - marka sawir gaar ah la gelin */
.hero-ph{position:absolute;inset:0;background:
  url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20512%20512%22%20width%3D%22512%22%20height%3D%22512%22%20role%3D%22img%22%20aria-label%3D%22MOHA%20PRO%20BOT%20v56%20MT5%22%3E%20%3Cdefs%3E%20%3ClinearGradient%20id%3D%22gold%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%220%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20%20%20stop-color%3D%22%23FCECBA%22%2F%3E%20%3Cstop%20offset%3D%220.45%22%20stop-color%3D%22%23D6AA50%22%2F%3E%20%3Cstop%20offset%3D%221%22%20%20%20stop-color%3D%22%238C6422%22%2F%3E%20%3C%2FlinearGradient%3E%20%3ClinearGradient%20id%3D%22inner%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%220%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23141E30%22%2F%3E%20%3Cstop%20offset%3D%221%22%20stop-color%3D%22%230A0E18%22%2F%3E%20%3C%2FlinearGradient%3E%20%3Cfilter%20id%3D%22glow%22%20x%3D%22-30%25%22%20y%3D%22-30%25%22%20width%3D%22160%25%22%20height%3D%22160%25%22%3E%20%3CfeGaussianBlur%20stdDeviation%3D%226%22%20result%3D%22b%22%2F%3E%20%3CfeMerge%3E%3CfeMergeNode%20in%3D%22b%22%2F%3E%3CfeMergeNode%20in%3D%22SourceGraphic%22%2F%3E%3C%2FfeMerge%3E%20%3C%2Ffilter%3E%20%3CclipPath%20id%3D%22hexclip%22%3E%20%3Cpolygon%20points%3D%22256%2C23%20458%2C139.5%20458%2C372.5%20256%2C489%2054%2C372.5%2054%2C139.5%22%2F%3E%20%3C%2FclipPath%3E%20%3C%2Fdefs%3E%20%20%3Cpolygon%20points%3D%22256%2C23%20458%2C139.5%20458%2C372.5%20256%2C489%2054%2C372.5%2054%2C139.5%22%20fill%3D%22url%28%23inner%29%22%2F%3E%20%3Cg%20clip-path%3D%22url%28%23hexclip%29%22%20opacity%3D%220.5%22%3E%20%3Cg%20stroke%3D%22%231E2C40%22%20stroke-width%3D%221.6%22%3E%20%3Cline%20x1%3D%22106%22%20y1%3D%22180%22%20x2%3D%22406%22%20y2%3D%22180%22%2F%3E%20%3Cline%20x1%3D%22106%22%20y1%3D%22220%22%20x2%3D%22406%22%20y2%3D%22220%22%2F%3E%20%3Cline%20x1%3D%22106%22%20y1%3D%22260%22%20x2%3D%22406%22%20y2%3D%22260%22%2F%3E%20%3Cline%20x1%3D%22106%22%20y1%3D%22300%22%20x2%3D%22406%22%20y2%3D%22300%22%2F%3E%20%3C%2Fg%3E%20%3C%2Fg%3E%20%20%3Cg%20opacity%3D%220.85%22%3E%20%3Cg%20fill%3D%22%232E966C%22%20stroke%3D%22%232E966C%22%20stroke-width%3D%224%22%3E%20%3Cline%20x1%3D%22150%22%20y1%3D%22180%22%20x2%3D%22150%22%20y2%3D%22300%22%2F%3E%3Crect%20x%3D%22141%22%20y%3D%22198%22%20width%3D%2218%22%20height%3D%2284%22%20rx%3D%223%22%20stroke%3D%22none%22%2F%3E%20%3Cline%20x1%3D%22194%22%20y1%3D%22212%22%20x2%3D%22194%22%20y2%3D%22318%22%2F%3E%3Crect%20x%3D%22185%22%20y%3D%22226%22%20width%3D%2218%22%20height%3D%2276%22%20rx%3D%223%22%20stroke%3D%22none%22%2F%3E%20%3C%2Fg%3E%20%3Cg%20fill%3D%22%23B0404A%22%20stroke%3D%22%23B0404A%22%20stroke-width%3D%224%22%3E%20%3Cline%20x1%3D%22318%22%20y1%3D%22196%22%20x2%3D%22318%22%20y2%3D%22320%22%2F%3E%3Crect%20x%3D%22309%22%20y%3D%22210%22%20width%3D%2218%22%20height%3D%2286%22%20rx%3D%223%22%20stroke%3D%22none%22%2F%3E%20%3Cline%20x1%3D%22362%22%20y1%3D%22176%22%20x2%3D%22362%22%20y2%3D%22308%22%2F%3E%3Crect%20x%3D%22353%22%20y%3D%22190%22%20width%3D%2218%22%20height%3D%2290%22%20rx%3D%223%22%20stroke%3D%22none%22%2F%3E%20%3C%2Fg%3E%20%3C%2Fg%3E%20%20%3Cpolyline%20points%3D%22146%2C300%20200%2C166%20256%2C222%20312%2C166%20366%2C300%22%20fill%3D%22none%22%20stroke%3D%22url%28%23gold%29%22%20stroke-width%3D%2224%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%20filter%3D%22url%28%23glow%29%22%2F%3E%20%3Ccircle%20cx%3D%22378%22%20cy%3D%22148%22%20r%3D%2217%22%20fill%3D%22url%28%23gold%29%22%2F%3E%20%20%3Ctext%20x%3D%22256%22%20y%3D%22392%22%20text-anchor%3D%22middle%22%20fill%3D%22url%28%23gold%29%22%20font-family%3D%22Helvetica%20Neue%2C%20Helvetica%2C%20Arial%2C%20sans-serif%22%20font-weight%3D%22700%22%20font-size%3D%2252%22%3EMOHA%20PRO%3C%2Ftext%3E%20%3Ctext%20x%3D%22256%22%20y%3D%22424%22%20text-anchor%3D%22middle%22%20fill%3D%22%23B2BED0%22%20letter-spacing%3D%223%22%20font-family%3D%22Helvetica%20Neue%2C%20Helvetica%2C%20Arial%2C%20sans-serif%22%20font-weight%3D%22700%22%20font-size%3D%2219%22%3EBOT%20%20v56%20%20%C2%B7%20%20MT5%3C%2Ftext%3E%20%20%3Cpolygon%20points%3D%22256%2C23%20458%2C139.5%20458%2C372.5%20256%2C489%2054%2C372.5%2054%2C139.5%22%20fill%3D%22none%22%20stroke%3D%22url%28%23gold%29%22%20stroke-width%3D%227%22%2F%3E%20%3C%2Fsvg%3E") no-repeat center 42%/auto 46%,
  radial-gradient(900px 340px at 50% -8%,rgba(57,135,229,.30),transparent 64%),
  linear-gradient(160deg,#151d2b 0%,#101010 70%)}
.hero-fade{position:absolute;inset:0;pointer-events:none;background:
  linear-gradient(180deg,rgba(13,13,13,.58) 0%,rgba(13,13,13,0) 26%,
                  rgba(13,13,13,.22) 56%,rgba(13,13,13,.93) 100%)}
.hero-in{position:relative;z-index:2;display:flex;flex-direction:column;align-items:flex-start;width:100%}

/* v4: badhamada sawirka - kore midig */
.edit{display:inline-flex;align-items:center;gap:7px;padding:8px 13px;border-radius:999px;
  background:rgba(10,10,10,.66);border:1px solid rgba(255,255,255,.2);color:#eceae2;
  font-size:12.5px;font-weight:600;backdrop-filter:blur(8px);
  box-shadow:0 4px 14px rgba(0,0,0,.4)}
.edit:hover{filter:brightness(1.15)}
.edit svg{stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.edit svg{width:15px;height:15px}
/* v4.2: badhankii ✕ waa la qariyay - sawirka waxaa lagu saaraa
   adigoo "Beddel sawirka" SI DHEER u haysta (1 ilbiriqsi) */

.hero h1{font-size:32px;margin:0;letter-spacing:-.01em;text-shadow:0 2px 18px rgba(0,0,0,.8)}
.hero h1 b{color:var(--s1);font-weight:800}
.chips{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-start;margin-top:11px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:6px 11px;
  border-radius:999px;background:rgba(18,18,17,.74);border:1px solid rgba(255,255,255,.14);
  color:#d8d7cf;backdrop-filter:blur(8px)}
#pick{display:none}

/* v5: foomka maamulka */
.frow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0}
.frow>span:first-child{font-size:13.5px;color:var(--ink)}
.frow small{color:var(--ink3);font-size:11px}
.frow i{font-style:normal;color:var(--ink3);font-size:12px;margin-left:6px}
.frow input{width:110px;text-align:center;padding:9px 8px;font-size:14px;font-weight:700;
  border-radius:10px;background:#0f1013;border:1px solid var(--line);color:var(--ink)}
.hr{height:1px;background:var(--line);margin:10px 0}
.sw{width:62px;height:30px;border-radius:999px;background:#3a3c44;border:none;padding:0;
  position:relative;cursor:pointer;transition:background .15s}
.sw i{position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;
  background:#f2f2f0;transition:left .15s}
.sw.on{background:#26aa6e}
.sw.on i{left:35px}

.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;color:var(--ink)}
.rawbox{margin:10px 0 0;padding:12px;border-radius:10px;background:#0f1013;border:1px solid var(--line);
  color:#cfd4dd;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;line-height:1.45;
  max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word}

/* ---------- ACTIONS ---------- */
.sec-t{font-size:13px;color:var(--ink2);font-weight:600;text-transform:uppercase;
  letter-spacing:.07em;margin:0 0 11px}
.acts{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.act{border-radius:16px;padding:15px 6px 13px;border:1px solid;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;gap:8px;
  font-size:11.5px;font-weight:700;letter-spacing:.05em;font-family:inherit;
  transition:transform .08s,filter .15s}
.act:active{transform:scale(.97)}
.act:disabled{opacity:.5}
.act svg{width:25px;height:25px;stroke:currentColor;fill:none;stroke-width:1.9;
  stroke-linecap:round;stroke-linejoin:round}
.act.go{background:rgba(12,163,12,.17);border-color:rgba(12,163,12,.6);color:#7fd67f}
.act.stop{background:rgba(208,59,59,.17);border-color:rgba(208,59,59,.6);color:#f0a0a0}
.act.warn{background:rgba(236,131,90,.16);border-color:rgba(236,131,90,.55);color:#f2ad8c}
.act.calm{background:rgba(57,135,229,.15);border-color:rgba(57,135,229,.55);color:#8fc0f5}

/* ---------- TAB BAR ---------- */
.appbar{position:fixed;left:0;right:0;bottom:0;z-index:40;display:flex;
  background:rgba(16,16,15,.97);backdrop-filter:blur(12px);
  border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.appbar button{flex:1;background:none;border:none;border-radius:0;cursor:pointer;
  padding:9px 2px 9px;color:var(--ink3);font-size:10.5px;font-family:inherit;font-weight:600;
  display:flex;flex-direction:column;align-items:center;gap:4px;letter-spacing:.02em}
.appbar button.on{color:var(--s1)}
.appbar button:hover{background:none;color:var(--ink2)}
.appbar button.on:hover{color:var(--s1)}
.appbar svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.8;
  stroke-linecap:round;stroke-linejoin:round}
.rng{display:flex;gap:7px;overflow-x:auto;margin-bottom:14px;padding-bottom:2px}
.rng button{flex:1 1 auto;padding:8px 6px;border-radius:999px;font-size:12.5px;
  white-space:nowrap;min-width:0}
@media(min-width:520px){.rng button{flex:0 0 auto;padding:8px 16px;font-size:13px}}
.rng button.on{background:var(--s1);border-color:var(--s1);color:#fff;font-weight:600}
.bar{display:flex;align-items:center;gap:10px;padding:7px 0;
  border-bottom:1px solid #232322;font-size:13.5px}
.bar:last-child{border:none}
.bar .lb{flex:0 0 74px;color:var(--ink2);font-variant-numeric:tabular-nums}
.bar .tr{flex:1;height:9px;border-radius:5px;background:#232322;overflow:hidden;display:flex}
.bar .fi{height:100%;border-radius:5px}
.bar .fi.p{background:var(--good)} .bar .fi.n{background:var(--crit)}
.bar .vl{flex:0 0 96px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
.bar .sb{flex:0 0 52px;text-align:right;color:var(--ink3);font-size:11.5px}
.bw{font-size:14px}
.bw .big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.bw .sm{color:var(--ink3);font-size:12.5px;margin-top:4px}
@media(max-width:560px){ .bar .lb{flex-basis:62px} .bar .vl{flex-basis:80px}
  .bar .sb{display:none} }
.pane{display:none}
.pane.on{display:block}
/* ---------- v12.18: Maamul (xeeladaha) · ⚙️ Input ---------- */
.sttiles{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.stt{border:1px solid var(--line);border-radius:14px;padding:11px;background:#141413;min-width:0}
.stt .h{display:flex;justify-content:space-between;align-items:center;gap:6px}.stt .h b{font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stt .sw{width:46px;height:24px;flex:0 0 auto}.stt .sw i{width:18px;height:18px}.stt .sw.on i{left:25px}
.stt .m{font-size:11px;color:var(--ink3);margin-top:7px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.stp2{font-size:9.5px;font-weight:800;letter-spacing:.05em;padding:2px 7px;border-radius:999px;background:#22211f;color:var(--ink3)}
.stp2.run{background:rgba(34,197,94,.14);color:#7fe0ab}.stp2.wt{background:rgba(240,192,112,.13);color:#f0c070}
.stt .p{font-size:16px;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums}.stt .p.g{color:#7fe0ab}.stt .p.r{color:#f2a3a3}
.stt .lnk{display:inline-block;margin-top:7px;font-size:11.5px;font-weight:700;color:var(--s1);cursor:pointer;background:none;border:none;padding:0;font-family:inherit}
.stt.off .m,.stt.off .p{opacity:.55}.stsr{font-size:11px;font-weight:800;color:var(--ink2)}
.fold{margin-bottom:12px;padding:0}.fold>summary{list-style:none;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 16px;font-size:13.5px;font-weight:700}
.fold>summary::-webkit-details-marker{display:none}.fold>summary small{color:var(--ink3);font-weight:500;font-size:11px}
.fold>summary:after{content:"›";color:var(--ink3);font-size:18px;margin-left:auto}.fold[open]>summary:after{content:"⌄"}
.fold>:not(summary){padding:0 16px 14px}.fold .card{border:none;background:none;padding:0;margin:0!important}
.inph{position:sticky;top:0;z-index:6;background:var(--plane);padding:2px 0 8px}
.insrch{display:flex;align-items:center;gap:8px;margin:0;background:#0f1013;border:1px solid var(--line);border-radius:12px;padding:0 12px}
.insrch svg{width:17px;height:17px;stroke:var(--ink3);fill:none;stroke-width:2.2;flex:0 0 auto}
.insrch input{flex:1;background:none;border:none;color:var(--ink);font:inherit;font-size:14px;padding:11px 0;outline:none;min-width:0}
.incats{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.incats button{font:inherit;font-size:12px;font-weight:700;color:var(--ink2);background:#161615;border:1px solid var(--line);border-radius:11px;padding:7px 10px;cursor:pointer}
.incats button b{font-size:10.5px;color:var(--ink3);margin-left:3px}.incats button.on{background:var(--s1);border-color:var(--s1);color:#fff}.incats button.on b{color:rgba(255,255,255,.8)}
.incats button.dt:after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#f0c070;margin-left:5px;vertical-align:2px}
.insync{margin-top:8px;font-size:11.5px;color:#8fb59a;background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.25);border-radius:10px;padding:7px 10px;line-height:1.45}
.insync.warn{color:#f0d9a0;background:rgba(240,192,112,.07);border-color:rgba(240,192,112,.3)}
.incat{display:none}.incat.on{display:block}#pInput.srch .incat{display:block}
.incard{margin-top:10px;padding:6px 14px}.incard>.grp:first-child{margin-top:8px}
.ingen:empty{display:none}
.inacc{border:1px solid var(--line);border-radius:14px;margin-top:9px;background:#161615;overflow:hidden}
.inah{width:100%;display:flex;align-items:center;gap:10px;padding:12px;background:none;border:none;color:var(--ink);font:inherit;text-align:left;cursor:pointer}
.inah .ic{width:28px;height:28px;border-radius:9px;background:#1d1d1b;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:13px;flex:0 0 auto}
.inah .nm{flex:1;min-width:0;font-size:13.5px;font-weight:700;line-height:1.25}.inah .nm small{display:block;color:var(--ink3);font-size:11px;font-weight:500}
.inct{font-size:11px;font-weight:800;padding:3px 8px;border-radius:7px;border:1px solid var(--line);color:var(--ink2)}
.indot{width:7px;height:7px;border-radius:50%;background:#f0c070;box-shadow:0 0 6px #f0c070;display:none}.inacc.chg .indot{display:inline-block}
.inah:after{content:"›";color:var(--ink3);font-size:18px}.inacc.open .inah:after{content:"⌄"}
.inab{display:none;padding:0 12px 8px;border-top:1px solid var(--line)}.inacc.open .inab{display:block}
.inrow{border-bottom:1px solid #222220}.inrow:last-child{border-bottom:none}
.inrow .l{flex:1 1 auto;min-width:0;font-size:13.5px;line-height:1.3}.inrow .l small{display:block;margin-top:2px;word-break:break-word}
.inrow .nmx{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;color:#6d6c66}
.inrow .ctl{display:flex;align-items:center;gap:4px;margin-left:auto;flex:0 0 auto}
.inrow input[type=number]{width:96px}
.inrow select{max-width:150px;padding:8px;font-size:13.5px;font-weight:700;border-radius:10px;background:#0f1013;border:1px solid var(--line);color:var(--ink)}
.inrow input[type=text]{width:132px;font-weight:600;font-size:13px}.inrow input[type=color]{width:56px;height:36px;padding:2px;border-radius:9px;background:#0f1013;border:1px solid var(--line)}
.inrow.chg input,.inrow.chg select{border-color:#f0c070;color:#f0d9a0;box-shadow:0 0 0 3px rgba(240,192,112,.1)}
.inrow.chg .sw{box-shadow:0 0 0 3px rgba(240,192,112,.35)}
.inrow.pend input,.inrow.pend select{border-style:dashed;border-color:#c084fc;color:#e9d5ff}.inrow.pend .sw{box-shadow:0 0 0 3px rgba(192,132,252,.45)}
.inrst{background:none;border:none;color:var(--ink3);font-size:16px;cursor:pointer;padding:4px 2px;visibility:hidden}.inrow.chg .inrst,.inrow.pend .inrst{visibility:visible}
.inbdg{display:inline-block;font-size:9px;font-weight:800;padding:1px 5px;border-radius:5px;background:rgba(56,189,248,.14);color:#7cc8ff;margin-left:6px;vertical-align:1px}
.inlk{opacity:.6}.inlkv{font-size:11px;font-weight:800;color:var(--ink3);border:1px solid var(--line);border-radius:7px;padding:3px 7px}
.inoff .ctl{opacity:.45;pointer-events:none}
.insave{position:sticky;bottom:calc(66px + env(safe-area-inset-bottom));z-index:5;margin-top:14px;background:rgba(18,18,17,.97);border:1px solid var(--line);border-radius:16px;padding:10px 12px;backdrop-filter:blur(8px)}
.insave .acts{margin-top:0!important}.insave .note{margin-top:6px;font-size:11.5px;line-height:1.35}
.bsub.fold{cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:8px}
.bsub.fold:after{content:"⌄";font-size:14px;color:#8a7440}.bsub.fold.shut:after{content:"›"}
.bsub.fold.shut+.bsec{display:none}#pInput.srch .bsec{display:block!important}
.inmain .grp>.gh{cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.inmain .grp>.gh:after{content:"⌄";color:var(--ink3)}.inmain .grp.shut>.gh:after{content:"›"}
.inmain .grp.shut>:not(.gh){display:none}#pInput.srch .inmain .grp>*{display:revert}
.inhid{display:none!important}
/* v12.19: 🧭 JIHADA SUUQA */
.tkjd{margin-top:6px;border-radius:12px;padding:10px;text-align:center;border:1px solid var(--line);background:#141413}
.tkjd b{display:block;font-size:17px;font-weight:900;letter-spacing:.05em}.tkjd small{display:block;font-size:11px;color:var(--ink3);margin-top:3px}
.tkjd.dn{background:linear-gradient(180deg,rgba(127,29,29,.45),rgba(60,14,16,.6));border-color:rgba(248,113,113,.55)}.tkjd.dn b{color:#fca5a5}
.tkjd.up{background:linear-gradient(180deg,rgba(20,83,45,.45),rgba(12,40,24,.6));border-color:rgba(74,222,128,.5)}.tkjd.up b{color:#86efac}
.tkjm{position:relative;height:10px;border-radius:6px;margin:10px 2px 3px;background:linear-gradient(90deg,#ef4444 0%,#7f1d1d 30%,#3a3c44 42%,#3a3c44 58%,#14532d 70%,#22c55e 100%)}
.tkjm i{position:absolute;top:-5px;width:4px;height:20px;border-radius:2px;background:#fff;box-shadow:0 0 0 3px rgba(255,255,255,.2);transform:translateX(-2px)}
.tkjml{display:flex;justify-content:space-between;font-size:9px;font-weight:800;color:var(--ink3);letter-spacing:.04em}
.tkjs{margin-top:6px}.tkjs div{display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:5px 1px;border-bottom:1px solid #222220;color:var(--ink2)}
.tkjs div:last-child{border-bottom:none}.tkjs b{font-weight:800}.tkjs .dn{color:#f87171}.tkjs .up{color:#4ade80}.tkjs .nt{color:var(--ink3)}
.tkjr{margin-top:7px;font-size:11.5px;line-height:1.45;color:#f0d9a0;background:rgba(240,192,112,.07);border:1px solid rgba(240,192,112,.3);border-radius:10px;padding:7px 9px}

/* ---------- v10: kaadhka maamulka (qaybo) ---------- */
.grp{border:1px solid var(--line);border-radius:14px;padding:4px 12px 6px;margin:12px 0;background:#161615}
/* v12.7: GOLD BASKET */
.gold{display:inline-flex;align-items:center;gap:8px;font-weight:900;color:#f0cf86;letter-spacing:.08em}
.gold small{font-size:10px;color:var(--ink3);letter-spacing:.06em;font-weight:700}
.coin{width:18px;height:18px;border-radius:50%;flex:0 0 auto;background:radial-gradient(circle at 35% 35%,#fff3c4,#e2b650 55%,#9c7424);box-shadow:0 0 8px rgba(240,207,134,.45)}
.bskg{border-color:rgba(217,174,85,.45);background:linear-gradient(180deg,#1b1810,#161615 60%)}
.bskgh{display:flex;align-items:center;justify-content:space-between;padding:10px 0 6px}
.bsub{font-size:10.5px;font-weight:800;letter-spacing:.1em;color:#b89448;padding:12px 0 2px;border-top:1px dashed rgba(217,174,85,.22);margin-top:6px}
.bskg.off .bsub,.bskg.off .frow,.bskg.off .sltph:not(:first-of-type){opacity:.45}
.bskc{border-color:rgba(217,174,85,.4)}
/* v12.9: ASIA BREAKOUT */
.assvg{width:100%;height:auto;display:block;margin-top:10px;background:#0e110e;border:1px solid var(--line);border-radius:12px}
.astot{margin-top:10px}.astot b.up{color:#7fe0ab}.astot b.dn{color:#f2a3a3}
.ashr{display:grid;grid-template-columns:54px 1fr auto;gap:8px;align-items:center;padding:7px 2px;border-bottom:1px solid var(--line);font-size:12.5px}
.ashr:last-child{border-bottom:none}.ashr .d{color:var(--ink3);font-variant-numeric:tabular-nums}.ashr .p{font-weight:800;font-variant-numeric:tabular-nums}
.ashr .g{color:#7fe0ab}.ashr .r{color:#f2a3a3}.ashr .m{color:var(--ink3)}
/* v12.12: ⚡ TICK SCALPER */
.tkkpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.tkkpi div{background:#121412;border:1px solid var(--line);border-radius:12px;padding:8px 6px;text-align:center;font-size:11px;color:var(--ink3)}
.tkkpi b{display:block;font-size:17px;color:var(--ink);margin-bottom:2px;font-variant-numeric:tabular-nums}
.tkkpi b.up{color:#7fe0ab}.tkkpi b.dn{color:#f2a3a3}
.tkopen{background:#121a12;border:1px solid #2f6b3f;border-radius:14px;padding:10px 12px;margin-top:12px}
.tkopen .t{display:flex;justify-content:space-between;font-size:14px;font-weight:800}
.tkopen .t .pos{color:#7fe0ab}.tkopen .t .neg{color:#f2a3a3}
.tkbar{height:8px;border-radius:5px;background:#2a1414;position:relative;margin:10px 0 4px;overflow:hidden}
.tkbar i{position:absolute;top:0;bottom:0}
.tklg{display:flex;justify-content:space-between;gap:6px;font-size:10.5px;color:var(--ink3);flex-wrap:wrap}
/* v12.13: kaarka TICK - qaab premium (glass + dahab) */
.tkc{position:relative;background:linear-gradient(180deg,#1a1c21,#0e0f12);border:1px solid rgba(201,154,63,.55);box-shadow:0 10px 30px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,236,190,.07)}
.tkc:before{content:"";position:absolute;left:22px;right:22px;top:-1px;height:2px;border-radius:2px;background:linear-gradient(90deg,transparent,#f0cf86,transparent)}
.tkhd{display:flex;align-items:center;gap:10px}
.tkbd{width:38px;height:38px;border-radius:12px;background:linear-gradient(180deg,#f3d58f,#c4923a);display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(201,154,63,.35);flex:0 0 auto}
.tkbd svg{width:19px;height:19px;fill:#1a1305}
.tkhn{flex:1;min-width:0;line-height:1.2}.tkhn b{display:block;font-size:15.5px;font-weight:900;letter-spacing:.06em;color:var(--ink)}.tkhn small{font-size:10.5px;color:#8f96a0;font-weight:700;letter-spacing:.05em}
.tkpill{font-size:10.5px;font-weight:900;letter-spacing:.08em;padding:5px 10px;border-radius:999px;display:flex;align-items:center;gap:6px;white-space:nowrap;background:#26262a;color:var(--ink2)}
.tkpill i{width:7px;height:7px;border-radius:50%;background:currentColor}
.tkpill.run{background:rgba(34,197,94,.14);color:#7fe0ab}.tkpill.run i{box-shadow:0 0 8px #22c55e}
.tkpill.wait{background:rgba(240,192,112,.13);color:#f0c070}.tkpill.off{background:rgba(239,68,68,.12);color:#f2a3a3}
.tkwhy{margin-top:11px;font-size:12.5px;color:#f0d9a0;background:rgba(240,192,112,.07);border:1px solid rgba(240,192,112,.22);border-radius:11px;padding:8px 11px;line-height:1.45}
.tksec{font-size:10px;font-weight:900;letter-spacing:.16em;color:#c99a3f;margin:14px 0 7px}
.tktt{display:flex;align-items:center;gap:10px}
.tkdots{display:flex;gap:4px;flex-wrap:wrap}
.tkdots span{width:21px;height:23px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:900}
.tkdots .u{background:rgba(34,197,94,.16);color:#4ade80;border:1px solid rgba(34,197,94,.35)}
.tkdots .d{background:rgba(239,68,68,.14);color:#f87171;border:1px solid rgba(239,68,68,.3)}
.tkdots .n{background:#17191d;color:#5c626b;border:1px solid #262a31}
.tkttn{font-size:12px;color:#aab0ba;line-height:1.35}.tkttn b{font-size:14px}.tkttn .g{color:#7fe0ab}.tkttn .r{color:#f2a3a3}
.tkmv{margin-top:9px}
.tkmvb{height:7px;border-radius:5px;background:#1e2229;position:relative}
.tkmvb i{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:linear-gradient(90deg,#1e7fd6,#38bdf8);max-width:100%}
.tkmvb em{position:absolute;right:0;top:-4px;bottom:-4px;width:2px;background:#f0cf86;border-radius:1px}
.tkmvl{display:flex;justify-content:space-between;font-size:10.5px;color:#8f96a0;margin-top:5px}.tkmvl b{color:#7cc8ff}
.tkc .tkkpi div{background:rgba(255,255,255,.025);border-color:#252a31;font-size:9.5px;letter-spacing:.06em;font-weight:700;text-transform:uppercase}
.tkc .tkkpi b{font-size:16.5px;letter-spacing:0;text-transform:none}
.tkc .tkkpi b.y{color:#f0cf86}
.tkses{margin-top:13px}
.tksesb{height:6px;border-radius:4px;background:#1b1f25;position:relative}
.tksesb i{position:absolute;top:0;bottom:0;border-radius:4px;background:linear-gradient(90deg,rgba(201,154,63,.35),#f0cf86)}
.tksesb em{position:absolute;top:-4px;width:12px;height:14px;margin-left:-6px;border-radius:4px;background:#fff;box-shadow:0 0 0 3px rgba(240,207,134,.35)}
.tksesl{display:flex;justify-content:space-between;font-size:10px;color:#7a818b;margin-top:6px;letter-spacing:.05em}.tksesl .m{color:#f0cf86}
.tkchips{display:flex;gap:6px;flex-wrap:wrap;margin-top:11px}
.tkchips span{font-size:10.5px;font-weight:800;color:#c7ccd4;background:#15181d;border:1px solid #262b33;border-radius:8px;padding:4px 8px}
.tkchips b{color:#fff}
.tktr{margin-top:12px;border-radius:14px;padding:10px 11px;background:linear-gradient(180deg,rgba(18,40,26,.85),rgba(14,22,17,.9));border:1px solid rgba(47,107,63,.8)}
.tktrh{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tktrh b{font-size:14px;font-weight:900}.tktrh span{font-size:15px;font-weight:900}.tktrh .pos{color:#7fe0ab}.tktrh .neg{color:#f2a3a3}
.tktrs{font-size:10.5px;color:#8fb59a;margin-top:3px}
.tkbar2{position:relative;height:10px;border-radius:6px;background:#2a1517;margin:12px 0 6px}
.tkbar2 .sg{position:absolute;top:0;bottom:0;border-radius:6px}
.tkbar2 .mk{position:absolute;top:-5px;width:3px;height:20px;border-radius:2px;margin-left:-1px}
.tkbarl{display:flex;justify-content:space-between;gap:6px;font-size:10px;color:#93a39a;flex-wrap:wrap}.tkbarl .y{color:#f0cf86}
.tkst{display:flex;gap:6px;margin-top:9px}
.tkst span{flex:1;text-align:center;font-size:9.5px;font-weight:900;letter-spacing:.05em;padding:5px 3px;border-radius:8px;border:1px solid #2a332d;color:#6f7a73}
.tkst .done{background:rgba(201,154,63,.16);border-color:rgba(201,154,63,.5);color:#f0cf86}
.tkst .now{background:#22c55e;border-color:#22c55e;color:#062312}
.tkhr{display:grid;grid-template-columns:46px 40px 1fr auto;gap:6px;font-size:12px;padding:7px 2px;border-bottom:1px solid #1d2127;color:#c7ccd4}
.tkhr:last-child{border-bottom:none}.tkhr .t{color:#7a818b;font-variant-numeric:tabular-nums}.tkhr b{font-variant-numeric:tabular-nums}.tkhr .g{color:#7fe0ab}.tkhr .r{color:#f2a3a3}
.tkft{margin-top:11px;padding-top:9px;border-top:1px solid #20242b;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:10.5px;color:#7a818b}
.tkft b{color:#c7ccd4}.tkft .g{color:#7fe0ab}.tkft .r{color:#f2a3a3}
/* v12.14: tayada bixitaanka + SL broker */
.tkqa{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.tkqa div{background:rgba(34,197,94,.05);border:1px solid rgba(34,197,94,.22);border-radius:11px;padding:7px 5px;text-align:center;font-size:9.5px;letter-spacing:.06em;color:#8f96a0;font-weight:700;text-transform:uppercase}
.tkqa b{display:block;font-size:15.5px;color:var(--ink);letter-spacing:0;text-transform:none;font-variant-numeric:tabular-nums}
.tkqa b.g{color:#7fe0ab}.tkqa b.r{color:#f2a3a3}.tkqa b.y{color:#f0cf86}
.tkqh{font-size:11.5px;line-height:1.5;color:#c7ccd4;background:#111411;border:1px solid #2a352a;border-radius:10px;padding:8px 10px;margin-top:8px}.tkqh b{color:#fff}
.tklock{display:inline-flex;align-items:center;gap:3px;font-size:9.5px;font-weight:900;letter-spacing:.05em;color:#7fe0ab;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.35);border-radius:6px;padding:1px 6px;margin-left:5px}
.tkhr2{border-bottom:1px solid #1d2127;padding:6px 2px}.tkhr2:last-child{border-bottom:none}
.tkhr2 .tkhr{border:none;padding:0 0 2px}
.tkhr2 .x{font-size:10px;color:#7a818b;margin-left:52px}.tkhr2 .x b{color:#c7ccd4;font-weight:700}.tkhr2 .x b.r{color:#f2a3a3}
/* v12.16: TICK BASKET card */
.tkbk{margin-top:12px;border-radius:14px;padding:11px 12px;background:linear-gradient(180deg,rgba(18,40,26,.85),rgba(14,22,17,.9));border:1px solid rgba(47,107,63,.8)}
.tkbk .h{display:flex;justify-content:space-between;align-items:center;gap:8px}.tkbk .h b{font-size:14px;font-weight:900}.tkbk .h span{font-size:15.5px;font-weight:900}
.tkbk .h .pos{color:#7fe0ab}.tkbk .h .neg{color:#f2a3a3}
.tkbk .tg{margin:10px 0 4px}.tkbk .tgb{height:9px;border-radius:6px;background:#1b1f25;position:relative;overflow:hidden}.tkbk .tgb i{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,#1f8a4c,#22c55e);border-radius:6px}
.tkbk .tgl{display:flex;justify-content:space-between;font-size:10.5px;color:#93a39a;margin-top:4px}.tkbk .tgl b{color:#7fe0ab}.tkbk .tgl .y{color:#f0cf86}
.tkly{display:grid;grid-template-columns:28px 62px 1fr auto;gap:6px;align-items:center;font-size:11.5px;padding:6px 2px;border-bottom:1px solid rgba(47,107,63,.35);color:#c7ccd4}
.tkly:last-child{border-bottom:none}.tkly .n{font-weight:900;color:#f0cf86}.tkly .w{color:#6f7a73}.tkly b{font-variant-numeric:tabular-nums}.tkly .g{color:#7fe0ab}.tkly .r{color:#f2a3a3}
.tkly.wait{opacity:.5}
/* v12.17: TP RAAC */
.tktpr{margin:8px 0 4px;padding:2px 10px 6px;border:1px solid rgba(127,224,171,.28);border-radius:12px;background:rgba(34,197,94,.05)}
.tknew{display:inline-block;margin:0 6px;padding:1px 6px;border-radius:6px;background:#1f8a4c;color:#eafff2;font-size:9.5px;font-weight:900;letter-spacing:.4px;vertical-align:1px}
.tkrc{color:#7fe0ab;font-weight:800}
.tkbk .sl{display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;font-size:10.5px;color:#93a39a;margin-top:8px}.tkbk .sl .y{color:#f0cf86}.tkbk .sl .g{color:#7fe0ab}
.tkbd4{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.tkbd4 div{background:rgba(201,154,63,.06);border:1px solid rgba(201,154,63,.3);border-radius:11px;padding:7px 5px;text-align:center;font-size:9.5px;letter-spacing:.06em;color:#8f96a0;font-weight:700;text-transform:uppercase}
.tkbd4 b{display:block;font-size:15px;color:var(--ink);letter-spacing:0;text-transform:none}.tkbd4 b.g{color:#7fe0ab}.tkbd4 b.r{color:#f2a3a3}
.tkterm{background:#000;border:1px solid #2a2a2a;border-radius:10px;padding:10px 12px;font-family:Consolas,"DejaVu Sans Mono",Menlo,monospace;font-size:11.4px;line-height:1.6;margin-top:12px;white-space:pre-wrap;word-break:break-word}
.tkterm .y{color:#ffd700}.tkterm .s{color:#c0c0c0}.tkterm .lg{color:#32cd32}.tkterm .o{color:#ffa500}.tkterm .b{color:#00bfff}.tkterm .w{color:#fff}.tkterm .r{color:#ff6347}.tkterm .d{color:#8a8a8a}.tkterm .gr{color:#808080}
.aswarn{color:#f0d9a0!important;background:#1a1609;border:1px solid #6b5320;border-radius:10px;padding:8px 10px!important;margin:6px 0}
.asln{font-size:12.5px;color:var(--ink2);margin-top:8px;line-height:1.5}.asln b{color:var(--ink)}
/* v12.10: GRID / MARTINGALE */
.asgh{display:flex;justify-content:space-between;align-items:center}
#asgBox.off{opacity:.45}
.aslots{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin:6px 0}
.aslots div{background:#111411;border:1px solid var(--line);border-radius:10px;padding:6px 2px;text-align:center;font-size:10.5px;color:var(--ink3)}
.aslots b{display:block;font-size:14px;color:var(--ink)}
.aslock{font-size:12px;color:var(--ink2);line-height:1.6;border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0;background:#121412}
.aslock b{color:var(--ink)}
.rdim{opacity:.4}
/* v12.8: heerka zone-ka + sababta trade la'aanta */
.sdtbl{width:100%;border-collapse:collapse;font-size:11.5px;margin:4px 0 2px}
.sdtbl th,.sdtbl td{padding:5px 4px;border-bottom:1px solid var(--line);text-align:center}
.sdtbl th{color:var(--ink3);font-weight:700}.sdtbl td:first-child,.sdtbl th:first-child{text-align:left}
.sdtbl td.on,.sdtbl th.on{color:#f0cf86;font-weight:800;background:rgba(217,174,85,.08)}
.rsnc .sec-t{margin:0 0 6px}
.rsnal{background:linear-gradient(180deg,#3a1418,#2a0f12);border:1px solid #7a2a31;border-radius:12px;padding:10px 12px;margin-bottom:12px}
.rsnal>b{color:#ffb3b3;font-size:13.5px;display:block}.rsnal p b{color:#fff}.rsnal p{font-size:12px;color:#e8c2c2;margin:4px 0 0;line-height:1.45}
.rsntot{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 0 8px}
.rsntot div{border:1px solid var(--line);border-radius:10px;padding:7px 4px;text-align:center;font-size:10.5px;color:var(--ink3)}
.rsntot b{display:block;font-size:18px;color:var(--ink)}
.rsnr{display:grid;grid-template-columns:74px 1fr auto;gap:8px;align-items:center;padding:8px 2px;border-bottom:1px solid var(--line);font-size:12.5px}
.rsnr:last-child{border-bottom:none}
.rsnr .sy{font-weight:800}.rsnr .wy{min-width:0;overflow-wrap:anywhere}.rsnr .wy small{display:block;color:var(--ink3);font-size:10.5px}
.rb{font-size:10px;font-weight:800;padding:3px 7px;border-radius:6px;white-space:nowrap}
.rb.r{background:#361519;color:#f2a3a3}.rb.a{background:#33280e;color:#f0c070}.rb.g{background:#12301f;color:#7fe0ab}.rb.c{background:rgba(139,143,153,.15);color:#8b8f99}
.bskh{display:flex;align-items:center;justify-content:space-between;gap:10px}
.bchip{font-size:11px;font-weight:900;padding:4px 9px;border-radius:8px;background:#26262a;color:var(--ink2);white-space:nowrap}
.bchip.run{background:#12301f;color:#7fe0ab}.bchip.off{background:#361519;color:#f2a3a3}.bchip.wait{background:#33280e;color:#f0c070}
.bchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.bchips span{font-size:11px;font-weight:800;padding:3px 8px;border-radius:7px;background:#172a45;color:#9cc3f5}
.bchips span.y{background:#33280e;color:#f0c070}.bchips span.g{background:#12301f;color:#7fe0ab}.bchips span.r{background:#361519;color:#f2a3a3}
.bbig{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px 10px;margin:10px 0 2px}
.bbig b{font-size:30px;font-weight:900;font-variant-numeric:tabular-nums}
.bbig b.pos{color:#3ddc84}.bbig b.neg{color:#f07a7a}
.bbig span{color:var(--ink3);font-size:12.5px}
.bbar{height:10px;border-radius:99px;background:#26262a;position:relative;overflow:hidden;margin:10px 0 4px}
.bbar i{position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:linear-gradient(90deg,#26aa6e,#3ddc84);transition:width .4s}
.bbar i.neg{background:linear-gradient(90deg,#c24848,#f07a7a)}
.bbar em{position:absolute;top:-2px;bottom:-2px;width:2px;background:#f0cf86;opacity:.8}
.bbl{display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink3)}
.btr{display:grid;grid-template-columns:30px 1fr auto 44px;gap:8px;align-items:center;padding:8px 10px;border-radius:10px;font-size:13px;background:#161615;border:1px solid #262624;margin-top:6px}
.btr .n{font-weight:900;color:#d9ae55}.btr .p{text-align:right;font-weight:800;font-variant-numeric:tabular-nums}.btr .rr{text-align:right;color:var(--ink3);font-size:11.5px}
.btr .s{font-size:10px;font-weight:900;padding:2px 6px;border-radius:6px;margin-left:6px;background:#26262a;color:var(--ink2)}
.btr .s.TP{background:#12301f;color:#7fe0ab}.btr .s.SL{background:#361519;color:#f2a3a3}.btr .s.OPEN{background:#172a45;color:#9cc3f5}.btr .s.WAIT{background:#33280e;color:#f0c070}
.bacts{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.bacts .act{flex-direction:row;gap:8px;padding:12px 6px;font-size:13px}
.bidle{font-size:13px;color:var(--ink2);line-height:1.5;margin-top:10px}
.bday{display:flex;justify-content:space-between;align-items:center;gap:10px;border-top:1px solid var(--line);margin-top:12px;padding-top:10px;font-size:12.5px;color:var(--ink3)}
.bday b{color:var(--ink);font-weight:800;font-variant-numeric:tabular-nums}
.gh{font-size:11.5px;font-weight:800;letter-spacing:.1em;color:#d9ae55;padding:10px 0 4px}
.lic .licr{display:flex;align-items:center;justify-content:space-between;gap:12px}
.licbig{font-size:21px;font-weight:750;margin:4px 0 2px}
.licpill{font-size:11px;font-weight:800;letter-spacing:.04em;padding:4px 10px;border-radius:999px;white-space:nowrap;background:#2a2a28;color:var(--ink2)}
.licpill.ok{background:rgba(38,170,110,.2);color:#7fe0ab}.licpill.bad{background:rgba(208,59,59,.2);color:#f2a3a3}.licpill.warn{background:rgba(230,160,60,.2);color:#f0c070}
.lockband{display:flex;gap:9px;align-items:center;background:rgba(217,174,85,.08);border:1px dashed rgba(217,174,85,.45);border-radius:10px;padding:9px 11px;color:#f0cf86;font-size:12.5px;margin:10px 0 4px}
.lockband svg{width:16px;height:16px;flex:0 0 16px;fill:none;stroke:currentColor;stroke-width:2}
/* v12.1: COLOUR MATRIX */
@property --mxc{syntax:'<color>';inherits:true;initial-value:#f0cf86}
@property --mxa{syntax:'<number>';inherits:true;initial-value:0}
body.mx{animation:mxHue var(--mxh,9s) linear infinite,mxPulse var(--mxp,2.4s) ease-in-out infinite}
@keyframes mxHue{0%,100%{--mxc:#f0cf86}25%{--mxc:#38d4ff}50%{--mxc:#c77dff}75%{--mxc:#3fe0a0}}
@keyframes mxPulse{0%,100%{--mxa:.08}50%{--mxa:1}}
body.mx .hero-photo{filter:brightness(calc(1 + .16*var(--mxa))) saturate(calc(1 + .4*var(--mxa)))}
body.mx .hero::after{content:"";position:absolute;inset:0;pointer-events:none;z-index:1;border-radius:inherit;
  box-shadow:inset 0 0 calc(70px*var(--mxa)) color-mix(in srgb,var(--mxc) calc(75%*var(--mxa)),transparent),
             inset 0 0 0 calc(2px*var(--mxa)) color-mix(in srgb,var(--mxc) calc(90%*var(--mxa)),transparent)}
body.mx .hero h1{text-shadow:0 0 calc(14px*var(--mxa)) color-mix(in srgb,var(--mxc) calc(100%*var(--mxa)),transparent),
  0 0 calc(36px*var(--mxa)) color-mix(in srgb,var(--mxc) calc(85%*var(--mxa)),transparent),0 2px 18px rgba(0,0,0,.8)}
body.mx .hero h1 b{color:var(--mxc)}
body.mx .act,body.mx .hero .edit,body.mx .top .btn,body.mx .top .pill{
  box-shadow:0 0 calc(18px*var(--mxa)) color-mix(in srgb,var(--mxc) calc(75%*var(--mxa)),transparent);
  border-color:color-mix(in srgb,var(--mxc) calc(85%*var(--mxa)),var(--line))}
.mxcard{display:flex;align-items:center;gap:14px}
.mxic{font-size:28px;width:40px;text-align:center;flex:0 0 40px}
.mxt{flex:1;min-width:0}.mxt b{display:block;font-size:17px}.mxt small{display:block;color:var(--ink3);font-size:13px;line-height:1.35;margin-top:2px}
@media (prefers-reduced-motion:reduce){body.mx{animation:none;--mxa:.6;--mxc:var(--mx0,#f0cf86)}}
/* v12.2: MIDABKA APP-KA (html.th + --acc) */
html.th{--s1:var(--acc);
  --plane:color-mix(in srgb,var(--acc) 7%,#0b0b0c);
  --surface:color-mix(in srgb,var(--acc) 9%,#161616);
  --line:color-mix(in srgb,var(--acc) 26%,#2a2a2a)}
html.th body{background:var(--plane)}
html.th .sec-t,html.th .tile .k{color:color-mix(in srgb,var(--acc) 55%,#c3c2b7)}
html.th .sw.on{background:var(--acc)}
html.th .appbar{background:color-mix(in srgb,var(--acc) 10%,rgba(16,16,15,.97));border-top-color:color-mix(in srgb,var(--acc) 35%,#2a2a2a)}
html.th .top .btn,html.th .top .pill,html.th .hero .chip{border-color:color-mix(in srgb,var(--acc) 45%,#333)}
html.th .hero-fade{background:linear-gradient(180deg,rgba(13,13,13,.58) 0%,rgba(13,13,13,0) 26%,
  color-mix(in srgb,var(--acc) 10%,rgba(13,13,13,.22)) 56%,color-mix(in srgb,var(--acc) 18%,rgba(11,11,12,.93)) 100%)}
.thsw{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0 4px}
.thsw button{width:40px;height:40px;border-radius:50%;border:2px solid rgba(255,255,255,.12);cursor:pointer;position:relative;padding:0;flex:0 0 40px}
.thsw button.on{outline:3px solid #fff;outline-offset:3px}
.thsw button.on:after{content:"✓";position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:900;font-size:17px;text-shadow:0 1px 3px rgba(0,0,0,.7)}
.thsw .plus{background:conic-gradient(#f43f5e,#f59e0b,#22c55e,#06b6d4,#6366f1,#a855f7,#f43f5e)}
.thsw .plus:after{content:"+";position:absolute;inset:3px;border-radius:50%;background:#161616;color:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700}
.thsw .plus.on{background:var(--cus,#888)}
.thsw .plus.on:after{content:"✓";inset:0;background:none;font-size:17px;font-weight:900;text-shadow:0 1px 3px rgba(0,0,0,.7)}
.thsw input[type=color]{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.thl{font-size:13.5px;font-weight:650;margin-top:14px}.thl small{display:block;font-weight:400;color:var(--ink3);font-size:12px}
.mxch{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.mxch button{display:inline-flex;align-items:center;gap:7px;padding:7px 11px 7px 8px;border-radius:999px;border:1px solid #3a3a3a;background:none;font-size:12.5px;color:#aaa;cursor:pointer}
.mxch button i{width:16px;height:16px;border-radius:50%;flex:0 0 16px}
.mxch button.on{color:var(--c);border-color:var(--c);background:rgba(255,255,255,.06)}
.mxch button.on:after{content:"✓";font-weight:800}
.sepx{height:1px;background:var(--line);margin:16px 0}
.spd{display:flex;gap:6px;margin-top:10px}
.spd button{flex:1;text-align:center;padding:9px;border-radius:10px;border:1px solid var(--line);background:none;font-size:12.5px;font-weight:600;color:#bbb;cursor:pointer}
.spd button.on{background:var(--s1);border-color:var(--s1);color:#fff}
.mxopt[hidden]{display:none}
/* v12.3: HEERARKA & RANGE */
.lvsyms{display:flex;gap:8px;overflow-x:auto;margin-bottom:12px;padding-bottom:2px;scrollbar-width:none}
.lvsyms::-webkit-scrollbar{display:none}
.lvsyms button{flex:0 0 auto;padding:8px 14px;border-radius:999px;border:1px solid var(--line);background:none;color:var(--ink2);font-size:13px;font-weight:650;cursor:pointer}
.lvsyms button.on{background:var(--s1);border-color:var(--s1);color:#fff}
.lvhd{display:flex;justify-content:space-between;align-items:center;gap:10px}
.lvpill{font-size:11px;font-weight:800;letter-spacing:.04em;padding:5px 10px;border-radius:999px;background:rgba(240,207,134,.14);color:#f0cf86;border:1px solid rgba(240,207,134,.4);white-space:nowrap}
.lvpill.up{background:rgba(38,170,110,.15);color:#7fe0ab;border-color:rgba(38,170,110,.45)}
.lvpill.dn{background:rgba(208,59,59,.15);color:#f2a3a3;border-color:rgba(208,59,59,.45)}
.lvleg{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--ink3);margin-top:6px}
.lvleg i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
.lvk{font-size:11px;font-weight:800;letter-spacing:.08em;color:var(--ink3);text-transform:uppercase}
.lvbig{font-size:22px;font-weight:800;margin:4px 0 0;font-variant-numeric:tabular-nums}
.lvbot{display:flex;gap:10px;align-items:flex-start;background:color-mix(in srgb,var(--s1) 12%,transparent);border:1px solid color-mix(in srgb,var(--s1) 45%,transparent);border-radius:12px;padding:12px;font-size:13.5px;line-height:1.45}
.lvbot b{color:color-mix(in srgb,var(--s1) 55%,#fff)}
.lvz{border-radius:12px;padding:12px;margin-top:10px}
.lvz.d{background:rgba(38,170,110,.10);border:1px solid rgba(38,170,110,.35)}.lvz.s{background:rgba(208,59,59,.10);border:1px solid rgba(208,59,59,.35)}
.lvz .zk{font-size:11px;font-weight:800;letter-spacing:.07em}.lvz.d .zk{color:#7fe0ab}.lvz.s .zk{color:#f2a3a3}
.lvz .zv{font-size:19px;font-weight:800;margin:3px 0 6px;font-variant-numeric:tabular-nums}
.lvz .ev{display:flex;flex-direction:column;gap:3px;font-size:12.5px;color:var(--ink2)}
.lvdots{display:inline-flex;gap:4px;margin-left:6px;vertical-align:middle}.lvdots b{width:9px;height:9px;border-radius:50%;background:#26aa6e}
.lvbar{height:6px;border-radius:6px;background:#2a2a28;margin-top:9px;overflow:hidden}.lvbar i{display:block;height:100%}
.lvchg{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--ink2)}.lvchg li{margin:5px 0}
.lvsrc{display:grid;grid-template-columns:1fr 1fr;gap:8px}.lvsrc div{border:1px solid var(--line);border-radius:10px;padding:9px}
.lvsrc b{display:block;font-size:11.5px;letter-spacing:.06em;color:#f0cf86}.lvsrc span{font-size:11.5px;color:var(--ink3)}
#lvChart svg{display:block;width:100%;height:auto}
.mkt{color:#f0c070;font-weight:700}
.locked{opacity:.55}.locked input,.locked button{pointer-events:none}
.lk{display:inline-block;width:13px;height:13px;margin-left:6px;vertical-align:-2px;fill:none;stroke:#f0cf86;stroke-width:2.2}
.act.lockd{opacity:.35;filter:grayscale(1);pointer-events:none}
.rhint{display:block;font-size:11px;color:var(--ink3);font-weight:500}
.savebar{font-size:12.5px;color:var(--ink2);border-radius:10px;padding:8px 11px;line-height:1.45;
  background:rgba(230,160,60,.08);border:1px solid rgba(230,160,60,.4)}
.savebar.ok{background:rgba(38,170,110,.08);border-color:rgba(38,170,110,.4)}
.savebar b{color:var(--ink)} .savebar.ok b{color:#8fe6ad}
.savebar .dt{display:inline-block;width:8px;height:8px;border-radius:50%;background:#e0a040;margin-right:6px;vertical-align:1px}
.savebar.ok .dt{background:#26aa6e}
.stp{display:flex;align-items:center;gap:10px}
.stp button{width:34px;height:34px;border-radius:10px;border:1px solid var(--line);background:#1f1f1e;color:var(--ink);font-size:18px;padding:0;cursor:pointer}
.stp b{min-width:22px;text-align:center;font-size:16px;font-variant-numeric:tabular-nums}
#mCard .frow>span:last-child{white-space:nowrap;flex:0 0 auto}
#mCard .frow>span:first-child{flex:1 1 auto;min-width:0;padding-right:8px}

/* ---------- v12.5: xeeladda (SR/SD · SMC · LABADA) + EMA filter ---------- */
.sg3{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;background:#121211;border:1px solid var(--line);border-radius:13px;padding:4px;margin:6px 0 8px}
.sg3 button{padding:10px 2px 8px;border:none;border-radius:10px;background:none;color:var(--ink2);font-family:inherit;font-size:14px;font-weight:800;line-height:1.15;cursor:pointer}
.sg3 button small{display:block;font-size:10.5px;font-weight:600;opacity:.75;margin-top:2px}
.sg3 button.on{background:linear-gradient(180deg,#f0cf86,#d9ae55);color:#15110a}
.sg3 button:disabled{opacity:.5;cursor:default}
.echip{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;font-weight:800;padding:6px 12px;border-radius:14px;border:1px solid;margin-bottom:4px}
.echip i{width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor}
.echip.up{color:#7fe0ab;background:#0f1a14;border-color:rgba(38,170,110,.6)}
.echip.dn{color:#f2a3a3;background:#1e1113;border-color:rgba(208,59,59,.6)}
.echip.mx{color:#f0c070;background:#1e180e;border-color:rgba(224,160,64,.6)}
.echip.off{color:var(--ink2);background:#171716;border-color:var(--line)}

/* ---------- v12.6: maamulka cusub ---------- */
.frow-col{display:block;padding:4px 0}
.seg-s{display:inline-flex;margin:0;width:156px}
.seg-s button{padding:7px 2px;font-size:12.5px}
#mSTRAT{margin:4px 0 2px}
.frow.off{opacity:.4}

/* ---------- v9.3: habka SL/TP ---------- */
.seg{display:flex;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:2px 0 8px}
.seg button{flex:1;padding:11px 4px;background:none;border:none;border-radius:0;border-left:1px solid var(--line);
  color:var(--ink2);font:700 13px/1.2 inherit;font-family:inherit;letter-spacing:.04em;cursor:pointer}
.seg button:first-child{border-left:none}
.seg button.on{background:var(--s1);color:#fff}
.seg button:focus-visible{outline:2px solid #9cc8fb;outline-offset:-3px}
.sltph{font-size:12.5px;color:var(--ink2);line-height:1.5;background:#171716;border:1px solid var(--line);border-radius:10px;
  padding:9px 11px;margin-bottom:6px}
.sltph b{color:var(--ink)}
.frow.off{opacity:.38;pointer-events:none}

/* ---------- v9.1: SAWIRRADA TRADE-YADA ---------- */
.flt{display:flex;gap:7px;margin-bottom:12px;overflow-x:auto}
.flt button{flex:0 0 auto;font-size:12.5px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);
  background:none;color:var(--ink2);cursor:pointer}
.flt button.on{background:var(--s1);border-color:var(--s1);color:#fff;font-weight:600}
.shr{display:flex;gap:11px;padding:11px 0;border-top:1px solid #262624;align-items:center;cursor:pointer;width:100%;
  background:none;border-left:none;border-right:none;border-bottom:none;border-radius:0;color:var(--ink);font:inherit;text-align:left}
.shr:first-child{border-top:none}
.shth{position:relative;flex:0 0 112px;height:70px;border-radius:10px;overflow:hidden;border:1px solid var(--line);background:#101218;
  display:flex;align-items:center;justify-content:center;color:var(--ink3);font-size:11px;text-align:center}
.shth img{width:100%;height:100%;object-fit:cover}
.shth .c{position:absolute;right:5px;bottom:5px;font-size:10.5px;font-weight:700;background:rgba(0,0,0,.72);border-radius:6px;padding:1px 6px;color:#ddd}
.shr .mid{flex:1;min-width:0}.shr .sy{font-weight:700;font-size:15px}.shr .sub{font-size:12px;color:var(--ink3);margin-top:2px}
.ttag{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:6px;margin-left:6px;vertical-align:1px}
.ttag.sell{background:rgba(208,59,59,.2);color:#f2a3a3}.ttag.buy{background:rgba(12,163,12,.2);color:#7fd67f}
.shr .pl{text-align:right;font-weight:700;font-size:15px;font-variant-numeric:tabular-nums;white-space:nowrap}
.shr .pl .p{font-size:11.5px;font-weight:500;color:var(--ink3)}
.shr .pl .op{color:#8fc0f5;font-size:12px}
.shv{position:fixed;inset:0;z-index:86;background:var(--plane);overflow-y:auto}
.shv[hidden]{display:none}
.shv .ch{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top)) 12px 10px;
  border-bottom:1px solid var(--line);background:#141413}
.shv .ch button{background:none;border:none;color:var(--ink);padding:8px;border-radius:10px;cursor:pointer;display:flex}
.shv .ch svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.shv .t1{font-weight:700;font-size:16px}.shv .t2{font-size:12px;color:var(--ink3)}
.shv .lbl{display:flex;justify-content:space-between;margin:16px 14px 7px;font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--ink2)}
.shv .lbl span{font-weight:500;color:var(--ink3);letter-spacing:0}
.shv .big{display:block;width:calc(100% - 28px);margin:0 14px;border-radius:12px;border:1px solid var(--line);cursor:zoom-in;background:#101218}
.shv .none{margin:0 14px;padding:26px 14px;border:1px dashed var(--line);border-radius:12px;text-align:center;color:var(--ink3);font-size:13px}
.shv .stats{margin:14px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.shv .stats div{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:9px 10px}
.shv .stats b{display:block;font-size:14.5px;font-variant-numeric:tabular-nums}.shv .stats i{font-style:normal;font-size:11px;color:var(--ink3)}
.shv .res{margin:0 14px 24px;padding:11px 13px;border-radius:12px;font-weight:650;font-size:14px}
.shv .res.w{background:rgba(63,207,111,.1);border:1px solid rgba(63,207,111,.45);color:#8fe6ad}
.shv .res.l{background:rgba(208,59,59,.1);border:1px solid rgba(208,59,59,.45);color:#f2a3a3}
.shv .res.o{background:rgba(57,135,229,.1);border:1px solid rgba(57,135,229,.45);color:#9cc8fb}

/* ---------- v9: WADA-SHEEKAYSI ---------- */
.cfab{position:fixed;right:16px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:45;width:56px;height:56px;
  border-radius:50%;border:1px solid rgba(217,174,85,.75);background:#1d1a12;color:#f0cf86;cursor:pointer;
  display:flex;align-items:center;justify-content:center;box-shadow:0 6px 22px rgba(0,0,0,.5);padding:0}
.cfab svg{width:26px;height:26px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.cfab:focus-visible{outline:2px solid #f0cf86;outline-offset:3px}
.cfab{touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}   /* v9.2: la jiidi karo */
.cfab.drag{transform:scale(1.1);box-shadow:0 14px 34px rgba(0,0,0,.7);cursor:grabbing;opacity:.96}
.cfab.snap{transition:left .2s ease,top .2s ease}
@media (prefers-reduced-motion:reduce){.cfab.snap{transition:none}.cfab.drag{transform:none}}
.cfab .n{position:absolute;top:-3px;right:-3px;min-width:21px;height:21px;padding:0 6px;border-radius:999px;
  background:#e5534b;color:#fff;font:700 11.5px/21px system-ui,sans-serif;text-align:center}
.chat{position:fixed;inset:0;z-index:85;background:var(--plane);display:flex;flex-direction:column}
.chat[hidden],.chat [hidden]{display:none!important}
.chat .ch{display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top)) 12px 10px;
  border-bottom:1px solid var(--line);background:#141413}
.chat .ch button{background:none;border:none;color:var(--ink);padding:8px;border-radius:10px;cursor:pointer;display:flex}
.chat .ch svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.chat .ch .tt{flex:1;min-width:0}
.chat .ch .t1{font-weight:700;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat .ch .t2{font-size:12px;color:var(--ink3)}
.clist{flex:1;overflow-y:auto}
.cth{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid #232322;cursor:pointer;background:none;border-radius:0;
  border-left:none;border-right:none;border-top:none;width:100%;text-align:left;color:var(--ink);font:inherit}
.cth:hover{background:#171716}
.cth .av{flex:0 0 42px;height:42px;border-radius:50%;background:#2a2620;color:#f0cf86;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:16px}
.cth .mid{flex:1;min-width:0}
.cth .nm{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cth .lm{font-size:13px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cth .rt{text-align:right;font-size:11.5px;color:var(--ink3);display:flex;flex-direction:column;gap:5px;align-items:flex-end}
.cth .un{min-width:20px;height:20px;padding:0 6px;border-radius:999px;background:#e5534b;color:#fff;font-weight:700;
  font-size:11px;line-height:20px;text-align:center}
.cmsgs{flex:1;overflow-y:auto;padding:14px 12px 8px;display:flex;flex-direction:column;gap:6px}
.cday{align-self:center;font-size:11.5px;color:var(--ink3);background:#1a1a19;border:1px solid var(--line);
  padding:3px 10px;border-radius:999px;margin:8px 0 4px}
.cm{display:flex}
.cm.me{justify-content:flex-end}
.cb{max-width:80%;border-radius:16px;padding:8px 11px 5px;background:#1f1f1e;border:1px solid #2c2c2a}
.cm.me .cb{background:#2a2414;border-color:#4a3e1e;border-bottom-right-radius:5px}
.cm.them .cb{border-bottom-left-radius:5px}
.ctext{white-space:pre-wrap;word-break:break-word;font-size:15px;line-height:1.42}
.cimg{display:block;max-width:100%;width:240px;max-height:320px;object-fit:cover;border-radius:11px;margin:2px 0 4px;
  cursor:zoom-in;background:#111}
.cgone{font-size:12.5px;color:var(--ink3);font-style:italic;margin:2px 0 4px}
.ctm{font-size:10.5px;color:var(--ink3);text-align:right;margin-top:2px;font-variant-numeric:tabular-nums}
.ctm .ck{margin-left:4px;letter-spacing:-2px}
.ctm .ck.rd{color:#5fb0ff}
.cempty{margin:auto;text-align:center;color:var(--ink3);font-size:14px;line-height:1.55;max-width:300px;padding:20px}
.cprev{display:flex;align-items:center;gap:10px;padding:8px 12px 0}
.cprev[hidden]{display:none}
.cprev img{width:64px;height:64px;object-fit:cover;border-radius:10px;border:1px solid var(--line)}
.cprev button{background:#2a2a28;border:1px solid var(--line);color:var(--ink);border-radius:999px;padding:6px 12px;cursor:pointer}
.ccomp{display:flex;align-items:flex-end;gap:8px;padding:10px 10px calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line);background:#141413}
.ccomp textarea{flex:1;resize:none;min-height:42px;max-height:120px;border-radius:21px;border:1px solid var(--line);
  background:#1c1c1b;color:var(--ink);padding:10px 14px;font:15px/1.4 inherit;font-family:inherit}
.ccomp .ib{flex:0 0 42px;height:42px;border-radius:50%;border:1px solid var(--line);background:#1c1c1b;color:var(--ink2);
  display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0}
.ccomp .ib.send{background:#d9ae55;border-color:#d9ae55;color:#1a1408}
.ccomp .ib:disabled{opacity:.45}
.ccomp svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.cerr{padding:6px 14px 0;color:#f2a3a3;font-size:12.5px}
.cerr:empty{display:none}
.iview{position:fixed;inset:0;z-index:95;background:rgba(0,0,0,.94);display:flex;align-items:center;justify-content:center;padding:16px}
.iview[hidden]{display:none}
.iview img{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px}

/* ---------- v8.2: rakibidda app-ka (sheet) ---------- */
.edit.inst{border-color:rgba(217,174,85,.7);color:#f0cf86;background:rgba(217,174,85,.14)}
.isheet{position:fixed;inset:0;z-index:90;display:flex;align-items:flex-end;justify-content:center;
  background:rgba(0,0,0,.62)}
.isheet[hidden]{display:none}
.isheet .box{width:100%;max-width:520px;background:#16181e;border:1px solid #2b2f3a;border-bottom:none;
  border-radius:20px 20px 0 0;padding:20px 18px calc(20px + env(safe-area-inset-bottom));
  max-height:88vh;overflow:auto}
.isheet h3{margin:0 0 4px;font-size:19px}
.isheet .lead{color:var(--ink2);font-size:13.5px;margin:0 0 14px;line-height:1.5}
.isheet ol{margin:0;padding-left:0;list-style:none;display:flex;flex-direction:column;gap:10px;counter-reset:st}
.isheet li{counter-increment:st;display:flex;gap:12px;align-items:flex-start;font-size:15px;line-height:1.45}
.isheet li::before{content:counter(st);flex:0 0 26px;height:26px;border-radius:50%;background:rgba(217,174,85,.18);
  color:#f0cf86;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center;margin-top:1px}
.isheet .k{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:24px;padding:0 6px;
  border:1px solid #3a3f4c;border-radius:7px;background:#20232b;color:#f0cf86;font-weight:700;font-size:14px;vertical-align:-3px}
.isheet .diag{margin-top:16px;padding-top:12px;border-top:1px solid #2b2f3a;font:12px/1.6 ui-monospace,Menlo,Consolas,monospace;color:var(--ink3)}
.isheet .diag b{font-weight:600}
.isheet .diag .y{color:#6fd49c}.isheet .diag .n{color:#ef8a82}
.isheet .close{margin-top:16px;width:100%;padding:13px;border-radius:12px;font-weight:700}

/* ---------- v7.2: CAAFIMAADKA BOT-KA ---------- */
.hc{display:flex;gap:11px;align-items:flex-start;padding:11px 12px;border-radius:12px;
  margin-bottom:9px;border:1px solid var(--line);background:#17181c}
.hc i{flex:0 0 10px;height:10px;border-radius:50%;margin-top:5px;background:#6b6e78}
.hc.red{border-color:rgba(208,59,59,.55);background:rgba(208,59,59,.09)}
.hc.red i{background:#e5534b}
.hc.amb{border-color:rgba(230,160,60,.5);background:rgba(230,160,60,.08)}
.hc.amb i{background:#e0a040}
.hc.ok{border-color:rgba(38,170,110,.45);background:rgba(38,170,110,.07)}
.hc.ok i{background:#26aa6e}
.hc .ht{font-size:13.5px;font-weight:650;line-height:1.35}
.hc .hd{font-size:12.3px;color:var(--ink2);margin-top:3px;line-height:1.45}
.tbadge{position:absolute;top:5px;margin-left:16px;min-width:17px;height:17px;padding:0 5px;
  border-radius:999px;background:#e5534b;color:#fff;font-size:10.5px;font-weight:700;
  line-height:17px;text-align:center}
.appbar button{position:relative}
/* v12.21: Muusig + Fariimo nav-ka hoose · badhanka wareegsan waa la saaray */
.cfab{display:none!important}
.appbar button{min-width:0;padding-left:0;padding-right:0;white-space:nowrap}
@media(max-width:430px){.appbar button{font-size:9.6px;letter-spacing:0}.appbar svg{width:20px;height:20px}}
.appbar .nic{position:relative;display:block;line-height:0}
.appbar #navMus.play{color:#f472b6}
.appbar .neq{position:absolute;right:-8px;top:-3px;display:none;align-items:flex-end;gap:1px;height:9px}
.appbar #navMus.play .neq{display:flex}
.appbar .neq i{display:block;width:2px;background:#f472b6;border-radius:1px;animation:neq .9s ease-in-out infinite}
.appbar .neq i:nth-child(2){animation-delay:.3s}.appbar .neq i:nth-child(3){animation-delay:.6s}
@keyframes neq{0%,100%{height:3px}50%{height:9px}}
@media (prefers-reduced-motion:reduce){.appbar .neq i{animation:none;height:7px}}
.appbar .nbd{position:absolute;top:-6px;right:-10px;min-width:16px;height:16px;padding:0 4px;border-radius:999px;
  background:#e5534b;color:#fff;font:700 9.5px/16px system-ui,sans-serif;text-align:center}
.appbar .nbd[hidden]{display:none}
.chtoast{position:fixed;left:12px;right:12px;z-index:44;bottom:calc(72px + env(safe-area-inset-bottom));display:flex;align-items:center;gap:9px;
  padding:10px 10px 10px 13px;border-radius:14px;background:#1d1a12;border:1px solid rgba(217,174,85,.75);color:#eceae6;font-size:13px;
  box-shadow:0 10px 28px rgba(0,0,0,.6)}
.chtoast[hidden]{display:none!important}
.chtoast .tx{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chtoast b{color:#f0cf86}
.chtoast button{border:none;border-radius:999px;padding:6px 13px;font:inherit;font-size:12px;font-weight:800;cursor:pointer;background:#f0cf86;color:#1d1a12}
.chtoast button.x{background:none;color:#a8a294;padding:6px 8px}
@media(min-width:900px){.chtoast{left:auto;width:420px;right:16px}}

/* ---------- v7: ANALIIS ---------- */
.zc{border:1px solid var(--line);border-radius:14px;padding:13px 14px;margin-bottom:11px;
  background:#17181c}
.zc.in{border-color:rgba(38,170,110,.55);background:rgba(38,170,110,.07)}
.zc.near{border-color:rgba(57,135,229,.5);background:rgba(57,135,229,.06)}
.zc.signal{border-color:rgba(38,170,110,.9);background:rgba(38,170,110,.13)}
.zc.news{border-color:rgba(208,59,59,.55);background:rgba(208,59,59,.07)}
.zc .zh{display:flex;align-items:center;justify-content:space-between;gap:10px}
.zc .zs{font-size:17px;font-weight:700;letter-spacing:.02em}
.zbadge{font-size:10.5px;font-weight:700;letter-spacing:.06em;padding:5px 10px;
  border-radius:999px;white-space:nowrap;background:#2a2c33;color:var(--ink2)}
.zbadge.in,.zbadge.signal{background:rgba(38,170,110,.22);color:#7fe0ab}
.zbadge.near{background:rgba(57,135,229,.22);color:#9cc8fb}
.zbadge.none{background:rgba(236,131,90,.2);color:#f0b08f}
.zbadge.news{background:rgba(208,59,59,.22);color:#f2a3a3}
.zc .zl{font-size:13px;color:var(--ink2);margin:7px 0 10px;font-variant-numeric:tabular-nums}
.ztr{height:8px;border-radius:5px;background:#26272c;overflow:hidden}
.ztr i{display:block;height:100%;border-radius:5px;background:var(--s1)}
.ztr i.in{background:#26aa6e} .ztr i.far{background:#4b4d55}
.zft{display:flex;justify-content:space-between;gap:10px;margin-top:6px;
  font-size:11.5px;color:var(--ink3);font-variant-numeric:tabular-nums}
.zwhy{margin-top:10px;padding-top:9px;border-top:1px solid #26272c;
  font-size:12.5px;color:#e0a86a;font-weight:600;line-height:1.45}
.zc.signal .zwhy{color:#7fe0ab}
.tsub{font-size:11px;color:var(--ink3);margin-top:3px;line-height:1.45;font-variant-numeric:tabular-nums;min-width:118px}
.tsub span{white-space:nowrap}
.zmeta{margin-top:6px;font-size:11.8px;color:var(--ink3);line-height:1.45}
.nv{display:flex;align-items:center;justify-content:space-between;gap:10px;
  border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:9px}
.nv .nt{font-size:13.5px;font-weight:600}
.nv .nm{font-size:12px;color:var(--ink3);margin-top:3px;letter-spacing:.04em}
.nv .nc{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.nv .nc.soon{color:#e8756f}
.nwarn{border:1px solid rgba(208,59,59,.5);background:rgba(208,59,59,.1);color:#f2a3a3;
  border-radius:12px;padding:11px 13px;font-size:12.5px;line-height:1.45}
@media(min-width:900px){.cols.two{grid-template-columns:1fr}}
/* ---------- v12.23: 🖼 sawirka shaashadda oo dhan · ⏻ MT5 SHID/DAMI · ✕ XIDH · 🎵 player la jiidi karo ---------- */
.hero.hfull{min-height:calc(100vh - 66px);min-height:calc(100svh - 66px - env(safe-area-inset-bottom));padding:70px 14px 14px;border-bottom:none}
.hero.hfull .hero-fade{background:linear-gradient(180deg,rgba(10,8,18,.5) 0%,rgba(10,8,18,0) 16%,rgba(10,8,18,0) 40%,rgba(14,11,22,.78) 64%,rgba(16,12,24,.97) 100%)}
.hfull #btnPic span{display:none}
.hfull #btnPic{padding:9px;border-radius:50%}
.hmb{width:38px;height:38px;padding:0;justify-content:center;border-radius:50%;font-size:18px;font-weight:900;letter-spacing:1px}
.hfull .hero-in{gap:0}
.hacc{display:inline-flex;gap:6px;font-size:11.5px;font-weight:800;padding:4px 10px;border-radius:999px;background:rgba(15,12,24,.55);border:1px solid rgba(255,255,255,.18);color:#d8d2e6;margin-bottom:6px}
.hfull h1{font-size:34px}
.hst{display:flex;align-items:center;gap:7px;font-size:12.5px;color:#d8d2e6;margin-top:4px;line-height:1.45;flex-wrap:wrap}
.hst .dot{flex:0 0 auto;margin-top:5px}
.hst{align-items:flex-start}.hst #st{flex:1;min-width:0}
.hstats{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;width:100%;margin-top:12px}
.hstats div{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.13);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-radius:13px;padding:8px 10px;min-width:0}
.hstats small{display:block;font-size:9.5px;letter-spacing:.12em;color:#b9b3c8;font-weight:800}
.hstats b,.hstats b.v{display:block;font-size:16px;font-weight:800;margin:2px 0 0;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hctl{display:grid;grid-template-columns:1.45fr 1fr;gap:9px;width:100%;margin-top:11px;align-items:center}
.mtsw{position:relative;height:68px;border-radius:999px;padding:0;border:none;cursor:pointer;background:linear-gradient(135deg,#fff1b8,#e8b44a 30%,#8a5a12 55%,#f6d58b 80%,#a8741c);box-shadow:0 0 18px rgba(240,180,60,.45),0 6px 18px rgba(0,0,0,.6);touch-action:pan-y;-webkit-tap-highlight-color:transparent}
.mtsw:before{content:"";position:absolute;inset:3px;border-radius:999px;background:radial-gradient(ellipse at 50% 30%,#2a2418,#0b0907 75%);box-shadow:inset 0 0 0 2px rgba(255,215,120,.35),inset 0 4px 14px rgba(0,0,0,.9)}
.mtsw .knob{position:absolute;top:2px;left:calc(100% - 66px);width:64px;height:64px;border-radius:50%;z-index:2;background:#0b0907 center/cover no-repeat;box-shadow:0 0 0 2px rgba(34,197,94,.95),0 0 22px rgba(34,197,94,.75);transition:left .32s cubic-bezier(.3,1.4,.5,1),box-shadow .3s,filter .3s}
.mtsw.off .knob{left:2px;box-shadow:0 0 0 2px rgba(239,68,68,.95),0 0 22px rgba(239,68,68,.6);filter:saturate(.55) brightness(.85)}
.mtsw.na .knob{left:calc(50% - 32px);box-shadow:0 0 0 2px rgba(160,160,170,.7);filter:grayscale(1) brightness(.7)}
.mtsw .lbl{position:absolute;z-index:2;top:50%;transform:translateY(-50%);left:22px;right:76px;text-align:left;line-height:1.2;pointer-events:none}
.mtsw.off .lbl{left:76px;right:18px;text-align:right}
.mtsw.na .lbl{left:14px;right:14px;text-align:center;opacity:0}
.mtsw .lbl b{display:block;font-size:15px;letter-spacing:.1em;color:#86efac;text-shadow:0 0 10px rgba(34,197,94,.6)}
.mtsw.off .lbl b{color:#fca5a5;text-shadow:0 0 10px rgba(239,68,68,.5)}
.mtsw .lbl small{display:block;font-size:10px;color:#d9c79b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mtsw.pend .knob{animation:mtpulse 1s ease-in-out infinite}
@keyframes mtpulse{50%{box-shadow:0 0 0 2px #f0cf86,0 0 30px rgba(240,207,134,.9)}}
@media (prefers-reduced-motion:reduce){.mtsw .knob{transition:none}.mtsw.pend .knob{animation:none}}
.mtsw:disabled{cursor:default;opacity:.55}
.mtsw:focus-visible,.hxid:focus-visible{outline:2px solid #f0cf86;outline-offset:3px}
.hxid{display:flex;align-items:center;gap:9px;height:68px;padding:0 12px;border-radius:18px;cursor:pointer;color:#fff;text-align:left;font:inherit;background:linear-gradient(120deg,rgba(249,115,22,.2),rgba(20,16,28,.88));border:1px solid rgba(249,115,22,.55);min-width:0}
.hxid .xi{width:44px;height:44px;flex:0 0 44px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:19px;font-weight:900;color:#fdba74;border:2px solid #f97316;background:#22150c}
.hxid .pt{min-width:0}.hxid .pt b{display:block;font-size:14px;letter-spacing:.08em;color:#fdba74}
.hxid .pt small{display:block;font-size:10.5px;color:#d6c9bd;white-space:pre-line;line-height:1.3}
.hxid:disabled{opacity:.45;cursor:default}
.hmenu{position:fixed;top:58px;right:12px;z-index:70;width:230px;display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:16px;background:#181420;border:1px solid #3a3150;box-shadow:0 18px 40px rgba(0,0,0,.65)}
.hmenu[hidden]{display:none!important}
.hmenu .spacer{display:none}
.hmenu select{width:100%!important}
.hmenu .pill{justify-content:flex-start}
.hmenu .btn{display:block;width:100%;text-align:left;padding:10px 12px;border-radius:10px}
.cfm{position:fixed;inset:0;z-index:95;display:flex;flex-direction:column;justify-content:flex-end;background:rgba(0,0,0,.5)}
.cfm[hidden]{display:none!important}
.cfm .sh{background:#17131f;border-top:1px solid #3a3150;border-radius:22px 22px 0 0;padding:14px 16px calc(18px + env(safe-area-inset-bottom));max-width:560px;width:100%;margin:0 auto}
.cfm .gr{width:40px;height:4px;border-radius:2px;background:#3a3150;margin:0 auto 12px}
.cfm h4{font-size:17px;margin:0}
.cfm .bd{font-size:13px;color:#c9c3d8;line-height:1.55;margin-top:8px}
.cfm .ls{display:flex;flex-direction:column;gap:5px;margin-top:10px;max-height:34vh;overflow-y:auto}
.cfm .ls span{font-size:12.5px;background:#211c2e;border:1px solid #3a3150;border-radius:10px;padding:7px 10px;display:flex;justify-content:space-between;gap:8px}
.cfm .bt{display:grid;grid-template-columns:1.3fr 1fr;gap:8px;margin-top:14px}
.cfm .bt button{padding:13px;border-radius:14px;font:inherit;font-weight:900;font-size:14px;cursor:pointer}
.cfm .bt .d{background:rgba(239,68,68,.2);border:1px solid #ef4444;color:#fecaca}
.cfm .bt .o{background:rgba(249,115,22,.2);border:1px solid #f97316;color:#fed7aa}
.cfm .bt .c{background:#211c2e;border:1px solid #3a3150;color:#d8d2e6}
.cfm .alt{display:block;width:100%;margin-top:10px;background:none;border:none;color:#93c5fd;font:inherit;font-weight:800;font-size:13px;cursor:pointer;padding:6px}
.cfm .nt{font-size:12px;margin-top:8px;color:#f6d58b;min-height:1px}
/* 🎵 player la jiidi karo · la qarin karo */
.mubar{padding-left:2px}
.muhd{flex:0 0 22px;align-self:stretch;display:flex;align-items:center;justify-content:center;color:#b58aa8;font-size:16px;letter-spacing:-3px;cursor:grab;touch-action:none;border-right:1px solid #3a2b3a;user-select:none;-webkit-user-select:none}
.mubar.drag{box-shadow:0 22px 44px rgba(0,0,0,.8),0 0 0 2px rgba(236,72,153,.6);transform:scale(1.02)}
.mubar.drag .muhd{cursor:grabbing}
.mux{position:absolute;top:-13px;width:28px;height:28px;border-radius:50%;padding:0;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:900;cursor:pointer;background:#2a2230;box-shadow:0 4px 12px rgba(0,0,0,.6)}
.mux.mn{right:40px;border:2px solid #f472b6;color:#f9a8d4}
.mux.cl{right:6px;border:2px solid #6b5a72;color:#e2d5e6}
.mububl{position:fixed;z-index:41;width:62px;height:62px;border-radius:50%;cursor:pointer;touch-action:none;user-select:none;-webkit-user-select:none;background:radial-gradient(circle,#f0cf86 0 7%,#1a1a1a 8% 30%,#2c2c2c 31% 33%,#151515 34% 100%) center/cover no-repeat;border:3px solid #ec4899;box-shadow:0 0 0 6px rgba(236,72,153,.2),0 10px 24px rgba(0,0,0,.7)}
.mububl[hidden]{display:none!important}
.mububl.drag{transform:scale(1.1)}
.mububl .eq{position:absolute;right:-5px;bottom:-5px;width:24px;height:24px;border-radius:50%;background:#ec4899;display:flex;gap:2px;align-items:flex-end;justify-content:center;padding-bottom:6px;z-index:3}
.mububl .eq i{width:3px;height:4px;background:#fff;border-radius:1px}
.mububl.play .eq i{animation:neq .9s ease-in-out infinite}.mububl.play .eq i:nth-child(2){animation-delay:.3s}.mububl.play .eq i:nth-child(3){animation-delay:.6s}
.muhost.bub{border-radius:50%}
.muhost.mini{pointer-events:none}   /* saxanka / sawirka yar: taabashadu ha gaadho bar-ka */
.hero.hfull.hoff .hero-photo,.hero.hfull.hoff .hero-ph{filter:saturate(.3) brightness(.7);transition:filter .4s}
.mtsw .knob{background-image:url("data:image/webp;base64,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")}

/* ---------- v12.22: 💱 LAMAANAHA + 🎯 ZONE YAR (EA v70.8) ---------- */
.prstrip{display:flex;gap:6px;overflow-x:auto;margin:0 0 14px;padding:7px;border-radius:14px;background:var(--surface);border:1px solid var(--line);scrollbar-width:none}
.prstrip::-webkit-scrollbar{display:none}
.prstrip[hidden]{display:none!important}
.prc{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:7px 10px;border-radius:10px;border:1px solid var(--line);background:#17181c;color:var(--ink);font:inherit;font-size:12.5px;font-weight:750;cursor:pointer;white-space:nowrap}
.prc i{width:8px;height:8px;border-radius:50%;background:#6b7280;flex:0 0 auto}
.prc.x2 i{background:#22c55e}.prc.x1 i{background:#facc15}.prc.x3 i{background:#f97316}.prc.x4 i{background:#ef4444}
.prc em{font-style:normal;font-weight:700;font-size:11.5px;color:var(--ink3)}.prc em.p{color:#4ade80}.prc em.n{color:#f87171}
.prc.on{border-color:var(--s1);background:rgba(57,135,229,.18)}
.prc.add{color:#f0cf86;font-weight:900;padding:7px 12px}
.prflt{display:flex;align-items:center;gap:8px;margin:-6px 2px 12px;font-size:12px;color:var(--ink2)}
.prflt[hidden]{display:none!important}
.prflt button{background:none;border:1px solid var(--line);color:var(--ink2);border-radius:999px;padding:3px 10px;font:inherit;font-size:11.5px;cursor:pointer}
.znc{position:relative;background:linear-gradient(180deg,#1b1a1f,#0f0f12);border:1px solid rgba(239,68,68,.35)}
.znc[hidden]{display:none!important}
.znh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.znh b{font-size:13px;letter-spacing:.12em;color:#f0cf86}
.znh .sp{flex:1}
.znp{font-size:11px;font-weight:800;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--ink2)}
.znh .seg{margin:0;width:auto}
.znh .seg button{padding:6px 10px;font-size:12px}
.znst{margin-top:10px;border-radius:12px;padding:9px 11px;font-weight:850;font-size:14px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--ink)}
.znst small{display:block;font-weight:500;font-size:11.5px;margin-top:3px;color:var(--ink2);line-height:1.4}
.znst.s2{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.5);color:#fca5a5}
.znst.s3{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.45);color:#86efac}
.znst.s4{background:rgba(250,178,25,.08);border-color:rgba(250,178,25,.4);color:#f6d58b}
.znst.s0{color:var(--ink3)}
.znlad{display:block;width:100%;margin-top:8px}
.znmeta{font-size:11.5px;color:var(--ink2);margin-top:4px;line-height:1.5}
.znrj{margin-top:8px;font-size:11.5px;color:#f2a3a3;line-height:1.45;border-top:1px dashed #2b2d33;padding-top:7px}
.znrj[hidden]{display:none}
.prcard .prh{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.prcard .prn{font-size:12px;color:var(--ink2);font-weight:700}
.prrow{border-bottom:1px solid #26272b;padding:9px 0}
.prrow:last-child{border-bottom:none}
.prr{display:flex;align-items:center;gap:10px;cursor:pointer}
.prr .fl{flex:0 0 34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:17px;background:#22242a}
.prr .nm{flex:1;min-width:0}
.prr .nm b{display:block;font-size:14px}
.prr .nm small{display:block;font-size:11.5px;color:var(--ink3);line-height:1.45}
.prr .tg{display:inline-block;margin:4px 4px 0 0;font-size:10px;font-weight:800;padding:2px 7px;border-radius:999px;background:rgba(57,135,229,.16);color:#9cc8fb}
.prr .crown{font-size:11px;font-weight:800;color:#f0cf86;border:1px solid rgba(240,207,134,.5);border-radius:999px;padding:3px 9px}
.prx2{color:#4ade80}.prx1{color:#facc15}.prx3{color:#fb923c}.prx4{color:#f87171}.prx0{color:var(--ink3)}
.pred{margin:8px 0 2px 44px;padding:10px;border-radius:12px;background:#141518;border:1px solid var(--line)}
.pred[hidden]{display:none}
.pred .l{font-size:11.5px;color:var(--ink2);font-weight:700;margin:6px 0 4px}
.pred .seg{margin:0 0 4px}
.pred .seg button{padding:8px 2px;font-size:12px}
.pred .rm{margin-top:8px;background:none;border:1px solid rgba(208,59,59,.5);color:#f2a3a3;border-radius:10px;padding:7px 12px;font:inherit;font-size:12px;cursor:pointer}
.pradd{display:block;width:100%;margin-top:8px;padding:10px;border:1px dashed #3a3f48;border-radius:12px;background:none;color:#f0cf86;font:inherit;font-size:13px;font-weight:800;cursor:pointer}
.pradd:disabled{opacity:.4;cursor:default}
.prsh{margin-top:10px;padding:10px;border-radius:12px;border:1px solid var(--line);background:#141518}
.prsh[hidden]{display:none}
.prsh input{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:#0e0f12;color:var(--ink);font:inherit;font-size:14px}
.prres{max-height:260px;overflow-y:auto;margin-top:8px}
.prres button{display:flex;align-items:center;gap:10px;width:100%;padding:9px 6px;background:none;border:none;border-bottom:1px solid #24262c;color:var(--ink);font:inherit;font-size:13.5px;font-weight:700;cursor:pointer;text-align:left}
.prres button span{flex:1}
.prres button i{font-style:normal;color:#f0cf86;font-size:18px}
.prres .none{padding:12px 4px;color:var(--ink3);font-size:12.5px}
.prwarn{margin-top:10px;border:1px solid rgba(250,178,25,.4);background:rgba(250,178,25,.07);border-radius:12px;padding:9px 11px;font-size:12px;color:#f6d58b;line-height:1.5}
.prhelp{margin-top:10px;border:1px solid var(--line);border-radius:12px;background:#141518}
.prhelp summary{padding:10px 12px;font-size:13px;font-weight:700;color:var(--ink2);cursor:pointer}
.prhelp ol{margin:0;padding:0 14px 12px 32px;font-size:12.5px;line-height:1.65;color:var(--ink2)}
.prhelp b{color:#fff}

/* ---------- v12.25: 🪜 GRID STOP (EA v71.0) ---------- */
.grc{background:linear-gradient(180deg,#1a1820,#0f0f12);border:1px solid rgba(240,207,134,.32)}
.grc[hidden]{display:none!important}
.grh{display:flex;align-items:center;gap:8px}
.grh b{font-size:13px;letter-spacing:.12em;color:#f0cf86}.grh .sp{flex:1}
.grh .sw{width:52px;height:28px}.grh .sw i{width:22px;height:22px}.grh .sw.on i{left:27px}
.grkeli{font-size:10.5px;font-weight:850;padding:3px 8px;border-radius:999px;border:1px solid rgba(240,207,134,.5);color:#f0cf86}
.grst{margin-top:10px;border-radius:12px;padding:9px 11px;font-weight:850;font-size:14px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--ink)}
.grst small{display:block;font-weight:500;font-size:11.5px;margin-top:3px;color:var(--ink2);line-height:1.45}
.grst.s{background:rgba(239,68,68,.11);border-color:rgba(239,68,68,.5);color:#fca5a5}
.grst.b{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.45);color:#86efac}
.grst.a{background:rgba(250,178,25,.08);border-color:rgba(250,178,25,.4);color:#f6d58b}
.grst.o{color:var(--ink3)}
.grkp{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:9px}
.grkp div{background:#15161b;border:1px solid #24262d;border-radius:11px;padding:7px 9px;min-width:0}
.grkp small{display:block;font-size:9.5px;color:var(--ink3);letter-spacing:.07em}.grkp b{font-size:15px;white-space:nowrap}
.grkp .p{color:#4ade80}.grkp .n{color:#f87171}
.grlad{display:flex;gap:3px;margin-top:9px}.grlad i{flex:1;height:18px;border-radius:4px;background:#2a2c33;border:1px dashed #4a4d55}
.grlad i.f{border:none}.grlad.s i.f{background:#e5484d}.grlad.b i.f{background:#22c55e}
.grll{display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink3);margin-top:4px}
.grper{margin-top:12px}
.greq{margin-top:9px;background:#15161b;border:1px solid #24262d;border-radius:12px;padding:7px 8px}
.greq small{font-size:10px;color:var(--ink3);letter-spacing:.07em}.greq svg{display:block;width:100%;height:64px}
.grhl{margin-top:8px}
.grhr{display:grid;grid-template-columns:58px 1fr auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid #1f2227;font-size:12px}
.grhr:last-child{border-bottom:none}
.grhr .d{font-weight:800;font-size:11px;padding:3px 6px;border-radius:7px;text-align:center}
.grhr .d.s{background:rgba(239,68,68,.15);color:#fca5a5}.grhr .d.b{background:rgba(34,197,94,.15);color:#86efac}
.grhr small{display:block;color:var(--ink3);font-size:10.5px}.grhr b.p{color:#4ade80}.grhr b.n{color:#f87171}
.grnone{font-size:12px;color:var(--ink3);padding:8px 0}
.grrj{margin-top:8px;font-size:11.5px;color:var(--ink3);line-height:1.5}

/* ---------- v12.24: 📏 CABBIR LAMAANE (EA v70.9) ---------- */
.prc u{text-decoration:none;font-size:10.5px;font-weight:850;color:#f0cf86}
.prr .tg.tk{background:rgba(250,204,21,.14);color:#fde68a}
.prr .tg.bk{background:rgba(34,197,94,.14);color:#86efac}
.prr .tg.gr{background:rgba(240,207,134,.12);color:#f0cf86;border:1px solid rgba(240,207,134,.35)}
.prtg{flex-wrap:wrap}.prtg button{flex:1 1 30%}
.prr .tg.cb{background:rgba(240,207,134,.16);color:#f0cf86}
.prtg{display:flex;gap:8px;margin:0 0 4px}
.prtg button{flex:1;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 10px;border-radius:10px;border:1px solid var(--line);background:none;color:var(--ink2);font:inherit;font-size:12.5px;font-weight:800;cursor:pointer;white-space:nowrap;min-width:0}
.prtg button.on{color:var(--ink);border-color:rgba(38,170,110,.6);background:rgba(38,170,110,.08)}
.prtg button:disabled{opacity:.5;cursor:default}
.prsw{position:relative;flex:0 0 36px;width:36px;height:20px;border-radius:999px;background:#3a3c44;transition:background .15s}
.prsw::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#f2f2f0;transition:left .15s}
.prtg button.on .prsw{background:#26aa6e}.prtg button.on .prsw::after{left:18px}
html.th .prtg button.on .prsw{background:var(--acc)}
.prcab{margin-top:8px;border-radius:12px;background:rgba(240,207,134,.06);border:1px solid rgba(240,207,134,.38);padding:9px 10px}
.prcab .h{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12.5px;font-weight:900;color:#f0cf86;letter-spacing:.04em}
.prcab .h b{font-size:20px;letter-spacing:0}
.prcab .s{font-size:11.5px;color:#cdbf97;margin-top:3px;line-height:1.45}
.prcab .s.w{color:#f6d58b}
.prmini{display:grid;grid-template-columns:1fr auto auto;gap:3px 10px;margin-top:7px;font-size:11.5px;font-variant-numeric:tabular-nums}
.prmini i{font-style:normal;color:var(--ink2)}.prmini em{font-style:normal;color:var(--ink3);text-align:right;text-decoration:line-through}.prmini b{text-align:right;color:var(--ink)}
.prcm{display:flex;gap:6px;margin-top:8px;align-items:stretch}
.prcm button{flex:1;padding:7px 4px;border-radius:9px;border:1px solid var(--line);background:none;color:var(--ink2);font:inherit;font-size:12px;font-weight:700;cursor:pointer}
.prcm button.on{border-color:#f0cf86;color:#f0cf86;background:rgba(240,207,134,.1);font-weight:850}
.prcm button:disabled{opacity:.5;cursor:default}
.prcmx{display:flex;align-items:center;gap:6px;margin-top:7px;font-size:12px;color:var(--ink2)}
.prcmx[hidden]{display:none}
.prcmx input{width:110px;padding:7px 9px;border-radius:9px;border:1px solid var(--line);background:#0e0f12;color:var(--ink);font:inherit;font-size:13px}
.prtk{margin-top:6px;font-size:11.5px;color:var(--ink3);line-height:1.45}
.cbpairs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.cbpairs span{font-size:11.5px;font-weight:800;padding:3px 8px;border-radius:999px;border:1px solid var(--line);color:var(--ink2)}
.cbpairs span b{color:#f0cf86}
.cbgo{margin-top:8px;background:none;border:1px solid rgba(240,207,134,.5);color:#f0cf86;border-radius:10px;padding:8px 12px;font:inherit;font-size:12.5px;font-weight:800;cursor:pointer}

/* ---------- v12.20: 🎵 MUUSIG ---------- */
.edit.mus{border-color:rgba(236,72,153,.6);color:#f9a8d4;background:rgba(236,72,153,.13)}
body.mu-on{padding-bottom:calc(136px + env(safe-area-inset-bottom))}
.mubar{position:fixed;left:8px;right:8px;z-index:41;bottom:calc(64px + env(safe-area-inset-bottom));height:62px;
  display:flex;align-items:center;gap:9px;padding:4px 8px 4px 4px;border-radius:14px;
  background:linear-gradient(90deg,rgba(40,22,40,.97),rgba(22,22,26,.97));border:1px solid rgba(236,72,153,.38);
  box-shadow:0 8px 26px rgba(0,0,0,.55);backdrop-filter:blur(10px)}
.mubar[hidden]{display:none!important}
.mubar .art{flex:0 0 96px;height:54px;border-radius:10px;background:#16161a center/cover no-repeat;display:flex;
  align-items:center;justify-content:center;font-size:22px;overflow:hidden;cursor:pointer}
.mubar .mi{flex:1;min-width:0;cursor:pointer}
.mubar .mi b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#fff}
.mubar .mi small{display:block;font-size:11px;color:#c9a3bb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mubar .pb{height:3px;border-radius:2px;background:#3a2b36;margin-top:5px;overflow:hidden}
.mubar .pb i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ec4899,#f0cf86)}
.mubtn{flex:0 0 auto;width:36px;height:36px;border-radius:50%;border:1px solid #3a3440;background:#1d1a22;color:#f3e8ef;
  font-size:15px;line-height:1;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0}
.mubtn.p{width:42px;height:42px;background:#ec4899;border-color:#ec4899;color:#fff;font-size:17px}
.mubtn.on{border-color:#f0cf86;color:#f0cf86;background:rgba(240,207,134,.12)}
.mubtn:disabled{opacity:.35;cursor:default}
@media(max-width:380px){.mubar .art{flex-basis:72px;height:42px}.mubar .mubtn:not(.p){width:32px;height:32px}}
@media(min-width:900px){.mubar{left:auto;width:520px;right:16px}}

.musheet{position:fixed;inset:0;z-index:86;background:var(--plane);display:flex;flex-direction:column}
.musheet[hidden]{display:none!important}
.musheet .mh{display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top)) 12px 8px;
  border-bottom:1px solid var(--line);background:#141413}
.musheet .mh b{flex:1;font-size:16px}
.musheet .mh button{background:none;border:1px solid var(--line);color:var(--ink2);border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
.musheet .mtop{padding:10px 12px 8px;border-bottom:1px solid var(--line);background:var(--plane)}
.museg{display:flex;gap:6px}
.museg button{flex:1;padding:9px 4px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink2);
  font:inherit;font-size:13px;font-weight:650;cursor:pointer;white-space:nowrap}
.museg button.on{background:rgba(236,72,153,.16);border-color:#ec4899;color:#fff}
.museg button i{font-style:normal;font-size:11px;color:var(--ink3);margin-left:3px}
.muslot{position:relative;margin-top:10px;width:100%;aspect-ratio:16/9;max-height:34vh;border-radius:12px;background:#111114;
  border:1px solid var(--line);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:6px;color:var(--ink3);font-size:13px;text-align:center;padding:10px}
.muslot.sp{aspect-ratio:auto;height:152px}
.muslot.am{aspect-ratio:auto;height:252px}
.muslot.lo{aspect-ratio:auto;height:118px}
.muslot .big{font-size:30px}
.muctl{display:flex;justify-content:center;gap:10px;margin-top:9px}
.musheet .mscr{flex:1;overflow-y:auto;padding:4px 12px calc(28px + env(safe-area-inset-bottom));-webkit-overflow-scrolling:touch}
.muh{font-size:10.5px;font-weight:800;letter-spacing:.13em;color:#c99a3f;margin:16px 2px 8px;display:flex;align-items:center;gap:8px}
.muh span{flex:1}
.muh button{background:none;border:1px solid var(--line);color:var(--ink2);border-radius:999px;padding:3px 10px;font:inherit;font-size:11px;letter-spacing:0;font-weight:600;cursor:pointer}
.muh button.on{border-color:#f0cf86;color:#f0cf86}
.mulist{display:flex;flex-direction:column;gap:6px}
.muit{display:flex;align-items:center;gap:10px;padding:8px 9px;border-radius:12px;background:var(--surface);border:1px solid var(--line);cursor:pointer}
.muit.now{border-color:#ec4899;background:rgba(236,72,153,.1)}
.muit .th{flex:0 0 56px;height:36px;border-radius:7px;background:#222 center/cover no-repeat;display:flex;align-items:center;justify-content:center;font-size:16px;color:var(--ink3)}
.muit .tt{flex:1;min-width:0}
.muit .tt b{display:block;font-size:13.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.muit .tt small{display:block;font-size:11.5px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.musrc{flex:0 0 auto;font-size:10px;font-weight:800;letter-spacing:.04em;padding:3px 7px;border-radius:999px}
.musrc.yt{background:rgba(239,68,68,.15);color:#fca5a5}.musrc.sp{background:rgba(34,197,94,.15);color:#86efac}
.musrc.am{background:rgba(251,146,60,.15);color:#fdba74}.musrc.lo{background:rgba(240,207,134,.15);color:#f0cf86}
.muit .ed{display:none;gap:4px}.mulist.edit .muit .ed{display:flex}.mulist.edit .musrc{display:none}
.muit .ed button{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:#222226;color:var(--ink2);font-size:13px;cursor:pointer;padding:0}
.muit .ed button.x{color:#f2a3a3;border-color:rgba(208,59,59,.5)}
.muempty{padding:18px 12px;border:1px dashed var(--line);border-radius:12px;text-align:center;color:var(--ink3);font-size:13px;line-height:1.5}
.muadd{display:flex;gap:6px}
.muadd input{flex:1;min-width:0;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:#121214;color:var(--ink);font:inherit;font-size:14px}
.muadd button{padding:0 13px;border-radius:10px;border:1px solid #ec4899;background:rgba(236,72,153,.16);color:#fff;font:inherit;font-weight:700;font-size:13px;cursor:pointer;white-space:nowrap}
.muadd button.g{border-color:var(--line);background:var(--surface);color:var(--ink2);font-weight:600}
.mucats{display:flex;gap:6px;margin-top:7px;align-items:center;font-size:12px;color:var(--ink3)}
.mucats button{padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:none;color:var(--ink2);font:inherit;font-size:12px;cursor:pointer}
.mucats button.on{border-color:#ec4899;color:#fff;background:rgba(236,72,153,.14)}
.muhint{font-size:12px;color:var(--ink3);line-height:1.5;margin-top:7px}
.muhint b{color:var(--ink2)}
.mumsg{font-size:12.5px;margin-top:7px;min-height:1px}
.mumsg.e{color:#f2a3a3}.mumsg.o{color:#86efac}
.muchips{display:flex;flex-wrap:wrap;gap:7px}
.muchips a{padding:7px 12px;border-radius:999px;border:1px solid rgba(201,154,63,.45);background:rgba(201,154,63,.08);color:#f0cf86;font-size:12.5px;font-weight:600}
.muchips a.alt{border-color:var(--line);color:var(--ink2);background:none}
.mueq{background:linear-gradient(160deg,rgba(236,72,153,.1),rgba(26,26,25,1) 60%);border:1px solid rgba(236,72,153,.35);border-radius:14px;padding:12px}
.mueq .pre{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.mueq .pre button{padding:9px 2px;border-radius:10px;border:1px solid var(--line);background:#18181b;color:var(--ink2);font:inherit;font-size:12px;font-weight:650;cursor:pointer}
.mueq .pre button.on{border-color:#ec4899;color:#fff;background:rgba(236,72,153,.2)}
.mueq .sl{display:flex;align-items:center;gap:10px;margin-top:11px;font-size:12.5px;color:var(--ink2)}
.mueq .sl span{flex:0 0 74px}.mueq .sl b{flex:0 0 52px;text-align:right;font-variant-numeric:tabular-nums;color:#fff}
.mueq .sl input{flex:1;accent-color:#ec4899}
.mueq .viz{display:flex;align-items:flex-end;gap:3px;height:38px;margin-top:10px}
.mueq .viz i{flex:1;border-radius:3px 3px 0 0;background:linear-gradient(0deg,#ec4899,#f0cf86);min-height:2px;transition:height .08s linear}
.mueq .st{margin-top:9px;font-size:12px;line-height:1.5;padding:8px 10px;border-radius:10px}
.mueq .st.ok{background:rgba(34,197,94,.1);color:#9ae6b4;border:1px solid rgba(34,197,94,.35)}
.mueq .st.no{background:rgba(250,178,25,.08);color:#f6d58b;border:1px solid rgba(250,178,25,.35)}
.mugd{margin-top:8px;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
.mugd summary{padding:10px 12px;font-size:13px;font-weight:650;cursor:pointer;color:var(--ink2)}
.mugd ol{margin:0;padding:0 14px 12px 32px;font-size:12.5px;line-height:1.6;color:var(--ink2)}
.mugd ol b{color:#fff}
.muopt{display:flex;align-items:center;gap:10px;padding:10px 11px;border-radius:12px;background:var(--surface);border:1px solid var(--line);margin-bottom:6px}
.muopt div{flex:1;min-width:0}.muopt b{display:block;font-size:13px;font-weight:650}.muopt small{display:block;font-size:11.5px;color:var(--ink3)}
.muhost{position:fixed;z-index:42;overflow:hidden;border-radius:10px;background:#000;display:none}
.muhost.show{display:block}
.muhost.big{z-index:87;border-radius:12px}
.muhost iframe,.muhost #muYT{border:0;display:block}
.muhost.ytm iframe{width:100%!important;height:100%!important}
.muhost .lo{display:flex;align-items:center;gap:12px;padding:12px;height:100%;background:linear-gradient(120deg,#2a1530,#141418);color:#fff}
.muhost .lo .d{flex:0 0 auto;width:72px;height:72px;border-radius:50%;background:radial-gradient(circle,#f0cf86 0 9%,#1a1a1a 10% 30%,#2c2c2c 31% 33%,#151515 34% 100%);
  box-shadow:0 0 0 2px rgba(236,72,153,.5)}
.muhost .lo .d.spin{animation:muspin 2.2s linear infinite}
@keyframes muspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.muhost .lo .d.spin{animation:none}}
.muhost .lo b{display:block;font-size:14px}.muhost .lo small{display:block;font-size:11.5px;color:#d7b9cc;margin-top:3px}
.muhost.mini .lo{padding:4px;gap:0;justify-content:center}.muhost.mini .lo .d{width:44px;height:44px}.muhost.mini .lo .tx{display:none}
#muFile{display:none}
body.mu-on .cfab{bottom:calc(146px + env(safe-area-inset-bottom))}
</style></head><body>

<div class="hero hfull">
  <div class="hero-ph"></div>
  <div class="hero-photo" id="heroPhoto"></div>
  <div class="hero-fade"></div>
  <div class="hero-top">
    <span class="chip"><span class="dot" id="dot2"></span><span id="st2">…</span></span>
    <span class="hero-btns">
      <button class="edit inst" id="btnInst" type="button" title="Ku rakib app-ka" aria-label="Ku rakib app-ka">
        <svg viewBox="0 0 24 24"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
        <span>Install</span>
      </button>
      <button class="edit" id="btnPic" title="Beddel sawirka" aria-label="Beddel sawirka">
        <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
        <span>Beddel sawirka</span>
      </button>
      <button class="edit hmb" id="heroMenuB" type="button" aria-label="Menu: account · password · bax" aria-haspopup="true" aria-expanded="false">⋯</button>
    </span>
  </div>
  <div class="hero-in">   <!-- v12.23: wajiga hore -->
    <span class="hacc"><span id="heroAcc">—</span> · MT5</span>
    <h1>MOHA PRO <b id="heroVer"></b></h1>
    <div class="hst"><span class="dot" id="dot"></span><span id="st">Xiriirinaya…</span></div>
    <div class="hstats">
      <div><small>BALANCE</small><b class="v neu" id="bal">—</b></div>
      <div><small>EQUITY</small><b class="v neu" id="eq">—</b></div>
      <div><small>MAANTA</small><b class="v" id="pf">—</b></div>
    </div>
    <div class="hctl">
      <button class="mtsw na" id="pwrSw" type="button" data-perm="run" aria-label="Bot-ka shid / dami" aria-pressed="false"><span class="lbl"><b id="pwrT">—</b><small id="pwrS">—</small></span><span class="knob" aria-hidden="true"></span></button>
      <button class="hxid" id="xidB" type="button" data-perm="close" aria-label="Xidh trade-yada furan" disabled><span class="xi" aria-hidden="true">✕</span><span class="pt"><b>XIDH</b><small id="xidS">—</small></span></button>
    </div>
  </div>
</div>
<input type="file" id="pick" accept="image/png,image/jpeg,image/webp">

<div class="hmenu" id="heroMenu" hidden role="menu">   <!-- v12.23: ⋯ -->
  {% if is_admin %}
  <select id="accSel" autocomplete="off" style="width:auto;padding:7px 10px;font-size:13px">
    {% for a in accounts %}<option value="{{ a }}" {% if a==sel %}selected{% endif %}>{{ a }}</option>{% endfor %}
    {% if me not in accounts %}<option value="{{ me }}" {% if me==sel %}selected{% endif %}>{{ me }}</option>{% endif %}
  </select>
  {% else %}<span class="pill">Account: {{ me }}</span>{% endif %}
  <span class="spacer"></span>
  <span class="pill">{{ name }}</span>
  {% if is_admin %}<a class="btn" href="/admin">🛡 Admin (maamul)</a>{% endif %}
  <a class="btn" href="/password">🔑 Password</a>
  <a class="btn" href="/logout">⎋ Bax</a>
</div>

<div class="wrap">

  <!-- ============ GUUD ============ -->
  <section class="pane on" id="pGuud">
    <!-- v12.22 (EA v70.8): 💱 LAMAANAHA -->
    <div class="prstrip" id="prStrip" hidden role="tablist" aria-label="Lamaanaha"></div>
    <div class="prflt" id="prFlt" hidden><span>💱 <b id="prFltS">—</b> oo keliya · Trade-yada</span><button type="button" id="prFltX">✕ Dhammaan</button></div>
    <div class="grid">
      <div class="tile"><div class="k">Faa'iido furan (float)</div><div class="v" id="fl">—</div></div>
      <div class="tile"><div class="k">Wadarta hadda</div><div class="v" id="tot">—</div></div>
      <div class="tile"><div class="k">Win rate</div><div class="v neu" id="wr">—</div></div>
      <div class="tile"><div class="k">Drawdown</div><div class="v" id="dd">—</div></div>
      <div class="tile"><div class="k">Trade furan</div><div class="v neu" id="ot">—</div></div>
    </div>

    <!-- v12: laysinka (macmiil) -->
    <div class="card lic" id="licCard" style="margin-bottom:16px" hidden>
      <div class="licr"><div><p class="sec-t" style="margin:0">Laysinka</p>
        <div class="licbig" id="licBig">—</div><div class="note" id="licSub" style="margin:0">—</div></div>
        <span class="licpill" id="licPill">—</span></div>
    </div>

    <!-- v12.8: SABABTA TRADE LA'AANTA (EA v69.6) -->
    <div class="card rsnc" id="rsnCard" style="margin-bottom:16px" hidden>
      <div class="rsnal" id="rsnAlert" hidden><b id="rsnAT">—</b><p id="rsnAP">—</p></div>
      <p class="sec-t">Sababta · hadda</p>
      <div class="rsntot"><div><b id="rsnSig">0</b>signal</div><div><b id="rsnZn">0</b>zone hadda</div><div><b id="rsnN">0</b>chart</div></div>
      <div id="rsnRows"></div>
    </div>

    <!-- v12.12: ⚡ TICK SCALPER (EA v70.0) -->
    <!-- v12.22 (EA v70.8): 🎯 ZONE YAR -->
    <!-- v12.25 (EA v71.0): 🪜 GRID STOP -->
    <div class="card grc" id="grCard" style="margin-bottom:16px" hidden>
      <div class="grh"><b>🪜 GRID STOP</b><span class="grkeli" id="grKeli">KELI</span><span class="sp"></span><button class="sw" id="grSw" type="button" aria-label="GRID shid / dami"><i></i></button></div>
      <div class="grst o" id="grSt">—</div>
      <div class="grkp" id="grKp" hidden><div><small>FLOAT</small><b id="grFl">—</b></div><div><small>🔒 QUFUL</small><b id="grLk">—</b></div><div><small>🛑 SL GUUD</small><b class="n" id="grSl">—</b></div></div>
      <div class="grlad" id="grLad" hidden></div><div class="grll" id="grLl" hidden></div>
      <div class="seg grper" id="grPer" role="tablist" aria-label="Muddada natiijada"><button type="button" data-v="d">MAANTA</button><button type="button" data-v="w">TODOBAAD</button><button type="button" data-v="m">BIL</button><button type="button" data-v="a">DHAMMAAN</button></div>
      <div class="grkp"><div><small>GRID-YO</small><b id="grN">—</b></div><div><small>GUUL</small><b id="grW">—</b></div><div><small>P/L</small><b id="grPL">—</b></div></div>
      <div class="grkp"><div><small>PROFIT FACTOR</small><b id="grPF">—</b></div><div><small>DD UGU WEYN</small><b class="n" id="grDD">—</b></div><div><small>LAKAB CELCELIS</small><b id="grAv">—</b></div></div>
      <div class="greq"><small id="grEqT">EQUITY-GA GRID-KA</small><svg id="grEq" viewBox="0 0 340 64" preserveAspectRatio="none"></svg></div>
      <div class="grhl" id="grHl"></div>
      <div class="grrj" id="grRj"></div>
    </div>
    <div class="card znc" id="znCard" style="margin-bottom:16px" hidden>
      <div class="znh"><b>🎯 ZONE YAR</b><span class="znp" id="znTf">M5</span><span class="sp"></span>
        <div class="seg seg-s" id="znEye" role="radiogroup" aria-label="Chart-ka MT5" style="width:auto"><button type="button" data-v="1" role="radio">👁 Muuji</button><button type="button" data-v="0" role="radio">🙈 Qari</button></div></div>
      <div class="znst" id="znSt">—</div>
      <svg class="znlad" id="znLad" viewBox="0 0 320 120" role="img" aria-label="Jaranjarada zone-yada"></svg>
      <div class="znmeta" id="znMeta">—</div>
      <div class="znrj" id="znRj" hidden></div>
    </div>
    <div class="card tkc" id="tkCard" style="margin-bottom:16px" hidden>
      <div class="tkhd"><div class="tkbd"><svg viewBox="0 0 24 24"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg></div>
        <div class="tkhn"><b>TICK SCALPER</b><small id="tkSub">XAUUSD</small></div>
        <span class="tkpill" id="tkPill"><i></i><span id="tkState">—</span></span></div>
      <div class="tkwhy" id="tkWhy" hidden>—</div>
      <div class="tktr" id="tkOpen" hidden>
        <div class="tktrh"><b id="tkOT">—</b><span id="tkOPL">—</span></div>
        <div class="tktrs" id="tkTrs">—</div>
        <div class="tkbar2" id="tkBar"></div>
        <div class="tkbarl" id="tkLg"></div>
        <div class="tkst" id="tkSteps"></div>
      </div>
      <div class="tkbk" id="tkBsk" hidden>
        <div class="h"><b id="tkBkH">—</b><span id="tkBkPL">—</span></div>
        <div class="tktrs" id="tkBkS">—</div>
        <div class="tg" id="tkBkTg"><div class="tgb"><i id="tkBkTgI"></i></div><div class="tgl"><span id="tkBkTgL">—</span><span id="tkBkTgR">—</span></div></div>
        <div id="tkBkL"></div>
        <div class="sl" id="tkBkSl"></div>
        <div class="tkst" id="tkBkSt"></div>
      </div>
      <div class="tksec" id="tkTTh">TICK TRACKER</div>
      <div class="tktt"><div class="tkdots" id="tkDots"></div><div class="tkttn" id="tkTTn">—</div></div>
      <div class="tkmv" id="tkMv"><div class="tkmvb"><i id="tkMvI"></i><em></em></div><div class="tkmvl"><span id="tkMvL">—</span><span id="tkMvR">—</span></div></div>
      <div id="tkJh" hidden>   <!-- v12.19 (EA v70.7): 🧭 JIHADA SUUQA -->
        <div class="tksec">🧭 JIHADA SUUQA</div>
        <div class="tkjd" id="tkJd"><b id="tkJdB">—</b><small id="tkJdS">—</small></div>
        <div class="tkjm"><i id="tkJmI"></i></div><div class="tkjml"><span>▼ HOOS ADAG</span><span>RANGE · HA GALIN</span><span>KOR ADAG ▲</span></div>
        <div class="tkjs" id="tkJs"></div>
        <div class="tkjr" id="tkJb" hidden></div>
        <div class="tkjr" id="tkJr" hidden></div>
      </div>
      <div class="tkkpi">
        <div><b id="tkDN">—</b>maanta</div>
        <div><b id="tkDPL">—</b>maanta $</div>
        <div><b id="tkWR">—</b>guul</div>
        <div><b id="tkPF">—</b>PF</div>
        <div><b id="tkMDD">—</b>hist DD</div>
        <div><b id="tkCL">—</b>khasaare</div>
      </div>
      <div class="tkses" id="tkSes"><div class="tksesb"><i id="tkSesI"></i><em id="tkSesE"></em></div>
        <div class="tksesl"><span id="tkSesA">—</span><span class="m" id="tkSesM">—</span><span id="tkSesB">—</span></div></div>
      <div class="tkchips" id="tkChips"></div>
      <div id="tkBkD" hidden>
        <div class="tksec">BASKET-YADA MAANTA</div>
        <div class="tkbd4"><div><b id="tkBkDN">—</b>basket</div><div><b id="tkBkDH">—</b>bartilmaameed</div><div><b id="tkBkDP">—</b>basket $</div></div>
      </div>
      <div id="tkQA" hidden>
        <div class="tksec">TAYADA BIXITAANKA</div>
        <div class="tkqa"><div><b id="tkAPK">—</b>peak (celcelis)</div><div><b id="tkASL">—</b>slippage</div><div><b id="tkASP">—</b>spread</div></div>
        <div class="tkqh" id="tkQH">—</div>
      </div>
      <div class="tksec">TRADE-YADII U DAMBEEYAY</div>
      <div id="tkHist"></div>
      <div class="tkft"><span id="tkFtL">—</span><span id="tkFtR">—</span></div>
    </div>

    <!-- v12.9: ASIA BREAKOUT (EA v69.7) -->
    <div class="card bskc" id="asCard" style="margin-bottom:16px" hidden>
      <div class="bskh"><span class="gold"><i class="coin"></i><span id="asTitle">ASIA BREAKOUT</span></span><span class="bchip" id="asState">—</span></div>
      <svg class="assvg" id="asSvg" viewBox="0 0 400 200" role="img" aria-label="Asia range iyo qiimaha hadda"></svg>
      <div class="rsntot astot"><div><b id="asRng">—</b>range maanta</div><div><b id="asTr">—</b>trend H4</div><div><b id="asCnt">—</b>trade maanta</div></div>
      <div id="asLive" hidden>
        <div class="bbig"><b id="asPL">—</b><span id="asSub">—</span></div>
        <div class="asln" id="asLvl">—</div>
      </div>
      <div class="bidle" id="asWhy">—</div>
      <p class="sec-t" style="margin:14px 0 2px">7-dii maalmood ee u dambeeyay</p>
      <div id="asHist"></div>
      <div class="bday"><span>Wadar</span><b id="asSum">—</b></div>
    </div>

    <!-- v12.7: GOLD BASKET - basket-ka hadda socda (EA v69.0) -->
    <div class="card bskc" id="bskCard" style="margin-bottom:16px" hidden>
      <div class="bskh"><span class="gold"><i class="coin"></i><span id="bskTitle">GOLD BASKET</span></span><span class="bchip" id="bskState">—</span></div>
      <div class="bchips" id="bskChips"></div>
      <div id="bskLive" hidden>
        <div class="bbig"><b id="bskPL">—</b><span id="bskSub">—</span></div>
        <div class="bbar"><i id="bskBar"></i><em id="bskBE"></em></div>
        <div class="bbl"><span id="bskL">—</span><span>BE</span><span id="bskR">—</span></div>
        <div id="bskRows"></div>
        {% if can_control %}<div class="bacts">
          <button class="act warn" data-cmd="BASKET:CLOSE" data-perm="close" id="bskClose">✕ Xidh basket-ka</button>
          <button class="act calm" data-cmd="BASKET:BE" data-perm="close" id="bskBEBtn">⇆ BE hadda</button>
        </div>{% endif %}
      </div>
      <div class="bidle" id="bskIdle">—</div>
      <div class="bday"><span>Maanta · dahab</span><b id="bskDay">—</b></div>
    </div>

    <div class="card"><p class="sec-t">Xogta account-ka</p>
      <div class="note" id="meta" style="margin:0">—</div></div>

    {% if not is_admin %}
    <!-- v11: furaha bot-ka isticmaalaha -->
    <div class="card" id="myKey" style="margin-top:16px"><p class="sec-t">Furahaaga bot-ka</p>
      {% if bkey and bkey.active %}
      <div class="mykey" id="myKeyVal">{{ bkey.key }}</div>
      <button type="button" class="act go" id="myKeyCp" style="flex-direction:row;padding:13px;width:100%;margin-top:10px">KOOBI GAREE FURAHA</button>
      <ol class="ksteps">
        <li><span>MT5 → bot-ka <b>MOHA PRO</b> chart-ka ku dhaji</span></li>
        <li><span>Tab-ka <b>Inputs</b> → <code>MohaPro_Key</code> → halkaas ku dheji furaha</span></li>
        <li><span><b>OK</b> riix. 1 daqiiqo gudaheed xogtaadu halkan ayay ka soo muuqanaysaa.</span></li>
      </ol>
      <div class="note" id="myKeySt" style="margin-top:10px">—</div>
      <div class="note" style="margin-top:8px">Furahan <b>account-kaaga #{{ me }} oo keliya</b> ayuu u shaqeeyaa. Qof kale ha siin. Haddii uu lumo, admin-ka u sheeg — fure cusub ayuu kuu samaynayaa, kii hore-na wuu dhimanayaa.</div>
      {% elif bkey %}
      <div class="nwarn">Furahaaga waa la xannibay. Admin-ka la xidhiidh (badhanka fariimaha).</div>
      {% else %}
      <div class="note">Admin-ku weli furaha kuuma samayn. Marka lagu ansixiyo, halkan ayuu ka soo muuqanayaa.</div>
      {% endif %}
    </div>
    <style>
    .mykey{font:700 19px ui-monospace,Menlo,Consolas,monospace;color:#f0cf86;letter-spacing:.04em;text-align:center;padding:14px;
      background:#101113;border:1px dashed #6b5a2e;border-radius:12px;word-break:break-all}
    .ksteps{margin:12px 0 0;padding-left:0;list-style:none;counter-reset:s;display:flex;flex-direction:column;gap:9px}
    .ksteps li{counter-increment:s;display:flex;gap:10px;font-size:14px;line-height:1.45}
    .ksteps li:before{content:counter(s);flex:0 0 24px;height:24px;border-radius:50%;background:rgba(217,174,85,.18);color:#f0cf86;
      font-weight:700;font-size:12.5px;display:flex;align-items:center;justify-content:center}
    .ksteps code{font:600 12.5px ui-monospace,Menlo,monospace;background:#20232b;border:1px solid #3a3f4c;border-radius:6px;padding:1px 6px;color:#f0cf86}
    </style>
    {% endif %}
    <!-- v12.1: Colour Matrix (telefoon kasta gooni) -->
    <div class="card" id="mxCard" style="margin-top:16px"><p class="sec-t">Muuqaalka</p>
      <div class="thl" style="margin-top:4px">Midabka app-ka<small>App-ka oo dhan ayuu beddelaa — badhamada, tab-yada, xariiqyada, chart-ka</small></div>
      <div class="thsw" id="thSw" role="radiogroup" aria-label="Midabka app-ka"></div>
      <div class="sepx"></div>
      <div class="mxcard"><span class="mxic" aria-hidden="true">🎨</span>
        <div class="mxt"><b>Colour Matrix</b><small>Sawirka, badhamada iyo magaca way iftiimayaan — shid / dami</small></div>
        <button class="sw" id="mxSw" type="button" role="switch" aria-checked="false" aria-label="Colour Matrix"><i></i></button></div>
      <div class="mxopt" id="mxOpt" hidden>
        <div class="thl">Midabada iftiinka<small>Dooro inta aad rabto — way is beddelayaan (ugu yaraan 1)</small></div>
        <div class="mxch" id="mxCh"></div>
        <div class="thl">Xawaaraha</div>
        <div class="spd" id="mxSpd"><button type="button" data-s="0">Gaabis</button><button type="button" data-s="1">Caadi</button><button type="button" data-s="2">Degdeg</button></div>
      </div>
    </div>
  </section>


  <!-- ============ MAAMUL (v10.1: tab gooni ah) ============ -->
  {% if can_control %}
  <section class="pane" id="pMaamul">
    <!-- v12.18: MAAMUL = amarrada · xeeladaha · khatarta (gaaban). Sitinka kale oo dhan -> ⚙️ Input -->
    <!-- v12.22 (EA v70.8): 💱 LAMAANAHA -->
    <div class="card prcard" id="prCard" style="margin-bottom:16px">
      <div class="prh"><p class="sec-t" style="margin:0">💱 Lamaanaha</p><span class="prn" id="prCnt">—</span></div>
      <div id="prList"><div class="empty" style="padding:12px 0">Soo raraya…</div></div>
      <button type="button" class="pradd" id="prAddB">＋ Ku dar lamaane</button>
      <div class="prsh" id="prAdd" hidden>
        <input id="prQ" type="search" placeholder="Raadi lamaane… (eur, btc, us30)" autocomplete="off" aria-label="Raadi lamaane">
        <div class="prres" id="prRes"></div>
      </div>
      <div class="acts" style="margin-top:12px;grid-template-columns:1fr"><button class="act go" id="prSave" style="flex-direction:row;gap:9px;padding:13px">KAYDI &amp; DIR</button></div>
      <div class="note" id="prNote" style="margin-top:8px">—</div>
      <div class="prwarn">📏 <b>⚡ TICK · 🧺 BASKET</b> hadda lamaane kasta (fiddo · BTC · US30 …) — sitinka $ si toos ah ayaa loo cabbiraa (lamaanaha riix → furo). <b>ASIA</b> dahabka (👑) oo keliya. Xeeladda caadiga ah: <b>SR/SD · SMC · LABADA</b>.</div>
      <details class="prhelp"><summary>⚙️ Hal mar oo keliya PC-ga (template)</summary><ol>
        <li>MT5 → chart-ka <b>XAUUSD</b> ee bot-ku ku shidan yahay.</li>
        <li>Chart-ka midig ku riix → <b>Templates → Save Template</b>.</li>
        <li>Magaca: <b id="prTplN">MOHA_PRO</b> → <b>Save</b>.</li>
        <li>MT5: <b>Algo Trading</b> shid · PC-ga / VPS-ka ha damin.</li>
        <li>Kadib app-ka ka shid lamaanaha — bot-ka 👑 ayaa chart-ka u furaya.</li>
      </ol></details>
    </div>
    <div class="card" style="margin-bottom:16px">
      <p class="sec-t">Amarrada</p>
      <div class="acts">
        <button class="act go" data-cmd="START" data-perm="run">
          <svg viewBox="0 0 24 24"><path d="m6 4 14 8-14 8Z"/></svg>SHID</button>
        <button class="act stop" data-cmd="STOP" data-perm="run">
          <svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>DAMI</button>
        <button class="act warn" data-cmd="CLOSE_ALL" data-perm="close">
          <svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>XIDH</button>
      </div>
      <div class="acts" style="margin-top:10px">
        <button class="act calm" data-cmd="CLOSE_PROFIT" data-perm="close" style="grid-column:span 3;flex-direction:row;gap:9px;padding:13px">
          <svg viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 7-7"/><path d="M14 8h6v6"/></svg>XIDH FAA'IIDO</button>
      </div>
      <div class="note" id="cmdNote">Amarku wuxuu gaadhayaa EA-da 3–5 ilbiriqsi gudahood.</div>
    </div>

    <div class="card" id="stCard" style="margin-bottom:16px">
      <p class="sec-t">Xeeladaha</p>
      <div class="sttiles">
        <div class="stt" data-k="TICK"><div class="h"><b>⚡ TICK</b><button class="sw stsw" type="button" data-sw="mTK" aria-label="TICK SCALPER"><i></i></button></div>
          <div class="m"><span class="stp2" id="stTkS">—</span><span id="stTkN"></span></div><div class="p" id="stTkP">—</div><button type="button" class="lnk" data-go="TICK">⚙️ Input › TICK</button></div>
        <div class="stt" data-k="GOLD"><div class="h"><b>🥇 GOLD</b><button class="sw stsw" type="button" data-sw="mBSK" aria-label="GOLD BASKET"><i></i></button></div>
          <div class="m"><span class="stp2" id="stGdS">—</span></div><div class="p" id="stGdP">—</div><button type="button" class="lnk" data-go="GOLD">⚙️ Input › GOLD</button></div>
        <div class="stt" data-k="ASIA"><div class="h"><b>🌅 ASIA</b><button class="sw stsw" type="button" data-sw="mASIA" aria-label="ASIA BREAKOUT"><i></i></button></div>
          <div class="m"><span class="stp2" id="stAsS">—</span></div><div class="p" id="stAsP">—</div><button type="button" class="lnk" data-go="ASIA">⚙️ Input › ASIA</button></div>
        <div class="stt" data-k="MAIN"><div class="h"><b>🎯 MAIN</b><span class="stsr" id="stMnN">—</span></div>
          <div class="m"><span class="stp2" id="stMnS">—</span></div><div class="p" id="stMnP">—</div><button type="button" class="lnk" data-go="MAIN">⚙️ Input › MAIN</button></div>
      </div>
      <div class="note">Shid / dami → <b>KAYDI &amp; DIR</b> (hoose). Sitin kasta oo kale → <b>⚙️ Input</b>.</div>
    </div>
    <div class="card" id="mCard" style="margin-bottom:16px">
      <p class="sec-t">Khatarta <small style="text-transform:none;letter-spacing:0;color:var(--ink3);font-weight:500">· dhammaan xeeladaha</small></p>
      <div class="savebar" id="mSave"><span class="dt"></span>—</div>
      {% if lic %}<div class="lockband" id="mLockBand"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg><span>Waxa quful ah <b>admin-ka ayaa maamula</b> — waad arki kartaa oo keliya.</span></div>{% endif %}

        <div class="frow"><span>Risk trade kasta <small>(lot-ka auto ayuu go'aamiyaa)</small></span><span><input id="mRISK" type="number" min="0.01" max="10" step="0.05"> <i>%</i></span></div>
        <div class="frow"><span>Lot go'an <small>(0 = auto · risk %)</small></span><span><input id="mLOT" type="number" min="0" max="100" step="0.01"> <i>lot</i></span></div>
        <div class="frow"><span>Khasaaraha maalinlaha <small>(kadib wuu joogsadaa maanta)</small></span><span><input id="mDLOSS" type="number" min="0" max="50" step="0.5"> <i>%</i></span></div>
        <div class="frow"><span>Drawdown ugu badan <small>(emergency stop)</small></span><span><input id="mMAXDD" type="number" min="0" max="100" step="1"> <i>%</i></span></div>
      
      <div class="acts" style="margin-top:12px;grid-template-columns:1fr">
        <button class="act go" id="mSend2" type="button" style="flex-direction:row;gap:9px;padding:14px">KAYDI &amp; DIR</button>
      </div>
      <div class="note" id="mNote2">—</div>
    </div>
    <details class="card fold" id="fdLock"><summary><span>💰 Faa'iidada la xidhay <span class="cnt" id="cLock"></span></span><small>step-lock · BE</small></summary>
    <div>
      
      <div class="scroll"><table id="tl">
        <thead><tr><th>Waqti</th><th>Symbol</th><th style="text-align:right">Faa'iido</th>
          <th style="text-align:right">Xidhay</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">Weli wax lama xidhin.</td></tr></tbody>
      </table></div>
      <div class="note" id="lockSum" style="margin-top:10px">—</div>
    </div>
    </details>
    <details class="card fold" id="fdAcc"><summary><span>👤 Account · furaha bot-ka</span><small>xogta account-ka</small></summary><div id="fdAccB"></div></details>
    <details class="card fold" id="fdMx"><summary><span>🎨 Muuqaalka</span><small>midabka · Colour Matrix</small></summary><div id="fdMxB"></div></details>
  </section>

  <!-- ============ v12.18: ⚙️ INPUT (tab cusub · Chart-kii meeshiisa) ============ -->
  <section class="pane" id="pInput">
    {% if lic %}<div class="lockband" id="mLockBand2"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg><span>Waxa quful ah <b>admin-ka ayaa maamula</b> — waad arki kartaa oo keliya.</span></div>{% endif %}
    <div class="inph">
      <label class="insrch"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input id="inQ" type="search" placeholder="Raadi input… (trailing, session, RR)" autocomplete="off" aria-label="Raadi input"></label>
      <div class="incats" id="inCats" role="tablist">
        <button type="button" data-c="TICK">⚡ TICK <b></b></button><button type="button" data-c="GRID">🪜 GRID <b></b></button><button type="button" data-c="ZONE">🎯 ZONE <b></b></button><button type="button" data-c="GOLD">🥇 GOLD <b></b></button><button type="button" data-c="ASIA">🌅 ASIA <b></b></button>
        <button type="button" data-c="MAIN">🎯 MAIN <b></b></button><button type="button" data-c="PROT">🛡 Ilaalin <b></b></button><button type="button" data-c="FILT">🕐 Waqti &amp; Filter <b></b></button><button type="button" data-c="SYS">📨 Nidaam <b></b></button>
      </div>
      <div class="insync" id="inSync">—</div>
    </div>
    <div class="incat" data-c="TICK"><div class="card incard">
      <!-- v12.12: ⚡ TICK SCALPER (EA v70.0) -->
      <div class="grp bskg" id="tkGrp">
        <div class="gh bskgh"><span class="gold"><i class="coin"></i>⚡ TICK SCALPER <small>XAUUSD</small></span><button class="sw" id="mTK" type="button" aria-label="TICK SCALPER"><i></i></button></div>
        <div class="sltph">Momentum scalper: tick kasta ayuu akhriyaa → marka tick-yadu hal jiho si degdeg ah u wada dhaqaaqaan ayuu galaa → <b>virtual SL / TP · break-even · trailing</b> (broker-ka lama tuso) + SL adag ilaalin ah. <b>EA v70.0+</b></div>
        <div class="frow"><span>TICK oo keliya <small>main · basket · asia XAUUSD ha furin</small></span><span><button class="sw on" id="mTKONLY" type="button" aria-label="TICK oo keliya"><i></i></button></span></div>
        <div class="bsub">1 · GELITAAN · TICK TRACKER</div>
        <div class="frow"><span>Tick-yada (W) <small>isbeddelada qiimaha la eegayo · 3 – 50</small></span><span class="stp"><button type="button" id="mTKWm" aria-label="Ka yaree">−</button><b id="mTKW">8</b><button type="button" id="mTKWp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>Jiho isku mid (K) <small>W ka mid · 5 / 8 = momentum</small></span><span class="stp"><button type="button" id="mTKKm" aria-label="Ka yaree">−</button><b id="mTKK">5</b><button type="button" id="mTKKp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>Dhaqdhaqaaq ugu yar <small>window-ka gudihiisa</small></span><span><input id="mTKMV" type="number" min="0" max="20" step="0.05" value="0.4"> <i>$</i></span></div>
        <div class="frow"><span>Xawaare <small>window ≤ X ilbiriqsi · 0 = off</small></span><span><input id="mTKSEC" type="number" min="0" max="300" step="1" value="10"> <i>s</i></span></div>
        <div class="frow"><span>Filter spread <small>dhaqdhaqaaq ≥ X × spread · 0 = off · EA v70.2+</small></span><span><input id="mTKSPM" type="number" min="0" max="10" step="0.5" value="3"> <i>×</i></span></div>
        <div class="frow"><span>Filter trend <small>EMA50 M5 · BUY kor oo keliya / SELL hoos</small></span><span><button class="sw" id="mTKTR" type="button" aria-label="Filter trend"><i></i></button></span></div>
        <div id="tkJSec" class="tktpr">
        <div class="frow" id="rTKDRON"><span>🧭 Jihada suuqa <b class="tknew">CUSUB</b><small>tick-yada jihada guud oo keliya · pullback / RANGE → ha galin · EA v70.7+</small></span><span><button class="sw" id="mTKDRON" type="button" aria-label="Jihada suuqa"><i></i></button></span></div>
        <div id="tkJOpts">
        <div class="frow frow-col" id="rTKDRM"><span style="margin-bottom:6px">Heerka <small>inta calaamadood ee jihada isku raaca (4)</small></span><div class="seg" id="mTKDRM" role="radiogroup" aria-label="Heerka jihada">
          <button type="button" data-v="0" role="radio">JILICSAN<small style="display:block;font-size:10px;opacity:.8">2 / 4</small></button>
          <button type="button" data-v="1" role="radio">DHEXE<small style="display:block;font-size:10px;opacity:.8">3 / 4</small></button>
          <button type="button" data-v="2" role="radio">ADAG<small style="display:block;font-size:10px;opacity:.8">4 / 4</small></button>
        </div></div>
        <div class="frow"><span>Shumacyada M1 <small>la eegayo → inta jiho isku mid ah</small></span><span><input id="mTKDRB" type="number" min="3" max="20" step="1" value="6" style="width:62px"> <i>→</i> <input id="mTKDRN" type="number" min="2" max="20" step="1" value="4" style="width:62px"></span></div>
        <div class="frow"><span>Xoogga ugu yar <small>ka yar → RANGE → ha galin</small></span><span><input id="mTKDRS" type="number" min="0" max="100" step="5" value="40"> <i>%</i></span></div>
        <div class="frow"><span>Khasaare kadib → jihadaas sug <small>BUY khasaaray → BUY kale X daq ma jiro · 0 = off</small></span><span><input id="mTKDRW" type="number" min="0" max="240" step="5" value="10"> <i>daq</i></span></div>
        </div>
        <div class="sltph" id="mTKJHint">—</div>
        </div>
        <div class="bsub">2 · LOT</div>
        <div class="frow frow-col" id="rTKLM"><div class="seg" id="mTKLM" role="radiogroup" aria-label="Lot">
          <button type="button" data-v="0" role="radio">GO'AN</button>
          <button type="button" data-v="1" role="radio">RISK %</button>
        </div></div>
        <div class="frow" id="rTKLOT"><span>Lot go'an</span><span><input id="mTKLOT" type="number" min="0.01" max="100" step="0.01" value="0.01"> <i>lot</i></span></div>
        <div class="frow" id="rTKRISK"><span>Risk trade kasta <small>% balance ÷ virtual SL</small></span><span><input id="mTKRISK" type="number" min="0.01" max="5" step="0.05" value="0.25"> <i>%</i></span></div>
        <div class="bsub">3 · BIXITAAN</div>
        <div class="frow frow-col" id="rTKSLM"><span style="margin-bottom:6px">Stop Loss <small>halka SL-ku ku jiro · EA v70.2+</small></span><div class="seg" id="mTKSLM" role="radiogroup" aria-label="Stop Loss">
          <button type="button" data-v="0" role="radio">VIRTUAL<small style="display:block;font-size:10px;opacity:.8">bot-ka ayaa xidha</small></button>
          <button type="button" data-v="1" role="radio">BROKER<small style="display:block;font-size:10px;opacity:.8">server-ka ayaa xidha</small></button>
        </div></div>
        <div class="sltph" id="mTKSLMHint">—</div>
        <div class="frow frow-col" id="rTKTPM" style="margin-top:8px"><span style="margin-bottom:6px">Take Profit <small>halka TP-gu ku jiro · EA v70.3+</small></span><div class="seg" id="mTKTPM" role="radiogroup" aria-label="Take Profit">
          <button type="button" data-v="0" role="radio">VIRTUAL<small style="display:block;font-size:10px;opacity:.8">bot-ka ayaa xidha</small></button>
          <button type="button" data-v="1" role="radio">BROKER<small style="display:block;font-size:10px;opacity:.8">server-ka ayaa xidha</small></button>
        </div></div>
        <div class="sltph" id="mTKTPMHint">—</div>
        <div class="frow"><span>Virtual SL</span><span><input id="mTKVSL" type="number" min="0.1" max="100" step="0.1" value="2.5"> <i>$</i></span></div>
        <div class="frow"><span>Virtual TP <small>0 = trailing oo keliya</small></span><span><input id="mTKVTP" type="number" min="0" max="100" step="0.1" value="1.5"> <i>$</i></span></div>
        <div id="tkTprSec" class="tktpr">
        <div class="frow" id="rTKTRON"><span>TP RAAC <b class="tknew">CUSUB</b><small>TP u dhow → TP hore u rar + SL quful · EA v70.5+</small></span><span><button class="sw" id="mTKTRON" type="button" aria-label="TP RAAC"><i></i></button></span></div>
        <div id="tkTprOpts">
        <div class="frow"><span>Bilow <small>faa'iido ≥ X% TP-ga</small></span><span><input id="mTKTRST" type="number" min="50" max="95" step="5" value="80"> <i>%</i></span></div>
        <div class="frow"><span>Qufulka SL <small>SL = X% TP-ga (faa'iido)</small></span><span><input id="mTKTRLK" type="number" min="10" max="90" step="5" value="70"> <i>%</i></span></div>
        <div class="frow"><span>TP u dheeree <small>raac kasta</small></span><span><input id="mTKTRSTEP" type="number" min="0.1" max="50" step="0.1" value="1"> <i>$</i></span></div>
        <div class="frow"><span>Raac ugu badan <small>jeer</small></span><span><input id="mTKTRMAX" type="number" min="1" max="10" step="1" value="3"></span></div>
        </div>
        <div class="sltph" id="mTKTRHint">—</div>
        </div>
        <div class="frow"><span>Break-even <small>faa'iido $X → SL = entry + lock · 0 = off</small></span><span><input id="mTKBE" type="number" min="0" max="100" step="0.05" value="0.6"> <i>$</i></span></div>
        <div class="frow"><span>Break-even lock</span><span><input id="mTKBEL" type="number" min="0" max="50" step="0.05" value="0.1"> <i>$</i></span></div>
        <div class="frow"><span>Trailing bilow <small>0 = off</small></span><span><input id="mTKTRS" type="number" min="0" max="100" step="0.1" value="1"> <i>$</i></span></div>
        <div class="frow"><span>Trailing masaafo</span><span><input id="mTKTRD" type="number" min="0.05" max="50" step="0.05" value="0.5"> <i>$</i></span></div>
        <div class="frow"><span>Waqtiga ugu dheer <small>trade → xidh · 0 = off</small></span><span><input id="mTKHOLD" type="number" min="0" max="86400" step="30" value="900"> <i>s</i></span></div>
        <div class="bsub">4 · 🔒 ILAALIN</div>
        <div class="frow" id="rTKHARD"><span>SL adag (broker) <small id="mTKHARDs">internet go'a · ugu yaraan VSL + $1</small></span><span><input id="mTKHARD" type="number" min="0.5" max="200" step="0.5" value="6"> <i>$</i></span></div>
        <div class="frow"><span>Saacadaha (server) <small>bilow → dhammaad</small></span><span><input id="mTKHS" type="number" min="0" max="23" step="1" value="1" style="width:56px"> <i>→</i> <input id="mTKHE" type="number" min="1" max="24" step="1" value="22" style="width:56px"></span></div>
        <div class="frow"><span>Spread ugu badan <small>sent · 35 = $0.35</small></span><span><input id="mTKSPR" type="number" min="5" max="500" step="1" value="35"> <i>¢</i></span></div>
        <div class="frow"><span>Cooldown <small>kadib trade kasta</small></span><span><input id="mTKCD" type="number" min="0" max="3600" step="5" value="20"> <i>s</i></span></div>
        <div class="frow"><span>Trade maalintii</span><span><input id="mTKMAXD" type="number" min="1" max="1000" step="1" value="60"></span></div>
        <div class="frow"><span>Khasaare isku xiga → hakad <small>0 = off</small></span><span><input id="mTKML" type="number" min="0" max="20" step="1" value="4"> <i>→</i> <input id="mTKPAUSE" type="number" min="1" max="1440" step="5" value="60" style="width:64px"> <i>daq</i></span></div>
        <div class="frow"><span>Khasaaraha maalinlaha <small>% balance → maanta jooji · 0 = off</small></span><span><input id="mTKDL" type="number" min="0" max="50" step="0.5" value="2"> <i>%</i></span></div>
        <div class="frow"><span>Bartilmaameed maalinle <small>% balance → maanta jooji · 0 = off</small></span><span><input id="mTKDT" type="number" min="0" max="100" step="0.5" value="0"> <i>%</i></span></div>
        <div id="tkBskSec">
        <div class="bsub">5 · BASKET <small style="font-weight:600;letter-spacing:0;opacity:.75">EA v70.4+</small></div>
        <div class="frow frow-col" id="rTKBON"><div class="seg" id="mTKBON" role="radiogroup" aria-label="Basket">
          <button type="button" data-v="0" role="radio">HAL TRADE</button>
          <button type="button" data-v="1" role="radio">BASKET</button>
        </div></div>
        <div id="tkBskOpts">
        <div class="frow"><span>Trade-yada basket-ka <small>2 – 5</small></span><span class="stp"><button type="button" id="mTKBNm" aria-label="Ka yaree">−</button><b id="mTKBN">3</b><button type="button" id="mTKBNp" aria-label="Ku dar">+</button></span></div>
        <div class="frow frow-col" id="rTKBENT"><span style="margin-bottom:6px">Lakabyada <small>sida lakab cusub loo furo</small></span><div class="seg" id="mTKBENT" role="radiogroup" aria-label="Lakabyada">
          <button type="button" data-v="0" role="radio">ISKU MAR</button>
          <button type="button" data-v="1" role="radio">SIGNAL KASTA</button>
        </div></div>
        <div class="frow frow-col" id="rTKBEX"><span style="margin-bottom:6px">Xidhitaanka</span><div class="seg" id="mTKBEX" role="radiogroup" aria-label="Xidhitaanka">
          <button type="button" data-v="0" role="radio">TP KASTA</button>
          <button type="button" data-v="1" role="radio">WADAJIR $</button>
        </div></div>
        <div class="frow" id="rTKBTGT"><span>Bartilmaameed basket <small>faa'iidada guud → xidh dhammaan</small></span><span><input id="mTKBTGT" type="number" min="0" max="10000" step="0.5" value="3"> <i>$</i></span></div>
        <div class="frow"><span>Break-even basket <small>faa'iido $X → SL wadaag = celceliska entry · 0 = off</small></span><span><input id="mTKBBE" type="number" min="0" max="10000" step="0.5" value="1.5"> <i>$</i></span></div>
        <div class="frow"><span>Lot-ka balance-ka la kora <small>0.01 lot $X kasta · 0 = off</small></span><span><input id="mTKBLP" type="number" min="0" max="1000000" step="50" value="0"> <i>$</i></span></div>
        </div>
        <div class="sltph" id="mTKBHint">—</div>
        </div>
        <div class="sltph" id="mTKHint">—</div>
      </div>
        <!-- v12.24 (EA v70.9): 📏 CABBIR LAMAANE -->
        <div class="grp bskg" id="cbGrp">
          <div class="gh bskgh"><span class="gold">📏 CABBIR LAMAANE <small>TICK + BASKET · lamaane kasta</small></span><button class="sw" id="mCBON" type="button" aria-label="Cabbir lamaane"><i></i></button></div>
          <div class="sltph">Sitinka $ ee TICK / BASKET waa <b>qiimaha dahabka</b>. Bot-ku wuxuu cabbiraa <b>dhaqdhaqaaqa lamaane kasta (ATR)</b> → sitin kasta × <b>CABBIR</b> (fiddo ≈ ×0.02 · BTC ≈ ×60 · US30 ≈ ×10). Khatarta % isma beddesho. <b>EA v70.9+</b></div>
          <div class="frow frow-col"><span style="margin-bottom:6px">ATR timeframe <small>dhaqdhaqaaqa lagu cabbiro</small></span><div class="seg" id="mCBTF" role="radiogroup" aria-label="ATR timeframe">
            <button type="button" data-v="0" role="radio">M5</button><button type="button" data-v="1" role="radio">M15</button><button type="button" data-v="2" role="radio">H1</button></div></div>
          <div class="frow frow-col"><span style="margin-bottom:6px">Muddada <small>inta maalmood ee la celceliyo</small></span><div class="seg" id="mCBD" role="radiogroup" aria-label="Muddada ATR">
            <button type="button" data-v="1" role="radio">1 maalin</button><button type="button" data-v="2" role="radio">2</button><button type="button" data-v="4" role="radio">4</button><button type="button" data-v="6" role="radio">6</button></div></div>
          <div class="frow frow-col"><span style="margin-bottom:6px">Spread xadka <small>spread &gt; X% SL-ka → TICK ma galo</small></span><div class="seg" id="mCBSQ" role="radiogroup" aria-label="Spread xadka">
            <button type="button" data-v="0" role="radio">OFF</button><button type="button" data-v="15" role="radio">15%</button><button type="button" data-v="25" role="radio">25%</button><button type="button" data-v="40" role="radio">40%</button></div></div>
          <div class="sltph" id="mCBHint">—</div>
          <button type="button" class="cbgo" id="cbGo">💱 Lamaanaha → ⚡ TICK / 🧺 BASKET dooro</button>
        </div>
    </div><div class="ingen" data-c="TICK"></div></div>
    <div class="incat" data-c="GRID"><div class="card incard">
      <!-- v12.25 (EA v71.0): 🪜 GRID STOP -->
      <div class="grp bskg" id="grGrp">
        <div class="gh bskgh"><span class="gold">🪜 GRID STOP <small>xeelad keli ah · magic gooni</small></span><button class="sw" id="mGRON" type="button" aria-label="GRID STOP"><i></i></button></div>
        <div class="sltph">Amarro <b>stop (virtual)</b> oo jihada trend-ka oo keliya ah · lot isku mid (martingale ma jiro) · <b>SL guud</b> · 🔒 quful · 🎯 TP guud. Range / war / spread → grid ma dhigo. <b>EA v71.0+</b></div>
        <div class="frow"><span>GRID OO KELIYA <small>shidan → TICK · BASKET · ASIA · SR/SMC chart-kan ma furaan (natiijo saafi)</small></span><button class="sw" id="mGRONLY" type="button" aria-label="GRID oo keliya"><i></i></button></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">Jihada <small>TREND = 🧭 jihada suuqa (ammaan)</small></span><div class="seg" id="mGRDIR" role="radiogroup" aria-label="Jihada grid-ka">
          <button type="button" data-v="0" role="radio">🧭 TREND</button><button type="button" data-v="1" role="radio">BUY oo keliya</button><button type="button" data-v="2" role="radio">SELL oo keliya</button></div></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">Xoogga jihada ugu yar <small>ka yar = RANGE → grid ma jiro</small></span><div class="seg" id="mGRSTR" role="radiogroup" aria-label="Xoogga jihada">
          <button type="button" data-v="30" role="radio">30%</button><button type="button" data-v="40" role="radio">40%</button><button type="button" data-v="50" role="radio">50%</button><button type="button" data-v="60" role="radio">60%</button></div></div>
        <div class="frow"><span>Masaafada <small>$ qiime dahab (📏 × cabbir) · 0 = AUTO (ATR)</small></span><span><input id="mGRSTEP" type="number" min="0" max="1000" step="0.05" value="0"> <i>$</i></span></div>
        <div class="frow"><span>AUTO: × ATR M5 <small>0.05 – 1.0</small></span><span><input id="mGRSATR" type="number" min="0.05" max="1" step="0.01" value="0.15"> <i>×</i></span></div>
        <div class="frow"><span>Masaafo ≥ × spread <small>1 – 10 (ka yar → sug)</small></span><span><input id="mGRSPX" type="number" min="1" max="10" step="0.5" value="3"> <i>×</i></span></div>
        <div class="frow"><span>Lakab ugu badan <small>2 – 30</small></span><span><input id="mGRLV" type="number" min="2" max="30" step="1" value="12"> <i>#</i></span></div>
        <div class="frow"><span>Lot lakab kasta <small>isku mid · martingale ma jiro</small></span><span><input id="mGRLOT" type="number" min="0.01" max="100" step="0.01" value="0.01"> <i>lot</i></span></div>
        <div class="frow"><span>🎯 TP guud <small>faa'iidada basket-ka $ · 0 = off</small></span><span><input id="mGRTP" type="number" min="0" max="100000" step="1" value="12"> <i>$</i></span></div>
        <div class="frow"><span>🔒 Quful: bilow <small>faa'iido $X kadib · 0 = off</small></span><span><input id="mGRLK" type="number" min="0" max="100000" step="1" value="6"> <i>$</i></span></div>
        <div class="frow"><span>🔒 Quful: ilaali <small>% faa'iidada ugu sarreysa · 10 – 90</small></span><span><input id="mGRLKP" type="number" min="10" max="90" step="5" value="50"> <i>%</i></span></div>
        <div class="frow"><span>🛑 SL guud <small>khasaaraha basket-ka % balance · 0.1 – 5</small></span><span><input id="mGRSL" type="number" min="0.1" max="5" step="0.1" value="0.5"> <i>%</i></span></div>
        <div class="frow"><span>SL broker trade kasta <small>X lakab (internet go'a) · 2 – 30</small></span><span><input id="mGRHL" type="number" min="2" max="30" step="1" value="6"> <i>#</i></span></div>
        <div class="frow"><span>SL kadib sug <small>daqiiqo · 0 = off</small></span><span><input id="mGRCD" type="number" min="0" max="1440" step="5" value="30"> <i>daq</i></span></div>
        <div class="frow"><span>Grid maalintii <small>ugu badan · 1 – 50</small></span><span><input id="mGRMD" type="number" min="1" max="50" step="1" value="4"> <i>#</i></span></div>
        <div class="frow"><span>Khasaaraha maalinlaha <small>% balance → maanta jooji · 0 = off</small></span><span><input id="mGRDL" type="number" min="0" max="50" step="0.5" value="2"> <i>%</i></span></div>
        <div class="frow"><span>📰 War xoog leh <small>grid / lakab cusub ma jiro</small></span><button class="sw" id="mGRNW" type="button" aria-label="War"><i></i></button></div>
        <div class="frow"><span>Saacadaha (server) <small>bilow – dhammaad</small></span><span><input id="mGRHS" type="number" min="0" max="23" step="1" value="1" style="width:56px"> – <input id="mGRHE" type="number" min="1" max="24" step="1" value="22" style="width:56px"></span></div>
        <div class="frow"><span>Grid ugu dheer <small>daqiiqo → xidh · 0 = off</small></span><span><input id="mGRMM" type="number" min="0" max="10080" step="10" value="0"> <i>daq</i></span></div>
        <div class="sltph" id="mGRHint">—</div>
      </div>
    </div><div class="ingen" data-c="GRID"></div></div>
    <div class="incat" data-c="ZONE"><div class="card incard">
      <!-- v12.22 (EA v70.8): 🎯 ZONE YAR -->
      <div class="grp bskg" id="znGrp">
        <div class="gh bskgh"><span class="gold">🎯 ZONE YAR <small>XAUUSD · TICK</small></span><button class="sw" id="mZNON" type="button" aria-label="ZONE YAR"><i></i></button></div>
        <div class="sltph">Zone-yo <b>khafiif ah oo xoog leh</b> (×taabasho) ayaa si toos ah loo sawiraa. TICK wuxuu ka galaa <b>zone-ka oo keliya</b>: SELL saqafka · BUY dabaqa · SL zone-ka geeskiisa · TP zone-ka xiga. <b>EA v70.8+</b></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">Timeframe-ka ganacsiga <small>LABADA = zone M5 + shumac M1 xaqiijin</small></span><div class="seg" id="mZNTF" role="radiogroup" aria-label="Timeframe-ka zone-ka">
          <button type="button" data-v="0" role="radio">M1</button><button type="button" data-v="1" role="radio">M5</button><button type="button" data-v="2" role="radio">LABADA</button></div></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">Chart-ka MT5 <small>qari = chart nadiif · bot-ku wuu sii ganacsanayaa</small></span><div class="seg" id="mZNSH" role="radiogroup" aria-label="Chart-ka MT5">
          <button type="button" data-v="1" role="radio">👁 MUUJI</button><button type="button" data-v="0" role="radio">🙈 QARI</button></div></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">Xoogga zone-ka <small>taabasho ugu yar · ka yar = lama sawiro</small></span><div class="seg" id="mZNT" role="radiogroup" aria-label="Xoogga zone-ka">
          <button type="button" data-v="2" role="radio">×2</button><button type="button" data-v="3" role="radio">×3</button><button type="button" data-v="4" role="radio">×4</button><button type="button" data-v="5" role="radio">×5</button></div></div>
        <div class="frow"><span>Ballaca ugu badan <small>× ATR · 0.05 – 1.0 (yar = khafiif)</small></span><span><input id="mZNW" type="number" min="0.05" max="1" step="0.05" value="0.25"> <i>×</i></span></div>
        <div class="frow"><span>Zone-yada la hayo <small>kuwa qiimaha ugu dhow · 1 – 10</small></span><span><input id="mZNMX" type="number" min="1" max="10" step="1" value="5"></span></div>
        <div class="frow"><span>SL zone-ka geeskiisa <small>± $ · 0.1 – 20</small></span><span><input id="mZNSL" type="number" min="0.1" max="20" step="0.1" value="1"> <i>$</i></span></div>
        <div class="frow frow-col"><span style="margin-bottom:6px">TP</span><div class="seg" id="mZNTP" role="radiogroup" aria-label="TP">
          <button type="button" data-v="0" role="radio">ZONE-KA XIGA</button><button type="button" data-v="1" role="radio">TICK CAADI</button></div></div>
        <div class="sltph" id="mZNHint">—</div>
      </div>
    </div></div>
    <div class="incat" data-c="GOLD"><div class="card incard">
      <!-- v12.7: GOLD BASKET (EA v69.0) -->
      <div class="grp bskg" id="bskGrp">
        <div class="gh bskgh"><span class="gold"><i class="coin"></i>🥇 GOLD BASKET <small>EMA · XAUUSD</small></span><button class="sw" id="mBSK" type="button" aria-label="GOLD BASKET"><i></i></button></div>
        <div class="sltph">Marka ON: <b>XAUUSD</b> signal kasta hal trade halkii, <b>basket</b> ayuu furaa. Lammaanayaasha kale sidooda ayay u shaqeeyaan. Grid / Martingale ma jiraan.</div>
        <div class="bsub">0 · SIGNAL-KA DAHABKA</div>
        <div class="frow frow-col" id="rBSKSIG"><div class="sg3" id="mBSKSIG" role="radiogroup" aria-label="Signal-ka dahabka" style="grid-template-columns:repeat(4,1fr)">
          <button type="button" data-v="0" role="radio">ZONE<small>demand / supply</small></button>
          <button type="button" data-v="1" role="radio">EMA<small>pullback</small></button>
          <button type="button" data-v="2" role="radio">LABADA<small>zone + EMA</small></button>
          <button type="button" data-v="3" role="radio">DOUBLE<small>top / bottom</small></button>
        </div></div>
        <div class="sltph" id="mBSKSIGHint">—</div>
        <div id="bskDbl">
          <div class="frow"><span>Timeframe <small>DOUBLE · la tijaabiyay</small></span><span><b>M15</b></span></div>
          <div class="frow"><span>Dulqaadka labada sal <small>x ATR · ka yar = tayo sare</small></span><span><input id="mBSKDTOL" type="number" min="0.05" max="1" step="0.05" value="0.2"> <i>ATR</i></span></div>
          <div class="frow"><span>Neckline ugu yar <small>x ATR · salka ka sarreeya</small></span><span><input id="mBSKDNECK" type="number" min="0.3" max="3" step="0.1" value="1"> <i>ATR</i></span></div>
          <div class="frow"><span>TP <small>lakab kasta · x R</small></span><span><input id="mBSKDTP" type="number" min="1" max="5" step="0.5" value="2"> <i>R</i></span></div>
        </div>
        <div class="frow"><span>Basket maalintii</span><span class="stp"><button type="button" id="mBSKDAYm" aria-label="Ka yaree">−</button><b id="mBSKDAY">3</b><button type="button" id="mBSKDAYp" aria-label="Ku dar">+</button></span></div>
        <div class="bsub">1 · BASKET-KA</div>
        <div class="frow"><span>Trade-yada basket-ka <small>2 – 5</small></span><span class="stp"><button type="button" id="mBSKNm" aria-label="Ka yaree">−</button><b id="mBSKN">3</b><button type="button" id="mBSKNp" aria-label="Ku dar">+</button></span></div>
        <div class="frow frow-col" id="rBSKENT"><div class="seg" id="mBSKENT" role="radiogroup" aria-label="Sida loo galo">
          <button type="button" data-v="0" role="radio">LAKAB · zone-ka gudihiisa</button>
          <button type="button" data-v="1" role="radio">ISKU MAR</button>
        </div></div>
        <div class="sltph" id="mBSKENTHint">—</div>
        <div class="bsub">2 · KHATARTA (basket oo dhan)</div>
        <div class="frow"><span>Risk basket-ka oo dhan <small>waa la qaybiyaa, ma labanlaabmo</small></span><span><input id="mBSKRISK" type="number" min="0.05" max="5" step="0.05"> <i>%</i></span></div>
        <div class="frow"><span>Khasaaraha ugu badan <small>xad adag — gaadho = xidh dhammaan</small></span><span><input id="mBSKMAX" type="number" min="0" max="100000" step="1"> <i>$</i></span></div>
        <div class="frow"><span>Spread ugu badan <small>sent · 35 = $0.35</small></span><span><input id="mBSKSPR" type="number" min="5" max="500" step="1"> <i>¢</i></span></div>
        <div class="bsub">3 · FAA'IIDADA (TP)</div>
        <div class="frow frow-col" id="rBSKTP"><div class="seg" id="mBSKTP" role="radiogroup" aria-label="TP-ga basket-ka">
          <button type="button" data-v="0" role="radio">JARANJAR · 1R 2R 3R</button>
          <button type="button" data-v="1" role="radio">WADAJIR · $</button>
        </div></div>
        <div class="frow" id="rBSKTGT"><span>Bartilmaameedka WADAJIR</span><span><input id="mBSKTGT" type="number" min="1" max="100000" step="1"> <i>$</i></span></div>
        <div class="bsub">4 · ILAALINTA</div>
        <div class="frow"><span>TP1 kadib → SL = Break-even <small>basket-ku ma khasaari karo</small></span><span><button class="sw" id="mBSKBE" type="button" aria-label="TP1 kadib break-even"><i></i></button></span></div>
        <div class="frow"><span>Qufulka faa'iidada <small>50% faa'iidada ugu sarreysa ha lumin</small></span><span><button class="sw" id="mBSKLOCK" type="button" aria-label="Qufulka faa'iidada"><i></i></button></span></div>
        <div class="frow"><span>Waqtiga dahabka <small>London + NY · 07:00 – 17:00 GMT</small></span><span><button class="sw" id="mBSKSES" type="button" aria-label="Waqtiga dahabka"><i></i></button></span></div>
        <div class="frow"><span>2 basket oo khasaara → jihadaas jooji maanta</span><span><button class="sw" id="mBSKBLK" type="button" aria-label="Jihada jooji"><i></i></button></span></div>
      </div>
    </div><div class="ingen" data-c="GOLD"></div></div>
    <div class="incat" data-c="ASIA"><div class="card incard">
      <!-- v12.9: ASIA BREAKOUT (EA v69.7) -->
      <div class="grp bskg" id="asGrp">
        <div class="gh bskgh"><span class="gold"><i class="coin"></i>🌅 ASIA BREAKOUT <small>XAUUSD</small></span><button class="sw" id="mASIA" type="button" aria-label="ASIA BREAKOUT"><i></i></button></div>
        <div class="bsub" style="border-top:none;margin-top:0">XEELADDA DAHABKA</div>
        <div class="frow frow-col" id="rGOLD"><div class="sg3" id="mGOLD" role="radiogroup" aria-label="Xeeladda dahabka">
          <button type="button" data-v="0" role="radio">EMA BASKET<small>(hore)</small></button>
          <button type="button" data-v="1" role="radio">ASIA<small>breakout</small></button>
          <button type="button" data-v="2" role="radio">LABADA<small>mid kasta</small></button>
        </div></div>
        <div class="sltph">Asia (00–07 GMT) sare iyo hoos ayaa la calaamadeeyaa → London jebinta ayaa la raacaa. <b>Maalintii 1 trade.</b> Baaritaan 6 bilood: 97 trade · win 50% · <b>+29R</b> · PF 1.67.</div>
        <div class="bsub">1 · WAQTIGA (GMT)</div>
        <div class="frow"><span>Asia range <small>sare / hoos laga qaado</small></span><span><b>00 → 07</b></span></div>
        <div class="frow"><span>Daaqadda jebinta <small>07 → saacaddan · kadib maanta ma furmo</small></span><span class="stp"><button type="button" id="mASIAWEm" aria-label="Ka yaree">−</button><b id="mASIAWE">12</b><button type="button" id="mASIAWEp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>Xidh maalinta <small>trade furan → la xidhaa · Jimce 20 ugu dambeyn</small></span><span class="stp"><button type="button" id="mASIACLm" aria-label="Ka yaree">−</button><b id="mASIACL">20</b><button type="button" id="mASIACLp" aria-label="Ku dar">+</button></span></div>
        <div class="bsub">2 · GELITAANKA</div>
        <div class="frow frow-col" id="rASIADIR"><div class="seg" id="mASIADIR" role="radiogroup" aria-label="Jihada">
          <button type="button" data-v="0" role="radio">LABADA</button>
          <button type="button" data-v="1" role="radio">BUY</button>
          <button type="button" data-v="2" role="radio">SELL</button>
        </div></div>
        <div class="frow"><span>Trend H4 (EMA50) <small>jebinta trend-ka raacda oo keliya</small></span><span><button class="sw on" id="mASIATR" type="button" aria-label="Trend H4"><i></i></button></span></div>
        <div class="frow"><span>Range ugu weyn <small>ka weyn → maanta ma ganacsado · 0 = off</small></span><span><input id="mASIAMAXR" type="number" min="0" max="1000" step="1" value="90"> <i>$</i></span></div>
        <div class="bsub">3 · SL / TP</div>
        <div class="frow frow-col" id="rASIASL"><div class="seg" id="mASIASL" role="radiogroup" aria-label="Stop Loss">
          <button type="button" data-v="0" role="radio">BARTAMAHA RANGE</button>
          <button type="button" data-v="1" role="radio">DHINACA KALE</button>
        </div></div>
        <div class="frow"><span>Take Profit <small>R = masaafada SL-ka</small></span><span class="stp"><button type="button" id="mASIATPm" aria-label="Ka yaree">−</button><b id="mASIATP">2.0</b><button type="button" id="mASIATPp" aria-label="Ku dar">+</button></span></div>
        <div class="sltph" id="mASIASLHint">—</div>
        <div class="bsub">4 · KHATARTA</div>
        <div class="frow"><span>Risk trade kasta <small>% balance · lakabyada oo dhan</small></span><span><input id="mASIARISK" type="number" min="0.05" max="5" step="0.05" value="1"> <i>%</i></span></div>
        <div class="frow"><span>Khasaaraha ugu badan <small>xad adag — gaadho = xidh · 0 = off</small></span><span><input id="mASIAMAX" type="number" min="0" max="100000" step="1" value="30"> <i>$</i></span></div>
        <div class="sltph aswarn" id="mASIARiskHint" hidden>—</div>
        <div class="bsub">5 · BASKET (ikhtiyaari)</div>
        <div class="frow frow-col" id="rASIAL"><div class="sg3" id="mASIAL" role="radiogroup" aria-label="Lakabyada">
          <button type="button" data-v="1" role="radio">1<small>hal trade</small></button>
          <button type="button" data-v="2" role="radio">2<small>lakab</small></button>
          <button type="button" data-v="3" role="radio">3<small>lakab</small></button>
        </div></div>
        <div class="sltph" id="mASIALHint">—</div>
        <!-- v12.10: GRID / MARTINGALE ilaalin leh (EA v69.8) -->
        <div class="bsub asgh">6 · GRID / MARTINGALE <span><button class="sw" id="mASG" type="button" aria-label="Grid / Martingale"><i></i></button></span></div>
        <div id="asgBox">
        <div class="frow frow-col" id="rASGM"><div class="seg" id="mASGM" role="radiogroup" aria-label="Nooca grid-ka">
          <button type="button" data-v="0" role="radio">GRID · lot isku mid</button>
          <button type="button" data-v="1" role="radio">MARTINGALE · lot kordha</button>
        </div></div>
        <div class="frow"><span>Lakabyada <small>#1 + kuwa kale (2–5)</small></span><span class="stp"><button type="button" id="mASGLm" aria-label="Ka yaree">−</button><b id="mASGL">3</b><button type="button" id="mASGLp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>Masaafada lakabyada <small>R = entry → SL (0.15–0.33)</small></span><span class="stp"><button type="button" id="mASGSm" aria-label="Ka yaree">−</button><b id="mASGS">0.25</b><button type="button" id="mASGSp" aria-label="Ku dar">+</button></span></div>
        <div class="frow" id="rASGX"><span>Lot kordhin <small>MARTINGALE oo keliya (1.0–2.0)</small></span><span class="stp"><button type="button" id="mASGXm" aria-label="Ka yaree">−</button><b id="mASGX">×2.0</b><button type="button" id="mASGXp" aria-label="Ku dar">+</button></span></div>
        <div class="aslots" id="mASGLots"></div>
        <div class="sltph" id="mASGHint">—</div>
        <div class="aslock"><div>🔒 <b>SL wadaag</b> = bartamaha range-ka — lakab kasta isla SL</div><div>🔒 <b>Risk guud ≤ risk %</b> — lot-ka waxaa loo xisaabiyaa sidii dhammaan buuxsameen</div><div>🔒 <b>TP asal</b> — dhammaan isla TP · 20:00 GMT xidh</div><div>🔒 <b>Bilaa SL = MAYA</b></div></div>
        <div class="sltph">Tijaabo 6 bilood: MARTINGALE 3 ×2 0.25R = <b>+50R · PF 2.21</b> · GRID 3 = +43R · hal trade = +29R. Bilaa SL: −39R hal maalin ✕</div>
        </div>
      </div>
    </div><div class="ingen" data-c="ASIA"></div></div>
    <div class="incat" data-c="MAIN"><div class="card incard inmain">
      <div class="grp"><div class="gh">🧭 XEELADDA · ZONE · FILTER</div>
        <div class="frow frow-col" id="rSTRAT"><div class="sg3" id="mSTRAT" role="radiogroup" aria-label="Xeeladda">
          <button type="button" data-s="0" role="radio">SR/SD<small>zone S&amp;D</small></button>
          <button type="button" data-s="1" role="radio">SMC<small>Order Block</small></button>
          <button type="button" data-s="2" role="radio">LABADA<small>2-da</small></button>
        </div></div>
        <div class="frow"><span>Sniper <small>(CHoCH M5 gudaha zone / OB)</small></span><span><button class="sw" id="mSNIPER" type="button" aria-label="Sniper"><i></i></button></span></div>
        <!-- v12.8 (EA v69.6): heerka zone-ka -->
        <div class="frow"><span>Heerka zone-ka <small>inta zone ee la aqbalayo</small></span></div>
        <div class="frow frow-col" id="rSDPROF"><div class="sg3" id="mSDPROF" role="radiogroup" aria-label="Heerka zone-ka">
          <button type="button" data-v="0" role="radio">TAYO<small>adag · yar</small></button>
          <button type="button" data-v="1" role="radio">DHEXE<small>isku dheelli</small></button>
          <button type="button" data-v="2" role="radio">BADAN<small>trade badan</small></button>
        </div></div>
        <table class="sdtbl" id="sdTbl"><tr><th></th><th data-c="0">TAYO</th><th data-c="1">DHEXE</th><th data-c="2">BADAN</th></tr>
          <tr><td>Impulse ugu yar</td><td data-c="0">25p</td><td data-c="1">20p</td><td data-c="2">15p</td></tr>
          <tr><td>BOS khasab</td><td data-c="0">✓</td><td data-c="1">✓</td><td data-c="2">✕</td></tr>
          <tr><td>Taabasho (saac)</td><td data-c="0">6</td><td data-c="1">8</td><td data-c="2">12</td></tr>
          <tr><td>Gelitaan ≤</td><td data-c="0">8p</td><td data-c="1">10p</td><td data-c="2">12p</td></tr></table>
        <div class="sltph" id="mSDPROFHint">—</div>
        <div class="frow"><span>Trade maalintii <small>(SR + SMC wadaag · chart-yada oo dhan)</small></span><span class="stp"><button type="button" id="mSNDAYm" aria-label="Ka yaree">−</button><b id="mSNDAY">3</b><button type="button" id="mSNDAYp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>EMA filter <small>(EMA50 H1 + EMA200 H4 · RANGE → ma ganacsado)</small></span><span><button class="sw" id="mEMAF" type="button" aria-label="EMA filter"><i></i></button></span></div>
        <div class="frow frow-col" id="rEMA"><div class="echip" id="emaChip" hidden><i></i><span>EMA</span></div></div>
        <div class="frow"><span>★ ugu yar</span><span><span class="seg seg-s" id="mSTARS" role="radiogroup" aria-label="★ ugu yar"><button type="button" data-v="1" role="radio">★</button><button type="button" data-v="2" role="radio">★★</button><button type="button" data-v="3" role="radio">★★★</button></span></span></div>
        <div class="frow"><span>Lot-ka ★★ <small>(★★★ = 100%)</small></span><span><input id="mLOT2" type="number" min="10" max="100" step="5"> <i>%</i></span></div>
        <div class="frow"><span>Filtarka wararka <small>(jooji warka ka hor/kadib)</small></span><span><button class="sw" id="mNEWS" type="button" aria-label="Filtarka wararka"><i></i></button></span></div>
      </div>

      <div class="grp"><div class="gh">🎯 SL / TP (SNIPER)</div>
        <div class="sltph"><b>SL</b> = zone-ka / OB-ga gadaashiisa · <b>TP</b> = SL × RR. Pip go'an ma jiro.</div>
        <div class="frow"><span>RR <small>(TP = SL × …)</small></span><span><input id="mSNRR" type="number" min="1" max="10" step="0.5"> <i>R</i></span></div>
        <div class="frow"><span>SL ugu weyn <small>(SR iyo SMC · ka weyn → trade ma furmo)</small></span><span><input id="mSNSLMAX" type="number" min="0" max="500" step="1"> <i>pip</i></span></div>
      </div>

      <div class="grp"><div class="gh">🛡 ILAALINTA FAA'IIDADA</div>
        <div class="seg" id="mPROT" role="radiogroup" aria-label="Ilaalinta faa'iidada">
          <button type="button" data-p="0" role="radio">DAMMAN</button>
          <button type="button" data-p="1" role="radio">BREAK-EVEN</button>
          <button type="button" data-p="2" role="radio">TALLAABO</button>
        </div>
        <div class="sltph" id="mPROTHint">—</div>
        <div class="frow" id="rSTEPSTART"><span>Bilowga <small>(faa'iidada loo baahan yahay)</small></span><span><input id="mSTEPSTART" type="number" min="1" max="5000" step="1"> <i>pip</i></span></div>
        <div class="frow" id="rSTEP"><span>Tallaabo kasta <small>(TALLAABO oo keliya)</small></span><span><input id="mSTEP" type="number" min="1" max="5000" step="1"> <i>pip</i></span></div>
      </div>
    </div><div class="ingen" data-c="MAIN"></div></div>
    <div class="incat" data-c="PROT"><div class="ingen" data-c="PROT"></div></div>
    <div class="incat" data-c="FILT"><div class="ingen" data-c="FILT"></div></div>
    <div class="incat" data-c="SYS"><div class="ingen" data-c="SYS"></div></div>
    <div class="insave" id="inSave">
      <div class="acts" style="margin-top:14px;grid-template-columns:2fr 1fr">
        <button class="act go" id="mSend" style="flex-direction:row;gap:9px;padding:14px">KAYDI &amp; DIR</button>
        {% if not lic %}<button class="act" id="mReset" style="flex-direction:row;gap:8px;padding:14px;border-color:var(--line);color:var(--ink2)">CELI</button>{% endif %}
      </div>
      <div class="note" id="inCnt" hidden></div>
      <div class="note" id="mNote">Bot-ku wuxuu hadda isticmaalayaa: —</div>

    </div>
    <div class="note" style="margin:10px 4px 0">Sitinkan ayaa bot-ka u ah <b>.set</b>: server-ka ayuu ku kaydsan yahay, <b>chart kasta</b> wuu gaadhayaa, MT5 ama VPS dib u kicin → bot-ku halkan ayuu ka soo qaadanayaa (EA v67.4+).</div>
  </section>
  {% endif %}

  <!-- ============ TRADE ============ -->
  <section class="pane" id="pTrade">
    <!-- v9.1: sawirrada trade-yada (EA v67.2+) -->
    <div class="card" style="margin-bottom:16px">
      <h2>Sawirrada trade-yada <span class="cnt" id="cShots"></span></h2>
      <div class="flt" id="shFlt">
        <button type="button" class="on" data-f="all">Dhammaan</button>
        <button type="button" data-f="win">Faa'iido</button>
        <button type="button" data-f="loss">Khasaare</button>
        <button type="button" data-f="open">Furan</button>
      </div>
      <div id="shList"><div class="empty" style="padding:18px 0">Soo raraya…</div></div>
      <div class="note" id="shNote"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Trade-yada furan <span class="cnt" id="cOpen"></span></h2>
      <div class="scroll xscroll"><table id="tt">
        <thead><tr><th>Symbol</th><th>Nooc</th><th>Xeelad</th><th>Lots</th>
          <th style="text-align:right">P/L</th></tr></thead>
        <tbody><tr><td colspan="5" class="empty">Wax lama helin.</td></tr></tbody>
      </table></div>
    </div>
    <div class="card">
      <h2>Trade-yada la xidhay <span class="cnt" id="cCls"></span></h2>
      <div class="scroll xscroll"><table id="tc">
        <thead><tr><th>Xidhmay</th><th>Symbol</th><th>Nooc</th><th>Xeelad</th>
          <th>Lots</th><th style="text-align:right">Points</th>
          <th style="text-align:right">P/L</th></tr></thead>
        <tbody><tr><td colspan="7" class="empty">Wax lama helin.</td></tr></tbody>
      </table></div>
      <div class="note" id="clsSum"></div>
    </div>
  </section>

  <!-- ============ ANALIIS (v7) ============ -->
  <section class="pane" id="pAnaliis">
    <!-- v12.3: HEERARKA & RANGE (EA v67.7+) -->
    <div id="lvWrap">
      <div class="lvsyms" id="lvSyms" role="tablist" aria-label="Symbol"></div>
      <div id="lvBody" hidden>
        <div class="card" style="margin-bottom:12px">
          <div class="lvhd"><p class="sec-t" style="margin:0">Heerarka &amp; Range</p><span class="lvpill" id="lvPill">—</span></div>
          <div class="note" id="lvSub" style="margin:2px 0 8px">—</div>
          <div id="lvChart"></div>
          <div class="lvleg"><span><i style="background:var(--s1)"></i>Qiimaha</span><span><i style="background:#f0cf86"></i>Dhaqaaqa la filayo</span><span><i style="background:#26aa6e"></i>Demand</span><span><i style="background:#d03b3b"></i>Supply</span></div>
        </div>
        <div class="card" style="margin-bottom:12px"><div class="lvk">Dhaqaaqa la filayo (ATR D1) · maalin</div>
          <div class="lvbig" id="lvRange">—</div><div class="note" id="lvRangeS" style="margin:0">—</div></div>
        <div class="lvbot" id="lvBot" style="margin-bottom:12px">—</div>
        <div class="card" style="margin-bottom:12px"><div class="lvk">Caddaynta zone kasta</div><div id="lvZones"></div></div>
        <div class="card" style="margin-bottom:12px"><div class="lvk">Maxaa beddeli kara aragtida?</div><ul class="lvchg" id="lvChg"></ul></div>
        <div class="card" style="margin-bottom:16px"><div class="lvk" style="margin-bottom:8px">Xogta laga soo qaaday</div>
          <div class="lvsrc"><div><b>ZONE</b><span>Supply &amp; Demand (bot-ka)</span></div><div><b>TIJAABOOYIN</b><span>Taabasho · xooga ka laabashada</span></div>
          <div><b>JIHADA</b><span>H4 · EMA200</span></div><div><b>DHAQAAQA</b><span>ATR D1 (14)</span></div></div></div>
      </div>
      <div class="card" id="lvEmpty" style="margin-bottom:16px" hidden><p class="sec-t">Heerarka &amp; Range</p>
        <div class="note" style="margin:0">Weli xog lama helin. Bot-ka <b>v67.7</b> ku cusboonaysii (F7) — chart kasta 1 daqiiqo gudaheed ayuu soo diraa zone-yada, ATR-ka iyo shumacyada.</div></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="zh" style="margin-bottom:12px">
        <p class="sec-t" style="margin:0">Analiiska bot-ka</p>
        <span class="note" id="anAge">—</span>
      </div>
      <div id="anList"></div>
      <div class="note" id="anNote" style="margin-top:4px"></div>
    </div>

    <div class="card">
      <p class="sec-t">Wararka (news)</p>
      <div id="nwList"></div>
      <div class="note" id="nwNote"></div>
    </div>
  </section>

  <!-- ============ CHART ============ -->
  <!-- ============ JOURNAL ============ -->
  <section class="pane" id="pJournal">
    <!-- v7.2: caafimaadka bot-ka - baadhis toos ah -->
    <div class="card" style="margin-bottom:16px">
      <div class="zh" style="margin-bottom:12px">
        <p class="sec-t" style="margin:0">Caafimaadka bot-ka</p>
        <span class="note" id="hcSum">—</span>
      </div>
      <div id="hcList"></div>
    </div>
    <!-- v6: xogta EA-du dirayso + soo dejin -->
    <div class="card" style="margin-bottom:16px">
      <p class="sec-t">Xogta EA-du dirayso</p>
      <div class="frow"><span>La helay</span><span id="rawAge" class="mono">—</span></div>
      <div class="frow"><span>Cabbirka xogta</span><span id="rawSize" class="mono">—</span></div>
      <div class="acts" style="margin-top:12px;grid-template-columns:1fr 1fr">
        <a class="act" id="dlCsv" href="/api/export/trades.csv"
           style="flex-direction:row;gap:8px;padding:13px;text-decoration:none;border-color:var(--line);color:var(--ink)">CSV · trade-yada</a>
        <a class="act" id="dlJson" href="/api/export/snapshot.json"
           style="flex-direction:row;gap:8px;padding:13px;text-decoration:none;border-color:var(--line);color:var(--ink)">JSON · xogta EA</a>
      </div>
      <details style="margin-top:12px">
        <summary style="cursor:pointer;font-size:13px;color:var(--ink2)">Xogta oo dhan halkan ka eeg</summary>
        <pre id="rawBox" class="rawbox">—</pre>
      </details>
    </div>

    <div class="rng" id="rng">
      <button data-r="day">Maanta</button>
      <button data-r="week">Usbuuc</button>
      <button class="on" data-r="month">Bil</button>
      <button data-r="year">Sanad</button>
      <button data-r="all">Dhammaan</button>
    </div>

    <div class="grid">
      <div class="tile"><div class="k">Wadarta</div><div class="v" id="jNet">—</div></div>
      <div class="tile"><div class="k">Trade</div><div class="v neu" id="jN">—</div></div>
      <div class="tile"><div class="k">Win rate</div><div class="v neu" id="jWR">—</div></div>
      <div class="tile"><div class="k">Profit Factor</div><div class="v" id="jPF">—</div></div>
    </div>

    <div class="cols two" style="margin-bottom:16px">
      <div class="card"><p class="sec-t">Ugu fiican</p>
        <div class="bw" id="jBest"><span class="empty">—</span></div></div>
      <div class="card"><p class="sec-t">Ugu xun</p>
        <div class="bw" id="jWorst"><span class="empty">—</span></div></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2>Lammaanaha</h2>
      <div id="jSym"><p class="empty">Wax lama helin.</p></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2>Saacadaha (waqtiga broker-ka)</h2>
      <div id="jHour"><p class="empty">Wax lama helin.</p></div>
      <div class="note">Sadarka cagaaran = faa'iido. Casaan = khasaare.</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2>Xeeladaha</h2>
      <div id="jStrat"><p class="empty">Wax lama helin.</p></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h2>Maalin kasta</h2>
      <div class="scroll xscroll"><table id="jDay">
        <thead><tr><th>Maalin</th><th>Trade</th><th>Guul</th>
          <th style="text-align:right">Natiijo</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">Wax lama helin.</td></tr></tbody>
      </table></div>
    </div>

    <div class="card">
      <h2>Bil kasta</h2>
      <div class="scroll xscroll"><table id="jMon">
        <thead><tr><th>Bil</th><th>Trade</th><th>Guul</th>
          <th style="text-align:right">Natiijo</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">Wax lama helin.</td></tr></tbody>
      </table></div>
      <div class="note" id="jStore"></div>
    </div>
  </section>
</div>

<nav class="appbar">
  <button class="on" data-tab="Guud">
    <svg viewBox="0 0 24 24"><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1Z"/></svg>Guud</button>
  <button data-tab="Trade">
    <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/></svg>Trade</button>
  <button data-tab="Analiis">
    <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M11 8v3l2 2"/></svg>Analiis</button>
  {% if can_control %}
  <button data-tab="Maamul">
    <svg viewBox="0 0 24 24"><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/></svg>Maamul</button>
  <button data-tab="Input">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>Input</button>
  {% endif %}
  <button data-tab="Journal">
    <svg viewBox="0 0 24 24"><path d="M5 3h11l4 4v14H5Z"/><path d="M9 9h7M9 13h7M9 17h4"/></svg>Journal<span class="tbadge" id="hcBadge" style="display:none"></span></button>
  <button data-act="mus" id="navMus" type="button" aria-label="Muusig">   <!-- v12.21 -->
    <span class="nic"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><em class="neq"><i></i><i></i><i></i></em></span>Muusig</button>
  <button data-act="chat" id="navChat" type="button" aria-label="Fariimaha">
    <span class="nic"><svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.9A8 8 0 1 1 21 12Z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg><b class="nbd" id="navChatBd" hidden></b></span>Fariimo</button>
</nav>
<div class="chtoast" id="chToast" hidden role="status"><span>💬</span><span class="tx"><b>Fariin cusub</b> · <span id="chToastN"></span></span><button type="button" id="chToastGo">FUR</button><button type="button" class="x" id="chToastX" aria-label="Xidh">✕</button></div>
<div class="tip" id="tip"></div>
<div class="cfm" id="cfm" hidden role="dialog" aria-modal="true" aria-labelledby="cfmT"><div class="sh"><div class="gr"></div><h4 id="cfmT">—</h4><div class="bd" id="cfmB"></div><div class="ls" id="cfmL" hidden></div>
  <div class="bt"><button type="button" id="cfmOk" class="d">OK</button><button type="button" id="cfmNo" class="c">Ka noqo</button></div><button type="button" class="alt" id="cfmAlt" hidden>—</button><div class="nt" id="cfmN"></div></div></div>

<!-- v12.20: 🎵 MUUSIG -->
<div class="mubar" id="muBar" hidden>
  <span class="muhd" id="muHd" role="button" aria-label="Jiid player-ka">⋮⋮</span>
  <button class="mux mn" type="button" id="muMin" aria-label="Qari player-ka (heestu way socotaa)">–</button>
  <button class="mux cl" type="button" id="muX" aria-label="Jooji oo xidh">✕</button>
  <div class="art" id="muArt" role="button" tabindex="0" aria-label="Fur muusigga">🎵</div>
  <div class="mi" id="muMi" role="button" tabindex="0" aria-label="Fur muusigga"><b id="muT">—</b><small id="muS">—</small><div class="pb"><i id="muPb"></i></div></div>
  <button class="mubtn" type="button" id="muPrev" aria-label="Heestii hore">⏮</button>
  <button class="mubtn p" type="button" id="muPlay" aria-label="Shid / jooji">▶</button>
  <button class="mubtn" type="button" id="muNext" aria-label="Heesta xigta">⏭</button>
</div>
<div class="mububl" id="muBub" hidden role="button" tabindex="0" aria-label="Muusigga: taabo → player-ka · jiid → meel kale"><span class="eq"><i></i><i></i><i></i></span></div>
<div class="muhost" id="muHost"></div>
<audio id="muAudio" preload="metadata" playsinline></audio>
<input type="file" id="muFile" accept="audio/*,.mp3,.m4a,.aac,.ogg,.wav,.flac" multiple>
<div class="musheet" id="muSheet" hidden role="dialog" aria-modal="true" aria-labelledby="muTitle">
  <div class="mh"><b id="muTitle">🎵 Muusig</b><button type="button" id="muClose">✕ Xidh</button></div>
  <div class="mtop">
    <div class="museg" id="muSeg">
      <button type="button" data-c="SO" class="on">🇸🇴 Soomaali<i id="muNSO"></i></button>
      <button type="button" data-c="WO">🌍 Adduunka<i id="muNWO"></i></button>
      <button type="button" data-c="MY">⭐ Kuwayga<i id="muNMY"></i></button>
    </div>
    <div class="muslot" id="muSlot"><span class="big">🎧</span><span>Hees taabo si ay halkan uga shidanto</span></div>
    <div class="muctl">
      <button class="mubtn" type="button" id="muShuf" aria-label="Isku qas">🔀</button>
      <button class="mubtn" type="button" id="muPrev2" aria-label="Heestii hore">⏮</button>
      <button class="mubtn p" type="button" id="muPlay2" aria-label="Shid / jooji">▶</button>
      <button class="mubtn" type="button" id="muNext2" aria-label="Heesta xigta">⏭</button>
      <button class="mubtn" type="button" id="muRep" aria-label="Ku celi">🔁</button>
    </div>
  </div>
  <div class="mscr">
    <div class="muh"><span id="muLH">LIISKA</span><button type="button" id="muEdit">✎ Habee</button></div>
    <div class="mulist" id="muList"></div>

    <div class="muh"><span>KU DAR HEES</span></div>
    <div class="muadd">
      <input id="muUrl" type="url" inputmode="url" autocomplete="off" placeholder="Link-ga ku dheji (YouTube · Spotify · Audiomack)…" aria-label="Link-ga heesta">
      <button type="button" class="g" id="muPaste" aria-label="Dheji">📋</button>
      <button type="button" id="muAdd">+ KU DAR</button>
    </div>
    <div class="mucats" id="muCats">Liiska:
      <button type="button" data-c="SO">🇸🇴</button><button type="button" data-c="WO">🌍</button><button type="button" data-c="MY">⭐</button>
      <span style="flex:1"></span>
      <button type="button" id="muPick">📱 MP3 telefoonka</button>
    </div>
    <div class="mumsg" id="muMsg" role="status"></div>
    <div class="muhint">YouTube / Spotify-ga heesta ka fur → <b>Share → Copy link</b> → halkan ku dheji → <b>+ KU DAR</b>. Magaca iyo sawirka si toos ah ayaa loo soo qaadanayaa. <b>📱 MP3 telefoonka</b> = heesaha telefoonkaaga ku jira — kuwaas ayaa <b>BASS-ka app-ku</b> ka shaqeeyaa.</div>

    <div class="muh"><span id="muRH">RAADI · FANAANIINTA</span><button type="button" id="muWhere">YouTube</button></div>
    <div class="muchips" id="muChips"></div>

    <div class="muh"><span>🔊 BASS MACAAN</span></div>
    <div class="mueq">
      <div class="pre" id="muPre">
        <button type="button" data-p="0">Caadi</button>
        <button type="button" data-p="1">Bass macaan</button>
        <button type="button" data-p="2">Bass xoog</button>
        <button type="button" data-p="3">Cod cad</button>
      </div>
      <div class="sl"><span>Bass</span><input type="range" id="muBass" min="0" max="14" step="1" value="8" aria-label="Bass"><b id="muBassV">+8 dB</b></div>
      <div class="sl"><span>Sub (gunta)</span><input type="range" id="muSub" min="0" max="8" step="1" value="3" aria-label="Sub bass"><b id="muSubV">+3 dB</b></div>
      <div class="sl"><span>Treble</span><input type="range" id="muTre" min="-6" max="8" step="1" value="2" aria-label="Treble"><b id="muTreV">+2 dB</b></div>
      <div class="sl"><span>Codka</span><input type="range" id="muVol" min="0" max="100" step="1" value="90" aria-label="Codka"><b id="muVolV">90%</b></div>
      <div class="viz" id="muViz"></div>
      <div class="st" id="muEqSt"></div>
      <details class="mugd" id="muGd">
        <summary>📱 Bass-ka YouTube / Spotify — telefoonka ka shid</summary>
        <ol>
          <li><b>Samsung:</b> Settings → Sounds and vibration → Sound quality and effects → <b>Equalizer</b> → <b>Bass boost</b> (ama Custom: 63 Hz iyo 250 Hz kor u qaad). <b>Dolby Atmos</b> → shid → <b>Music</b>.</li>
          <li><b>Android kale (Xiaomi · Tecno · Infinix · Oppo):</b> Settings → Sound → <b>Sound effects / Audio effects / Dirac</b> → Bass ama <b>Rock / Hip-hop</b>.</li>
          <li><b>iPhone:</b> Settings → Music → <b>EQ → Bass Booster</b> (app-ka Music) · ama sameecadaha AirPods / Beats-ka app-kooda.</li>
          <li><b>Spotify app:</b> Settings → Playback → <b>Equalizer</b> → <b>Bass Booster</b>.</li>
          <li><b>Sameecad / Bluetooth speaker:</b> badhanka <b>BASS / EXTRA BASS</b> (JBL Bass Boost, Sony Extra Bass) — kan ayaa ugu xoog badan.</li>
        </ol>
      </details>
    </div>

    <div class="muh"><span>SITINKA</span></div>
    <div class="muopt"><div><b>Ku sii socodsii markaad tab beddesho</b><small>Guud · Trade · Input … heestu ma istaagto</small></div><button class="sw on" type="button" id="muOKeep" role="switch" aria-checked="true" aria-label="Ku sii socodsii"><i></i></button></div>
    <div class="muopt"><div><b>Hoos u dhig marka trade xidhmo (digniin)</b><small>codka 4 ilbiriqsi ayuu yaraanayaa + dhawaq yar</small></div><button class="sw on" type="button" id="muODuck" role="switch" aria-checked="true" aria-label="Digniinta trade-ka"><i></i></button></div>
    <div class="muopt"><div><b>Heesta xigta si toos ah</b><small>marka heestu dhammaato</small></div><button class="sw on" type="button" id="muOAuto" role="switch" aria-checked="true" aria-label="Heesta xigta si toos ah"><i></i></button></div>
    <div class="muhint">Liiska link-yada server-ka ayaa kaydiya → telefoon kasta oo aad ka gasho isla liiska ayaad helaysaa. MP3-yada telefoonka isla telefoonkaas ayay ku jiraan. Shaashadda oo xidhan: <b>MP3</b> way sii socotaa; YouTube-na browser-ka ayaa joojiya.</div>
  </div>
</div>
<!-- v9: wada-sheekaysi -->
<button class="cfab" id="chatFab" type="button" aria-label="Fariimaha">
  <svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.9A8 8 0 1 1 21 12Z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>
  <span class="n" id="chatBadge" hidden></span>
</button>
<div class="chat" id="chatPanel" hidden role="dialog" aria-modal="true" aria-labelledby="chatT1">
  <div class="ch">
    <button type="button" id="chatBack" aria-label="Dib u noqo"><svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg></button>
    <div class="tt"><div class="t1" id="chatT1">Fariimaha</div><div class="t2" id="chatT2"></div></div>
  </div>
  <div class="clist" id="chatThreads" hidden></div>
  <div class="cmsgs" id="chatMsgs" hidden></div>
  <div class="cerr" id="chatErr" role="alert"></div>
  <div class="cprev" id="chatPrev" hidden><img id="chatPrevImg" alt="Sawirka la dirayo"><button type="button" id="chatPrevX">Ka saar</button></div>
  <div class="ccomp" id="chatComp" hidden>
    <input type="file" id="chatFile" accept="image/*" hidden>
    <button class="ib" type="button" id="chatAttach" aria-label="Ku dar sawir">
      <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m21 16-5-5-7 7"/></svg></button>
    <textarea id="chatText" rows="1" placeholder="Qor fariin…" maxlength="2000" aria-label="Fariin"></textarea>
    <button class="ib send" type="button" id="chatSend" aria-label="Dir">
      <svg viewBox="0 0 24 24"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></svg></button>
  </div>
</div>
<div class="shv" id="shView" hidden role="dialog" aria-modal="true" aria-labelledby="shT1">
  <div class="ch">
    <button type="button" id="shBack" aria-label="Dib u noqo"><svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg></button>
    <div><div class="t1" id="shT1"></div><div class="t2" id="shT2"></div></div>
  </div>
  <div id="shBody"></div>
</div>
<div class="iview" id="imgView" hidden><img id="imgViewImg" alt="Sawir"></div>
<div class="isheet" id="iSheet" hidden>
  <div class="box" role="dialog" aria-modal="true" aria-labelledby="iTitle">
    <h3 id="iTitle">Ku rakib MOHA PRO</h3>
    <p class="lead" id="iLead"></p>
    <ol id="iSteps"></ol>
    <div class="diag" id="iDiag"></div>
    <button class="close" id="iClose" type="button">Xidh</button>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const accSel=$("#accSel");
/* v8.1: account-ka la doortay xasuuso - app-ka marka la furo isla kii ayaa soo baxa */
(function(){
  if(!accSel) return;
  let want=null; try{ want=localStorage.getItem("mohapro_acc"); }catch(e){}
  if(want && [...accSel.options].some(o=>o.value===want)) accSel.value=want;
  accSel.addEventListener("change",function(){ try{ localStorage.setItem("mohapro_acc",accSel.value); }catch(e){} });
})();
let HIST=[];

const money=v=>(v==null||isNaN(v))?"—":Number(v).toLocaleString("en-US",
  {minimumFractionDigits:2,maximumFractionDigits:2});
const cls=v=>v>0?"pos":(v<0?"neg":"neu");

function paint(d){
  const x=d.data||{};
  if(d.account && d.account!==ACC){ ACC=d.account; loadBrandCache(); }   // v4.1
  const RS=runState(d);                                     // v8.3: xaaladda DHABTA ah
  $("#dot").className="dot "+(d.online?(RS.ok?"on":"warn"):"off");
  $("#dot2").className="dot "+(d.online?(RS.ok?"on":"warn"):"off");
  $("#st2").textContent=d.online?RS.short:"OFFLINE";
  const MK=mktState(d);                                     // v12.4: suuqa xidhan
  if(MK.closed){ $("#dot2").className="dot warn"; $("#st2").textContent="SUUQA XIDHAN"; }
  const vv=verOf(d); $("#heroVer").textContent=vv?("v"+vv):"";
  $("#heroAcc").textContent="#"+d.account;
  setBrand(d.brand||"");   // v4.1: madhan -> kii hore ayaa la sii hayaa
  BSKCFG=(d.bsk && d.bsk.cfg)?d.bsk.cfg:null;       // v12.7.1
  ASIACFG=(d.asia && d.asia.cfg)?d.asia.cfg:null;   // v12.9
  TKCFG=(d.tick && d.tick.cfg)?d.tick.cfg:null;     // v12.12
  ASIA=d.asia||null;
  paintSettings(x.settings); paintLocks(x.locks);   // v5
  paintRaw(x, d);                                   // v6
  paintAnalysis(d.analysis);                        // v7
  paintHealth(d);                                   // v7.2
  paintChatBadge(d.chat_unread);                    // v9
  paintSave(d);                                     // v10
  paintMyKey(d);                                    // v11
  paintLic(d);                                      // v12
  paintTick(d.tick);                                // v12.12
  try{ znPaint(d.zn); PRD=d.pairs||null; prSeed(); prStripPaint(); prPaint(); }catch(e){ console.error(e); }
  try{ CBD=(d.tick && d.tick.cb)?d.tick.cb:null; cbInputPaint(); }catch(e){ console.error(e); }   // v12.24: 📏 CABBIR LAMAANE
  try{ GR_BAL=Number((d.data||{}).balance)||0; grPaint(d.grid||null,GR_BAL); }catch(e){ console.error(e); }   // v12.25: 🪜 GRID STOP
  try{ heroPaint(d); }catch(e){ console.error(e); }   // v12.23: ⏻ SHID/DAMI · ✕ XIDH   // v12.22: 🎯 ZONE YAR · 💱 LAMAANAHA
  try{ muOnState(d,x); }catch(e){}                  // v12.20: 🎵 trade xidhmay -> codka hoos u dhig
  INPV=d.inp||null; if(INPS) inpPaint();            // v12.18: ⚙️ INPUT
  stPaint(d);                                       // v12.18: Maamul -> xeeladaha
  paintAsia(d.asia);                                // v12.9
  paintBasket(d.bsk);                               // v12.7
  paintReasons(d);                                  // v12.8
  $("#st").textContent=d.online?(RS.long+" · "+(d.age||0)+"s ka hor")
    :(d.age==null?"Xog lama helin":"OFFLINE · "+d.age+"s ka hor");
  if(MK.closed){
    if(!d.online && d.age!=null) $("#dot").className="dot warn";
    $("#st").insertAdjacentHTML("beforeend",' · <b class="mkt">🌙 suuqa '+(MK.openAt?('wuxuu furmayaa '+esc(soWhen(MK.openAt))):'waa xidhan yahay')+'</b>');
  }
  $("#bal").textContent=money(x.balance);
  $("#eq").textContent=money(x.equity);
  const p=Number(x.profit||0);
  $("#pf").textContent=(p>0?"+":"")+money(p); $("#pf").className="v "+cls(p);
  $("#wr").textContent=(x.winrate==null?"—":Number(x.winrate).toFixed(1)+"%");
  const dd=Number(x.drawdown||0);
  $("#dd").textContent=dd.toFixed(1)+"%"; $("#dd").className="v "+(dd>=8?"neg":(dd>=5?"neu":"neu"));
  $("#ot").textContent=x.opentrades==null?"—":x.opentrades;

  const bits=[];
  if(x.broker)bits.push(x.broker);
  if(x.server)bits.push(x.server);
  if(x.currency)bits.push(x.currency);
  if(x.bot)bits.push(x.bot);
  if(d.pending&&d.pending.length)bits.push("Amar sugaya: "+d.pending.join(", "));
  $("#meta").textContent=bits.join(" · ");

  const rows=(Array.isArray(x.trades)?x.trades:[]).filter(t=>!PRF||String(t.symbol||t.sym||"")===PRF);   // v12.22: 💱 lamaane la doortay
  const isOpen=t=>String(t.st||"OPEN").toUpperCase()==="OPEN";
  const open=rows.filter(isOpen);
  const clsd=rows.filter(t=>!isOpen(t))
                 .sort((a,b)=>(Number(b.ct||b.ot||0))-(Number(a.ct||a.ot||0)));

  $("#cOpen").textContent=open.length?("· "+open.length):"";
  $("#cCls").textContent =clsd.length?("· "+clsd.length):"";

  const tb=$("#tt tbody"); tb.innerHTML="";
  if(!open.length){
    tb.innerHTML='<tr><td colspan="5" class="empty">Trade furan ma jiro.</td></tr>';
  }else{
    for(const t of open){
      const pl=Number(t.profit||0);
      const tr=document.createElement("tr");
      // v8.3: entry · SL · TP (SL-ka faa'iido ku xidhan -> cagaar)
      let sub="";
      if(t.entry!=null){
        const dg=Number(t.dg)>=0&&Number(t.dg)<=8?Number(t.dg):5, f=v=>Number(v)>0?Number(v).toFixed(dg):"—";
        const buy=String(t.type||"").toUpperCase()==="BUY", e=Number(t.entry), sl=Number(t.sl||0);
        const locked=sl>0&&(buy?sl>=e:sl<=e);
        sub='<div class="tsub"><span>E '+f(t.entry)+'</span> · <span class="'+(locked?"pos":"")+'">SL '+f(t.sl)+(locked?" ✓":"")
           +'</span> · <span>TP '+f(t.tp)+'</span></div>';
      }
      tr.innerHTML='<td>'+esc(t.sym||t.symbol||"")+sub+'</td>'+tag(t)+
        '<td>'+esc(t.strat||t.strategy||"")+'</td>'+
        '<td>'+esc(lots(t))+'</td>'+
        '<td style="text-align:right" class="'+cls(pl)+'">'+sign(pl)+'</td>';
      tb.appendChild(tr);
    }
  }

  const tc=$("#tc tbody"); tc.innerHTML="";
  if(!clsd.length){
    tc.innerHTML='<tr><td colspan="7" class="empty">Weli midna lama xidhin.</td></tr>';
    $("#clsSum").textContent="";
  }else{
    let win=0,tot=0,gp=0,gl=0;
    for(const t of clsd.slice(0,50)){
      const pl=Number(t.profit||0);
      const tr=document.createElement("tr");
      tr.innerHTML='<td>'+esc(when(t.ct))+'</td>'+
        '<td>'+esc(t.sym||t.symbol||"")+'</td>'+tag(t)+
        '<td>'+esc(t.strat||t.strategy||"")+'</td>'+
        '<td>'+esc(lots(t))+'</td>'+
        '<td style="text-align:right" class="'+cls(Number(t.points||0))+'">'+
          (t.points==null?"—":(Number(t.points)>0?"+":"")+Number(t.points).toFixed(0))+'</td>'+
        '<td style="text-align:right" class="'+cls(pl)+'">'+sign(pl)+'</td>';
      tc.appendChild(tr);
    }
    for(const t of clsd){
      const pl=Number(t.profit||0); tot++;
      if(pl>0){win++; gp+=pl;} else gl+=Math.abs(pl);
    }
    const wr=tot?(win/tot*100):0;
    const pf=gl>0?(gp/gl):(gp>0?99:0);
    $("#clsSum").textContent=tot+" trade · guul "+win+" ("+wr.toFixed(0)+"%) · "+
      "Profit Factor "+(gl>0?pf.toFixed(2):"—")+" · wadarta "+
      ((gp-gl)>0?"+":"")+money(gp-gl);
  }

  const flo=open.reduce((a,t)=>a+Number(t.profit||0),0);
  $("#fl").textContent=(flo>0?"+":"")+money(flo); $("#fl").className="v "+cls(flo);
  const all=p+flo;
  $("#tot").textContent=(all>0?"+":"")+money(all); $("#tot").className="v "+cls(all);

  HIST=d.history||[];
  drawChart();
}

function lots(t){ return (t.lot!=null)?t.lot:((t.lots!=null)?t.lots:""); }
function sign(v){ return (v>0?"+":"")+money(v); }
function tag(t){
  const ty=String(t.type||t.dir||"").toUpperCase();
  const k=ty.indexOf("BUY")>=0?"buy":(ty.indexOf("SELL")>=0?"sell":"");
  return '<td><span class="tag '+k+'">'+esc(ty||"—")+'</span></td>';
}
function when(ts){
  const n=Number(ts||0); if(!n) return "—";
  const d=new Date(n*1000);
  return String(d.getDate()).padStart(2,"0")+"/"+String(d.getMonth()+1).padStart(2,"0")+
         " "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");
}
function esc(s){const n=document.createElement("span");n.textContent=s==null?"":s;return n.innerHTML;}

/* ---- Equity line chart: hal series, crosshair + tooltip ---- */
function drawChart(){
  const svg=$("#chart"), note=$("#chartNote");
  if(!svg) return;   // v12.18: tab-ka Chart waa la saaray (⚙️ Input)
  const W=svg.clientWidth||600, H=210, P={t:12,r:52,b:22,l:10};
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  svg.innerHTML="";
  if(HIST.length<2){ note.textContent="Xogta taariikhda weli way yar tahay."; return; }
  note.textContent="";
  const xs=HIST.map(p=>p.t), ys=HIST.map(p=>p.e);
  const x0=Math.min(...xs), x1=Math.max(...xs);
  let y0=Math.min(...ys), y1=Math.max(...ys);
  if(y1-y0<1e-9){ y0-=1; y1+=1; }
  const pad=(y1-y0)*0.12; y0-=pad; y1+=pad;
  const px=t=>P.l+(W-P.l-P.r)*((t-x0)/((x1-x0)||1));
  const py=v=>P.t+(H-P.t-P.b)*(1-(v-y0)/((y1-y0)||1));
  const NS="http://www.w3.org/2000/svg";
  const add=(n,a)=>{const e=document.createElementNS(NS,n);
    for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e;};

  for(let i=0;i<=3;i++){
    const v=y0+(y1-y0)*i/3, y=py(v);
    add("line",{class:"grid-l",x1:P.l,x2:W-P.r,y1:y,y2:y});
    const tx=add("text",{x:W-P.r+7,y:y+4});
    tx.textContent=Math.round(v).toLocaleString("en-US");
  }
  const d=HIST.map((p,i)=>(i?"L":"M")+px(p.t).toFixed(1)+" "+py(p.e).toFixed(1)).join(" ");
  add("path",{class:"ar",d:d+" L"+px(x1).toFixed(1)+" "+py(y0)+" L"+px(x0).toFixed(1)+" "+py(y0)+" Z"});
  add("path",{class:"ln",d:d});
  const t0=new Date(x0*1000), t1=new Date(x1*1000);
  const fmt=dt=>String(dt.getHours()).padStart(2,"0")+":"+String(dt.getMinutes()).padStart(2,"0");
  const a=add("text",{x:P.l,y:H-5}); a.textContent=fmt(t0);
  const b=add("text",{x:W-P.r,y:H-5,"text-anchor":"end"}); b.textContent=fmt(t1);

  const ch=add("line",{class:"grid-l",x1:0,x2:0,y1:P.t,y2:H-P.b,opacity:0});
  const mk=add("circle",{r:4.5,fill:"var(--s1)",stroke:"var(--surface)","stroke-width":2,opacity:0});
  const hit=add("rect",{x:0,y:0,width:W,height:H,fill:"transparent"});
  const tip=$("#tip");
  hit.addEventListener("pointermove",ev=>{
    const r=svg.getBoundingClientRect();
    const mx=(ev.clientX-r.left)*(W/r.width);
    let best=0,bd=1e9;
    HIST.forEach((p,i)=>{const dd=Math.abs(px(p.t)-mx); if(dd<bd){bd=dd;best=i;}});
    const p=HIST[best], X=px(p.t), Y=py(p.e);
    ch.setAttribute("x1",X); ch.setAttribute("x2",X); ch.setAttribute("opacity",1);
    mk.setAttribute("cx",X); mk.setAttribute("cy",Y); mk.setAttribute("opacity",1);
    tip.style.opacity=1;
    tip.style.left=Math.min(window.innerWidth-170,ev.clientX+14)+"px";
    tip.style.top=(ev.clientY-46)+"px";
    tip.innerHTML="<b>"+money(p.e)+"</b><br>Balance "+money(p.b)+"<br>"+
      new Date(p.t*1000).toLocaleTimeString();
  });
  hit.addEventListener("pointerleave",()=>{
    ch.setAttribute("opacity",0); mk.setAttribute("opacity",0); tip.style.opacity=0;
  });
}
addEventListener("resize",drawChart);

/* ---- Sawirka hero-ka ---- */
/* v4.1: sawirku MA BAABA'AYO. Kaliya marka aad adigu tirtirto (badhanka X) ayuu ka bexeyaa.
   Server-ku haddii uu soo celiyo madhan (xiriir go'ay / account beddelmay / DB gaabis),
   kii hore ayaa la sii hayaa. Sidoo kale localStorage ayuu ku kaydsan yahay - marka bogga
   la furo isla markiiba wuu soo baxayaa, ka hor inta aan server-ku jawaabin. */
let BRAND="";
function brandKey(){ return "mohapro_brand_"+(ACC||"me"); }
function setBrand(src,force){
  if(!src && !force) return;               // madhan + ma aha tirtirid -> HA SAARIN
  if(src===BRAND) return;
  BRAND=src;
  try{ if(src) localStorage.setItem(brandKey(),src); else localStorage.removeItem(brandKey()); }catch(e){}
  const ph=$("#heroPhoto");            // v4: hal sawir - HERO buuxa
  if(src){
    ph.style.backgroundImage="url('"+src.replace(/'/g,"%27")+"')";
    ph.classList.add("on");
  }else{
    ph.style.backgroundImage=""; ph.classList.remove("on");
  }
}

/* v4.1: sawirkii ugu dambeeyay - isla markiiba soo bandhig */
let ACC="";
function loadBrandCache(){
  try{ const c=localStorage.getItem(brandKey()); if(c) setBrand(c,true); }catch(e){}
}
loadBrandCache();

$("#btnPic").addEventListener("click",()=>{ if(window.__longPressActive && window.__longPressActive()) return; $("#pick").click(); });

$("#pick").addEventListener("change",async ev=>{
  const f=ev.target.files && ev.target.files[0];
  ev.target.value="";
  if(!f) return;
  const btn=$("#btnPic"); const old=btn.innerHTML;   // v12.23: icon-ka ha lumin
  btn.disabled=true; btn.innerHTML="⏳";
  try{
    const data=await shrink(f,1600,0.86);
    const body={img:data};
    if(accSel)body.account=accSel.value;
    const r=await fetch("/api/branding",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){ setBrand(data); }
    else    { alert(d.error||"Sawirka lama keydin."); }
  }catch(e){ alert("Sawirka lama akhriyi karin."); }
  btn.disabled=false; btn.innerHTML=old;
});

/* v4.2: sawirka ka saarid - "Beddel sawirka" si dheer u hay (1 ilbiriqsi) */
async function delBrand(){
  if(!BRAND) return;
  if(!confirm("Sawirka ma ka saaraa?")) return;
  const body={img:""};
  if(accSel)body.account=accSel.value;
  await fetch("/api/branding",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  setBrand("",true);   // tirtirid CAD - halkan oo keliya ayuu sawirku ka bexeyaa
}
(function(){
  const b=$("#btnPic"); let t=null, long=false;
  const start=()=>{ long=false; t=setTimeout(()=>{ long=true; delBrand(); },1000); };
  const stop =()=>{ if(t){ clearTimeout(t); t=null; } };
  b.addEventListener("touchstart",start,{passive:true});
  b.addEventListener("touchend",stop);
  b.addEventListener("touchcancel",stop);
  b.addEventListener("mousedown",start);
  b.addEventListener("mouseup",stop);
  b.addEventListener("mouseleave",stop);
  b.addEventListener("contextmenu",e=>e.preventDefault());
  window.__longPressActive=()=>long;
})();

function shrink(file,maxW,q){
  return new Promise((res,rej)=>{
    const fr=new FileReader();
    fr.onerror=()=>rej();
    fr.onload=()=>{
      const im=new Image();
      im.onerror=()=>rej();
      im.onload=()=>{
        const sc=Math.min(1,maxW/im.width);
        const w=Math.round(im.width*sc), h=Math.round(im.height*sc);
        const cv=document.createElement("canvas");
        cv.width=w; cv.height=h;
        cv.getContext("2d").drawImage(im,0,0,w,h);
        let out=cv.toDataURL("image/jpeg",q);
        let step=q;
        while(out.length>1300000 && step>0.4){ step-=0.12; out=cv.toDataURL("image/jpeg",step); }
        res(out);
      };
      im.src=fr.result;
    };
    fr.readAsDataURL(file);
  });
}

async function tick(){
  try{
    const q=accSel?("?account="+encodeURIComponent(accSel.value)):"";
    const r=await fetch("/api/state"+q,{headers:{"Accept":"application/json"}});
    if(r.status===401){location.href="/login";return;}
    const d=await r.json();
    if(d.ok)paint(d);
  }catch(e){
    $("#dot").className="dot off"; $("#st").textContent="Xiriir la'aan";
    $("#dot2").className="dot off"; $("#st2").textContent="OFFLINE";
  }
}
/* ---- v8: PWA - badhanka "Install" (Android + PC Chrome/Edge) ---- */
let _instEvt=null;
/* v8.2: badhanku MAR WALBA wuu muuqdaa (browser-ka). Chrome-ku haddii uu diyaar u yahay -> daaqadda
   rakibidda ayaa si toos ah u furmaysa. Haddii kale (Chrome ma diyaarin, Samsung, iPhone) -> tilmaamo. */
const IS_APP = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone===true;
function uaKind(){
  const u=navigator.userAgent||"";
  if(/iPhone|iPad|iPod/i.test(u)) return /CriOS|FxiOS|EdgiOS/i.test(u)?"ios-other":"ios";
  if(/SamsungBrowser/i.test(u)) return "samsung";
  if(/Android/i.test(u)) return /Chrome[/]/i.test(u)?"android":"android-other";
  if(/Edg[/]/i.test(u)) return "edge";
  if(/Chrome[/]/i.test(u)) return "desktop";
  return "other";
}
addEventListener("beforeinstallprompt",function(e){ e.preventDefault(); _instEvt=e; });
addEventListener("appinstalled",function(){
  _instEvt=null; const b=$("#btnInst"); if(b) b.style.display="none"; closeSheet();
});
function closeSheet(){ const s=$("#iSheet"); if(s) s.hidden=true; }
async function openSheet(){
  const k=uaKind(), K=t=>'<span class="k">'+t+'</span>';
  let lead="", st=[];
  if(k==="android"){
    lead="Chrome: saddex taabasho. Kadib icon-ka MOHA PRO ayaa home screen-ka ka soo baxaya.";
    st=["Riix "+K("⋮")+" — saddexda dhibcood (URL bar-ka agtiisa).",
        "Dooro <b>Add to Home screen</b> ama <b>Install app</b>.",
        "Riix <b>Install</b> (ama <b>Add</b>).",
        "Chrome-ka xidh. Ka fur icon-ka <b>MOHA PRO</b> ee home screen-ka."];
  }else if(k==="samsung"){
    lead="Samsung Internet:";
    st=["Riix "+K("≡")+" — menu-ga hoose.","<b>Add page to</b> → <b>Home screen</b>.","Riix <b>Add</b>.","Ka fur icon-ka <b>MOHA PRO</b>."];
  }else if(k==="ios"){
    lead="iPhone — Safari keliya ayaa rakibi kara:";
    st=["Riix "+K("⬆")+" <b>Share</b> (hoose, dhexda).","Hoos u dhaadhac → <b>Add to Home Screen</b>.","Riix <b>Add</b> (sare midig).","Ka fur icon-ka <b>MOHA PRO</b>."];
  }else if(k==="ios-other"){
    lead="iPhone-ka browser-kan kama rakibi karo. Fur Safari:";
    st=["Koobi garee link-ga bogga.","<b>Safari</b> ku fur oo login samee.","Riix "+K("⬆")+" <b>Share</b> → <b>Add to Home Screen</b>."];
  }else if(k==="desktop"||k==="edge"){
    lead=(k==="edge"?"Edge":"Chrome")+" — PC:";
    st=["URL bar-ka dhinaca midig, riix icon-ka <b>install</b> "+K("⊕")+".",
        "Ama: "+K("⋮")+" → <b>Cast, save and share</b> → <b>Install page as app</b>.","Riix <b>Install</b>."];
  }else{
    lead="Browser-kan app kama samayn karo. Fur <b>Chrome</b> (Android/PC) ama <b>Safari</b> (iPhone).";
  }
  $("#iLead").innerHTML=lead;
  $("#iSteps").innerHTML=st.map(x=>"<li><span>"+x+"</span></li>").join("");
  // hubin - haddii aanay shaqayn, sawirkan ayaa sheegaya sababta
  let sw=false, ctl=!!(navigator.serviceWorker&&navigator.serviceWorker.controller), man=false;
  try{ sw=!!(navigator.serviceWorker && await navigator.serviceWorker.getRegistration("/")); }catch(e){}
  try{ const r=await fetch("/manifest.webmanifest",{cache:"no-store"}); man=r.ok && !!(await r.json()).name; }catch(e){}
  const Y=(ok)=>ok?'<b class="y">✓</b>':'<b class="n">✗</b>';
  $("#iDiag").innerHTML="Hubin: manifest "+Y(man)+" · service worker "+Y(sw)+" · xakamayn "+Y(ctl)
    +" · https "+Y(location.protocol==="https:"||location.hostname==="127.0.0.1"||location.hostname==="localhost")
    +"<br>browser: "+k+" · Chrome diyaar: "+Y(!!_instEvt);
  $("#iSheet").hidden=false;
}
(function(){
  const b=$("#btnInst"); if(!b) return;
  if(IS_APP){ b.style.display="none"; return; }          // app-ka gudihiisa -> looma baahna
  b.addEventListener("click",async function(){
    if(_instEvt){                                          // Chrome diyaar -> daaqadda rasmiga ah
      _instEvt.prompt();
      try{ const c=await _instEvt.userChoice; if(c && c.outcome==="accepted"){ b.style.display="none"; } }catch(e){}
      _instEvt=null; return;
    }
    openSheet();
  });
  const c=$("#iClose"); if(c) c.addEventListener("click",closeSheet);
  const sh=$("#iSheet"); if(sh) sh.addEventListener("click",function(e){ if(e.target===sh) closeSheet(); });
})();

/* ---- v12.4: SUUQA XIDHAN (weekend) ----
   Forex: Jimce 17:00 New York -> Axad 17:00 New York (xagaa/jiilaal labaduba sax). EA v67.8+ wuxuu soo diraa "mkt"
   (session-ka broker-ka) -> marka bot-ku online yahay kaas ayaa la raacaa. */
const SO_DAY=["Axad","Isniin","Talaado","Arbaco","Khamiis","Jimce","Sabti"];
function nyParts(ms){
  const f=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",hourCycle:"h23",weekday:"short",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
  const o={}; f.formatToParts(new Date(ms)).forEach(p=>{ o[p.type]=p.value; });
  const wd=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].indexOf(o.weekday);
  return {y:+o.year,mo:+o.month,d:+o.day,h:(+o.hour)%24,mi:+o.minute,wd:wd};
}
function nyToUtc(ms,y,mo,d,h,mi){   // waqti New York -> ms (UTC)
  const p=nyParts(ms), off=Date.UTC(p.y,p.mo-1,p.d,p.h,p.mi)-Math.floor(ms/60000)*60000;
  return Date.UTC(y,mo-1,d,h,mi)-off;
}
function mktClock(ms){
  ms=ms||Date.now(); const p=nyParts(ms), mins=p.h*60+p.mi;
  const closed=(p.wd===6)||(p.wd===5&&mins>=17*60)||(p.wd===0&&mins<17*60);
  let openAt=null, closedAt=null;
  if(closed){
    const back=(p.wd===5)?0:(p.wd===6?1:2), fwd=(p.wd===5)?2:(p.wd===6?1:0);
    const base=Date.UTC(p.y,p.mo-1,p.d);
    const f=new Date(base-back*86400000), o=new Date(base+fwd*86400000);
    closedAt=nyToUtc(ms,f.getUTCFullYear(),f.getUTCMonth()+1,f.getUTCDate(),17,0);
    openAt=nyToUtc(ms,o.getUTCFullYear(),o.getUTCMonth()+1,o.getUTCDate(),17,0);
  }
  return {closed:closed,openAt:openAt,closedAt:closedAt};
}
function mktState(d){
  const c=mktClock(), x=(d&&d.data)||{};
  const ea=(d&&d.online&&x.mkt!==undefined&&x.mkt!==null)?Number(x.mkt):null;
  const closed=(ea===null)?c.closed:(ea===0);
  return {closed:closed,openAt:c.openAt,closedAt:c.closedAt,src:(ea===null?"clock":"ea")};
}
function soWhen(ms){ if(!ms) return ""; const t=new Date(ms);
  return SO_DAY[t.getDay()]+" "+String(t.getHours()).padStart(2,"0")+":"+String(t.getMinutes()).padStart(2,"0"); }

/* ---- v7.2: CAAFIMAADKA BOT-KA ----
   Xogta EA-du soo dirto ayaa la baadhaa. Wax kasta oo is-burinaya ama khatar ah -> kaadh.
   red = khalad dhab ah · amb = digniin · ok = wax walba waa sax */
function healthChecks(d){
  const x=d.data||{}, st=x.settings||null, an=d.analysis||[], out=[];
  const add=(lv,t,dd)=>out.push({lv:lv,t:t,d:dd||""});
  // 1) xiriirka
  if(d.age==null){ add("red","Bot-ka xog lagama helin","EA-du weli wax uma dirin server-ka. Hubi EnableCloudDashboard = true iyo URL-ka WebRequest-ka (Tools → Options → Expert Advisors)."); return out; }
  const MK=mktState(d);                                                      // v12.4
  if(MK.closed){
    const hrs=MK.openAt?Math.max(0,Math.round((MK.openAt-Date.now())/3600000)):null;
    add("amb","🌙 Suuqa waa xidhan yahay (weekend)",
      (MK.closedAt?("Forex-ku wuxuu xidhmay "+soWhen(MK.closedAt)+" (waqtigaaga) · "):"")
      +(MK.openAt?("wuxuu furmayaa "+soWhen(MK.openAt)+(hrs!=null?(" ("+hrs+" saac ka dib)"):"")+". "):"")
      +"Bot-ku trade cusub ma furo — tani waa caadi. Trade-yada furan way sugayaan ilaa suuqu furmo. (Waqtiga furitaanka broker-ka ayuu ku xidhan yahay.)");
    if(d.online) add("ok","Bot-ku wuu xidhan yahay","Xitaa weekend-ka xogta wuu soo dirayaa · amarrada wuu qaataa.");
  }
  if(!d.online){
    const m=Math.round(d.age/60);
    if(MK.closed)
      add("amb","Bot-ku xog ma soo dirin ("+(m>=1?(m+" daqiiqo"):(d.age+"s"))+")","Weekend-ka waa caadi haddii MT5 ama PC-ga la xidhay. Haddii MT5 furan yahay, bot-ka v67.8 ku cusboonaysii — noocyadii hore weekend-ka xog ma dirin.");
    else
      add("red","Bot-ku wuu OFFLINE yahay ("+(m>=1?(m+" daqiiqo"):(d.age+"s"))+")","MT5 ama VPS-ka waa la xidhay, internet-ku wuu go'ay, ama WebRequest-ku wuu fashilmay. Experts tab-ka ka eeg: CLOUD DASHBOARD FAILED.");
  }
  if(!st){ add("amb","Sitinka bot-ka lama helin","EA-gu waa nooc duug ah (ka hor v66.3). Ku shub MOHA_PRO v67.0 ama ka dambeeya."); }
  else{
    // 2) maamulka
    if(st.mgmt_off){
      let dd="Trade-ku wuxuu ku xidhmayaa SL ama TP oo keliya. Trailing, partial close, weekend close iyo emergency drawdown midna ma shaqeynayaan.";
      if(st.be) dd+=" Break-even waa ON laakiin MA SHAQEYNAYO maamulka oo damman awgii.";
      const still=[]; if(st.steplock) still.push("Step-Lock"); if(st.adaptive) still.push("ATR adaptive");
      if(still.length) dd+=" ("+still.join(" iyo ")+(still.length>1?" way sii shaqeynayaan.)":" wuu sii shaqeynayaa.)");
      add("red","Maamulka trade-ka waa DAMMAN",dd+" Shid: Maamul → «Maamulka trade-ka» → KAYDI & DIR.");
    }
    // 3) khatarta
    const r=Number(st.risk||0);
    if(r>2) add("red","Khatarta trade kasta waa "+r.toFixed(2)+"%","In ka badan 2% trade kasta wuxuu dhowr khasaare oo isku xiga kaga dhigayaa drawdown weyn.");
    else if(r>1) add("amb","Khatarta trade kasta waa "+r.toFixed(2)+"%","1% ka badan. Hubi inay tahay waxaad ula jeeddo.");
    if(!st.auto_lot && Number(st.lot)>1) add("amb","Lot go'an: "+Number(st.lot).toFixed(2),"Lot-ku ma raacayo haraaga - risk % lama isticmaalayo.");
    // 4) step-lock
    if(st.steplock && Number(st.stepstart)>0 && Number(st.stepstart)<8)
      add("amb","Step-Lock wuxuu bilaabmayaa +"+Number(st.stepstart)+"p","Aad buu u dhow yahay - SL-ku wuxuu u guuri karaa break-even ka hor inta trade-ku neefsan, oo buuq yar ayaa xidhi kara.");
    if(st.steplock && Number(st.lockmode)===0 && Number(st.step)>0 && Number(st.step)<8)
      add("amb","Tallaabada Step-Lock waa "+Number(st.step)+"p","SL-ku aad buu ugu dhow yahay sicirka - trade-yo badan ayaa goor hore ku xidhmi doona.");
    // 5) SL/TP go'an oo aan macquul ahayn
    const sl=Number(st.sl||0), tp=Number(st.tp||0);
    if(sl>0 && tp>0 && tp<sl) add("amb","TP ("+tp+"p) ayaa ka yar SL ("+sl+"p)","RR 1:"+(tp/sl).toFixed(2)+" - win-rate aad u sarreeya ayaad u baahan tahay si aad faa'iido u samayso.");
    if(sl>0 && sl<5) add("amb","SL aad u yar: "+sl+"p","Spread-ka iyo buuqa ayaa xidhi kara ka hor inta aanu trade-ku socon.");
  }
  // 5b) v8.3: xaaladda chart-yada
  const cr=chartRows(d);
  if(d.online && cr.length){
    const by=k=>cr.filter(a=>a.status===k).map(a=>a.sym);
    if(by("EMERGENCY").length) add("red","EMERGENCY STOP — drawdown","Bot-ku wuu joojiyay trade-yada cusub oo kuwa furan ayuu xidhay: "+by("EMERGENCY").join(", ")+". Eeg Max_Total_Drawdown_Pct.");
    if(by("ALGO_OFF").length) add("red","Algo Trading waa DAMMAN","MT5-ka badhanka 'Algo Trading' shid (sare, toolbar-ka). Chart: "+by("ALGO_OFF").join(", ")+".");
    if(by("LICENSE").length) add("red","Laysinka — trade cusub ma furmo","Laysinku wuu dhacay, waa la xannibay, ama bot-ku weli ma hubin (MohaPro_Key + WebRequest URL). Chart: "+by("LICENSE").join(", ")+".");
    const stp=by("STOPPED");
    if(stp.length===cr.length) add("amb","Bot-ka waa LA DAMIYAY","Trade cusub ma furmo. Kuwa furan waa la sii maamulayaa. Shid: Guud → SHID.");
    else if(stp.length) add("amb",stp.length+" chart ayaa la damiyay",stp.join(", ")+" — trade cusub kama furmo. Kuwa kale way shaqeynayaan.");
    const vs=[...new Set(cr.map(a=>a.ver).filter(Boolean))];
    if(vs.length>1) add("amb","Chart-yadu version kala duwan ayay wataan",cr.map(a=>a.sym+" v"+(a.ver||"?")).join(" · ")+". EA-ga cusub chart kasta ku dhaji.");
    const sk=a=>{const c=a.settings||{};return [c.risk,c.mgmt_off,c.sniper,c.sl,c.tp,c.steplock,c.lockmode,c.adaptive].join("|");};
    const sset=[...new Set(cr.filter(a=>a.settings).map(sk))];
    if(sset.length>1){
      const g={}; cr.filter(a=>a.settings).forEach(a=>{const c=a.settings;const k="risk "+c.risk+"% · maamul "+(c.mgmt_off?"OFF":"ON")+(c.sniper?" · sniper":"");(g[k]=g[k]||[]).push(a.sym);});
      add("amb","Chart-yadu sitin kala duwan ayay leeyihiin",Object.keys(g).map(k=>k+": "+g[k].join(", ")).join(" | ")+". Isla .set-ka chart kasta ku shub, ama app-ka sitinka mar kale dir.");
    }
  }
  // 6) drawdown
  const dd=Number(x.drawdown||0);
  if(dd>=10) add("red","Drawdown: "+dd.toFixed(1)+"%","Haraaga ayaa si weyn hoos ugu dhacay. Eeg trade-yada la xidhay ka hor inta aanad sii wadin.");
  else if(dd>=5) add("amb","Drawdown: "+dd.toFixed(1)+"%","Si dhow ula soco.");
  // 7) analiiska chart-yada
  if(d.online && !an.length) add("amb","Analiis lama helin","Bot-ku wuu online yahay laakiin analiis ma soo dirin. EA v66.9+ ku shub oo Enable_SD_Engine = true ka dhig.");
  an.forEach(a=>{
    if(d.online && Number(a._age)>300) add("amb",(a.sym||"Chart")+" — "+Math.round(a._age/60)+" daqiiqo ma dirin","Chart-kan waa la xidhay ama EA-gii waa laga saaray. Kuwa kale way socdaan.");
  });
  const nz=an.filter(a=>a.st==="NONE").length;
  if(an.length>=3 && nz===an.length) add("amb","Chart kasta: zone ma jiro","Filtarrada zone-ka ayaa aad u adag suuqan hadda, ama SD_Zone_TF waa khaldan yahay.");
  if(!out.length) add("ok","Wax walba waa sax","Bot-ku wuu online yahay, maamulku wuu shaqeynayaa, sitinkuna is ma burinayaan.");
  return out;
}
function paintHealth(d){
  const box=$("#hcList"); if(!box) return;
  const rows=healthChecks(d);
  const ord={red:0,amb:1,ok:2}; rows.sort((a,b)=>ord[a.lv]-ord[b.lv]);
  box.innerHTML=rows.map(r=>'<div class="hc '+r.lv+'"><i></i><div><div class="ht">'+esc(r.t)
    +'</div>'+(r.d?('<div class="hd">'+esc(r.d)+'</div>'):"")+'</div></div>').join("");
  const nr=rows.filter(r=>r.lv==="red").length, na=rows.filter(r=>r.lv==="amb").length;
  $("#hcSum").textContent=(nr||na)?((nr?nr+" khalad":"")+(nr&&na?" · ":"")+(na?na+" digniin":"")):"sax";
  const b=$("#hcBadge");
  if(b){ if(nr){ b.textContent=nr; b.style.display=""; } else b.style.display="none"; }
}

/* ---- v9.1: SAWIRRADA TRADE-YADA ---- */
const SH={rows:[],f:"all",t:0,loaded:false};
function shNum(v,dg){ const n=Number(v); return (!n)?"—":n.toFixed(dg==null?5:dg); }
function shWhen(ts){ if(!ts) return ""; const d=new Date(ts*1000), t=new Date(), y=new Date(); y.setDate(t.getDate()-1);
  const hm=d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  if(d.toDateString()===t.toDateString()) return "Maanta "+hm;
  if(d.toDateString()===y.toDateString()) return "Shalay "+hm;
  return d.toLocaleDateString()+" "+hm; }
function shPips(t){ const dg=Number(t.dg)||5, pip=(dg===3||dg===5)?Math.pow(10,-(dg-1)):Math.pow(10,-dg);
  const e=Number(t.entry), c=Number(t.closep); if(!e||!c) return null;
  const d=(String(t.type)==="BUY")?(c-e):(e-c); return d/pip; }
function shReason(t){ const c=Number(t.closep), tp=Number(t.tp), sl=Number(t.sl), e=Number(t.entry); if(!c) return "";
  const dg=Number(t.dg)||5, tol=3*((dg===3||dg===5)?Math.pow(10,-(dg-1)):Math.pow(10,-dg));
  if(tp && Math.abs(c-tp)<=tol) return "TP"; if(sl && Math.abs(c-sl)<=tol) return (Math.abs(sl-e)<=tol?"BE":"SL"); return "gacan/kale"; }
function shState(t){ return t.close ? (Number(t.profit)>=0?"win":"loss") : "open"; }
function shThumb(t){
  const s=t.close&&t.close.img?t.close:(t.open&&t.open.img?t.open:null);
  const n=(t.open&&t.open.img?1:0)+(t.close&&t.close.img?1:0);
  return '<span class="shth">'+(s?('<img loading="lazy" src="/api/shots/img/'+s.id+'" alt="">'):"sawir<br>lama helin")
    +(n?('<span class="c">'+n+'</span>'):"")+'</span>';
}
function paintShots(){
  const box=$("#shList"); if(!box) return;
  const rows=SH.rows.filter(t=>SH.f==="all"||shState(t)===SH.f);
  $("#cShots").textContent=SH.rows.length?("· "+SH.rows.length):"";
  if(!SH.rows.length){ box.innerHTML='<div class="empty" style="padding:18px 0">Weli sawir lama helin.<br>Bot-ka v67.2 ayaa soo dira marka trade la furo ama la xidho.</div>'; $("#shNote").textContent=""; return; }
  if(!rows.length){ box.innerHTML='<div class="empty" style="padding:18px 0">Midna kuma jiro shaandhadan.</div>'; return; }
  box.innerHTML=rows.map((t,i)=>{
    const st=shState(t), pp=shPips(t), dg=Number(t.dg)||5;
    const sub=t.close?(shWhen(t.ot)+" → "+shWhen(t.ct).replace(/^(Maanta|Shalay) /,"")+" · "+shReason(t)):(shWhen(t.ot)+" · "+Number(t.lot||0).toFixed(2)+" lot");
    const pl=st==="open"?'<span class="op">FURAN</span>'
      :('<span class="'+(st==="win"?"pos":"neg")+'">'+(Number(t.profit)>=0?"+":"−")+"$"+Math.abs(Number(t.profit)).toFixed(2)+'</span>'
        +(pp!=null?('<div class="p">'+(pp>=0?"+":"−")+Math.abs(pp).toFixed(0)+' pip</div>'):""));
    return '<button type="button" class="shr" data-i="'+SH.rows.indexOf(t)+'">'+shThumb(t)
      +'<span class="mid"><div class="sy">'+esc(t.sym||"")+'<span class="ttag '+(t.type==="BUY"?"buy":"sell")+'">'+esc(t.type||"")+'</span></div>'
      +'<div class="sub">'+esc(sub)+'</div></span><span class="pl">'+pl+'</span></button>';
  }).join("");
  box.querySelectorAll(".shr").forEach(b=>b.addEventListener("click",()=>shOpen(SH.rows[Number(b.dataset.i)])));
  $("#shNote").textContent="Sawirrada "+(SH.days||30)+" maalmood kadib way tirtirmaan.";
}
async function loadShots(){
  try{
    const q=accSel?("?account="+encodeURIComponent(accSel.value)):"";
    const r=await fetch("/api/shots"+q,{headers:{"Accept":"application/json"}}); if(r.status===401){ location.href="/login"; return; }
    const d=await r.json(); if(!d.ok) return;
    SH.rows=d.trades||[]; SH.days=d.days; SH.loaded=true; paintShots();
  }catch(e){}
}
document.querySelectorAll("#shFlt button").forEach(b=>b.addEventListener("click",()=>{
  SH.f=b.dataset.f; document.querySelectorAll("#shFlt button").forEach(x=>x.classList.toggle("on",x===b)); paintShots(); }));
function shImg(s,label,when){
  return '<div class="lbl">'+label+'<span>'+esc(when)+'</span></div>'
    +(s&&s.img?('<img class="big" src="/api/shots/img/'+s.id+'" alt="'+esc(label)+'">')
      :('<div class="none">'+(s?"Sawir lama helin — MQL5 VPS chart-yada ma sawiro":"Weli lama xidhin")+'</div>'));
}
function shOpen(t){
  if(!t) return;
  const dg=Number(t.dg)||5, st=shState(t), pp=shPips(t), rs=shReason(t);
  $("#shT1").textContent=(t.sym||"")+" · "+(t.type||"");
  $("#shT2").textContent=(t.pos&&t.pos!=="0"?("#"+t.pos+" · "):"")+Number(t.lot||0).toFixed(2)+" lot";
  const e=Number(t.entry), sl=Number(t.sl), tp=Number(t.tp);
  const rr=(e&&sl&&tp&&Math.abs(e-sl)>0)?("1 : "+(Math.abs(tp-e)/Math.abs(e-sl)).toFixed(1)):"—";
  let dur=""; if(t.ct&&t.ot){ const m=Math.round((t.ct-t.ot)/60); dur=m>=60?(Math.floor(m/60)+" saac "+(m%60)+" daq"):(m+" daq"); }
  const res=st==="open"?'<div class="res o">Weli waa furan yahay</div>'
    :('<div class="res '+(st==="win"?"w":"l")+'">'+(st==="win"?"✓ Faa'iido ":"✗ Khasaare ")
      +(Number(t.profit)>=0?"+":"−")+"$"+Math.abs(Number(t.profit)).toFixed(2)
      +(pp!=null?(" · "+(pp>=0?"+":"−")+Math.abs(pp).toFixed(0)+" pip"):"")+(dur?(" · "+dur):"")+(rs?(" · "+rs):"")+'</div>');
  $("#shBody").innerHTML=shImg(t.open,"MARKA LA FURAY",shWhen(t.ot))
    +shImg(t.close,"MARKA LA XIDHAY",t.close?(shWhen(t.ct)+(rs?(" · "+rs):"")):"")
    +'<div class="stats"><div><b>'+shNum(t.entry,dg)+'</b><i>Entry</i></div><div><b>'+shNum(t.closep,dg)+'</b><i>Xidhan</i></div><div><b>'+Number(t.lot||0).toFixed(2)+'</b><i>Lot</i></div>'
    +'<div><b class="neg">'+shNum(t.sl,dg)+'</b><i>SL</i></div><div><b class="pos">'+shNum(t.tp,dg)+'</b><i>TP</i></div><div><b>'+rr+'</b><i>RR</i></div></div>'+res;
  $("#shView").hidden=false; $("#shView").scrollTop=0; document.body.style.overflow="hidden";
  try{ history.pushState({mohaShot:1},""); }catch(e){}
}
function shClose(fromPop){
  if($("#shView").hidden) return;
  $("#shView").hidden=true; if(!CH.open) document.body.style.overflow="";
  if(!fromPop){ chSkipPop=true; try{ history.back(); }catch(e){ chSkipPop=false; } }
}
$("#shBack").addEventListener("click",()=>shClose(false));
$("#shBody").addEventListener("click",e=>{
  const im=e.target.closest(".big"); if(!im) return;
  $("#imgViewImg").src=im.src; $("#imgView").hidden=false;
  try{ history.pushState({mohaShot:1,img:1},""); }catch(err){}
});

/* ---- v9: WADA-SHEEKAYSI (isticmaale <-> admin) ---- */
const IS_ADMIN = {{ 'true' if is_admin else 'false' }};
const CH={open:false,thread:null,last:0,seen:0,timer:null,img:null,busy:false,lastDay:""};
function chFmtTime(ts){ const d=new Date(ts*1000); return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}); }
function chDay(ts){
  const d=new Date(ts*1000), t=new Date(); const y=new Date(); y.setDate(t.getDate()-1);
  if(d.toDateString()===t.toDateString()) return "Maanta";
  if(d.toDateString()===y.toDateString()) return "Shalay";
  return d.toLocaleDateString();
}
function chMsgHTML(m){
  const mine = IS_ADMIN ? m.from_admin : !m.from_admin;
  let h="";
  const day=chDay(m.ts); if(day!==CH.lastDay){ CH.lastDay=day; h+='<div class="cday">'+esc(day)+'</div>'; }
  let inner="";
  if(m.img_id>0) inner+='<img class="cimg" loading="lazy" src="/api/chat/img/'+m.img_id+'" alt="Sawir">';
  else if(m.img_id===-1) inner+='<div class="cgone">Sawirka waa la tirtiray (30 maalmood kadib)</div>';
  if(m.body) inner+='<div class="ctext">'+esc(m.body)+'</div>';
  inner+='<div class="ctm">'+chFmtTime(m.ts)+(mine?'<span class="ck" data-id="'+m.id+'">✓</span>':'')+'</div>';
  return h+'<div class="cm '+(mine?'me':'them')+'"><div class="cb">'+inner+'</div></div>';
}
function chTicks(){
  document.querySelectorAll("#chatMsgs .ck").forEach(e=>{
    const rd=Number(e.dataset.id)<=CH.seen; e.textContent=rd?"✓✓":"✓"; e.classList.toggle("rd",rd);
  });
}
function chErr(t){ $("#chatErr").textContent=t||""; }
function chScroll(){ const b=$("#chatMsgs"); b.scrollTop=b.scrollHeight; }
async function chPoll(){
  if(!CH.open || !CH.thread || document.hidden) return;
  try{
    const r=await fetch("/api/chat?thread="+encodeURIComponent(CH.thread)+"&after="+CH.last,{headers:{"Accept":"application/json"}});
    if(r.status===401){ location.href="/login"; return; }
    const d=await r.json(); if(!d.ok) return;
    const box=$("#chatMsgs"), first=(CH.last===0);
    if(first){
      box.innerHTML=d.messages.length?"":'<div class="cempty">'+(IS_ADMIN
        ?"Weli fariin lama isweydaarsan. Qor fariin ama dir sawir."
        :"Su'aal ma qabtaa? Halkan qor, ama soo dir sawir (tusaale: screenshot-ka MT5). Admin-ka ayaa kuu jawaabaya.")+'</div>';
    }
    if(d.messages.length){
      const e=box.querySelector(".cempty"); if(e) e.remove();
      const near=(box.scrollHeight-box.scrollTop-box.clientHeight)<120;
      box.insertAdjacentHTML("beforeend",d.messages.map(chMsgHTML).join(""));
      CH.last=d.messages[d.messages.length-1].id;
      if(near||first) chScroll();
    }
    CH.seen=d.seen_upto||0; chTicks();
    if(IS_ADMIN && d.peer){ $("#chatT1").textContent=d.peer; }
  }catch(e){}
}
async function chThreads(){
  if(!IS_ADMIN || !CH.open || CH.thread) return;
  try{
    const r=await fetch("/api/chat/threads"); const d=await r.json(); if(!d.ok) return;
    const box=$("#chatThreads");
    if(!d.threads.length){ box.innerHTML='<div class="cempty">Weli isticmaale la ansixiyay ma jiro.</div>'; return; }
    box.innerHTML=d.threads.map(t=>{
      const l=t.last, prev=l?((l.from_admin?"Adiga: ":"")+(l.img&&!l.body?"📷 Sawir":(l.body||""))):"Weli fariin ma jirto";
      return '<button class="cth" type="button" data-t="'+esc(t.thread)+'" data-n="'+esc(t.name)+'">'
        +'<span class="av">'+esc((t.name||"?").trim().charAt(0).toUpperCase())+'</span>'
        +'<span class="mid"><div class="nm">'+esc(t.name)+' <span style="color:var(--ink3);font-weight:400">#'+esc(t.thread)+'</span></div>'
        +'<div class="lm">'+esc(prev)+'</div></span>'
        +'<span class="rt">'+(l?esc(chDay(l.ts)==="Maanta"?chFmtTime(l.ts):chDay(l.ts)):"")
        +(t.unread?('<span class="un">'+t.unread+'</span>'):"")+'</span></button>';
    }).join("");
    box.querySelectorAll(".cth").forEach(b=>b.addEventListener("click",()=>chOpenThread(b.dataset.t,b.dataset.n)));
  }catch(e){}
}
function chShowList(){
  CH.thread=null; CH.last=0; CH.lastDay="";
  $("#chatT1").textContent="Fariimaha"; $("#chatT2").textContent="Isticmaalayaasha";
  $("#chatThreads").hidden=false; $("#chatMsgs").hidden=true; $("#chatComp").hidden=true; $("#chatPrev").hidden=true; chErr("");
  $("#chatThreads").innerHTML='<div class="cempty">Soo raraya…</div>';
  chThreads();
}
function chOpenThread(t,name){
  CH.thread=t; CH.last=0; CH.seen=0; CH.lastDay="";
  $("#chatT1").textContent=IS_ADMIN?(name||t):"MOHA PRO · Taageero";
  $("#chatT2").textContent=IS_ADMIN?("#"+t):"Admin-ka ayaa kuu jawaabaya";
  $("#chatThreads").hidden=true; $("#chatMsgs").hidden=false; $("#chatComp").hidden=false;
  $("#chatMsgs").innerHTML='<div class="cempty">Soo raraya…</div>'; chErr("");
  chPoll();
}
function openChat(){
  if(CH.open) return;
  CH.open=true; $("#chatPanel").hidden=false; document.body.style.overflow="hidden";
  try{ history.pushState({mohaChat:1},""); }catch(e){}
  if(IS_ADMIN) chShowList(); else chOpenThread("me","");
  clearInterval(CH.timer); CH.timer=setInterval(()=>{ if(CH.thread) chPoll(); else chThreads(); },4000);
}
function closeChat(fromPop){
  if(!CH.open) return;
  CH.open=false; clearInterval(CH.timer); $("#chatPanel").hidden=true; document.body.style.overflow="";
  chClearImg(); CH.thread=null;
  if(!fromPop){ try{ if(history.state&&history.state.mohaChat) history.back(); }catch(e){} }
  tick();
}
let chSkipPop=false;
addEventListener("popstate",()=>{
  if(chSkipPop){ chSkipPop=false; return; }
  if(!$("#imgView").hidden){ $("#imgView").hidden=true; return; }
  if(!$("#shView").hidden){ shClose(true); return; }   // v9.1
  if(CH.open){
    if(IS_ADMIN && CH.thread){ chShowList(); try{ history.pushState({mohaChat:1},""); }catch(e){} }
    else closeChat(true);
  }
});
$("#chatFab").addEventListener("click",()=>{ if(FAB.justDragged) return; openChat(); });   // v9.2: jiidis kadib ha furin

/* ---- v9.2: badhanka fariimaha — farta ku hay oo jiid; dhinaca ugu dhow ayuu ku dhegaa; wuu xasuustaa ---- */
const FAB={justDragged:false};
(function(){
  const f=$("#chatFab"); if(!f) return;
  const KEY="mohapro_fab", M=12;
  const bounds=()=>{
    const s=f.offsetWidth||56, bar=((document.querySelector(".appbar")||{offsetHeight:0}).offsetHeight||0)+(document.body.classList.contains("mu-on")?70:0);   // v12.20
    return {minX:M, maxX:Math.max(M,innerWidth-s-M), minY:M, maxY:Math.max(M,innerHeight-bar-s-M)};
  };
  const place=(x,y)=>{
    const b=bounds(); x=Math.min(b.maxX,Math.max(b.minX,x)); y=Math.min(b.maxY,Math.max(b.minY,y));
    f.style.left=x+"px"; f.style.top=y+"px"; f.style.right="auto"; f.style.bottom="auto"; return {x:x,y:y,b:b};
  };
  const load=()=>{ try{ return JSON.parse(localStorage.getItem(KEY)||"null"); }catch(e){ return null; } };
  const apply=()=>{                                   // meeshii la kaydiyay (dhinac + boqolley dherer) -> shaashad kasta
    const v=load(); if(!v || (v.side!=="L" && v.side!=="R")) return;
    const b=bounds(), yf=Math.min(1,Math.max(0,Number(v.yf)||0));
    place(v.side==="L"?b.minX:b.maxX, b.minY+yf*(b.maxY-b.minY));
  };
  apply();
  addEventListener("resize",apply);
  let st=null, moved=false;
  f.addEventListener("pointerdown",e=>{
    if(e.button!==undefined && e.button>0) return;
    const r=f.getBoundingClientRect();
    st={px:e.clientX,py:e.clientY,x:r.left,y:r.top,id:e.pointerId}; moved=false;
    try{ f.setPointerCapture(e.pointerId); }catch(err){}   // mouse: dhaqdhaqaaqa ha raaco xitaa marka uu badhanka ka baxo
  });
  f.addEventListener("pointermove",e=>{
    if(!st || e.pointerId!==st.id) return;
    const dx=e.clientX-st.px, dy=e.clientY-st.py;
    if(!moved){
      if(Math.hypot(dx,dy)<8) return;                 // taabasho yar = riix (furi), maaha jiid
      moved=true; f.classList.remove("snap"); f.classList.add("drag");
    }
    place(st.x+dx, st.y+dy); e.preventDefault();
  });
  const end=e=>{
    if(!st) return;
    if(moved){
      const r=f.getBoundingClientRect(), b=bounds(), mid=(b.minX+b.maxX)/2;
      const side=(r.left+ (f.offsetWidth||56)/2 < mid+ (f.offsetWidth||56)/2)?"L":"R";
      f.classList.remove("drag"); f.classList.add("snap");
      const p=place(side==="L"?b.minX:b.maxX, r.top);
      const yf=(p.b.maxY>p.b.minY)?(p.y-p.b.minY)/(p.b.maxY-p.b.minY):1;
      try{ localStorage.setItem(KEY,JSON.stringify({side:side,yf:Math.round(yf*1000)/1000})); }catch(err){}
      FAB.justDragged=true; setTimeout(()=>{ FAB.justDragged=false; },400);
      setTimeout(()=>f.classList.remove("snap"),260);
    }
    try{ if(st && f.hasPointerCapture && f.hasPointerCapture(st.id)) f.releasePointerCapture(st.id); }catch(err){}
    st=null;
  };
  f.addEventListener("pointerup",end); f.addEventListener("pointercancel",end);
  f.addEventListener("contextmenu",e=>e.preventDefault());   // farta oo la hayo -> menu-ga browser-ka ha soo bixin
})();
$("#chatBack").addEventListener("click",()=>{ if(IS_ADMIN && CH.thread) chShowList(); else closeChat(false); });
function chClearImg(){ CH.img=null; $("#chatPrev").hidden=true; $("#chatPrevImg").removeAttribute("src"); $("#chatFile").value=""; }
function chCompress(file){
  return new Promise((res,rej)=>{
    const url=URL.createObjectURL(file), im=new Image();
    im.onload=()=>{
      const M=1280, s=Math.min(1,M/Math.max(im.naturalWidth,im.naturalHeight));
      const cv=document.createElement("canvas"); cv.width=Math.max(1,Math.round(im.naturalWidth*s)); cv.height=Math.max(1,Math.round(im.naturalHeight*s));
      const g=cv.getContext("2d"); g.fillStyle="#fff"; g.fillRect(0,0,cv.width,cv.height); g.drawImage(im,0,0,cv.width,cv.height);
      URL.revokeObjectURL(url);
      let q=0.82, out=cv.toDataURL("image/jpeg",q);
      while(out.length>880000 && q>0.35){ q-=0.12; out=cv.toDataURL("image/jpeg",q); }
      out.length>900000 ? rej(new Error("big")) : res(out);
    };
    im.onerror=()=>{ URL.revokeObjectURL(url); rej(new Error("bad")); };
    im.src=url;
  });
}
$("#chatAttach").addEventListener("click",()=>$("#chatFile").click());
$("#chatFile").addEventListener("change",async e=>{
  const f=e.target.files&&e.target.files[0]; if(!f) return;
  chErr("");
  try{ CH.img=await chCompress(f); $("#chatPrevImg").src=CH.img; $("#chatPrev").hidden=false; }
  catch(err){ chClearImg(); chErr("Sawirkan lama furi karo. Isku day JPEG ama PNG."); }
});
$("#chatPrevX").addEventListener("click",chClearImg);
const chTa=$("#chatText");
chTa.addEventListener("input",()=>{ chTa.style.height="auto"; chTa.style.height=Math.min(120,chTa.scrollHeight)+"px"; });
chTa.addEventListener("keydown",e=>{ if(e.key==="Enter" && !e.shiftKey && matchMedia("(pointer:fine)").matches){ e.preventDefault(); chSend(); } });
async function chSend(){
  if(CH.busy || !CH.thread) return;
  const text=chTa.value.trim(); if(!text && !CH.img) return;
  CH.busy=true; $("#chatSend").disabled=true; chErr("");
  try{
    const body={text:text,img:CH.img||""}; if(IS_ADMIN) body.thread=CH.thread;
    const r=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json().catch(()=>({ok:false,error:"Server-ka jawaab lama helin."}));
    if(!d.ok){ chErr(d.error||"Lama dirin."); return; }
    chTa.value=""; chTa.style.height="auto"; chClearImg();
    await chPoll(); chScroll();
  }catch(e){ chErr("Internet ma jiro — lama dirin."); }
  finally{ CH.busy=false; $("#chatSend").disabled=false; }
}
$("#chatSend").addEventListener("click",chSend);
$("#chatMsgs").addEventListener("click",e=>{
  const im=e.target.closest(".cimg"); if(!im) return;
  $("#imgViewImg").src=im.src; $("#imgView").hidden=false;
  try{ history.pushState({mohaChat:1,img:1},""); }catch(err){}
});
$("#imgView").addEventListener("click",()=>{ $("#imgView").hidden=true; chSkipPop=true; try{ history.back(); }catch(e){ chSkipPop=false; } });
let CHN_LAST=null, CHN_T=null;                         // v12.21: fariin cusub -> calaamad nav + xariiq
function paintChatBadge(n){
  const b=$("#chatBadge"); n=Number(n||0);
  if(n>0){ b.textContent=n>99?"99+":n; b.hidden=false; } else b.hidden=true;
  const nb=$("#navChatBd"); if(nb){ if(n>0){ nb.textContent=n>99?"99+":n; nb.hidden=false; } else nb.hidden=true; }
  if(CHN_LAST!==null && n>CHN_LAST && !(typeof CH!=="undefined" && CH.open)) chToast(n);
  if(n===0) chToastHide();
  CHN_LAST=n;
}
function chToastPos(){
  const t=$("#chToast"); if(!t) return;
  const ab=document.querySelector(".appbar"), mb=document.getElementById("muBar");
  let h=(ab?ab.offsetHeight:60)+8; if(mb && !mb.hidden) h+=mb.offsetHeight+6;
  t.style.bottom=h+"px";
}
function chToast(n){
  const t=$("#chToast"); if(!t) return;
  $("#chToastN").textContent=n+" aan la akhrin"; t.hidden=false; chToastPos();
  clearTimeout(CHN_T); CHN_T=setTimeout(chToastHide,8000);
}
function chToastHide(){ const t=$("#chToast"); if(t) t.hidden=true; clearTimeout(CHN_T); }

/* ---- v8.3: xaaladda bot-ka (chart kasta) ---- */
const ST_TXT={RUNNING:"SHAQEYNAYA",STOPPED:"LA DAMIYAY",EMERGENCY:"EMERGENCY STOP",ALGO_OFF:"ALGO TRADING DAMMAN",LICENSE:"LAYSIN"};
function chartRows(d){ return (d.analysis||[]).filter(a=>a && a.status && Number(a._age)<=300); }
function runState(d){
  const x=d.data||{}, rows=chartRows(d);
  let sts=rows.map(a=>String(a.status));
  if(!sts.length) sts=[String(x.status||"RUNNING")];
  const n=sts.length, c=k=>sts.filter(s=>s===k).length;
  if(c("EMERGENCY")) return {ok:false,short:"EMERGENCY",long:"EMERGENCY STOP (drawdown)"};
  if(c("ALGO_OFF"))  return {ok:false,short:"ALGO OFF",long:"ALGO TRADING DAMMAN ("+c("ALGO_OFF")+"/"+n+" chart)"};
  if(c("LICENSE"))   return {ok:false,short:"LAYSIN",long:"LAYSIN — trade cusub ma furmo ("+c("LICENSE")+"/"+n+" chart)"};
  if(c("STOPPED")===n) return {ok:false,short:"LA DAMIYAY",long:"LA DAMIYAY — trade cusub ma furmo"};
  if(c("STOPPED")) return {ok:false,short:"QAYB DAMMAN",long:(n-c("STOPPED"))+"/"+n+" chart ayaa shaqeynaya"};
  return {ok:true,short:"ONLINE",long:"SHAQEYNAYA"+(n>1?(" · "+n+" chart"):"")};
}
function verOf(d){
  const vs=chartRows(d).map(a=>a.ver).filter(Boolean);
  return vs.length?vs.sort().slice(-1)[0]:((d.data||{}).ver||"");
}
const FR_NM=["News","Session","Regime/ADX","MTF","EMA200","Correlation","Hal trade/H1","Spread/trade furan","ATR yar","RR yar","SL/TP"];

/* ---- v12.8 (EA v69.6): SABABTA TRADE LA'AANTA ---- */
const RC_B={ALGO:["ALGO OFF","r"],KEY:["FURAHA","r"],STOP:["DAMMAN","r"],LIC:["LAYSIN","r"],EMER:["EMERGENCY","r"],PAIR:["✕","r"],
  NEWS:["NEWS","a"],TIME:["WAQTI","c"],MAX:["XAD","a"],ZONE:["ZONE ✕","c"],WAIT:["SUG","a"],SIG:["SIGNAL","g"],BSK:["BASKET","a"],ASIA:["ASIA","a"],TICK:["TICK","a"],GRID:["🪜 GRID","a"]};
const RC_P={ALGO:0,KEY:1,EMER:2,LIC:3,STOP:4,PAIR:5,SIG:6,BSK:7,ASIA:7,TICK:7,WAIT:8,MAX:9,NEWS:10,ZONE:11,TIME:12};
function rcOf(a){
  if(a.rc && RC_B[a.rc]) return a.rc;   // EA v69.6+
  const s=String(a.status||"");
  if(s==="ALGO_OFF") return "ALGO"; if(s==="LICENSE") return "LIC"; if(s==="STOPPED") return "STOP"; if(s==="EMERGENCY") return "EMER";
  if(a.st==="NONE") return "ZONE"; if(a.st==="SIGNAL") return "SIG"; if(a.st==="NEWS") return "NEWS";
  return "WAIT";
}
function paintReasons(d){
  const c=$("#rsnCard"); if(!c) return;
  const rows=chartRows(d).slice();
  if(!rows.length){ c.hidden=true; return; }
  c.hidden=false;
  rows.sort((a,b)=>(RC_P[rcOf(a)]-RC_P[rcOf(b)])||String(a.sym).localeCompare(String(b.sym)));
  let sig=0, zn=0, nAlgo=0; const keys=[], lics=[];
  rows.forEach(a=>{ sig+=Number(a.sig)||0; zn+=Number(a.zn!==undefined?a.zn:a.zones)||0; const k=rcOf(a);
    if(k==="ALGO") nAlgo++; if(k==="KEY") keys.push(a.sym); if(k==="LIC") lics.push(a.sym); });
  $("#rsnSig").textContent=sig; $("#rsnZn").textContent=zn; $("#rsnN").textContent=rows.length;
  const al=$("#rsnAlert"), at=$("#rsnAT"), ap=$("#rsnAP");
  if(nAlgo){ al.hidden=false; at.textContent="⚠ ALGO TRADING WAA DAMMAN (MT5) · "+nAlgo+"/"+rows.length+" chart";
    ap.innerHTML="Bot-yadu trade ma furi karaan. MT5 → badhanka <b>Algo Trading</b> shid · Tools → Options → Expert Advisors → ka saar <b>“Disable algorithmic trading when the chart symbol or period has been changed”</b>"; }
  else if(keys.length){ al.hidden=false; at.textContent="⚠ FURAHA WAA LA DIIDAY · "+keys.join(", ");
    ap.textContent="Chart-kaas EA-ga ka saar oo dib ugu dar (furaha saxda ah). Trade cusub ma furmayo ilaa furaha la saxo."; }
  else if(lics.length){ al.hidden=false; at.textContent="⚠ LAYSIN · "+lics.join(", "); ap.textContent="Trade cusub ma furmayo — admin-ka la xiriir."; }
  else al.hidden=true;
  $("#rsnRows").innerHTML=rows.map(a=>{
    const k=rcOf(a), B=RC_B[k]||["—","c"];
    let t=a.rt||a.why||(AN_LBL[a.st]||"—"), sub="";
    if(k==="WAIT" && Number(a.dist)>0 && !a.rt) sub="ugu dhow: "+Number(a.dist).toFixed(1)+"p";
    if(a.ver) sub=(sub?sub+" · ":"")+"v"+a.ver;
    return '<div class="rsnr"><span class="sy">'+esc(a.sym||"—")+'</span><span class="wy">'+esc(t)+(sub?'<small>'+esc(sub)+'</small>':'')
      +'</span><span class="rb '+B[1]+'">'+esc(B[0])+'</span></div>';
  }).join("");
}

/* ---- v7: ANALIISKA LIVE ---- */
const AN_LBL={SIGNAL:"SIGNAL DIYAAR",IN:"ZONE GUDIHIISA",NEAR:"U DHOW",
              WAIT:"SUGAYA",NONE:"ZONE MA JIRTO",NEWS:"NEWS — JOOJIN"};
const AN_CLS={SIGNAL:"signal",IN:"in",NEAR:"near",WAIT:"",NONE:"none",NEWS:"news"};
function anNum(v,dg){ const n=Number(v); return (v==null||isNaN(n))?"—":n.toFixed(dg==null?5:dg); }
function anMins(m){
  m=Number(m);
  if(isNaN(m)) return "—";
  if(m<0) return Math.abs(m)+" daq ka hor";
  if(m<60) return m+" daqiiqo";
  const h=Math.floor(m/60), r=m%60;
  return h+" saac"+(r?(" "+r+"d"):"");
}
/* ---- v12.3: HEERARKA & RANGE ---- */
var LV={sym:"",last:0,an:[],d:null};
async function loadLevels(force){
  if(!$("#lvWrap")) return;
  if(!force && Date.now()-LV.last<20000) return;
  LV.last=Date.now();
  const q=new URLSearchParams(); if(accSel) q.set("account",accSel.value); if(LV.sym) q.set("sym",LV.sym);
  try{ const r=await fetch("/api/levels?"+q.toString()); const d=await r.json(); if(d&&d.ok){ LV.d=d; paintLevels(d); } }catch(e){}
}
function lvStrength(z,mtf){
  let s=0; const ia=Number(z.ia)||0, t=Number(z.t)||0, r1=Number(z.r1);
  if(ia>=1.2) s+=2; else if(ia>=0.8) s+=1;
  if(t===0) s+=1; else if(t>=3) s-=1;
  const al=z.d?(mtf>0):(mtf<0), ag=z.d?(mtf<0):(mtf>0);
  if(al) s+=1; if(ag) s-=1;
  if(r1>=25) s+=1;
  const lbl=s>=3?"XOOG SARE":(s>=1?"DHEXE":"DACIIF");
  return {s:s,lbl:lbl,w:Math.max(12,Math.min(95,30+s*16)),al:al,ag:ag};
}
function lvChart(lv,dem,sup){
  const b=(lv.b||[]).filter(x=>Array.isArray(x)&&x[0]>0&&x[1]>0);
  const W=340,H=290,L=48,R=8,T=12,B=22, dg=lv.dg, pp=Number(lv.pp)||Math.pow(10,-(dg>=3?dg-1:dg)), px=Number(lv.px)||0;
  const half=(Number(lv.atr)||0)/2*pp;
  let vals=[]; b.forEach(x=>{vals.push(x[0],x[1]);}); if(px) vals.push(px);
  [dem,sup].forEach(z=>{ if(z){ vals.push(z.lo,z.hi); } });
  if(half>0){ vals.push(px-half,px+half); }
  if(!vals.length) return '<div class="empty" style="padding:30px 0">Shumacyo weli lama helin.</div>';
  let lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals); const pad=(hi-lo)*0.08||pp*5; lo-=pad; hi+=pad;
  const y=v=>T+(hi-v)/(hi-lo)*(H-T-B), n=Math.max(b.length,2), x=i=>L+i*(W-L-R)/(n-1);
  let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Qiimaha iyo zone-yada">';
  for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4; s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'" stroke="var(--line)"/>'
      +'<text x="'+(L-5)+'" y="'+(y(v)+3.5).toFixed(1)+'" text-anchor="end" font-size="9.5" fill="#8b8a82">'+v.toFixed(dg)+'</text>'; }
  const zr=(z,fill,col,name)=>{ if(!z) return; const a=y(z.hi), c=y(z.lo);
    s+='<rect x="'+L+'" y="'+a.toFixed(1)+'" width="'+(W-L-R)+'" height="'+Math.max(2,c-a).toFixed(1)+'" fill="'+fill+'"/>';
    const ty=(z.d? a+12 : a+12); s+='<text x="'+(L+6)+'" y="'+Math.min(H-B-3,Math.max(T+10,ty)).toFixed(1)+'" font-size="10" font-weight="700" fill="'+col+'">'+name+' '+Number(z.lo).toFixed(dg)+'–'+Number(z.hi).toFixed(dg)+'</text>'; };
  zr(sup,"rgba(208,59,59,.20)","#f2a3a3","SUPPLY"); zr(dem,"rgba(38,170,110,.20)","#7fe0ab","DEMAND");
  if(half>0) [px-half,px+half].forEach(v=>{ s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'" stroke="#f0cf86" stroke-dasharray="4 4" opacity=".75"/>'; });
  b.forEach((q,i)=>{ s+='<line x1="'+x(i).toFixed(1)+'" x2="'+x(i).toFixed(1)+'" y1="'+y(q[0]).toFixed(1)+'" y2="'+y(q[1]).toFixed(1)+'" stroke="#6f7076"/>'; });
  if(b.length>1) s+='<polyline points="'+b.map((q,i)=>x(i).toFixed(1)+","+y(q[2]).toFixed(1)).join(" ")+'" fill="none" stroke="var(--s1)" stroke-width="2.2" stroke-linejoin="round"/>';
  if(px){ const sy=y(px); s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+sy.toFixed(1)+'" y2="'+sy.toFixed(1)+'" stroke="#c3c2b7" stroke-dasharray="2 3" opacity=".6"/>'
      +'<rect x="'+(W-R-96)+'" y="'+(sy-19).toFixed(1)+'" width="94" height="17" rx="8.5" fill="#15181d" stroke="var(--s1)"/>'
      +'<text x="'+(W-R-49)+'" y="'+(sy-7).toFixed(1)+'" text-anchor="middle" font-size="9.5" font-weight="700" fill="#e6e6e6">HADDA '+px.toFixed(dg)+'</text>'; }
  s+='<text x="'+L+'" y="'+(H-6)+'" font-size="9.5" fill="#8b8a82">'+b.length+' × '+esc(lv.tf||"")+'</text><text x="'+(W-R)+'" y="'+(H-6)+'" text-anchor="end" font-size="9.5" fill="#8b8a82">hadda</text>';
  return s+'</svg>';
}
function paintLevels(d){
  const syb=$("#lvSyms"), body=$("#lvBody"), emp=$("#lvEmpty"); if(!syb) return;
  syb.innerHTML=(d.syms||[]).map(x=>'<button type="button" role="tab" aria-selected="'+(x===d.sym)+'" class="'+(x===d.sym?"on":"")+'" data-s="'+esc(x)+'">'+esc(x)+'</button>').join("");
  const lv=d.lv;
  if(!lv){ body.hidden=true; emp.hidden=false; return; }
  body.hidden=false; emp.hidden=true; LV.sym=d.sym;
  const dg=(Number(lv.dg)>=0&&Number(lv.dg)<=8)?Number(lv.dg):5, pp=Number(lv.pp)||0, px=Number(lv.px)||0, mtf=Number(lv.mtf)||0;
  lv.dg=dg;
  const zs=lv.z||[], dem=zs.find(z=>z.d), sup=zs.find(z=>!z.d);
  const pill=$("#lvPill");
  if(dem&&sup){ pill.className="lvpill"; pill.textContent="RANGE"; }
  else if(mtf>0){ pill.className="lvpill up"; pill.textContent="TREND ↑"; }
  else if(mtf<0){ pill.className="lvpill dn"; pill.textContent="TREND ↓"; }
  else { pill.className="lvpill"; pill.textContent="—"; }
  $("#lvSub").textContent="Zone-yada Supply & Demand · dhaqaaqa la filayo · "+d.sym+" "+(lv.tf||"")+(d.age!=null?(" · "+d.age+"s ka hor"):"");
  $("#lvChart").innerHTML=lvChart(lv,dem,sup);
  const atr=Number(lv.atr)||0;
  if(atr>0&&pp>0&&px>0){ const h=atr/2*pp; $("#lvRange").textContent=(px-h).toFixed(dg)+" – "+(px+h).toFixed(dg);
    $("#lvRangeS").textContent="Qiyaastii ±"+(atr/2).toFixed(0)+" pip · ATR D1 = "+atr.toFixed(0)+" pip (dhaqaaqa caadiga ah ee maalin)"; }
  else { $("#lvRange").textContent="—"; $("#lvRangeS").textContent="ATR weli lama helin."; }
  /* bot-ku maxuu sugayaa */
  const a=(LV.an||[]).find(r=>r&&r.sym===d.sym)||null, bot=$("#lvBot");
  let t="Xaaladda bot-ka chart-kan weli lama helin.";
  if(a){ const st=String(a.st||""), side=String(a.side||""), hasZ=Number(a.lo)>0&&Number(a.hi)>0, dir=(side==="DEMAND")?"BUY":"SELL";
    const zt=esc(side)+" "+anNum(a.lo,dg)+"–"+anNum(a.hi,dg), dist=Number(a.dist);
    if(a.status&&a.status!=="RUNNING") t='<b>Bot-ku ma furayo trade cusub:</b> '+esc(ST_TXT[a.status]||a.status)+'.';
    else if(st==="SIGNAL") t='<b>Signal!</b> Shuruudihii waa buuxsameen — bot-ku '+dir+' ayuu furayaa ('+zt+').';
    else if(st==="NEWS") t='<b>War ayaa socda:</b> '+esc(a.news||"")+' — bot-ku wuu sugayaa ilaa uu dhammaado.';
    else if(st==="IN"&&hasZ) t='<b>Qiimuhu wuxuu ku jiraa</b> '+zt+' — bot-ku wuxuu sugayaa xaqiijin (CHoCH) → <b>'+dir+'</b>.';
    else if(hasZ) t='<b>Bot-ku wuxuu sugayaa:</b> qiimuhu '+zt+' ha taabto + xaqiijin (CHoCH) → <b>'+dir+'</b>.'+(dist>0?(' Masaafo: <b>'+dist.toFixed(1)+' pip</b>.'):"");
    else t='<b>Zone ku habboon weli lama helin.</b>'+(a.why?(' '+esc(a.why)):"");
  }
  bot.innerHTML='<span aria-hidden="true">🎯</span><div>'+t+'</div>';
  /* caddaynta */
  const mtfTxt=mtf>0?"KOR ↑":(mtf<0?"HOOS ↓":"dhexe");
  const card=z=>{ if(!z) return ""; const S=lvStrength(z,mtf), t=Number(z.t)||0, dp=Number(z.dp)||0;
    const col=z.d?"#26aa6e":"#d03b3b";
    let ev='<span>Taabasho: '+(t===0?'<b>cusub — weli lama taaban</b>':('<b>'+t+' jeer · dhammaan way qabteen</b><span class="lvdots">'+'<b></b>'.repeat(Math.min(t,5))+'</span>'))+'</span>';
    ev+='<span>Impulse ka baxay: <b>'+Number(z.ip||0).toFixed(0)+' pip</b> ('+Number(z.ia||0).toFixed(1)+' × ATR)</span>';
    if(Number(z.r1)>0) ev+='<span>Ka laabashadii 1aad: <b>'+Number(z.r1).toFixed(0)+' pip</b></span>';
    ev+='<span>Jihada weyn (H4 · EMA200): <b>'+mtfTxt+'</b>'+(S.al?' · la socota ✓':(S.ag?' · ka soo horjeeda ✕':''))+'</span>';
    ev+='<span>'+(dp>0?('Masaafo: <b>'+dp.toFixed(1)+' pip</b>'):'<b>Qiimuhu zone-ka gudihiisa ayuu ku jiraa</b>')+'</span>';
    if(z.u) ev+='<span style="color:#f0c070">Zone-kan hore waa loo ganacsaday — mar kale lama galo.</span>';
    return '<div class="lvz '+(z.d?"d":"s")+'"><div class="zk">'+(z.d?"DEMAND (IIBSO)":"SUPPLY (IIBI)")+' · '+S.lbl+'</div>'
      +'<div class="zv">'+Number(z.lo).toFixed(dg)+' – '+Number(z.hi).toFixed(dg)+'</div><div class="ev">'+ev+'</div>'
      +'<div class="lvbar"><i style="width:'+S.w+'%;background:'+col+'"></i></div></div>'; };
  $("#lvZones").innerHTML=(dem||sup)?(card(dem)+card(sup)):'<div class="note" style="margin-top:8px">Zone nool oo u dhow qiimaha weli lama helin.</div>';
  /* maxaa beddeli kara */
  const li=[];
  if(sup) li.push('Shumac '+esc(lv.tf||"")+' oo <b>ka xidhma '+Number(sup.hi).toFixed(dg)+' kor</b> → supply-ga waa jabay (breakout)');
  if(dem) li.push('Shumac '+esc(lv.tf||"")+' oo <b>ka xidhma '+Number(dem.lo).toFixed(dg)+' hoos</b> → demand-ka waa jabay');
  const nw=(a&&a._news||[]).filter(n=>n&&n.im==="High"&&Number(n.m)>=0);
  if(nw.length) li.push('War xoog leh: <b>'+esc(nw[0].ti||"")+'</b> ('+Number(nw[0].m)+' daq) → bot-ku wuu joogsadaa');
  else li.push('War xoog leh (NFP, CPI, dulsaarka) → bot-ku wuu joogsadaa');
  $("#lvChg").innerHTML=li.map(x=>"<li>"+x+"</li>").join("");
}
if($("#lvSyms")) $("#lvSyms").addEventListener("click",e=>{ const b=e.target.closest("button"); if(!b) return; LV.sym=b.dataset.s; loadLevels(true); });

function paintAnalysis(rows){
  const box=$("#anList"); if(!box) return;
  rows=rows||[];
  LV.an=rows;                                        // v12.3
  if($("#pAnaliis") && $("#pAnaliis").classList.contains("on")) loadLevels(false);
  const nb=$("#anBadge"); if(nb) nb.textContent=rows.length?rows.length:"";
  if(!rows.length){
    box.innerHTML='<div class="empty" style="padding:22px 0">Weli analiis lama helin.<br>'
      +'Chart kasta EnableCloudDashboard = true ka dhig.</div>';
    $("#anAge").textContent="—"; $("#anNote").textContent="";
    paintNews([]); return;
  }
  let minAge=1e9;
  box.innerHTML=rows.map(r=>{
    const st=String(r.st||"WAIT");
    const k=AN_CLS[st]||"", lb=AN_LBL[st]||st;
    if(Number(r._age)<minAge) minAge=Number(r._age);
    let dg=Number(r.dg);
    if(isNaN(dg)||dg<0||dg>8){ const q=Number(r.px)||0;
      dg=(q>=500)?2:((q>=20)?3:5); }
    const d=Number(r.dist);
    const hasZ=(Number(r.lo)>0&&Number(r.hi)>0);
    // 0 pip = buuxa, 60 pip+ = madhan
    let pct=0, fcl="";
    if(!hasZ){ pct=2; fcl="far"; }
    else if(!(d>=0)){ pct=2; fcl="far"; }
    else if(d<=0){ pct=100; fcl="in"; }
    else { pct=Math.max(3,Math.round((1-Math.min(d,60)/60)*100)); fcl=(d<=25?"":"far"); }
    const zline=hasZ
      ? ((r.side||"")+"  "+anNum(r.lo,dg)+" – "+anNum(r.hi,dg)+"  ·  "+(r.tf||""))
      : ("zone lama helin  ·  "+(r.zones||0)+" zone nool");
    const right=hasZ?((d>0)?(d.toFixed(1)+" pip u jira"):"zone gudihiisa"):"0 zone";
    const why=r.why?('<div class="zwhy">'+esc(r.why)+'</div>')
                   :(st==="SIGNAL"?'<div class="zwhy">Shuruudihii waa buuxsameen.</div>':"");
    const nx=(st==="NEWS"&&r.news)?('<div class="zwhy">📰 '+esc(r.news)+'</div>'):"";
    // v8.3: chart-kan xaaladdiisa, sitinkiisa iyo sababaha diidmada
    let cx="";
    if(r.status && r.status!=="RUNNING")
      cx+='<div class="zwhy" style="color:#f2a3a3">'+esc(ST_TXT[r.status]||r.status)+'</div>';
    const cs=r.settings||null;
    if(cs){
      const bits=[];
      bits.push("risk "+Number(cs.risk||0)+"%");
      if(Number(cs.sl)>0||Number(cs.tp)>0) bits.push("SL "+Number(cs.sl||0)+" / TP "+Number(cs.tp||0));
      bits.push(cs.mgmt_off?"maamul DAMMAN":"maamul ON");
      if(cs.sniper) bits.push("sniper");
      if(cs.adaptive) bits.push("ATR");
      cx+='<div class="zmeta">'+esc(bits.join(" · "))+(r.ver?(' · v'+esc(r.ver)):"")+'</div>';
    }
    if(Array.isArray(r.fr)){
      const top=r.fr.map((v,i)=>[Number(v)||0,i]).filter(z=>z[0]>0).sort((a,b)=>b[0]-a[0]).slice(0,3);
      if(top.length) cx+='<div class="zmeta">Diidmo: '+esc(top.map(z=>FR_NM[z[1]]+" "+z[0]).join(" · "))
        +(r.opened!=null?(' · la furay '+Number(r.opened)):"")+'</div>';
    }
    return '<div class="zc '+k+'">'
      +'<div class="zh"><span class="zs">'+esc(r.sym||"")+'</span>'
      +'<span class="zbadge '+k+'">'+lb+'</span></div>'
      +'<div class="zl">'+esc(zline)+'</div>'
      +'<div class="ztr"><i class="'+fcl+'" style="width:'+pct+'%"></i></div>'
      +'<div class="zft"><span>sicir '+anNum(r.px,dg)+'</span><span>'+esc(right)+'</span></div>'
      +why+nx+cx+'</div>';
  }).join("");
  $("#anAge").textContent=(minAge<1e9)?(minAge+"s ka hor"):"—";
  $("#anNote").textContent="Chart "+rows.length+"  ·  cusboonaysii 12 ilbiriqsi kasta  ·  chart kastaa gooni";
  // wararka: mid kasta hal mar (symbol-yada oo dhan la isku daray)
  const seen={}, nw=[];
  rows.forEach(r=>(r._news||[]).forEach(n=>{
    const key=(n.ti||"")+"|"+(n.cc||"")+"|"+(n.m||0);
    if(seen[key]) return; seen[key]=1; nw.push(n);
  }));
  nw.sort((a,b)=>Number(a.m)-Number(b.m));
  paintNews(nw);
}
function paintNews(rows){
  const box=$("#nwList"); if(!box) return;
  rows=(rows||[]).filter(n=>Number(n.m)>=-30).slice(0,8);
  if(!rows.length){
    box.innerHTML='<div class="empty" style="padding:16px 0">War soo socda lama hayo.</div>';
    $("#nwNote").textContent=""; return;
  }
  const IMP={High:"XOOG BADAN",Medium:"DHEXDHEXAAD",Low:"YAR"};
  box.innerHTML=rows.map(n=>{
    const m=Number(n.m), soon=(m<=30&&m>=-30);
    return '<div class="nv"><div><div class="nt">'+esc(n.ti||"")+'</div>'
      +'<div class="nm">'+esc(n.cc||"")+'  ·  '+esc(IMP[n.im]||n.im||"")+'</div></div>'
      +'<div class="nc'+(soon?" soon":"")+'">'+esc(anMins(m))+'</div></div>';
  }).join("");
  $("#nwNote").innerHTML='<div class="nwarn">Bot-ku wuu joojinayaa ganacsiga waqtiga warka '
    +'(ka hor iyo ka dib), haddii News Filter uu ON yahay.</div>';
}

/* ---- v6: xogta EA-du dirayso ---- */
function paintRaw(x, d){
  const box=$("#rawBox"); if(!box) return;
  let txt="{}";
  try{ txt=JSON.stringify(x,null,2); }catch(e){ txt="(lama akhriyi karo)"; }
  if(txt.length>40000) txt=txt.slice(0,40000)+"\\n… (la gooyay)";
  box.textContent=txt;
  const kb=(new Blob([txt]).size/1024).toFixed(1);
  $("#rawSize").textContent=kb+" KB";
  $("#rawAge").textContent=(d.age==null)?"—":(d.age+"s ka hor"+(d.online?"":"  ·  OFFLINE"));
  const q=accSel?("?account="+encodeURIComponent(accSel.value)):"";
  $("#dlCsv").href="/api/export/trades.csv"+q;
  $("#dlJson").href="/api/export/snapshot.json"+q;
}

/* ---- v5: MAAMULKA ---- */
const MF=["LOT","STEP","STEPSTART","RISK","DLOSS","MAXDD","SNRR","SNSLMAX","LOT2","BSKRISK","BSKMAX","BSKSPR","BSKTGT","BSKDTOL","BSKDNECK","BSKDTP"];   // v12.6 · v12.7
let mTouched=false, mSeeded=false;
function swSet(id,on){ const e=$("#"+id); if(e) e.classList.toggle("on",!!on); }
function swGet(id){ const e=$("#"+id); return e && e.classList.contains("on"); }
["mSNIPER","mNEWS","mEMAF","mBSK","mBSKBE","mBSKLOCK","mBSKSES","mBSKBLK"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; if(id==="mBSK") bskGrpPaint(); }); });   // v12.6 · v12.7
MF.forEach(k=>{ const e=$("#m"+k); if(e) e.addEventListener("input",()=>{ mTouched=true; }); });

/* ---- v12.1/v12.2: MUUQAALKA - midabka app-ka + Colour Matrix (localStorage - telefoon kasta gooni) ---- */
(function(){
  const sw=$("#mxSw"); if(!sw) return;
  const PAL=[["Buluug","#3987e5"],["Dahab","#d9ae55"],["Cagaar","#22c55e"],["Guduud","#a855f7"],
             ["Casaan","#ef4444"],["Oranji","#f97316"],["Cyan","#06b6d4"],["Pink","#ec4899"]];
  const DEF_ACC="#3987e5", DEF_MX=["#d9ae55","#3987e5","#a855f7","#22c55e"];
  const SPD=[["14s","3.6s"],["9s","2.4s"],["4.5s","1.2s"]];
  const HEX=/^#[0-9a-f]{6}$/;
  const rd=(k,d)=>{ try{ const v=localStorage.getItem(k); return v===null?d:v; }catch(e){ return d; } };
  const wr=(k,v)=>{ try{ localStorage.setItem(k,v); }catch(e){} };
  let T={}; try{ T=JSON.parse(rd("mohaTheme","{}"))||{}; }catch(e){ T={}; }
  let acc=HEX.test(String(T.acc||"").toLowerCase())?T.acc.toLowerCase():DEF_ACC;
  let cus=HEX.test(String(T.cus||"").toLowerCase())?T.cus.toLowerCase():"";
  let mx=Array.isArray(T.mx)?T.mx.map(x=>String(x).toLowerCase()).filter(x=>HEX.test(x)).slice(0,12):[];
  if(!mx.length) mx=DEF_MX.slice();
  let spd=[0,1,2].includes(Number(T.spd))?Number(T.spd):1;
  const save=()=>wr("mohaTheme",JSON.stringify({acc:acc,cus:cus,mx:mx,spd:spd}));
  const root=document.documentElement;
  function applyAcc(){
    const on=(acc!==DEF_ACC);
    root.classList.toggle("th",on);
    if(on) root.style.setProperty("--acc",acc); else root.style.removeProperty("--acc");
    const m=document.querySelector('meta[name="theme-color"]');
    if(m) m.setAttribute("content",on?getComputedStyle(document.body).backgroundColor:"#0f1013");
  }
  function applyMx(){
    let st=$("#mxKF"); if(!st){ st=document.createElement("style"); st.id="mxKF"; document.head.appendChild(st); }
    const n=mx.length, fr=mx.map((c,i)=>Math.round(i*100/n)+"%{--mxc:"+c+"}").join("");
    st.textContent="@keyframes mxHue{"+fr+"100%{--mxc:"+mx[0]+"}}";
    document.body.style.setProperty("--mx0",mx[0]);
    document.body.style.setProperty("--mxh",SPD[spd][0]); document.body.style.setProperty("--mxp",SPD[spd][1]);
  }
  /* swatches - midabka app-ka */
  const box=$("#thSw");
  function paintSw(){
    const inPal=PAL.some(p=>p[1]===acc);
    box.innerHTML=PAL.map(p=>'<button type="button" role="radio" data-c="'+p[1]+'" aria-label="'+p[0]+'" aria-checked="'+(p[1]===acc)+'" class="'+(p[1]===acc?"on":"")+'" style="background:'+p[1]+'"></button>').join("")
      +'<button type="button" role="radio" class="plus'+(!inPal?" on":"")+'" id="thPlus" aria-label="Midab kale" aria-checked="'+(!inPal)+'"'+(!inPal?' style="--cus:'+acc+'"':'')+'></button>'
      +'<input type="color" id="thPick" value="'+(cus||acc)+'" aria-label="Dooro midab kale">';
  }
  box.addEventListener("click",e=>{
    const b=e.target.closest("button"); if(!b) return;
    if(b.id==="thPlus"){ const pk=$("#thPick"); pk.value=cus||acc; if(pk.showPicker){ try{ pk.showPicker(); return; }catch(_){ } } pk.click(); return; }
    acc=b.dataset.c; save(); applyAcc(); paintSw();
  });
  box.addEventListener("input",e=>{ if(e.target.id!=="thPick") return; const v=String(e.target.value).toLowerCase(); if(!HEX.test(v)) return;
    acc=v; cus=v; save(); applyAcc();
    const pl=$("#thPlus"); box.querySelectorAll("button").forEach(x=>{ x.classList.remove("on"); x.setAttribute("aria-checked","false"); });
    pl.classList.add("on"); pl.setAttribute("aria-checked","true"); pl.style.setProperty("--cus",v); });
  box.addEventListener("change",e=>{ if(e.target.id==="thPick") paintSw(); });
  /* midabada iftiinka */
  const ch=$("#mxCh");
  function paintCh(){
    ch.innerHTML=PAL.map(p=>{ const on=mx.includes(p[1]); return '<button type="button" aria-pressed="'+on+'" data-c="'+p[1]+'" class="'+(on?"on":"")+'" style="--c:'+p[1]+'"><i style="background:'+p[1]+'"></i>'+p[0]+'</button>'; }).join("");
  }
  ch.addEventListener("click",e=>{ const b=e.target.closest("button"); if(!b) return; const c=b.dataset.c;
    if(mx.includes(c)){ if(mx.length===1) return; mx=mx.filter(x=>x!==c); }
    else mx=PAL.map(p=>p[1]).filter(x=>x===c||mx.includes(x));
    save(); applyMx(); paintCh(); });
  /* xawaaraha */
  const sp=$("#mxSpd");
  const paintSp=()=>sp.querySelectorAll("button").forEach(b=>{ const on=Number(b.dataset.s)===spd; b.classList.toggle("on",on); b.setAttribute("aria-pressed",on); });
  sp.addEventListener("click",e=>{ const b=e.target.closest("button"); if(!b) return; spd=Number(b.dataset.s); save(); applyMx(); paintSp(); });
  /* shid / dami */
  const setMx=v=>{ document.body.classList.toggle("mx",v); sw.classList.toggle("on",v); sw.setAttribute("aria-checked",v?"true":"false"); $("#mxOpt").hidden=!v; };
  sw.addEventListener("click",()=>{ const v=!document.body.classList.contains("mx"); setMx(v); wr("mohaMx",v?"1":"0"); });
  applyAcc(); applyMx(); paintSw(); paintCh(); paintSp(); setMx(rd("mohaMx","0")==="1");
})();

/* ---- v12: laysinka + oggolaanshaha macmiilka ---- */
const PERMS={{ perms_json|safe }};
const PERM_OF={RISK:"risk",LOT:"lot",SLTP:"sltp",SL:"sltp",TP:"sltp",SNIPER:"sltp",SNRR:"sltp",SNSLMAX:"sltp",ADAPT:"sltp",
  SNDAY:"day",NEWS:"prot",STEPON:"prot",STEP:"prot",STEPSTART:"prot",BE:"prot",LOCKMODE:"prot",PROT:"prot"};   // STRAT/EMAF/STARS/LOT2/MGMT = admin
const PERM_EL={mRISK:"risk",mDLOSS:"",mMAXDD:"",mSNIPER:"sltp",mSNDAYm:"day",mSNRR:"sltp",mSNSLMAX:"sltp",mNEWS:"prot",
  mLOT:"lot",mSTEP:"prot",mSTEPSTART:"prot",mEMAF:"",mLOT2:"",mSTARS:"",
  mBSKDAYm:"",mBSKNm:"",mBSKRISK:"",mBSKMAX:"",mBSKSPR:"",mBSKTGT:"",mBSKBE:"",mBSKLOCK:"",mBSKSES:"",mBSKBLK:"",mBSKDTOL:"",mBSKDNECK:"",mBSKDTP:"",
  mASIAWEm:"",mASIACLm:"",mASIATR:"",mASIAMAXR:"",mASIATPm:"",mASIARISK:"",mASIAMAX:"",
  mASGLm:"",mASGSm:"",mASGXm:"",
  mTKONLY:"",mTKWm:"",mTKKm:"",mTKMV:"",mTKSEC:"",mTKLOT:"",mTKRISK:"",mTKVSL:"",mTKVTP:"",mTKBE:"",mTKBEL:"",mTKTRS:"",mTKTRD:"",
  mTKHOLD:"",mTKHARD:"",mTKHS:"",mTKSPR:"",mTKCD:"",mTKMAXD:"",mTKML:"",mTKDL:"",mTKDT:"",mTKSPM:"",mTKTR:"",mTKBNm:"",mTKBTGT:"",mTKBBE:"",mTKBLP:"",
  mTKTRON:"",mTKTRST:"",mTKTRLK:"",mTKTRSTEP:"",mTKTRMAX:"",
  mTKDRON:"",mTKDRB:"",mTKDRN:"",mTKDRS:"",mTKDRW:""};   // v12.19: JIHADA = admin   // v12.17: TP RAAC = admin   // v12.12: TICK = admin   // v12.10: GRID = admin   // v12.9: ASIA = admin   // v12.7: GOLD BASKET = admin
let licOK=true;
const LKSVG='<svg class="lk" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
function permOK(p){ return !PERMS || (licOK && !!(p && PERMS[p])); }
function lockEl(row){ if(!row||row.classList.contains("locked")) return; row.classList.add("locked");
  row.querySelectorAll("input,button").forEach(x=>{ x.disabled=true; x.tabIndex=-1; });
  const lab=row.querySelector("span"); if(lab) lab.insertAdjacentHTML("beforeend",LKSVG); }
function applyPerms(){
  if(!PERMS) return;
  Object.keys(PERM_EL).forEach(id=>{ const e=$("#"+id); if(!e) return;
    if(!permOK(PERM_EL[id])) lockEl(e.closest(".frow")); });
  const segLock=id=>{ const sg=$("#"+id); if(sg){ sg.classList.add("locked"); sg.querySelectorAll("button").forEach(b=>b.disabled=true); } };
  if(!permOK("prot")) segLock("mPROT");
  segLock("mSTRAT"); const rs=$("#rSTRAT"); if(rs) rs.classList.add("locked");   // v12.6: xeeladda = admin
  segLock("mSDPROF"); const rsd=$("#rSDPROF"); if(rsd) rsd.classList.add("locked");   // v12.8: heerka zone-ka = admin
  ["mBSKSIG","mBSKENT","mBSKTP"].forEach(segLock); const bsw=$("#mBSK"); if(bsw){ bsw.disabled=true; bsw.classList.add("lockd"); }   // v12.7
  ["mGOLD","mASIADIR","mASIASL","mASIAL","mASGM"].forEach(segLock); const asw=$("#mASIA"); if(asw){ asw.disabled=true; asw.classList.add("lockd"); }   // v12.9
  { const gsw=$("#mASG"); if(gsw){ gsw.disabled=true; gsw.classList.add("lockd"); } }   // v12.10
  segLock("mTKLM"); segLock("mTKSLM"); segLock("mTKTPM"); ["mTKBON","mTKBENT","mTKBEX","mTKDRM"].forEach(segLock); { const tsw=$("#mTK"); if(tsw){ tsw.disabled=true; tsw.classList.add("lockd"); } }   // v12.12
  document.querySelectorAll("[data-perm]").forEach(b=>{ if(!permOK(b.dataset.perm)){ b.classList.add("lockd"); b.disabled=true; } });

  const r=$("#mRISK"); if(r && permOK("risk")){ r.min=PERMS.lo; r.max=PERMS.hi;
    const lab=r.closest(".frow").querySelector("span"); if(lab && !lab.querySelector(".rhint")) lab.insertAdjacentHTML("beforeend",'<small class="rhint">xadka: '+Number(PERMS.lo).toFixed(2)+' – '+Number(PERMS.hi).toFixed(2)+'%</small>'); }
  const any=Object.keys(PERM_OF).some(k=>permOK(PERM_OF[k])); ["mSend","mSend2"].forEach(id=>{ const sb=$("#"+id); if(sb && !any){ sb.disabled=true; sb.classList.add("lockd"); } });
}
function paintLic(d){
  const c=$("#licCard"); if(!c) return; const L=d.lic;
  if(!L){ c.hidden=true; return; } c.hidden=false;
  const pill=$("#licPill"), big=$("#licBig"), sub=$("#licSub");
  const dt=L.until?new Date(L.until*1000).toLocaleDateString([], {day:"numeric",month:"short",year:"numeric"}):"";
  if(L.state==="OK"){ big.textContent="Shaqeynaya"; sub.textContent="Wuxuu dhacayaa "+dt+" · "+L.days+" maalmood ayaa haray";
    pill.className="licpill "+(L.days<=5?"warn":"ok"); pill.textContent=L.days<=5?"DHOW":"✓ ACTIVE"; }
  else if(L.state==="BLOCKED"){ big.textContent="Waa la xannibay"; sub.textContent="Bot-ku trade cusub ma furo — admin-ka la xiriir.";
    pill.className="licpill bad"; pill.textContent="XANNIBAN"; }
  else { big.textContent="Wuu dhacay"; sub.textContent=(dt?("Wuxuu dhacay "+dt+" · "):"")+"bot-ku trade cusub ma furo — admin-ka la xiriir.";
    pill.className="licpill bad"; pill.textContent="DHACAY"; }
  if(PERMS){ const ok=(L.state==="OK"); if(ok!==licOK){ licOK=ok; if(!ok) applyPerms(); } }
}
applyPerms();

/* ---- v11: furaha bot-ka (isticmaalaha) ---- */
(function(){
  const b=$("#myKeyCp"); if(!b) return;
  b.addEventListener("click",()=>{
    const k=$("#myKeyVal").textContent.trim(), old=b.textContent;
    const ok=()=>{ b.textContent="LA KOOBIYAY ✓"; setTimeout(()=>b.textContent=old,1800); };
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(k).then(ok,()=>{ const r=document.createRange(); r.selectNode($("#myKeyVal")); getSelection().removeAllRanges(); getSelection().addRange(r); });
  });
})();
function paintMyKey(d){
  const e=$("#myKeySt"); if(!e) return;
  const a=d.key_seen_age;
  if(a===null||a===undefined){ e.innerHTML='<span style="color:#e0a040">● Weli bot-kaagu furahan ma isticmaalin</span>'; return; }
  const live=a<120;
  e.innerHTML=live?('<span style="color:#7fe0ab">✓ Bot-kaagu wuu xidhan yahay · '+a+' ilbiriqsi ka hor</span>')
                  :('<span style="color:#e0a040">● Bot-ku wuu go’ay · '+Math.round(a/60)+' daqiiqo ka hor</span>');
}

/* ---- v10: stepper-ka trade maalintii + bar-ka kaydinta ---- */
function snDay(d){ const e=$("#mSNDAY"); if(!e) return; let v=Number(e.textContent)||0; v=Math.max(0,Math.min(50,v+d)); e.textContent=v; mTouched=true; }
if($("#mSNDAYm")) $("#mSNDAYm").addEventListener("click",()=>snDay(-1));
if($("#mSNDAYp")) $("#mSNDAYp").addEventListener("click",()=>snDay(1));
function paintSave(d){
  const b=$("#mSave"); if(!b) return;
  const rev=Number(d.cfg_rev||0), rows=(d.analysis||[]).filter(a=>a&&a.settings&&Number(a._age)<=300);
  if(!rev){ b.className="savebar"; b.innerHTML='<span class="dt"></span>Weli lama kaydin — bot-ku wuxuu isticmaalayaa sitinka koodka. Riix <b>KAYDI &amp; DIR</b>.'; return; }
  const got=rows.filter(a=>Number(a.settings.rev)===rev).length, n=rows.length;
  const when=d.cfg_saved?new Date(d.cfg_saved*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}):"";
  const all=(n>0 && got===n);
  b.className="savebar"+(all?" ok":"");
  b.innerHTML='<span class="dt"></span>Server-ka ayuu ku kaydsan yahay'+(when?(' · '+esc(when)):"")+' · '
    +(n?('<b>'+got+'/'+n+' chart</b> '+(all?"way qaateen":"ayaa qaatay — kuwa kale way sugayaan")):'chart online ma jiro');
}

/* ---- v12.6: maamulka - xeeladda · ★ · ilaalinta faa'iidada ---- */
let mStrat=null, mStars=null, mProt=null, mSavedAt=0;
function segPaint(id,attr,val){ document.querySelectorAll("#"+id+" button").forEach(b=>{ const on=(String(b.dataset[attr])===String(val)); b.classList.toggle("on",on); b.setAttribute("aria-checked",on?"true":"false"); }); }
function protHint(){
  const h=$("#mPROTHint"); const st=$("#mSTEPSTART"), sp=$("#mSTEP");
  const a=st&&st.value?st.value:"15", b=sp&&sp.value?sp.value:"15";
  if(h) h.innerHTML = mProt===0 ? "<b>DAMMAN</b> — SL meesha uu ku furmay ayuu joogaa ilaa SL ama TP."
    : mProt===1 ? "<b>BREAK-EVEN</b> — marka +"+esc(a)+" pip la gaadho, SL → gelitaanka. Kadib TP ama BE."
    : mProt===2 ? "<b>TALLAABO</b> — +"+esc(a)+" pip kadib SL-ku kor buu u socdaa "+esc(b)+" pip kasta."
    : "Dooro.";
  const r1=$("#rSTEPSTART"), r2=$("#rSTEP");
  if(r1) r1.classList.toggle("off",mProt===0);
  if(r2) r2.classList.toggle("off",mProt!==2);
  segPaint("mPROT","p",mProt);
}
document.querySelectorAll("#mPROT button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mProt=Number(b.dataset.p); mTouched=true; protHint(); }));
document.querySelectorAll("#mSTRAT button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mStrat=Number(b.dataset.s); mTouched=true; segPaint("mSTRAT","s",mStrat); }));
/* v12.8 (EA v69.6): heerka zone-ka */
let mSdProf=null;
function sdPaint(){
  segPaint("mSDPROF","v",mSdProf);
  document.querySelectorAll("#sdTbl [data-c]").forEach(c=>c.classList.toggle("on",String(c.dataset.c)===String(mSdProf)));
  const h=$("#mSDPROFHint"); if(!h) return;
  h.innerHTML = mSdProf===0 ? "<b>TAYO:</b> zone-yada ugu xoogga badan oo keliya → trade aad u yar."
    : mSdProf===1 ? "<b>DHEXE:</b> BOS + impulse 20p waa khasab. Warbixinta: zone-yada badankood waa la diiday."
    : mSdProf===2 ? "<b>BADAN:</b> BOS khasab ma aha · impulse 15p · taabasho 12 saac → fursado badan (Sniper CHoCH weli wuu xaqiijiyaa)."
    : "EA v69.6 ama ka dambe ayaa loo baahan yahay.";
}
document.querySelectorAll("#mSDPROF button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mSdProf=Number(b.dataset.v); mTouched=true; sdPaint(); }));
document.querySelectorAll("#mSTARS button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mStars=Number(b.dataset.v); mTouched=true; segPaint("mSTARS","v",mStars); }));
["mSTEPSTART","mSTEP"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("input",protHint); });
const STRAT_I={SR:0,SMC:1,BOTH:2};
function paintEma(st){
  const ch=$("#emaChip"); if(!ch || !st) return;
  if(st.ema===undefined){ ch.hidden=true; return; }
  ch.hidden=false; const e=Number(st.ema), on=(st.ema_on!==false);
  const rg=!!Number(st.rng||0);   // v12.6.1: RANGE (EA v68.3)
  ch.className="echip "+(!on?"off":(rg?"mx":(e>0?"up":(e<0?"dn":"mx"))));
  ch.querySelector("span").textContent=!on?"EMA filter: damman":(rg?"Hadda: RANGE · suuqu wuu isku fadhiyaa · ma ganacsado":(e>0?"Hadda: EMA ↑ KOR · BUY kaliya":(e<0?"Hadda: EMA ↓ HOOS · SELL kaliya":"Hadda: EMA ⇅ · H1/H4 isma hagaagsana · ma ganacsado")));
}

/* ---- v12.7: GOLD BASKET (maamul) ---- */
let mBskSig=null, mBskEnt=null, mBskTP=null;
function bskHints(){
  const sh=$("#mBSKSIGHint"), eh=$("#mBSKENTHint");
  if(sh) sh.innerHTML = mBskSig===0 ? "<b>ZONE:</b> demand (BUY) / supply (SELL) cusub → taabasho → CHoCH M5 → basket."
    : mBskSig===1 ? "<b>EMA:</b> trend cad (EMA50 &gt; EMA200 M15 + H1/H4) → sicirku EMA 21–50 ayuu dib ugu soo laabtaa → shumac diidmo → basket."
    : mBskSig===2 ? "<b>LABADA:</b> mid kasta wuu furi karaa → fursado badan. <b>Zone + EMA isku meel</b> = ★★★ (lot buuxa) · mid keliya = ★★."
    : mBskSig===3 ? "<b>DOUBLE:</b> trend EMA50/200 (M15) → double bottom (BUY) / double top (SELL) → <b>salka 2 + engulfing / pin</b> → basket shumaca xiga · SL hoosta labada sal · TP 2R · <b>saacadaha oo dhan</b> (Tokyo · London · NY). Tijaabo 6 bil: +47R · EV +0.30R. <b>ISKU MAR</b> ayaa la tijaabiyay."
    : "Dooro.";
  if(eh) eh.innerHTML = mBskEnt===1 ? "<b>ISKU MAR:</b> dhammaan trade-yada hal mar ayay furmaan (qiime isku mid ah)."
    : "<b>LAKAB:</b> #1 = hadda · #2 #3 = zone-ka gudihiisa (qiime ka fiican). 60 daqiiqo gudahood haddii aan la gaadhin → waa la tirtiraa.";
  segPaint("mBSKSIG","v",mBskSig); segPaint("mBSKENT","v",mBskEnt); segPaint("mBSKTP","v",mBskTP);
  { const db=$("#bskDbl"); if(db) db.hidden=(mBskSig!==3); }   // v12.11
  const tg=$("#rBSKTGT"); if(tg) tg.classList.toggle("off",mBskTP!==1);
}
function bskGrpPaint(){ const g=$("#bskGrp"); if(g) g.classList.toggle("off",!swGet("mBSK")); const db=$("#bskDbl"); if(db) db.hidden=(mBskSig!==3); }   // v12.11
document.querySelectorAll("#mBSKSIG button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mBskSig=Number(b.dataset.v); mTouched=true; bskHints(); }));
document.querySelectorAll("#mBSKENT button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mBskEnt=Number(b.dataset.v); mTouched=true; bskHints(); }));
document.querySelectorAll("#mBSKTP button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mBskTP=Number(b.dataset.v); mTouched=true; bskHints(); }));
function bskStep(id,d,lo,hi){ const e=$("#"+id); if(!e) return; let v=Number(e.textContent)||lo; v=Math.max(lo,Math.min(hi,v+d)); e.textContent=v; mTouched=true; }
[["mBSKN",2,5],["mBSKDAY",1,10]].forEach(([id,lo,hi])=>{
  const m=$("#"+id+"m"), p=$("#"+id+"p");
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) bskStep(id,-1,lo,hi); });
  if(p) p.addEventListener("click",()=>{ if(!p.disabled) bskStep(id,1,lo,hi); });
});
/* ---- v12.12: ⚡ TICK SCALPER (Maamul · admin) ---- */
let TKCFG=null, TICK=null, mTkLM=0, mTkTouched=false, mTkSLM=1, mTkTPM=1, mTkBON=0, mTkBENT=1, mTkBEX=1, mTkDRM=1;
const TK_IN=["DRB","DRN","DRS","DRW","TRST","TRLK","TRSTEP","TRMAX","BTGT","BBE","BLP","SPM","MV","SEC","LOT","RISK","VSL","VTP","BE","BEL","TRS","TRD","HOLD","HARD","HS","HE","SPR","CD","MAXD","ML","PAUSE","DL","DT"];
function tkVal(k){ const e=$("#mTK"+k); if(!e||e.value==="") return null; const v=Number(e.value); return isFinite(v)?v:null; }
function tkPaint(){
  const g=$("#tkGrp"); if(g) g.classList.toggle("off",!swGet("mTK"));
  segPaint("mTKLM","v",mTkLM);
  const rl=$("#rTKLOT"), rr=$("#rTKRISK"); if(rl) rl.style.display=(mTkLM!==0)?"none":""; if(rr) rr.style.display=(mTkLM!==1)?"none":"";
  segPaint("mTKSLM","v",mTkSLM);   // v12.14
  segPaint("mTKTPM","v",mTkTPM);   // v12.15
  { segPaint("mTKBON","v",mTkBON); segPaint("mTKBENT","v",mTkBENT); segPaint("mTKBEX","v",mTkBEX);   // v12.16 BASKET
    const has=!!(TKCFG && TKCFG.bon!==undefined && TKCFG.bon!==null), sec=$("#tkBskSec"), op=$("#tkBskOpts"), bh=$("#mTKBHint"), tg=$("#rTKBTGT");
    if(sec) sec.style.opacity=(TKCFG && !has)?".45":"";
    if(op) op.style.display=(mTkBON===1)?"":"none";
    if(tg) tg.style.display=(mTkBEX===1)?"":"none";
    const N=Number(($("#mTKBN")||{}).textContent)||3, vsl=tkVal("VSL")||2, tgt=tkVal("BTGT")||0, lot=tkVal("LOT")||0.01;
    if(bh) bh.innerHTML=(TKCFG && !has)?"Bot-ka chart-ka wuxuu u baahan yahay <b>EA v70.4</b> si BASKET u shaqeeyo."
      :(mTkBON!==1?"<b>HAL TRADE:</b> signal kasta = hal trade (sidii hore).":
        ("<b>"+N+" trade</b> × "+lot.toFixed(2)+" · SL wadaag $"+vsl.toFixed(2)+" → khatar ugu badan <b>$"+(N*lot*100*vsl).toFixed(2)+"</b>"+
         (mTkBEX===1?(" · bartilmaameed <b>$"+tgt.toFixed(2)+"</b> → dhammaan hal mar."):" · lakab kasta TP-giisa (T/P broker).")+
         (mTkBENT===1?" Lakab cusub: signal isku jiho + basket-ka faa'iido ku jiro.":" Dhammaan lakabyada signal-ka ayay furmaan."))); }
  { const has=!!(TKCFG && TKCFG.dron!==undefined && TKCFG.dron!==null), sec=$("#tkJSec"), op=$("#tkJOpts"), hh=$("#mTKJHint"), on=swGet("mTKDRON");   // v12.19 JIHADA
    segPaint("mTKDRM","v",mTkDRM);
    if(sec) sec.style.opacity=(TKCFG && !has)?".45":"";
    if(op) op.style.display=on?"":"none";
    const b=tkVal("DRB")||6, k=Math.min(tkVal("DRN")||4,b), st=tkVal("DRS"), w=tkVal("DRW"), nm=["JILICSAN","DHEXE","ADAG"][mTkDRM]||"DHEXE";
    if(hh) hh.innerHTML=(TKCFG && !has)?"Bot-ka chart-ka wuxuu u baahan yahay <b>EA v70.7</b> si JIHADA SUUQA u shaqeyso."
      :(!on?"<b>DAMMAN:</b> tick tracker-ku jiho kasta wuu galaa (pullback-ka sidoo kale).":
        ("<b>"+nm+"</b>: "+(mTkDRM+2)+" ka mid ah 4 calaamadood (shumacyada "+k+"/"+b+" · EMA20/50 · 5 daq · EMA50 M5) iyo xoog ≥ <b>"+(st===null?40:st)+"%</b> → jihada. "
         +"Signal ka soo horjeeda ama RANGE → <b>⛔ la diiday</b>."+((w||0)>0?(" Khasaare kadib jihadaas <b>"+w+" daq</b> ma furmo."):""))); }
  { const has=!!(TKCFG && TKCFG.tron!==undefined && TKCFG.tron!==null), sec=$("#tkTprSec"), op=$("#tkTprOpts"), hh=$("#mTKTRHint"), on=swGet("mTKTRON");   // v12.17 TP RAAC
    if(sec) sec.style.opacity=(TKCFG && !has)?".45":"";
    if(op) op.style.display=on?"":"none";
    const vt=tkVal("VTP")||0, st=tkVal("TRST")||80, lk=tkVal("TRLK")||70, sp=tkVal("TRSTEP")||1, mx=Math.max(1,Math.min(10,Math.round(tkVal("TRMAX")||3)));
    if(hh) hh.innerHTML=(TKCFG && !has)?"Bot-ka chart-ka wuxuu u baahan yahay <b>EA v70.5</b> si TP RAAC u shaqeeyo."
      :(!on?"<b>DAMMAN:</b> TP-gu meeshiisa ayuu joogaa (sidii hore)."
       :(vt<=0?'<span style="color:#f0d9a0">Virtual TP = 0 → TP RAAC ma shaqeeyo (TP ma jiro).</span>'
        :("TP $"+vt.toFixed(2)+" → "+Math.round(st)+"% = <b>$"+(vt*st/100).toFixed(2)+"</b> → TP cusub <b>$"+(vt+sp).toFixed(2)+"</b> · SL <b>+$"+(vt*lk/100).toFixed(2)+"</b> 🔒. Raac "+mx+" → TP ugu fog <b>$"+(vt+sp*mx).toFixed(2)+"</b>."
          +(lk>=st?'<br><span style="color:#f0d9a0">Qufulka waa in uu ka yar yahay Bilowga → bot-ku wuxuu u dhigayaa '+Math.max(10,Math.round(st-10))+'%.</span>':"")))); }
  { const th=$("#mTKTPMHint"), vt=tkVal("VTP")||0, has=!!(TKCFG && TKCFG.tpm!==undefined && TKCFG.tpm!==null), r=$("#rTKTPM");
    if(r) r.style.opacity=(TKCFG && !has)?".45":"";
    if(th) th.innerHTML=(TKCFG && !has)?"Bot-ka chart-ka wuxuu u baahan yahay <b>EA v70.3</b> si TP broker u shaqeeyo."
      :(mTkTPM===1?("<b>BROKER:</b> TP $"+vt.toFixed(2)+" wuxuu galayaa tiirka <b>T/P</b> ee MT5 → broker-ka ayaa ku xidha qiimaha TP-ga, xitaa marka internet-ku go'o. Trailing-ku wuu sii shaqaynayaa (SL-ka ayuu dhaqaajiyaa).")
                   :("<b>VIRTUAL:</b> bot-ka ayaa xidha marka faa'iidadu gaadho $"+vt.toFixed(2)+" · T/P-ga MT5 = 0.00.")); }
  { const sh=$("#mTKSLMHint"), hr=$("#rTKHARD"), hs=$("#mTKHARDs"), v=tkVal("VSL")||2;
    if(sh) sh.innerHTML=mTkSLM===1?("<b>BROKER:</b> SL $"+v.toFixed(2)+" wuxuu galayaa server-ka broker-ka → qiimaha marka la gaadho isla markiiba wuu xidhmaa (slippage yar). BE / trailing → bot-ka ayaa SL-ka broker-ka dhaqaajiya.")
                                   :("<b>VIRTUAL:</b> bot-ka ayaa xidha marka qiimuhu gaadho SL-ka → dhaqdhaqaaq degdeg ah = slippage (demo: −$4.14). SL adag ayaa ilaalin ah.");
    if(hr) hr.style.opacity=mTkSLM===1?".45":""; if(hs) hs.textContent=mTkSLM===1?"BROKER: lama isticmaalo (SL = VSL)":"internet go'a · ugu yaraan VSL + $1"; }
  { const sp=(TICK&&TICK.sp!==undefined)?Number(TICK.sp):0, mx=tkVal("SPM"), mv=tkVal("MV")||0, hh=$("#mTKHint");
    if(hh && sp>0 && mx>0){ hh.dataset.sp="Spread hadda <b>$"+sp.toFixed(2)+"</b> × "+mx+" → dhaqdhaqaaqa loo baahan yahay <b>$"+Math.max(mv,sp*mx).toFixed(2)+"</b>."; } else if(hh) hh.dataset.sp=""; }
  const h=$("#mTKHint"); if(!h) return;
  const vsl=tkVal("VSL")||2.5, vtp=tkVal("VTP"), hard=tkVal("HARD")||6, lot=tkVal("LOT")||0.01, w=Number(($("#mTKW")||{}).textContent)||8, k=Number(($("#mTKK")||{}).textContent)||5;
  const bits=[];
  bits.push("Tick tracker <b>"+w+" / "+k+"</b>: "+k+" ka mid ah "+w+"-da isbeddel ee u dambeeyay hal jiho → gelitaan.");
  if(mTkLM===0) bits.push("Lot <b>"+lot.toFixed(2)+"</b> → Virtual SL ≈ <b>$"+(vsl*lot*100).toFixed(2)+"</b>"+(vtp>0?(" · TP ≈ <b>$"+(vtp*lot*100).toFixed(2)+"</b>"):"")+" · SL adag ≈ $"+(Math.max(hard,vsl+1)*lot*100).toFixed(2)+" (XAUUSD · 100 oz).");
  if(hard<vsl+1) bits.push('<span style="color:#f0d9a0">SL adag waa in uu ≥ VSL + $1 → bot-ku wuxuu u dhigayaa $'+(vsl+1).toFixed(2)+'.</span>');
  if(vtp!==null && vtp>0 && vtp<vsl) bits.push("TP &lt; SL → guusha loo baahan yahay ≥ <b>"+Math.round(100*vsl/(vsl+vtp))+"%</b> (spread ka hor).");
  if(h.dataset.sp) bits.push(h.dataset.sp);   // v12.14
  h.innerHTML=bits.join("<br>");
}
function tkSeed(){
  const c=TKCFG; if(!c){ tkPaint(); return; }
  swSet("mTK",!!c.on); swSet("mTKONLY",!!c.only);
  if($("#mTKW")) $("#mTKW").textContent=c.w; if($("#mTKK")) $("#mTKK").textContent=c.k;
  const m={MV:c.mv,SEC:c.sec,LOT:c.lot,RISK:(c.risk>0?c.risk:0.25),VSL:c.vsl,VTP:c.vtp,BE:c.be,BEL:c.bel,TRS:c.trs,TRD:c.trd,HOLD:c.hold,HARD:c.hard,HS:c.hs,HE:c.he,SPR:c.spr,CD:c.cd,MAXD:c.maxd,ML:c.ml,PAUSE:c.pause,DL:c.dl,DT:c.dt};
  Object.keys(m).forEach(k=>{ const e=$("#mTK"+k); if(e && m[k]!==undefined && m[k]!==null) e.value=m[k]; });
  mTkLM=(Number(c.risk)>0)?1:0;
  mTkTPM=(c.tpm===0)?0:1;   // v12.15
  if(c.bon!==undefined && c.bon!==null){ mTkBON=c.bon?1:0; mTkBENT=(c.bent===0)?0:1; mTkBEX=(c.bex===0)?0:1;   // v12.16
    if($("#mTKBN")) $("#mTKBN").textContent=c.bn||3;
    const st=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; }; st("mTKBTGT",c.btgt); st("mTKBBE",c.bbe); st("mTKBLP",c.blp); }
  if(c.dron!==undefined && c.dron!==null){ swSet("mTKDRON",!!c.dron); mTkDRM=(c.drm>=0&&c.drm<=2)?c.drm:1;   // v12.19 JIHADA
    const sj=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; }; sj("mTKDRB",c.drb); sj("mTKDRN",c.drn); sj("mTKDRS",c.drs); sj("mTKDRW",c.drw); }
  if(c.tron!==undefined && c.tron!==null){ swSet("mTKTRON",!!c.tron);   // v12.17 TP RAAC
    const st=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; }; st("mTKTRST",c.trst); st("mTKTRLK",c.trlk); st("mTKTRSTEP",c.trstep); st("mTKTRMAX",c.trmax); }
  mTkSLM=(c.slm===0)?0:1; swSet("mTKTR",!!c.tr); { const e=$("#mTKSPM"); if(e && c.spm!==undefined) e.value=c.spm; }   // v12.14
  tkPaint();
}
function tickCmds(cmds){
  if(!TKCFG && !mTkTouched) return;   // EA v70.0 weli ma jiro -> waxba ha dirin
  cmds.push("SET:TK="+(swGet("mTK")?1:0)); cmds.push("SET:TKONLY="+(swGet("mTKONLY")?1:0));
  const w=Number(($("#mTKW")||{}).textContent), k=Number(($("#mTKK")||{}).textContent);
  if(w>=3&&w<=50) cmds.push("SET:TKW="+w); if(k>=2&&k<=50) cmds.push("SET:TKK="+Math.min(k,w||k));
  const L={MV:[0,20],SEC:[0,300],LOT:[0.01,100],VSL:[0.1,100],VTP:[0,100],BE:[0,100],BEL:[0,50],TRS:[0,100],TRD:[0.05,50],HOLD:[0,86400],HARD:[0.5,200],
           HS:[0,23],HE:[1,24],SPR:[5,500],CD:[0,3600],MAXD:[1,1000],ML:[0,20],PAUSE:[1,1440],DL:[0,50],DT:[0,100]};
  Object.keys(L).forEach(key=>{ const v=tkVal(key); if(v===null) return; const c=Math.max(L[key][0],Math.min(L[key][1],v)); cmds.push("SET:TK"+key+"="+c); });
  if(mTkLM===1){ const r=tkVal("RISK"); cmds.push("SET:TKRISK="+Math.max(0.01,Math.min(5,(r===null?0.25:r)))); }
  else cmds.push("SET:TKRISK=0");
  if(TKCFG && TKCFG.slm!==undefined){   // v12.14: EA v70.2+ oo keliya
    if(TKCFG.tpm!==undefined && TKCFG.tpm!==null) cmds.push("SET:TKTPM="+mTkTPM);   // v12.15: EA v70.3+
    if(TKCFG.bon!==undefined && TKCFG.bon!==null){   // v12.16: EA v70.4+
      cmds.push("SET:TKBON="+mTkBON); cmds.push("SET:TKBENT="+mTkBENT); cmds.push("SET:TKBEX="+mTkBEX);
      const bn=Number(($("#mTKBN")||{}).textContent); if(bn>=2&&bn<=5) cmds.push("SET:TKBN="+bn);
      const t1=tkVal("BTGT"), t2=tkVal("BBE"), t3=tkVal("BLP");
      if(t1!==null) cmds.push("SET:TKBTGT="+Math.max(0,Math.min(10000,t1))); if(t2!==null) cmds.push("SET:TKBBE="+Math.max(0,Math.min(10000,t2)));
      if(t3!==null) cmds.push("SET:TKBLP="+Math.round(Math.max(0,Math.min(1000000,t3)))); }
    if(TKCFG.dron!==undefined && TKCFG.dron!==null){   // v12.19: EA v70.7+ (JIHADA SUUQA)
      cmds.push("SET:TKDRON="+(swGet("mTKDRON")?1:0)); cmds.push("SET:TKDRM="+mTkDRM);
      const b1=tkVal("DRB"), b2=tkVal("DRN"), b3=tkVal("DRS"), b4=tkVal("DRW");
      const bb=(b1===null)?null:Math.round(Math.max(3,Math.min(20,b1)));
      if(bb!==null) cmds.push("SET:TKDRB="+bb);
      if(b2!==null) cmds.push("SET:TKDRN="+Math.round(Math.max(2,Math.min(bb===null?20:bb,b2))));
      if(b3!==null) cmds.push("SET:TKDRS="+Math.max(0,Math.min(100,b3)));
      if(b4!==null) cmds.push("SET:TKDRW="+Math.round(Math.max(0,Math.min(240,b4)))); }
    if(TKCFG.tron!==undefined && TKCFG.tron!==null){   // v12.17: EA v70.5+ (TP RAAC)
      cmds.push("SET:TKTRON="+(swGet("mTKTRON")?1:0));
      const a1=tkVal("TRST"), a2=tkVal("TRLK"), a3=tkVal("TRSTEP"), a4=tkVal("TRMAX");
      const s1=(a1===null)?null:Math.max(50,Math.min(95,a1));
      if(s1!==null) cmds.push("SET:TKTRST="+s1);
      if(a2!==null){ let l=Math.max(10,Math.min(90,a2)); const ref=(s1!==null)?s1:(Number(TKCFG.trst)||80); if(l>=ref) l=Math.max(10,ref-10); cmds.push("SET:TKTRLK="+l); }
      if(a3!==null) cmds.push("SET:TKTRSTEP="+Math.max(0.1,Math.min(50,a3)));
      if(a4!==null) cmds.push("SET:TKTRMAX="+Math.round(Math.max(1,Math.min(10,a4)))); }
    cmds.push("SET:TKSLM="+mTkSLM); cmds.push("SET:TKTR="+(swGet("mTKTR")?1:0));
    const sm=tkVal("SPM"); if(sm!==null) cmds.push("SET:TKSPM="+Math.max(0,Math.min(10,sm)));
  }
}
{ const e=$("#mTK"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; mTkTouched=true; tkPaint(); }); }
{ const e=$("#mTKONLY"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; mTkTouched=true; }); }
[["mTKBON",v=>{mTkBON=v;}],["mTKBENT",v=>{mTkBENT=v;}],["mTKBEX",v=>{mTkBEX=v;}]].forEach(([id,f])=>document.querySelectorAll("#"+id+" button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; f(Number(b.dataset.v)); mTouched=true; mTkTouched=true; tkPaint(); })));   // v12.16
{ const m=$("#mTKBNm"), p=$("#mTKBNp"); const go=d=>{ const e=$("#mTKBN"); if(!e) return; e.textContent=Math.max(2,Math.min(5,(Number(e.textContent)||3)+d)); mTouched=true; mTkTouched=true; tkPaint(); };
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) go(-1); }); if(p) p.addEventListener("click",()=>{ if(!p.disabled) go(1); }); }
document.querySelectorAll("#mTKTPM button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mTkTPM=Number(b.dataset.v); mTouched=true; mTkTouched=true; tkPaint(); }));   // v12.15
document.querySelectorAll("#mTKSLM button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mTkSLM=Number(b.dataset.v); mTouched=true; mTkTouched=true; tkPaint(); }));   // v12.14
{ const e=$("#mTKTR"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; mTkTouched=true; }); }
{ const e=$("#mTKDRON"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; mTkTouched=true; tkPaint(); }); }   // v12.19
document.querySelectorAll("#mTKDRM button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mTkDRM=Number(b.dataset.v); mTouched=true; mTkTouched=true; tkPaint(); }));
{ const e=$("#mTKTRON"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; mTkTouched=true; tkPaint(); }); }   // v12.17
document.querySelectorAll("#mTKLM button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mTkLM=Number(b.dataset.v); mTouched=true; mTkTouched=true; tkPaint(); }));
TK_IN.forEach(k=>{ const e=$("#mTK"+k); if(e) e.addEventListener("input",()=>{ mTouched=true; mTkTouched=true; tkPaint(); }); });
[["mTKW",3,50],["mTKK",2,50]].forEach(([id,lo,hi])=>{
  const m=$("#"+id+"m"), p=$("#"+id+"p");
  const go=d=>{ const e=$("#"+id); if(!e) return; let v=Number(e.textContent)||lo; v=Math.max(lo,Math.min(hi,v+d)); e.textContent=v;
    const W=Number(($("#mTKW")||{}).textContent)||8, K=$("#mTKK"); if(K && Number(K.textContent)>W) K.textContent=W; mTouched=true; mTkTouched=true; tkPaint(); };
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) go(-1); });
  if(p) p.addEventListener("click",()=>{ if(!p.disabled) go(1); });
});


/* ================= v12.18: ⚙️ INPUT (dhammaan input-yada EA v70.6) · Maamul xeeladaha ================= */
let INPS=null, INPV=null, INPBY={}, inEd={}, inDel={}, inSentM={}, inLoading=false, inCat="TICK";
try{ inCat=localStorage.getItem("mp_incat")||"TICK"; }catch(e){}
const IN_ICON=[["0 ·","🧪"],["0c","📐"],["0a","🪜"],["0b","🎯"],["1 ·","🔒"],["2 ·","🧭"],["3 ·","🔢"],["4 ·","💰"],["5 ·","🛑"],["6 ·","🎯"],["7 ·","🛡"],["8 ·","✂️"],["9 ·","⏏"],
  ["10 ·","🌡"],["11 ·","📈"],["12 ·","➡️"],["13 ·","🧰"],["14 ·","🕐"],["15 ·","📰"],["16 ·","🧠"],["19 ·","🏦"],["19b","⭐"],["21b","📦"],["22 ·","⚙️"],["23 ·","🏢"],["24 ·","📨"],["25 ·","☁️"],["26 ·","🎨"],["27 ·","🪵"]];
function inIcon(g){ let ic="•"; IN_ICON.forEach(([p,i])=>{ if(g.startsWith(p)) ic=i; }); if(/TICK/.test(g)) ic="⚡"; else if(/GOLD/.test(g)) ic="🥇"; else if(/ASIA/.test(g)) ic="🌅"; return ic; }
function inGName(g){ return g.replace(/\s*\((?:v|b)\d[^)]*\)\s*/g," ").replace(/\s+/g," ").trim(); }
function inHex(t){ return "h"+Array.from(new TextEncoder().encode(String(t))).map(b=>b.toString(16).padStart(2,"0")).join(""); }
function inColHex(v){ v=Number(v)||0; const r=v&255,g=(v>>8)&255,b=(v>>16)&255; return "#"+[r,g,b].map(x=>x.toString(16).padStart(2,"0")).join(""); }
function inColInt(h){ const m=/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(h||""); if(!m) return 0; return parseInt(m[1],16)+parseInt(m[2],16)*256+parseInt(m[3],16)*65536; }
function inEq(t,a,b){ if(a===undefined||b===undefined||a===null||b===null) return a===b; if(t==="s") return String(a)===String(b); return Math.abs(Number(a)-Number(b))<1e-9; }
function inFmt(it,v){ if(v===undefined||v===null) return "—"; if(it.t==="b") return Number(v)?"ON":"OFF"; if(it.t==="e"){ const o=(it.o||[]).find(o=>o[0]===Number(v)); return o?o[1]:String(v); } if(it.t==="c") return inColHex(v); if(it.t==="s") return v===""?"(madhan)":('"'+String(v).slice(0,24)+(String(v).length>24?"…":"")+'"'); return String(v); }
function inOk(){ return !!(INPS && INPV && INPV.h===INPS.h && Array.isArray(INPV.v)); }
function inCur(it){ return inOk()?INPV.v[it.x]:undefined; }
function inBase(it){ return inOk()?INPV.b[it.x]:undefined; }
function inShown(it){ if(it.n in inEd) return inEd[it.n]; if(inDel[it.n]) return inBase(it); if(it.n in inSentM) return inSentM[it.n].v; return inCur(it); }
async function inpLoad(){
  if(INPS||inLoading||!$("#pInput")) return; inLoading=true;
  try{ const r=await fetch("/api/inp_schema"); if(r.ok){ INPS=await r.json(); INPBY={}; INPS.items.forEach(it=>{ INPBY[it.n]=it; }); inpBuild(); inpPaint(); } }catch(e){}
  inLoading=false;
}
function inCatSet(c){
  inCat=c; try{ localStorage.setItem("mp_incat",c); }catch(e){}
  document.querySelectorAll("#pInput .incat").forEach(x=>x.classList.toggle("on",x.dataset.c===c));
  document.querySelectorAll("#inCats button").forEach(b=>b.classList.toggle("on",b.dataset.c===c));
}
function inRowHTML(it){
  const lab=esc(it.l||it.n), nm='<span class="nmx">'+esc(it.n)+'</span>';
  if(it.k===2) return '<div class="frow inrow inlk" data-n="'+esc(it.n)+'"><span class="l">'+lab+'<small>'+nm+' · MT5 oo keliya (ammaan)</small></span><span class="ctl"><span class="inlkv">🔒 MT5</span></span></div>';
  let ctl="";
  if(it.t==="b") ctl='<button class="sw" type="button" data-in="1" aria-label="'+esc(it.l||it.n)+'"><i></i></button>';
  else if(it.t==="i") ctl='<input type="number" step="1" inputmode="numeric" data-in="1">';
  else if(it.t==="d") ctl='<input type="number" step="any" inputmode="decimal" data-in="1">';
  else if(it.t==="e") ctl='<select data-in="1">'+(it.o||[]).map(o=>'<option value="'+o[0]+'">'+esc(o[1])+'</option>').join("")+'</select>';
  else if(it.t==="c") ctl='<input type="color" data-in="1">';
  else ctl='<input type="text" maxlength="200" data-in="1">';
  const bd=it.r?'<span class="inbdg" title="EA-ga dib u kicin kadib ayuu shaqeeyaa">↻ MT5</span>':'';
  return '<div class="frow inrow" data-n="'+esc(it.n)+'"><span class="l">'+lab+bd+'<small>'+nm+' · <span class="inb">MT5: —</span></small></span><span class="ctl">'+ctl+'<button class="inrst" type="button" title="MT5-ka ku celi" aria-label="MT5-ka ku celi">↺</button></span></div>';
}
function inpBuild(){
  const G=INPS.groups, by={};
  INPS.items.forEach(it=>{ if(it.k===1) return; (by[it.g]=by[it.g]||[]).push(it); });
  const cnt={}; INPS.items.forEach(it=>{ const c=G[it.g].cat; cnt[c]=(cnt[c]||0)+1; });
  document.querySelectorAll("#inCats button").forEach(b=>{ const x=b.querySelector("b"); if(x) x.textContent=cnt[b.dataset.c]||""; });
  let open={}; try{ open=JSON.parse(localStorage.getItem("mp_inacc")||"{}")||{}; }catch(e){}
  document.querySelectorAll("#pInput .ingen").forEach(box=>{
    const c=box.dataset.c; let h="";
    G.forEach((g,gi)=>{
      if(g.cat!==c || !by[gi]) return;
      const its=by[gi], mod=(c==="TICK"||c==="GOLD"||c==="ASIA"||c==="GRID");
      const nm=mod?("Sitinka kale · "+inGName(g.name)):inGName(g.name);
      const sub=its.length+" input"+(its.some(i=>i.k===2)?" · 🔒 MT5":"")+(its.some(i=>i.r)?" · ↻ qaar":"");
      h+='<div class="inacc'+(open[gi]?" open":"")+'" data-g="'+gi+'"><button type="button" class="inah"><span class="ic">'+inIcon(g.name)+'</span><span class="nm">'+esc(nm)+'<small>'+esc(sub)+'</small></span><span class="indot"></span><span class="inct">'+its.length+'</span></button>'
        +'<div class="inab">'+its.map(inRowHTML).join("")+'</div></div>';
    });
    box.innerHTML=h;
  });
}
function inRowPaint(row){
  const it=INPBY[row.dataset.n]; if(!it || it.k!==0) return;
  const ok=inOk(), cur=inCur(it), base=inBase(it), v=inShown(it);
  row.classList.toggle("inoff",!ok);
  const pend=(it.n in inEd)||!!inDel[it.n]||(it.n in inSentM);
  row.classList.toggle("pend",pend);
  row.classList.toggle("chg",ok && !pend && !inEq(it.t,cur,base));
  const bs=row.querySelector(".inb"); if(bs) bs.textContent="MT5: "+inFmt(it,base)+((it.n in inSentM)?" · ⏳ EA-ga ayaa la dirayaa":"");
  const c=row.querySelector("[data-in]"); if(!c || (document.activeElement===c && it.t!=="b")) return;
  if(it.t==="b") c.classList.toggle("on",!!Number(v));
  else if(it.t==="c") c.value=inColHex(v);
  else if(v===undefined||v===null) c.value="";
  else c.value=String(v);
}
function inpPaint(){
  if(!INPS) return;
  const now=Date.now();
  Object.keys(inSentM).forEach(n=>{ const it=INPBY[n], s=inSentM[n]; if(!it) return delete inSentM[n];
    if(inOk() && inEq(it.t,inCur(it),s.v)) delete inSentM[n]; else if(now-s.t>90000) delete inSentM[n]; });
  document.querySelectorAll("#pInput .inrow").forEach(inRowPaint);
  const dirty=new Set();
  document.querySelectorAll("#pInput .inacc").forEach(a=>{ const ch=!!a.querySelector(".inrow.chg,.inrow.pend"); a.classList.toggle("chg",ch); if(ch) dirty.add(INPS.groups[a.dataset.g].cat); });
  document.querySelectorAll("#inCats button").forEach(b=>b.classList.toggle("dt",dirty.has(b.dataset.c)));
  const sy=$("#inSync"); if(sy){
    if(!INPV){ sy.className="insync warn"; sy.innerHTML="EA v70.6 weli qiimaha input-yada ma soo dirin — chart-ka <b>MOHA_PRO_V70_6</b> ku dhaji (5 daqiiqo gudahood ayay soo muuqdaan)."; }
    else if(INPV.h!==INPS.h){ sy.className="insync warn"; sy.innerHTML="⚠️ EA-ga chart-ka (#"+esc(INPV.h||"?")+") iyo app-ka (#"+esc(INPS.h)+") isku version ma aha → <b>EA v"+esc(INPS.ver)+"</b> rakib. Qaybaha gaarka ah (TICK · GOLD · ASIA · MAIN) way shaqeeyaan."; }
    else { let n=0; INPS.items.forEach(it=>{ if(it.k===0 && !inEq(it.t,INPV.v[it.x],INPV.b[it.x])) n++; });
      const ag=Number(INPV.age)||0, w=ag<90?(ag+"s"):(ag<5400?(Math.round(ag/60)+" daq"):(Math.round(ag/3600)+" saac"));
      sy.className="insync"; sy.innerHTML="✓ MT5 ⇄ App · "+esc(INPV.chart||"")+" · EA v"+esc(INPV.ver||"")+" · "+w+" ka hor · <b>● "+n+"</b> way ka duwan yihiin MT5"; }
  }
  inCount();
}
function inCount(){
  const n=Object.keys(inEd).length+Object.keys(inDel).length, c=$("#inCnt");
  if(c){ c.hidden=!n; c.innerHTML=n?("<b>"+n+"</b> input ayaa sugaya → <b>KAYDI &amp; DIR</b>"):""; }
}
function inSetEdit(n,val){
  const it=INPBY[n]; if(!it) return;
  delete inDel[n]; delete inSentM[n];
  if(inOk() && inEq(it.t,val,inCur(it))) delete inEd[n]; else inEd[n]=val;
  mTouched=true; const row=document.querySelector('#pInput .inrow[data-n="'+n+'"]'); if(row) inRowPaint(row); inpPaint();
}
function inpCollect(values){
  if(!INPS) return [];
  Object.keys(inEd).forEach(n=>{ const it=INPBY[n]; if(!it) return; values["IN:"+n]=(it.t==="s")?inHex(inEd[n]):String(inEd[n]); });
  return Object.keys(inDel).map(n=>"IN:"+n);
}
function inpSent(){
  const t=Date.now();
  Object.keys(inEd).forEach(n=>{ inSentM[n]={v:inEd[n],t:t}; });
  Object.keys(inDel).forEach(n=>{ const it=INPBY[n]; if(it) inSentM[n]={v:inBase(it),t:t}; });
  inEd={}; inDel={}; if(INPS) inpPaint();
}
function inSearch(q){
  q=String(q||"").trim().toLowerCase(); const P=$("#pInput"); if(!P) return;
  P.classList.toggle("srch",!!q);
  P.querySelectorAll(".inrow,.incard .frow").forEach(r=>{ const hit=!q || (r.textContent+" "+(r.dataset.n||"")).toLowerCase().includes(q); r.classList.toggle("inhid",!hit); });
  P.querySelectorAll(".inacc").forEach(a=>{ const any=!!a.querySelector(".inrow:not(.inhid)"); a.classList.toggle("inhid",!!q && !any); if(q) a.classList.toggle("open",any); });
  P.querySelectorAll(".incard").forEach(c=>{ c.classList.toggle("inhid",!!q && !c.querySelector(".frow:not(.inhid)")); });
  P.querySelectorAll(".incat").forEach(c=>{ c.classList.toggle("inhid",!!q && !c.querySelector(".frow:not(.inhid)")); });
}
function inFold(){
  let open={}; try{ open=JSON.parse(localStorage.getItem("mp_fold")||"{}")||{}; }catch(e){}
  const save=()=>{ try{ localStorage.setItem("mp_fold",JSON.stringify(open)); }catch(e){} };
  document.querySelectorAll("#pInput .bskg .bsub").forEach(h=>{
    const key=(h.closest(".bskg").id||"g")+":"+h.textContent.trim().slice(0,22);
    const sec=document.createElement("div"); sec.className="bsec";
    let n=h.nextElementSibling;
    while(n && !n.classList.contains("bsub") && !n.querySelector(":scope > .bsub")){ const nx=n.nextElementSibling; sec.appendChild(n); n=nx; }
    h.after(sec); h.classList.add("fold"); if(!open[key]) h.classList.add("shut");
    h.addEventListener("click",e=>{ if(e.target.closest("button")) return; h.classList.toggle("shut"); open[key]=!h.classList.contains("shut"); save(); });
  });
  document.querySelectorAll("#pInput .inmain .grp").forEach(g=>{
    const h=g.querySelector(":scope > .gh"); if(!h) return; const key="m:"+h.textContent.trim().slice(0,22);
    if(!open[key]) g.classList.add("shut");
    h.addEventListener("click",()=>{ g.classList.toggle("shut"); open[key]=!g.classList.contains("shut"); save(); });
  });
}
function inExpandAll(){ document.querySelectorAll("#pInput .bsub.fold").forEach(h=>h.classList.remove("shut")); document.querySelectorAll("#pInput .inmain .grp").forEach(g=>g.classList.remove("shut")); document.querySelectorAll("#pInput .inacc").forEach(a=>a.classList.add("open")); }
(function(){
  const P=$("#pInput"); if(!P) return;
  inFold(); inCatSet(inCat);
  document.querySelectorAll("#inCats button").forEach(b=>b.addEventListener("click",()=>{ const q=$("#inQ"); if(q && q.value){ q.value=""; inSearch(""); } inCatSet(b.dataset.c); }));
  const q=$("#inQ"); if(q) q.addEventListener("input",()=>inSearch(q.value));
  P.addEventListener("click",e=>{
    const ah=e.target.closest(".inah");
    if(ah){ const a=ah.parentElement; a.classList.toggle("open"); let o={}; try{ o=JSON.parse(localStorage.getItem("mp_inacc")||"{}")||{}; }catch(_){}
      o[a.dataset.g]=a.classList.contains("open"); try{ localStorage.setItem("mp_inacc",JSON.stringify(o)); }catch(_){} return; }
    const row=e.target.closest(".inrow"); if(!row || row.classList.contains("inoff")) return;
    const n=row.dataset.n, it=INPBY[n]; if(!it || it.k!==0) return;
    if(e.target.closest(".inrst")){ delete inEd[n]; delete inSentM[n]; if(inOk() && !inEq(it.t,inCur(it),inBase(it))) inDel[n]=1; else delete inDel[n]; mTouched=true; inRowPaint(row); inpPaint(); return; }
    const sw=e.target.closest(".sw[data-in]"); if(sw){ inSetEdit(n, Number(inShown(it))?0:1); }
  });
  P.addEventListener("change",e=>{
    const c=e.target.closest("[data-in]"); if(!c || c.classList.contains("sw")) return;
    const row=c.closest(".inrow"), it=row && INPBY[row.dataset.n]; if(!it) return;
    let v=c.value;
    if(it.t==="i"){ if(v===""||!isFinite(Number(v))) return inRowPaint(row); v=String(Math.round(Number(v))); }
    else if(it.t==="d"){ if(v===""||!isFinite(Number(v))) return inRowPaint(row); v=String(Number(v)); }
    else if(it.t==="e") v=String(Number(v));
    else if(it.t==="c") v=String(inColInt(v));
    inSetEdit(it.n,v);
  });
  /* Maamul: kaadhadhka xeeladaha -> badhamada dhabta ah (Input) */
  document.querySelectorAll("#stCard .stsw").forEach(b=>b.addEventListener("click",()=>{ const t=$("#"+b.dataset.sw); if(t && !t.disabled){ t.click(); stSync(); } }));
  document.querySelectorAll("#stCard [data-go]").forEach(b=>b.addEventListener("click",()=>{ tab("Input"); inCatSet(b.dataset.go); }));
  /* Account · furaha · muuqaalka -> Maamul (laab) */
  const acc=$("#meta") && $("#meta").closest(".card"), fa=$("#fdAccB"); if(acc && fa){ fa.appendChild(acc); const k=$("#myKey"); if(k) fa.appendChild(k); }
  const mx=$("#mxCard"), fm=$("#fdMxB"); if(mx && fm) fm.appendChild(mx);
})();
function stSync(){
  document.querySelectorAll("#stCard .stsw").forEach(b=>{ const t=$("#"+b.dataset.sw); if(!t) return; b.classList.toggle("on",t.classList.contains("on")); b.disabled=!!t.disabled; b.closest(".stt").classList.toggle("off",!t.classList.contains("on")); });
}
function stPaint(d){
  if(!$("#stCard")) return;
  stSync();
  const pl=(id,v)=>{ const e=$("#"+id); if(!e) return; if(v===null||v===undefined){ e.textContent="—"; e.className="p"; return; } const n=Number(v)||0; e.textContent=(n>=0?"+$":"-$")+Math.abs(n).toFixed(2); e.className="p "+(n>0?"g":(n<0?"r":"")); };
  const pill=(id,t,c)=>{ const e=$("#"+id); if(e){ e.textContent=t; e.className="stp2 "+(c||""); } };
  const T=d.tick; if(T){ const M={OFF:["DAMMAN",""],SEARCH:["RAADINAYA","run"],PAUSE:["SUGAYA","wt"],OPEN:[(T.bkt?"BASKET SOCDA":"TRADE SOCDA"),"run"]}, m=M[T.st]||["—",""];
    pill("stTkS",m[0],m[1]); const D=T.day||{}, cf=T.cfg||{}; $("#stTkN").textContent=(D.n||0)+"/"+(cf.maxd||"—"); pl("stTkP",D.pl); } else { pill("stTkS","XOG MA JIRO",""); $("#stTkN").textContent=""; pl("stTkP",null); }
  const B=d.bsk; if(B){ pill("stGdS",B.act?"BASKET SOCDA":(B.on?"SUGAYA SIGNAL":"DAMMAN"),B.act?"run":(B.on?"wt":"")); pl("stGdP",(B.day||{}).pl); } else { pill("stGdS","XOG MA JIRO",""); pl("stGdP",null); }
  const A=d.asia; if(A){ const M={OFF:["DAMMAN",""],RANGE:["RANGE","wt"],WAIT:["SUGAYA JEBIN","wt"],OPEN:["TRADE SOCDA","run"],DONE:["MAANTA LA QAATAY","run"],SKIP:["MAANTA MA JIRO",""]}, m=M[A.st]||[String(A.st||"—"),""];
    pill("stAsS",m[0],m[1]); const h=(A.hist||[])[0]; pl("stAsP",(h && Number(h.d)===Number(A.day))?h.pl:null); } else { pill("stAsS","XOG MA JIRO",""); pl("stAsP",null); }
  const st=(d.data||{}).settings||{}, SN={SR:"SR / SD",SMC:"SMC",BOTH:"LABADA"};
  const mn=$("#stMnN"); if(mn) mn.textContent=SN[st.strat]||"—";
  const tko=!!(T && T.cfg && T.cfg.on && T.cfg.only);
  pill("stMnS",tko?"TICK OO KELIYA":(d.online?"SHAQAYNAYA":"OFFLINE"),tko?"wt":(d.online?"run":""));
  pl("stMnP",(d.data||{}).profit);
}
/* ---- v12.12: ⚡ TICK SCALPER (Guud · live) - panel-ka chart-ka oo kale ---- */
const TK_STG=["VIRTUAL SL","BREAK-EVEN","TRAILING","TP RAAC"];   // v12.17
function tkMoney(v){ const n=Number(v)||0; return (n>=0?"+$":"-$")+Math.abs(n).toFixed(2); }
const TK_X={1:"VIRTUAL TP",2:"VIRTUAL SL",3:"WAQTI",6:"BREAK-EVEN",8:"TRAILING",9:"SL ADAG / GACAN",12:"TP RAAC"};   // v12.17
function tkHM(t){ if(!t) return "—"; const d=new Date(t*1000); return String(d.getUTCHours()).padStart(2,"0")+":"+String(d.getUTCMinutes()).padStart(2,"0"); }
function paintTick(a){
  const c=$("#tkCard"); if(!c) return;
  if(!a){ c.hidden=true; return; }
  c.hidden=false; TICK=a;
  const cf=a.cfg||{}, D=a.day||{}, T=a.tot||{}, dg=Number(a.dg)||2, tt=a.tt||{};
  const ST={OFF:["DAMMAN","off"],SEARCH:["RAADINAYA","run"],PAUSE:["SUGAYA","wait"],OPEN:[(a.bkt?"BASKET SOCDA":"TRADE SOCDA"),"run"]};
  const s=ST[a.st]||["—",""];
  $("#tkPill").className="tkpill "+s[1]; $("#tkState").textContent=s[0];
  $("#tkSub").textContent=(a.sym||"XAUUSD")+(a.bkt?(" · BASKET "+((a.bkt.lay||[]).length)+" / "+(cf.bn||"—")):(cf.bon?(" · BASKET "+(cf.bn||"")):(cf.only?" · TICK OO KELIYA":"")));
  //--- sababta
  const wy=$("#tkWhy");
  if(!a.on && a.st!=="OPEN"){ wy.hidden=false; wy.textContent="⏻ TICK SCALPER waa damman — Maamul → Xeeladaha → ⚡ TICK (ama ⚙️ Input → TICK · badhanka ⚡ TICK ee chart-ka)."; }
  else if(a.st==="PAUSE"){ const w=String(a.why||"sug"); wy.hidden=false; wy.textContent=(/war|news/i.test(w)?"📰 ":(/spread/i.test(w)?"↔ ":(/saac|weekend|Jimce/i.test(w)?"🕐 ":"⏸ ")))+w+" — trade cusub ma furmo"; }
  else wy.hidden=true;
  //--- trade furan
  const op=$("#tkOpen");
  //--- v12.16: TICK BASKET card
  const bc=$("#tkBsk"), B=a.bkt;
  if(a.st==="OPEN" && B){
    bc.hidden=false;
    const L=B.lay||[], N=Number(cf.bn)||L.length, lot=L.reduce((x,l)=>x+(Number(l.lot)||0),0), pl=Number(B.pl)||0, buy=(B.dir!=="SELL");
    $("#tkBkH").textContent=(B.dir||"")+" BASKET · "+L.length+" lakab · "+lot.toFixed(2)+" lot";
    const pe=$("#tkBkPL"); pe.textContent=tkMoney(pl); pe.className=pl>=0?"pos":"neg";
    const age=(a.srv&&B.t0)?Math.max(0,a.srv-B.t0):0, ag=s=>(Math.floor(s/60)+"m "+String(s%60).padStart(2,"0")+"s");
    $("#tkBkS").textContent=(age>0&&age<86400?(ag(age)+" · "):"")+"peak "+tkMoney(B.pk)+(cf.bent===1&&L.length<N?(" · lakab cusub: signal "+(buy?"▲":"▼")+" + basket faa'iido"):"");
    const tgt=Number(cf.btgt)||0, tgW=$("#tkBkTg");
    if(cf.bex===1 && tgt>0){ tgW.hidden=false; $("#tkBkTgI").style.width=Math.max(0,Math.min(100,pl/tgt*100))+"%";
      $("#tkBkTgL").innerHTML="wadar <b>"+tkMoney(pl)+"</b>"; $("#tkBkTgR").innerHTML='bartilmaameed <b class="y">$'+tgt.toFixed(2)+'</b> → xidh dhammaan'; }
    else tgW.hidden=true;
    let rows=""; for(let k=0;k<Math.max(N,L.length);k++){ const l=L[k];
      if(l){ const p=Number(l.pl)||0, s2=(a.srv&&l.t)?Math.max(0,a.srv-l.t):0;
        rows+='<div class="tkly"><span class="n">#'+(k+1)+'</span><span>'+Number(l.e).toFixed(dg)+'</span><span class="w">'+Number(l.lot).toFixed(2)+(s2>0&&s2<86400?(' · '+ag(s2)):'')+((Number(l.rc)||0)>0?(' · <span class="tkrc">raac '+Number(l.rc)+'</span>'):'')+'</span><b class="'+(p>=0?"g":"r")+'">'+tkMoney(p)+'</b></div>'; }
      else rows+='<div class="tkly wait"><span class="n">#'+(k+1)+'</span><span>—</span><span class="w">'+(cf.bent===1?("sugaya signal "+(buy?"▲":"▼")):"—")+'</span><b>—</b></div>'; }
    $("#tkBkL").innerHTML=rows;
    const sl=Number(B.sl)||0, risk=B.be?0:L.reduce((x,l)=>x+(Number(l.lot)||0)*100*Math.abs((Number(l.e)||0)-sl),0);
    $("#tkBkSl").innerHTML='<span>SL wadaag <b class="y">'+sl.toFixed(dg)+'</b> <span class="tklock">🔒 BROKER</span></span><span>'+(B.be?'BE ✓':('BE @ +$'+Number(cf.bbe||0).toFixed(2)))+'</span><span>khatar hadda <b class="'+(risk<=0.005?"g":"")+'">$'+risk.toFixed(2)+'</b></span>';
    const nm=["① SL WADAAG","② BREAK-EVEN",(cf.bex===1?("③ $"+tgt.toFixed(2)+" → XIDH"):"③ TP KASTA")], stg=B.be?1:0;
    $("#tkBkSt").innerHTML=nm.map((t,i)=>'<span class="'+(i<stg?"done":(i===stg?"now":""))+'">'+t+'</span>').join("");
  } else bc.hidden=true;
  { const bd=$("#tkBkD"), K=a.bk;   // basket-yada maanta
    if(K && (cf.bon || K.n>0)){ bd.hidden=false; $("#tkBkDN").textContent=K.n; const h=$("#tkBkDH"); h.textContent=K.hit+" / "+K.n; h.className=K.hit>0?"g":"";
      const p=$("#tkBkDP"); p.textContent=tkMoney(K.pl); p.className=(Number(K.pl)||0)>0?"g":((Number(K.pl)||0)<0?"r":""); }
    else bd.hidden=true; }
  if(a.st==="OPEN" && !B){
    op.hidden=false;
    const buy=(a.dir!=="SELL"), e=Number(a.e)||0, vs=Number(a.vs)||0, vsl=Number(cf.vsl)||2.5, vtp=Number(cf.vtp)||0, fav=Number(a.fav)||0, pl=Number(a.pl)||0, stg=Number(a.stg)||0;
    $("#tkOT").textContent=(a.dir||"BUY")+" "+Number(a.lot||0).toFixed(2)+" · @ "+e.toFixed(dg);
    const o=$("#tkOPL"); o.textContent=tkMoney(pl); o.className=pl>=0?"pos":"neg";
    const age=(a.srv&&a.t0)?Math.max(0,a.srv-a.t0):0;
    const rc=Number(a.rc)||0, tpd=(vtp>0 && Number(a.tpd)>0)?Number(a.tpd):vtp, trm=Number(cf.trmax)||3, tron=(cf.tron===1 && vtp>0);   // v12.17 TP RAAC
    const ag2=s=>(s>=60?(Math.floor(s/60)+"m "+String(s%60).padStart(2,"0")+"s"):(s+"s"));
    $("#tkTrs").innerHTML=(age>0&&age<86400?(ag2(age)+" · "):"")+"peak +$"+fav.toFixed(2)+" · "
      +(tron&&(rc>0||stg===3)?('<b class="tkrc">TP RAAC '+rc+' / '+trm+'</b>')
        :((cf.trs>0?(stg>=2?"trailing wuu socdaa":("trailing wuxuu bilaabmayaa +$"+Number(cf.trs).toFixed(2))):"trailing off")
          +(tron?(' · TP RAAC @ +$'+(tpd*(Number(cf.trst)||80)/100).toFixed(2)):'')));
    const sl0=buy?e-vsl:e+vsl, top=vtp>0?tpd:Math.max(fav,vsl/2), tp=buy?e+top:e-top;
    const X=v=>Math.max(0,Math.min(100,(buy?(v-sl0):(sl0-v))/(vsl+top)*100));
    const xe=X(e), xv=X(vs), xp=X(buy?e+fav:e-fav);
    $("#tkBar").innerHTML='<div class="sg" style="left:0;width:'+xe+'%;background:linear-gradient(90deg,#7a2a2a,#4a1c1e)"></div>'
      +(xp>xe?'<div class="sg" style="left:'+xe+'%;width:'+(xp-xe)+'%;background:linear-gradient(90deg,#1f8a4c,#22c55e)"></div>':'')
      +'<div class="mk" style="left:'+xe+'%;background:#e8ece8"></div><div class="mk" style="left:'+xv+'%;background:'+(stg>0?'#f0cf86':'#ef4444')+'"></div>'
      +(vtp>0?'<div class="mk" style="left:100%;background:#7fe0ab"></div>':'');
    const brk=(cf.slm===1 && Number(a.bsl)>0 && Math.abs(Number(a.bsl)-vs)<Math.pow(10,-dg)*0.6);   // v12.14
    const btp=Number(a.btp)>0, tpShow=btp?Number(a.btp):tp;   // v12.15: TP broker
    const slP=buy?(vs-e):(e-vs), slTxt=(stg>0&&slP>0.004)?('+$'+slP.toFixed(2)):vs.toFixed(dg), rcT=rc>0?(' RAAC '+rc):'';   // v12.17: SL qufulan -> +$ faa'iido
    $("#tkLg").innerHTML='<span>SL asal '+sl0.toFixed(dg)+'</span><span class="y">'+(cf.slm===1?"SL":"VSL")+' → '+slTxt+(brk?('<span class="tklock">🔒'+(btp?'':' BROKER')+'</span>'):'')+'</span>'+(vtp>0?(btp?('<span style="color:#7fe0ab">TP '+tpShow.toFixed(dg)+'<span class="tklock">🔒 '+(rc>0?rcT.trim():'BROKER')+'</span></span>'):('<span'+(rc>0?' style="color:#7fe0ab"':'')+'>VTP '+tp.toFixed(dg)+(rc>0?('<span class="tklock">'+rcT.trim()+'</span>'):'')+'</span>')):'<span>VTP off</span>');
    const nm=[(cf.slm===1?"① STOP LOSS":"① VIRTUAL SL"),"② BREAK-EVEN",(stg===3?"③ TP RAAC":"③ TRAILING")], si=Math.min(stg,2);
    $("#tkSteps").innerHTML=nm.map((t,i)=>'<span class="'+(i<si?"done":(i===si?"now":""))+'">'+t+'</span>').join("");
  } else op.hidden=true;
  //--- tick tracker
  const W=Number(cf.w)||8, K=Number(cf.k)||5, sq=String(a.seq||"");
  $("#tkTTh").textContent="TICK TRACKER · "+W+" / "+K;
  let dots=""; for(let i=0;i<W;i++){ const ch=sq.length===W?sq[i]:"N"; dots+='<span class="'+(ch==="U"?"u":(ch==="D"?"d":"n"))+'">'+(ch==="U"?"▲":(ch==="D"?"▼":"·"))+'</span>'; }
  $("#tkDots").innerHTML=dots;
  $("#tkTTn").innerHTML='<b class="g">'+(tt.up||0)+'</b> kor · <b class="r">'+(tt.dn||0)+'</b> hoos<br>loo baahan '+K;
  const mv=Math.abs(Number(tt.mv)||0), need=Number(tt.need)||Number(cf.mv)||0;   // v12.14: xadka + filter spread
  $("#tkMvI").style.width=(need>0?Math.min(100,mv/need*100):0)+"%";
  $("#tkMvL").innerHTML='dhaqdhaqaaq <b>$'+mv.toFixed(2)+'</b>'+((Number(tt.mv)||0)<0?' ▼':((Number(tt.mv)||0)>0?' ▲':''));
  $("#tkMvR").textContent="xadka $"+need.toFixed(2);
  //--- v12.19 (EA v70.7): 🧭 JIHADA SUUQA
  { const J=a.jh, box=$("#tkJh");
    if(!J || !J.on){ box.hidden=true; }
    else {
      box.hidden=false;
      const d=Number(J.d)||0, sc=Number(J.s)||0, el=$("#tkJd");
      el.className="tkjd "+(d<0?"dn":(d>0?"up":""));
      $("#tkJdB").textContent=d<0?"▼ HOOS · SELL OO KELIYA":(d>0?"▲ KOR · BUY OO KELIYA":"↔ RANGE · HA GALIN");
      const held=(a.srv && J.t0)?Math.max(0,a.srv-J.t0):0, hm=held>=3600?(Math.floor(held/3600)+" saac"):(Math.floor(held/60)+" daq");
      $("#tkJdS").textContent="xoog "+Math.round(Math.abs(sc))+"%"+(held>0&&held<86400*3?(" · "+hm):"")+(d<0?" · BUY waa xidhan yahay":(d>0?" · SELL waa xidhan yahay":" · labadaba way xidhan yihiin"));
      $("#tkJmI").style.left=Math.max(0,Math.min(100,(sc+100)/2))+"%";
      const v=J.v||[0,0,0,0], cl=x=>x<0?"dn":(x>0?"up":"nt"), mvv=Number(J.mv)||0;
      const row=(t,val,x)=>'<div><span>'+t+'</span><b class="'+cl(x)+'">'+val+'</b></div>';
      $("#tkJs").innerHTML=row("🕯 Shumacyada M1 ("+(J.nb||"—")+")",(J.cd||0)+" ▼ · "+(J.cu||0)+" ▲",v[0])
        +row("📉 EMA20 / EMA50 (M1)",v[1]<0?"hoos ↘":(v[1]>0?"kor ↗":"—"),v[1])
        +row("⏱ Dhaqdhaqaaqa 5 daq",(mvv>=0?"+$":"−$")+Math.abs(mvv).toFixed(2),v[2])
        +row("🧱 EMA50 M5 (trend weyn)",v[3]<0?"hoos":(v[3]>0?"kor":"—"),v[3]);
      const bl=$("#tkJb"), bd=Number(J.bd)||0;
      if(bd!==0 && a.srv && J.bt>a.srv){ bl.hidden=false; bl.innerHTML="⏳ <b>"+(bd>0?"BUY":"SELL")+"</b> waa la xannibay ilaa "+esc(tkHM(J.bt))+" — khasaare kadib"; } else bl.hidden=true;
      const rj=$("#tkJr"); if(J.rj){ rj.hidden=false; rj.innerHTML="⛔ "+esc(J.rj)+(J.rn?(' <span style="color:#8b918b">· maanta '+J.rn+' la diiday</span>'):''); } else rj.hidden=true;
    } }
  //--- lambarrada
  $("#tkDN").innerHTML=(D.n||0)+'<span style="font-size:11px;color:#7a818b">/'+(cf.maxd||"—")+'</span>';
  const dp=$("#tkDPL"); dp.textContent=tkMoney(D.pl); dp.className=(Number(D.pl)||0)>0?"up":((Number(D.pl)||0)<0?"dn":"");
  $("#tkWR").textContent=(T.n>0)?(Math.round(100*(T.w||0)/T.n)+"%"):"—";
  const pf=$("#tkPF"); pf.textContent=(T.n>0)?Number(T.pf||0).toFixed(2):"—"; pf.className=(T.n>0)?"y":"";
  $("#tkMDD").textContent=Number(a.mdd||0).toFixed(2)+"%";
  $("#tkCL").textContent=(D.cl||0)+" / "+(cf.ml||"—");
  //--- saacadaha (waqtiga server-ka)
  const hs=(cf.hs!==undefined)?Number(cf.hs):1, he=Number(cf.he)||22;
  const si=$("#tkSesI"); if(hs<he){ si.style.left=(hs/24*100)+"%"; si.style.width=((he-hs)/24*100)+"%"; } else { si.style.left="0"; si.style.width="100%"; }
  const se=$("#tkSesE");
  if(a.srv){ const d=new Date(a.srv*1000), h=d.getUTCHours()+d.getUTCMinutes()/60; se.hidden=false; se.style.left=(h/24*100)+"%"; $("#tkSesM").textContent="SAACADAHA · HADDA "+tkHM(a.srv); }
  else { se.hidden=true; $("#tkSesM").textContent="SAACADAHA (server)"; }
  $("#tkSesA").textContent=String(hs).padStart(2,"0")+":00"; $("#tkSesB").textContent=String(he).padStart(2,"0")+":00";
  //--- chips
  const BL=(a.bkt&&a.bkt.lay)?a.bkt.lay:null;   // v12.16
  const lot=BL?BL.reduce((x,l)=>x+(Number(l.lot)||0),0):((a.st==="OPEN")?Number(a.lot||0):(Number(a.lotp)||Number(cf.lot)||0.01));
  $("#tkChips").innerHTML='<span>LOT <b>'+lot.toFixed(2)+'</b> '+(cf.risk>0?("risk "+Number(cf.risk).toFixed(2)+"%"):"go'an")+'</span>'
    +(a.sp!==undefined?'<span>SPREAD <b>$'+Number(a.sp).toFixed(2)+'</b></span>':'')
    +'<span>TRADE <b>'+(BL?(BL.length+'/'+(cf.bn||BL.length)):((a.st==="OPEN"?1:0)+'/1'))+'</b></span><span>DD <b>'+Number(a.dd||0).toFixed(2)+'%</b></span>';
  //--- taariikhda
  const H=a.hist||[];
  $("#tkHist").innerHTML=H.length?H.map(h=>{ const p=Number(h.pl)||0;
      const nb=Number(h.n)||0, BX={10:"$ ✓",11:"BE",12:"TP RAAC",2:"SL",1:"TP",3:"WAQTI",9:"—"};
      const nm=nb>0?("BASKET ×"+nb+" · "+(BX[h.x]||"—")):((h.b===1 && h.x===2)?"STOP LOSS":((h.b===2 && h.x===1)?"TAKE PROFIT":(TK_X[h.x]||"—"))), ex=(h.k!==undefined);   // v12.15 · v12.16
      const sl=Number(h.s)||0;
      return '<div class="tkhr2"><div class="tkhr"><span class="t">'+tkHM(h.t)+'</span><span>'+(h.d>0?"BUY":"SELL")+'</span><span>'+esc(nm)+(h.b?' 🔒':'')+'</span><b class="'+(p>0?"g":(p<0?"r":""))+'">'+tkMoney(p)+'</b></div>'
        +(nb>0?('<div class="x">'+nb+' lakab · peak <b>'+tkMoney(h.k)+'</b> · spread <b>$'+Number(h.sp||0).toFixed(2)+'</b></div>')
          :(ex?('<div class="x">peak <b>+$'+Number(h.k||0).toFixed(2)+'</b> · slip <b class="'+(sl>0.3?"r":"")+'">$'+sl.toFixed(2)+'</b> · spread <b>$'+Number(h.sp||0).toFixed(2)+'</b></div>'):''))+'</div>'; }).join("")
    :'<div class="bidle" style="margin-top:4px">Weli trade lama xidhin (EA v70.1+).</div>';
  //--- v12.14: tayada bixitaanka (EA v70.2+)
  const qa=$("#tkQA");
  if(T.apk!==undefined && T.n>0){
    qa.hidden=false;
    const apk=Number(T.apk)||0, asl=Number(T.asl)||0, asp=Number(T.asp)||0, vtp=Number(cf.vtp)||0;
    const e1=$("#tkAPK"); e1.textContent="+$"+apk.toFixed(2); e1.className=(vtp>0&&apk>=vtp)?"g":"y";
    const e2=$("#tkASL"); e2.textContent=(T.sln>0)?("$"+asl.toFixed(2)):"—"; e2.className=(asl>0.3)?"r":"g";
    $("#tkASP").textContent="$"+asp.toFixed(2);
    const q=[];
    if(vtp>0) q.push(apk<vtp?("Peak <b>$"+apk.toFixed(2)+" &lt; TP $"+vtp.toFixed(2)+"</b> → trade-yadu inta badan TP ma gaadhaan · TP ≈ $"+Math.max(0.5,apk*0.9).toFixed(2)+" tijaabi."):("Peak <b>$"+apk.toFixed(2)+" ≥ TP</b> → TP-gu waa macquul."));
    if(T.sln>0) q.push(asl>0.3?("Slippage <b>$"+asl.toFixed(2)+"</b> weyn → SL BROKER isticmaal / broker kale."):("Slippage <b>$"+asl.toFixed(2)+"</b> → SL-ku si sax ah ayuu u shaqeeyaa."));
    $("#tkQH").innerHTML=q.join("<br>")||"—";
  } else qa.hidden=true;
  //--- hoose
  $("#tkFtL").innerHTML=(T.n||0)+' trade · <b class="'+((Number(T.pl)||0)>=0?"g":"r")+'">'+tkMoney(T.pl)+'</b>';
  $("#tkFtR").innerHTML='🛡 SL adag <b>$'+Number(cf.hard||0).toFixed(2)+'</b> · VSL <b>$'+Number(cf.vsl||0).toFixed(2)+'</b> · VTP <b>'+(cf.vtp>0?("$"+Number(cf.vtp).toFixed(2)):"off")+'</b> · BE <b>$'+Number(cf.be||0).toFixed(2)+'</b>';
}

/* ---- v12.9: ASIA BREAKOUT (Maamul · admin) ---- */
let ASIACFG=null, ASIA=null, mAsDir=0, mAsSL=0, mAsL=1, mAsTP=2.0;
let mAgM=1, mAgL=3, mAgS=0.25, mAgX=2.0;   // v12.10: GRID / MARTINGALE
function agHints(){
  const on=swGet("mASG");
  const bx=$("#asgBox"); if(bx) bx.classList.toggle("off",!on);
  ["rASIAL","mASIALHint"].forEach(id=>{ const e=$("#"+id); if(e) e.classList.toggle("rdim",on); });
  segPaint("mASGM","v",mAgM);
  if($("#mASGL")) $("#mASGL").textContent=mAgL;
  if($("#mASGS")) $("#mASGS").textContent=Number(mAgS).toFixed(2);
  if($("#mASGX")) $("#mASGX").textContent="×"+Number(mAgX).toFixed(Number.isInteger(mAgX*1)?1:2);
  const rx=$("#rASGX"); if(rx) rx.classList.toggle("rdim",mAgM!==1);
  const mult=(mAgM===1)?mAgX:1, A=ASIA||{}, mn=Number(A.mn)||0.01;
  let nE=mAgL; while(nE>2 && (nE-1)*mAgS>0.8+1e-9) nE--;   // EA: lakabka ugu hooseeya SL-ka 0.2R ka fog
  const w=[]; for(let k=0;k<nE;k++) w.push(Math.pow(mult,k));
  const lots=w.map(x=>Math.max(mn,Math.floor(mn*x/mn+1e-9)*mn));
  const L=$("#mASGLots"); if(L) L.innerHTML=lots.map((l,k)=>'<div><b>'+l.toFixed(2)+'</b>#'+(k+1)+(k?(' · −'+(k*mAgS).toFixed(2)+'R'):' · entry')+'</div>').join("");
  const sw=w.reduce((s,x,k)=>s+x*(1-k*mAgS),0), sp=w.reduce((s,x,k)=>s+x*(mAsTP+k*mAgS),0);
  const h=$("#mASGHint"); if(h) h.innerHTML=on?("Dhammaan buuxsamaan + SL → khasaare ≈ <b>risk-gaaga</b> ("+Number(asNum("mASIARISK",1))+"%) · TP → ilaa <b>"+(sp/sw).toFixed(1)+"×</b> risk-ga ("+nE+" lakab"+(nE<mAgL?(" · "+mAgL+" → "+nE+": masaafo yar dooro"):"")+") · #1 oo keliya → "+(mAsTP/sw).toFixed(2)+"×. Lakabyadu way wadaagaan SL-ka iyo TP-ga.")
    :"OFF → lakabyada hore (5 · BASKET) ayaa shaqeynaya.";
}
function agStep(which,d){
  if(which==="L") mAgL=Math.max(2,Math.min(5,mAgL+d));
  if(which==="S") mAgS=Math.max(0.15,Math.min(0.33,Math.round((mAgS+d*0.05)*100)/100));
  if(which==="X") mAgX=Math.max(1,Math.min(2,Math.round((mAgX+d*0.25)*100)/100));
  mTouched=true; agHints();
}
function goldPaint(){
  const b=swGet("mBSK"), a=swGet("mASIA");
  segPaint("mGOLD","v",(b&&a)?2:(a?1:(b?0:-1)));
  const g=$("#asGrp"); if(g) g.classList.toggle("off",!a);
  bskGrpPaint();
}
function asNum(id,d){ const e=$("#"+id); const v=e?Number(e.value):NaN; return isFinite(v)&&e.value!==""?v:d; }
function asHints(){
  segPaint("mASIADIR","v",mAsDir); segPaint("mASIASL","v",mAsSL); segPaint("mASIAL","v",mAsL);
  const tp=$("#mASIATP"); if(tp) tp.textContent=Number(mAsTP).toFixed(1);
  const A=ASIA||{}, rg=(Number(A.hi)>0&&Number(A.lo)>0)?(A.hi-A.lo):0;
  const vpp=Number(A.vpp)||100, mn=Number(A.mn)||0.01, bal=Number(A.bal)||0;
  const slD=rg>0?(mAsSL===0?rg/2:rg):0;
  const sh=$("#mASIASLHint");
  if(sh) sh.innerHTML=rg>0
    ? ("Range-ka maanta <b>$"+rg.toFixed(1)+"</b> → SL ≈ <b>$"+(slD*mn*vpp).toFixed(0)+"</b> · TP ≈ <b>$"+(slD*mAsTP*mn*vpp).toFixed(0)+"</b> ("+mn.toFixed(2)+" lot)")
    : (mAsSL===0?"<b>BARTAMAHA:</b> SL = kala bar range-ka (≈ $25 marka range-ku $50 yahay · 0.01 lot).":"<b>DHINACA KALE:</b> SL = dhinaca kale ee range-ka (SL weyn, win rate sare).");
  const rh=$("#mASIARiskHint");
  if(rh){
    const risk=asNum("mASIARISK",1), mx=asNum("mASIAMAX",30);
    if(rg>0 && bal>0){
      const r1=slD*mn*vpp, want=bal*risk/100, lim=mx>0?mx:want*2;
      if(r1>lim){ rh.hidden=false; rh.innerHTML="⚠ "+mn.toFixed(2)+" lot khatartiisu <b>$"+r1.toFixed(0)+"</b> &gt; xadka $"+lim.toFixed(0)+" → <b>maanta trade ma furmo</b>. Xadka $ kordhi ama SL BARTAMAHA dooro."; }
      else if(r1>want){ rh.hidden=false; rh.innerHTML="⚠ Balance $"+bal.toFixed(0)+" · "+risk+"% = $"+want.toFixed(0)+" &lt; SL "+mn.toFixed(2)+" lot ($"+r1.toFixed(0)+") → maanta <b>"+mn.toFixed(2)+" lot (risk $"+r1.toFixed(0)+")</b>. Balance ka weyn ama cent account ayaa ku habboon."; }
      else rh.hidden=true;
    } else rh.hidden=true;
  }
  const lh=$("#mASIALHint");
  if(lh) lh.innerHTML=mAsL===1?"<b>1 · HAL TRADE:</b> tijaabada ugu fiican (+29R). La talin."
    :("<b>"+mAsL+" LAKAB:</b> kuwa kale waxay furmaan <b>retest</b> heerka la jebiyay (2 saac gudahood). Risk-ga guud isma beddelo — lot-ka ayaa la qaybiyaa (account weyn ayuu u baahan yahay).");
}
function asSeed(){
  const c=ASIACFG; if(!c){ goldPaint(); asHints(); agHints(); return; }
  swSet("mASIA",!!c.on); swSet("mASIATR",!!c.tr);
  const set=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; };
  set("mASIAMAXR",c.maxr); set("mASIARISK",c.risk); set("mASIAMAX",c.max);
  if($("#mASIAWE")) $("#mASIAWE").textContent=c.we; if($("#mASIACL")) $("#mASIACL").textContent=c.cl;
  mAsDir=Number(c.dir)||0; mAsSL=Number(c.sl)||0; mAsL=Math.max(1,Math.min(3,Number(c.l)||1)); mAsTP=Number(c.tp)||2;
  if(c.g!==undefined){ swSet("mASG",!!c.g); mAgM=Number(c.gm)?1:0; mAgL=Math.max(2,Math.min(5,Number(c.gl)||3)); mAgS=Number(c.gs)||0.25; mAgX=Number(c.gx)||2; }   // v12.10
  goldPaint(); asHints(); agHints();
}
function asiaCmds(cmds){
  cmds.push("SET:ASIA="+(swGet("mASIA")?1:0));
  cmds.push("SET:ASIADIR="+mAsDir); cmds.push("SET:ASIASL="+mAsSL); cmds.push("SET:ASIAL="+mAsL);
  cmds.push("SET:ASIATR="+(swGet("mASIATR")?1:0));
  cmds.push("SET:ASIATP="+Number(mAsTP).toFixed(1));
  const we=Number(($("#mASIAWE")||{}).textContent), cl=Number(($("#mASIACL")||{}).textContent);
  if(we>=8&&we<=20) cmds.push("SET:ASIAWE="+we);
  if(cl>=13&&cl<=23) cmds.push("SET:ASIACL="+cl);
  const mr=$("#mASIAMAXR"), rk=$("#mASIARISK"), mx=$("#mASIAMAX");
  if(mr && mr.value!=="") cmds.push("SET:ASIAMAXR="+Math.round(Number(mr.value)));
  if(rk && rk.value!=="") cmds.push("SET:ASIARISK="+rk.value);
  if(mx && mx.value!=="") cmds.push("SET:ASIAMAX="+Math.round(Number(mx.value)));
  cmds.push("SET:ASG="+(swGet("mASG")?1:0)); cmds.push("SET:ASGM="+mAgM); cmds.push("SET:ASGL="+mAgL);   // v12.10
  cmds.push("SET:ASGS="+Number(mAgS).toFixed(2)); cmds.push("SET:ASGX="+Number(mAgX).toFixed(2));
}
document.querySelectorAll("#mGOLD button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; const v=Number(b.dataset.v); swSet("mBSK",v!==1); swSet("mASIA",v!==0); mTouched=true; goldPaint(); }));
{ const e=$("#mASG"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; agHints(); }); }   // v12.10
document.querySelectorAll("#mASGM button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mAgM=Number(b.dataset.v); mTouched=true; agHints(); }));
[["L","mASGL"],["S","mASGS"],["X","mASGX"]].forEach(([w,id])=>{ const m=$("#"+id+"m"), p=$("#"+id+"p");
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) agStep(w,-1); }); if(p) p.addEventListener("click",()=>{ if(!p.disabled) agStep(w,1); }); });
["mASIA","mASIATR"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; e.classList.toggle("on"); mTouched=true; goldPaint(); }); });
{ const e=$("#mBSK"); if(e) e.addEventListener("click",()=>goldPaint()); }
document.querySelectorAll("#mASIADIR button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mAsDir=Number(b.dataset.v); mTouched=true; asHints(); }));
document.querySelectorAll("#mASIASL button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mAsSL=Number(b.dataset.v); mTouched=true; asHints(); }));
document.querySelectorAll("#mASIAL button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mAsL=Number(b.dataset.v); mTouched=true; asHints(); }));
["mASIAMAXR","mASIARISK","mASIAMAX"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("input",()=>{ mTouched=true; asHints(); }); });
[["mASIAWE",8,20,1],["mASIACL",13,23,1]].forEach(([id,lo,hi,st])=>{
  const m=$("#"+id+"m"), p=$("#"+id+"p");
  const go=d=>{ const e=$("#"+id); if(!e) return; let v=Number(e.textContent)||lo; v=Math.max(lo,Math.min(hi,v+d*st)); e.textContent=v;
    const we=Number(($("#mASIAWE")||{}).textContent)||12, c=$("#mASIACL"); if(c && Number(c.textContent)<we) c.textContent=Math.max(13,we); mTouched=true; };
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) go(-1); });
  if(p) p.addEventListener("click",()=>{ if(!p.disabled) go(1); });
});
{ const m=$("#mASIATPm"), p=$("#mASIATPp");
  const go=d=>{ mAsTP=Math.max(0.5,Math.min(5,Math.round((Number(mAsTP)+d*0.5)*10)/10)); mTouched=true; asHints(); };
  if(m) m.addEventListener("click",()=>{ if(!m.disabled) go(-1); });
  if(p) p.addEventListener("click",()=>{ if(!p.disabled) go(1); }); }

/* ---- v12.9: ASIA BREAKOUT (Guud · live) ---- */
const AS_DN=["Axd","Isn","Tal","Arb","Kha","Jim","Sab"], AS_X={1:"TP",2:"SL",3:"20:00",4:"XAD",5:"jebin lama helin",6:"BE",7:"la dhaafay"};
function asDay(d){ const y=Math.floor(d/10000), m=Math.floor(d/100)%100, dd=d%100; const t=new Date(Date.UTC(y,m-1,dd)); return (AS_DN[t.getUTCDay()]||"")+" "+String(dd).padStart(2,"0"); }
function asSvg(a){
  const sv=$("#asSvg"); if(!sv) return;
  const dg=Number(a.dg)||2, hi=Number(a.hi)||0, lo=Number(a.lo)||0, px=Number(a.px)||0;
  if(!(hi>0&&lo>0)){ sv.innerHTML='<text x="200" y="104" fill="#8b918b" font-size="13" text-anchor="middle">Asia range waa la dhisayaa ('+String(a.rs||0).padStart(2,"0")+':00–'+String(a.re||7).padStart(2,"0")+':00 GMT)</text>'; return; }
  const pts=[hi,lo,px]; if(a.st==="OPEN"){ pts.push(Number(a.sl)||lo, Number(a.tp)||hi, Number(a.e)||px); }
  let mx=Math.max(...pts.filter(v=>v>0)), mn=Math.min(...pts.filter(v=>v>0)); const pad=(mx-mn)*0.12||1; mx+=pad; mn-=pad;
  const Y=v=>10+(mx-v)/(mx-mn)*180, W=400;
  const mid=(hi+lo)/2, f=v=>Number(v).toFixed(dg);
  let h='<rect x="8" y="'+Y(hi)+'" width="170" height="'+Math.max(2,Y(lo)-Y(hi))+'" fill="rgba(240,207,134,.08)" stroke="#7a6230" stroke-dasharray="4 4"/>'
    +'<rect x="178" y="6" width="140" height="188" fill="rgba(34,197,94,.06)"/>'
    +'<text x="14" y="'+(Y(hi)-6)+'" fill="#d4a94f" font-size="11" font-weight="700">ASIA '+String(a.rs||0).padStart(2,"0")+'–'+String(a.re||7).padStart(2,"0")+'</text>'
    +'<text x="184" y="20" fill="#7fe0ab" font-size="11" font-weight="700">JEBINTA '+String(a.re||7).padStart(2,"0")+'–'+String(a.we||12).padStart(2,"0")+'</text>'
    +'<line x1="8" y1="'+Y(hi)+'" x2="'+(W-8)+'" y2="'+Y(hi)+'" stroke="#22c55e" stroke-width="1.6"/>'
    +'<line x1="8" y1="'+Y(lo)+'" x2="'+(W-8)+'" y2="'+Y(lo)+'" stroke="#ef4444" stroke-width="1.6"/>'
    +'<line x1="8" y1="'+Y(mid)+'" x2="'+(W-8)+'" y2="'+Y(mid)+'" stroke="#8b918b" stroke-dasharray="3 5"/>'
    +'<text x="'+(W-10)+'" y="'+(Y(hi)-5)+'" fill="#7fe0ab" font-size="10.5" text-anchor="end">SARE '+f(hi)+' → BUY</text>'
    +'<text x="'+(W-10)+'" y="'+(Y(lo)+14)+'" fill="#f2a3a3" font-size="10.5" text-anchor="end">HOOS '+f(lo)+' → SELL</text>'
    +'<text x="'+(W-10)+'" y="'+(Y(mid)+13)+'" fill="#8b918b" font-size="10" text-anchor="end">bartamaha '+f(mid)+'</text>';
  if(a.st==="OPEN"){
    const e=Number(a.e), sl=Number(a.sl), tp=Number(a.tp);
    h+='<line x1="178" y1="'+Y(e)+'" x2="'+(W-8)+'" y2="'+Y(e)+'" stroke="#3987e5" stroke-width="1.6"/>'
      +'<line x1="178" y1="'+Y(tp)+'" x2="'+(W-8)+'" y2="'+Y(tp)+'" stroke="#22c55e" stroke-dasharray="6 4"/>'
      +'<line x1="178" y1="'+Y(sl)+'" x2="'+(W-8)+'" y2="'+Y(sl)+'" stroke="#ef4444" stroke-dasharray="6 4"/>'
      +'<text x="184" y="'+((Y(tp)-4<34)?(Y(tp)+13):(Y(tp)-4))+'" fill="#7fe0ab" font-size="10">TP '+f(tp)+'</text>'
      +'<text x="184" y="'+(Y(sl)+12)+'" fill="#f2a3a3" font-size="10">SL '+f(sl)+'</text>'
      +'<text x="184" y="'+(Y(e)-4)+'" fill="#9cc3f5" font-size="10">'+esc(a.dir||"")+' '+f(e)+'</text>';
  }
  if(px>0) h+='<circle cx="330" cy="'+Y(px)+'" r="4" fill="#fff"/><text x="322" y="'+(Y(px)+4)+'" fill="#e8ece8" font-size="11.5" font-weight="700" text-anchor="end">HADDA '+f(px)+'</text>';
  sv.innerHTML=h;
}
function paintAsia(a){
  const c=$("#asCard"); if(!c) return;
  if(!a){ c.hidden=true; return; }
  c.hidden=false; ASIA=a;
  const ST={OFF:["DAMMAN","off"],RANGE:["RANGE LA DHISAYAA","wait"],WAIT:["SUGAYA JEBIN","wait"],OPEN:["● TRADE SOCDA","run"],DONE:["MAANTA WAA LA QAATAY","run"],SKIP:["MAANTA MA JIRO","off"]};
  const s=ST[a.st]||["—",""]; const st=$("#asState"); st.className="bchip "+s[1]; st.textContent=s[0];
  $("#asTitle").textContent="ASIA BREAKOUT · "+(a.sym||"XAUUSD");
  asSvg(a);
  const rg=(Number(a.hi)>0&&Number(a.lo)>0)?(a.hi-a.lo):0;
  $("#asRng").textContent=rg>0?("$"+rg.toFixed(1)):"—";
  const tr=$("#asTr"); tr.textContent=a.tr>0?"↑ KOR":(a.tr<0?"↓ HOOS":"— ma cadda"); tr.className=a.tr>0?"up":(a.tr<0?"dn":"");
  $("#asCnt").textContent=((a.st==="OPEN"||a.st==="DONE")?1:0)+" / 1";
  const live=$("#asLive");
  if(a.st==="OPEN"){
    live.hidden=false;
    const pl=Number(a.pl)||0, e=$("#asPL"); e.textContent=bskMoney(pl); e.className=pl>=0?"pos":"neg";
    const rr=(Number(a.risk)>0)?(pl/a.risk):0;
    $("#asSub").textContent=(rr>=0?"+":"")+rr.toFixed(2)+"R · risk $"+(Number(a.risk)||0).toFixed(2)+" · "+((a.lk&&a.lk.length>1)?a.lk.map(x=>Number(x).toFixed(2)).join("/"):(a.lot||0))+" lot"+((a.n||1)>1?(" · "+((a.cfg&&a.cfg.g)?(a.cfg.gm?"MARTI":"GRID"):"lakab")+" "+(a.fill||0)+"/"+a.n):"");
    const dg=Number(a.dg)||2;
    $("#asLvl").innerHTML="<b>"+esc(a.dir||"")+"</b> @ "+Number(a.e).toFixed(dg)+" · SL "+Number(a.sl).toFixed(dg)+" · TP "+Number(a.tp).toFixed(dg)+" · xidh "+String(a.cl||20).padStart(2,"0")+":00 GMT";
  } else live.hidden=true;
  $("#asWhy").hidden=(a.st==="OPEN");
  let why=a.why||"";
  if(!a.on && a.st!=="OPEN") why="ASIA BREAKOUT waa damman — Maamul → Xeeladaha → 🌅 ASIA ka shid.";
  else if(a.st==="WAIT" && a.tr!==0){ const d=a.tr>0?"BUY":"SELL"; why=esc(why)+'<br><small style="color:var(--ink3)">Trend-ku waa '+(a.tr>0?"KOR":"HOOS")+' → <b>'+d+' oo keliya</b> marka '+(a.tr>0?"SARE":"HOOS")+' la jebiyo (ilaa '+String(a.we||12).padStart(2,"0")+':00 GMT).</small>'; $("#asWhy").innerHTML=why; why=null; }
  if(why!==null) $("#asWhy").textContent=why||"—";
  const H=(a.hist||[]);
  $("#asHist").innerHTML=H.length?H.map(h=>{
    const t=Number(h.dir)!==0, p=Number(h.pl)||0;
    const txt=t?((h.dir>0?"BUY":"SELL")+" · "+(AS_X[h.x]||"")+" "+(Number(h.r)>=0?"+":"")+Number(h.r).toFixed(1)+"R"):(AS_X[h.x]||"—");
    return '<div class="ashr"><span class="d">'+esc(asDay(h.d))+'</span><span class="'+(t?(p>=0?"g":"r"):"m")+'">'+esc(txt)+'</span><span class="p '+(t?(p>=0?"g":"r"):"m")+'">'+(t?bskMoney(p):"—")+'</span></div>';
  }).join(""):'<div class="bidle" style="margin-top:6px">Weli trade ma jiro.</div>';
  const T=H.filter(h=>Number(h.dir)!==0), W=T.filter(h=>Number(h.pl)>0).length, S=T.reduce((x,h)=>x+(Number(h.pl)||0),0), R=T.reduce((x,h)=>x+(Number(h.r)||0),0);
  $("#asSum").textContent=T.length?(T.length+" trade · win "+Math.round(100*W/T.length)+"% · "+bskMoney(S)+" · "+(R>=0?"+":"")+R.toFixed(1)+"R"):"—";
  if(!mTouched) asHints();
}

let BSKCFG=null;   // v12.7.1: sitinka chart-ka dahabka (chart-yada kale kama duwanaan karaan)
function bskSeed(st){
  if(BSKCFG){ const c=BSKCFG; st=Object.assign({},st||{},{bsk_on:!!c.on,bsk_sig:c.sig,bsk_n:c.n,bsk_ent:c.ent,bsk_risk:c.risk,bsk_max:c.max,bsk_day:c.day,
    bsk_spr:c.spr,bsk_tp:c.tp,bsk_tgt:c.tgt,bsk_be:!!c.be,bsk_lock:!!c.lock,bsk_ses:!!c.ses,bsk_blk:!!c.blk,
    bsk_dtol:c.dtol,bsk_dneck:c.dneck,bsk_dtp:c.dtp}); }
  if(!st || st.bsk_on===undefined){ bskHints(); bskGrpPaint(); return; }
  const set=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; };
  swSet("mBSK",!!st.bsk_on); swSet("mBSKBE",!!st.bsk_be); swSet("mBSKLOCK",!!st.bsk_lock); swSet("mBSKSES",!!st.bsk_ses); swSet("mBSKBLK",!!st.bsk_blk);
  set("mBSKRISK",st.bsk_risk); set("mBSKMAX",st.bsk_max); set("mBSKSPR",st.bsk_spr); set("mBSKTGT",st.bsk_tgt);
  set("mBSKDTOL",st.bsk_dtol); set("mBSKDNECK",st.bsk_dneck); set("mBSKDTP",st.bsk_dtp);   // v12.11
  if($("#mBSKN")) $("#mBSKN").textContent=st.bsk_n; if($("#mBSKDAY")) $("#mBSKDAY").textContent=st.bsk_day;
  mBskSig=Number(st.bsk_sig); mBskEnt=Number(st.bsk_ent); mBskTP=Number(st.bsk_tp);
  bskHints(); bskGrpPaint();
}
function bskCmds(cmds){
  const num=id=>{ const e=$("#"+id); return e&&e.value!==""?e.value:null; };
  cmds.push("SET:BSK="+(swGet("mBSK")?1:0));
  if(mBskSig===0||mBskSig===1||mBskSig===2||mBskSig===3) cmds.push("SET:BSKSIG="+mBskSig);
  { const dt=num("mBSKDTOL"), dn=num("mBSKDNECK"), dp=num("mBSKDTP");   // v12.11 DOUBLE
    if(dt!==null) cmds.push("SET:BSKDTOL="+dt); if(dn!==null) cmds.push("SET:BSKDNECK="+dn); if(dp!==null) cmds.push("SET:BSKDTP="+dp); }
  if(mBskEnt===0||mBskEnt===1) cmds.push("SET:BSKENT="+mBskEnt);
  if(mBskTP===0||mBskTP===1) cmds.push("SET:BSKTP="+mBskTP);
  const n=Number(($("#mBSKN")||{}).textContent), dd=Number(($("#mBSKDAY")||{}).textContent);
  if(n>=2&&n<=5) cmds.push("SET:BSKN="+n);
  if(dd>=1&&dd<=10) cmds.push("SET:BSKDAY="+dd);
  const rk=num("mBSKRISK"), mx=num("mBSKMAX"), sp=num("mBSKSPR"), tg=num("mBSKTGT");
  if(rk!==null) cmds.push("SET:BSKRISK="+rk);
  if(mx!==null) cmds.push("SET:BSKMAX="+Math.round(Number(mx)));
  if(sp!==null) cmds.push("SET:BSKSPR="+Math.round(Number(sp)));
  if(tg!==null) cmds.push("SET:BSKTGT="+Math.round(Number(tg)));
  cmds.push("SET:BSKBE="+(swGet("mBSKBE")?1:0)); cmds.push("SET:BSKLOCK="+(swGet("mBSKLOCK")?1:0));
  cmds.push("SET:BSKSES="+(swGet("mBSKSES")?1:0)); cmds.push("SET:BSKBLK="+(swGet("mBSKBLK")?1:0));
}

/* ---- v12.7: GOLD BASKET (Guud · live) ---- */
function bskMoney(v){ const n=Number(v)||0; return (n>=0?"+$":"−$")+Math.abs(n).toFixed(2); }
function paintBasket(b){
  const c=$("#bskCard"); if(!c) return;
  if(!b){ c.hidden=true; return; }
  c.hidden=false;
  const SIGN=["ZONE","EMA","LABADA","DOUBLE"];   // v12.11
  const st=$("#bskState"), chips=$("#bskChips"), live=$("#bskLive"), idle=$("#bskIdle");
  const day=b.day||{};
  $("#bskDay").textContent=(day.n||0)+" / "+(day.max||0)+" basket  ·  "+bskMoney(day.pl)+(day.blk?("  ·  la joojiyay: "+day.blk):"");
  if(!b.act){
    $("#bskTitle").textContent=(b.sym||"XAUUSD")+" · GOLD BASKET";
    st.className="bchip "+(b.on?"wait":"off"); st.textContent=b.on?"● SUGAYA SIGNAL":"DAMMAN";
    chips.innerHTML='<span class="y">'+esc(SIGN[b.sig]||"LABADA")+'</span>';
    live.hidden=true; idle.hidden=false;
    idle.innerHTML=b.on?("Basket ma socdo — bot-ku wuxuu sugayaa signal "+esc(SIGN[b.sig]||"")+" tayo leh."+(b.why?('<br><small style="color:var(--ink3)">Ugu dambeeyay: '+esc(b.why)+'</small>'):""))
                       :"GOLD BASKET waa damman — Maamul → Xeeladaha → 🥇 GOLD BASKET ka shid.";
    return;
  }
  $("#bskTitle").textContent=(b.sym||"XAUUSD")+" · "+(b.dir||"")+" BASKET";
  st.className="bchip run"; st.textContent="● SOCDA";
  const stars="★".repeat(Math.max(1,Math.min(3,b.stars||2)));
  chips.innerHTML='<span>'+esc(b.src||"")+' '+stars+'</span><span class="y">'+esc(b.ent||"")+' '+(b.fill||0)+'/'+(b.n||0)+'</span>'
    +(b.be?'<span class="g">SL = BE ✓</span>':(b.tp1?'<span class="g">TP1 ✓</span>':''));
  live.hidden=false; idle.hidden=true;
  const pl=Number(b.pl)||0, mx=Math.max(0.01,Number(b.max)||Number(b.risk)||1), tg=Math.max(0.01,Number(b.tgt)||mx);
  const e=$("#bskPL"); e.textContent=bskMoney(pl); e.className=pl>=0?"pos":"neg";
  $("#bskSub").textContent="/ bartilmaameed $"+tg.toFixed(0)+" · khasaare ugu badan $"+mx.toFixed(0);
  const span=mx+tg, pos=Math.max(0,Math.min(1,(pl+mx)/span)), be=mx/span;
  const bar=$("#bskBar");
  if(pl>=0){ bar.style.left=(be*100)+"%"; bar.style.width=((pos-be)*100)+"%"; bar.className=""; }
  else { bar.style.left=(pos*100)+"%"; bar.style.width=((be-pos)*100)+"%"; bar.className="neg"; }
  $("#bskBE").style.left="calc("+(be*100)+"% - 1px)";
  $("#bskL").textContent="−$"+mx.toFixed(0)+" (xad)"; $("#bskR").textContent="+$"+tg.toFixed(0)+(b.tpm===1?"":" (3R)");
  const dg=Number(b.dg)||2, SN={WAIT:"sug",OPEN:"furan",TP:"TP ✓",SL:"SL",BE:"BE",OFF:"—"};
  $("#bskRows").innerHTML=(b.rows||[]).map(r=>{
    const p=Number(r.pl)||0, off=(r.st==="OFF"||r.st==="WAIT");
    return '<div class="btr"'+(r.st==="OFF"?' style="opacity:.45"':'')+'><span class="n">#'+r.k+'</span><span>'+Number(r.px).toFixed(dg)+' · '+Number(r.lot).toFixed(2)
      +'<span class="s '+esc(r.st)+'">'+esc(SN[r.st]||r.st)+'</span></span><span class="p" style="color:'+(off?"var(--ink3)":(p>=0?"#3ddc84":"#f07a7a"))+'">'+(off?"—":bskMoney(p))+'</span><span class="rr">'+(r.r>0?(Number(r.r).toFixed(1)+"R"):"")+'</span></div>';
  }).join("");
  const bb=$("#bskBEBtn"); if(bb && !bb.classList.contains("lockd")) bb.disabled=!!b.be;
}

function paintSettings(st){
  if(!st) return;
  paintEma(st);
  const note=$("#mNote");
  const prot=(st.prot!==undefined)?Number(st.prot):(st.steplock?(Number(st.lockmode)===1?1:2):(st.be?1:0));
  const PN=["DAMMAN","BREAK-EVEN","TALLAABO"];
  const SN={SR:"SR/SD",SMC:"SMC",BOTH:"LABADA"};
  if(note && Date.now()-mSavedAt>8000) note.textContent="Bot-ku wuxuu hadda isticmaalayaa:  "+(st.strat?(SN[st.strat]||st.strat)+"  ·  ":"")
    +"RR 1:"+(st.rr||3)+"  ·  Lot "+(st.auto_lot?"auto":(st.lot||0))
    +"  ·  "+PN[prot]+(prot>0?(" +"+(st.stepstart||0)+"p"):"")
    +(st.ema_on===undefined?"":("  ·  EMA "+(st.ema_on?"ON":"OFF")))
    +(st.stars===undefined?"":("  ·  ★≥"+st.stars));
  if(mTouched && mSeeded) return;          // qofku wuu wax qorayaa - ha ka qaadin gacanta
  const set=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; };
  set("mLOT",st.auto_lot?0:st.lot);
  set("mSTEP",st.step); set("mSTEPSTART",st.stepstart);
  set("mRISK",st.risk); set("mDLOSS",st.dloss); set("mMAXDD",st.maxdd); set("mSNRR",st.rr); set("mSNSLMAX",st.snslmax);
  set("mLOT2",(st.lot2!==undefined)?st.lot2:70);
  if(st.snday!==undefined && $("#mSNDAY")) $("#mSNDAY").textContent=st.snday;
  swSet("mSNIPER",(st.sniper_on!==undefined)?!!st.sniper_on:!!st.sniper); swSet("mNEWS",st.news===undefined?true:!!st.news);
  swSet("mEMAF",st.ema_on===undefined?true:!!st.ema_on);
  mStrat=(st.strat!==undefined && STRAT_I[st.strat]!==undefined)?STRAT_I[st.strat]:null; segPaint("mSTRAT","s",mStrat);
  mStars=(st.stars!==undefined)?Number(st.stars):2; segPaint("mSTARS","v",mStars);
  mSdProf=(st.sdprof!==undefined)?Number(st.sdprof):null; sdPaint();   // v12.8
  mProt=prot; protHint();
  bskSeed(st);   // v12.7
  asSeed();      // v12.9
  tkSeed();      // v12.12
  mSeeded=true;
}
function paintLocks(rows){
  const tb=$("#tl") && $("#tl").querySelector("tbody"); if(!tb) return;
  rows=rows||[];
  $("#cLock").textContent=rows.length?rows.length:"";
  if(!rows.length){ tb.innerHTML='<tr><td colspan="4" class="empty">Weli wax lama xidhin.</td></tr>'; $("#lockSum").textContent="—"; return; }
  let sum=0;
  tb.innerHTML=rows.map(r=>{
    sum+=Number(r.lock||0);
    const t=new Date((r.t||0)*1000).toLocaleTimeString();
    return "<tr><td>"+t+"</td><td>"+(r.sym||"")+"</td><td style='text-align:right' class='pos'>+"
      +Number(r.prof||0).toFixed(1)+"p</td><td style='text-align:right'><b>"
      +(Number(r.lock||0)>0?("+"+Number(r.lock).toFixed(0)+"p"):"0 (BE)")+"</b></td></tr>";
  }).join("");
  $("#lockSum").textContent="Xidhitaan "+rows.length+" jeer  ·  faa'iido la ilaaliyay "+sum.toFixed(0)+" pip";
}
async function sendSettings(){
  const btn=$("#mSend"); const old=btn.textContent;
  const num=id=>{ const e=$("#"+id); return e&&e.value!==""?e.value:null; };
  const cmds=[];
  // v12.6: SL/TP = SNIPER oo keliya (FIXED/ATR waa la saaray) · maamulku mar walba wuu shaqeeyaa · ATR adaptive = damman
  cmds.push("SET:SLTP=2"); cmds.push("SET:ADAPT=0"); cmds.push("SET:MGMT=1");
  const lot=num("mLOT"), stp=num("mSTEP"), sst=num("mSTEPSTART");
  if(lot!==null) cmds.push("SET:LOT="+lot);
  if(stp!==null) cmds.push("SET:STEP="+stp);
  if(sst!==null) cmds.push("SET:STEPSTART="+sst);
  if(mProt===0||mProt===1||mProt===2){
    cmds.push("SET:PROT="+mProt);
    cmds.push("SET:STEPON="+(mProt>0?1:0)); cmds.push("SET:LOCKMODE="+(mProt===1?1:0)); cmds.push("SET:BE=0");   // EA hore (v68.1-) sidoo kale
  }
  const r2=num("mRISK"), dl=num("mDLOSS"), md=num("mMAXDD"), rr=num("mSNRR"), sm=num("mSNSLMAX"), l2=num("mLOT2");
  if(r2!==null) cmds.push("SET:RISK="+r2);
  if(dl!==null) cmds.push("SET:DLOSS="+dl);
  if(md!==null) cmds.push("SET:MAXDD="+md);
  if(rr!==null) cmds.push("SET:SNRR="+rr);
  if(sm!==null) cmds.push("SET:SNSLMAX="+sm);
  if(l2!==null) cmds.push("SET:LOT2="+Math.round(Number(l2)));
  cmds.push("SET:SNDAY="+(Number($("#mSNDAY").textContent)||0));
  cmds.push("SET:SNIPER="+(swGet("mSNIPER")?1:0));
  cmds.push("SET:NEWS="+(swGet("mNEWS")?1:0));
  cmds.push("SET:EMAF="+(swGet("mEMAF")?1:0));
  if(mStrat===0||mStrat===1||mStrat===2) cmds.push("SET:STRAT="+mStrat);
  if(mStars>=1&&mStars<=3) cmds.push("SET:STARS="+mStars);
  if(mSdProf===0||mSdProf===1||mSdProf===2) cmds.push("SET:SDPROF="+mSdProf);   // v12.8
  bskCmds(cmds);   // v12.7: GOLD BASKET
  asiaCmds(cmds);  // v12.9: ASIA BREAKOUT
  tickCmds(cmds);  // v12.12: ⚡ TICK SCALPER
  znCmds(cmds);    // v12.22: 🎯 ZONE YAR
  cbCmds(cmds);    // v12.24: 📏 CABBIR LAMAANE
  grCmds(cmds);    // v12.25: 🪜 GRID STOP
  const values={}; cmds.forEach(c=>{ const m=/^SET:([A-Z0-9]+)=(.+)$/.exec(c); if(m) values[m[1]]=m[2]; });
  if(PERMS) Object.keys(values).forEach(k=>{ if(!permOK(PERM_OF[k])) delete values[k]; });   // v12: macmiil
  const unset=PERMS?[]:inpCollect(values);   // v12.18: ⚙️ INPUT (admin)
  btn.disabled=true; btn.textContent="Kaydinaya…";
  let err="";
  try{
    const body={values:values}; if(unset.length) body.unset=unset; if(accSel)body.account=accSel.value;
    const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json(); if(!d.ok) err=d.error||"Lama kaydin.";
  }catch(e){ err="Internet ma jiro — lama kaydin."; }
  btn.disabled=false; btn.textContent=old;
  if(!err){ mTouched=false; mSavedAt=Date.now(); inpSent(); }
  $("#mNote").textContent = err ? err : "La kaydiyay oo la diray. Chart kasta 20 ilbiriqsi gudahood ayuu qaadanayaa.";
  if($("#mNote2")) $("#mNote2").textContent=$("#mNote").textContent;   // v12.18
  tick();
}
if($("#mSend"))  $("#mSend").addEventListener("click",sendSettings);
if($("#mSend2")) $("#mSend2").addEventListener("click",sendSettings);   // v12.18: Maamul
if($("#mReset")) $("#mReset").addEventListener("click",async ()=>{
  const body={reset:true}; if(accSel)body.account=accSel.value;
  await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  mTouched=false; mSeeded=false; inEd={}; inDel={}; inSentM={}; if(INPS) inpPaint();
  $("#mNote").textContent="Celis: sitinka app-ka waa la tirtiray - bot-ku wuxuu ku noqonayaa sitinka koodka.";
  tick();
});

document.querySelectorAll("[data-cmd]").forEach(b=>{
  b.addEventListener("click",()=>send(b.dataset.cmd,b));
});


async function send(cmd,btn){
  const note=$("#cmdNote"); if(!note)return;
  if(btn){btn.disabled=true;}
  try{
    const body={cmd:cmd};
    if(accSel)body.account=accSel.value;
    const r=await fetch("/api/command",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    note.textContent = d.ok ? ("Waa la diray: "+cmd+" — EA-du 3–5s gudahood buu qaadanayaa.")
                            : ("Khalad: "+(d.error||"lama diri karin"));
  }catch(e){ note.textContent="Khalad shabakad."; }
  if(btn)setTimeout(()=>{btn.disabled=false;},1200);
  tick();
}
/* ---- Journal: tirakoob ---- */
let RANGE="month", jLoaded=false;

document.querySelectorAll("#rng button").forEach(b=>{
  b.addEventListener("click",()=>{
    RANGE=b.dataset.r;
    document.querySelectorAll("#rng button").forEach(x=>x.classList.toggle("on",x===b));
    loadJournal();
  });
});

function bars(host,items,keyf,labf){
  const el=$(host);
  if(!items || !items.length){ el.innerHTML='<p class="empty">Wax lama helin.</p>'; return; }
  const mx=Math.max(...items.map(i=>Math.abs(i.net)),1);
  el.innerHTML="";
  for(const it of items){
    const pos=it.net>=0, w=Math.max(2,Math.abs(it.net)/mx*100);
    const wr=it.n?Math.round(it.w/it.n*100):0;
    const d=document.createElement("div");
    d.className="bar";
    d.innerHTML='<span class="lb">'+esc(labf(it))+'</span>'+
      '<span class="tr"><span class="fi '+(pos?"p":"n")+'" style="width:'+w.toFixed(1)+'%"></span></span>'+
      '<span class="sb">'+it.n+"tr · "+wr+'%</span>'+
      '<span class="vl '+cls(it.net)+'">'+(it.net>0?"+":"")+money(it.net)+'</span>';
    el.appendChild(d);
  }
}

function rowsInto(id,items,labf){
  const tb=$(id+" tbody"); tb.innerHTML="";
  if(!items || !items.length){
    tb.innerHTML='<tr><td colspan="4" class="empty">Wax lama helin.</td></tr>'; return;
  }
  for(const it of items.slice().reverse()){
    const tr=document.createElement("tr");
    const wr=it.n?Math.round(it.w/it.n*100):0;
    tr.innerHTML='<td>'+esc(labf(it))+'</td><td>'+it.n+'</td><td>'+it.w+' ('+wr+'%)</td>'+
      '<td style="text-align:right" class="'+cls(it.net)+'">'+(it.net>0?"+":"")+money(it.net)+'</td>';
    tb.appendChild(tr);
  }
}

function bwCard(id,t,label){
  const el=$(id);
  if(!t){ el.innerHTML='<span class="empty">Weli midna lama xidhin.</span>'; return; }
  el.innerHTML='<div class="big '+cls(t.profit)+'">'+(t.profit>0?"+":"")+money(t.profit)+'</div>'+
    '<div class="sm">'+esc(t.sym||"")+" · "+esc(t.type||"")+" · "+esc(t.strat||"")+'</div>'+
    '<div class="sm">'+esc(when(t.ct))+'</div>';
}

async function loadJournal(){
  try{
    let q="?range="+RANGE;
    if(accSel)q+="&account="+encodeURIComponent(accSel.value);
    const r=await fetch("/api/journal"+q);
    if(r.status===401){location.href="/login";return;}
    const d=await r.json();
    if(!d.ok)return;
    jLoaded=true;
    const s=d.summary;
    $("#jNet").textContent=(s.net>0?"+":"")+money(s.net); $("#jNet").className="v "+cls(s.net);
    $("#jN").textContent=s.n;
    $("#jWR").textContent=s.n?(s.winrate.toFixed(1)+"%"):"—";
    $("#jPF").textContent=s.n?(s.pf>=99?"∞":s.pf.toFixed(2)):"—";
    $("#jPF").className="v "+(s.n?(s.pf>=1.3?"pos":(s.pf>=1?"neu":"neg")):"neu");

    bwCard("#jBest",d.best); bwCard("#jWorst",d.worst);
    bars("#jSym",  d.by_symbol,  null, i=>i.sym);
    bars("#jHour", d.by_hour,    null, i=>String(i.h).padStart(2,"0")+":00");
    bars("#jStrat",d.by_strategy,null, i=>i.strat);
    rowsInto("#jDay",d.by_day,i=>i.d);
    rowsInto("#jMon",d.by_month,i=>i.m);

    $("#jStore").textContent = d.stored_total
      ? (d.stored_total+" trade oo kaydsan"+(d.since?(" · laga bilaabo "+when(d.since)):""))
      : "Weli wax lama kaydin.";
  }catch(e){}
}


/* ======================================================================
   v12.20: 🎵 MUUSIG — YouTube · Spotify · Audiomack (link) + 📱 MP3 telefoonka (BASS EQ).
   Hal player (#muHost) oo mar walba nool: sheet-ka oo furan -> weyn (slot-ka), xidhan -> mini bar-ka.
   ====================================================================== */
const mu$=id=>document.getElementById(id);
const MU={items:[],loc:[],blobs:{},cat:"SO",addC:"SO",q:[],idx:-1,cur:"",it:null,kind:"",playing:false,shuf:false,rep:1,edit:false,
  yt:null,ytReady:false,ytP:null,sp:null,spApi:null,spP:null,spNear:false,ac:null,nodes:null,url:"",urlId:"",
  eq:{p:1,b:8,s:3,t:2,v:90},opt:{keep:1,duck:1,auto:1},where:"yt",loaded:false,loading:false,duck:false,duckT:null,
  lastOT:null,lastAcc:"",tm:0,du:0,tok:0,viz:0,delArm:"",tmr:null,last:""};
const MU_CN={SO:"Soomaali",WO:"Adduunka",MY:"Kuwayga"};
const MU_SRC={yt:["yt","YouTube"],ytl:["yt","YouTube"],sp:["sp","Spotify"],am:["am","Audiomack"],lo:["lo","📱 MP3"]};
const MU_PRE=[{b:0,s:0,t:0},{b:8,s:3,t:2},{b:12,s:6,t:1},{b:3,s:0,t:5}];
const MU_CH={
  SO:["Magool","Hasan Aden Samatar","Mohamed Mooge","Saado Cali Warsame","Maryam Mursal","Nimco Happy","Axmed Naaji Sacad","Cabdullaahi Qarshe","Sahra Halgan","Aar Maanta","Heeso Soomaali cusub"],
  WO:["Afrobeats","Amapiano","Bongo Flava","Arabic hits","R&B love songs","Reggae","Bass boosted","Lo-fi (shaqo)"],
  MY:["Qaraami","Heeso aroos","Nashiid","Heeso cusub 2026","Lo-fi (shaqo)"]};

function muSave(){
  try{ localStorage.setItem("mp_mu",JSON.stringify({cat:MU.cat,addC:MU.addC,eq:MU.eq,opt:MU.opt,shuf:MU.shuf,rep:MU.rep,where:MU.where,last:MU.cur||MU.last})); }catch(e){}
}
(function(){
  try{
    const s=JSON.parse(localStorage.getItem("mp_mu")||"null"); if(!s) return;
    if(MU_CN[s.cat]) MU.cat=s.cat; if(MU_CN[s.addC]) MU.addC=s.addC;
    if(s.eq && typeof s.eq==="object") ["p","b","s","t","v"].forEach(k=>{ if(isFinite(s.eq[k])) MU.eq[k]=Number(s.eq[k]); });
    if(s.opt && typeof s.opt==="object") ["keep","duck","auto"].forEach(k=>{ if(k in s.opt) MU.opt[k]=s.opt[k]?1:0; });
    MU.shuf=!!s.shuf; MU.rep=[0,1,2].includes(s.rep)?s.rep:1; MU.where=s.where==="sp"?"sp":"yt"; MU.last=String(s.last||"");
  }catch(e){}
})();
function muFmt(s){ s=Math.max(0,Math.floor(Number(s)||0)); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
function muMsg(t,bad){ const m=mu$("muMsg"); if(!m) return; m.textContent=t||""; m.className="mumsg"+(t?(bad?" e":" o"):""); }

/* ---- 📱 MP3-yada telefoonka: IndexedDB (server-ka ma gaadhaan) ---- */
const MUDB={db:null};
function muIdb(){
  return new Promise((res,rej)=>{
    if(MUDB.db) return res(MUDB.db);
    if(!window.indexedDB) return rej(new Error("Browser-kan ma kaydin karo MP3"));
    const r=indexedDB.open("mohapro_mus",1);
    r.onupgradeneeded=()=>{ try{ r.result.createObjectStore("f",{keyPath:"id"}); }catch(e){} };
    r.onsuccess=()=>{ MUDB.db=r.result; res(r.result); };
    r.onerror=()=>rej(r.error||new Error("idb"));
  });
}
function muIdbTx(mode,fn){
  return muIdb().then(db=>new Promise((res,rej)=>{
    const tx=db.transaction("f",mode), st=tx.objectStore("f"); let out;
    try{ out=fn(st); }catch(e){ return rej(e); }
    tx.oncomplete=()=>res(out&&out.result!==undefined?out.result:out);
    tx.onerror=()=>rej(tx.error||new Error("idb")); tx.onabort=()=>rej(tx.error||new Error("Meel kuma filna"));
  }));
}
function muIdbAll(){
  return muIdb().then(db=>new Promise((res,rej)=>{
    const out=[], rq=db.transaction("f","readonly").objectStore("f").openCursor();
    rq.onsuccess=()=>{ const c=rq.result; if(c){ const v=c.value; out.push(v); c.continue(); } else res(out); };
    rq.onerror=()=>rej(rq.error);
  }));
}
function muIdbPatch(id,patch){
  return muIdbTx("readwrite",st=>{ const g=st.get(id); g.onsuccess=()=>{ if(g.result) st.put(Object.assign(g.result,patch)); }; return null; });
}
async function muLocLoad(){
  try{
    const all=await muIdbAll(); all.sort((a,b)=>(a.ts||0)-(b.ts||0));
    MU.loc=all.map(v=>{ MU.blobs[v.id]=v.blob; return {id:v.id,k:"lo",t:v.t,a:"📱 telefoonkan",c:v.c,sz:v.sz,ts:v.ts}; });
  }catch(e){ MU.loc=[]; }
}

/* ---- liiska ---- */
function muAll(){
  return MU.items.map(x=>Object.assign({key:"s:"+x.id},x)).concat(MU.loc.map(x=>Object.assign({key:"l:"+x.id},x)));
}
function muCatList(c){ return muAll().filter(x=>x.c===c); }
function muFind(key){ return muAll().find(x=>x.key===key)||null; }
async function muLoad(force){
  if(MU.loading || (MU.loaded && !force)) return;
  MU.loading=true;
  try{
    const r=await fetch("/api/music",{headers:{"Accept":"application/json"}});
    if(r.status===401){ location.href="/login"; return; }
    const d=await r.json(); if(d.ok && Array.isArray(d.items)) MU.items=d.items;
  }catch(e){ muMsg("⚠️ Liiska lama soo qaadi karo — internet-ka hubi.",1); }
  await muLocLoad();
  MU.loaded=true; MU.loading=false;
  if(!MU.cur && MU.last){ const it=muFind(MU.last); if(it){ MU.cur=it.key; MU.it=it; MU.kind=it.k==="ytl"?"yt":it.k; muQueue(it); } }
  muRender(); muBar();
}
function muQueue(it){ MU.q=muCatList(it.c).map(x=>x.key); MU.idx=MU.q.indexOf(it.key); }
function muBgUrl(u){ return /^https:[/][/][A-Za-z0-9.-]+[/][^"'()\\s]+$/.test(u||"")?"background-image:url('"+u+"')":""; }
function muRender(){
  const L=mu$("muList"); if(!L) return;
  ["SO","WO","MY"].forEach(c=>{ const n=muCatList(c).length; const e=mu$("muN"+c); if(e) e.textContent=n?String(n):""; });
  document.querySelectorAll("#muSeg button").forEach(b=>b.classList.toggle("on",b.dataset.c===MU.cat));
  document.querySelectorAll("#muCats button[data-c]").forEach(b=>b.classList.toggle("on",b.dataset.c===MU.addC));
  mu$("muLH").textContent=(MU.cat==="SO"?"🇸🇴 ":MU.cat==="WO"?"🌍 ":"⭐ ")+MU_CN[MU.cat].toUpperCase()+" · LIISKAYGA";
  const rows=muCatList(MU.cat);
  L.classList.toggle("edit",MU.edit); mu$("muEdit").classList.toggle("on",MU.edit); mu$("muEdit").textContent=MU.edit?"✓ Dhammee":"✎ Habee";
  if(!rows.length){
    L.innerHTML='<div class="muempty">'+(MU.loaded?'Liiskan weli hees kuma jirto.<br>👇 Fanaan hoose ka taabo → <b>YouTube</b> ayaa kuu raadinaya → <b>Share → Copy link</b> → halkan ku dheji → <b>+ KU DAR</b>.<br>Ama <b>📱 MP3 telefoonka</b> — bass-ka app-ka ayaa ka shaqeeya.':'Waa la soo qaadayaa…')+'</div>';
    return;
  }
  L.innerHTML=rows.map((x,i)=>{
    const s=MU_SRC[x.k]||["yt","?"], now=x.key===MU.cur;
    const sub=(now?(MU.playing?"▶ hadda waa socotaa":"⏸ la joojiyay"):((x.a?x.a+" · ":"")+(x.k==="ytl"?"playlist":x.k==="sp"?(String(x.r||"").split(":")[0]):x.k==="lo"?(x.sz?(x.sz/1048576).toFixed(1)+" MB · BASS ✓":"BASS ✓"):"")));
    const th=x.k==="lo"?"":muBgUrl(x.th);
    return '<div class="muit'+(now?' now':'')+'" data-k="'+esc(x.key)+'" role="button" tabindex="0">'
      +'<span class="th" style="'+th+'">'+(th?"":(x.k==="lo"?"📱":"♪"))+'</span>'
      +'<span class="tt"><b>'+(now?"▶ ":"")+esc(x.t||"—")+'</b><small>'+esc(sub)+'</small></span>'
      +'<span class="musrc '+s[0]+'">'+s[1]+'</span>'
      +'<span class="ed"><button type="button" data-a="up" aria-label="Kor">↑</button><button type="button" data-a="dn" aria-label="Hoos">↓</button>'
      +'<button type="button" data-a="ren" aria-label="Magac beddel">✎</button><button type="button" data-a="cat" aria-label="Liis kale u wareeji">⇄</button>'
      +'<button type="button" class="x" data-a="del" aria-label="Tirtir">'+(MU.delArm===x.key?"✓?":"🗑")+'</button></span></div>';
  }).join("");
}
function muChips(){
  const C=mu$("muChips"); if(!C) return;
  mu$("muWhere").textContent=MU.where==="sp"?"Spotify ⇄":"YouTube ⇄";
  mu$("muRH").textContent=MU.cat==="SO"?"RAADI · FANAANIINTA SOOMAALIDA":MU.cat==="WO"?"RAADI · ADDUUNKA":"RAADI";
  C.innerHTML=(MU_CH[MU.cat]||[]).map(n=>{
    const q=MU.cat==="SO"&&!/cusub/.test(n)?n+" heesaha":n;
    const u=MU.where==="sp"?"https://open.spotify.com/search/"+encodeURIComponent(q):"https://www.youtube.com/results?search_query="+encodeURIComponent(q);
    return '<a href="'+esc(u)+'" target="_blank" rel="noopener">'+esc(n)+'</a>';
  }).join("")+'<a class="alt" href="'+(MU.where==="sp"?"https://open.spotify.com/search":"https://www.youtube.com/")+'" target="_blank" rel="noopener">+ raadi</a>';
}

/* ---- server ops ---- */
async function muOp(body){
  try{
    const r=await fetch("/api/music",{method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},body:JSON.stringify(body)});
    if(r.status===401){ location.href="/login"; return null; }
    const d=await r.json();
    if(Array.isArray(d.items)) MU.items=d.items;
    if(!d.ok){ muMsg("⚠️ "+(d.error||"Khalad"),1); muRender(); return null; }
    return d;
  }catch(e){ muMsg("⚠️ Internet ma jiro — mar kale isku day.",1); return null; }
}
async function muAdd(){
  const inp=mu$("muUrl"), url=(inp.value||"").trim();
  if(!url){ muMsg("Marka hore link-ga ku dheji (📋).",1); inp.focus(); return; }
  const b=mu$("muAdd"); b.disabled=true; muMsg("⏳ Waa la darayaa…");
  const d=await muOp({op:"add",url:url,c:MU.addC});
  b.disabled=false;
  if(d){ inp.value=""; MU.cat=MU.addC; muSave(); muMsg("✓ Waa lagu daray: "+((d.item&&d.item.t)||"hees")); muRender(); muChips(); }
}
async function muPaste(){
  try{
    const t=await navigator.clipboard.readText();
    if(t){ mu$("muUrl").value=t.trim(); muMsg(""); return; }
  }catch(e){}
  muMsg("Sanduuqa farta ku hay → Paste.",1); mu$("muUrl").focus();
}
async function muFiles(fl){
  const files=[...(fl||[])].filter(f=>f && (/^audio[/]/.test(f.type||"") || /[.](mp3|m4a|aac|ogg|oga|wav|flac|opus)$/i.test(f.name||"")));
  if(!files.length){ muMsg("Fayl cod ah lama dooran (MP3 · M4A …).",1); return; }
  muMsg("⏳ "+files.length+" hees ayaa la kaydinayaa…");
  let ok=0, big=0;
  for(let i=0;i<files.length;i++){
    const f=files[i]; if(f.size>80*1048576){ big++; continue; }
    const id=Math.random().toString(16).slice(2,10).padEnd(8,"0");
    const rec={id:id,t:String(f.name||"Hees").replace(/[.][A-Za-z0-9]{2,5}$/,"").replace(/[_]+/g," ").slice(0,120),c:MU.addC,sz:f.size,ts:Date.now()+i,blob:f};
    try{ await muIdbTx("readwrite",st=>st.put(rec)); MU.blobs[id]=f; MU.loc.push({id:id,k:"lo",t:rec.t,a:"📱 telefoonkan",c:rec.c,sz:rec.sz,ts:rec.ts}); ok++; }
    catch(e){ muMsg("⚠️ Telefoonka meel kuma filna — heesaha qaar tirtir.",1); break; }
  }
  try{ if(navigator.storage && navigator.storage.persist) navigator.storage.persist(); }catch(e){}
  if(ok){ MU.cat=MU.addC; muSave(); muMsg("✓ "+ok+" hees MP3 ah ayaa la daray (telefoonkan keliya) · BASS ✓"+(big?" · "+big+" aad u weyn (>80MB)":"")); }
  muRender();
}

/* ---- player-ka ---- */
function muHostInit(){
  const h=mu$("muHost"); if(!h || h.dataset.ok) return;
  h.dataset.ok="1";
  h.innerHTML='<div id="muYTw" style="width:100%;height:100%;display:none"><div id="muYT"></div></div>'
    +'<div id="muSPw" style="display:none"><div id="muSP"></div></div><div id="muAMw" style="display:none"></div>'
    +'<div id="muLOw" class="lo" style="display:none"><span class="d" id="muDisc"></span><span class="tx"><b id="muLOt">—</b><small id="muLOs">📱 MP3 · BASS</small></span></div>';
}
function muShowKind(){
  muHostInit();
  const k=MU.kind;
  mu$("muYTw").style.display=k==="yt"?"block":"none"; mu$("muSPw").style.display=k==="sp"?"block":"none";
  mu$("muAMw").style.display=k==="am"?"block":"none"; mu$("muLOw").style.display=k==="lo"?"flex":"none";
  const s=mu$("muSlot"); s.className="muslot"+(k==="sp"?" sp":k==="am"?" am":k==="lo"?" lo":"");
  s.innerHTML=k?(k==="lo"?"":'<span class="big">⏳</span><span>'+(MU_SRC[k]||["",""])[1]+' waa la soo dejinayaa…</span>'):'<span class="big">🎧</span><span>Hees taabo si ay halkan uga shidanto</span>';
  mu$("muHost").classList.toggle("ytm",k==="yt");
}
function muLayout(){
  const h=mu$("muHost"); if(!h) return;
  const open=!mu$("muSheet").hidden, bar=mu$("muBar");
  const live=MU.kind && (MU.kind==="lo" || h.dataset.made);
  if(!live){ h.classList.remove("show"); return; }
  let el=null; const ub=mu$("muBub");
  if(open) el=mu$("muSlot"); else if(!bar.hidden) el=mu$("muArt"); else if(ub && !ub.hidden) el=ub;   // v12.23: saxan yar
  h.classList.toggle("bub",!open && !!el && el===ub);
  if(!el){ h.classList.remove("show"); return; }
  const r=el.getBoundingClientRect();
  h.style.left=r.left+"px"; h.style.top=r.top+"px"; h.style.width=r.width+"px"; h.style.height=r.height+"px";
  h.classList.add("show"); h.classList.toggle("big",open); h.classList.toggle("mini",!open);
}
function muBar(){
  const b=mu$("muBar"), ub=mu$("muBub"); if(!b) return;
  let bub=false, dock=true; try{ bub=(MUP.mode==="bub"); dock=(MUP.y===null); }catch(e){}   // v12.23
  const has=!!MU.cur, show=has && !bub, was=!b.hidden;
  b.hidden=!show; if(ub) ub.hidden=!(has && bub);
  document.body.classList.toggle("mu-on",show && dock);
  try{ mupPlaceBar(); mupPlaceBub(); }catch(e){ const ab=document.querySelector(".appbar"); if(ab) b.style.bottom=(ab.offsetHeight+6)+"px"; }
  const it=MU.it;
  mu$("muT").textContent=it?(it.t||"—"):"—";
  const art=mu$("muArt"); art.setAttribute("style",it&&it.k!=="lo"?muBgUrl(it.th):""); art.textContent=it&&(it.k==="lo"||!it.th)?(it.k==="lo"?"📱":"🎵"):"";
  muTick();
  if(show!==was){ try{ dispatchEvent(new Event("resize")); }catch(e){} }
  try{ chToastPos(); }catch(e){}
  muLayout();
}
function muTick(){
  const a=mu$("muAudio");
  if(MU.kind==="yt" && MU.yt && MU.ytReady){
    try{ MU.tm=MU.yt.getCurrentTime()||0; MU.du=MU.yt.getDuration()||0; MU.playing=MU.yt.getPlayerState()===1; }catch(e){}
  }else if(MU.kind==="lo"){ MU.tm=a.currentTime||0; MU.du=isFinite(a.duration)?a.duration:0; MU.playing=!a.paused; }
  const s=MU_SRC[MU.it?MU.it.k:"yt"]||["","—"];
  const sub=(MU.kind==="lo"?(MU.eq.b>0?"🔊 +"+MU.eq.b+" dB":"📱 MP3"):(MU.it?s[1]:"—"))+(MU.du?" · "+muFmt(MU.tm)+"/"+muFmt(MU.du):(MU.kind==="am"?" · player-ka ku riix ▶":""));
  const e=mu$("muS"); if(e) e.textContent=sub;
  const pb=mu$("muPb"); if(pb) pb.style.width=(MU.du?Math.min(100,MU.tm/MU.du*100):0)+"%";
  ["muPlay","muPlay2"].forEach(id=>{ const b=mu$(id); if(b) b.textContent=MU.playing?"⏸":"▶"; });
  const d=mu$("muDisc"); if(d) d.classList.toggle("spin",MU.kind==="lo"&&MU.playing);
  const nm=mu$("navMus"); if(nm) nm.classList.toggle("play",!!MU.playing);   // v12.21
  const ubp=mu$("muBub"); if(ubp) ubp.classList.toggle("play",!!MU.playing);   // v12.23
  if("mediaSession" in navigator){ try{ navigator.mediaSession.playbackState=MU.playing?"playing":"paused"; }catch(e){} }
}
function muHalt(keep){
  if(keep!=="yt" && MU.yt && MU.ytReady){ try{ MU.yt.stopVideo(); }catch(e){} }
  if(keep!=="sp" && MU.sp){ try{ MU.sp.pause(); }catch(e){} }
  if(keep!=="lo"){ const a=mu$("muAudio"); try{ a.pause(); }catch(e){} }
  if(keep!=="am"){ const w=mu$("muAMw"); if(w) w.innerHTML=""; }
}
function muLoadScript(src){
  return new Promise((res,rej)=>{
    const s=document.createElement("script"); s.src=src; s.async=true;
    s.onload=()=>res(s); s.onerror=()=>{ s.remove(); rej(new Error("internet")); }; document.head.appendChild(s);
  });
}
function muYTApi(){
  if(window.YT && window.YT.Player) return Promise.resolve();
  if(MU.ytP) return MU.ytP;
  MU.ytP=new Promise((res,rej)=>{
    const prev=window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady=function(){ try{ if(prev) prev(); }catch(e){} res(); };
    muLoadScript("https://www.youtube.com/iframe_api").catch(()=>{ MU.ytP=null; rej(new Error("YouTube lama soo dejin karo — internet-ka hubi.")); });
    setTimeout(()=>{ if(!(window.YT && window.YT.Player)){ MU.ytP=null; rej(new Error("YouTube wuu daahay — mar kale ▶ riix.")); } },15000);
  });
  return MU.ytP;
}
function muYTState(e){
  if(!e) return;
  if(e.data===1){ MU.playing=true; if(MU.duck) MU.yt.setVolume(Math.round(MU.eq.v*0.25)); }
  else if(e.data===2) MU.playing=false;
  else if(e.data===0){
    MU.playing=false;
    if(MU.it && MU.it.k==="ytl"){
      try{ const pl=MU.yt.getPlaylist()||[]; if(MU.yt.getPlaylistIndex()<pl.length-1) return; }catch(err){}
    }
    muEnded();
  }
  muTick(); muRender();
}
function muYTErr(e){
  const c=e&&e.data;
  muMsg(c===101||c===150?"⚠️ Milkiilaha heestan YouTube-ka kuma ogola in meel kale laga shido — waa la dhaafayaa.":"⚠️ Heestan lama shidi karo (link khaldan ama la tirtiray) — waa la dhaafayaa.",1);
  const tok=MU.tok; setTimeout(()=>{ if(tok===MU.tok && MU.q.length>1) muNext(true); },2500);
}
async function muPlayYT(it,tok){
  await muYTApi(); if(tok!==MU.tok) return;
  const pv={playsinline:1,rel:0,modestbranding:1,autoplay:1,fs:0,iv_load_policy:3,origin:location.origin};
  if(!MU.yt){
    await new Promise((res,rej)=>{
      const o={host:"https://www.youtube-nocookie.com",width:"100%",height:"100%",playerVars:pv,
        events:{onReady:()=>{ MU.ytReady=true; try{ MU.yt.setVolume(MU.eq.v); MU.yt.playVideo(); }catch(e){} res(); },onStateChange:muYTState,onError:muYTErr}};
      if(it.k==="yt") o.videoId=it.r; else { pv.listType="playlist"; pv.list=it.r; }
      MU.yt=new window.YT.Player("muYT",o);
      mu$("muHost").dataset.made="1"; muLayout();
      setTimeout(()=>{ if(!MU.ytReady) rej(new Error("YouTube wuu daahay — mar kale ▶ riix.")); },15000);
    });
  }else{
    if(it.k==="yt") MU.yt.loadVideoById(it.r); else MU.yt.loadPlaylist({list:it.r,listType:"playlist",index:0});
    try{ MU.yt.setVolume(MU.eq.v); }catch(e){}
  }
}
function muSPApi(){
  if(MU.spApi) return Promise.resolve(MU.spApi);
  if(MU.spP) return MU.spP;
  MU.spP=new Promise((res,rej)=>{
    window.onSpotifyIframeApiReady=function(api){ MU.spApi=api; res(api); };
    muLoadScript("https://open.spotify.com/embed/iframe-api/v1").catch(()=>{ MU.spP=null; rej(new Error("Spotify lama soo dejin karo — internet-ka hubi.")); });
    setTimeout(()=>{ if(!MU.spApi){ MU.spP=null; rej(new Error("Spotify wuu daahay — mar kale ▶ riix.")); } },15000);
  });
  return MU.spP;
}
function muSPUpd(e){
  const d=(e&&e.data)||{}; if(MU.kind!=="sp") return;
  MU.playing=!d.isPaused; MU.tm=(d.position||0)/1000; MU.du=(d.duration||0)/1000;
  const tr=MU.it && /^(track|episode):/.test(MU.it.r||"");
  if(tr && MU.spNear && d.isPaused){ MU.spNear=false; muEnded(); return; }
  MU.spNear=!!(tr && d.duration>0 && d.position>=d.duration-1500 && !d.isPaused);
  muTick();
}
async function muPlaySP(it,tok){
  const api=await muSPApi(); if(tok!==MU.tok) return;
  const uri="spotify:"+it.r;
  if(!MU.sp){
    await new Promise(res=>{
      api.createController(mu$("muSP"),{uri:uri,width:"100%",height:152},c=>{
        MU.sp=c; c.addListener("playback_update",muSPUpd);
        c.addListener("ready",()=>{ try{ if(MU.kind==="sp") c.play(); }catch(e){} });
        res();
      });
      mu$("muHost").dataset.made="1"; muLayout();
    });
  }else{ MU.sp.loadUri(uri); setTimeout(()=>{ try{ if(MU.kind==="sp") MU.sp.play(); }catch(e){} },900); }
}
function muPlayAM(it){
  const p=String(it.r||"").split("/"); if(p.length<3) throw new Error("Audiomack link khaldan");
  const w=mu$("muAMw");
  w.innerHTML='<iframe title="Audiomack" src="https://audiomack.com/embed/'+encodeURIComponent(p[0])+"/"+encodeURIComponent(p[1])+"/"+encodeURIComponent(p.slice(2).join("/"))
    +'?background=1" width="100%" height="252" allow="autoplay; encrypted-media" loading="lazy"></iframe>';
  mu$("muHost").dataset.made="1"; MU.playing=false;
  muMsg("Audiomack: player-ka dhexdiisa ▶ ku riix.");
}
function muPlayLO(it){
  const a=mu$("muAudio"), bl=MU.blobs[it.id];
  if(!bl) throw new Error("Faylka MP3 telefoonkan kuma jiro.");
  if(MU.urlId!==it.id){ if(MU.url) URL.revokeObjectURL(MU.url); MU.url=URL.createObjectURL(bl); MU.urlId=it.id; a.src=MU.url; }
  mu$("muLOt").textContent=it.t||"—";
  muEqInit();
  const p=a.play(); if(p && p.catch) p.catch(()=>{ MU.playing=false; muTick(); });
}
function muStart(it){
  const tok=++MU.tok;
  MU.cur=it.key; MU.it=it; MU.kind=it.k==="ytl"?"yt":it.k; MU.tm=0; MU.du=0; MU.playing=false; MU.spNear=false;
  muSave(); muMsg("");
  muHalt(MU.kind); muShowKind(); muBar(); muRender(); muMeta(it); muEqStatus();
  try{
    if(it.k==="lo") muPlayLO(it);
    else if(it.k==="am") muPlayAM(it);
    else (MU.kind==="yt"?muPlayYT(it,tok):muPlaySP(it,tok)).then(()=>{ if(tok===MU.tok){ muLayout(); muTick(); } })
      .catch(e=>{ if(tok!==MU.tok) return; const t=(e&&e.message)||"Lama shidi karo"; muMsg("⚠️ "+t,1);
        const sl=mu$("muSlot"); if(sl && !mu$("muHost").dataset.made) sl.innerHTML='<span class="big">📡</span><span>'+esc(t)+'</span>'; });
  }catch(e){ muMsg("⚠️ "+((e&&e.message)||"Lama shidi karo"),1); }
  muLayout();
}
function muPlayKey(key,keepQ){
  const it=muFind(key); if(!it) return;
  if(!keepQ) muQueue(it); else MU.idx=MU.q.indexOf(key);
  muStart(it);
}
function muToggle(){
  if(!MU.cur){ const L=muCatList(MU.cat); if(!L.length){ muOpen(); return; } muPlayKey(L[0].key); return; }
  const live=MU.kind==="lo" || mu$("muHost").dataset.made;
  if(MU.kind==="yt" && MU.yt && MU.ytReady && live){ try{ if(MU.yt.getPlayerState()===1) MU.yt.pauseVideo(); else MU.yt.playVideo(); }catch(e){} }
  else if(MU.kind==="sp" && MU.sp){ try{ MU.sp.togglePlay(); }catch(e){} }
  else if(MU.kind==="lo" && MU.urlId===(MU.it&&MU.it.id)){ muEqInit(); const a=mu$("muAudio"); if(a.paused){ const p=a.play(); if(p&&p.catch) p.catch(()=>{}); } else a.pause(); }
  else if(MU.kind==="am" && mu$("muAMw").innerHTML){ muOpen(); muMsg("Audiomack: player-ka dhexdiisa ▶ / ⏸ ku riix."); }
  else { const it=muFind(MU.cur); if(it) muPlayKey(it.key); }
  setTimeout(muTick,250);
}
function muNext(auto){
  if(!MU.q.length){ const it=muFind(MU.cur); if(it) muQueue(it); }
  const n=MU.q.length; if(!n) return;
  let i;
  if(MU.shuf && n>1){ do{ i=Math.floor(Math.random()*n); }while(i===MU.idx); }
  else{ i=MU.idx+1; if(i>=n){ if(MU.rep===1 || !auto) i=0; else { MU.playing=false; muTick(); return; } } }
  MU.idx=i; muPlayKey(MU.q[i],true);
}
function muPrev(){
  if(MU.tm>4 && (MU.kind==="yt"||MU.kind==="lo")){
    if(MU.kind==="yt"){ try{ MU.yt.seekTo(0,true); }catch(e){} } else mu$("muAudio").currentTime=0;
    return;
  }
  if(!MU.q.length) return;
  MU.idx=(MU.idx-1+MU.q.length)%MU.q.length; muPlayKey(MU.q[MU.idx],true);
}
function muEnded(){
  if(MU.rep===2){
    if(MU.kind==="yt"){ try{ MU.yt.seekTo(0,true); MU.yt.playVideo(); }catch(e){} return; }
    if(MU.kind==="lo"){ const a=mu$("muAudio"); a.currentTime=0; a.play().catch(()=>{}); return; }
    if(MU.kind==="sp"){ try{ MU.sp.seek(0); MU.sp.play(); }catch(e){} return; }
  }
  if(MU.opt.auto) muNext(true); else { MU.playing=false; muTick(); }
}
function muMeta(it){
  if(!("mediaSession" in navigator) || !window.MediaMetadata) return;
  try{
    const art=it.th&&it.k!=="lo"?[{src:it.th,sizes:"320x180"}]:[{src:"/icons/icon-192.png",sizes:"192x192",type:"image/png"}];
    navigator.mediaSession.metadata=new MediaMetadata({title:it.t||"Hees",artist:it.a||(MU_SRC[it.k]||["",""])[1],album:"MOHA PRO · "+MU_CN[it.c||"MY"],artwork:art});
  }catch(e){}
}
if("mediaSession" in navigator){
  try{
    navigator.mediaSession.setActionHandler("play",()=>{ if(!MU.playing) muToggle(); });
    navigator.mediaSession.setActionHandler("pause",()=>{ if(MU.playing) muToggle(); });
    navigator.mediaSession.setActionHandler("nexttrack",()=>muNext(false));
    navigator.mediaSession.setActionHandler("previoustrack",()=>muPrev());
  }catch(e){}
}
(function(){
  const a=mu$("muAudio"); if(!a) return;
  a.addEventListener("play",()=>{ MU.playing=true; muTick(); muRender(); });
  a.addEventListener("pause",()=>{ MU.playing=false; muTick(); muRender(); });
  a.addEventListener("ended",()=>{ MU.playing=false; muEnded(); });
  a.addEventListener("error",()=>{ if(MU.kind==="lo") muMsg("⚠️ Faylkan lama shidi karo (nooc aan la taageerin).",1); });
})();

/* ---- 🔊 BASS MACAAN: Web Audio (MP3-yada telefoonka) ---- */
function muEqInit(){
  if(MU.ac){ if(MU.ac.state==="suspended") MU.ac.resume().catch(()=>{}); return true; }
  const AC=window.AudioContext||window.webkitAudioContext; if(!AC) return false;
  try{
    const ac=new AC(), src=ac.createMediaElementSource(mu$("muAudio"));
    const pre=ac.createGain(), sub=ac.createBiquadFilter(), low=ac.createBiquadFilter(), mud=ac.createBiquadFilter(),
          hi=ac.createBiquadFilter(), comp=ac.createDynamicsCompressor(), vol=ac.createGain(), an=ac.createAnalyser();
    sub.type="peaking"; sub.frequency.value=50; sub.Q.value=1.1;          // gunta (sub) — "dhulka ka dhaqaaqa"
    low.type="lowshelf"; low.frequency.value=105;                          // bass-ka
    mud.type="peaking"; mud.frequency.value=320; mud.Q.value=1.0;          // "qasnaanta" ka saar -> bass macaan oo cad
    hi.type="highshelf"; hi.frequency.value=3800;                          // treble — codka fanaanka
    comp.threshold.value=-14; comp.knee.value=12; comp.ratio.value=4; comp.attack.value=0.006; comp.release.value=0.22;   // ma qaylinayo / ma jabayo
    an.fftSize=64; an.smoothingTimeConstant=0.75;
    src.connect(pre); pre.connect(sub); sub.connect(low); low.connect(mud); mud.connect(hi); hi.connect(comp); comp.connect(vol); vol.connect(an); an.connect(ac.destination);
    MU.ac=ac; MU.nodes={pre:pre,sub:sub,low:low,mud:mud,hi:hi,vol:vol,an:an};
    if(ac.state==="suspended") ac.resume().catch(()=>{});
    muEqApply(); return true;
  }catch(e){ return false; }
}
function muEqApply(){
  const e=MU.eq, sg=v=>(v>0?"+":"")+v+" dB";
  const set=(id,v,t)=>{ const i=mu$(id); if(i && Number(i.value)!==v) i.value=v; const b=mu$(id+"V"); if(b) b.textContent=t; };
  set("muBass",e.b,sg(e.b)); set("muSub",e.s,sg(e.s)); set("muTre",e.t,sg(e.t)); set("muVol",e.v,e.v+"%");
  document.querySelectorAll("#muPre button").forEach(b=>b.classList.toggle("on",Number(b.dataset.p)===e.p));
  if(MU.yt && MU.ytReady){ try{ MU.yt.setVolume(Math.round(e.v*(MU.duck?0.25:1))); }catch(err){} }
  const n=MU.nodes; if(!n) return;
  const t=MU.ac.currentTime, boost=Math.max(0,e.b+e.s*0.6);
  n.low.gain.setTargetAtTime(e.b,t,0.05); n.sub.gain.setTargetAtTime(e.s,t,0.05); n.hi.gain.setTargetAtTime(e.t,t,0.05);
  n.mud.gain.setTargetAtTime(e.b>0?-Math.min(4,e.b*0.3):0,t,0.05);
  n.pre.gain.setTargetAtTime(Math.pow(10,-(boost*0.45)/20),t,0.05);                // meel bannaan (headroom) -> bass-ku ma dildilaaco
  n.vol.gain.setTargetAtTime((e.v/100)*(MU.duck?0.25:1)*(boost>0?1.15:1),t,0.08);
}
function muEqStatus(){
  const s=mu$("muEqSt"); if(!s) return;
  const k=MU.it?MU.it.k:"";
  s.style.color="";
  if(k==="lo"){ s.className="st ok"; s.innerHTML="✅ <b>BASS-ku hadda wuu shaqeynayaa</b> — heestan 📱 MP3 ah. Isku day <b>Bass macaan</b> ama <b>Bass xoog</b>."; }
  else if(k){ s.className="st no"; s.innerHTML="⚠️ <b>"+(MU_SRC[k]||["",""])[1]+"</b> codkiisa app-ku ma beddeli karo (browser-ka ayaa diida — ilaalinta YouTube/Spotify). Bass-ka 👇 <b>telefoonka</b> ka shid, ama hees <b>📱 MP3</b> ah dooro. <b>Codka</b> (volume) wuu shaqeeyaa."; const g=mu$("muGd"); if(g) g.open=true; }
  else { s.className="st"; s.style.color="var(--ink3)"; s.innerHTML="BASS-ka app-ku wuxuu ka shaqeeyaa heesaha <b>📱 MP3 telefoonka</b>. YouTube / Spotify → equalizer-ka telefoonka 👇"; }
}
function muViz(){
  const box=mu$("muViz"); if(!box) return;
  if(!box.children.length) box.innerHTML="<i></i>".repeat(16);
  const bars=box.children, arr=new Uint8Array(32);
  const loop=()=>{
    if(mu$("muSheet").hidden){ MU.viz=0; return; }
    const live=MU.nodes&&MU.kind==="lo"&&MU.playing;
    if(live) MU.nodes.an.getByteFrequencyData(arr);
    for(let i=0;i<16;i++){ const v=live?arr[i]/255:0.04; bars[i].style.height=Math.max(4,Math.round(v*100))+"%"; }
    MU.viz=requestAnimationFrame(loop);
  };
  if(!MU.viz) MU.viz=requestAnimationFrame(loop);
}

/* ---- 🔔 trade xidhmay -> codka hoos u dhig + dhawaq yar ---- */
function muChime(){
  try{
    const AC=window.AudioContext||window.webkitAudioContext; if(!AC) return;
    const ac=MU.ac||(MU.ac2=MU.ac2||new AC()); const t=ac.currentTime+0.02;
    [[880,0],[1318.5,0.16]].forEach(([f,d])=>{ const o=ac.createOscillator(), g=ac.createGain(); o.type="sine"; o.frequency.value=f;
      g.gain.setValueAtTime(0.0001,t+d); g.gain.exponentialRampToValueAtTime(0.18,t+d+0.02); g.gain.exponentialRampToValueAtTime(0.0001,t+d+0.45);
      o.connect(g); g.connect(ac.destination); o.start(t+d); o.stop(t+d+0.5); });
  }catch(e){}
}
function muDuck(){
  if(!MU.opt.duck || !MU.playing) return;
  MU.duck=true; muEqApply(); muChime();
  const spWas=MU.kind==="sp"; if(spWas){ try{ MU.sp.pause(); }catch(e){} }
  clearTimeout(MU.duckT);
  MU.duckT=setTimeout(()=>{ MU.duck=false; muEqApply(); if(spWas && MU.kind==="sp"){ try{ MU.sp.resume(); }catch(e){} } },4000);
}
function muOnState(d,x){
  const ot=Number(x && x.opentrades), acc=String((d&&d.account)||"");
  if(!isFinite(ot)) return;
  if(MU.lastOT!==null && acc===MU.lastAcc && ot<MU.lastOT) muDuck();
  MU.lastOT=ot; MU.lastAcc=acc;
}
function muTab(){ if(!MU.opt.keep && MU.playing) muToggle(); }

/* ---- sheet ---- */
function muOpen(){
  const s=mu$("muSheet"); s.hidden=false; document.body.style.overflow="hidden";
  muLoad(); muRender(); muChips(); muEqApply(); muEqStatus(); muShowKind(); muViz();
  requestAnimationFrame(muLayout);
}
function muClose(){ mu$("muSheet").hidden=true; document.body.style.overflow=""; MU.edit=false; MU.delArm=""; muLayout(); }
(function(){
  if(!mu$("muSheet")) return;
  const on=(id,ev,fn)=>{ const e=mu$(id); if(e) e.addEventListener(ev,fn); };
  on("btnMus","click",muOpen); on("muClose","click",muClose);
  ["muArt","muMi"].forEach(id=>{ on(id,"click",muOpen); on(id,"keydown",e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); muOpen(); } }); });
  ["muPlay","muPlay2"].forEach(id=>on(id,"click",muToggle));
  ["muNext","muNext2"].forEach(id=>on(id,"click",()=>muNext(false)));
  ["muPrev","muPrev2"].forEach(id=>on(id,"click",muPrev));
  on("muShuf","click",()=>{ MU.shuf=!MU.shuf; muSave(); muCtl(); });
  on("muRep","click",()=>{ MU.rep=(MU.rep+1)%3; muSave(); muCtl(); });
  on("muEdit","click",()=>{ MU.edit=!MU.edit; MU.delArm=""; muRender(); });
  on("muAdd","click",muAdd); on("muPaste","click",muPaste);
  on("muUrl","keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); muAdd(); } });
  on("muPick","click",()=>mu$("muFile").click());
  on("muFile","change",e=>{ muFiles(e.target.files); e.target.value=""; });
  on("muWhere","click",()=>{ MU.where=MU.where==="sp"?"yt":"sp"; muSave(); muChips(); });
  document.querySelectorAll("#muSeg button").forEach(b=>b.addEventListener("click",()=>{ MU.cat=b.dataset.c; MU.addC=b.dataset.c; MU.edit=false; muSave(); muRender(); muChips(); }));
  document.querySelectorAll("#muCats button[data-c]").forEach(b=>b.addEventListener("click",()=>{ MU.addC=b.dataset.c; muSave(); muRender(); }));
  document.querySelectorAll("#muPre button").forEach(b=>b.addEventListener("click",()=>{ const p=Number(b.dataset.p); Object.assign(MU.eq,MU_PRE[p],{p:p}); muEqInit(); muEqApply(); muSave(); }));
  [["muBass","b"],["muSub","s"],["muTre","t"],["muVol","v"]].forEach(([id,k])=>on(id,"input",e=>{
    MU.eq[k]=Number(e.target.value); if(k!=="v"){ const m=MU_PRE.findIndex(P=>P.b===MU.eq.b&&P.s===MU.eq.s&&P.t===MU.eq.t); MU.eq.p=m; }
    muEqApply(); muSave(); }));
  [["muOKeep","keep"],["muODuck","duck"],["muOAuto","auto"]].forEach(([id,k])=>{
    const b=mu$(id); if(!b) return;
    const paint=()=>{ b.classList.toggle("on",!!MU.opt[k]); b.setAttribute("aria-checked",MU.opt[k]?"true":"false"); };
    paint(); b.addEventListener("click",()=>{ MU.opt[k]=MU.opt[k]?0:1; paint(); muSave(); });
  });
  mu$("muList").addEventListener("click",async e=>{
    const row=e.target.closest(".muit"); if(!row) return;
    const key=row.dataset.k, btn=e.target.closest("button[data-a]");
    if(!btn){ if(!MU.edit){ if(key===MU.cur && (MU.kind!=="yt"||MU.ytReady)) muToggle(); else muPlayKey(key); } return; }
    const a=btn.dataset.a, it=muFind(key); if(!it) return;
    const loc=it.k==="lo";
    if(a==="del"){
      if(MU.delArm!==key){ MU.delArm=key; muRender(); setTimeout(()=>{ if(MU.delArm===key){ MU.delArm=""; muRender(); } },3000); return; }
      MU.delArm="";
      if(key===MU.cur){ muHalt(""); MU.cur=""; MU.it=null; MU.kind=""; MU.playing=false; muShowKind(); muBar(); }
      if(loc){ try{ await muIdbTx("readwrite",st=>st.delete(it.id)); }catch(err){} MU.loc=MU.loc.filter(x=>x.id!==it.id); delete MU.blobs[it.id]; }
      else await muOp({op:"del",id:it.id});
      MU.q=MU.q.filter(k=>k!==key); MU.idx=MU.q.indexOf(MU.cur); muRender(); return;
    }
    if(a==="cat"){
      const c={SO:"WO",WO:"MY",MY:"SO"}[it.c]||"MY";
      if(loc){ await muIdbPatch(it.id,{c:c}).catch(()=>{}); const x=MU.loc.find(y=>y.id===it.id); if(x) x.c=c; }
      else await muOp({op:"cat",id:it.id,c:c});
      muMsg("⇄ "+(it.t||"")+" → "+MU_CN[c]); muRender(); return;
    }
    if(a==="up"||a==="dn"){
      const d=a==="up"?-1:1;
      if(loc){
        const L=MU.loc.filter(x=>x.c===it.c), i=L.findIndex(x=>x.id===it.id), j=i+d;
        if(j<0||j>=L.length) return;
        const A=L[i], B=L[j], ta=A.ts; A.ts=B.ts; B.ts=ta;
        MU.loc.sort((x,y)=>(x.ts||0)-(y.ts||0));
        muIdbPatch(A.id,{ts:A.ts}).catch(()=>{}); muIdbPatch(B.id,{ts:B.ts}).catch(()=>{});
      }else await muOp({op:"move",id:it.id,d:d});
      muRender(); return;
    }
    if(a==="ren"){
      const b=row.querySelector(".tt b"); if(!b || row.querySelector(".tt input")) return;
      const inp=document.createElement("input"); inp.value=it.t||""; inp.maxLength=120;
      inp.style.cssText="width:100%;padding:6px 8px;border-radius:8px;border:1px solid #ec4899;background:#111;color:#fff;font:inherit;font-size:13px";
      b.replaceWith(inp); inp.focus(); inp.select();
      let done=false;
      const fin=async ok=>{
        if(done) return; done=true;
        const t=inp.value.trim();
        if(ok && t && t!==it.t){
          if(loc){ await muIdbPatch(it.id,{t:t}).catch(()=>{}); const x=MU.loc.find(y=>y.id===it.id); if(x) x.t=t; }
          else await muOp({op:"ren",id:it.id,t:t});
          if(MU.it && MU.it.key===key){ MU.it=muFind(key); muBar(); }
        }
        muRender();
      };
      inp.addEventListener("keydown",ev=>{ if(ev.key==="Enter"){ ev.preventDefault(); fin(true); } else if(ev.key==="Escape") fin(false); });
      inp.addEventListener("blur",()=>fin(true));
    }
  });
  mu$("muList").addEventListener("keydown",e=>{ if((e.key==="Enter"||e.key===" ") && e.target.classList && e.target.classList.contains("muit")){ e.preventDefault(); e.target.click(); } });
  addEventListener("keydown",e=>{ if(e.key==="Escape" && !mu$("muSheet").hidden) muClose(); });
  addEventListener("resize",()=>{ const b=mu$("muBar"), ab=document.querySelector(".appbar"); if(b&&ab) b.style.bottom=(ab.offsetHeight+6)+"px"; muLayout(); });
  addEventListener("orientationchange",()=>setTimeout(muLayout,300));
  muCtl();
  MU.tmr=setInterval(()=>{ if(MU.cur) muTick(); },1000);
  if(MU.last) muLoad();
})();
function muCtl(){
  const s=mu$("muShuf"), r=mu$("muRep"); if(!s||!r) return;
  s.classList.toggle("on",MU.shuf); s.setAttribute("aria-pressed",MU.shuf?"true":"false");
  r.classList.toggle("on",MU.rep>0); r.textContent=MU.rep===2?"🔂":"🔁";
  r.setAttribute("aria-label",MU.rep===2?"Ku celi heestan":MU.rep===1?"Ku celi liiska":"Ku celin maya");
}


/* ================= v12.22 (EA v70.8): 💱 LAMAANAHA + 🎯 ZONE YAR ================= */
let ZN=null, PRD=null, PRF="", PRL=null, prDirty=false, prOpen=-1, prPend=null, mZnTouched=false;
const mZn={on:1,tf:1,sh:1,t:3,tp:0};
const PR_STN=["SR/SD","SMC","LABADA"], PR_XN=["dansan","furmaya…","socda","daminaya","khalad"];
const PR_CTL=!!document.querySelector('.appbar button[data-tab="Maamul"]');
function prFlag(s){
  const u=String(s||"").toUpperCase();
  if(/^XAU|GOLD/.test(u)) return "🥇"; if(/^XAG|SILVER/.test(u)) return "🥈"; if(/^BTC/.test(u)) return "₿"; if(/^ETH/.test(u)) return "Ξ";
  if(/OIL|WTI|BRENT/.test(u)) return "🛢️"; if(/US30|NAS|SPX|US500|US100|DJ|GER|DAX|UK100|JP225/.test(u)) return "📈";
  if(/^EUR/.test(u)) return "💶"; if(/^GBP/.test(u)) return "💷"; if(/^USDJPY|^JPY/.test(u)) return "💴"; if(/^USD/.test(u)) return "💵";
  return "💱";
}
function prMoney(v){ v=Number(v)||0; return (v>=0?"+":"−")+"$"+Math.abs(v).toFixed(2); }
function prSp(p){ const v=Number(p.sp)||0, dg=Number(p.dg); return (isFinite(dg)&&dg>=0&&dg<=8)?v.toFixed(dg):String(v); }
async function cfgPost(values){
  const body={values:values}; if(accSel) body.account=accSel.value;
  try{
    const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json(); return d.ok?"":(d.error||"Lama kaydin.");
  }catch(e){ return "Internet ma jiro — lama kaydin."; }
}

/* ---- 💱 xariiqda lamaanaha (Guud) ---- */
function prStripPaint(){
  const s=$("#prStrip"), fl=$("#prFlt"); if(!s) return;
  const l=(PRD&&Array.isArray(PRD.l))?PRD.l.filter(p=>p.mm||p.on||Number(p.x)===3):[];
  if(l.length<2){ s.hidden=true; if(PRF){ PRF=""; } if(fl) fl.hidden=true; return; }
  if(PRF && !l.some(p=>p.s===PRF)) PRF="";
  s.hidden=false;
  s.innerHTML=l.map(p=>{
    const x=Number(p.x)||0, pl=Number(p.pl)||0;
    const em=(x===1)?"furmaya":((x===4)?"khalad":(pl?prMoney(pl):(p.n?p.n+" trade":"")));
    return '<button type="button" class="prc x'+x+(PRF===p.s?' on':'')+'" data-s="'+esc(p.s)+'" role="tab" aria-selected="'+(PRF===p.s)+'"><i></i>'+prFlag(p.s)+' '+esc(p.s)+(p.mm?' 👑':'')
      +(em?' <em class="'+(pl>0?'p':(pl<0?'n':''))+'">'+esc(em)+'</em>':'')+((!p.mm && Number(p.cf)>0 && Number(p.fl)>0 && Math.abs(Number(p.cf)-1)>1e-9)?' <u>×'+prFx(p.cf)+'</u>':'')+'</button>';   // v12.24: 📏
  }).join("")+(PR_CTL?'<button type="button" class="prc add" data-add="1" aria-label="Ku dar lamaane">＋</button>':'');
  if(fl){ fl.hidden=!PRF; if(PRF) $("#prFltS").textContent=PRF; }
}
if($("#prStrip")) $("#prStrip").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b) return;
  if(b.dataset.add){ tab("Maamul"); prAddOpen(true); setTimeout(()=>{ const c=$("#prCard"); if(c) c.scrollIntoView({block:"start"}); },60); return; }
  PRF=(PRF===b.dataset.s)?"":b.dataset.s; prStripPaint(); tick();
});
if($("#prFltX")) $("#prFltX").addEventListener("click",()=>{ PRF=""; prStripPaint(); tick(); });

/* ---- 💱 LAMAANAHA (Maamul) ---- */
function prKey(l){ return (l||[]).map(p=>p.s+":"+(p.on?1:0)+":"+p.tf+":"+p.st+":"+p.rk+":"+((p.fl|0)&7)+":"+prCmS(p.cm)).join(";"); }   // v12.24: + TICK/BASKET · cabbir
function prFromLive(){
  const l=(PRD&&Array.isArray(PRD.l))?PRD.l:null; if(!l) return null;
  return l.filter(p=>!p.mm).map(p=>({s:String(p.s),on:p.on?1:0,tf:(Number(p.tf)===1?1:5),st:Math.max(0,Math.min(2,Number(p.st)||0)),rk:Math.max(0,Math.min(5,Number(p.rk)||0)),fl:prFlOf(p),cm:prCmOf(p)}));
}
function prSeed(){
  if(prDirty) return;
  const live=prFromLive();
  if(prPend && Date.now()-prPend.t<90000 && prKey(live)!==prPend.k) return;   // bot-ka 👑 weli ma qaadan
  prPend=null; PRL=live;
}
function prLive(s){ const l=(PRD&&Array.isArray(PRD.l))?PRD.l:[]; return l.find(p=>p.s===s)||null; }
function prOnCount(){ return 1+(PRL||[]).filter(p=>p.on).length; }
function prPaint(){
  const box=$("#prList"); if(!box) return;
  const has=!!(PRD && Array.isArray(PRD.l)), ab=$("#prAddB"), sv=$("#prSave");
  if(!has){
    box.innerHTML='<div class="empty" style="padding:12px 0">Bot-ka 👑 (chart-ka dahabka · EA v70.8+) weli xog ma soo dirin.</div>';
    if(ab) ab.disabled=true; if(sv) sv.disabled=true; $("#prCnt").textContent="—"; return;
  }
  if(ab) ab.disabled=!!PERMS || (PRL||[]).length>=7; if(sv) sv.disabled=!!PERMS;
  if($("#prTplN")) $("#prTplN").textContent=PRD.tpl||"MOHA_PRO";
  const stale=Number(PRD.age||0)>180, mx=Number(PRD.mx)||5;
  $("#prCnt").textContent=prOnCount()+" / "+mx+" socda"+(stale?" · 👑 offline":"")+(prDirty?" · la kaydin":"");
  const m=PRD.l.find(p=>p.mm)||{s:PRD.m||"XAUUSD"};
  let h='<div class="prrow"><div class="prr"><span class="fl">'+prFlag(m.s)+'</span><span class="nm"><b>'+esc(m.s)+' 👑</b><small><span class="prx2">● SOCDA</span> · spread '+prSp(m)+' · maanta '+prMoney(m.pl)+(m.n?' · '+m.n+' trade':'')+'</small>'
    +'<span class="tg">sitinka guud</span><span class="tg">TICK · ZONE · ASIA · BASKET</span></span><span class="crown">👑 ugu weyn</span></div></div>';
  (PRL||[]).forEach((p,i)=>{
    const lv=prLive(p.s)||{}, x=lv.s?(Number(lv.x)||0):-1;
    let st;
    if(!p.on) st=(x===3)?'<span class="prx3">● DAMINAYA · '+(lv.n||0)+' trade weli furan</span>':'<span class="prx0">○ dansan</span>';
    else if(x<0 || prDirty) st='<span class="prx1">● KAYDI &amp; DIR riix</span>';
    else st='<span class="prx'+x+'">● '+PR_XN[x].toUpperCase()+'</span>';
    let sub=st;
    if(lv.s && p.on && x===2) sub+=' · spread '+prSp(lv)+' · maanta '+prMoney(lv.pl)+(lv.n?' · '+lv.n+' trade':'');
    if(lv.e) sub+=' · '+esc(lv.e);
    h+='<div class="prrow"><div class="prr" data-i="'+i+'"><span class="fl">'+prFlag(p.s)+'</span><span class="nm"><b>'+esc(p.s)+'</b><small>'+sub+'</small>'
      +'<span class="tg">'+(p.tf===1?'M1':'M5')+'</span><span class="tg">'+PR_STN[p.st]+'</span><span class="tg">'+(p.rk>0?p.rk+'%':'khatar guud')+'</span>'+prTagsHtml(p,lv)+'</span>'
      +'<button class="sw'+(p.on?' on':'')+'" type="button" data-sw="'+i+'" aria-label="'+esc(p.s)+' shid / dami"'+(PERMS?' disabled':'')+'><i></i></button></div>'
      +'<div class="pred"'+(prOpen===i?'':' hidden')+'>'
      +'<div class="l">Timeframe</div><div class="seg" data-k="tf" data-i="'+i+'"><button type="button" data-v="1">M1</button><button type="button" data-v="5">M5</button></div>'
      +'<div class="l">Xeeladda</div><div class="seg" data-k="st" data-i="'+i+'"><button type="button" data-v="0">SR/SD</button><button type="button" data-v="1">SMC</button><button type="button" data-v="2">LABADA</button></div>'
      +prFlHtml(p,i)+(p.fl?prCabHtml(p,lv,i):'')   // v12.24: ⚡ TICK · 🧺 BASKET · 📏 CABBIR
      +'<div class="l">Khatarta trade kasta</div><div class="seg" data-k="rk" data-i="'+i+'"><button type="button" data-v="0">Guud</button><button type="button" data-v="0.1">0.1%</button><button type="button" data-v="0.25">0.25%</button><button type="button" data-v="0.5">0.5%</button><button type="button" data-v="1">1%</button></div>'
      +'<button type="button" class="rm" data-rm="'+i+'">🗑 Ka saar liiska</button></div></div>';
  });
  box.innerHTML=h;
  box.querySelectorAll(".pred .seg").forEach(sg=>{
    const i=Number(sg.dataset.i), k=sg.dataset.k, v=PRL[i][k];
    sg.querySelectorAll("button").forEach(b=>{ const on=Number(b.dataset.v)===Number(v); b.classList.toggle("on",on); b.disabled=!!PERMS; });
  });
}
function prDirtySet(msg){ prDirty=true; prPaint(); prStripPaint(); $("#prNote").textContent=msg||"Isbeddel → KAYDI & DIR riix."; }
if($("#prList")) $("#prList").addEventListener("click",e=>{
  if(!PRL || PERMS) return;
  const sw=e.target.closest("[data-sw]"), rm=e.target.closest("[data-rm]"), sb=e.target.closest(".pred .seg button"), row=e.target.closest(".prr[data-i]");
  if(sw){
    const i=Number(sw.dataset.sw), mx=Number(PRD&&PRD.mx)||5;
    if(!PRL[i].on && prOnCount()+1>mx){ $("#prNote").textContent="⚠️ Ugu badnaan "+mx+" lamaane (👑 ku jiro) — mid dami marka hore."; return; }
    PRL[i].on=PRL[i].on?0:1; prDirtySet(PRL[i].s+(PRL[i].on?" → SHID":" → DAMI")+" · KAYDI & DIR riix."); return;
  }
  if(rm){ const i=Number(rm.dataset.rm), s=PRL[i].s; PRL.splice(i,1); prOpen=-1; prDirtySet(s+" liiska waa laga saaray · KAYDI & DIR riix."); return; }
  const flb=e.target.closest("[data-fl]"), cmb=e.target.closest(".prcm button[data-cm]");   // v12.24
  if(flb){ const i=Number(flb.dataset.i), bit=Number(flb.dataset.fl); PRL[i].fl=((PRL[i].fl|0)^bit)&7;
    prDirtySet(PRL[i].s+": "+(bit===1?"⚡ TICK":(bit===2?"🧺 BASKET":"🪜 GRID"))+((PRL[i].fl&bit)?" → SHID":" → DAMI")+" · KAYDI & DIR riix."); return; }
  if(cmb){ const i=Number(cmb.closest(".prcm").dataset.i), v=cmb.dataset.cm;
    if(v==="g"){ const C=prCab(PRL[i],prLive(PRL[i].s)); PRL[i].cm=(PRL[i].cm>0)?PRL[i].cm:((C.f&&C.f>0)?Number(C.f.toPrecision(3)):1); }
    else PRL[i].cm=Number(v);
    prDirtySet(PRL[i].s+": 📏 cabbir "+(PRL[i].cm>0?("GACAN ×"+prFx(PRL[i].cm)):(PRL[i].cm<0?"DAMI":"AUTO"))+" · KAYDI & DIR riix.");
    if(v==="g") setTimeout(()=>{ const x=document.querySelector('[data-cmi="'+i+'"]'); if(x){ x.focus(); x.select(); } },30);
    return; }
  if(sb){ const sg=sb.closest(".seg"), i=Number(sg.dataset.i), k=sg.dataset.k; PRL[i][k]=Number(sb.dataset.v); prDirtySet(); return; }
  if(row){ const i=Number(row.dataset.i); prOpen=(prOpen===i)?-1:i; prPaint(); }
});
function prAddOpen(show){
  const a=$("#prAdd"); if(!a || PERMS) return;
  a.hidden=(show===undefined)?!a.hidden:!show;
  if(!a.hidden){ prResPaint(); setTimeout(()=>{ const q=$("#prQ"); if(q) q.focus(); },50); }
}
function prResPaint(){
  const r=$("#prRes"); if(!r) return;
  const av=(PRD&&Array.isArray(PRD.av))?PRD.av:[];
  if(!av.length){ r.innerHTML='<div class="none">Liiska broker-ka weli ma iman — bot-ka 👑 ayaa soo diraya (1–2 daqiiqo).</div>'; return; }
  const q=String(($("#prQ")||{}).value||"").trim().toUpperCase();
  const have=new Set((PRL||[]).map(p=>p.s)); have.add(PRD.m||"");
  const m=av.filter(s=>!have.has(s) && (!q || String(s).toUpperCase().indexOf(q)>=0)).slice(0,40);
  r.innerHTML=m.length?m.map(s=>'<button type="button" data-s="'+esc(s)+'"><b>'+prFlag(s)+'</b><span>'+esc(s)+'</span><i>＋</i></button>').join(""):'<div class="none">Lama helin.</div>';
}
if($("#prAddB")) $("#prAddB").addEventListener("click",()=>prAddOpen());
if($("#prQ")) $("#prQ").addEventListener("input",prResPaint);
if($("#prRes")) $("#prRes").addEventListener("click",e=>{
  const b=e.target.closest("button[data-s]"); if(!b || !PRL) return;
  if(PRL.length>=7){ $("#prNote").textContent="⚠️ Liisku waa buuxaa."; return; }
  const mx=Number(PRD&&PRD.mx)||5, on=(prOnCount()+1<=mx)?1:0;
  PRL.push({s:b.dataset.s,on:on,tf:5,st:0,rk:0,fl:0,cm:0}); prOpen=PRL.length-1;
  $("#prAdd").hidden=true; $("#prQ").value="";
  prDirtySet("＋ "+b.dataset.s+(on?"":" (dansan · xadka "+mx+")")+" · timeframe / xeelad dooro → KAYDI & DIR riix.");
});
async function prSave(){
  if(!PRL || PERMS) return;
  const mx=Number(PRD&&PRD.mx)||5;
  if(prOnCount()>mx){ $("#prNote").textContent="⚠️ Ugu badnaan "+mx+" lamaane (👑 ku jiro) — qaar dami."; return; }
  const txt=prKey(PRL);
  const hex=Array.from(new TextEncoder().encode(txt)).map(b=>b.toString(16).padStart(2,"0")).join("");
  const btn=$("#prSave"), old=btn.textContent; btn.disabled=true; btn.textContent="Kaydinaya…";
  const err=await cfgPost({PAIRS:"h"+hex});
  btn.disabled=false; btn.textContent=old;
  if(!err){ prDirty=false; prPend={k:txt,t:Date.now()}; $("#prNote").textContent="✓ La kaydiyay. Bot-ka 👑 ~10–20 ilbiriqsi gudahood ayuu chart-yada furayaa / xidhayaa."; }
  else $("#prNote").textContent="⚠️ "+err;
  prPaint(); tick();
}
if($("#prSave")) $("#prSave").addEventListener("click",prSave);


/* ---- v12.24 (EA v70.9): 📏 CABBIR LAMAANE · ⚡ TICK / 🧺 BASKET lamaane kasta ---- */
let CBD=null, mCbTouched=false;
const mCb={on:1,tf:1,n:384,sq:25}, CB_PD=[288,96,24], CB_TFN=["M5","M15","H1"];
function prGold(s){ return /^(XAU|GOLD)/i.test(String(s||"")) || /XAU/i.test(String(s||"")); }
function prFlOf(p){
  const f=Number(p.fl);
  if(p.fl!==undefined && p.fl!==null && isFinite(f) && f>=0) return Math.max(0,Math.min(7,Math.round(f)));   // v12.25: 4 = 🪜 GRID
  return prGold(p.s)?(((TKCFG&&TKCFG.on)?1:0)|((BSKCFG&&BSKCFG.on)?2:0)):0;   // EA v70.8 (hore): dahab = badhamada guud
}
function prCmOf(p){ const c=Number(p.cm); if(!isFinite(c) || c===0) return 0; return c<0?-1:Math.max(0.00001,Math.min(100000,c)); }
function prCmS(c){ c=Number(c)||0; if(c===0) return "0"; if(c<0) return "-1"; return c.toFixed(8).replace(/0+$/,"").replace(/\\.$/,""); }
function prFx(f){ f=Number(f)||0; if(f<=0) return "—"; if(f>=100) return f.toFixed(0); if(f>=10) return f.toFixed(1); if(f>=1) return f.toFixed(2); return String(Number(f.toPrecision(2))); }
/* cabbirka lamaanaha: {f, md: AUTO|GACAN|DAMI, st: ok|wait|unk} */
function prCab(p,lv){
  const cm=prCmOf(p), md=cm>0?"GACAN":(cm<0?"DAMI":"AUTO");
  if(cm>0) return {f:cm,md:md,st:"ok"};
  if(cm<0) return {f:1,md:md,st:prGold(p.s)?"ok":"bad"};
  const cs=Number(lv&&lv.cs), cf=Number(lv&&lv.cf);
  if(lv && lv.cs!==undefined && isFinite(cs)){
    if(cs>=10) return {f:null,md:md,st:"wait"};
    if(cs===1) return prGold(p.s)?{f:1,md:md,st:"ok"}:{f:null,md:md,st:"off"};   // CABBIR-ka guud waa damman (Input)
    if(cs>=0 && cf>0) return {f:cf,md:md,st:"ok"};
  }
  return {f:null,md:md,st:"unk"};
}
function prDg(lv){ const d=Number(lv&&lv.dg); return (isFinite(d)&&d>=0&&d<=8)?d:5; }
function prCabHtml(p,lv,i){
  const C=prCab(p,lv), dg=prDg(lv), T=TKCFG||{}, base=[["Dhaqdhaqaaq ugu yar",Number(T.mv)||0.40],["SL (VSL)",Number(T.vsl)||2.50],["TP (VTP)",Number(T.vtp)],["Spread xad",(Number(T.spr)||35)*0.01]];
  let s;
  if(C.md==="GACAN") s="Adiga ayaa go'aamiyay: sitin kasta × <b>"+prFx(C.f)+"</b>.";
  else if(C.md==="DAMI") s=prGold(p.s)?"Sitinka $ ee dahabka sidiisa (×1).":"⚠️ DAMI = $ dahab — "+esc(p.s)+" kuma habboona → bot-ku trade <b>ma furo</b>. AUTO ama GACAN dooro.";
  else if(C.st==="ok") s=esc(p.s)+" wuxuu u dhaqaaqaa ≈ <b>"+(C.f<1?("1/"+Math.round(1/C.f)):("×"+prFx(C.f)))+"</b> dahabka (ATR "+(CB_TFN[mCb.tf]||"M15")+") · 3 daqiiqo kasta waa la cusboonaysiiyaa.";
  else if(C.st==="off") s="⏸ CABBIR-ka guud waa damman (Input → ⚡ TICK → 📏) → "+esc(p.s)+" trade <b>ma furo</b>. Shid ama GACAN ×N dooro.";
  else if(C.st==="wait") s="⏳ Bot-ku wuxuu sugayaa xogta ATR ("+esc(p.s)+" + XAUUSD) — trade cusub weli ma furmo.";
  else s="Bot-ka chart-kan ayaa xisaabin doona marka la kaydiyo (KAYDI &amp; DIR).";
  let h='<div class="prcab"><div class="h"><span>📏 CABBIR · '+C.md+'</span><b>'+(C.f?("×"+prFx(C.f)):"—")+'</b></div><div class="s'+((C.st==="bad"||C.st==="off")?" w":"")+'">'+s+'</div>';
  if(C.f && C.st!=="bad") h+='<div class="prmini">'+base.filter(r=>r[1]>0).map(r=>'<i>'+r[0]+'</i><em>'+r[1].toFixed(2)+'</em><b>'+(r[1]*C.f).toFixed(dg)+'</b>').join("")+'</div>';
  h+='<div class="prcm" data-i="'+i+'"><button type="button" data-cm="0"'+(C.md==="AUTO"?' class="on"':'')+'>AUTO</button><button type="button" data-cm="g"'+(C.md==="GACAN"?' class="on"':'')+'>GACAN ×</button><button type="button" data-cm="-1"'+(C.md==="DAMI"?' class="on"':'')+'>DAMI</button></div>'
    +'<div class="prcmx"'+(C.md==="GACAN"?'':' hidden')+'>× <input type="number" inputmode="decimal" min="0.00001" max="100000" step="any" data-cmi="'+i+'" value="'+(prCmOf(p)>0?prCmS(prCmOf(p)):"")+'" aria-label="Cabbirka gacanta"> <span>sitin kasta waa lagu dhuftaa</span></div>';
  const cq=Number(lv&&lv.cq), sq=CBD?Number(CBD.sq):25;
  if((p.fl&1) && lv && isFinite(cq) && sq>0 && cq>sq) h+='<div class="prwarn" style="margin-top:8px">⚠️ Spread-ku waa <b>'+Math.round(cq)+'%</b> SL-ka → TICK wuu sugayaa ilaa spread-ku yaraado (xadka '+sq+'%).</div>';
  return h+'</div>';
}
function prTagsHtml(p,lv){
  const C=prCab(p,lv);
  return ((p.fl&1)?'<span class="tg tk">⚡ TICK</span>':'')+((p.fl&2)?'<span class="tg bk">🧺 BASKET</span>':'')+((p.fl&4)?'<span class="tg gr">🪜 GRID</span>':'')
    +((p.fl && C.f && C.st==="ok" && Math.abs(C.f-1)>1e-9)?'<span class="tg cb">📏 ×'+prFx(C.f)+'</span>':'');
}
function prFlHtml(p,i){
  const b=(bit,lab)=>'<button type="button" data-fl="'+bit+'" data-i="'+i+'"'+((p.fl&bit)?' class="on"':'')+(PERMS?' disabled':'')+' aria-pressed="'+((p.fl&bit)?'true':'false')+'">'+lab+'<span class="prsw" aria-hidden="true"></span></button>';
  return '<div class="l">Xeeladaha lamaanahan <small style="color:var(--ink3);font-weight:500">📏 sitinka $ si toos ah ayaa loo cabbiraa</small></div><div class="prtg">'+b(1,"⚡ TICK")+b(2,"🧺 BASKET")+b(4,"🪜 GRID")+'</div>'
    +(((p.fl&1) && TKCFG && TKCFG.only)?'<div class="prtk">⚡ TICK OO KELIYA ayaa shidan → xeeladda kore (SR/SD · SMC) lamaanahan way istaagtaa.</div>':'');
}
/* ---- Input → ⚡ TICK → 📏 CABBIR LAMAANE ---- */
function cbDays(){ return Math.max(1,Math.round(mCb.n/(CB_PD[mCb.tf]||96))); }
function cbInputPaint(){
  const g=$("#cbGrp"); if(!g) return;
  const has=!!CBD;
  if(has && !mCbTouched){ mCb.on=CBD.on?1:0; const t=Number(CBD.tf); mCb.tf=(t>=0&&t<=2)?t:1; mCb.n=Number(CBD.n)||384; mCb.sq=isFinite(Number(CBD.sq))?Number(CBD.sq):25; }
  swSet("mCBON",!!mCb.on); segPaint("mCBTF","v",mCb.tf); segPaint("mCBD","v",cbDays()); segPaint("mCBSQ","v",mCb.sq);
  g.querySelectorAll("#mCBON,#mCBTF button,#mCBD button,#mCBSQ button").forEach(e=>{ e.disabled=!has || !!PERMS; });
  const go=$("#cbGo"); if(go) go.hidden=!PR_CTL;
  const hn=$("#mCBHint"); if(!hn) return;
  if(!has){ hn.innerHTML="⚠️ <b>EA v70.9</b> ayaa loo baahan yahay (chart-ka dahabka) — bot-ka cusboonaysii."; return; }
  const l=(PRD&&Array.isArray(PRD.l))?PRD.l.filter(p=>!p.mm && p.on && (Number(p.fl)>0)):[];
  const chips=l.map(p=>{ const C=prCab({s:p.s,cm:p.cm},p); return '<span>'+prFlag(p.s)+' '+esc(p.s)+' <b>'+(C.f?("×"+prFx(C.f)):(C.st==="wait"?"⏳":"—"))+'</b></span>'; }).join("");
  hn.innerHTML=(mCb.on?("✅ Tixraac: <b>"+esc(CBD.ref||"XAUUSD")+"</b> ×1 · ATR "+CB_TFN[mCb.tf]+" · "+cbDays()+" maalmood ("+mCb.n+" bar)")
                      :"⏸ CABBIR waa damman → lamaanaha aan dahabka ahayn TICK / BASKET <b>ma furaan</b> (GACAN ×N mooyee).")
    +(chips?'<div class="cbpairs">'+chips+'</div>':'<div style="margin-top:6px">Lamaane TICK / BASKET leh weli ma jiro — Maamul → 💱 Lamaanaha.</div>');
}
function cbCmds(cmds){
  if(!CBD) return;   // EA < v70.9 -> waxba ha dirin
  cmds.push("SET:CBON="+(mCb.on?1:0)); cmds.push("SET:CBTF="+mCb.tf); cmds.push("SET:CBN="+Math.max(20,Math.min(2000,Math.round(mCb.n)))); cmds.push("SET:CBSQ="+mCb.sq);
}
{ const e=$("#mCBON"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; mCb.on=mCb.on?0:1; mCbTouched=true; mTouched=true; cbInputPaint(); }); }
document.querySelectorAll("#mCBTF button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; const d=cbDays(); mCb.tf=Number(b.dataset.v); mCb.n=Math.max(20,Math.min(2000,d*CB_PD[mCb.tf])); mCbTouched=true; mTouched=true; cbInputPaint(); }));
document.querySelectorAll("#mCBD button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mCb.n=Math.max(20,Math.min(2000,Number(b.dataset.v)*(CB_PD[mCb.tf]||96))); mCbTouched=true; mTouched=true; cbInputPaint(); }));
document.querySelectorAll("#mCBSQ button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mCb.sq=Number(b.dataset.v); mCbTouched=true; mTouched=true; cbInputPaint(); }));
if($("#cbGo")) $("#cbGo").addEventListener("click",()=>{ tab("Maamul"); setTimeout(()=>{ const c=$("#prCard"); if(c) c.scrollIntoView({block:"start"}); },60); });

if($("#prList")) $("#prList").addEventListener("change",e=>{   // v12.24: 📏 cabbirka gacanta
  const x=e.target.closest("[data-cmi]"); if(!x || !PRL || PERMS) return;
  const i=Number(x.dataset.cmi), v=Number(x.value);
  if(!isFinite(v) || v<0.00001 || v>100000){ $("#prNote").textContent="⚠️ Cabbirka gacanta: 0.00001 – 100000."; return; }
  PRL[i].cm=v; prDirtySet(PRL[i].s+": 📏 GACAN ×"+prFx(v)+" · KAYDI & DIR riix.");
});

/* ================= v12.25 (EA v71.0): 🪜 GRID STOP ================= */
let GRD=null, GRP="w", mGrTouched=false;
try{ const v=localStorage.getItem("mp_grper"); if(v && "dwma".indexOf(v)>=0) GRP=v; }catch(e){}
const GR_X=["","🎯 TP guud","🔒 quful","🛑 SL guud","SL broker / gacan","⏱ waqti","📅 Jimce / suuq"];
function grMoney(v){ v=Number(v)||0; return (v>=0?"+":"−")+"$"+Math.abs(v).toFixed(2); }
function grDur(m){ m=Number(m)||0; return m>=60?(Math.floor(m/60)+" saac "+(m%60)+" daq"):(m+" daq"); }
function grRows(){
  const all=(GRD && Array.isArray(GRD.gh))?GRD.gh.slice():[];
  const now=Date.now()/1000, d0=new Date(); d0.setHours(0,0,0,0);
  const from=GRP==="d"?d0.getTime()/1000:(GRP==="w"?now-7*86400:(GRP==="m"?now-30*86400:0));
  return all.filter(r=>Number(r.t)>=from).sort((a,b)=>a.t-b.t);
}
function grPaint(g,bal){
  GRD=g||null;
  const c=$("#grCard"); if(!c) return;
  if(!GRD){ c.hidden=true; grInputPaint(); return; }
  c.hidden=false;
  swSet("grSw",!!GRD.on); $("#grSw").disabled=!!PERMS; $("#grKeli").hidden=!GRD.only;
  const st=$("#grSt"), dir=String(GRD.dir||""), dg=Number(GRD.dg)||2, run=(GRD.st==="RUN");
  const fx=v=>(Number(v)||0).toFixed(dg);
  if(run){
    const m=Math.max(0,Math.round((Number(GRD.srv)-Number(GRD.t0))/60));
    st.className="grst "+(dir==="SELL"?"s":"b");
    st.innerHTML=(dir==="SELL"?"▼":"▲")+" "+dir+" GRID SOCDA · "+GRD.n+" / "+GRD.lv+" lakab<small>"+esc(GRD.why||"")+" · masaafo "+fx(GRD.step)+" · "+grDur(m)+"</small>";
  } else if(GRD.st==="ARMED"){ st.className="grst a"; st.innerHTML="🪜 "+dir+" STOP diyaar<small>"+esc(GRD.why||"")+"</small>"; }
  else if(GRD.st==="WAIT"){ st.className="grst a"; st.innerHTML="⏳ SUGAYA<small>"+esc(GRD.why||"")+"</small>"; }
  else { st.className="grst o"; st.innerHTML="⏸ GRID waa damman<small>Shid → bot-ku wuxuu sugaa jiho xoog leh (🧭) kadibna grid ayuu dhigaa.</small>"; }
  $("#grKp").hidden=!run; $("#grLad").hidden=!run; $("#grLl").hidden=!run;
  if(run){
    const fl=Number(GRD.fl)||0, lk=Number(GRD.lk)||0;
    const f=$("#grFl"); f.textContent=grMoney(fl); f.className=fl>=0?"p":"n";
    $("#grLk").textContent=lk>0?("+$"+lk.toFixed(2)):"—"; $("#grSl").textContent="−$"+(Number(GRD.sl)||0).toFixed(2);
    const lv=Math.max(1,Number(GRD.lv)||1), n=Math.min(lv,Number(GRD.n)||0), lad=$("#grLad");
    lad.className="grlad "+(dir==="SELL"?"s":"b"); lad.innerHTML=Array.from({length:lv},(_,i)=>'<i class="'+(i<n?'f':'')+'"></i>').join("");
    $("#grLl").innerHTML="<span>"+n+" furan</span><span>"+(lv-n)+" sugaya</span><span>🎯 "+(Number(GRD.tp)>0?("+$"+Number(GRD.tp).toFixed(2)):"off")+"</span>";
  }
  segPaint("grPer","v",GRP);
  const rows=grRows();
  let w=0,pl=0,gw=0,gl=0,lsum=0,pk=0,cum=0,dd=0;
  rows.forEach(r=>{ const p=Number(r.pl)||0; pl+=p; if(p>0){ w++; gw+=p; } else gl+=-p; lsum+=Number(r.n)||0; cum+=p; if(cum>pk) pk=cum; if(pk-cum>dd) dd=pk-cum; });
  const N=rows.length;
  $("#grN").textContent=N; $("#grW").textContent=N?Math.round(100*w/N)+"%":"—"; $("#grW").className=N&&w/N>=0.5?"p":"";
  const e=$("#grPL"); e.textContent=N?grMoney(pl):"—"; e.className=pl>0?"p":(pl<0?"n":"");
  $("#grPF").textContent=N?(gl>0?(gw/gl).toFixed(2):(gw>0?"∞":"—")):"—";
  $("#grDD").innerHTML=N?("−$"+dd.toFixed(2)+(bal>0?'<small style="display:block;font-size:10.5px;color:var(--ink3);font-weight:600">'+(100*dd/bal).toFixed(2)+'% balance</small>':"")):"—";
  $("#grAv").textContent=N?(lsum/N).toFixed(1):"—";
  const svg=$("#grEq");
  if(N>=1){
    let c2=0; const pts=[0].concat(rows.map(r=>(c2+=Number(r.pl)||0)));
    const mn=Math.min.apply(null,pts), mx=Math.max.apply(null,pts), rg=(mx-mn)||1;
    const P=pts.map((v,i)=>(i*340/(pts.length-1||1)).toFixed(1)+","+(58-(v-mn)/rg*52).toFixed(1)).join(" ");
    const z=(58-(0-mn)/rg*52).toFixed(1);
    svg.innerHTML='<line x1="0" x2="340" y1="'+z+'" y2="'+z+'" stroke="#2a2c33"/><polyline fill="none" stroke="'+(pl>=0?'#4ade80':'#f87171')+'" stroke-width="2.5" points="'+P+'"/>';
  } else svg.innerHTML='<text x="170" y="36" fill="#6b7280" font-size="12" text-anchor="middle">grid weli ma dhammaan</text>';
  $("#grEqT").textContent="EQUITY-GA GRID-KA ("+{d:"maanta",w:"todobaadkan",m:"bishan",a:"dhammaan"}[GRP]+")";
  const last=rows.slice(-6).reverse();
  $("#grHl").innerHTML=last.length?last.map(r=>{ const s=Number(r.d)<0, p=Number(r.pl)||0;
    return '<div class="grhr"><span class="d '+(s?'s':'b')+'">'+(s?'▼ SELL':'▲ BUY')+'</span><span><b>'+(Number(r.n)||0)+' lakab · '+grDur(r.m)+'</b><small>'+(GR_X[Number(r.x)]||"—")+' · '+new Date(Number(r.t)*1000).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})+'</small></span><b class="'+(p>=0?'p':'n')+'">'+grMoney(p)+'</b></div>'; }).join("")
    :'<div class="grnone">Muddadan grid ma dhammaan.</div>';
  const rj=Array.isArray(GRD.rj)?GRD.rj:[0,0,0,0], dy=GRD.day||{};
  $("#grRj").textContent="Maanta: "+(Number(dy.n)||0)+" grid · "+grMoney(dy.pl)+" · la diiday: range "+(rj[0]||0)+" · war "+(rj[1]||0)+" · spread "+(rj[2]||0)+" · cooldown "+(rj[3]||0);
  grInputPaint();
}
document.querySelectorAll("#grPer button").forEach(b=>b.addEventListener("click",()=>{ GRP=b.dataset.v; try{ localStorage.setItem("mp_grper",GRP); }catch(e){} grPaint(GRD,GR_BAL); }));
let GR_BAL=0;
if($("#grSw")) $("#grSw").addEventListener("click",async ()=>{
  if(!GRD || PERMS) return;
  const v=GRD.on?0:1; GRD.on=v; swSet("grSw",!!v);
  const err=await cfgPost({GRON:v});
  $("#grSt").innerHTML=err?("⚠️ "+esc(err)):(v?"🪜 GRID waa la shiday · bot-ku ~10 ilbiriqsi gudahood ayuu qaadanayaa.":"⏸ GRID waa la damiyay · grid socda (haddii uu jiro) waa la sii maamulayaa.");
});
/* ---- Input → 🪜 GRID ---- */
const mGr={on:0,only:1,dir:0,str:40,nw:1};
const GR_NUM=[["mGRSTEP","step",0,1000],["mGRSATR","satr",0.05,1],["mGRSPX","spx",1,10],["mGRLV","lv",2,30],["mGRLOT","lot",0.01,100],["mGRTP","tp",0,100000],["mGRLK","lk",0,100000],
  ["mGRLKP","lkp",10,90],["mGRSL","sl",0.1,5],["mGRHL","hl",2,30],["mGRCD","cd",0,1440],["mGRMD","md",1,50],["mGRDL","dl",0,50],["mGRHS","hs",0,23],["mGRHE","he",1,24],["mGRMM","mm",0,10080]];
const GR_KEY={step:"GRSTEP",satr:"GRSATR",spx:"GRSPX",lv:"GRLV",lot:"GRLOT",tp:"GRTP",lk:"GRLK",lkp:"GRLKP",sl:"GRSL",hl:"GRHL",cd:"GRCD",md:"GRMD",dl:"GRDL",hs:"GRHS",he:"GRHE",mm:"GRMM"};
const GR_INT={lv:1,hl:1,cd:1,md:1,hs:1,he:1,mm:1};
function grInputPaint(){
  const g=$("#grGrp"); if(!g) return;
  const cf=(GRD && GRD.cfg)?GRD.cfg:null, has=!!cf;
  if(has && !mGrTouched){
    mGr.on=cf.on?1:0; mGr.only=cf.only?1:0; mGr.dir=Number(cf.dir)||0; mGr.str=Number(cf.str); mGr.nw=cf.nw?1:0;
    GR_NUM.forEach(([id,k])=>{ const e=$("#"+id); if(e && document.activeElement!==e && cf[k]!==undefined && cf[k]!==null) e.value=cf[k]; });
  }
  swSet("mGRON",!!mGr.on); swSet("mGRONLY",!!mGr.only); swSet("mGRNW",!!mGr.nw); segPaint("mGRDIR","v",mGr.dir); segPaint("mGRSTR","v",mGr.str);
  g.querySelectorAll("button,input").forEach(e=>{ e.disabled=!has || !!PERMS; });
  const hn=$("#mGRHint"); if(!hn) return;
  hn.innerHTML=!has?"⚠️ <b>EA v71.0</b> ayaa loo baahan yahay (chart-ka dahabka) — bot-ka cusboonaysii."
    :(mGr.on?("✅ GRID shidan"+(mGr.only?" · <b>KELI</b> (xeeladaha kale chart-kan ma furaan)":" · xeeladaha kale way la socdaan")+" · "+(["🧭 jihada trend-ka","BUY oo keliya","SELL oo keliya"][mGr.dir]||"")+" · 🛑 SL guud "+((Number(($("#mGRSL")||{}).value)||0.5))+"%")
             :"⏸ GRID waa damman. Lamaanaha: Maamul → 💱 → 🪜 GRID.");
}
function grCmds(cmds){
  if(!GRD || !GRD.cfg) return;   // EA < v71.0 -> waxba ha dirin
  cmds.push("SET:GRON="+(mGr.on?1:0)); cmds.push("SET:GRONLY="+(mGr.only?1:0)); cmds.push("SET:GRDIR="+mGr.dir); cmds.push("SET:GRSTR="+mGr.str); cmds.push("SET:GRNW="+(mGr.nw?1:0));
  GR_NUM.forEach(([id,k,lo,hi])=>{ const e=$("#"+id); if(!e || e.value==="") return; let v=Number(e.value); if(!isFinite(v)) return; v=Math.max(lo,Math.min(hi,v)); if(GR_INT[k]) v=Math.round(v); cmds.push("SET:"+GR_KEY[k]+"="+v); });
}
[["mGRON","on"],["mGRONLY","only"],["mGRNW","nw"]].forEach(([id,k])=>{ const e=$("#"+id); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; mGr[k]=mGr[k]?0:1; mGrTouched=true; mTouched=true; grInputPaint(); }); });
[["mGRDIR","dir"],["mGRSTR","str"]].forEach(([id,k])=>document.querySelectorAll("#"+id+" button").forEach(b=>b.addEventListener("click",()=>{ if(b.disabled) return; mGr[k]=Number(b.dataset.v); mGrTouched=true; mTouched=true; grInputPaint(); })));
GR_NUM.forEach(([id])=>{ const e=$("#"+id); if(e) e.addEventListener("input",()=>{ mGrTouched=true; mTouched=true; }); });

/* ---- 🎯 ZONE YAR (Guud kaarka + Input) ---- */
function znLadder(zs,at,px,f){
  const sv=$("#znLad"); if(!sv) return;
  if(!zs.length){ sv.setAttribute("viewBox","0 0 320 22"); sv.innerHTML='<text x="160" y="15" fill="#8b8a82" font-size="11" text-anchor="middle">zone ma jiro</text>'; return; }
  let lo=Math.min(px,...zs.map(q=>q[0])), hi=Math.max(px,...zs.map(q=>q[1]));
  const pad=((hi-lo)*0.08)||1; lo-=pad; hi+=pad;
  const H=Math.max(90,24*zs.length+20), y=v=>8+(hi-v)/(hi-lo)*(H-16);
  let s="";
  zs.forEach((q,k)=>{
    const col=(k===at)?"#f0cf86":(q[0]>px?"#ef4444":"#38bdf8"), y1=y(q[1]), h=Math.max(4,y(q[0])-y1), ym=(y1+h/2+4).toFixed(1);
    s+='<rect x="30" y="'+y1.toFixed(1)+'" width="200" height="'+h.toFixed(1)+'" rx="2" fill="'+col+'" opacity="'+(k===at?0.9:0.55)+'"/>'
      +'<text x="2" y="'+ym+'" fill="'+col+'" font-size="11" font-weight="800">Z'+(k+1)+'</text>'
      +'<text x="318" y="'+ym+'" fill="#9a9ea8" font-size="10" text-anchor="end">'+f((q[0]+q[1])/2)+' ×'+q[2]+'</text>';
  });
  const py=y(px).toFixed(1);
  s+='<line x1="26" x2="234" y1="'+py+'" y2="'+py+'" stroke="#f0cf86" stroke-width="1.4" stroke-dasharray="4 3"/><circle cx="206" cy="'+py+'" r="5" fill="#f0cf86"/>'
    +'<text x="198" y="'+(Number(py)-7).toFixed(1)+'" fill="#f0cf86" font-size="10" font-weight="800" text-anchor="end">qiimaha '+f(px)+'</text>';
  sv.setAttribute("viewBox","0 0 320 "+H); sv.innerHTML=s;
}
function znPaint(z){
  ZN=(z&&typeof z==="object")?z:null;
  const c=$("#znCard"); if(!c){ znInputPaint(); return; }
  if(!ZN){ c.hidden=true; znInputPaint(); return; }
  c.hidden=false;
  const dg=Number(ZN.dg)||2, f=v=>Number(v).toFixed(dg), zs=Array.isArray(ZN.z)?ZN.z:[], at=Number(ZN.at), st=Number(ZN.st)||0, px=Number(ZN.px)||0;
  $("#znTf").textContent=["M1","M5","LABADA"][Number(ZN.tf)]||"M5";
  segPaint("znEye","v",ZN.sh?1:0); const eye=$("#znEye"); if(eye) eye.hidden=!!PERMS || !PR_CTL;
  const zr=k=>"Z"+(k+1)+" "+f(zs[k][0])+"–"+f(zs[k][1]);
  let t="—", sub="";
  if(!ZN.on){ t="DAMMAN"; sub="Input → 🎯 ZONE → shid"; }
  else if(!zs.length){ t="SUGAYA · zone xoog leh (×"+ZN.t+") weli ma jiro"; sub="bar cusub kasta ayaa dib loo xisaabiyaa"; }
  else if(st===2 && zs[at]){ t="SELL DIYAAR · "+zr(at); sub="EMA HOOS · qiimuhu zone-ka ayuu taabtay · sugaya tick HOOS"; }
  else if(st===3 && zs[at]){ t="BUY DIYAAR · "+zr(at); sub="EMA KOR · qiimuhu zone-ka ayuu taabtay · sugaya tick KOR"; }
  else if(st===4 && zs[at]){ t="ZONE · "+zr(at); sub="jiho ma jirto (RANGE) → ha gelin"; }
  else {
    let nk=-1, nd=1e18; zs.forEach((q,k)=>{ const d=px>q[1]?px-q[1]:(px<q[0]?q[0]-px:0); if(d<nd){ nd=d; nk=k; } });
    t="SUGAYA · qiimuhu zone kuma jiro"; if(nk>=0) sub="ugu dhow "+zr(nk)+" · $"+nd.toFixed(2)+(px<zs[nk][0]?" kor":" hoos");
  }
  if(ZN.on && !ZN.tk) sub+=(sub?" · ":"")+"⚡ TICK waa damman (trade lama galo)";
  const box=$("#znSt"); box.className="znst s"+(ZN.on?st:0); box.innerHTML=esc(t)+(sub?"<small>"+esc(sub)+"</small>":"");
  znLadder(ZN.on?zs:[],at,px,f);
  let meta="ATR $"+(Number(ZN.atr)||0).toFixed(2)+" · ballac ≤ $"+((Number(ZN.atr)||0)*(Number(ZN.w)||0)).toFixed(2)+" · taabasho ×"+ZN.t+"+";
  if((st===2||st===3) && zs[at]){
    const q=zs[at], sell=(st===2), sl=sell?q[1]+Number(ZN.sl):q[0]-Number(ZN.sl);
    let tp=null;
    if(sell){ for(let k=at+1;k<zs.length;k++){ if(zs[k][1]<px){ tp=[k,zs[k][1]]; break; } } }
    else { for(let k=at-1;k>=0;k--){ if(zs[k][0]>px){ tp=[k,zs[k][0]]; break; } } }
    meta="Z"+(at+1)+" ×"+q[2]+" · $"+(q[1]-q[0]).toFixed(2)+" · SL "+f(sl)+" · TP "+((Number(ZN.tp)===1||!tp)?"TICK caadi":("Z"+(tp[0]+1)+" "+f(tp[1])));
  }
  $("#znMeta").textContent=meta;
  const rj=$("#znRj"); if(ZN.rj){ rj.hidden=false; rj.textContent="⛔ "+ZN.rj+(ZN.rn?" · maanta "+ZN.rn+" la diiday":""); } else rj.hidden=true;
  znInputPaint();
}
function znInputPaint(){
  const g=$("#znGrp"); if(!g) return;
  const has=!!ZN;
  if(has && !mZnTouched){
    mZn.on=ZN.on?1:0; mZn.tf=Number(ZN.tf)||0; mZn.sh=ZN.sh?1:0; mZn.t=Number(ZN.t)||3; mZn.tp=Number(ZN.tp)||0;
    [["mZNW",ZN.w],["mZNMX",ZN.mx],["mZNSL",ZN.sl]].forEach(([id,v])=>{ const e=$("#"+id); if(e && document.activeElement!==e && v!==undefined && v!==null) e.value=v; });
  }
  swSet("mZNON",!!mZn.on); segPaint("mZNTF","v",mZn.tf); segPaint("mZNSH","v",mZn.sh); segPaint("mZNT","v",mZn.t); segPaint("mZNTP","v",mZn.tp);
  g.querySelectorAll("button,input").forEach(e=>{ e.disabled=!has || !!PERMS; });
  const hn=$("#mZNHint");
  if(hn) hn.innerHTML=!has?"⚠️ <b>EA v70.8</b> ayaa loo baahan yahay (chart-ka dahabka) — bot-ka cusboonaysii."
    :(mZn.on?"✅ TICK wuxuu ka galaa zone-ka oo keliya · zone-yada hadda: <b>"+((ZN.z||[]).length)+"</b>"+(ZN.tk?"":" · ⚡ TICK waa damman")
            :"⏸ ZONE YAR waa damman → TICK sidii hore (tick tracker + 🧭 jihada).");
}
function znCmds(cmds){
  if(!ZN) return;   // EA < v70.8 -> waxba ha dirin
  cmds.push("SET:ZNON="+(mZn.on?1:0)); cmds.push("SET:ZNTF="+mZn.tf); cmds.push("SET:ZNSH="+mZn.sh); cmds.push("SET:ZNT="+mZn.t); cmds.push("SET:ZNTP="+mZn.tp);
  const n=(id,lo,hi)=>{ const e=$("#"+id); if(!e || e.value==="") return null; const v=Number(e.value); return isFinite(v)?Math.max(lo,Math.min(hi,v)):null; };
  const w=n("mZNW",0.05,1), mx=n("mZNMX",1,10), sl=n("mZNSL",0.1,20);
  if(w!==null) cmds.push("SET:ZNW="+w); if(mx!==null) cmds.push("SET:ZNMX="+Math.round(mx)); if(sl!==null) cmds.push("SET:ZNSL="+sl);
}
{ const e=$("#mZNON"); if(e) e.addEventListener("click",()=>{ if(e.disabled) return; mZn.on=mZn.on?0:1; mZnTouched=true; mTouched=true; znInputPaint(); }); }
[["mZNTF","tf"],["mZNSH","sh"],["mZNT","t"],["mZNTP","tp"]].forEach(([id,k])=>document.querySelectorAll("#"+id+" button").forEach(b=>b.addEventListener("click",()=>{
  if(b.disabled) return; mZn[k]=Number(b.dataset.v); mZnTouched=true; mTouched=true; znInputPaint(); })));
["mZNW","mZNMX","mZNSL"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("input",()=>{ mZnTouched=true; mTouched=true; }); });
document.querySelectorAll("#znEye button").forEach(b=>b.addEventListener("click",async ()=>{
  if(!ZN || PERMS) return;
  const v=Number(b.dataset.v); ZN.sh=v; mZn.sh=v; segPaint("znEye","v",v); znInputPaint();
  const err=await cfgPost({ZNSH:v});
  $("#znMeta").textContent=err?("⚠️ "+err):(v?"👁 Zone-yada chart-ka MT5 ayaa lagu muujinayaa (~20 ilbiriqsi).":"🙈 Chart-ka MT5 waa la nadiifinayaa — bot-ku wuu sii ganacsanayaa.");
}));


/* ================= v12.23: 🖼 wajiga hore · ⏻ MT5 SHID/DAMI · ✕ XIDH · ⋯ menu ================= */
const HERO={on:null,pend:null,open:[],flo:0};
function cfmOpen(o){
  const c=$("#cfm"); if(!c) return;
  $("#cfmT").textContent=o.title||""; $("#cfmB").innerHTML=o.html||""; $("#cfmL").innerHTML=o.list||""; $("#cfmL").hidden=!o.list; $("#cfmN").textContent="";
  const ok=$("#cfmOk"), al=$("#cfmAlt");
  ok.textContent=o.ok||"OK"; ok.className=o.okCls||"d"; ok.onclick=()=>{ cfmClose(); if(o.onOk) o.onOk(); };
  if(o.alt){ al.hidden=false; al.textContent=o.alt; al.onclick=()=>{ cfmClose(); if(o.onAlt) o.onAlt(); }; } else al.hidden=true;
  c.hidden=false; setTimeout(()=>{ try{ $("#cfmNo").focus(); }catch(e){} },30);
}
function cfmClose(){ const c=$("#cfm"); if(c) c.hidden=true; }
if($("#cfm")){
  $("#cfmNo").addEventListener("click",cfmClose);
  $("#cfm").addEventListener("click",e=>{ if(e.target.id==="cfm") cfmClose(); });
  addEventListener("keydown",e=>{ if(e.key==="Escape" && !$("#cfm").hidden) cfmClose(); });
}
async function heroCmd(cmd){
  const note=$("#cmdNote");
  try{
    const body={cmd:cmd}; if(accSel) body.account=accSel.value;
    const r=await fetch("/api/command",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(note) note.textContent=d.ok?("Waa la diray: "+cmd+" — EA-du 3–5s gudahood buu qaadanayaa."):("Khalad: "+(d.error||"lama diri karin"));
    if(!d.ok){ HERO.pend=null; heroCtlPaint(); alertNote(d.error||"Amarka lama diri karin."); }
  }catch(e){ HERO.pend=null; heroCtlPaint(); alertNote("Internet ma jiro — amarka lama dirin."); }
  tick();
}
function alertNote(t){ const s=$("#pwrS"); if(s){ s.textContent="⚠️ "+t; } }
function heroCtlPaint(){
  const sw=$("#pwrSw"), t=$("#pwrT"), s=$("#pwrS"); if(!sw) return;
  if(HERO.pend && Date.now()-HERO.pend.t>45000) HERO.pend=null;
  if(HERO.pend && HERO.on!==null && HERO.on===HERO.pend.want) HERO.pend=null;
  const on=HERO.pend?HERO.pend.want:HERO.on;
  sw.classList.toggle("off",on===false); sw.classList.toggle("na",on===null); sw.classList.toggle("pend",!!HERO.pend);
  sw.setAttribute("aria-pressed",on?"true":"false");
  { const hr=document.querySelector(".hero.hfull"); if(hr) hr.classList.toggle("hoff",on===false); }
  if(on===null){ t.textContent="OFFLINE"; s.textContent="bot-ku ma xidhiidhsana"; }
  else if(HERO.pend){ t.textContent=on?"SHIDAYA…":"DAMINAYA…"; s.textContent="EA-da ayaa qaadanaysa (3–5s)"; }
  else { t.textContent=on?"SHIDAN":"DAMMAN"; s.textContent=on?"taabo → dami":"taabo → shid"; }
  const xb=$("#xidB"), xs=$("#xidS"), n=HERO.open.length;
  if(xs) xs.textContent=n?(n+" trade\\n"+(HERO.flo>=0?"+":"−")+"$"+Math.abs(HERO.flo).toFixed(2)):"trade furan\\nma jiro";
  if(xb && permOK("close")) xb.disabled=(n===0);
}
function heroPaint(d){
  const x=d.data||{}, RS=runState(d);
  HERO.on=d.online?(RS.short!=="LA DAMIYAY"):null;
  if(!d.online && d.data && x.status) HERO.on=(String(x.status)!=="STOPPED");
  const rows=Array.isArray(x.trades)?x.trades:[];
  HERO.open=rows.filter(t=>String(t.st||"OPEN").toUpperCase()==="OPEN");
  HERO.flo=HERO.open.reduce((a,t)=>a+(Number(t.profit)||0),0);
  heroCtlPaint();
}
if($("#pwrSw")) $("#pwrSw").addEventListener("click",()=>{
  const sw=$("#pwrSw"); if(sw.disabled) return;
  const on=HERO.pend?HERO.pend.want:HERO.on;
  if(on===false || on===null){ HERO.pend={want:true,t:Date.now()}; heroCtlPaint(); heroCmd("START"); return; }
  cfmOpen({title:"⏻ Bot-ka dami?",html:"Trade <b>cusub ma furmo</b> (chart-yada oo dhan). Trade-yada hadda furan <b>waa la sii maamulayaa</b> — SL · BE · trailing way shaqeynayaan.",
    ok:"⏻ DAMI",okCls:"d",onOk:()=>{ HERO.pend={want:false,t:Date.now()}; heroCtlPaint(); heroCmd("STOP"); }});
});
if($("#xidB")) $("#xidB").addEventListener("click",()=>{
  const n=HERO.open.length; if(!n) return;
  const list=HERO.open.slice(0,20).map(t=>{ const p=Number(t.profit)||0; return '<span><b>'+esc(t.sym||t.symbol||"")+'</b> · '+esc(String(t.type||""))+'<b class="'+(p>=0?'pos':'neg')+'">'+(p>=0?"+":"−")+Math.abs(p).toFixed(2)+'</b></span>'; }).join("");
  cfmOpen({title:"✕ Xidh dhammaan trade-yada?",html:"<b>"+n+" trade</b> ayaa furan · hadda <b class=\\""+(HERO.flo>=0?"pos":"neg")+"\\">"+(HERO.flo>=0?"+":"−")+"$"+Math.abs(HERO.flo).toFixed(2)+"</b>. Dhammaan isla markiiba waa la xidhayaa. Bot-ku <b>wuu sii shidnaanayaa</b>.",
    list:list, ok:"✕ XIDH DHAMMAAN", okCls:"o", onOk:()=>heroCmd("CLOSE_ALL"),
    alt:permOK("close")?"📈 Faa'iidada oo keliya xidh":"", onAlt:()=>heroCmd("CLOSE_PROFIT")});
});
/* ⋯ menu: account · Admin · Maamul · Password · Bax */
(function(){
  const b=$("#heroMenuB"), m=$("#heroMenu"); if(!b||!m) return;
  b.addEventListener("click",e=>{ e.stopPropagation(); m.hidden=!m.hidden; b.setAttribute("aria-expanded",m.hidden?"false":"true"); });
  document.addEventListener("click",e=>{ if(!m.hidden && !m.contains(e.target) && e.target!==b){ m.hidden=true; b.setAttribute("aria-expanded","false"); } });
  addEventListener("keydown",e=>{ if(e.key==="Escape") m.hidden=true; });
})();

/* ---- 🎵 player-ka: jiid (⋮⋮) · – qari (saxan yar) · ✕ jooji + xidh ---- */
const MUP={mode:"bar",y:null,bx:"R",by:0.55};
try{ const v=JSON.parse(localStorage.getItem("mp_mu_pos")||"null"); if(v){ if(v.mode==="bub") MUP.mode="bub"; if(typeof v.y==="number") MUP.y=Math.max(0,Math.min(1,v.y)); if(v.bx==="L") MUP.bx="L"; if(typeof v.by==="number") MUP.by=Math.max(0,Math.min(1,v.by)); } }catch(e){}
function mupSave(){ try{ localStorage.setItem("mp_mu_pos",JSON.stringify(MUP)); }catch(e){} }
function mupBounds(h){ const ab=document.querySelector(".appbar"), bot=(ab?ab.offsetHeight:60)+6; return {min:8,max:Math.max(8,innerHeight-bot-h)}; }
function mupPlaceBar(){
  const b=mu$("muBar"); if(!b || b.hidden) return;
  const ab=document.querySelector(".appbar");
  if(MUP.y===null){ b.style.top="auto"; b.style.bottom=((ab?ab.offsetHeight:60)+6)+"px"; return; }
  const B=mupBounds(b.offsetHeight||62); b.style.bottom="auto"; b.style.top=Math.round(B.min+MUP.y*(B.max-B.min))+"px";
}
function mupPlaceBub(){
  const u=mu$("muBub"); if(!u || u.hidden) return;
  const B=mupBounds(62), x=(MUP.bx==="L")?10:(innerWidth-62-10);
  u.style.left=x+"px"; u.style.top=Math.round(B.min+MUP.by*(B.max-B.min))+"px";
}
function mupDrag(el,onMove,onEnd,onTap){
  let st=null;
  el.addEventListener("pointerdown",e=>{ if(e.button!==undefined && e.button>0) return; st={x:e.clientX,y:e.clientY,id:e.pointerId,mv:false}; try{ el.setPointerCapture(e.pointerId); }catch(err){} });
  el.addEventListener("pointermove",e=>{ if(!st || e.pointerId!==st.id) return; const dx=e.clientX-st.x, dy=e.clientY-st.y; if(!st.mv && Math.hypot(dx,dy)<8) return; st.mv=true; e.preventDefault(); onMove(e,dx,dy); });
  const end=e=>{ if(!st) return; const s=st; st=null; try{ el.releasePointerCapture(s.id); }catch(err){} if(s.mv) onEnd(e); else if(onTap) onTap(e); };
  el.addEventListener("pointerup",end); el.addEventListener("pointercancel",end);
}
(function(){
  const b=mu$("muBar"), hd=mu$("muHd"), u=mu$("muBub"); if(!b||!hd||!u) return;
  let y0=0;
  hd.addEventListener("pointerdown",()=>{ y0=b.getBoundingClientRect().top; });
  mupDrag(hd,(e,dx,dy)=>{ b.classList.add("drag"); const B=mupBounds(b.offsetHeight); b.style.bottom="auto"; b.style.top=Math.max(B.min,Math.min(B.max,y0+dy))+"px"; document.body.classList.remove("mu-on"); muLayout(); },
    ()=>{ b.classList.remove("drag"); const B=mupBounds(b.offsetHeight), t=b.getBoundingClientRect().top;
      MUP.y=(B.max-t<40)?null:((B.max>B.min)?(t-B.min)/(B.max-B.min):0); mupSave(); muBar(); });
  let p0=null;
  u.addEventListener("pointerdown",()=>{ const r=u.getBoundingClientRect(); p0={x:r.left,y:r.top}; });
  mupDrag(u,(e,dx,dy)=>{ u.classList.add("drag"); const B=mupBounds(62); u.style.left=Math.max(4,Math.min(innerWidth-66,p0.x+dx))+"px"; u.style.top=Math.max(B.min,Math.min(B.max,p0.y+dy))+"px"; muLayout(); },
    ()=>{ u.classList.remove("drag"); const r=u.getBoundingClientRect(), B=mupBounds(62); MUP.bx=(r.left+31<innerWidth/2)?"L":"R"; MUP.by=(B.max>B.min)?(r.top-B.min)/(B.max-B.min):0; mupSave(); mupPlaceBub(); muLayout(); },
    ()=>{ MUP.mode="bar"; mupSave(); muBar(); });
  mu$("muMin").addEventListener("click",e=>{ e.stopPropagation(); MUP.mode="bub"; mupSave(); muBar(); });
  mu$("muX").addEventListener("click",e=>{ e.stopPropagation(); muHalt(""); MU.cur=""; MU.it=null; MU.kind=""; MU.playing=false; MU.last=""; muSave(); MUP.mode="bar"; mupSave(); muShowKind(); muBar(); muRender(); });
  addEventListener("resize",()=>{ mupPlaceBar(); mupPlaceBub(); });
})();

/* ---- Tabs ---- */
function tab(n){
  try{ muTab(); }catch(e){}                          // v12.20: 🎵
  document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.id==="p"+n));
  document.querySelectorAll(".appbar button").forEach(b=>b.classList.toggle("on",b.dataset.tab===n));
  if(n==="Input") inpLoad();                          // v12.18
  if(n==="Journal") loadJournal();
  if(n==="Trade") loadShots();                        // v9.1
  if(n==="Analiis") loadLevels(true);                 // v12.3
  try{ localStorage.setItem("mp_tab",n); }catch(e){}
  scrollTo({top:0,behavior:"instant"});
}
document.querySelectorAll(".appbar button[data-tab]").forEach(b=>{
  b.addEventListener("click",()=>tab(b.dataset.tab));
});
/* v12.21: Muusig + Fariimo nav-ka */
(function(){
  const m=$("#navMus"), c=$("#navChat");
  if(m) m.addEventListener("click",()=>muOpen());
  if(c) c.addEventListener("click",()=>{ chToastHide(); openChat(); });
  const g=$("#chToastGo"), x=$("#chToastX");
  if(g) g.addEventListener("click",()=>{ chToastHide(); openChat(); });
  if(x) x.addEventListener("click",chToastHide);
  addEventListener("resize",chToastPos);
})();
try{ const t=localStorage.getItem("mp_tab"); if(t && $("#p"+t)) tab(t); }catch(e){}

if(accSel)accSel.addEventListener("change",()=>{tick(); if(jLoaded)loadJournal(); if(SH.loaded)loadShots(); LV.sym=""; if($("#pAnaliis").classList.contains("on")) loadLevels(true);});
setInterval(()=>{ if(!document.hidden && $("#pTrade").classList.contains("on")) loadShots(); },60000);   // v9.1
/* v5.4 BANDWIDTH: 5s -> 12s, oo marka bogga la qariyo GEBI AHAAN wuu joogsanayaa.
   Taleefanka oo furan maalin dhan: ~3 MB halkii 30 MB. */
const POLL_MS=12000;
let pollT=null;
function pollStart(){ if(pollT) return; pollT=setInterval(tick,POLL_MS); }
function pollStop(){ if(pollT){ clearInterval(pollT); pollT=null; } }
document.addEventListener("visibilitychange",()=>{
  if(document.hidden) pollStop(); else { tick(); pollStart(); }
});
tick(); pollStart();
setInterval(()=>{ if(!document.hidden && jLoaded && $("#pJournal").classList.contains("on")) loadJournal(); },60000);
</script></body></html>"""

T_ADMIN = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1013">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="MOHA PRO">
<script>if("serviceWorker" in navigator){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){});});}</script>
<title>MOHA PRO — Admin</title><style>""" + CSS + """</style></head><body>
<div class="top">
  <span class="brand">MOHA PRO · ADMIN</span><span class="spacer"></span>
  <a class="btn" href="/dashboard">Dashboard</a><a class="btn" href="/logout">Bax</a>
</div>
<div class="wrap">
  <div class="card" style="margin-bottom:16px">
    <h2>Isticmaalayaasha</h2>
    <div class="scroll xscroll"><table>
      <thead><tr><th>Account</th><th>Magac</th><th>Xaalad</th><th>Doorka</th>
        <th>Password</th><th>Xog</th><th>Galitaankii u dambeeyay</th><th>Ficil</th></tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td><b>{{ r.account }}</b></td>
          <td>{{ r.name or "—" }}</td>
          <td>{% if r.approved %}<span class="tag buy">LA ANSIXIYAY</span>
              {% else %}<span class="tag sell">SUGAYA</span>{% endif %}</td>
          <td>{{ r.role }}{% if not r.can_control %} · akhris{% endif %}</td>
          <td>{% if r.pw_self %}<span class="tag buy">gaar ah</span>
              {% else %}<span class="tag">Render</span>{% endif %}</td>
          <td>{% if r.online %}<span class="tag buy">ONLINE</span>
              {% elif r.has_data %}<span class="tag">offline</span>
              {% else %}<span class="tag">—</span>{% endif %}</td>
          <td>{{ r.last_login }}</td>
          <td style="white-space:nowrap">
            {% if r.account != me %}
            <form method="post" action="/admin/user" style="display:inline">
              <input type="hidden" name="account" value="{{ r.account }}">
              {% if r.approved %}
                <button name="action" value="revoke">Xidh</button>
                {% if r.can_control %}<button name="action" value="control_off">Akhris kaliya</button>
                {% else %}<button name="action" value="control_on">Ogolow amar</button>{% endif %}
              {% else %}
                <button name="action" value="approve" class="b-go">Ansixi</button>
              {% endif %}
              {% if r.pw_self %}<button name="action" value="pw_env">Password dib u celi</button>{% endif %}
              <button name="action" value="delete" class="b-stop">Tirtir</button>
            </form>
            {% else %}<span class="tag">adiga</span>{% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- v11: furaha bot-ka -->
  <div class="card" id="keys" style="margin-bottom:16px">
    <h2>Furaha bot-ka · qof kasta</h2>
    <p class="note" style="margin-top:0">Fure kasta <b>hal account oo keliya</b> ayuu furaa. Isticmaalaha u dir furihiisa →
      MT5 → Inputs → <code>MohaPro_Key</code>. {% if legacy %}Furaha guud ee hore wuu sii shaqeynayaa <b>account aan weli fure lahayn oo keliya</b>.{% else %}Furaha guud ee hore <b>waa damman yahay</b>.{% endif %}</p>
    <!-- v12: master -->
    <form method="post" action="/admin/master" class="mst">
      <div><div class="knm">Sitinka macaamiisha (master)</div>
        <div class="kseen" style="margin-top:2px">Macmiil kasta oo "sitinkaaga raac" leh wuxuu qaataa sitinka account-kan. Maamul → KAYDI &amp; DIR account-kan → dhammaan way qaataan.</div></div>
      <div class="mrow"><select name="account" id="masterSel">
        <option value="">— dooro account —</option>
        {% for a in mopts %}<option value="{{ a }}" {% if a == master %}selected{% endif %}>#{{ a }}</option>{% endfor %}
      </select><button class="kb gold" type="submit">Keydi</button></div>
    </form>
    {% for k in keyrows %}
    <div class="krow" id="k{{ k.account }}">
      <div class="khd"><span class="kav">{{ (k.name or k.account)[:1]|upper }}</span>
        <div class="kmid"><div class="knm">{{ k.name or "—" }}{% if k.is_master %} <span class="kmst">MASTER</span>{% endif %}</div><div class="kac">#{{ k.account }}</div></div>
        {% if not k.has %}<span class="kst">FURE MA LEH</span>
        {% elif not k.active %}<span class="kst off">XANNIBAN</span>
        {% elif k.live %}<span class="kst ok">SHAQEYNAYA</span>
        {% else %}<span class="kst wait">SUGAYA</span>{% endif %}
      </div>
      {% if k.has %}
      <div class="kkey{% if not k.active %} dim{% endif %}"><code>{{ k.key if k.active else "— furaha waa la joojiyay —" }}</code>
        {% if k.active %}<button type="button" class="kcp" data-k="{{ k.key }}">Koobi</button>{% endif %}</div>
      <div class="kseen">{% if not k.active %}Bot-kiisu xog ma diri karo, amarrona ma qaadan karo
        {% elif k.seen is none %}Weli bot-ku ma isticmaalin{% else %}Bot-ku wuu isticmaalay · {{ k.seen }} ilbiriqsi ka hor{% endif %}</div>
      {% endif %}
      <form method="post" action="/admin/key" class="kbtns">
        <input type="hidden" name="account" value="{{ k.account }}">
        <button name="action" value="new" class="kb gold">{{ "Fure cusub" if k.has else "Samee fure" }}</button>
        {% if k.has and k.active %}<button name="action" value="off" class="kb red">Xannib</button>{% endif %}
        {% if k.has and not k.active %}<button name="action" value="on" class="kb">Fur</button>{% endif %}
      </form>
      {% if not k.is_master %}
      <!-- v12: laysinka + oggolaanshaha -->
      <details class="klic"{% if k.lic %} open{% endif %}>
        <summary>{% if k.lic %}Laysin ·
          {% if k.lic.state == "OK" %}<b class="lok">ACTIVE</b> · {{ k.until_s }} · {{ k.lic.days }} maalmood ayaa haray
          {% elif k.lic.state == "BLOCKED" %}<b class="lbad">FURAHA WAA XANNIBAN</b>
          {% else %}<b class="lbad">WUU DHACAY</b> · bot-ku trade cusub ma furo{% endif %}
          {% else %}Laysin ma leh · u samee macmiil{% endif %}</summary>
        <form method="post" action="/admin/license" class="kbtns">
          <input type="hidden" name="account" value="{{ k.account }}">
          <button name="action" value="add30" class="kb gold">+30 maalmood</button>
          <button name="action" value="add365" class="kb gold">+1 sano</button>
          {% if k.lic and k.lic.state == "OK" %}<button name="action" value="stop" class="kb red">Jooji</button>{% endif %}
        </form>
        {% if k.lic %}
        <form method="post" action="/admin/license" class="kperm">
          <input type="hidden" name="account" value="{{ k.account }}"><input type="hidden" name="action" value="perms">
          <div class="kph">Maxaa macmiilka loo oggol yahay?</div>
          {% for pk in perm_keys %}
          <label class="kpr"><span>{{ perm_names[pk] }}</span><input type="checkbox" name="p_{{ pk }}" {% if k.lic.perms[pk] %}checked{% endif %}><i class="tg"></i></label>
          {% if pk == "risk" %}<div class="kpr sub"><span>Xadka risk-ka</span><span><input name="lo" type="number" step="0.01" min="0.01" max="10" value="{{ '%.2f'|format(k.lic.perms.lo) }}"> – <input name="hi" type="number" step="0.01" min="0.01" max="10" value="{{ '%.2f'|format(k.lic.perms.hi) }}"> %</span></div>{% endif %}
          {% endfor %}
          <label class="kpr" style="margin-top:6px"><span><b>Sitinkaaga (master) raac</b>{% if not master %} <small class="lbad">· master lama dooran</small>{% endif %}</span><input type="checkbox" name="follow" {% if k.lic.follow %}checked{% endif %}><i class="tg"></i></label>
          <button class="kb gold" style="width:100%;margin-top:8px" type="submit">Keydi oggolaanshaha</button>
        </form>
        <form method="post" action="/admin/license" style="margin-top:6px">
          <input type="hidden" name="account" value="{{ k.account }}">
          <button name="action" value="remove" class="klink">Ka saar laysinka (account caadi ah ka dhig)</button>
        </form>
        {% endif %}
      </details>
      {% endif %}
    </div>
    {% endfor %}
    <p class="note"><b>Fure cusub</b> → kii hore isla markiiba wuu dhintaa (bot-ka furihii hore wata wuu go'ayaa ilaa furaha cusub la geliyo).</p>
  </div>
  <style>
  .krow{border-top:1px solid #262624;padding:12px 0}.krow:first-of-type{border-top:none}
  .khd{display:flex;align-items:center;gap:10px}.kav{width:34px;height:34px;border-radius:50%;background:#2a2620;color:#f0cf86;display:flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 34px}
  .kmid{flex:1;min-width:0}.knm{font-weight:650}.kac{font-size:12px;color:var(--ink3)}
  .kst{font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px;background:#2a2a28;color:var(--ink2);white-space:nowrap}
  .kst.ok{background:rgba(38,170,110,.2);color:#7fe0ab}.kst.wait{background:rgba(230,160,60,.2);color:#f0c070}.kst.off{background:rgba(208,59,59,.2);color:#f2a3a3}
  .kkey{margin-top:9px;display:flex;align-items:center;gap:8px;background:#101113;border:1px solid var(--line);border-radius:10px;padding:8px 10px}
  .kkey code{flex:1;font:600 13px ui-monospace,Menlo,Consolas,monospace;color:#f0cf86;letter-spacing:.03em;word-break:break-all}
  .kkey.dim code{color:#6f7076}
  .kcp,.kb{font-size:12px;font-weight:700;padding:7px 11px;border-radius:8px;border:1px solid #3a3a38;background:#232322;color:#ddd;cursor:pointer}
  .kseen{font-size:11.5px;color:var(--ink3);margin-top:6px}
  .kbtns{display:flex;gap:7px;margin-top:8px}.kbtns .kb{flex:1}
  .mst{background:#141517;border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:4px 0 8px}
  .mrow{display:flex;gap:8px;margin-top:8px}.mrow select{flex:1;min-width:0}
  .kmst{font-size:10px;font-weight:800;letter-spacing:.06em;color:#f0cf86;border:1px solid rgba(217,174,85,.5);border-radius:999px;padding:1px 7px;vertical-align:middle}
  .klic{margin-top:10px;background:#141517;border:1px solid var(--line);border-radius:12px;padding:9px 12px}
  .klic summary{cursor:pointer;font-size:12.5px;color:var(--ink2)}
  .lok{color:#7fe0ab}.lbad{color:#f2a3a3}
  .kph{font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin:12px 0 4px}
  .kpr{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;margin:0;border-top:1px solid #222;font-size:13px;color:var(--ink);cursor:pointer;position:relative}
  .kpr.sub{cursor:default;border-top:none;padding-top:0;color:var(--ink3);font-size:12px}
  .kpr.sub input{width:62px;padding:4px 6px}
  .kpr input[type=checkbox]{position:absolute;opacity:0;width:1px;height:1px}
  .kpr .tg{width:38px;height:22px;border-radius:99px;background:#343a47;position:relative;flex:0 0 38px}
  .kpr .tg:after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#9aa0ac;transition:left .15s}
  .kpr input:checked+.tg{background:#2e9c68}.kpr input:checked+.tg:after{left:19px;background:#fff}
  .kpr input:focus-visible+.tg{outline:2px solid #f0cf86;outline-offset:2px}
  .klink{background:none;border:none;color:var(--ink3);font-size:12px;text-decoration:underline;cursor:pointer;padding:4px 0}
  .kb.gold{border-color:rgba(217,174,85,.7);color:#f0cf86}.kb.red{border-color:rgba(208,59,59,.6);color:#f2a3a3}
  </style>
  <script>
  document.querySelectorAll(".kcp").forEach(function(b){ b.addEventListener("click",function(){
    var k=b.getAttribute("data-k"), done=function(){ b.textContent="✓"; setTimeout(function(){ b.textContent="Koobi"; },1500); };
    if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(k).then(done,function(){ prompt("Koobi:",k); }); }
    else { prompt("Koobi:",k); }
  }); });
  </script>

  <div class="card" style="margin-bottom:16px">
    <h2>Ku dar isticmaale toos ah</h2>
    <form method="post" action="/admin/create"
          style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));align-items:end">
      <div><label for="ca">Account MT5</label>
        <input id="ca" name="account" inputmode="numeric" required></div>
      <div><label for="cn">Magac</label><input id="cn" name="name"></div>
      <div><label for="cp">Password</label>
        <input id="cp" name="password" type="password" minlength="8" required></div>
      <div><button class="btn-pri" style="margin:0" type="submit">KU DAR</button></div>
    </form>
    <p class="note">Isticmaalaha sidan loo abuuray si toos ah ayaa loo ansixiyaa.</p>
    <p class="note"><b>Tiirka Password:</b> <i>Render</i> = password-ku wuxuu ka yimaadaa
    <code>EXTRA_USERS</code>, boot kasta waa dib loo dhisayaa.
    <i>gaar ah</i> = qofku isagu wuu beddelay — mar dambe lama tirtirayo.
    "Password dib u celi" = dib ugu noqo kii Render (boot-ka xiga).</p>
  </div>

  {% if orphans %}
  <div class="card">
    <h2>Account xog soo dirtay laakiin aan isticmaale lahayn</h2>
    <p class="note">{{ orphans|join(", ") }}</p>
    <p class="note">Kuwan EA ayaa soo diraya. Abuur isticmaale lambarkiisa si uu u arko xogtiisa.</p>
  </div>
  {% endif %}
</div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
