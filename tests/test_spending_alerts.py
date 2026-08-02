"""
Tests for the Spending Alert Threshold feature.

Covers:
- POST /settings/save-alert-threshold (off / overall / per_account modes)
- get_triggered_spending_alerts() (app.py helper)
- Home page banner rendering via GET /
- Locked-account exclusion (same reasoning as forecast/overview calculations)
- Zero impact on users who haven't set a threshold
"""

import json
import pytest
from tests.conftest import csrf


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _save_alert(auth_client, mode="off", overall_threshold=None, per_account=None):
    data = {**csrf(), "mode": mode}
    if overall_threshold is not None:
        data["overall_threshold"] = str(overall_threshold)
    if per_account:
        for acc_id, val in per_account.items():
            data[f"threshold_{acc_id}"] = str(val)
    return auth_client.post("/settings/save-alert-threshold", data=data, follow_redirects=False)


def _get_user_alert(db_conn, user_id):
    return db_conn.execute(
        "SELECT alert_mode, alert_overall_threshold FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def _get_account_threshold(db_conn, account_id):
    row = db_conn.execute("SELECT alert_threshold FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return row["alert_threshold"] if row else None


# ── SAVE ROUTE ─────────────────────────────────────────────────────────────────

class TestSaveAlertThresholdRoute:
    def test_off_clears_mode_and_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="off")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] is None
        assert row["alert_overall_threshold"] is None

    def test_overall_saves_mode_and_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] == "overall"
        assert row["alert_overall_threshold"] == 100.0

    def test_overall_rejects_invalid_amount(self, auth_client, test_user, test_account, db_conn):
        resp = _save_alert(auth_client, mode="overall", overall_threshold="abc")
        assert resp.status_code in (302, 303)
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] is None

    def test_overall_rejects_zero_amount(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="overall", overall_threshold="0")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] is None

    def test_overall_rejects_negative_amount(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="overall", overall_threshold="-50")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] is None

    def test_per_account_saves_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] == "per_account"
        assert _get_account_threshold(db_conn, test_account["id"]) == 50.0

    def test_per_account_blank_field_clears_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        assert _get_account_threshold(db_conn, test_account["id"]) == 50.0
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: ""})
        assert _get_account_threshold(db_conn, test_account["id"]) is None

    def test_per_account_invalid_field_is_silently_cleared(self, auth_client, test_user, test_account, db_conn):
        resp = _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "abc"})
        assert resp.status_code in (302, 303)
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] == "per_account"
        assert _get_account_threshold(db_conn, test_account["id"]) is None

    def test_per_account_skips_locked_accounts(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET is_locked = 1 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        assert _get_account_threshold(db_conn, test_account["id"]) is None

    def test_off_clears_existing_per_account_thresholds(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        assert _get_account_threshold(db_conn, test_account["id"]) == 50.0
        _save_alert(auth_client, mode="off")
        assert _get_account_threshold(db_conn, test_account["id"]) is None

    def test_invalid_mode_defaults_to_off(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="bogus")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] is None

    def test_toggle_overall_then_per_account(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] == "overall"

        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_mode"] == "per_account"
        assert _get_account_threshold(db_conn, test_account["id"]) == 50.0

    def test_editing_overall_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        _save_alert(auth_client, mode="overall", overall_threshold="250")
        row = _get_user_alert(db_conn, test_user["id"])
        assert row["alert_overall_threshold"] == 250.0

    def test_editing_per_account_threshold(self, auth_client, test_user, test_account, db_conn):
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "75"})
        assert _get_account_threshold(db_conn, test_account["id"]) == 75.0


# ── get_triggered_spending_alerts() ──────────────────────────────────────────

class TestGetTriggeredSpendingAlerts:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import get_triggered_spending_alerts
        self.get_alerts = get_triggered_spending_alerts

    def _accounts_dict(self, **overrides):
        base = {
            "Current": {"balance": 1000.0, "active": True, "is_locked": False, "alert_threshold": None},
        }
        for name, fields in overrides.items():
            base.setdefault(name, {"balance": 0.0, "active": True, "is_locked": False, "alert_threshold": None})
            base[name].update(fields)
        return base

    def test_no_alert_mode_returns_empty(self, test_user, test_account, db_conn):
        accounts = self._accounts_dict(Current={"balance": 10.0})
        assert self.get_alerts(test_user["id"], accounts) == []

    def test_overall_triggers_when_at_or_below(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = 100 WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 100.0})
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 1
        assert alerts[0]["account"] is None
        assert alerts[0]["balance"] == 100.0
        assert alerts[0]["threshold"] == 100.0

    def test_overall_does_not_trigger_when_above(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = 100 WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 150.0})
        assert self.get_alerts(test_user["id"], accounts) == []

    def test_overall_sums_multiple_accounts(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = 100 WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 40.0}, Savings={"balance": 40.0})
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 1
        assert alerts[0]["balance"] == 80.0

    def test_overall_excludes_locked_accounts_from_total(self, test_user, test_account, db_conn):
        """A locked account's frozen balance shouldn't count toward - or rescue -
        the combined total, same reasoning as calculate_financial_overview()
        excluding locked accounts from spending/savings totals."""
        db_conn.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = 100 WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(
            Current={"balance": 50.0},
            Vault={"balance": 5000.0, "is_locked": True},
        )
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 1
        assert alerts[0]["balance"] == 50.0  # locked 5000 excluded, else this wouldn't trigger

    def test_per_account_triggers_for_matching_account(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 40.0, "alert_threshold": 50.0})
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 1
        assert alerts[0]["account"] == "Current"
        assert alerts[0]["balance"] == 40.0
        assert alerts[0]["threshold"] == 50.0

    def test_per_account_does_not_trigger_when_above(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 60.0, "alert_threshold": 50.0})
        assert self.get_alerts(test_user["id"], accounts) == []

    def test_per_account_triggers_at_exact_threshold(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 50.0, "alert_threshold": 50.0})
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 1

    def test_per_account_skips_accounts_with_no_threshold_set(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Current={"balance": 1.0, "alert_threshold": None})
        assert self.get_alerts(test_user["id"], accounts) == []

    def test_per_account_multiple_triggered_accounts(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(
            Current={"balance": 10.0, "alert_threshold": 50.0},
            Savings={"balance": 400.0, "alert_threshold": 500.0},
        )
        alerts = self.get_alerts(test_user["id"], accounts)
        assert len(alerts) == 2
        names = {a["account"] for a in alerts}
        assert names == {"Current", "Savings"}

    def test_per_account_excludes_locked_accounts(self, test_user, test_account, db_conn):
        """A locked account's balance is frozen/stale - it can never be
        usefully warned about, so it's excluded even if a threshold value
        happens to still be stored on it (e.g. from before it was locked)."""
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(
            Current={"balance": 1000.0, "alert_threshold": None},
            Vault={"balance": 1.0, "alert_threshold": 50.0, "is_locked": True},
        )
        assert self.get_alerts(test_user["id"], accounts) == []

    def test_per_account_excludes_inactive_accounts(self, test_user, test_account, db_conn):
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        accounts = self._accounts_dict(Old={"balance": 1.0, "alert_threshold": 50.0, "active": False})
        assert self.get_alerts(test_user["id"], accounts) == []


# ── HOME PAGE BANNER ─────────────────────────────────────────────────────────

class TestHomePageBanner:
    def test_no_threshold_set_shows_no_banner(self, auth_client, test_user, test_account):
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' not in html

    def test_overall_triggered_shows_banner(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 50 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' in html
        assert "50.00" in html
        assert "100.00" in html

    def test_overall_not_triggered_shows_no_banner(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 500 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' not in html

    def test_per_account_triggered_shows_account_name(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 30 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' in html
        assert test_account["name"] in html

    def test_per_account_not_triggered_shows_no_banner(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 500 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="per_account", per_account={test_account["id"]: "50"})
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' not in html

    def test_per_account_multiple_triggered_lists_both(self, auth_client, test_user, test_account, second_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 10 WHERE id = ?", (test_account["id"],))
        db_conn.execute("UPDATE accounts SET balance = 20 WHERE id = ?", (second_account["id"],))
        _save_alert(auth_client, mode="per_account", per_account={
            test_account["id"]: "50",
            second_account["id"]: "100",
        })
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' in html
        assert test_account["name"] in html
        assert second_account["name"] in html

    def test_locked_account_below_threshold_does_not_trigger_banner(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 5, is_locked = 1 WHERE id = ?", (test_account["id"],))
        # Set the threshold directly - the save route itself would refuse to
        # store one for a locked account, but this proves the read-side check
        # (get_triggered_spending_alerts) independently excludes it too.
        db_conn.execute("UPDATE accounts SET alert_threshold = 50 WHERE id = ?", (test_account["id"],))
        db_conn.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (test_user["id"],))
        resp = auth_client.get("/")
        html = resp.get_data(as_text=True)
        assert 'id="alertThresholdBanner"' not in html

    def test_disabling_threshold_removes_banner(self, auth_client, test_user, test_account, db_conn):
        db_conn.execute("UPDATE accounts SET balance = 10 WHERE id = ?", (test_account["id"],))
        _save_alert(auth_client, mode="overall", overall_threshold="100")
        resp = auth_client.get("/")
        assert 'id="alertThresholdBanner"' in resp.get_data(as_text=True)

        _save_alert(auth_client, mode="off")
        resp = auth_client.get("/")
        assert 'id="alertThresholdBanner"' not in resp.get_data(as_text=True)
