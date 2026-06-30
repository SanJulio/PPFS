"""
Pytest configuration and shared fixtures for the Spendara test suite.

Key technical decisions:
- SECRET_KEY must be set before importing app.py (raises ValueError otherwise)
- DATABASE_URL removed → SQLite used instead of Postgres
- database.DB_PATH patched to temp file before import
- session_interface replaced with SecureCookieSessionInterface (Postgres backend
  returns early without DATABASE_URL so sessions wouldn't persist in tests)
- Custom SQLite schema (not init_db): the production CREATE TABLE uses SERIAL PRIMARY KEY
  which in SQLite gives id=NULL — INTEGER PRIMARY KEY AUTOINCREMENT is required for
  auto-generated integer IDs and WHERE id=? queries to work
- Rate limiting disabled (limiter.enabled = False) so route tests don't hit 429
- WAL journal mode + isolation_level=None (autocommit) to avoid write-lock conflicts
  between the test's direct DB connection and Flask's route connections
"""

import os
import sqlite3
from pathlib import Path

# Must be set before any app import
os.environ["SECRET_KEY"] = "test-secret-key-spendara-pytest"
os.environ.pop("DATABASE_URL", None)

import pytest
from werkzeug.security import generate_password_hash
from flask.sessions import SecureCookieSessionInterface


# ── CONSTANTS ─────────────────────────────────────────────────────────────────
CSRF_TOKEN = "test-csrf-fixed-token"


# ── TEST SCHEMA ───────────────────────────────────────────────────────────────
# Production schema uses SERIAL PRIMARY KEY (Postgres auto-increment).
# SQLite does not recognise SERIAL so id stays NULL — INTEGER PRIMARY KEY
# AUTOINCREMENT is required for rows to get integer IDs that WHERE id=? can match.
def _create_test_schema(db_path: Path):
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")

    tables = [
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT,
            created_at TEXT NOT NULL,
            display_name TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            verify_token TEXT,
            verify_token_expires_at TEXT,
            is_pro INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT,
            auto_apply_enabled INTEGER NOT NULL DEFAULT 1,
            auto_apply_confirm INTEGER NOT NULL DEFAULT 1,
            budget_cycle_start INTEGER NOT NULL DEFAULT 1,
            avatar TEXT,
            cycle_mode TEXT NOT NULL DEFAULT 'manual',
            google_id TEXT UNIQUE
        )""",
        """CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            type TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            include_in_overview INTEGER NOT NULL DEFAULT 1,
            user_id INTEGER NOT NULL,
            savings_type TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            account TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'manual',
            category TEXT NOT NULL DEFAULT 'Other',
            auto_generated INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS scheduled_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            day INTEGER,
            account TEXT NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'monthly',
            month INTEGER,
            last_applied TEXT,
            user_id INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            frequency TEXT NOT NULL,
            account TEXT NOT NULL,
            day INTEGER NOT NULL DEFAULT 1,
            weekly_day INTEGER DEFAULT 4,
            last_applied TEXT,
            rule_type TEXT,
            rule_config TEXT DEFAULT '{}',
            weekend_rule TEXT DEFAULT 'before',
            bank_holiday_rule TEXT DEFAULT 'before',
            first_payment_date TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS savings_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            day INTEGER NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'monthly',
            from_account TEXT NOT NULL,
            to_account TEXT NOT NULL,
            user_id INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS future_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            account TEXT NOT NULL,
            user_id INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS flask_sessions (
            sid TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            initial_amount REAL NOT NULL,
            date TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS investment_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            value REAL NOT NULL,
            date TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS cycle_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            UNIQUE(user_id, type, source_id, date)
        )""",
    ]

    for sql in tables:
        conn.execute(sql)

    conn.close()


# ── APP FIXTURE ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """Session-scoped Flask app wired to a temp SQLite database with correct schema."""
    db_path = tmp_path_factory.mktemp("db") / "test.db"

    import database
    database.DB_PATH = db_path
    database.USE_POSTGRES = False

    # Create schema BEFORE importing app (app.py calls init_db() at import time)
    _create_test_schema(db_path)

    from app import app as flask_app

    flask_app.session_interface = SecureCookieSessionInterface()
    flask_app.config["TESTING"] = True
    flask_app.config["SERVER_NAME"] = "localhost"

    # Disable rate limiting — in-memory storage persists across tests and hits limits
    from app import limiter
    limiter.enabled = False

    flask_app._test_db_path = db_path
    yield flask_app


# ── DATABASE CONNECTION ───────────────────────────────────────────────────────
@pytest.fixture
def db_conn(app):
    """
    Function-scoped SQLite connection in autocommit mode (isolation_level=None).
    Each statement commits immediately so this connection never holds write locks
    that would block Flask route connections.
    """
    conn = sqlite3.connect(str(app._test_db_path), timeout=15, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── HTTP CLIENTS ──────────────────────────────────────────────────────────────
@pytest.fixture
def client(app):
    """Fresh unauthenticated test client (no session state)."""
    return app.test_client()


@pytest.fixture
def csrf_client(app):
    """
    Unauthenticated test client with CSRF token pre-loaded in session.
    Use for routes that require CSRF but not authentication (forgot-password etc.).
    """
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["csrf_token"] = CSRF_TOKEN
    return c


# ── TEST USER ─────────────────────────────────────────────────────────────────
@pytest.fixture
def test_user(db_conn):
    """Insert a verified test user; remove all their data after the test."""
    import uuid
    email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    hashed = generate_password_hash("TestPass1!")
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password, created_at, verified, display_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (email, hashed, "2026-01-01", 1, "Test User"),
    )
    user_id = cur.lastrowid
    cur.close()

    yield {"id": user_id, "email": email, "password": "TestPass1!"}

    import gc
    gc.collect()

    cur = db_conn.cursor()
    for table in ("transactions", "accounts", "scheduled_expenses", "income",
                  "savings_rules", "future_events"):
        try:
            cur.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        except Exception:
            pass
    try:
        cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    except Exception:
        pass
    cur.close()


# ── TEST ACCOUNTS ─────────────────────────────────────────────────────────────
@pytest.fixture
def test_account(db_conn, test_user):
    """Insert a 'Current' account (£1000 balance) for the test user."""
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Current", 1000.00, "current", 1, test_user["id"], 1),
    )
    acct_id = cur.lastrowid
    cur.close()

    yield {"id": acct_id, "name": "Current", "balance": 1000.00, "user_id": test_user["id"]}

    cur = db_conn.cursor()
    try:
        cur.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
    except Exception:
        pass
    cur.close()


@pytest.fixture
def second_account(db_conn, test_user):
    """Insert a 'Savings' account (£500 balance) for transfer tests."""
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Savings", 500.00, "savings", 1, test_user["id"], 1),
    )
    acct_id = cur.lastrowid
    cur.close()

    yield {"id": acct_id, "name": "Savings", "balance": 500.00, "user_id": test_user["id"]}

    cur = db_conn.cursor()
    try:
        cur.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
    except Exception:
        pass
    cur.close()


# ── AUTHENTICATED CLIENT ──────────────────────────────────────────────────────
@pytest.fixture
def auth_client(app, test_user):
    """
    Test client with an active Flask-Login session for test_user.
    _user_id is set to the auto-generated integer id so load_user() finds the row.
    csrf_token is set so all POST requests can include the valid token.
    """
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(test_user["id"])
        sess["_fresh"] = True
        sess["csrf_token"] = CSRF_TOKEN
    return c


# ── CSRF HELPER ───────────────────────────────────────────────────────────────
def csrf():
    """Return {'csrf_token': CSRF_TOKEN} to include in any authenticated POST."""
    return {"csrf_token": CSRF_TOKEN}
