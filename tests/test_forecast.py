"""
Unit tests for simulate_balances_until() in Tracker.py.

_db_fetch is patched for every test so no real database is needed.
Savings rules are fetched once at the top of the simulation; weekly income
is fetched every Friday (weekday == 4).
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest


def _friday_after(d: date) -> date:
    """Return the first Friday on or after d."""
    days_ahead = (4 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def _simulate(
    target: date,
    accounts: dict,
    expenses: list = None,
    events: list = None,
    savings_rules: list = None,
    weekly_income: list = None,
):
    """
    Call simulate_balances_until with mocked _db_fetch.

    _db_fetch is called in two contexts:
      1. "SELECT * FROM savings_rules" → returns savings_rules list
      2. "SELECT * FROM income WHERE frequency = 'weekly'" → returns weekly_income list
    """
    from Tracker import simulate_balances_until

    rules = savings_rules or []
    wi = weekly_income or []

    def fake_db_fetch(query, params=None):
        if "savings_rules" in query:
            return rules
        if "income" in query and "weekly" in query:
            return wi
        return []

    with patch("Tracker._db_fetch", side_effect=fake_db_fetch):
        return simulate_balances_until(
            target,
            accounts,
            expenses or [],
            events or [],
        )


# ── BASIC BALANCE FLOW ────────────────────────────────────────────────────────
class TestSimulateBasic:
    def test_no_changes_returns_initial_balance(self):
        today = date.today()
        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        final, lowest = _simulate(today + timedelta(days=1), accounts)
        assert abs(final["Current"] - 1000.0) < 0.01

    def test_inactive_account_excluded(self):
        today = date.today()
        accounts = {
            "Current": {"balance": 1000.0, "type": "current", "active": True},
            "Hidden": {"balance": 5000.0, "type": "savings", "active": False},
        }
        final, lowest = _simulate(today + timedelta(days=1), accounts)
        assert "Hidden" not in final
        assert "Current" in final

    def test_target_equals_today_no_change(self):
        today = date.today()
        accounts = {"Current": {"balance": 500.0, "type": "current", "active": True}}
        # Simulation starts from tomorrow, so targeting today means 0 iterations
        final, lowest = _simulate(today, accounts)
        assert abs(final["Current"] - 500.0) < 0.01

    def test_multiple_accounts_tracked_independently(self):
        today = date.today()
        accounts = {
            "Current": {"balance": 1000.0, "type": "current", "active": True},
            "Savings": {"balance": 2000.0, "type": "savings", "active": True},
        }
        final, _ = _simulate(today + timedelta(days=1), accounts)
        assert abs(final["Current"] - 1000.0) < 0.01
        assert abs(final["Savings"] - 2000.0) < 0.01


# ── SCHEDULED EXPENSES ────────────────────────────────────────────────────────
class TestScheduledExpenses:
    def test_monthly_expense_deducted_on_correct_day(self):
        # Target a date where we know day 15 will fire
        today = date.today()
        # Build a range that includes a day-15
        target = today.replace(day=15) if today.day < 15 else (today + timedelta(days=30)).replace(day=15)
        if target <= today:
            target = target.replace(year=target.year + (1 if target.month == 12 else 0),
                                    month=target.month % 12 + 1)

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [{"day": 15, "amount": 100.0, "account": "Current", "frequency": "monthly"}]

        final, _ = _simulate(target, accounts, expenses)
        assert final["Current"] < 1000.0

    def test_expense_for_wrong_account_ignored(self):
        today = date.today()
        # Force day 1 to fire by targeting first of next month
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        target = today + timedelta(days=(last_day - today.day + 1))

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [{"day": 1, "amount": 500.0, "account": "Nonexistent", "frequency": "monthly"}]

        final, _ = _simulate(target, accounts, expenses)
        assert abs(final["Current"] - 1000.0) < 0.01

    def test_expense_with_none_day_skipped(self):
        today = date.today()
        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [{"day": None, "amount": 200.0, "account": "Current", "frequency": "monthly"}]

        final, _ = _simulate(today + timedelta(days=5), accounts, expenses)
        assert abs(final["Current"] - 1000.0) < 0.01

    def test_multiple_expenses_accumulate(self):
        today = date.today()
        # Both expenses fire on day 1 — target first of next month
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        target = today + timedelta(days=(last_day - today.day + 1))

        accounts = {"Current": {"balance": 2000.0, "type": "current", "active": True}}
        expenses = [
            {"day": 1, "amount": 200.0, "account": "Current", "frequency": "monthly"},
            {"day": 1, "amount": 300.0, "account": "Current", "frequency": "monthly"},
        ]

        final, _ = _simulate(target, accounts, expenses)
        assert abs(final["Current"] - (2000.0 - 500.0)) < 0.01


# ── WEEKLY INCOME ─────────────────────────────────────────────────────────────
class TestWeeklyIncome:
    def test_weekly_income_added_on_friday(self):
        today = date.today()
        next_friday = _friday_after(today + timedelta(days=1))

        accounts = {"Current": {"balance": 500.0, "type": "current", "active": True}}
        weekly_income = [{"account": "Current", "amount": 1000.0}]

        final, _ = _simulate(next_friday, accounts, weekly_income=weekly_income)
        assert final["Current"] > 500.0

    def test_weekly_income_for_wrong_account_ignored(self):
        today = date.today()
        next_friday = _friday_after(today + timedelta(days=1))

        accounts = {"Current": {"balance": 500.0, "type": "current", "active": True}}
        weekly_income = [{"account": "Savings", "amount": 1000.0}]

        final, _ = _simulate(next_friday, accounts, weekly_income=weekly_income)
        assert abs(final["Current"] - 500.0) < 0.01

    def test_no_weekly_income_when_no_friday(self):
        today = date.today()
        # Find a Tuesday; simulate Mon-Tue (no Friday)
        days_to_monday = (7 - today.weekday()) % 7 + 1  # next Monday
        next_monday = today + timedelta(days=days_to_monday)
        next_tuesday = next_monday + timedelta(days=1)

        if next_tuesday.weekday() == 4:
            # Edge case: skip
            return

        accounts = {"Current": {"balance": 500.0, "type": "current", "active": True}}
        weekly_income = [{"account": "Current", "amount": 1000.0}]

        final, _ = _simulate(next_tuesday, accounts, weekly_income=weekly_income)
        # No Friday in range Mon→Tue → no income applied
        assert abs(final["Current"] - 500.0) < 0.01


# ── FUTURE EVENTS ─────────────────────────────────────────────────────────────
class TestFutureEvents:
    def test_future_event_on_exact_date(self):
        today = date.today()
        target = today + timedelta(days=5)

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        events = [{"date": target, "account": "Current", "amount": 250.0}]

        final, _ = _simulate(target, accounts, events=events)
        assert abs(final["Current"] - 750.0) < 0.01

    def test_future_event_after_target_not_applied(self):
        today = date.today()
        target = today + timedelta(days=3)
        event_date = today + timedelta(days=10)

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        events = [{"date": event_date, "account": "Current", "amount": 250.0}]

        final, _ = _simulate(target, accounts, events=events)
        assert abs(final["Current"] - 1000.0) < 0.01

    def test_future_event_for_inactive_account_ignored(self):
        today = date.today()
        target = today + timedelta(days=5)

        accounts = {
            "Current": {"balance": 1000.0, "type": "current", "active": True},
            "Old": {"balance": 999.0, "type": "savings", "active": False},
        }
        events = [{"date": target, "account": "Old", "amount": 500.0}]

        final, _ = _simulate(target, accounts, events=events)
        assert "Old" not in final
        assert abs(final["Current"] - 1000.0) < 0.01


# ── SAVINGS RULES ─────────────────────────────────────────────────────────────
class TestSavingsRules:
    def test_monthly_rule_transfers_on_correct_day(self):
        today = date.today()
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        # Target day 1 of next month so the rule fires
        target = today + timedelta(days=(last_day - today.day + 1))

        accounts = {
            "Current": {"balance": 2000.0, "type": "current", "active": True},
            "Savings": {"balance": 0.0, "type": "savings", "active": True},
        }
        rules = [{
            "day": 1,
            "amount": 500.0,
            "from_account": "Current",
            "to_account": "Savings",
            "frequency": "monthly",
        }]

        final, _ = _simulate(target, accounts, savings_rules=rules)
        assert abs(final["Current"] - 1500.0) < 0.01
        assert abs(final["Savings"] - 500.0) < 0.01

    def test_rule_skipped_if_insufficient_funds(self):
        today = date.today()
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        target = today + timedelta(days=(last_day - today.day + 1))

        accounts = {
            "Current": {"balance": 100.0, "type": "current", "active": True},
            "Savings": {"balance": 0.0, "type": "savings", "active": True},
        }
        rules = [{
            "day": 1,
            "amount": 500.0,
            "from_account": "Current",
            "to_account": "Savings",
            "frequency": "monthly",
        }]

        final, _ = _simulate(target, accounts, savings_rules=rules)
        # Not enough funds → rule skipped
        assert abs(final["Current"] - 100.0) < 0.01
        assert abs(final["Savings"] - 0.0) < 0.01

    def test_weekly_rule_applies_on_friday(self):
        today = date.today()
        next_friday = _friday_after(today + timedelta(days=1))

        accounts = {
            "Current": {"balance": 2000.0, "type": "current", "active": True},
            "Savings": {"balance": 0.0, "type": "savings", "active": True},
        }
        rules = [{
            "day": 0,  # day is ignored for weekly
            "amount": 100.0,
            "from_account": "Current",
            "to_account": "Savings",
            "frequency": "weekly",
        }]

        final, _ = _simulate(next_friday, accounts, savings_rules=rules)
        assert final["Savings"] > 0


# ── LOWEST BALANCE TRACKING ───────────────────────────────────────────────────
class TestLowestBalances:
    def test_lowest_balance_tracked(self):
        today = date.today()
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        target = today + timedelta(days=(last_day - today.day + 1))

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [{"day": 1, "amount": 800.0, "account": "Current", "frequency": "monthly"}]

        final, lowest = _simulate(target, accounts, expenses)
        # lowest["Current"] should be <= final["Current"] (or even lower)
        assert lowest["Current"] <= final["Current"]

    def test_lowest_never_exceeds_initial(self):
        today = date.today()
        target = today + timedelta(days=10)

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [{"day": (today + timedelta(days=3)).day, "amount": 200.0,
                     "account": "Current", "frequency": "monthly"}]

        _, lowest = _simulate(target, accounts, expenses)
        assert lowest["Current"] <= 1000.0

    def test_lowest_balance_equals_initial_when_no_deductions(self):
        today = date.today()
        target = today + timedelta(days=5)

        accounts = {"Current": {"balance": 500.0, "type": "current", "active": True}}

        final, lowest = _simulate(target, accounts)
        assert abs(lowest["Current"] - 500.0) < 0.01

    def test_lowest_reflects_minimum_point(self):
        today = date.today()
        # Use two expenses on different days; lowest should be after both applied
        import calendar as cal
        last_day = cal.monthrange(today.year, today.month)[1]
        fire_day = (today + timedelta(days=2)).day

        target = today + timedelta(days=4)

        accounts = {"Current": {"balance": 1000.0, "type": "current", "active": True}}
        expenses = [
            {"day": fire_day, "amount": 300.0, "account": "Current", "frequency": "monthly"},
        ]

        final, lowest = _simulate(target, accounts, expenses)
        if today.day < fire_day <= target.day or today.day >= fire_day:
            assert lowest["Current"] <= 700.0
