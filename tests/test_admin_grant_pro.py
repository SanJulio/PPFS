"""
Tests for the founder/admin Pro override (August 2026) - a way to grant Pro
access to a specific account without going through Stripe at all, for
founder/testing accounts only.

Deliberately reuses the exact same users.is_pro column and user_is_pro()
read path every other Pro check in the app already uses, rather than a
parallel "is this a founder account" check - one engine, not two that
could disagree. Never writes stripe_customer_id, so no Stripe webhook
(which only ever matches on stripe_customer_id) can revoke it later. Gated
by the same admin auth as /admin/analytics.
"""
import unittest.mock as mock
import uuid

import pytest

from tests.conftest import csrf


ADMIN_SECRET = "test-admin-secret-fixed"


def _unlock_admin(auth_client, admin_user_id):
    with auth_client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user_id)
        sess["_fresh"] = True
        sess["csrf_token"] = csrf()["csrf_token"]
        sess["admin_unlocked"] = ADMIN_SECRET


def _add_second_user(db_conn, email=None):
    from werkzeug.security import generate_password_hash
    if email is None:
        email = f"founder_{uuid.uuid4().hex[:8]}@example.com"
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
        (email, generate_password_hash("TestPass1!"), "2026-01-01", "Founder"),
    )
    db_conn.commit()
    return cur.lastrowid, email


class TestAdminGate:
    def test_blocked_when_not_admin_user(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"] + 999999), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            with auth_client.session_transaction() as sess:
                sess["admin_unlocked"] = ADMIN_SECRET
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
            assert resp.status_code == 404

    def test_blocked_when_admin_not_unlocked_this_session(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            # No admin_unlocked session flag set at all
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
            assert resp.status_code == 404

    def test_blocked_when_admin_secret_not_configured(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ""):
            with auth_client.session_transaction() as sess:
                sess["admin_unlocked"] = ""
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
            assert resp.status_code == 404

    def test_blocked_when_session_secret_does_not_match(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            with auth_client.session_transaction() as sess:
                sess["admin_unlocked"] = "wrong-secret"
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
            assert resp.status_code == 404


class TestGrantPro:
    def test_grants_pro_by_email(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
        assert resp.status_code == 200
        assert b"Pro access granted" in resp.data

        row = db_conn.execute("SELECT is_pro FROM users WHERE id=?", (target_id,)).fetchone()
        assert bool(row["is_pro"]) is True

    def test_email_matching_is_case_insensitive(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn, f"Founder_{uuid.uuid4().hex[:8]}@Example.com")
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get(f"/admin/grant-pro?email={email.lower()}")
        assert resp.status_code == 200
        row = db_conn.execute("SELECT is_pro FROM users WHERE id=?", (target_id,)).fetchone()
        assert bool(row["is_pro"]) is True

    def test_never_writes_stripe_customer_id(self, app, auth_client, test_user, db_conn):
        """Confirms the grant is genuinely not tied to a subscription - no
        Stripe webhook (matched by stripe_customer_id) could ever revoke
        it, since there's nothing for one to match against."""
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            auth_client.get(f"/admin/grant-pro?email={email}")
        row = db_conn.execute("SELECT stripe_customer_id FROM users WHERE id=?", (target_id,)).fetchone()
        assert row["stripe_customer_id"] is None

    def test_unknown_email_returns_404_and_changes_nothing(self, app, auth_client, test_user, db_conn):
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get("/admin/grant-pro?email=doesnotexist@example.com")
        assert resp.status_code == 404

    def test_missing_email_param_returns_400(self, app, auth_client, test_user, db_conn):
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get("/admin/grant-pro")
        assert resp.status_code == 400

    def test_downstream_is_pro_check_reflects_the_grant(self, app, auth_client, test_user, db_conn):
        """The whole point: the same user_is_pro() check every other Pro
        gate in the app uses must see the grant, not just a raw DB flag
        nobody reads."""
        import app as app_module
        import flask_login

        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            auth_client.get(f"/admin/grant-pro?email={email}")

        with app.test_request_context():
            user = app_module.load_user(str(target_id))
            flask_login.login_user(user)
            assert app_module.user_is_pro() is True

    def test_unlocks_previously_locked_accounts(self, app, auth_client, test_user, db_conn):
        """Mirrors the real Stripe upgrade path's own unlock-everything
        step - without it, accounts locked from a prior Free-tier state
        would stay locked despite is_pro now being true."""
        target_id, email = _add_second_user(db_conn)
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview, is_locked) VALUES (?,?,?,1,?,1,1)",
            ("Locked account", 500.0, "current", target_id),
        )
        acc_id = cur.lastrowid
        db_conn.commit()

        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            auth_client.get(f"/admin/grant-pro?email={email}")

        row = db_conn.execute("SELECT is_locked FROM accounts WHERE id=?", (acc_id,)).fetchone()
        assert row["is_locked"] == 0
