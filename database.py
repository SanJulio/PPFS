import os
import sqlite3
import logging
from pathlib import Path
from dotenv import load_dotenv

# Set up logging so we can track database errors and info messages
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file (won't override already-set env vars)
load_dotenv(override=False)

# Check if we're using Postgres (production) or SQLite (local dev)
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = DATABASE_URL is not None

# Only import psycopg2 if we're on Postgres (not needed for local SQLite)
if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# SQLite fallback paths (only used locally)
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ppfs.db"


# --- DATABASE CONNECTION ---
# Opens a fresh database connection for each request
# Postgres on production (Render), SQLite locally
def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


# --- DATABASE RELEASE ---
# Closes the connection after each request
# We use direct connections (no pooling) to avoid connection exhaustion on Render free tier
def release_db(conn):
    try:
        conn.close()
    except Exception as e:
        logger.debug(f"Error closing database connection: {e}")


# --- DATABASE INITIALISATION ---
# Creates all tables on first run, and runs any column migrations needed
# Safe to run on every startup — uses IF NOT EXISTS and checks before altering
def init_db():
    db = get_db()
    cursor = db.cursor()

    # All tables — created if they don't exist yet
    tables = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            type TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            account TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'manual'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scheduled_expenses (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            day INTEGER,
            account TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS income (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            frequency TEXT NOT NULL,
            account TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS savings_rules (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            day INTEGER NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'monthly',
            from_account TEXT NOT NULL,
            to_account TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS future_events (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            account TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS flask_sessions (
            sid TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS investments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            initial_amount REAL NOT NULL,
            date TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS investment_updates (
            id SERIAL PRIMARY KEY,
            investment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            value REAL NOT NULL,
            date TEXT NOT NULL
        )
        """,
    ]

    # Run each table creation statement, rolling back on error so other tables still get created
    for table in tables:
        try:
            cursor.execute(table)
            db.commit()
        except Exception as e:
            logger.error(f"Table creation error: {e}")
            try:
                db.rollback()
            except Exception as rb_error:
                logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.include_in_overview ---
    # Lets users hide accounts from the financial overview widget
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
        logger.error(f"Column migration error (include_in_overview): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.verify_token_expires_at ---
    # Stores expiry timestamp for email verification and password reset tokens
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='verify_token_expires_at'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN verify_token_expires_at TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN verify_token_expires_at TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Token expiration migration error: {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.is_pro ---
    # Tracks whether a user has an active Pro subscription (set via Stripe webhook)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='is_pro'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN is_pro INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN is_pro INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (is_pro): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.stripe_customer_id ---
    # Stores the Stripe customer ID so we can manage subscriptions and open the billing portal
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='stripe_customer_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (stripe_customer_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: transactions.category ---
    # Adds spending category to transactions (e.g. Food, Transport, Bills)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='transactions' AND column_name='category'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE transactions ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE transactions ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (category): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    cursor.close()
    release_db(db)
