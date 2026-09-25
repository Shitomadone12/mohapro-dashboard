# -*- coding: utf-8 -*-
"""
MOHA PRO — Cloud Dashboard v2 (multi-user, MT5 account login)
=============================================================
Hal fayl. Flask + SQLite. Render.com diyaar.

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
    SESSION_COOKIE_SECURE=os.environ.get("RENDER", "") != "" or os.environ.get("FORCE_HTTPS", "") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

VALID_COMMANDS = [
    "START", "STOP", "CLOSE_ALL", "CLOSE_PROFIT",
    "STRATEGY:SR", "STRATEGY:BB", "STRATEGY:EMA",
    "STRATEGY:SMC", "STRATEGY:VSA", "STRATEGY:POC",
    "SET:RESET",
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
}

def valid_command(cmd):
    """True + amarka nadiifsan, ama False + sabab."""
    if cmd in VALID_COMMANDS:
        return True, cmd
    m = re.match(r"^SET:([A-Z]+)=([0-9]+(?:\.[0-9]+)?)$", cmd)
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
        "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"
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


def _presented_token():
    cached = getattr(g, "_ptok", None)          # /update wuu tirtiraa "token" jidhka -> kaydi
    if cached is not None:
        return cached
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip():
        g._ptok = auth[7:].strip()
        return g._ptok
    body = request.get_json(silent=True, force=True) or {}
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
           "BE": "prot", "LOCKMODE": "prot"}          # DLOSS, MAXDD, MGMT = admin oo keliya


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
            eff.update(_cfg_row(con, m)[0])
    eff.update({k: v for k, v in (ovr.get("a") or {}).items() if k in CFG_KEYS})
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
    d = request.get_json(silent=True, force=True) or {}
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
    with db() as con:
        con.execute(
            "INSERT INTO snapshots(account,bot,data,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET bot=excluded.bot,"
            " data=excluded.data, updated_at=excluded.updated_at",
            (acc, bot, json.dumps(d, ensure_ascii=False), now))

        _save_closed(con, acc, d.get("trades"))
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
    d = request.get_json(silent=True, force=True) or {}
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
            "SLTP", "SL", "TP", "LOT", "STEPON", "STEP", "STEPSTART", "BE", "LOCKMODE", "ADAPT", "MGMT")


def _cfg_cmds(vals, rev):
    out = []
    for k in CFG_KEYS:
        if k in vals:
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
    clean, bad = {}, []
    for k, v in raw.items():
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
    with db() as con:
        lr = _lic_row(con, acc)
        if lr is not None:                                    # v12: macmiil (admin ama isaga) -> ovr
            ovr = _jload(lr["ovr"])
            side = "c" if lic is not None else "a"
            part = dict(ovr.get(side) or {}); part.update(clean); ovr[side] = part
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
        rev = int(r["rev"]) + 1 if r else 1
        con.execute(
            "INSERT INTO ea_config(account,data,rev,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET data=excluded.data, rev=excluded.rev, updated_at=excluded.updated_at",
            (acc, json.dumps(merged), rev, now))
        cmds = _cfg_cmds(clean, rev)
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

    data = json.loads(snap["data"]) if snap else {}
    age = (now - snap["updated_at"]) if snap else None
    online = (age is not None and age < STALE_SECONDS)

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
    )


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
                "close" if cmd in ("CLOSE_ALL", "CLOSE_PROFIT") else "")
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
    d = request.get_json(silent=True, force=True) or {}
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

/* ---------- v10: kaadhka maamulka (qaybo) ---------- */
.grp{border:1px solid var(--line);border-radius:14px;padding:4px 12px 6px;margin:12px 0;background:#161615}
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
</style></head><body>

<div class="hero">
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
    </span>
  </div>
  <div class="hero-in">
    <h1>MOHA PRO <b id="heroVer"></b></h1>
    <div class="chips">
      <span class="chip"><span class="dot" id="dot"></span><span id="st">Xiriirinaya…</span></span>
      <span class="chip">MT5</span>
      <span class="chip" id="heroAcc">—</span>
    </div>
  </div>
</div>
<input type="file" id="pick" accept="image/png,image/jpeg,image/webp">

<div class="top">
  {% if is_admin %}
  <select id="accSel" autocomplete="off" style="width:auto;padding:7px 10px;font-size:13px">
    {% for a in accounts %}<option value="{{ a }}" {% if a==sel %}selected{% endif %}>{{ a }}</option>{% endfor %}
    {% if me not in accounts %}<option value="{{ me }}" {% if me==sel %}selected{% endif %}>{{ me }}</option>{% endif %}
  </select>
  {% else %}<span class="pill">Account: {{ me }}</span>{% endif %}
  <span class="spacer"></span>
  <span class="pill">{{ name }}</span>
  {% if is_admin %}<a class="btn" href="/admin">Maamul</a>{% endif %}
  <a class="btn" href="/password">Password</a>
  <a class="btn" href="/logout">Bax</a>
</div>

<div class="wrap">

  <!-- ============ GUUD ============ -->
  <section class="pane on" id="pGuud">
    <div class="grid">
      <div class="tile"><div class="k">Balance</div><div class="v neu" id="bal">—</div></div>
      <div class="tile"><div class="k">Equity</div><div class="v neu" id="eq">—</div></div>
      <div class="tile"><div class="k">Faa'iido xidhan (maanta)</div><div class="v" id="pf">—</div></div>
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

    {% if can_control %}
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
      <label for="stratSel" style="margin-top:16px">Beddel xeeladda</label>
      <select id="stratSel">
        <option value="">— dooro —</option>
        <option value="STRATEGY:SR">SR</option>
        <option value="STRATEGY:BB">Bollinger</option>
        <option value="STRATEGY:EMA">EMA</option>
        <option value="STRATEGY:SMC">SMC</option>
        <option value="STRATEGY:VSA">VSA</option>
        <option value="STRATEGY:POC">POC</option>
      </select>
      <div class="note" id="cmdNote">Amarku wuxuu gaadhayaa EA-da 3–5 ilbiriqsi gudahood.</div>
    </div>

    <!-- v5: xidhitaanka faa'iidada -->
    <div class="card" style="margin-bottom:16px">
      <h2>Faa'iidada la xidhay <span class="cnt" id="cLock"></span></h2>
      <div class="scroll"><table id="tl">
        <thead><tr><th>Waqti</th><th>Symbol</th><th style="text-align:right">Faa'iido</th>
          <th style="text-align:right">Xidhay</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">Weli wax lama xidhin.</td></tr></tbody>
      </table></div>
      <div class="note" id="lockSum" style="margin-top:10px">—</div>
    </div>
    {% endif %}

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
    <!-- v5: MAAMULKA (SL / TP / LOT / STEP-LOCK) -->
    <div class="card" id="mCard" style="margin-bottom:16px">
      <p class="sec-t">Maamulka bot-ka</p>
      <div class="savebar" id="mSave"><span class="dt"></span>—</div>
      {% if lic %}<div class="lockband" id="mLockBand"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg><span>Waxa quful ah <b>admin-ka ayaa maamula</b> — waad arki kartaa oo keliya.</span></div>{% endif %}

      <div class="grp"><div class="gh">1 · KHATARTA</div>
        <div class="frow"><span>Risk trade kasta</span><span><input id="mRISK" type="number" min="0.01" max="10" step="0.05"> <i>%</i></span></div>
        <div class="frow"><span>Khasaaraha maalinlaha <small>(kadib wuu joogsadaa maanta)</small></span><span><input id="mDLOSS" type="number" min="0" max="50" step="0.5"> <i>%</i></span></div>
        <div class="frow"><span>Drawdown ugu badan <small>(emergency stop)</small></span><span><input id="mMAXDD" type="number" min="0" max="100" step="1"> <i>%</i></span></div>
      </div>

      <div class="grp"><div class="gh">2 · XEELADDA</div>
        <div class="frow"><span>Sniper <small>(tayo · CHoCH gudaha zone-ka)</small></span><span><button class="sw" id="mSNIPER" type="button" aria-label="Sniper"><i></i></button></span></div>
        <div class="frow"><span>Trade maalintii <small>(chart-yada oo dhan)</small></span><span class="stp"><button type="button" id="mSNDAYm" aria-label="Ka yaree">−</button><b id="mSNDAY">3</b><button type="button" id="mSNDAYp" aria-label="Ku dar">+</button></span></div>
        <div class="frow"><span>RR <small>(TP = SL × …)</small></span><span><input id="mSNRR" type="number" min="1" max="10" step="0.5"> <i>R</i></span></div>
        <div class="frow"><span>SL ugu weyn <small>(ka weyn → trade ma furmo)</small></span><span><input id="mSNSLMAX" type="number" min="0" max="500" step="1"> <i>pip</i></span></div>
        <div class="frow"><span>Filtarka wararka <small>(jooji warka ka hor/kadib)</small></span><span><button class="sw" id="mNEWS" type="button" aria-label="Filtarka wararka"><i></i></button></span></div>
      </div>

      <div class="grp"><div class="gh">3 · SL / TP</div>
      <div class="seg" id="mSLTP" role="radiogroup" aria-label="Habka SL/TP">
        <button type="button" data-m="2" role="radio">SNIPER</button>
        <button type="button" data-m="0" role="radio">FIXED</button>
        <button type="button" data-m="1" role="radio">ATR</button>
      </div>
      <div class="sltph" id="mSLTPHint">—</div>
      <div class="frow" id="rSL"><span>Stop Loss</span><span><input id="mSL" type="number" min="0" max="5000" step="1"> <i>pip</i></span></div>
      <div class="frow" id="rTP"><span>Take Profit</span><span><input id="mTP" type="number" min="0" max="5000" step="1"> <i>pip</i></span></div>
      <div class="frow"><span>Lot <small>(0 = auto risk)</small></span><span><input id="mLOT" type="number" min="0" max="100" step="0.01"> <i>lot</i></span></div>
      </div>

      <div class="grp"><div class="gh">4 · MAAMULKA TRADE-KA FURAN</div>
      <div class="frow"><span>Step-Lock</span><span><button class="sw" id="mSTEPON" type="button"><i></i></button></span></div>
      <div id="mWarn"></div>
      <div class="frow" id="rADAPT"><span>ATR maamulo SL/TP <small>(suuqa ayuu la socdaa · ATR oo keliya)</small></span><span><button class="sw" id="mADAPT" type="button"><i></i></button></span></div>
      <div class="frow"><span>SL ha joogo break-even <small>(dami = SL kor buu u socdaa)</small></span><span><button class="sw" id="mLOCKMODE" type="button"><i></i></button></span></div>
      <div class="frow"><span>Break-even</span><span><button class="sw" id="mBE" type="button"><i></i></button></span></div>
      <div class="frow"><span>Maamulka trade-ka <small>(dami = SL/TP oo keliya)</small></span><span><button class="sw" id="mMGMT" type="button"><i></i></button></span></div>
      <div class="frow"><span>Tallaabo kasta</span><span><input id="mSTEP" type="number" min="1" max="5000" step="1"> <i>pip</i></span></div>
      <div class="frow"><span>Bilowga</span><span><input id="mSTEPSTART" type="number" min="1" max="5000" step="1"> <i>pip</i></span></div>
      </div>
      <div class="acts" style="margin-top:14px;grid-template-columns:2fr 1fr">
        <button class="act go" id="mSend" style="flex-direction:row;gap:9px;padding:14px">KAYDI &amp; DIR</button>
        {% if not lic %}<button class="act" id="mReset" style="flex-direction:row;gap:8px;padding:14px;border-color:var(--line);color:var(--ink2)">CELI</button>{% endif %}
      </div>
      <div class="note" id="mNote">Bot-ku wuxuu hadda isticmaalayaa: —</div>
      <div class="note" style="margin-top:6px">Sitinkan ayaa bot-ka u ah <b>.set</b>: server-ka ayuu ku kaydsan yahay, <b>chart kasta</b> wuu gaadhayaa, MT5 ama VPS dib u kicin → bot-ku halkan ayuu ka soo qaadanayaa (EA v67.4+).</div>
    </div>
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
  <section class="pane" id="pChart">
    <div class="card">
      <h2>Equity — 12 saac ee ugu dambeeyay</h2>
      <svg class="chart" id="chart" role="img" aria-label="Equity-ga waqti ahaan"></svg>
      <div class="note" id="chartNote"></div>
    </div>
  </section>

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
  {% endif %}
  <button data-tab="Chart">
    <svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/></svg>Chart</button>
  <button data-tab="Journal">
    <svg viewBox="0 0 24 24"><path d="M5 3h11l4 4v14H5Z"/><path d="M9 9h7M9 13h7M9 17h4"/></svg>Journal<span class="tbadge" id="hcBadge" style="display:none"></span></button>
</nav>
<div class="tip" id="tip"></div>
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
  const vv=verOf(d); $("#heroVer").textContent=vv?("v"+vv):"";
  $("#heroAcc").textContent="#"+d.account;
  setBrand(d.brand||"");   // v4.1: madhan -> kii hore ayaa la sii hayaa
  paintSettings(x.settings); paintLocks(x.locks);   // v5
  paintRaw(x, d);                                   // v6
  paintAnalysis(d.analysis);                        // v7
  paintHealth(d);                                   // v7.2
  paintChatBadge(d.chat_unread);                    // v9
  paintSave(d);                                     // v10
  paintMyKey(d);                                    // v11
  paintLic(d);                                      // v12
  $("#st").textContent=d.online?(RS.long+" · "+(d.age||0)+"s ka hor")
    :(d.age==null?"Xog lama helin":"OFFLINE · "+d.age+"s ka hor");
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

  const rows=Array.isArray(x.trades)?x.trades:[];
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
  const btn=$("#btnPic"); const old=btn.textContent;
  btn.disabled=true; btn.textContent="Cusboonaysiinaya…";
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
  btn.disabled=false; btn.textContent=old;
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

/* ---- v7.2: CAAFIMAADKA BOT-KA ----
   Xogta EA-du soo dirto ayaa la baadhaa. Wax kasta oo is-burinaya ama khatar ah -> kaadh.
   red = khalad dhab ah · amb = digniin · ok = wax walba waa sax */
function healthChecks(d){
  const x=d.data||{}, st=x.settings||null, an=d.analysis||[], out=[];
  const add=(lv,t,dd)=>out.push({lv:lv,t:t,d:dd||""});
  // 1) xiriirka
  if(d.age==null){ add("red","Bot-ka xog lagama helin","EA-du weli wax uma dirin server-ka. Hubi EnableCloudDashboard = true iyo URL-ka WebRequest-ka (Tools → Options → Expert Advisors)."); return out; }
  if(!d.online){
    const m=Math.round(d.age/60);
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
      add("red","Maamulka trade-ka waa DAMMAN",dd+" Shid: Guud → qabta «Maamulka trade-ka» → DIR BOT-KA.");
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
    const s=f.offsetWidth||56, bar=(document.querySelector(".appbar")||{offsetHeight:0}).offsetHeight||0;
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
function paintChatBadge(n){
  const b=$("#chatBadge"); n=Number(n||0);
  if(n>0){ b.textContent=n>99?"99+":n; b.hidden=false; } else b.hidden=true;
}

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
const MF=["SL","TP","LOT","STEP","STEPSTART"];
let mTouched=false, mSeeded=false;
function swSet(id,on){ const e=$("#"+id); if(e) e.classList.toggle("on",!!on); }
function swGet(id){ const e=$("#"+id); return e && e.classList.contains("on"); }
["mSTEPON","mBE","mLOCKMODE","mADAPT","mMGMT","mSNIPER","mNEWS"].forEach(id=>{ const e=$("#"+id); if(e) e.addEventListener("click",()=>{ e.classList.toggle("on"); mTouched=true; }); });
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
  SNDAY:"day",NEWS:"prot",STEPON:"prot",STEP:"prot",STEPSTART:"prot",BE:"prot",LOCKMODE:"prot"};
const PERM_EL={mRISK:"risk",mDLOSS:"",mMAXDD:"",mSNIPER:"sltp",mSNDAYm:"day",mSNRR:"sltp",mSNSLMAX:"sltp",mNEWS:"prot",
  mSL:"sltp",mTP:"sltp",mLOT:"lot",mSTEPON:"prot",mADAPT:"sltp",mLOCKMODE:"prot",mBE:"prot",mMGMT:"",mSTEP:"prot",mSTEPSTART:"prot"};
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
  if(!permOK("sltp")){ const sg=$("#mSLTP"); if(sg){ sg.classList.add("locked"); sg.querySelectorAll("button").forEach(b=>b.disabled=true); } }
  document.querySelectorAll("[data-perm]").forEach(b=>{ if(!permOK(b.dataset.perm)){ b.classList.add("lockd"); b.disabled=true; } });
  const ss=$("#stratSel"); if(ss){ ss.disabled=true; ss.style.display="none"; const lb=document.querySelector('label[for="stratSel"]'); if(lb) lb.style.display="none"; }
  const r=$("#mRISK"); if(r && permOK("risk")){ r.min=PERMS.lo; r.max=PERMS.hi;
    const lab=r.closest(".frow").querySelector("span"); if(lab && !lab.querySelector(".rhint")) lab.insertAdjacentHTML("beforeend",'<small class="rhint">xadka: '+Number(PERMS.lo).toFixed(2)+' – '+Number(PERMS.hi).toFixed(2)+'%</small>'); }
  const any=Object.keys(PERM_OF).some(k=>permOK(PERM_OF[k])); const sb=$("#mSend"); if(sb && !any){ sb.disabled=true; sb.classList.add("lockd"); }
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

/* ---- v9.3: habka SL/TP (SNIPER / FIXED / ATR) ---- */
let mMode=null, mRR=3, mSniperOn=true, mModeKnown=false;
function modeHint(){
  const h=$("#mSLTPHint"); if(!h) return;
  const m=mMode;
  let t="";
  if(m===2) t=mSniperOn?("<b>SNIPER</b> — SL sweep-ka gadaashiisa ayuu dhigaa · TP = SL × "+mRR+". SL/TP gacanta iyo ATR waa la iska dhaafayaa.")
                      :"<b>SNIPER</b> — Sniper-ku bot-ka waa ka damman yahay: SL = zone-ka (distal), TP = RR. SL/TP gacanta waa la iska dhaafayaa.";
  else if(m===0) t="<b>FIXED</b> — SL iyo TP waa pip-ka aad hoos ku qorto. Trade kasta isku mid.";
  else if(m===1) t="<b>ATR</b> — SL/TP waxaa laga xisaabiyaa dhaqdhaqaaqa suuqa (ATR). Shid <b>ATR maamulo</b> si ay suuqa ula socdaan trade-ka furan.";
  else t="Dooro habka.";
  if(!mModeKnown) t+='<div style="margin-top:6px;color:#e0a86a">Bot-ku waa nooc hore — habkan wuxuu u baahan yahay EA v67.3.</div>';
  h.innerHTML=t;
  document.querySelectorAll("#mSLTP button").forEach(b=>{ const on=Number(b.dataset.m)===m; b.classList.toggle("on",on); b.setAttribute("aria-checked",on?"true":"false"); });
  ["rSL","rTP"].forEach(id=>{ const r=$("#"+id); if(r) r.classList.toggle("off",m!==0); });
  const ra=$("#rADAPT"); if(ra) ra.classList.toggle("off",m!==1);
}
document.querySelectorAll("#mSLTP button").forEach(b=>b.addEventListener("click",()=>{ mMode=Number(b.dataset.m); mTouched=true; modeHint(); }));
const SLTP_NM=["FIXED","ATR","SNIPER"];

function paintSettings(st){
  if(!st) return;
  const note=$("#mNote");
  const em=(st.sltp===undefined||st.sltp===null)?null:Number(st.sltp);
  const src=em===null?("SL "+(st.sl||0)+"p · TP "+(st.tp||0)+"p")
    :(em===0?("FIXED "+(st.sl||st.fix_sl||0)+"/"+(st.tp||st.fix_tp||0)+"p")
      :(em===2?("SNIPER (RR 1:"+(st.rr||3)+")"):"ATR"+(st.adapt_eff?" · suuqa la socda":"")))
    +(em!==null && Number(st.sltp_src)<0?" (.set)":"");
  if(note) note.textContent="Bot-ku wuxuu hadda isticmaalayaa:  "+src+"  ·  Lot "
    +(st.auto_lot?"auto":(st.lot||0))+"  ·  Step-Lock "+(st.steplock?("ON "+(st.step||0)+"p"):"OFF")
    +"  ·  BE "+(st.be?"ON":"OFF")
    +"  ·  "+(Number(st.lockmode)===1?"SL break-even":"SL tallaabo")
    +((em===null && st.adaptive)?"  ·  ATR maamulaya":"");
  const w=$("#mWarn");
  if(w) w.innerHTML=st.mgmt_off
    ? '<div class="nwarn" style="margin-top:10px">MAAMULKU WAA DAMMAN — trade-ku wuxuu ku xidhmayaa SL ama TP oo keliya. Break-even, trailing iyo ATR midna ma shaqeynayaan.</div>'
    : "";
  if(mTouched && mSeeded) return;          // qofku wuu wax qorayaa - ha ka qaadin gacanta
  const set=(id,v)=>{ const e=$("#"+id); if(e && v!==undefined && v!==null) e.value=v; };
  // v9.3: sanduuqyada SL/TP = pip-ka FIXED (xitaa marka habka kale la isticmaalayo - si FIXED loo diyaariyo)
  set("mSL",(st.fix_sl!==undefined)?st.fix_sl:st.sl); set("mTP",(st.fix_tp!==undefined)?st.fix_tp:st.tp); set("mLOT",st.auto_lot?0:st.lot);
  mModeKnown=(em!==null); mRR=Number(st.rr||3); mSniperOn=(st.sniper_on!==undefined)?!!st.sniper_on:!!st.sniper;
  mMode=(em!==null)?em:(st.sniper?2:(Number(st.sl)>0?0:1));
  modeHint();
  set("mSTEP",st.step); set("mSTEPSTART",st.stepstart);
  set("mRISK",st.risk); set("mDLOSS",st.dloss); set("mMAXDD",st.maxdd); set("mSNRR",st.rr); set("mSNSLMAX",st.snslmax);   // v10
  if(st.snday!==undefined && $("#mSNDAY")) $("#mSNDAY").textContent=st.snday;
  swSet("mSNIPER",(st.sniper_on!==undefined)?!!st.sniper_on:!!st.sniper); swSet("mNEWS",st.news===undefined?true:!!st.news);
  swSet("mSTEPON",st.steplock); swSet("mBE",st.be);
  swSet("mLOCKMODE",Number(st.lockmode)===1); swSet("mADAPT",!!st.adaptive);
  swSet("mMGMT",!st.mgmt_off);            // v7.1: ON = maamulku wuu shaqeynayaa
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
  const sl=num("mSL"), tp=num("mTP"), lot=num("mLOT"), stp=num("mSTEP"), sst=num("mSTEPSTART");
  // v9.3: habka ayaa marka hore. SL/TP pip-ka waxaa la diraa FIXED oo keliya (hore 30/36 ayaa Sniper-ka jebin jiray).
  if(mMode===0||mMode===1||mMode===2) cmds.push("SET:SLTP="+mMode);
  if(mMode===0){
    if(sl!==null && Number(sl)>0) cmds.push("SET:SL="+sl);
    if(tp!==null && Number(tp)>0) cmds.push("SET:TP="+tp);
  }
  if(lot!==null) cmds.push("SET:LOT="+lot);
  if(stp!==null) cmds.push("SET:STEP="+stp);
  if(sst!==null) cmds.push("SET:STEPSTART="+sst);
  cmds.push("SET:STEPON="+(swGet("mSTEPON")?1:0));
  cmds.push("SET:BE="+(swGet("mBE")?1:0));
  cmds.push("SET:LOCKMODE="+(swGet("mLOCKMODE")?1:0));
  cmds.push("SET:ADAPT="+(swGet("mADAPT")?1:0));
  cmds.push("SET:MGMT="+(swGet("mMGMT")?1:0));
  // v10: sitinka cusub
  const r2=num("mRISK"), dl=num("mDLOSS"), md=num("mMAXDD"), rr=num("mSNRR"), sm=num("mSNSLMAX");
  if(r2!==null) cmds.push("SET:RISK="+r2);
  if(dl!==null) cmds.push("SET:DLOSS="+dl);
  if(md!==null) cmds.push("SET:MAXDD="+md);
  if(rr!==null) cmds.push("SET:SNRR="+rr);
  if(sm!==null) cmds.push("SET:SNSLMAX="+sm);
  cmds.push("SET:SNDAY="+(Number($("#mSNDAY").textContent)||0));
  cmds.push("SET:SNIPER="+(swGet("mSNIPER")?1:0));
  cmds.push("SET:NEWS="+(swGet("mNEWS")?1:0));
  const values={}; cmds.forEach(c=>{ const m=/^SET:([A-Z]+)=(.+)$/.exec(c); if(m) values[m[1]]=m[2]; });
  if(PERMS) Object.keys(values).forEach(k=>{ if(!permOK(PERM_OF[k])) delete values[k]; });   // v12: macmiil
  btn.disabled=true; btn.textContent="Kaydinaya…";
  let err="";
  try{
    const body={values:values}; if(accSel)body.account=accSel.value;
    const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json(); if(!d.ok) err=d.error||"Lama kaydin.";
  }catch(e){ err="Internet ma jiro — lama kaydin."; }
  btn.disabled=false; btn.textContent=old;
  if(!err) mTouched=false;
  $("#mNote").textContent = err ? err : "La kaydiyay oo la diray. Chart kasta 20 ilbiriqsi gudahood ayuu qaadanayaa.";
  tick();
}
if($("#mSend"))  $("#mSend").addEventListener("click",sendSettings);
if($("#mReset")) $("#mReset").addEventListener("click",async ()=>{
  const body={reset:true}; if(accSel)body.account=accSel.value;
  await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  mTouched=false; mSeeded=false;
  $("#mNote").textContent="Celis: sitinka app-ka waa la tirtiray - bot-ku wuxuu ku noqonayaa sitinka koodka.";
  tick();
});

document.querySelectorAll("[data-cmd]").forEach(b=>{
  b.addEventListener("click",()=>send(b.dataset.cmd,b));
});
const ss=$("#stratSel");
if(ss)ss.addEventListener("change",()=>{ if(ss.value){send(ss.value,null); ss.value=""; }});

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

/* ---- Tabs ---- */
function tab(n){
  document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.id==="p"+n));
  document.querySelectorAll(".appbar button").forEach(b=>b.classList.toggle("on",b.dataset.tab===n));
  if(n==="Chart") drawChart();
  if(n==="Journal") loadJournal();
  if(n==="Trade") loadShots();                        // v9.1
  if(n==="Analiis") loadLevels(true);                 // v12.3
  try{ localStorage.setItem("mp_tab",n); }catch(e){}
  scrollTo({top:0,behavior:"instant"});
}
document.querySelectorAll(".appbar button").forEach(b=>{
  b.addEventListener("click",()=>tab(b.dataset.tab));
});
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
