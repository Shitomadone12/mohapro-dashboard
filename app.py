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
                   render_template_string, make_response)
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
]

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

def token_ok():
    """EA auth: Bearer header ama token-ka jidhka JSON-ka."""
    if not CLOUD_TOKEN or len(CLOUD_TOKEN) < 8:
        return False
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if hmac.compare_digest(auth[7:].strip(), CLOUD_TOKEN):
            return True
    body = request.get_json(silent=True) or {}
    t = str(body.get("token", ""))
    if t and hmac.compare_digest(t, CLOUD_TOKEN):
        return True
    q = request.args.get("token", "")
    if q and hmac.compare_digest(q, CLOUD_TOKEN):
        return True
    return False

def clean_account(v):
    return re.sub(r"\D", "", str(v or ""))[:20]

# --------------------------------------------------------------------------
# EA endpoints
# --------------------------------------------------------------------------
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

    now = time.time()
    d["_server_ts"] = now
    with db() as con:
        con.execute(
            "INSERT INTO snapshots(account,bot,data,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET bot=excluded.bot,"
            " data=excluded.data, updated_at=excluded.updated_at",
            (acc, bot, json.dumps(d, ensure_ascii=False), now))

        _save_closed(con, acc, d.get("trades"))

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
        con.execute(
            "DELETE FROM closed_trades WHERE account=? AND id NOT IN"
            " (SELECT id FROM closed_trades WHERE account=? ORDER BY ts DESC LIMIT 500)",
            (acc, acc))
    return jsonify(ok=True, saved=n)


@app.get("/api/commands")
def ea_commands():
    """EA-du waxay soo qaadataa amarrada sugaya. Mid kasta hal mar oo keliya."""
    if not token_ok():
        return jsonify(ok=False, error="bad token"), 401
    acc = clean_account(request.args.get("account"))
    bot = request.args.get("bot", "")
    if not acc:
        acc = "bot-" + re.sub(r"[^A-Za-z0-9]", "", bot)[:16] or "bot-unknown"
    now = time.time()
    with db() as con:
        rows = con.execute(
            "SELECT id,cmd FROM commands WHERE account=? AND taken_at IS NULL"
            " ORDER BY id ASC LIMIT 10", (acc,)).fetchall()
        if rows:
            con.execute("UPDATE commands SET taken_at=? WHERE id IN (%s)"
                        % ",".join("?" * len(rows)),
                        [now] + [r["id"] for r in rows])
    payload = {"token": CLOUD_TOKEN, "account": acc,
               "commands": [r["cmd"] for r in rows], "ts": int(now)}
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
    return render_template_string(
        T_DASH, me=u["account"], name=u["name"] or u["account"],
        is_admin=(u["role"] == "admin"),
        can_control=bool(u["can_control"]), accounts=accounts)


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

    data = json.loads(snap["data"]) if snap else {}
    age = (now - snap["updated_at"]) if snap else None
    online = (age is not None and age < STALE_SECONDS)

    ct = []
    for r in closed:
        try:
            ct.append(json.loads(r["data"]))
        except ValueError:
            pass

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
    )


@app.post("/api/command")
@login_required
def api_command():
    u = request.user
    if not u["can_control"] and u["role"] != "admin":
        return jsonify(ok=False, error="Amar diritaanka lagaama ogola."), 403
    body = request.get_json(silent=True) or {}
    cmd = str(body.get("cmd", "")).strip().upper()
    if cmd not in VALID_COMMANDS:
        return jsonify(ok=False, error="Amar aan la aqoon."), 400
    acc = clean_account(body.get("account")) if u["role"] == "admin" else u["account"]
    acc = acc or u["account"]
    with db() as con:
        n = con.execute("SELECT COUNT(*) c FROM commands WHERE account=? AND taken_at IS NULL",
                        (acc,)).fetchone()["c"]
        if n >= 10:
            return jsonify(ok=False, error="Amaro badan ayaa safka ku jira."), 429
        con.execute("INSERT INTO commands(account,cmd,by_account,created_at) VALUES(?,?,?,?)",
                    (acc, cmd, u["account"], time.time()))
    return jsonify(ok=True, cmd=cmd, account=acc)

# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
RANGES = {"day": 1, "week": 7, "month": 30, "year": 365, "all": 0}


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
    return render_template_string(T_ADMIN, rows=rows, me=request.user["account"],
                                  orphans=orphans)


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
    return redirect(url_for("admin"))

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
.dot.on{background:var(--good)} .dot.off{background:var(--crit)}
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
<meta name="viewport" content="width=device-width,initial-scale=1">
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
<meta name="viewport" content="width=device-width,initial-scale=1">
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
<meta name="viewport" content="width=device-width,initial-scale=1">
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
.hero-ph{position:absolute;inset:0;background:
  radial-gradient(900px 340px at 50% -8%,rgba(57,135,229,.34),transparent 64%),
  linear-gradient(160deg,#1a2331 0%,#131312 70%)}
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
.edit svg,.rm svg{stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.edit svg{width:15px;height:15px}
.rm{display:none;align-items:center;justify-content:center;width:36px;height:36px;
  border-radius:50%;padding:0;background:rgba(43,30,30,.8);
  border:1px solid rgba(255,255,255,.2);color:#f0a0a0;backdrop-filter:blur(8px)}
.rm:hover{background:rgba(58,38,38,.9)}
.rm.on{display:inline-flex}
.rm svg{width:16px;height:16px}

.hero h1{font-size:32px;margin:0;letter-spacing:-.01em;text-shadow:0 2px 18px rgba(0,0,0,.8)}
.hero h1 b{color:var(--s1);font-weight:800}
.chips{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-start;margin-top:11px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:6px 11px;
  border-radius:999px;background:rgba(18,18,17,.74);border:1px solid rgba(255,255,255,.14);
  color:#d8d7cf;backdrop-filter:blur(8px)}
#pick{display:none}

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
@media(min-width:900px){.cols.two{grid-template-columns:1fr}}
</style></head><body>

<div class="hero">
  <div class="hero-ph"></div>
  <div class="hero-photo" id="heroPhoto"></div>
  <div class="hero-fade"></div>
  <div class="hero-top">
    <span class="chip"><span class="dot" id="dot2"></span><span id="st2">…</span></span>
    <span class="hero-btns">
      <button class="rm" id="btnPicDel" title="Ka saar sawirka" aria-label="Ka saar sawirka">
        <svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
      <button class="edit" id="btnPic" title="Beddel sawirka" aria-label="Beddel sawirka">
        <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
        <span>Beddel sawirka</span>
      </button>
    </span>
  </div>
  <div class="hero-in">
    <h1>MOHA PRO <b>v59</b></h1>
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
  <select id="accSel" style="width:auto;padding:7px 10px;font-size:13px">
    {% for a in accounts %}<option value="{{ a }}" {% if a==me %}selected{% endif %}>{{ a }}</option>{% endfor %}
    {% if me not in accounts %}<option value="{{ me }}" selected>{{ me }}</option>{% endif %}
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
    {% if can_control %}
    <div class="card" style="margin-bottom:16px">
      <p class="sec-t">Amarrada</p>
      <div class="acts">
        <button class="act go" data-cmd="START">
          <svg viewBox="0 0 24 24"><path d="m6 4 14 8-14 8Z"/></svg>SHID</button>
        <button class="act stop" data-cmd="STOP">
          <svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>DAMI</button>
        <button class="act warn" data-cmd="CLOSE_ALL">
          <svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>XIDH</button>
      </div>
      <div class="acts" style="margin-top:10px">
        <button class="act calm" data-cmd="CLOSE_PROFIT" style="grid-column:span 3;flex-direction:row;gap:9px;padding:13px">
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
    {% endif %}

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

    <div class="card"><p class="sec-t">Xogta account-ka</p>
      <div class="note" id="meta" style="margin:0">—</div></div>
  </section>

  <!-- ============ TRADE ============ -->
  <section class="pane" id="pTrade">
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
  <button data-tab="Chart">
    <svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/></svg>Chart</button>
  <button data-tab="Journal">
    <svg viewBox="0 0 24 24"><path d="M5 3h11l4 4v14H5Z"/><path d="M9 9h7M9 13h7M9 17h4"/></svg>Journal</button>
</nav>
<div class="tip" id="tip"></div>
<script>
const $=s=>document.querySelector(s);
const accSel=$("#accSel");
let HIST=[];

const money=v=>(v==null||isNaN(v))?"—":Number(v).toLocaleString("en-US",
  {minimumFractionDigits:2,maximumFractionDigits:2});
const cls=v=>v>0?"pos":(v<0?"neg":"neu");

function paint(d){
  const x=d.data||{};
  $("#dot").className="dot "+(d.online?"on":"off");
  $("#dot2").className="dot "+(d.online?"on":"off");          // v4: badge-ka kore
  $("#st2").textContent=d.online?"ONLINE":"OFFLINE";
  $("#heroAcc").textContent="#"+d.account;
  setBrand(d.brand||"");
  $("#st").textContent=d.online?("ONLINE · "+(d.age||0)+"s ka hor")
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
      tr.innerHTML='<td>'+esc(t.sym||t.symbol||"")+'</td>'+tag(t)+
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
let BRAND="";
function setBrand(src){
  if(src===BRAND) return;
  BRAND=src;
  const ph=$("#heroPhoto");            // v4: hal sawir - HERO buuxa
  if(src){
    ph.style.backgroundImage="url('"+src.replace(/'/g,"%27")+"')";
    ph.classList.add("on");
    $("#btnPicDel").classList.add("on");
  }else{
    ph.style.backgroundImage=""; ph.classList.remove("on");
    $("#btnPicDel").classList.remove("on");
  }
}

$("#btnPic").addEventListener("click",()=>$("#pick").click());

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

$("#btnPicDel").addEventListener("click",async ()=>{
  const body={img:""};
  if(accSel)body.account=accSel.value;
  await fetch("/api/branding",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  setBrand("");
});

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
  try{ localStorage.setItem("mp_tab",n); }catch(e){}
  scrollTo({top:0,behavior:"instant"});
}
document.querySelectorAll(".appbar button").forEach(b=>{
  b.addEventListener("click",()=>tab(b.dataset.tab));
});
try{ const t=localStorage.getItem("mp_tab"); if(t && $("#p"+t)) tab(t); }catch(e){}

if(accSel)accSel.addEventListener("change",()=>{tick(); if(jLoaded)loadJournal();});
tick(); setInterval(tick,5000);
setInterval(()=>{ if(jLoaded && $("#pJournal").classList.contains("on")) loadJournal(); },30000);
</script></body></html>"""

T_ADMIN = """<!doctype html><html lang="so"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
