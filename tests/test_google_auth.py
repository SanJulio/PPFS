"""
Tests for Google OAuth sign-in routes:
  GET  /auth/google           → redirects to Google
  GET  /auth/google/callback  → creates/links/logs in user

Strategy: patch oauth.google.authorize_access_token to return fake userinfo,
and patch oauth.google.authorize_redirect to avoid real OIDC metadata fetches.
"""

import pytest
from unittest.mock import MagicMock, patch


FAKE_USERINFO_NEW = {
    "sub": "google-sub-new-001",
    "email": "newgoogle@example.com",
    "name": "Google New User",
}

FAKE_USERINFO_EXISTING = {
    "sub": "google-sub-existing-002",
    "email": None,  # filled per test
    "name": "Google Existing User",
}

FAKE_USERINFO_RETURNING = {
    "sub": "google-sub-returning-003",
    "email": "returning@example.com",
    "name": "Returning Google User",
}


def _mock_token(userinfo: dict):
    """Return a mock token dict as authorize_access_token() would."""
    return {"access_token": "fake-token", "userinfo": userinfo}


def _set_callback_session(client, income="", payday="", bills="", balance=""):
    """Pre-seed the google_seed session key as /auth/google would."""
    with client.session_transaction() as sess:
        sess["google_seed"] = {"income": income, "payday": payday, "bills": bills, "balance": balance}
        sess["csrf_token"] = "test-csrf-fixed-token"


class TestGoogleNewUser:
    """Brand-new Google user: no matching google_id or email in DB."""

    def test_new_google_user_created_and_logged_in(self, client, db_conn):
        email = FAKE_USERINFO_NEW["email"]
        sub = FAKE_USERINFO_NEW["sub"]

        # Ensure no leftover user from a previous run
        db_conn.execute("DELETE FROM users WHERE email = ?", (email,))

        _set_callback_session(client)

        with patch("app.oauth.google.authorize_access_token", return_value=_mock_token(FAKE_USERINFO_NEW)):
            resp = client.get("/auth/google/callback", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert "/login" not in resp.headers["Location"]

        row = db_conn.execute(
            "SELECT id, email, password, verified, google_id FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        assert row is not None
        assert row["password"] is None          # Google-only user has no password
        assert row["verified"] == 1             # auto-verified
        assert row["google_id"] == sub

        # Cleanup
        uid = row["id"]
        for tbl in ("transactions", "accounts", "scheduled_expenses", "income", "savings_rules", "future_events"):
            try:
                db_conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            except Exception:
                pass
        db_conn.execute("DELETE FROM users WHERE id = ?", (uid,))


class TestGoogleLinkExisting:
    """Existing password account with same email → google_id gets linked."""

    def test_existing_account_linked(self, client, test_user, db_conn):
        userinfo = {
            "sub": FAKE_USERINFO_EXISTING["sub"],
            "email": test_user["email"],
            "name": FAKE_USERINFO_EXISTING["name"],
        }

        _set_callback_session(client)

        with patch("app.oauth.google.authorize_access_token", return_value=_mock_token(userinfo)):
            resp = client.get("/auth/google/callback", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert "/login" not in resp.headers["Location"]

        row = db_conn.execute(
            "SELECT google_id FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert row["google_id"] == userinfo["sub"]


class TestGoogleReturningUser:
    """Returning Google user: has google_id already stored → logs straight in."""

    def test_returning_google_user_logs_in(self, client, db_conn):
        email = FAKE_USERINFO_RETURNING["email"]
        sub = FAKE_USERINFO_RETURNING["sub"]

        # Create a Google-only user row
        db_conn.execute(
            "INSERT INTO users (email, password, created_at, verified, google_id) "
            "VALUES (?, NULL, '2026-01-01', 1, ?)",
            (email, sub),
        )
        uid = db_conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]

        _set_callback_session(client)

        with patch("app.oauth.google.authorize_access_token", return_value=_mock_token(FAKE_USERINFO_RETURNING)):
            resp = client.get("/auth/google/callback", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert "/login" not in resp.headers["Location"]

        # Cleanup
        for tbl in ("transactions", "accounts", "scheduled_expenses", "income", "savings_rules", "future_events"):
            try:
                db_conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            except Exception:
                pass
        db_conn.execute("DELETE FROM users WHERE id = ?", (uid,))


class TestGoogleSeedAccount:
    """New Google user with seed data gets a 'Current Account' with the correct balance."""

    USERINFO = {
        "sub": "google-sub-seed-004",
        "email": "seedaccounttest@example.com",
        "name": "Seed Account User",
    }

    def test_seed_creates_account_with_balance(self, client, db_conn):
        email = self.USERINFO["email"]
        sub = self.USERINFO["sub"]

        db_conn.execute("DELETE FROM users WHERE email = ?", (email,))

        _set_callback_session(client, income="2500", payday="25th", bills="900", balance="1234.56")

        with patch("app.oauth.google.authorize_access_token", return_value=_mock_token(self.USERINFO)):
            resp = client.get("/auth/google/callback", follow_redirects=False)

        assert resp.status_code in (302, 303)

        uid = db_conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]

        accounts = db_conn.execute(
            "SELECT name, balance, type FROM accounts WHERE user_id = ?", (uid,)
        ).fetchall()

        assert len(accounts) == 1
        assert accounts[0]["name"] == "Current Account"
        assert accounts[0]["type"] == "current"
        assert abs(accounts[0]["balance"] - 1234.56) < 0.01

        # Cleanup
        for tbl in ("transactions", "accounts", "scheduled_expenses", "income", "savings_rules", "future_events"):
            try:
                db_conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            except Exception:
                pass
        db_conn.execute("DELETE FROM users WHERE id = ?", (uid,))

    def test_seed_creates_account_with_zero_balance_when_missing(self, client, db_conn):
        userinfo = {
            "sub": "google-sub-seed-005",
            "email": "seednobalance@example.com",
            "name": "Seed No Balance",
        }
        db_conn.execute("DELETE FROM users WHERE email = ?", (userinfo["email"],))

        _set_callback_session(client, income="2000", payday="1st", bills="500", balance="")

        with patch("app.oauth.google.authorize_access_token", return_value=_mock_token(userinfo)):
            resp = client.get("/auth/google/callback", follow_redirects=False)

        assert resp.status_code in (302, 303)

        uid = db_conn.execute("SELECT id FROM users WHERE email = ?", (userinfo["email"],)).fetchone()["id"]

        accounts = db_conn.execute(
            "SELECT balance FROM accounts WHERE user_id = ?", (uid,)
        ).fetchall()

        assert len(accounts) == 1
        assert accounts[0]["balance"] == 0.0

        # Cleanup
        for tbl in ("transactions", "accounts", "scheduled_expenses", "income", "savings_rules", "future_events"):
            try:
                db_conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            except Exception:
                pass
        db_conn.execute("DELETE FROM users WHERE id = ?", (uid,))
