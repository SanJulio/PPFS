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

    # --- MIGRATION: users.notification_digest ---
    # User's preferred spending-summary email frequency: 'off', 'weekly', or 'monthly'
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='notification_digest'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN notification_digest VARCHAR(10) DEFAULT 'off'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN notification_digest VARCHAR(10) DEFAULT 'off'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (notification_digest): {e}")
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

    # --- MIGRATION: scheduled_expenses.frequency ---
    # Adds support for yearly bills (fires once per year on a specific day/month)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='scheduled_expenses' AND column_name='frequency'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (scheduled_expenses.frequency): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: scheduled_expenses.month ---
    # Stores the month for yearly bills (1-12); NULL means applies every month (monthly bills)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='scheduled_expenses' AND column_name='month'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN month INTEGER")
                db.commit()
        else:
            cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN month INTEGER")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (scheduled_expenses.month): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- analytics_events table ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    ts TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_user_id ON analytics_events(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics_events(event)")
            db.commit()
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_user_id ON analytics_events(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics_events(event)")
            db.commit()
    except Exception as e:
        logger.error(f"analytics_events table creation error: {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.day ---
    # Stores which day of the month monthly income is received (1-31); defaults to 1
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='day'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN day INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN day INTEGER NOT NULL DEFAULT 1")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.day): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.auto_apply_enabled ---
    # Whether scheduled bills/income are automatically applied to accounts when due (default on)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='auto_apply_enabled'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN auto_apply_enabled INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN auto_apply_enabled INTEGER NOT NULL DEFAULT 1")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (auto_apply_enabled): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.auto_apply_confirm ---
    # Whether to show a confirmation banner before applying (default on)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='auto_apply_confirm'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN auto_apply_confirm INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN auto_apply_confirm INTEGER NOT NULL DEFAULT 1")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (auto_apply_confirm): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: scheduled_expenses.last_applied ---
    # Tracks when this bill was last auto-applied, to prevent double-applying
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='scheduled_expenses' AND column_name='last_applied'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN last_applied TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN last_applied TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (scheduled_expenses.last_applied): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.last_applied ---
    # Tracks when this income source was last auto-applied
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='last_applied'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN last_applied TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN last_applied TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.last_applied): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: transactions.auto_generated ---
    # Marks transactions that were created by the auto-apply system (vs manually entered)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='transactions' AND column_name='auto_generated'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE transactions ADD COLUMN auto_generated INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE transactions ADD COLUMN auto_generated INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (transactions.auto_generated): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.budget_cycle_start ---
    # Day of month the user's budget cycle starts (1-28, default 1)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='budget_cycle_start'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN budget_cycle_start INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN budget_cycle_start INTEGER NOT NULL DEFAULT 1")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (budget_cycle_start): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.avatar ---
    # User's chosen avatar emoji (optional, shown in profile button)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='avatar'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (avatar): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.display_name ---
    # User's chosen display name (optional, shown in profile panel)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='display_name'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (display_name): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.rule_type ---
    # Stores the payment rule type for monthly income (fixed_date, relative_month_end,
    # last_working_day, nth_weekday).  NULL on pre-migration rows → legacy behaviour.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='rule_type'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN rule_type TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN rule_type TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.rule_type): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.rule_config ---
    # JSON blob storing rule-specific parameters (day, offset, nth weekday pattern, etc.)
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='rule_config'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN rule_config TEXT DEFAULT '{}'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN rule_config TEXT DEFAULT '{}'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.rule_config): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.weekend_rule ---
    # How to handle paydays falling on a weekend: before / after / nearest
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='weekend_rule'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN weekend_rule TEXT DEFAULT 'before'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN weekend_rule TEXT DEFAULT 'before'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.weekend_rule): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.bank_holiday_rule ---
    # How to handle paydays falling on a UK bank holiday: before / after / nearest
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='bank_holiday_rule'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN bank_holiday_rule TEXT DEFAULT 'before'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN bank_holiday_rule TEXT DEFAULT 'before'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.bank_holiday_rule): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.first_payment_date ---
    # ISO date of the next / anchor payment.  Required anchor for fortnightly and 4-weekly.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='first_payment_date'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN first_payment_date TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN first_payment_date TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.first_payment_date): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- cycle_overrides table ---
    # Per-occurrence amount overrides for income and bills paid items.
    # Keyed by (user_id, type, source_id, date) — one override per occurrence.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cycle_overrides (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    UNIQUE(user_id, type, source_id, date)
                )
            """)
            db.commit()
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cycle_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    UNIQUE(user_id, type, source_id, date)
                )
            """)
            db.commit()
    except Exception as e:
        logger.error(f"cycle_overrides table creation error: {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.is_primary ---
    # Marks which income source defines the user's budget cycle in automatic mode.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='is_primary'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.is_primary): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.cycle_mode ---
    # Whether the budget cycle is driven by an income schedule ('automatic') or a fixed day ('manual').
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='cycle_mode'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN cycle_mode TEXT NOT NULL DEFAULT 'manual'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN cycle_mode TEXT NOT NULL DEFAULT 'manual'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.cycle_mode): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.savings_type ---
    # Whether a savings account is variable (accessible) or fixed (locked-in, e.g. fixed-rate ISA).
    # Fixed savings are excluded from transfer suggestions in the banner and caution pill.
    # Existing savings accounts default to 'variable' to preserve current behaviour.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='accounts' AND column_name='savings_type'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN savings_type TEXT")
                cursor.execute("UPDATE accounts SET savings_type = 'variable' WHERE type = 'savings'")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN savings_type TEXT")
            cursor.execute("UPDATE accounts SET savings_type = 'variable' WHERE type = 'savings'")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (accounts.savings_type): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.google_id ---
    # Stores the Google OAuth subject ID for users who sign in with Google.
    # NULL for password-only accounts; UNIQUE so each Google account maps to one row.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='google_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.google_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: make users.password nullable ---
    # Google-only users have no password; the column must allow NULL.
    # SQLite cannot ALTER COLUMN constraints — the test schema handles this directly.
    if USE_POSTGRES:
        try:
            cursor.execute("ALTER TABLE users ALTER COLUMN password DROP NOT NULL")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    # --- MIGRATION: users.show_welcome_modal ---
    # Set to 1 on new-user creation (both email/password and Google paths).
    # Cleared to 0 by home() on the user's first dashboard visit.
    # Stored in DB so the flag survives the cross-site OAuth redirect chain.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='show_welcome_modal'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN show_welcome_modal INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN show_welcome_modal INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.show_welcome_modal): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.setup_dismissed ---
    # Set to 1 when user dismisses the setup guidance card; never resets.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='setup_dismissed'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN setup_dismissed INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN setup_dismissed INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.setup_dismissed): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.is_seeded ---
    # Set to 1 for accounts created by _apply_seed_data; 0 for manually created accounts.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='accounts' AND column_name='is_seeded'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN is_seeded INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN is_seeded INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (accounts.is_seeded): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.user_verified ---
    # Set to 1 when the user has explicitly edited this account at least once.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='accounts' AND column_name='user_verified'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN user_verified INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN user_verified INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (accounts.user_verified): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.user_verified ---
    # Set to 1 when the user has explicitly edited this income source at least once.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='user_verified'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN user_verified INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN user_verified INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.user_verified): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- SCHEMA-DRIFT FIXES (added retroactively — these columns/tables/indexes
    # already existed on production, applied directly outside this migration
    # process at some point, but were never added here. Added now so init_db()
    # run against a fresh database actually reproduces production's real schema. ---

    # --- MIGRATION: users.verified ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='verified'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.verified): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.verify_token ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='verify_token'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN verify_token TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN verify_token TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.verify_token): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: users.onboarding_dismissed ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='onboarding_dismissed'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE users ADD COLUMN onboarding_dismissed BOOLEAN DEFAULT FALSE")
                db.commit()
        else:
            cursor.execute("ALTER TABLE users ADD COLUMN onboarding_dismissed BOOLEAN DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (users.onboarding_dismissed): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.user_id (+ index) ---
    # Every account belongs to a user — this is load-bearing for the whole
    # multi-user app, but was never captured as a formal migration.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='accounts' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (accounts.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: accounts.savings_rate ---
    # Optional interest/growth rate shown on savings accounts in the forecast.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='accounts' AND column_name='savings_rate'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE accounts ADD COLUMN savings_rate NUMERIC(5,2) DEFAULT 0")
                db.commit()
        else:
            cursor.execute("ALTER TABLE accounts ADD COLUMN savings_rate NUMERIC(5,2) DEFAULT 0")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (accounts.savings_rate): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: transactions.user_id (+ index) ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='transactions' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (transactions.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: transactions.truelayer_tx_id ---
    # Lets synced Open Banking transactions be matched against re-syncs to avoid duplicates.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='transactions' AND column_name='truelayer_tx_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE transactions ADD COLUMN truelayer_tx_id TEXT")
                db.commit()
        else:
            cursor.execute("ALTER TABLE transactions ADD COLUMN truelayer_tx_id TEXT")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (transactions.truelayer_tx_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: scheduled_expenses.user_id (+ index) ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='scheduled_expenses' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE scheduled_expenses ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_expenses_user_id ON scheduled_expenses(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (scheduled_expenses.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.user_id (+ index) ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_income_user_id ON income(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: income.weekly_day ---
    # 0=Monday..4=Friday, for weekly-frequency income rows (legacy path in income_engine.py).
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='income' AND column_name='weekly_day'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE income ADD COLUMN weekly_day INTEGER DEFAULT 4")
                db.commit()
        else:
            cursor.execute("ALTER TABLE income ADD COLUMN weekly_day INTEGER DEFAULT 4")
            db.commit()
    except Exception as e:
        logger.error(f"Column migration error (income.weekly_day): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: savings_rules.user_id (+ index) ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='savings_rules' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE savings_rules ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE savings_rules ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_savings_rules_user_id ON savings_rules(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (savings_rules.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: future_events.user_id (+ index) ---
    try:
        if USE_POSTGRES:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='future_events' AND column_name='user_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE future_events ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                db.commit()
        else:
            cursor.execute("ALTER TABLE future_events ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
            db.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_future_events_user_id ON future_events(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Column migration error (future_events.user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: investments/investment_updates user_id indexes ---
    # The columns were already present in the base CREATE TABLE; only the
    # indexes were missing.
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_investments_user_id ON investments(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_investment_updates_user_id ON investment_updates(user_id)")
        db.commit()
    except Exception as e:
        logger.error(f"Index migration error (investments/investment_updates user_id): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: balance_adjustments table ---
    # Records each manual balance edit (old/new/delta) for forecast history.
    # app.py also lazily creates this on first use (quick_adjust/api_balance_adjustments)
    # as a defensive fallback, but it belongs here so a fresh init_db() is complete.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    account TEXT NOT NULL,
                    old_balance NUMERIC(12,2) NOT NULL,
                    new_balance NUMERIC(12,2) NOT NULL,
                    delta NUMERIC(12,2) NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various',
                    recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account TEXT NOT NULL,
                    old_balance REAL NOT NULL,
                    new_balance REAL NOT NULL,
                    delta REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various',
                    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
        db.commit()
    except Exception as e:
        logger.error(f"Table creation error (balance_adjustments): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    # --- MIGRATION: bank_connections table ---
    # Stores TrueLayer Open Banking OAuth tokens per user (one active connection at a time).
    # app.py also lazily creates this on first use (_ensure_bank_connections_table) as a
    # defensive fallback, but it belongs here so a fresh init_db() is complete.
    try:
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bank_connections (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    token_expiry TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bank_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    token_expiry TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
        db.commit()
    except Exception as e:
        logger.error(f"Table creation error (bank_connections): {e}")
        try:
            db.rollback()
        except Exception as rb_error:
            logger.debug(f"Rollback error: {rb_error}")

    cursor.close()
    release_db(db)
