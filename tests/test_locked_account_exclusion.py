"""
Tests for excluding locked accounts from forecast/overview calculations.

Decided behavior: a locked account's balance is frozen (no new activity can
touch it), so including it in Safe to Spend / net worth / the forecast would
present an increasingly stale number with the same confidence as live data.
Locked accounts must be excluded from all balance aggregations and
projections, but must remain fully visible (greyed out) in Manage > Accounts
- this file only covers the calculation-layer exclusion.
"""
import json
import html as html_lib
import re

import pytest

from tests.conftest import csrf


def _lock(db_conn, account_id):
    db_conn.execute("UPDATE accounts SET is_locked = 1 WHERE id = ?", (account_id,))


def _unlock(db_conn, account_id):
    db_conn.execute("UPDATE accounts SET is_locked = 0 WHERE id = ?", (account_id,))


class TestHomeOverviewExcludesLockedBalance:
    def test_net_worth_includes_both_accounts_when_unlocked(self, auth_client, test_account, second_account):
        resp = auth_client.get("/")
        assert b"\xc2\xa31500.00" in resp.data  # £1500.00 = 1000 (current) + 500 (savings)

    def test_net_worth_excludes_locked_account_balance(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/")
        assert b"\xc2\xa31000.00" in resp.data  # only the unlocked Current account
        assert b"\xc2\xa31500.00" not in resp.data

    def test_locked_note_appears_with_correct_count(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/")
        text = resp.data.decode()
        assert "1 account locked" in text
        assert "Upgrade to Pro" in text

    def test_no_locked_note_when_nothing_locked(self, auth_client, test_account, second_account):
        resp = auth_client.get("/")
        assert "account locked" not in resp.data.decode()

    def test_reupgrade_restores_locked_balance_to_net_worth(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/")
        assert b"\xc2\xa31000.00" in resp.data

        _unlock(db_conn, second_account["id"])
        resp = auth_client.get("/")
        assert b"\xc2\xa31500.00" in resp.data
        assert "account locked" not in resp.data.decode()


class TestApiOverviewExcludesLockedBalance:
    def _get_overview(self, auth_client, days=30):
        from datetime import date, timedelta
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=days)).isoformat()
        resp = auth_client.get(f"/api/overview?start={start}&end={end}")
        return resp.get_json()

    def test_safe_spending_excludes_locked_account(self, auth_client, test_user, test_account, second_account, db_conn):
        # Make the second account a spending-type account so it would count if included
        db_conn.execute("UPDATE accounts SET type = 'current' WHERE id = ?", (second_account["id"],))
        unlocked_data = self._get_overview(auth_client)

        _lock(db_conn, second_account["id"])
        locked_data = self._get_overview(auth_client)

        # With the second £500 current account locked out, safe_spending should drop
        assert locked_data["safe_spending"] < unlocked_data["safe_spending"]


class TestForecastExcludesLockedAccount:
    def test_locked_account_absent_from_forecast_chart_data(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/forecast")
        text = resp.data.decode()

        m = re.search(r"data-accounts='(\[.*?\])'", text)
        assert m, "could not find data-accounts attribute in forecast page"
        names = json.loads(html_lib.unescape(m.group(1)))
        assert second_account["name"] not in names
        assert test_account["name"] in names

    def test_locked_note_shown_on_forecast_page(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/forecast")
        text = resp.data.decode()
        assert "1 account locked" in text
        assert "Not included in this forecast" in text

    def test_no_locked_note_when_nothing_locked_on_forecast(self, auth_client, test_account, second_account):
        resp = auth_client.get("/forecast")
        assert "account locked" not in resp.data.decode()

    def test_reupgrade_restores_account_to_forecast(self, auth_client, test_account, second_account, db_conn):
        import app as app_module

        def _account_names(resp):
            m = re.search(r"data-accounts='(\[.*?\])'", resp.data.decode())
            assert m
            return json.loads(html_lib.unescape(m.group(1)))

        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/forecast?refresh=1")
        assert second_account["name"] not in _account_names(resp)

        app_module.sync_account_locks(test_account["user_id"], True)
        resp = auth_client.get("/forecast?refresh=1")
        assert second_account["name"] in _account_names(resp)
        assert "account locked" not in resp.data.decode()


class TestApiSnapshotExcludesLockedAccount:
    def test_locked_account_absent_from_snapshot_response(self, auth_client, test_account, second_account, db_conn):
        _lock(db_conn, second_account["id"])
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        assert second_account["name"] not in data["accounts"]
        assert test_account["name"] in data["accounts"]

    def test_unlocked_snapshot_includes_both(self, auth_client, test_account, second_account):
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        assert test_account["name"] in data["accounts"]
        assert second_account["name"] in data["accounts"]


class TestAutoApplySkipsLockedAccounts:
    def test_pending_bill_on_locked_account_is_excluded(self, db_conn, test_user, test_account, second_account):
        from datetime import date, timedelta
        import app as app_module

        _lock(db_conn, second_account["id"])

        past_day = (date.today() - timedelta(days=2)).day or 1
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, last_applied) "
            "VALUES (?, ?, ?, ?, ?, 'monthly', ?)",
            ("Locked Bill", 50.0, past_day, second_account["name"], test_user["id"],
             (date.today() - timedelta(days=40)).isoformat()),
        )
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, last_applied) "
            "VALUES (?, ?, ?, ?, ?, 'monthly', ?)",
            ("Unlocked Bill", 20.0, past_day, test_account["name"], test_user["id"],
             (date.today() - timedelta(days=40)).isoformat()),
        )

        pending = app_module.get_pending_auto_apply_items(test_user["id"])
        names = [p["name"] for p in pending]
        assert "Locked Bill" not in names
        assert "Unlocked Bill" in names
