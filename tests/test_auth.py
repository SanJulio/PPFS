"""
Tests for authentication routes:
  GET/POST /register
  GET/POST /login
  GET /logout
  GET/POST /forgot-password
  GET/POST /reset-password/<token>
"""

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash


# ── REGISTER ──────────────────────────────────────────────────────────────────
class TestRegister:
    def test_get_register_page(self, client):
        resp = client.get("/register")
        assert resp.status_code == 200

    @patch("app.send_verification_email", return_value=True)
    def test_register_valid_user(self, mock_email, client, db_conn):
        resp = client.post(
            "/register",
            data={
                "display_name": "Alice",
                "email": "alice_unique@example.com",
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                "age_confirm": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert mock_email.called

        # User should exist in DB
        row = db_conn.execute(
            "SELECT id FROM users WHERE email = ?", ("alice_unique@example.com",)
        ).fetchone()
        assert row is not None
        # Cleanup
        db_conn.execute("DELETE FROM users WHERE email = ?", ("alice_unique@example.com",))
        db_conn.commit()

    @patch("app.send_verification_email", return_value=True)
    def test_register_duplicate_email(self, mock_email, client, test_user):
        resp = client.post(
            "/register",
            data={
                "display_name": "Alice",
                "email": test_user["email"],
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data.lower() or b"already" in resp.data.lower()

    def test_register_password_mismatch(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Bob",
                "email": "bob@example.com",
                "password": "ValidPass1!",
                "confirm": "DifferentPass1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"do not match" in resp.data.lower()

    def test_register_short_password(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Bob",
                "email": "bob@example.com",
                "password": "Ab1!",
                "confirm": "Ab1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"8 characters" in resp.data.lower()

    def test_register_no_uppercase(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Bob",
                "email": "bob@example.com",
                "password": "alllower1!",
                "confirm": "alllower1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"uppercase" in resp.data.lower()

    def test_register_no_digit(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Bob",
                "email": "bob@example.com",
                "password": "NoDigitPass!",
                "confirm": "NoDigitPass!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"number" in resp.data.lower()

    def test_register_no_symbol(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Bob",
                "email": "bob@example.com",
                "password": "NoSymbol1A",
                "confirm": "NoSymbol1A",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"symbol" in resp.data.lower()

    def test_register_missing_name(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "",
                "email": "bob@example.com",
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"name" in resp.data.lower()

    def test_register_disposable_email(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Spammer",
                "email": "user@mailinator.com",
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                "age_confirm": "on",
            },
        )
        assert resp.status_code == 200
        assert b"disposable" in resp.data.lower() or b"real email" in resp.data.lower()

    def test_register_age_not_confirmed(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "Young",
                "email": "young@example.com",
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                # no age_confirm field
            },
        )
        assert resp.status_code == 200
        assert b"16" in resp.data


# ── LOGIN ─────────────────────────────────────────────────────────────────────
class TestLogin:
    def test_get_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_valid_credentials(self, client, test_user):
        resp = client.post(
            "/login",
            data={"email": test_user["email"], "password": test_user["password"]},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert resp.location.endswith("/") or "home" in resp.location or resp.location == "/"

    def test_login_wrong_password(self, client, test_user):
        resp = client.post(
            "/login",
            data={"email": test_user["email"], "password": "WrongPass99!"},
        )
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower()

    def test_login_nonexistent_email(self, client):
        resp = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "SomePass1!"},
        )
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower()

    def test_login_missing_email(self, client):
        resp = client.post(
            "/login",
            data={"email": "", "password": "SomePass1!"},
        )
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()

    def test_login_missing_password(self, client):
        resp = client.post(
            "/login",
            data={"email": "someone@example.com", "password": ""},
        )
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()

    def test_login_case_insensitive_email(self, client, test_user):
        resp = client.post(
            "/login",
            data={
                "email": test_user["email"].upper(),
                "password": test_user["password"],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)


# ── LOGOUT ────────────────────────────────────────────────────────────────────
class TestLogout:
    def test_logout_redirects(self, auth_client):
        resp = auth_client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_logout_unauthenticated_still_redirects(self, client):
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)


# ── FORGOT PASSWORD ───────────────────────────────────────────────────────────
class TestForgotPassword:
    def test_get_forgot_password_page(self, client):
        resp = client.get("/forgot-password")
        assert resp.status_code == 200

    @patch("app.send_reset_email", return_value=True)
    def test_forgot_password_existing_email(self, mock_email, csrf_client, test_user):
        from tests.conftest import CSRF_TOKEN
        resp = csrf_client.post(
            "/forgot-password",
            data={"email": test_user["email"], "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        # Always shows the same message regardless of whether email exists
        assert b"reset link" in resp.data.lower() or b"if that email" in resp.data.lower()

    @patch("app.send_reset_email", return_value=True)
    def test_forgot_password_nonexistent_email_same_message(self, mock_email, csrf_client):
        from tests.conftest import CSRF_TOKEN
        # Anti-enumeration: response should be identical whether email exists or not
        resp = csrf_client.post(
            "/forgot-password",
            data={"email": "nobody_here@example.com", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"reset link" in resp.data.lower() or b"if that email" in resp.data.lower()
        assert not mock_email.called

    def test_forgot_password_empty_email(self, csrf_client):
        from tests.conftest import CSRF_TOKEN
        resp = csrf_client.post(
            "/forgot-password",
            data={"email": "", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"enter your email" in resp.data.lower() or b"please" in resp.data.lower()


# ── RESET PASSWORD ────────────────────────────────────────────────────────────
class TestResetPassword:
    def _insert_reset_token(self, db_conn, user_id, token, hours_from_now=24):
        expires = (datetime.now() + timedelta(hours=hours_from_now)).isoformat()
        db_conn.execute(
            "UPDATE users SET verify_token = ?, verify_token_expires_at = ? WHERE id = ?",
            (token, expires, user_id),
        )
        db_conn.commit()

    def test_get_reset_page(self, client):
        resp = client.get("/reset-password/sometoken123")
        assert resp.status_code == 200

    def test_reset_password_valid_token(self, csrf_client, db_conn, test_user):
        from tests.conftest import CSRF_TOKEN
        token = "valid-reset-token-abc"
        self._insert_reset_token(db_conn, test_user["id"], token)

        resp = csrf_client.post(
            f"/reset-password/{token}",
            data={
                "password": "NewValidPass1!",
                "confirm": "NewValidPass1!",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303), f"Got {resp.status_code}, body: {resp.data[:300]}"

        # Re-query to check token was cleared (Flask route committed via its own connection)
        row = db_conn.execute(
            "SELECT verify_token FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert row["verify_token"] is None

    def test_reset_password_expired_token(self, csrf_client, db_conn, test_user):
        from tests.conftest import CSRF_TOKEN
        token = "expired-token-xyz"
        self._insert_reset_token(db_conn, test_user["id"], token, hours_from_now=-1)

        resp = csrf_client.post(
            f"/reset-password/{token}",
            data={"password": "NewValidPass1!", "confirm": "NewValidPass1!", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_reset_password_invalid_token(self, csrf_client):
        from tests.conftest import CSRF_TOKEN
        resp = csrf_client.post(
            "/reset-password/completely-bogus-token",
            data={"password": "NewValidPass1!", "confirm": "NewValidPass1!", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_reset_password_mismatch(self, csrf_client, db_conn, test_user):
        from tests.conftest import CSRF_TOKEN
        token = "mismatch-token-123"
        self._insert_reset_token(db_conn, test_user["id"], token)

        resp = csrf_client.post(
            f"/reset-password/{token}",
            data={"password": "NewValidPass1!", "confirm": "DifferentPass1!", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"do not match" in resp.data.lower()

    def test_reset_password_too_short(self, csrf_client, db_conn, test_user):
        from tests.conftest import CSRF_TOKEN
        token = "short-pw-token-456"
        self._insert_reset_token(db_conn, test_user["id"], token)

        resp = csrf_client.post(
            f"/reset-password/{token}",
            data={"password": "Ab1!", "confirm": "Ab1!", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"8 characters" in resp.data.lower()

    def test_reset_password_missing_fields(self, csrf_client, db_conn, test_user):
        from tests.conftest import CSRF_TOKEN
        token = "missing-fields-token"
        self._insert_reset_token(db_conn, test_user["id"], token)

        resp = csrf_client.post(
            f"/reset-password/{token}",
            data={"password": "", "confirm": "", "csrf_token": CSRF_TOKEN},
        )
        assert resp.status_code == 200
        assert b"required" in resp.data.lower()


# ── SEED DATA — account creation from landing page demo ──────────────────────
class TestSeedDataAccount:
    """Registration with seed params creates a 'Current Account' with the seeded balance."""

    @patch("app.send_verification_email", return_value=True)
    def test_register_with_seed_creates_account(self, mock_email, client, db_conn):
        email = "seedtest_reg@example.com"
        db_conn.execute("DELETE FROM users WHERE email = ?", (email,))

        resp = client.post(
            "/register",
            data={
                "display_name": "Seed Tester",
                "email": email,
                "password": "ValidPass1!",
                "confirm": "ValidPass1!",
                "age_confirm": "on",
                "seed_income": "2500",
                "seed_payday": "25th",
                "seed_bills": "900",
                "seed_balance": "1500.00",
            },
            follow_redirects=False,
        )

        assert resp.status_code in (302, 303)

        uid = db_conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]

        accounts = db_conn.execute(
            "SELECT name, balance, type FROM accounts WHERE user_id = ?", (uid,)
        ).fetchall()

        assert len(accounts) == 1
        assert accounts[0]["name"] == "Current Account"
        assert accounts[0]["type"] == "current"
        assert abs(accounts[0]["balance"] - 1500.00) < 0.01

        # Cleanup
        for tbl in ("transactions", "accounts", "scheduled_expenses", "income", "savings_rules", "future_events"):
            try:
                db_conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            except Exception:
                pass
        db_conn.execute("DELETE FROM users WHERE email = ?", (email,))
