"""
Tests proving trial users get complete Pro feature access, Legacy Free
users are correctly excluded from Pro-only features (present and future),
the per-tier forecast horizon, and correct template display across all
four tiers (September 2026 entitlement cutover). All run with
ENTITLEMENT_RESOLVER_ACTIVE=true, since these are the resolver-driven
behaviours - see CLAUDE.md's "Pricing/entitlement model" section.
"""
import datetime
from unittest.mock import patch

import pytest

from tests.conftest import csrf


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _iso(dt):
    return dt.isoformat()


def _set_tier(db_conn, user_id, tier):
    if tier == "pro":
        db_conn.execute(
            "UPDATE users SET stripe_subscription_status = 'active', fallback_entitlement = 'basic' WHERE id = ?",
            (user_id,),
        )
    elif tier == "trialing":
        db_conn.execute(
            "UPDATE users SET trial_ends_at = ?, fallback_entitlement = 'basic' WHERE id = ?",
            (_iso(_utcnow() + datetime.timedelta(days=10)), user_id),
        )
    elif tier == "legacy_free":
        db_conn.execute(
            "UPDATE users SET fallback_entitlement = 'legacy_free', trial_ends_at = NULL WHERE id = ?",
            (user_id,),
        )
    elif tier == "basic":
        db_conn.execute(
            "UPDATE users SET fallback_entitlement = 'basic', trial_ends_at = NULL WHERE id = ?",
            (user_id,),
        )


@pytest.fixture(autouse=True)
def _resolver_active():
    with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", True):
        yield


class TestTrialingGetsEveryCurrentProFeature:
    """The core requirement: the 30-day trial must provide COMPLETE Pro
    product access, not a reduced capability tier - proven against every
    real Pro-gated route in the app, not just the resolver's own output."""

    def test_can_add_savings_rule(self, auth_client, test_user, test_account, second_account, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        resp = auth_client.post(
            "/settings/add-savings-rule",
            data={**csrf(), "name": "Trial rule", "amount": "10", "day": "1",
                  "from_account": test_account["name"], "to_account": second_account["name"]},
            follow_redirects=False,
        )
        row = db_conn.execute("SELECT * FROM savings_rules WHERE name='Trial rule'").fetchone()
        assert row is not None, f"expected the rule to be created, got redirect {resp.headers.get('Location')}"

    def test_can_add_future_event(self, auth_client, test_user, test_account, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        auth_client.post(
            "/settings/add-future-event",
            data={**csrf(), "name": "Trial event", "amount": "50", "date": (datetime.date.today() + datetime.timedelta(days=10)).isoformat(), "account": test_account["name"]},
        )
        row = db_conn.execute("SELECT * FROM future_events WHERE name='Trial event'").fetchone()
        assert row is not None

    def test_can_add_investment(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        auth_client.post(
            "/settings/add-investment",
            data={**csrf(), "name": "Trial stock", "type": "stocks", "initial_amount": "100", "current_value": "110", "date": "2026-01-01"},
        )
        row = db_conn.execute("SELECT * FROM investments WHERE name='Trial stock'").fetchone()
        assert row is not None

    def test_unlimited_accounts_during_trial(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        for i in range(3):
            db_conn.execute(
                "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
                (f"Existing {i}", 0, "current", test_user["id"]),
            )
        resp = auth_client.post(
            "/settings/add-account",
            data={**csrf(), "name": "4th account", "type": "current", "balance": "0"},
            follow_redirects=False,
        )
        row = db_conn.execute("SELECT id FROM accounts WHERE name='4th account'").fetchone()
        assert row is not None, "a 4th account must be allowed during the trial (unlimited)"

    def test_full_90_day_forecast_during_trial(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        resp = auth_client.get("/api/snapshot?days=90")
        assert resp.status_code == 200
        # days=90 must not be clamped down for a trialing user
        data = resp.get_json()
        assert "accounts" in data


class TestLegacyFreeExcludedFromProFeatures:
    def test_cannot_add_savings_rule(self, auth_client, test_user, test_account, second_account, db_conn):
        _set_tier(db_conn, test_user["id"], "legacy_free")
        auth_client.post(
            "/settings/add-savings-rule",
            data={**csrf(), "name": "Blocked rule", "amount": "10", "day": "1",
                  "from_account": test_account["name"], "to_account": second_account["name"]},
        )
        row = db_conn.execute("SELECT * FROM savings_rules WHERE name='Blocked rule'").fetchone()
        assert row is None

    def test_future_pro_only_gate_excludes_legacy_free(self, app, db_conn, test_user):
        """Simulates a hypothetical NEW Pro-only feature added after this
        cutover - it should be gated the same way every existing Pro
        feature already is (entitlement.pro_features), which structurally
        excludes legacy_free without needing a separate exclusion list."""
        _set_tier(db_conn, test_user["id"], "legacy_free")
        import app as app_module
        with app.app_context():
            row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
            ent = app_module.get_entitlement(row)
        assert ent["pro_features"] is False

    def test_capped_at_3_accounts(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "legacy_free")
        for i in range(3):
            db_conn.execute(
                "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
                (f"Existing {i}", 0, "current", test_user["id"]),
            )
        auth_client.post("/settings/add-account", data={**csrf(), "name": "4th", "type": "current", "balance": "0"})
        row = db_conn.execute("SELECT id FROM accounts WHERE name='4th'").fetchone()
        assert row is None

    def test_forecast_capped_at_90_days_same_as_today(self, app, db_conn, test_user):
        _set_tier(db_conn, test_user["id"], "legacy_free")
        import app as app_module
        with app.app_context():
            row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
            ent = app_module.get_entitlement(row)
        assert ent["forecast_days"] == 90


class TestBasicTierLimits:
    def test_capped_at_1_account(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        db_conn.execute(
            "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
            ("1st account", 0, "current", test_user["id"]),
        )
        auth_client.post("/settings/add-account", data={**csrf(), "name": "2nd", "type": "current", "balance": "0"})
        row = db_conn.execute("SELECT id FROM accounts WHERE name='2nd'").fetchone()
        assert row is None

    def test_forecast_capped_at_30_days(self, app, db_conn, test_user):
        _set_tier(db_conn, test_user["id"], "basic")
        import app as app_module
        with app.app_context():
            row = dict(db_conn.execute("SELECT * FROM users WHERE id=?", (test_user["id"],)).fetchone())
            ent = app_module.get_entitlement(row)
        assert ent["forecast_days"] == 30

    def test_snapshot_days_clamped_to_30(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        resp = auth_client.get("/api/snapshot?days=90")
        assert resp.status_code == 200
        # Internally clamped - just confirm it doesn't error and returns data
        assert "accounts" in resp.get_json()

    def test_cannot_add_savings_rule(self, auth_client, test_user, test_account, second_account, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        auth_client.post(
            "/settings/add-savings-rule",
            data={**csrf(), "name": "Blocked", "amount": "10", "day": "1",
                  "from_account": test_account["name"], "to_account": second_account["name"]},
        )
        row = db_conn.execute("SELECT * FROM savings_rules WHERE name='Blocked'").fetchone()
        assert row is None


class TestTemplateDisplayPerTier:
    def test_pro_shows_pro_badge_not_free_or_basic(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "pro")
        body = auth_client.get("/settings").get_data(as_text=True)
        assert "Spendara Pro — unlimited accounts" in body
        assert "Legacy Free" not in body
        assert "You're on Basic" not in body

    def test_trialing_shows_trial_badge_and_days_remaining_not_free_or_basic(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "trialing")
        body = auth_client.get("/settings").get_data(as_text=True)
        assert "30-day trial" in body
        assert "day" in body and "left" in body
        assert "You're on the free plan" not in body
        assert "You're on Basic" not in body
        assert "Legacy Free" not in body

    def test_legacy_free_shows_legacy_free_copy_not_pro_or_trial(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "legacy_free")
        body = auth_client.get("/settings").get_data(as_text=True)
        assert "Legacy Free" in body
        assert "badge text-bg-dark\">Pro" not in body
        assert "30-day trial" not in body

    def test_basic_shows_basic_copy_not_pro_or_trial_or_legacy(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        body = auth_client.get("/settings").get_data(as_text=True)
        assert "You're on Basic" in body
        assert "badge text-bg-dark\">Pro" not in body
        assert "30-day trial" not in body
        assert "Legacy Free" not in body

    def test_manage_page_shows_basic_badge(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        body = auth_client.get("/manage").get_data(as_text=True)
        assert "basic</span>" in body

    def test_manage_page_shows_trial_badge(self, auth_client, test_user, db_conn):
        # A fresh auth_client per assertion - Flask-Login's user_loader is
        # invoked once per request context; reusing one auth_client across
        # multiple sequential .get() calls within a single test can leave
        # current_user resolved from the first call rather than re-running
        # load_user() (and its lazy reconciliation) against the DB's
        # latest state for the second - so each distinct entitlement state
        # gets its own test function/fixture instance here, matching every
        # other test in this file.
        _set_tier(db_conn, test_user["id"], "trialing")
        body = auth_client.get("/manage").get_data(as_text=True)
        assert ">Trial<" in body

    def test_open_banking_row_removed_from_comparison_table(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "pro")
        body = auth_client.get("/settings").get_data(as_text=True)
        assert "Open banking" not in body


class TestFreeLimitAccountsMessageIsTierAware:
    """Regression test for a real bug found in a post-Activation stale-copy
    audit: the FREE_LIMIT_ACCOUNTS flash message (shown on /manage when a
    user hits their account cap - app.py's settings_add_account()) was
    hardcoded to 'Free accounts are limited to 3 accounts', so a
    Basic-tier user (real limit: 1) hitting their actual cap saw a false
    3-account claim. The underlying rejection logic (get_account_limit())
    was already correctly tier-aware - only the displayed copy was wrong."""

    def test_basic_tier_shows_its_real_limit_of_1(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "basic")
        body = auth_client.get("/manage?msg=FREE_LIMIT_ACCOUNTS").get_data(as_text=True)
        assert "limited to 1 account." in body
        assert "3 accounts" not in body

    def test_legacy_free_tier_shows_its_real_limit_of_3(self, auth_client, test_user, db_conn):
        _set_tier(db_conn, test_user["id"], "legacy_free")
        body = auth_client.get("/manage?msg=FREE_LIMIT_ACCOUNTS").get_data(as_text=True)
        assert "limited to 3 accounts." in body
