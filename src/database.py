import sqlite3
import os
import secrets
import hashlib

class PantryDatabase:
    def __init__(self, db_path="pantry.db"):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Enable WAL mode for high concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            
            # 1. Base Tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS households (
                    household_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash BYTES NOT NULL,
                    salt BYTES NOT NULL,
                    household_id TEXT,
                    email TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (household_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id TEXT,
                    category TEXT,
                    item_name TEXT,
                    quantity INTEGER,
                    purchase_date DATE,
                    user_lambda REAL,
                    FOREIGN KEY (household_id) REFERENCES households (household_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS purchase_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id TEXT,
                    category TEXT,
                    interval_days REAL,
                    FOREIGN KEY (household_id) REFERENCES households (household_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    household_id TEXT,
                    category TEXT,
                    item_name TEXT,
                    quantity INTEGER,
                    purchase_date DATE,
                    recorded_by TEXT,
                    FOREIGN KEY (household_id) REFERENCES households (household_id)
                )
            """)
            
            # 2. THE MIGRATION: Check if 'email' column exists in 'users', add it if missing
            cursor.execute("PRAGMA table_info(users)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'email' not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")

            conn.commit()

    def create_user(self, username, password, household_name, email):
        """Creates a new user, hashes password, generates household, and saves email."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Generate salt and hash password
            salt = os.urandom(16)
            pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 260000)
            
            # Create household
            hh_id = secrets.token_hex(4)
            cursor.execute("INSERT INTO households (household_id, name) VALUES (?, ?)", (hh_id, household_name))
            
            # Insert user with email
            try:
                cursor.execute("""
                    INSERT INTO users (username, password_hash, salt, household_id, email) 
                    VALUES (?, ?, ?, ?, ?)
                """, (username, pwd_hash, salt, hh_id, email))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False # Username already exists

    def verify_user(self, username, password):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, password_hash, salt, household_id, email FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            if row:
                user_id, stored_hash, salt, hh_id, email = row
                pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 260000)
                if pwd_hash == stored_hash:
                    return {'id': user_id, 'username': username, 'household_id': hh_id, 'email': email}
            return None

    def update_user_email(self, user_id, email):
        """Used to migrate existing legacy users who logged in without an email."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
            conn.commit()