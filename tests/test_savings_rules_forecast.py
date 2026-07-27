"""
Tests for savings rules appearing in financial calculations.

Rules under test:
- api_snapshot includes savings rule deduction in bills_due for from_account
- api_snapshot includes savings rule deposit in income_arriving for to_account
- api_snapshot updates simulated balances correctly for both accounts
- calculate_financial_overview includes savings rules in Bills left total
- savings rules are deducted from safe_spending when from_account is spending type
- savings rules with day already past this cycle are not included
"""

import json
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from tests.conftest import csrf


class _FrozenDateTime(datetime):
    """datetime subclass with a fixed .today() - patched onto app.datetime so
    calculate_financial_overview() sees a deterministic 'today' regardless of
    the real calendar date. Only overrides today(); everything else behaves
    like a normal datetime/date so unrelated code paths aren't affected."""
    _frozen = datetime(2026, 1, 15)

    @classmethod
    def today(cls):
        return cls._frozen


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def current_account(db_conn, test_user):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
        "VALUES (?, ?, ?, 1, ?, 1)",
        ("Current", 1000.00, "current", test_user["id"]),
    )
    acct_id = cur.lastrowid
    cur.close()
    yield {"id": acct_id, "name": "Current", "balance": 1000.00}
    db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))


@pytest.fixture
def savings_account(db_conn, test_user):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
        "VALUES (?, ?, ?, 1, ?, 1)",
        ("Flex Saver", 500.00, "savings", test_user["id"]),
    )
    acct_id = cur.lastrowid
    cur.close()
    yield {"id": acct_id, "name": "Flex Saver", "balance": 500.00}
    db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))


def _add_savings_rule(db_conn, user_id, name, amount, day, from_account, to_account):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) "
        "VALUES (?, ?, ?, 'monthly', ?, ?, ?)",
        (name, amount, day, from_account, to_account, user_id),
    )
    rule_id = cur.lastrowid
    cur.close()
    return rule_id


def _delete_savings_rule(db_conn, rule_id):
    db_conn.execute("DELETE FROM savings_rules WHERE id = ?", (rule_id,))


# ---------------------------------------------------------------------------
# api_snapshot tests
# ---------------------------------------------------------------------------

class TestSnapshotSavingsRules:

    def test_deduction_appears_in_bills_due(self, auth_client, test_user, current_account, savings_account, db_conn):
        """Savings rule deduction should appear in bills_due for the from_account."""
        # Use a future day so it fires within the 30-day window
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Monthly saving", 200, target_day, "Current", "Flex Saver")
        try:
            resp = auth_client.get("/api/snapshot?days=30")
            data = resp.get_json()
            bill_names = [b["name"] for b in data.get("bills_due", [])]
            assert any("Flex Saver" in n for n in bill_names), f"Expected savings transfer in bills_due, got: {bill_names}"
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_deposit_appears_in_income_arriving(self, auth_client, test_user, current_account, savings_account, db_conn):
        """Savings rule deposit should appear in income_arriving for the to_account."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Monthly saving", 200, target_day, "Current", "Flex Saver")
        try:
            resp = auth_client.get("/api/snapshot?days=30")
            data = resp.get_json()
            income_names = [i["name"] for i in data.get("income_arriving", [])]
            assert any("Current" in n for n in income_names), f"Expected savings deposit in income_arriving, got: {income_names}"
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_balance_decreases_for_from_account(self, auth_client, test_user, current_account, savings_account, db_conn):
        """Simulated balance for from_account should decrease by the rule amount."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Monthly saving", 200, target_day, "Current", "Flex Saver")
        try:
            resp = auth_client.get("/api/snapshot?days=30")
            data = resp.get_json()
            current_data = data["accounts"].get("Current", {})
            assert current_data["balance_on_date"] < current_data["balance_today"], \
                "Current account balance should decrease after savings transfer"
            assert current_data["balance_today"] - current_data["balance_on_date"] >= 200
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_balance_increases_for_to_account(self, auth_client, test_user, current_account, savings_account, db_conn):
        """Simulated balance for to_account should increase by the rule amount."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Monthly saving", 200, target_day, "Current", "Flex Saver")
        try:
            resp = auth_client.get("/api/snapshot?days=30")
            data = resp.get_json()
            saver_data = data["accounts"].get("Flex Saver", {})
            assert saver_data["balance_on_date"] > saver_data["balance_today"], \
                "Flex Saver balance should increase after savings transfer"
            assert saver_data["balance_on_date"] - saver_data["balance_today"] >= 200
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_rule_not_in_window_not_included(self, auth_client, test_user, current_account, savings_account, db_conn):
        """A rule whose day has already passed this month should not appear in a short window."""
        # Day 1 of the month — if today is not day 1, this is in the past
        today = date.today()
        if today.day == 1:
            pytest.skip("Cannot test past-day rule on day 1 of month")
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Past rule", 200, 1, "Current", "Flex Saver")
        try:
            resp = auth_client.get("/api/snapshot?days=3")
            data = resp.get_json()
            bill_names = [b["name"] for b in data.get("bills_due", [])]
            assert not any("Flex Saver" in n for n in bill_names), \
                f"Past-month rule should not appear in 3-day window, got: {bill_names}"
        finally:
            _delete_savings_rule(db_conn, rule_id)


# ---------------------------------------------------------------------------
# calculate_financial_overview / Bills left tests
# ---------------------------------------------------------------------------

class TestOverviewSavingsRules:

    def _get_overview(self, auth_client):
        """Fetch the home page and extract overview from the rendered context via /api/overview."""
        start = date.today().replace(day=1).isoformat()
        import calendar
        today = date.today()
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()
        resp = auth_client.get(f"/api/overview?start={start}&end={end}")
        return resp.get_json()

    def test_savings_rule_included_in_future_bills(self, auth_client, test_user, current_account, savings_account, db_conn):
        """A savings rule due this cycle should be included in future_bills total."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Auto save", 150, target_day, "Current", "Flex Saver")
        try:
            data = self._get_overview(auth_client)
            assert data["future_bills"] >= 150, \
                f"future_bills should include savings rule amount, got: {data['future_bills']}"
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_savings_rule_appears_in_future_bills_list(self, auth_client, test_user, current_account, savings_account, db_conn):
        """A savings rule should appear as an item in future_bills_list."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Auto save", 150, target_day, "Current", "Flex Saver")
        try:
            data = self._get_overview(auth_client)
            bill_names = [b["name"] for b in data.get("future_bills_list", [])]
            assert "Auto save" in bill_names, \
                f"Savings rule name should appear in future_bills_list, got: {bill_names}"
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_savings_rule_reduces_safe_spending(self, auth_client, test_user, current_account, savings_account, db_conn):
        """Savings rule from a spending account should reduce safe_spending."""
        today = date.today()
        target_day = (today + timedelta(days=5)).day
        # No other bills — safe spending should equal balance minus rule amount
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Auto save", 150, target_day, "Current", "Flex Saver")
        try:
            data = self._get_overview(auth_client)
            # safe_spending = spending_balance - future_bills, rule should be in future_bills
            assert data["safe_spending"] <= current_account["balance"] - 150, \
                f"Safe spending should be reduced by savings rule, got: {data['safe_spending']}"
        finally:
            _delete_savings_rule(db_conn, rule_id)

    def test_past_savings_rule_not_included(self, auth_client, test_user, current_account, savings_account, db_conn):
        """A savings rule whose day has passed this month should not appear in Bills left.

        Uses a fixed 'today' (15th of the month, patched onto app.datetime) rather
        than the real calendar date. calculate_financial_overview() has an
        intentional near-term lookahead (~today+4 to today+5 days) that surfaces
        a rule's *next* occurrence when it crosses a month boundary soon - so
        asserting "never appears" using the real date would fail during the last
        few days of any month, when day-1-of-next-month legitimately falls inside
        that window. Freezing 'today' to the 15th keeps this test about the
        intended behaviour (a day-1 rule already passed this month) rather than
        an unrelated near-month-end interaction.
        """
        rule_id = _add_savings_rule(db_conn, test_user["id"], "Past save", 150, 1, "Current", "Flex Saver")
        try:
            with patch("app.datetime", _FrozenDateTime):
                resp = auth_client.get("/api/overview?start=2026-01-01&end=2026-01-31")
            data = resp.get_json()
            bill_names = [b["name"] for b in data.get("future_bills_list", [])]
            assert "Past save" not in bill_names, \
                f"Past-month savings rule should not be in Bills left, got: {bill_names}"
        finally:
            _delete_savings_rule(db_conn, rule_id)
