import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ppfs.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()

    db.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        balance REAL NOT NULL,
        type TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        account TEXT NOT NULL,
        type TEXT NOT NULL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        day INTEGER,
        account TEXT NOT NULL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS income (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        frequency TEXT NOT NULL,
        account TEXT NOT NULL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS savings_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        day INTEGER NOT NULL,
        from_account TEXT NOT NULL,
        to_account TEXT NOT NULL
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS future_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        account TEXT NOT NULL
    )
    """)

    db.commit()
    db.close()