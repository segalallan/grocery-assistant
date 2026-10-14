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

from config import DB_PATH, DEFAULT_CATEGORIES, get_secret

log = logging.getLogger(__name__)
PBKDF2_ITERATIONS = 260_000

TABLES = ["households", "users", "household_categories", "inventory",
          "purchase_history", "purchases", "shopping_list"]  # dependency order


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
    "users": {"email_alerts": "INTEGER DEFAULT 1", "email_lang": "TEXT DEFAULT 'en'"},
    "inventory": {"item_key": "TEXT", "confirmed_until": "TEXT", "last_alerted": "TEXT"},
    "purchase_history": {"item_key": "TEXT", "units": "INTEGER", "kind": "TEXT"},
    "purchases": {"item_key": "TEXT"},
    "shopping_list": {"item_key": "TEXT"},
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_inv_hh_key ON inventory(household_id, item_key)",
    "CREATE INDEX IF NOT EXISTS ix_hist_hh_key ON purchase_history(household_id, item_key)",
    "CREATE INDEX IF NOT EXISTS ix_hist_hh_cat ON purchase_history(household_id, category)",
    "CREATE INDEX IF NOT EXISTS ix_purch_hh_date ON purchases(household_id, purchase_date)",
    "CREATE INDEX IF NOT EXISTS ix_shop_hh ON shopping_list(household_id)",
    "CREATE INDEX IF NOT EXISTS ix_users_hh ON users(household_id)",
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
                hh_id = conn.execute(
                    "INSERT INTO households (household_code, name) VALUES (?, ?) RETURNING id",
                    (secrets.token_hex(4).upper(), household_name or "My Household"),
                ).fetchone()[0]
                conn.executemany(
                    "INSERT INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    [(hh_id, en, he) for en, he in DEFAULT_CATEGORIES],
                )

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
                             u.password_hash, u.salt, u.email_alerts, u.email_lang,
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
