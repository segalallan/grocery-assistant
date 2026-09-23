import sqlite3
import os
import secrets
import hashlib
import uuid

class PantryDatabase:
    def __init__(self, db_path="pantry.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        """Provides a raw connection configured for dictionary-like row access."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            
            # Base Tables aligned with app.py UI
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS households (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_code TEXT UNIQUE,
                    name TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash BYTES NOT NULL,
                    salt BYTES NOT NULL,
                    household_id INTEGER,
                    email TEXT,
                    display_name TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id INTEGER,
                    category TEXT,
                    item_name_en TEXT,
                    item_name_he TEXT,
                    units INTEGER,
                    purchase_date DATE,
                    user_lambda REAL,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS purchase_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id INTEGER,
                    category TEXT,
                    interval_days REAL,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id INTEGER,
                    category TEXT,
                    item_name_en TEXT,
                    item_name_he TEXT,
                    units INTEGER,
                    purchase_date DATE,
                    recorded_by TEXT,
                    recorded_at TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS shopping_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id INTEGER,
                    item_name_en TEXT,
                    item_name_he TEXT,
                    category TEXT,
                    source TEXT,
                    sort_order INTEGER,
                    added_at DATE,
                    added_by TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS household_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id INTEGER,
                    name_en TEXT,
                    name_he TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (id)
                )
            """)
            conn.commit()

    def create_user(self, email, password, first_name, last_name, household_name):
        """Creates a new user, hashes password, generates household, and saves email."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            salt = os.urandom(16)
            pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 260000)
            
            hh_code = str(uuid.uuid4())[:8].upper()
            cursor.execute("INSERT INTO households (household_code, name) VALUES (?, ?)", (hh_code, household_name))
            hh_id = cursor.lastrowid
            
            # Combine First and Last name for the display name
            display_name = f"{first_name} {last_name}".strip()
            
            try:
                cursor.execute("""
                    INSERT INTO users (username, password_hash, salt, household_id, email, display_name) 
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (email, pwd_hash, salt, hh_id, email, display_name))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False 

    def change_password(self, email, new_password):
        """Updates a user's password."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            salt = os.urandom(16)
            pwd_hash = hashlib.pbkdf2_hmac('sha256', new_password.encode('utf-8'), salt, 260000)
            cursor.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (pwd_hash, salt, email))
            conn.commit()
            return cursor.rowcount > 0 

    def verify_user(self, username, password):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, password_hash, salt FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            if row:
                pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), row["salt"], 260000)
                if pwd_hash == row["password_hash"]:
                    return True
            return False

    def update_user_email(self, user_id, email):
        with self.get_connection() as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
            conn.commit()

    def get_household_categories(self, household_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name_en as en, name_he as he FROM household_categories WHERE household_id = ?", (household_id,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]