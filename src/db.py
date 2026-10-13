"""Database access: connection handling, schema/migrations and account functions.

Every connection goes through `Database.connect()`, which commits on success,
rolls back on error and ALWAYS closes (sqlite3's own context manager doesn't close).
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager

from config import DB_PATH, DEFAULT_CATEGORIES

PBKDF2_ITERATIONS = 260_000


def normalize_key(name_en: str | None, name_he: str | None = None) -> str:
    """Stable identity for an item within a household (lowercased, whitespace-collapsed)."""
    base = (name_en or "").strip() or (name_he or "").strip()
    return re.sub(r"\s+", " ", base).lower()


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS households (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_code TEXT UNIQUE,
        name TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash BLOB NOT NULL,
        salt BLOB NOT NULL,
        household_id INTEGER REFERENCES households(id),
        email TEXT,
        display_name TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS household_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER REFERENCES households(id),
        name_en TEXT,
        name_he TEXT,
        UNIQUE(household_id, name_en)
    )""",
    # Current stock: one row per item the household has on hand.
    """CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        category TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        units INTEGER,
        purchase_date DATE,
        confirmed_until DATE,     -- last date someone said "still going" (censoring point)
        user_lambda REAL,         -- per-unit Weibull scale learned for this item, in days
        last_alerted DATE         -- set when an email alert went out for this stock row
    )""",
    # Observed consumption durations (per unit). One row per completed cycle.
    """CREATE TABLE IF NOT EXISTS purchase_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,            -- NULL for legacy category-only rows
        category TEXT,
        interval_days REAL,       -- days per unit
        units INTEGER,
        kind TEXT,                -- 'repurchase' | 'depleted'
        recorded_at TEXT,
        recorded_by TEXT
    )""",
    # Permanent ledger of every purchase.
    """CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        category TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        units INTEGER,
        purchase_date DATE,
        recorded_by TEXT,
        recorded_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS shopping_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER REFERENCES households(id),
        item_key TEXT,
        item_name_en TEXT,
        item_name_he TEXT,
        category TEXT,
        source TEXT,
        sort_order INTEGER,
        added_at DATE,
        added_by TEXT
    )""",
]

# Columns added after v1. Applied to existing databases with ALTER TABLE.
MIGRATION_COLUMNS = {
    "inventory": {"item_key": "TEXT", "confirmed_until": "DATE", "last_alerted": "DATE"},
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


class Database:
    def __init__(self, db_path: str | None = None):
        # Resolved at call time so tests/jobs can point PANTRY_DB_PATH elsewhere.
        self.db_path = db_path or os.environ.get("PANTRY_DB_PATH") or DB_PATH
        self._init_db()

    # ---------- connections ----------
    @contextmanager
    def connect(self):
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
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for stmt in SCHEMA:
                conn.execute(stmt)
            self._migrate(conn)
            for stmt in INDEXES:
                conn.execute(stmt)

    def _migrate(self, conn):
        for table, cols in MIGRATION_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, col_type in cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        # Backfill item_key on rows created before per-item learning existed.
        for table in ("inventory", "purchases", "shopping_list"):
            rows = conn.execute(
                f"SELECT id, item_name_en, item_name_he FROM {table} WHERE item_key IS NULL"
            ).fetchall()
            for r in rows:
                conn.execute(
                    f"UPDATE {table} SET item_key = ? WHERE id = ?",
                    (normalize_key(r["item_name_en"], r["item_name_he"]), r["id"]),
                )

    # ---------- accounts ----------
    def create_user(self, email, password, first_name, last_name, household_name=None, join_code=None):
        """Returns (ok, error_message)."""
        email = email.strip().lower()
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (email,)).fetchone():
                return False, "An account with this email already exists."

            if join_code:
                row = conn.execute(
                    "SELECT id FROM households WHERE household_code = ?", (join_code.strip().upper(),)
                ).fetchone()
                if not row:
                    return False, "Invalid join code."
                hh_id = row["id"]
            else:
                hh_code = secrets.token_hex(4).upper()
                cur = conn.execute(
                    "INSERT INTO households (household_code, name) VALUES (?, ?)",
                    (hh_code, household_name or "My Household"),
                )
                hh_id = cur.lastrowid
                conn.executemany(
                    "INSERT OR IGNORE INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?)",
                    [(hh_id, en, he) for en, he in DEFAULT_CATEGORIES],
                )

            salt = os.urandom(16)
            conn.execute(
                """INSERT INTO users (username, password_hash, salt, household_id, email, display_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (email, _hash_password(password, salt), salt, hh_id, email,
                 f"{first_name} {last_name}".strip()),
            )
        return True, None

    def authenticate(self, email: str, password: str):
        """Returns the user's profile dict on success, else None."""
        email = email.strip().lower()
        with self.connect() as conn:
            row = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.household_id, u.email,
                          u.password_hash, u.salt, h.name AS household_name, h.household_code
                   FROM users u LEFT JOIN households h ON u.household_id = h.id
                   WHERE u.username = ?""",
                (email,),
            ).fetchone()
        if not row:
            _hash_password(password, b"\x00" * 16)  # keep timing similar for unknown emails
            return None
        if not hmac.compare_digest(_hash_password(password, row["salt"]), row["password_hash"]):
            return None
        user = dict(row)
        user.pop("password_hash")
        user.pop("salt")
        return user

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        """Only for a logged-in user who knows their current password."""
        with self.connect() as conn:
            row = conn.execute("SELECT password_hash, salt FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row or not hmac.compare_digest(_hash_password(current_password, row["salt"]), row["password_hash"]):
                return False
            salt = os.urandom(16)
            conn.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (_hash_password(new_password, salt), salt, user_id),
            )
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
                (household_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_category(self, household_id, name_en, name_he) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?)",
                    (household_id, name_en.strip(), (name_he or name_en).strip()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_category(self, household_id, name_en) -> tuple[bool, int]:
        """Refuses to delete a category that items still use. Returns (deleted, items_using_it)."""
        with self.connect() as conn:
            in_use = conn.execute(
                """SELECT (SELECT COUNT(*) FROM inventory WHERE household_id = ? AND category = ?)
                        + (SELECT COUNT(*) FROM shopping_list WHERE household_id = ? AND category = ?) AS n""",
                (household_id, name_en, household_id, name_en),
            ).fetchone()["n"]
            if in_use:
                return False, in_use
            conn.execute(
                "DELETE FROM household_categories WHERE household_id = ? AND name_en = ?", (household_id, name_en)
            )
        return True, 0
