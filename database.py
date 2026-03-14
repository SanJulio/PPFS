import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=False)

DATABASE_URL = os.environ.get("DATABASE_URL")

USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ppfs.db"


def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        balance REAL NOT NULL,
        type TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        account TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'manual'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_expenses (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        day INTEGER,
        account TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS income (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        frequency TEXT NOT NULL,
        account TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS savings_rules (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        day INTEGER NOT NULL,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        from_account TEXT NOT NULL,
        to_account TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS future_events (
        id SERIAL PRIMARY KEY,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        account TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flask_sessions (
        sid TEXT PRIMARY KEY,
        data TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS investments (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        initial_amount REAL NOT NULL,
        date TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS investment_updates (
        id SERIAL PRIMARY KEY,
        investment_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        value REAL NOT NULL,
        date TEXT NOT NULL
    )
    """)

    db.commit()

    # Add include_in_overview column if it doesn't exist yet
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name='accounts' AND column_name='include_in_overview'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN include_in_overview INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN include_in_overview INTEGER NOT NULL DEFAULT 1")
            db.commit()
    except Exception as e:
        print(f">>> Column migration error: {e}", flush=True)
        try:
            db.rollback()
        except:
            pass

    cursor.close()
    db.close()