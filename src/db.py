"""Database access: connections, schema/migrations, accounts and categories.

Two backends, same code:
  * Postgres when DATABASE_URL is set (production: Neon / Supabase).
  * SQLite file otherwise (local development and tests).

All SQL in the app is written once, SQLite-style with `?` placeholders. The Postgres
adapter translates placeholders, and rows from both backends support row["col"] and row[0].
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import DB_PATH, DEFAULT_CATEGORIES, get_secret

log = logging.getLogger(__name__)
PBKDF2_ITERATIONS = 260_000

RESET_CODE_MINUTES = 15      # how long an emailed code stays valid
RESET_MAX_ATTEMPTS = 5       # wrong guesses before the code is burned
RESET_RESEND_SECONDS = 60    # minimum gap between codes for the same account
SESSION_DAYS = 30            # how long "remember me" keeps someone logged in

TABLES = ["households", "users", "household_categories", "products", "product_aliases", "inventory",
          "purchase_history", "purchases", "shopping_list", "password_resets", "login_sessions",
          "feedback", "app_errors"]  # dependency order


def normalize_key(name_en: str | None, name_he: str | None = None) -> str:
    """Stable identity for an item within a household (lowercased, whitespace-collapsed)."""
    base = (name_en or "").strip() or (name_he or "").strip()
    return re.sub(r"\s+", " ", base).lower()


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes(salt), PBKDF2_ITERATIONS)


# ---------------------------------------------------------------------------
# Schema (types filled in per backend)
# ---------------------------------------------------------------------------
_TYPES = {
    "sqlite":   {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB", "real": "REAL"},
    "postgres": {"pk": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA", "real": "DOUBLE PRECISION"},
}
# Dates are stored as ISO 'YYYY-MM-DD' text in both backends (sorts and compares correctly).
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS households (
        id {pk},
        household_code TEXT UNIQUE,
        name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id {pk},
        username TEXT UNIQUE NOT NULL,
        password_hash {blob} NOT NULL,
        salt {blob} NOT NULL,
        household_id INTEGER REFERENCES households(id),
        email TEXT,
        display_name TEXT,
        email_alerts INTEGER DEFAULT 1,   -- 1 = wants restock emails
        email_lang TEXT DEFAULT 'en'      -- 'en' | 'he'
    )""",
    """CREATE TABLE IF NOT EXISTS household_categories (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        name_en TEXT,
        name_he TEXT,
        UNIQUE(household_id, name_en)
    )""",
    # A household's products: what the model learns about. Brand and package size are not part of it.
    """CREATE TABLE IF NOT EXISTS products (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        name_en TEXT,
        name_he TEXT,
        category TEXT,
        ref_amount {real},        -- reference package size in base units (ml / g / pc); NULL = unknown
        ref_unit TEXT,            -- 'ml' | 'g' | 'pc'
        created_at TEXT
    )""",
    # Every way a product has been written ("חלב תנובה 3% 1 ל", "Milk"...), as a normalized match key.
    """CREATE TABLE IF NOT EXISTS product_aliases (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        alias TEXT NOT NULL,
        product_id INTEGER REFERENCES products(id),
        UNIQUE(household_id, alias)
    )""",
    # Current stock: one row per item the household has on hand.
    """CREATE TABLE IF NOT EXISTS inventory (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        category TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        units INTEGER,
        purchase_date TEXT,
        confirmed_until TEXT,     -- last date someone said "still have it" (censoring point)
        user_lambda {real},       -- per-unit Weibull scale learned for this item, in days
        last_alerted TEXT         -- set when an email alert went out for this stock row
    )""",
    # Observed consumption durations (per unit). One row per completed cycle.
    """CREATE TABLE IF NOT EXISTS purchase_history (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,            -- NULL for legacy category-only rows
        category TEXT,
        interval_days {real},     -- days per unit
        units INTEGER,
        kind TEXT,                -- 'repurchase' | 'depleted'
        recorded_at TEXT,
        recorded_by TEXT
    )""",
    # Permanent ledger of every purchase.
    """CREATE TABLE IF NOT EXISTS purchases (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        category TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        units INTEGER,
        purchase_date TEXT,
        recorded_by TEXT,
        recorded_at TEXT
    )""",
    # One pending reset code per user (stored hashed, never in plain text).
    """CREATE TABLE IF NOT EXISTS password_resets (
        id {pk},
        user_id INTEGER REFERENCES users(id),
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,   -- ISO UTC timestamp
        expires_at TEXT NOT NULL,   -- ISO UTC timestamp
        attempts INTEGER DEFAULT 0
    )""",
    # Beta feedback sent from the app.
    """CREATE TABLE IF NOT EXISTS feedback (
        id {pk},
        user_id INTEGER,
        household_id INTEGER,
        email TEXT,
        message TEXT NOT NULL,
        context TEXT,
        created_at TEXT NOT NULL
    )""",
    # Unexpected errors, one row per distinct error (signature), with a running count.
    """CREATE TABLE IF NOT EXISTS app_errors (
        id {pk},
        signature TEXT UNIQUE NOT NULL,
        message TEXT,
        traceback TEXT,
        last_user TEXT,
        count INTEGER DEFAULT 1,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        last_notified TEXT
    )""",
    # "Remember me" logins. The browser cookie holds a random token; only its hash is stored here.
    """CREATE TABLE IF NOT EXISTS login_sessions (
        id {pk},
        user_id INTEGER REFERENCES users(id),
        token_hash TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS shopping_list (
        id {pk},
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        category TEXT,
        source TEXT,
        sort_order INTEGER,
        added_at TEXT,
        added_by TEXT
    )""",
]

# Columns added after v1; applied to older databases.
MIGRATION_COLUMNS = {
    # eff_units = amount in "reference packages" (2 x 2 L with a 1 L reference = 4.0). NULL on old rows → use units.
    "users": {"email_alerts": "INTEGER DEFAULT 1", "email_lang": "TEXT DEFAULT 'en'",
              "intro_dismissed": "INTEGER DEFAULT 0"},
    "inventory": {"item_key": "TEXT", "confirmed_until": "TEXT", "last_alerted": "TEXT"},
    "purchase_history": {"item_key": "TEXT", "units": "INTEGER", "kind": "TEXT"},
    "purchases": {"item_key": "TEXT"},
    "shopping_list": {"item_key": "TEXT"},
}
# Amount tracking (added with products). REAL works on both SQLite and Postgres.
for _t, _cols in {
    "inventory": {"eff_units": "REAL", "size_amount": "REAL", "size_unit": "TEXT"},
    "purchases": {"eff_units": "REAL", "size_amount": "REAL", "size_unit": "TEXT", "raw_name": "TEXT"},
    "purchase_history": {"eff_units": "REAL"},
}.items():
    MIGRATION_COLUMNS.setdefault(_t, {}).update(_cols)

PRODUCT_PREFIX = "prod:"


def product_key(product_id: int) -> str:
    """The item_key used everywhere (inventory, history, shopping list) for a product."""
    return f"{PRODUCT_PREFIX}{int(product_id)}"


def product_id_of(item_key: str | None) -> int | None:
    if item_key and str(item_key).startswith(PRODUCT_PREFIX):
        return int(str(item_key)[len(PRODUCT_PREFIX):])
    return None

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_inv_hh_key ON inventory(household_id, item_key)",
    "CREATE INDEX IF NOT EXISTS ix_hist_hh_key ON purchase_history(household_id, item_key)",
    "CREATE INDEX IF NOT EXISTS ix_hist_hh_cat ON purchase_history(household_id, category)",
    "CREATE INDEX IF NOT EXISTS ix_purch_hh_date ON purchases(household_id, purchase_date)",
    "CREATE INDEX IF NOT EXISTS ix_shop_hh ON shopping_list(household_id)",
    "CREATE INDEX IF NOT EXISTS ix_users_hh ON users(household_id)",
    "CREATE INDEX IF NOT EXISTS ix_products_hh ON products(household_id)",
    "CREATE INDEX IF NOT EXISTS ix_alias_hh ON product_aliases(household_id, alias)",
]


# ---------------------------------------------------------------------------
# Postgres adapter: makes a psycopg connection look like sqlite3 to our code
# ---------------------------------------------------------------------------
class Row:
    """Read-only row supporting row["col"], row[0], dict(row) and row.keys()."""
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names = names
        self._values = tuple(bytes(v) if isinstance(v, memoryview) else v for v in values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._names.index(key)]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _to_pg(sql: str) -> str:
    # '%' is psycopg's placeholder marker, so literal % (e.g. LIKE '%@%') must be doubled first.
    return sql.replace("%", "%%").replace("?", "%s")


class _PgConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cur = self.raw.cursor(row_factory=_row_factory)
        cur.execute(_to_pg(sql), tuple(params))
        return cur

    def executemany(self, sql, seq):
        cur = self.raw.cursor(row_factory=_row_factory)
        cur.executemany(_to_pg(sql), [tuple(p) for p in seq])
        return cur


def _integrity_errors():
    errs = [sqlite3.IntegrityError]
    try:
        import psycopg
        errs.append(psycopg.IntegrityError)
    except ImportError:
        pass
    return tuple(errs)


INTEGRITY_ERRORS = _integrity_errors()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, db_path: str | None = None, database_url: str | None = None):
        """database_url (or the DATABASE_URL secret) selects Postgres; otherwise SQLite at db_path."""
        url = database_url if database_url is not None else (None if db_path else get_secret("DATABASE_URL"))
        if url:
            self.backend = "postgres"
            self.url = url
            self._pool = self._make_pool(url)
            self.db_path = None
        else:
            self.backend = "sqlite"
            self.db_path = db_path or os.environ.get("PANTRY_DB_PATH") or DB_PATH
        self._init_db()

    @property
    def is_postgres(self) -> bool:
        return self.backend == "postgres"

    def describe(self) -> str:
        if self.is_postgres:
            return "Postgres @ " + re.sub(r"//[^@]*@", "//***@", self.url).split("?")[0]
        return f"SQLite @ {self.db_path}"

    @staticmethod
    def _make_pool(url):
        from psycopg_pool import ConnectionPool
        return ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            # prepare_threshold=None keeps this compatible with PgBouncer-style poolers (Supabase, Neon).
            kwargs={"prepare_threshold": None},
            check=ConnectionPool.check_connection,  # hosted DBs drop idle connections
            open=True,
        )

    def close(self):
        if self.is_postgres:
            self._pool.close()

    # ---------- connections ----------
    @contextmanager
    def connect(self):
        """Commits on success, rolls back on error, always releases the connection."""
        if self.is_postgres:
            with self._pool.connection() as raw:  # pool handles commit / rollback
                yield _PgConnection(raw)
            return
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- schema ----------
    def _init_db(self):
        types = _TYPES[self.backend]
        with self.connect() as conn:
            if not self.is_postgres:
                conn.execute("PRAGMA journal_mode=WAL")
            for stmt in _SCHEMA:
                conn.execute(stmt.format(**types))
            self._migrate(conn)
            for stmt in INDEXES:
                conn.execute(stmt)

    def _columns(self, conn, table) -> set[str]:
        if self.is_postgres:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND table_schema = current_schema()",
                (table,))
            return {r[0] for r in rows}
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, conn):
        for table, cols in MIGRATION_COLUMNS.items():
            existing = self._columns(conn, table)
            for col, col_type in cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        # Backfill item_key on rows created before per-item learning existed.
        for table in ("inventory", "purchases", "shopping_list"):
            rows = conn.execute(
                f"SELECT id, item_name_en, item_name_he FROM {table} WHERE item_key IS NULL").fetchall()
            for r in rows:
                conn.execute(f"UPDATE {table} SET item_key = ? WHERE id = ?",
                             (normalize_key(r["item_name_en"], r["item_name_he"]), r["id"]))
        self._migrate_to_products(conn)

    def _migrate_to_products(self, conn):
        """Turn every name-based item_key into a product (one-time; safe to re-run).

        Each distinct (household, item_key) becomes a product named after its most recent row,
        aliases are recorded for its names, and all tables are re-pointed to "prod:<id>".
        """
        from normalize import match_key
        tables = ("inventory", "purchases", "shopping_list")
        legacy = {}
        for table in tables:
            for r in conn.execute(
                    f"SELECT household_id, item_key, item_name_en, item_name_he, category, id FROM {table} "
                    f"WHERE item_key IS NOT NULL AND item_key NOT LIKE 'prod:%' ORDER BY id").fetchall():
                legacy[(r["household_id"], r["item_key"])] = (r["item_name_en"], r["item_name_he"], r["category"])
        for r in conn.execute(
                "SELECT DISTINCT household_id, item_key, category FROM purchase_history "
                "WHERE item_key IS NOT NULL AND item_key NOT LIKE 'prod:%'").fetchall():
            legacy.setdefault((r["household_id"], r["item_key"]), (r["item_key"], None, r["category"]))
        now = datetime.now(timezone.utc).isoformat()
        for (hh, key), (en, he, cat) in legacy.items():
            pid = conn.execute(
                "INSERT INTO products (household_id, name_en, name_he, category, created_at) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id", (hh, en, he, cat, now)).fetchone()[0]
            for text in {en, he, key}:
                k = match_key(text)
                if k:
                    conn.execute("INSERT INTO product_aliases (household_id, alias, product_id) VALUES (?, ?, ?) "
                                 "ON CONFLICT DO NOTHING", (hh, k, pid))
            for table in tables + ("purchase_history",):
                conn.execute(f"UPDATE {table} SET item_key = ? WHERE household_id = ? AND item_key = ?",
                             (product_key(pid), hh, key))

    # ---------- one-time data copy ----------
    def is_empty(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM households").fetchone()[0] == 0

    def import_from_sqlite(self, sqlite_path: str) -> dict:
        """Copy every row (ids preserved) from a SQLite pantry DB into this (empty) database."""
        if not self.is_empty():
            raise RuntimeError("Target database already has data; refusing to import over it.")
        source = Database(db_path=sqlite_path)  # also upgrades the old file's schema first
        counts = {}
        with source.connect() as src, self.connect() as dst:
            for table in TABLES:
                cols = sorted(self._columns(dst, table) & source._columns(src, table))
                rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id").fetchall()
                if rows:
                    marks = ", ".join("?" * len(cols))
                    dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                                    [tuple(r[c] for c in cols) for r in rows])
                counts[table] = len(rows)
                if self.is_postgres:  # keep new ids from colliding with copied ones
                    dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
        return counts

    # ---------- accounts ----------
    @staticmethod
    def _create_household(conn, name):
        hh_id = conn.execute("INSERT INTO households (household_code, name) VALUES (?, ?) RETURNING id",
                             (secrets.token_hex(4).upper(), name or "My Household")).fetchone()[0]
        conn.executemany("INSERT INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?) "
                         "ON CONFLICT DO NOTHING", [(hh_id, en, he) for en, he in DEFAULT_CATEGORIES])
        return hh_id

    HOUSEHOLD_TABLES = ("inventory", "purchases", "purchase_history", "shopping_list",
                        "product_aliases", "products", "household_categories")

    @classmethod
    def _delete_household_data(cls, conn, household_id):
        for t in cls.HOUSEHOLD_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE household_id = ?", (household_id,))
        conn.execute("DELETE FROM households WHERE id = ?", (household_id,))

    def _drop_household_if_empty(self, conn, household_id):
        if household_id and not conn.execute("SELECT 1 FROM users WHERE household_id = ?", (household_id,)).fetchone():
            self._delete_household_data(conn, household_id)

    def leave_household(self, user_id: int, new_household_name: str) -> int:
        """Move the user into a brand-new household of their own. The old household keeps its data
        (it's deleted only if nobody is left in it). Returns the new household id."""
        with self.connect() as conn:
            old = conn.execute("SELECT household_id FROM users WHERE id = ?", (user_id,)).fetchone()
            new_id = self._create_household(conn, new_household_name)
            conn.execute("UPDATE users SET household_id = ? WHERE id = ?", (new_id, user_id))
            self._drop_household_if_empty(conn, old["household_id"] if old else None)
        return new_id

    def delete_user(self, user_id: int):
        """Delete an account, its logins and reset codes. The household's data is deleted too
        if this was its last member; otherwise it stays for the remaining members."""
        with self.connect() as conn:
            row = conn.execute("SELECT household_id FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return
            for t in ("login_sessions", "password_resets"):
                conn.execute(f"DELETE FROM {t} WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE feedback SET user_id = NULL, email = NULL WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._drop_household_if_empty(conn, row["household_id"])

    def set_intro_dismissed(self, user_id: int, dismissed: bool = True):
        with self.connect() as conn:
            conn.execute("UPDATE users SET intro_dismissed = ? WHERE id = ?", (1 if dismissed else 0, user_id))

    def household_members(self, household_id) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id, display_name, email FROM users WHERE household_id = ? ORDER BY id",
                                (household_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def admin_stats(self, today_iso: str) -> list[dict]:
        """Per-household activity for the beta admin view."""
        d7 = (datetime.fromisoformat(today_iso) - timedelta(days=7)).date().isoformat()
        d30 = (datetime.fromisoformat(today_iso) - timedelta(days=30)).date().isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT h.id, h.name, h.household_code,
                          (SELECT COUNT(*) FROM users u WHERE u.household_id = h.id) AS members,
                          (SELECT COUNT(*) FROM products p WHERE p.household_id = h.id) AS products,
                          (SELECT COUNT(*) FROM inventory i WHERE i.household_id = h.id) AS in_pantry,
                          (SELECT COUNT(*) FROM purchases pu WHERE pu.household_id = h.id AND pu.purchase_date >= ?) AS buys_7d,
                          (SELECT COUNT(*) FROM purchases pu WHERE pu.household_id = h.id AND pu.purchase_date >= ?) AS buys_30d,
                          (SELECT COUNT(*) FROM purchase_history ph WHERE ph.household_id = h.id) AS cycles,
                          (SELECT MAX(pu.purchase_date) FROM purchases pu WHERE pu.household_id = h.id) AS last_purchase
                   FROM households h ORDER BY h.id""", (d7, d30)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def create_user(self, email, password, first_name, last_name, household_name=None, join_code=None):
        """Returns (ok, error_message)."""
        email = email.strip().lower()
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (email,)).fetchone():
                return False, "An account with this email already exists."

            if join_code:
                row = conn.execute("SELECT id FROM households WHERE household_code = ?",
                                   (join_code.strip().upper(),)).fetchone()
                if not row:
                    return False, "Invalid join code."
                hh_id = row["id"]
            else:
                hh_id = self._create_household(conn, household_name)

            salt = os.urandom(16)
            conn.execute(
                """INSERT INTO users (username, password_hash, salt, household_id, email, display_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (email, _hash_password(password, salt), salt, hh_id, email, f"{first_name} {last_name}".strip()),
            )
        return True, None

    def authenticate(self, email: str, password: str):
        """Returns the user's profile dict on success, else None."""
        email = email.strip().lower()
        with self.connect() as conn:
            row = conn.execute(self._PROFILE_SQL + " WHERE u.username = ?", (email,)).fetchone()
        if not row:
            _hash_password(password, b"\x00" * 16)  # keep timing similar for unknown emails
            return None
        if not hmac.compare_digest(_hash_password(password, row["salt"]), bytes(row["password_hash"])):
            return None
        user = {k: row[k] for k in row.keys()}
        user.pop("password_hash")
        user.pop("salt")
        return user

    _PROFILE_SQL = """SELECT u.id, u.username, u.display_name, u.household_id, u.email,
                             u.password_hash, u.salt, u.email_alerts, u.email_lang, u.intro_dismissed,
                             h.name AS household_name, h.household_code
                      FROM users u LEFT JOIN households h ON u.household_id = h.id"""

    def get_user(self, user_id: int = None, email: str = None):
        """Profile dict (no password fields) by id or email, or None."""
        with self.connect() as conn:
            if user_id is not None:
                row = conn.execute(self._PROFILE_SQL + " WHERE u.id = ?", (user_id,)).fetchone()
            else:
                row = conn.execute(self._PROFILE_SQL + " WHERE u.username = ?",
                                   ((email or "").strip().lower(),)).fetchone()
        if not row:
            return None
        user = {k: row[k] for k in row.keys()}
        user.pop("password_hash")
        user.pop("salt")
        return user

    def get_email_prefs(self, user_id: int) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT email_alerts, email_lang FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"enabled": True, "lang": "en"}
        return {"enabled": row["email_alerts"] != 0, "lang": row["email_lang"] or "en"}

    def set_email_prefs(self, user_id: int, enabled: bool, lang: str):
        lang = lang if lang in ("en", "he") else "en"
        with self.connect() as conn:
            conn.execute("UPDATE users SET email_alerts = ?, email_lang = ? WHERE id = ?",
                         (1 if enabled else 0, lang, user_id))

    # ---------- remember-me sessions ----------
    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self, user_id: int, now: datetime | None = None) -> str:
        """Returns a new random token to put in the browser cookie."""
        now = now or datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("DELETE FROM login_sessions WHERE expires_at < ?", (now.isoformat(),))
            conn.execute(
                "INSERT INTO login_sessions (user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (user_id, self._token_hash(token), now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()))
        return token

    def user_for_session(self, token: str | None, now: datetime | None = None):
        """Profile of the user a cookie token belongs to, or None if it's unknown/expired."""
        if not isinstance(token, str) or not token:
            return None
        now = now or datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute("SELECT user_id, expires_at FROM login_sessions WHERE token_hash = ?",
                               (self._token_hash(token),)).fetchone()
        if not row or now > datetime.fromisoformat(row["expires_at"]):
            return None
        return self.get_user(user_id=row["user_id"])

    def end_session(self, token: str | None):
        if isinstance(token, str) and token:
            with self.connect() as conn:
                conn.execute("DELETE FROM login_sessions WHERE token_hash = ?", (self._token_hash(token),))

    def end_all_sessions(self, user_id: int, keep_token: str | None = None):
        """Log a user out everywhere (after a password change/reset), optionally keeping this device."""
        with self.connect() as conn:
            if isinstance(keep_token, str) and keep_token:
                conn.execute("DELETE FROM login_sessions WHERE user_id = ? AND token_hash != ?",
                             (user_id, self._token_hash(keep_token)))
            else:
                conn.execute("DELETE FROM login_sessions WHERE user_id = ?", (user_id,))

    # ---------- feedback & errors ----------
    def add_feedback(self, user_id, household_id, email, message, context=""):
        with self.connect() as conn:
            conn.execute("INSERT INTO feedback (user_id, household_id, email, message, context, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (user_id, household_id, email, message.strip()[:4000], context,
                          datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def list_feedback(self, limit=100):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def log_error(self, signature, message, tb, user, now=None, notify_every_minutes=60) -> bool:
        """Record an error occurrence. Returns True if the admin should be emailed about it now."""
        now = now or datetime.now(timezone.utc)
        iso = now.isoformat(timespec="seconds")
        with self.connect() as conn:
            row = conn.execute("SELECT id, last_notified FROM app_errors WHERE signature = ?", (signature,)).fetchone()
            if not row:
                conn.execute("INSERT INTO app_errors (signature, message, traceback, last_user, count, first_seen, "
                             "last_seen, last_notified) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                             (signature, message[:1000], tb[-8000:], user, iso, iso, iso))
                return True
            notify = (not row["last_notified"] or
                      now - datetime.fromisoformat(row["last_notified"]) >= timedelta(minutes=notify_every_minutes))
            conn.execute("UPDATE app_errors SET count = count + 1, last_seen = ?, last_user = ?, message = ?, "
                         "traceback = ?" + (", last_notified = ?" if notify else "") + " WHERE id = ?",
                         (iso, user, message[:1000], tb[-8000:], *([iso] if notify else []), row["id"]))
            return notify

    def list_errors(self, limit=50):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM app_errors ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    # ---------- Google sign-in ----------
    def create_oauth_user(self, email, display_name, household_name=None, join_code=None):
        """Account for someone who signs in with Google: no usable password (they can set one via reset).

        Returns (ok, error_message)."""
        ok, err = self.create_user(email, secrets.token_urlsafe(32), display_name or email.split("@")[0], "",
                                   household_name, join_code)
        return ok, err

    # ---------- password reset ----------
    @staticmethod
    def _reset_hash(user_id: int, code: str) -> str:
        return hashlib.sha256(f"{user_id}:{code.strip()}".encode()).hexdigest()

    def create_reset_code(self, email: str, now: datetime | None = None) -> dict:
        """Make a fresh 6-digit code for this email.

        Returns {"status": "ok", "code", "user"} | {"status": "unknown"} | {"status": "cooldown"}.
        The UI must show the same message for every status so it never reveals who has an account.
        """
        now = now or datetime.now(timezone.utc)
        user = self.get_user(email=email)
        if not user:
            return {"status": "unknown"}
        with self.connect() as conn:
            last = conn.execute("SELECT created_at FROM password_resets WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                                (user["id"],)).fetchone()
            if last and (now - datetime.fromisoformat(last["created_at"])).total_seconds() < RESET_RESEND_SECONDS:
                return {"status": "cooldown"}
            code = f"{secrets.randbelow(1_000_000):06d}"
            conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
            conn.execute(
                "INSERT INTO password_resets (user_id, code_hash, created_at, expires_at, attempts) VALUES (?, ?, ?, ?, 0)",
                (user["id"], self._reset_hash(user["id"], code), now.isoformat(),
                 (now + timedelta(minutes=RESET_CODE_MINUTES)).isoformat()))
        return {"status": "ok", "code": code, "user": user}

    def reset_password_with_code(self, email: str, code: str, new_password: str,
                                 now: datetime | None = None) -> tuple[bool, str]:
        """Returns (ok, reason). reason: 'ok' | 'invalid' | 'expired' | 'locked'."""
        now = now or datetime.now(timezone.utc)
        user = self.get_user(email=email)
        if not user:
            return False, "invalid"
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM password_resets WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                               (user["id"],)).fetchone()
            if not row:
                return False, "invalid"
            if row["attempts"] >= RESET_MAX_ATTEMPTS:
                return False, "locked"
            if now > datetime.fromisoformat(row["expires_at"]):
                conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
                return False, "expired"
            if not hmac.compare_digest(self._reset_hash(user["id"], code), row["code_hash"]):
                conn.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return False, ("locked" if row["attempts"] + 1 >= RESET_MAX_ATTEMPTS else "invalid")
            salt = os.urandom(16)
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                         (_hash_password(new_password, salt), salt, user["id"]))
            conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM login_sessions WHERE user_id = ?", (user["id"],))
        return True, "ok"

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        """Only for a logged-in user who knows their current password."""
        with self.connect() as conn:
            row = conn.execute("SELECT password_hash, salt FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row or not hmac.compare_digest(_hash_password(current_password, row["salt"]),
                                                  bytes(row["password_hash"])):
                return False
            salt = os.urandom(16)
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                         (_hash_password(new_password, salt), salt, user_id))
        return True

    def update_user_email(self, user_id: int, email: str) -> bool:
        """Email is also the login name, so both columns move together."""
        email = email.strip().lower()
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (email, user_id)).fetchone():
                return False
            conn.execute("UPDATE users SET email = ?, username = ? WHERE id = ?", (email, email, user_id))
        return True

    # ---------- categories ----------
    def get_household_categories(self, household_id):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name_en AS en, name_he AS he FROM household_categories WHERE household_id = ? ORDER BY id",
                (household_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def add_category(self, household_id, name_en, name_he) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?)",
                    (household_id, name_en.strip(), (name_he or name_en).strip()))
            return True
        except INTEGRITY_ERRORS:
            return False

    def delete_category(self, household_id, name_en) -> tuple[bool, int]:
        """Refuses to delete a category that items still use. Returns (deleted, items_using_it)."""
        with self.connect() as conn:
            in_use = conn.execute(
                """SELECT (SELECT COUNT(*) FROM inventory WHERE household_id = ? AND category = ?)
                        + (SELECT COUNT(*) FROM shopping_list WHERE household_id = ? AND category = ?) AS n""",
                (household_id, name_en, household_id, name_en)).fetchone()["n"]
            if in_use:
                return False, in_use
            conn.execute("DELETE FROM household_categories WHERE household_id = ? AND name_en = ?",
                         (household_id, name_en))
        return True, 0
