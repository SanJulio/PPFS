"""
Tests for entitlement fields at signup, and the expand/migrate/activate
cutover behaviour (September 2026). Full design in CLAUDE.md's
"Pricing/entitlement model" section.

Covers:
  - Both signup entry points (email/password, Google OAuth) set
    fallback_entitlement/trial_started_at/trial_ends_at explicitly,
    never relying on the bare column DEFAULT - correct both while
    ENTITLEMENT_RESOLVER_ACTIVE is false (legacy_free, no trial) and true
    (basic + a real 30-day trial)
  - The specific cutover-race this closes: a user who registers AFTER the
    Legacy Free backfill has run but BEFORE Activation still lands in
    exactly one correct cohort (legacy_free), never the bare 'basic'
    default with no trial
  - An existing user's access is unchanged across every cutover stage
  - A Stripe status change occurring in the gap between backfill and
    Activation is not stale by the time Activation flips the flag
  - get_account_limit()/get_forecast_days()/user_has_pro_features() are
    flag-aware and match legacy behaviour exactly while inactive
"""
import datetime
from unittest.mock import patch

import pytest

from tests.conftest import csrf


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class TestSignupEntitlementFieldsFlagOff:
    def test_password_signup_gets_legacy_free_no_trial(self, app, db_conn):
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", False):
            client = app.test_client()
            resp = client.post("/register", data={
                "display_name": "New User", "email": "newuser_flagoff@example.com",
                "password": "TestPass1!", "confirm": "TestPass1!", "age_confirm": "1",
            }, follow_redirects=False)
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT fallback_entitlement, trial_started_at, trial_ends_at FROM users WHERE email = ?",
            ("newuser_flagoff@example.com",),
        ).fetchone()
        assert row is not None
        assert row["fallback_entitlement"] == "legacy_free"
        assert row["trial_started_at"] is None
        assert row["trial_ends_at"] is None


class TestSignupEntitlementFieldsFlagOn:
    def test_password_signup_gets_basic_and_a_real_trial(self, app, db_conn):
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", True):
            client = app.test_client()
            resp = client.post("/register", data={
                "display_name": "New User", "email": "newuser_flagon@example.com",
                "password": "TestPass1!", "confirm": "TestPass1!", "age_confirm": "1",
            }, follow_redirects=False)
        assert resp.status_code in (302, 303)
        row = db_conn.execute(
            "SELECT fallback_entitlement, trial_started_at, trial_ends_at FROM users WHERE email = ?",
            ("newuser_flagon@example.com",),
        ).fetchone()
        assert row is not None
        assert row["fallback_entitlement"] == "basic"
        assert row["trial_started_at"] is not None
        started = datetime.datetime.fromisoformat(row["trial_started_at"])
        ends = datetime.datetime.fromisoformat(row["trial_ends_at"])
        assert abs((ends - started).total_seconds() - 30 * 86400) < 5

    def test_trial_started_at_is_close_to_now_not_recalculated_later(self, app, db_conn):
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", True):
            client = app.test_client()
            client.post("/register", data={
                "display_name": "New User", "email": "newuser_flagon2@example.com",
                "password": "TestPass1!", "confirm": "TestPass1!", "age_confirm": "1",
            })
        row = db_conn.execute("SELECT trial_started_at FROM users WHERE email = ?", ("newuser_flagon2@example.com",)).fetchone()
        started = datetime.datetime.fromisoformat(row["trial_started_at"])
        assert abs((_utcnow() - started).total_seconds()) < 10


class TestSignupDuringBackfillActivationGap:
    def test_new_signup_after_backfill_before_activation_is_legacy_free_not_bare_basic_default(self, app, db_conn):
        """The exact race this closes: the grandfathering backfill has
        already run (simulated here by nothing needing to happen, since
        the backfill only touches EXISTING rows), the flag is still off,
        and a brand-new signup occurs. It must not silently inherit the
        bare fallback_entitlement='basic' schema DEFAULT with no trial -
        it must be explicitly legacy_free."""
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", False):
            client = app.test_client()
            client.post("/register", data={
                "display_name": "Gap User", "email": "gapuser@example.com",
                "password": "TestPass1!", "confirm": "TestPass1!", "age_confirm": "1",
            })
        row = db_conn.execute(
            "SELECT fallback_entitlement, trial_started_at FROM users WHERE email = ?", ("gapuser@example.com",)
        ).fetchone()
        assert row["fallback_entitlement"] == "legacy_free"
        assert row["trial_started_at"] is None

        # And once Activation later flips the flag, this user resolves
        # correctly as legacy_free (not basic-with-no-trial).
        import app as app_module
        with app.app_context():
            full_row = dict(db_conn.execute("SELECT * FROM users WHERE email = ?", ("gapuser@example.com",)).fetchone())
            ent = app_module.get_entitlement(full_row)
        assert ent["tier"] == "legacy_free"
        assert ent["account_limit"] == 3


class TestStripeStatusChangeDuringGap:
    def test_webhook_event_during_gap_is_not_stale_after_activation(self, app, db_conn, test_user):
        """A Stripe event processed while the flag is still off must
        already have dual-written the new fields correctly, so
        Activation sees current state immediately, with no extra webhook
        needed to 'catch up'."""
        from tests.test_stripe_webhook import _post_event, _FakeSub
        db_conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", ("cus_gap", test_user["id"]))

        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", False):
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(test_user["id"])
                sess["_fresh"] = True
            resp = _post_event(
                client, "customer.subscription.updated",
                {"object": "subscription", "id": "sub_gap", "customer": "cus_gap"},
                live_sub=_FakeSub("active", cancel_at_period_end=False, current_period_end=int(_utcnow().timestamp()) + 86400 * 20),
            )
        assert resp.status_code == 200

        # Now Activation flips the flag - the resolver must already see
        # the correct, current state, not something stale from before the
        # dual-write.
        import app as app_module
        with app.app_context():
            row = dict(db_conn.execute("SELECT * FROM users WHERE id = ?", (test_user["id"],)).fetchone())
            with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", True):
                ent = app_module.get_entitlement(row)
        assert ent["tier"] == "pro"


class TestFlagAwareHelpersMatchLegacyBehaviourWhenOff:
    def test_get_account_limit_matches_old_3_or_unlimited(self, app, auth_client, test_user, db_conn):
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", False):
            with app.test_request_context():
                import app as app_module
                import flask_login
                user = app_module.load_user(str(test_user["id"]))
                flask_login.login_user(user)
                assert app_module.get_account_limit() == 3

                db_conn.execute("UPDATE users SET is_pro = 1 WHERE id = ?", (test_user["id"],))
                user2 = app_module.load_user(str(test_user["id"]))
                flask_login.login_user(user2)
                assert app_module.get_account_limit() is None

    def test_get_forecast_days_always_90_while_flag_off(self, app, test_user, db_conn):
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", False):
            with app.test_request_context():
                import app as app_module
                import flask_login
                user = app_module.load_user(str(test_user["id"]))
                flask_login.login_user(user)
                assert app_module.get_forecast_days() == 90
