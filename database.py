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

    db.commit()
    db.close()