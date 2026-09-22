"""
Tests for the retirement of /admin/grant-pro and its replacement,
/admin/extend-trial (September 2026 entitlement cutover).

/admin/grant-pro used to grant permanent Pro by writing is_pro=1 with no
real Stripe subscription behind it. Retired rather than redesigned: under
the entitlement system, is_pro is meant to mirror genuine Stripe state
only (see CLAUDE.md - "is_pro cache semantics") - writing it for a
non-Stripe grant would fabricate exactly the kind of fact that column
exists to report honestly, and it would no longer even grant anything
once the resolver is active (nothing reads is_pro for authorization any
more). This file confirms the route is genuinely gone (404, even for a
correctly-authenticated admin), not just broken.

/admin/extend-trial replaces it for a different, narrower purpose: a
one-time, admin-approved +30-day trial extension for genuinely useful
feedback. Same admin-gate pattern as /admin/analytics and the old
/admin/grant-pro.
"""
import unittest.mock as mock
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import csrf


ADMIN_SECRET = "test-admin-secret-fixed"


def _unlock_admin(auth_client, admin_user_id):
    with auth_client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user_id)
        sess["_fresh"] = True
        sess["csrf_token"] = csrf()["csrf_token"]
        sess["admin_unlocked"] = ADMIN_SECRET


def _add_second_user(db_conn, email=None, trial_ends_at=None):
    from werkzeug.security import generate_password_hash
    if email is None:
        email = f"trialuser_{uuid.uuid4().hex[:8]}@example.com"
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password, created_at, verified, display_name, trial_started_at, trial_ends_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?)",
        (email, generate_password_hash("TestPass1!"), "2026-01-01", "Trial User",
         (trial_ends_at - timedelta(days=30)).isoformat() if trial_ends_at else None,
         trial_ends_at.isoformat() if trial_ends_at else None),
    )
    db_conn.commit()
    return cur.lastrowid, email


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TestGrantProRetired:
    def test_route_no_longer_exists_even_for_a_valid_admin(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get(f"/admin/grant-pro?email={email}")
        assert resp.status_code == 404

    def test_never_writes_fake_stripe_state(self, app, db_conn):
        """No code path should exist that sets stripe_subscription_status
        for a non-Stripe grant - confirmed structurally by the route being
        gone rather than probing behaviour that no longer exists."""
        import app as app_module
        assert not hasattr(app_module, "admin_grant_pro")


class TestAdminGateOnExtendTrial:
    def test_blocked_when_not_admin_user(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"] + 999999), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            with auth_client.session_transaction() as sess:
                sess["admin_unlocked"] = ADMIN_SECRET
            resp = auth_client.get(f"/admin/extend-trial?email={email}")
            assert resp.status_code == 404

    def test_blocked_when_admin_not_unlocked_this_session(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            resp = auth_client.get(f"/admin/extend-trial?email={email}")
            assert resp.status_code == 404

    def test_blocked_when_session_secret_does_not_match(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            with auth_client.session_transaction() as sess:
                sess["admin_unlocked"] = "wrong-secret"
            resp = auth_client.get(f"/admin/extend-trial?email={email}")
            assert resp.status_code == 404


class TestExtendTrial:
    def test_extends_mid_trial_from_original_end_date(self, app, auth_client, test_user, db_conn):
        """Approved before expiry - the extra month is added on top of the
        original end date, not from 'now'."""
        original_end = _utcnow() + timedelta(days=10)  # still mid-trial
        target_id, email = _add_second_user(db_conn, trial_ends_at=original_end)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get(f"/admin/extend-trial?email={email}")
        assert resp.status_code == 200

        row = db_conn.execute("SELECT trial_ends_at FROM users WHERE id=?", (target_id,)).fetchone()
        new_end = datetime.fromisoformat(row["trial_ends_at"])
        expected = original_end + timedelta(days=30)
        assert abs((new_end - expected).total_seconds()) < 5

    def test_extends_from_now_when_approved_after_expiry(self, app, auth_client, test_user, db_conn):
        """Approved well after the trial already lapsed - anchoring to the
        stale past trial_ends_at would land in the past and grant nothing
        real; must anchor to 'now' instead."""
        original_end = _utcnow() - timedelta(days=45)  # long expired
        target_id, email = _add_second_user(db_conn, trial_ends_at=original_end)
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get(f"/admin/extend-trial?email={email}")
        assert resp.status_code == 200

        row = db_conn.execute("SELECT trial_ends_at FROM users WHERE id=?", (target_id,)).fetchone()
        new_end = datetime.fromisoformat(row["trial_ends_at"])
        assert new_end > _utcnow() + timedelta(days=29)  # a genuine ~month from now, not from the stale past date

    def test_records_audit_fields(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn, trial_ends_at=_utcnow() + timedelta(days=5))
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            auth_client.get(f"/admin/extend-trial?email={email}")

        row = db_conn.execute(
            "SELECT feedback_extension_granted_at, feedback_extension_granted_by FROM users WHERE id=?", (target_id,)
        ).fetchone()
        assert row["feedback_extension_granted_at"] is not None
        assert row["feedback_extension_granted_by"] == test_user["id"]

    def test_second_grant_is_rejected(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn, trial_ends_at=_utcnow() + timedelta(days=5))
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            r1 = auth_client.get(f"/admin/extend-trial?email={email}")
            assert r1.status_code == 200
            trial_ends_after_first = db_conn.execute(
                "SELECT trial_ends_at FROM users WHERE id=?", (target_id,)
            ).fetchone()["trial_ends_at"]

            r2 = auth_client.get(f"/admin/extend-trial?email={email}")
            assert r2.status_code == 400
            assert b"already received" in r2.data

        trial_ends_after_second = db_conn.execute(
            "SELECT trial_ends_at FROM users WHERE id=?", (target_id,)
        ).fetchone()["trial_ends_at"]
        assert trial_ends_after_second == trial_ends_after_first  # unchanged by the rejected second attempt

    def test_unlocks_accounts_immediately_in_same_request(self, app, auth_client, test_user, db_conn):
        """Full trial access restored synchronously, not left to the next
        request's lazy reconciliation."""
        target_id, email = _add_second_user(db_conn, trial_ends_at=_utcnow() + timedelta(days=5))
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
            auth_client.get(f"/admin/extend-trial?email={email}")

        row = db_conn.execute("SELECT is_locked FROM accounts WHERE id=?", (acc_id,)).fetchone()
        assert row["is_locked"] == 0

    def test_does_not_touch_stripe_fields(self, app, auth_client, test_user, db_conn):
        target_id, email = _add_second_user(db_conn, trial_ends_at=_utcnow() + timedelta(days=5))
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            auth_client.get(f"/admin/extend-trial?email={email}")

        row = db_conn.execute(
            "SELECT stripe_customer_id, stripe_subscription_status, is_pro FROM users WHERE id=?", (target_id,)
        ).fetchone()
        assert row["stripe_customer_id"] is None
        assert row["stripe_subscription_status"] is None
        assert bool(row["is_pro"]) is False

    def test_unknown_email_returns_404_and_changes_nothing(self, app, auth_client, test_user, db_conn):
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get("/admin/extend-trial?email=doesnotexist@example.com")
        assert resp.status_code == 404

    def test_missing_email_param_returns_400(self, app, auth_client, test_user, db_conn):
        with mock.patch("app.ADMIN_USER_ID", test_user["id"]), \
             mock.patch("app.ADMIN_SECRET", ADMIN_SECRET):
            _unlock_admin(auth_client, test_user["id"])
            resp = auth_client.get("/admin/extend-trial")
        assert resp.status_code == 400
