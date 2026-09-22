"""
Tests for the entitlement resolver and reconciliation (September 2026).

get_entitlement() is a pure function - the single source of truth for a
user's effective tier - and reconcile_and_get_entitlement() is its
side-effecting counterpart, called once per request from load_user(),
that keeps accounts.is_locked in sync with whatever the resolver just
computed, with no cron and no webhook dependency. Full design in
CLAUDE.md's "Pricing/entitlement model" section.

Covers:
  - All four tiers (pro, trialing, legacy_free, basic) and their
    account_limit/forecast_days/pro_features outputs
  - The renewal-never-downgrades guarantee: cancel_at_period_end is the
    only thing that can end Pro access via the date check - an ordinary
    renewal (cancel_at_period_end=False) can never trigger it, no matter
    how stale stripe_current_period_end is
  - Trial boundary (just before/at/after trial_ends_at)
  - reconcile_and_get_entitlement() locking an expired trial's excess
    accounts in a single call, generalizing to a paid subscription's
    scheduled cancellation reaching current_period_end before its
    terminal webhook arrives, and staying idempotent (no redundant
    re-lock once reconciled_account_limit already matches)
  - A second reconciliation cycle after a feedback extension re-arms it
"""
import datetime

import pytest


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _iso(dt):
    return dt.isoformat() if dt else None


def _base_row(**overrides):
    row = {
        "fallback_entitlement": "basic",
        "trial_started_at": None,
        "trial_ends_at": None,
        "reconciled_account_limit": None,
        "stripe_subscription_status": None,
        "stripe_cancel_at_period_end": 0,
        "stripe_current_period_end": None,
    }
    row.update(overrides)
    return row


class TestResolverTiers:
    def test_pro_active_subscription(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(stripe_subscription_status="active"))
        assert ent["tier"] == "pro"
        assert ent["pro_features"] is True
        assert ent["account_limit"] is None
        assert ent["forecast_days"] == 90

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due", "paused"])
    def test_all_access_granting_statuses_resolve_pro(self, app, status):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(stripe_subscription_status=status))
        assert ent["tier"] == "pro"

    @pytest.mark.parametrize("status", ["canceled", "unpaid", "incomplete_expired", "incomplete", None])
    def test_non_access_granting_statuses_fall_through(self, app, status):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(stripe_subscription_status=status, fallback_entitlement="basic"))
        assert ent["tier"] == "basic"

    def test_trialing_full_pro_access(self, app):
        """The core requirement: the 30-day trial provides COMPLETE Pro
        product access, not a reduced capability tier."""
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(trial_ends_at=_iso(_utcnow() + datetime.timedelta(days=10))))
        assert ent["tier"] == "trialing"
        assert ent["pro_features"] is True
        assert ent["account_limit"] is None
        assert ent["forecast_days"] == 90

    def test_legacy_free(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(fallback_entitlement="legacy_free"))
        assert ent["tier"] == "legacy_free"
        assert ent["pro_features"] is False
        assert ent["account_limit"] == 3
        assert ent["forecast_days"] == 90

    def test_basic(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(fallback_entitlement="basic"))
        assert ent["tier"] == "basic"
        assert ent["pro_features"] is False
        assert ent["account_limit"] == 1
        assert ent["forecast_days"] == 30

    def test_legacy_free_never_gets_pro_features_even_though_forecast_matches_pro(self, app):
        """Legacy Free matches Pro's 90-day forecast but must never
        inherit Pro-only feature access - pro_features is the capability
        flag every feature gate reads, decoupled from forecast_days."""
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(fallback_entitlement="legacy_free"))
        assert ent["forecast_days"] == 90
        assert ent["pro_features"] is False


class TestRenewalNeverDowngrades:
    def test_active_with_cancel_at_period_end_false_ignores_stale_period_end(self, app):
        """cancel_at_period_end=False means an ordinary renewing
        subscriber - the date branch must never even be consulted, no
        matter how far current_period_end has drifted into the past."""
        import app as app_module
        ancient = _iso(_utcnow() - datetime.timedelta(days=400))
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(
                stripe_subscription_status="active",
                stripe_cancel_at_period_end=0,
                stripe_current_period_end=ancient,
            ))
        assert ent["tier"] == "pro"

    def test_scheduled_cancellation_before_period_end_still_pro(self, app):
        import app as app_module
        future_end = _iso(_utcnow() + datetime.timedelta(days=5))
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(
                stripe_subscription_status="active",
                stripe_cancel_at_period_end=1,
                stripe_current_period_end=future_end,
            ))
        assert ent["tier"] == "pro"

    def test_scheduled_cancellation_after_period_end_falls_back(self, app):
        import app as app_module
        past_end = _iso(_utcnow() - datetime.timedelta(days=1))
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(
                stripe_subscription_status="active",
                stripe_cancel_at_period_end=1,
                stripe_current_period_end=past_end,
                fallback_entitlement="legacy_free",
            ))
        assert ent["tier"] == "legacy_free"


class TestTrialBoundary:
    def test_one_second_before_expiry_still_trialing(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(trial_ends_at=_iso(_utcnow() + datetime.timedelta(seconds=1))))
        assert ent["tier"] == "trialing"

    def test_one_second_after_expiry_falls_back(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(trial_ends_at=_iso(_utcnow() - datetime.timedelta(seconds=1))))
        assert ent["tier"] == "basic"

    def test_no_trial_dates_at_all_falls_back(self, app):
        import app as app_module
        with app.app_context():
            ent = app_module.get_entitlement(_base_row(fallback_entitlement="legacy_free"))
        assert ent["tier"] == "legacy_free"


class TestReconciliation:
    def _seed_user_with_accounts(self, db_conn, test_user, n=5, **user_overrides):
        for i in range(n):
            db_conn.execute(
                "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
                (f"Acc {i}", 100.0, "current", test_user["id"]),
            )
        set_clauses = ", ".join(f"{k} = ?" for k in user_overrides)
        if set_clauses:
            db_conn.execute(f"UPDATE users SET {set_clauses} WHERE id = ?", (*user_overrides.values(), test_user["id"]))

    def _locked_count(self, db_conn, test_user):
        return db_conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE user_id = ? AND is_locked = 1", (test_user["id"],)
        ).fetchone()["c"]

    def test_expired_trial_locks_excess_accounts_on_first_call(self, app, db_conn, test_user):
        import app as app_module
        expired = _iso(_utcnow() - datetime.timedelta(days=1))
        self._seed_user_with_accounts(db_conn, test_user, n=5, trial_ends_at=expired, fallback_entitlement="basic")
        row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
        with app.app_context():
            ent = app_module.reconcile_and_get_entitlement(row, test_user["id"])
        assert ent["tier"] == "basic"
        assert self._locked_count(db_conn, test_user) == 4  # 5 accounts, limit 1 -> 4 locked

    def test_reconciliation_is_idempotent_no_redundant_relock(self, app, db_conn, test_user):
        import app as app_module
        expired = _iso(_utcnow() - datetime.timedelta(days=1))
        self._seed_user_with_accounts(db_conn, test_user, n=5, trial_ends_at=expired, fallback_entitlement="basic")
        row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
        with app.app_context():
            app_module.reconcile_and_get_entitlement(row, test_user["id"])
            row2 = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
            assert row2["reconciled_account_limit"] == 1

            calls = []
            orig = app_module.sync_account_locks
            def _spy(*a, **kw):
                calls.append((a, kw))
                return orig(*a, **kw)
            app_module.sync_account_locks = _spy
            try:
                app_module.reconcile_and_get_entitlement(row2, test_user["id"])
            finally:
                app_module.sync_account_locks = orig
            assert calls == []  # no-op: reconciled_account_limit already matches

    def test_paid_cancellation_reaching_period_end_before_webhook_locks_excess(self, app, db_conn, test_user):
        """The gap explicitly flagged: a scheduled cancellation whose
        current_period_end has already passed, but whose terminal webhook
        hasn't arrived yet. accounts.is_locked must still catch up on the
        very next request."""
        import app as app_module
        self._seed_user_with_accounts(
            db_conn, test_user, n=5,
            stripe_subscription_status="active",
            stripe_cancel_at_period_end=1,
            stripe_current_period_end=_iso(_utcnow() - datetime.timedelta(hours=2)),
            fallback_entitlement="legacy_free",
            reconciled_account_limit=None,  # as if they were previously Pro (unlimited), never reconciled to a limit
        )
        row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
        with app.app_context():
            ent = app_module.reconcile_and_get_entitlement(row, test_user["id"])
        assert ent["tier"] == "legacy_free"
        assert self._locked_count(db_conn, test_user) == 2  # 5 accounts, limit 3 -> 2 locked

    def test_second_expiry_after_extension_reconciles_again(self, app, db_conn, test_user):
        """Proves the mechanism is genuinely reusable, not one-shot: after
        a feedback extension pushes trial_ends_at forward and the account
        limit is restored to unlimited, a SECOND later expiry must lock
        correctly again."""
        import app as app_module
        self._seed_user_with_accounts(
            db_conn, test_user, n=5,
            trial_ends_at=_iso(_utcnow() + datetime.timedelta(days=30)),
            fallback_entitlement="basic",
            reconciled_account_limit=None,
        )
        row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
        with app.app_context():
            # First reconciliation while still trialing - unlocks everything (limit None)
            app_module.reconcile_and_get_entitlement(row, test_user["id"])
            assert self._locked_count(db_conn, test_user) == 0

            # Now the (extended) trial itself expires
            db_conn.execute("UPDATE users SET trial_ends_at = ? WHERE id = ?", (_iso(_utcnow() - datetime.timedelta(seconds=1)), test_user["id"]))
            row2 = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
            ent = app_module.reconcile_and_get_entitlement(row2, test_user["id"])
        assert ent["tier"] == "basic"
        assert self._locked_count(db_conn, test_user) == 4
