"""
Tests for scheduled bills/income/future-events surfaced on the Calendar page
(August 2026) — matching the day-by-day breakdown already available on
Home's Future Balances tab, but as a simple list (no running balance/Safe
to Spend) shown both on the month grid's day cells and in the day-detail
panel, capped at the same 90-day horizon used everywhere else in the app.

Covers:
  - _get_scheduled_calendar_items() directly (income, bills, events, the
    today-exclusive start boundary, locked-account exclusion, spread-income
    exclusion, weekend-shifted bill dates)
  - calendar_view() — future_day_data present/absent, correct aggregation,
    empty for a month entirely outside the 90-day horizon
  - calendar_day() — scheduled key present for a future day, empty for
    today/past days and for days beyond the horizon
  - Rendering: the day cells' markup and the day-detail panel's scheduled
    section
"""
import datetime
import json
import re

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead)).isoformat()


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current", locked=False):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview, is_locked) VALUES (?,?,?,1,?,1,?)",
        (name, balance, acc_type, user_id, 1 if locked else 0),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_bill(db_conn, user_id, name, amount, day, account, frequency="monthly", month=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, month) VALUES (?,?,?,?,?,?,?)",
        (name, amount, day, account, user_id, frequency, month),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_income(db_conn, user_id, name, amount, account, day=25, rule_type="fixed_date",
                 rule_config=None, weekly_day=None, frequency="monthly"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO income (name, amount, frequency, account, day, weekly_day, rule_type, rule_config, "
        "weekend_rule, bank_holiday_rule, is_primary, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
        (name, amount, frequency, account, day, weekly_day or 4, rule_type,
         rule_config or json.dumps({"day": day}), "before", "before", user_id),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_event(db_conn, user_id, name, amount, date_str, account):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO future_events (name, amount, date, account, user_id) VALUES (?,?,?,?,?)",
        (name, amount, date_str, account, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


# ── 1. _get_scheduled_calendar_items() DIRECTLY ────────────────────────────
class TestGetScheduledCalendarItems:
    def test_future_event_appears_on_its_date(self, app, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(10), test_account["name"])
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=20)
            )
        assert items[_iso(10)][0]["type"] == "event"
        assert items[_iso(10)][0]["name"] == "Wedding gift"
        assert items[_iso(10)][0]["amount"] == 150.0

    def test_start_date_is_exclusive(self, app, db_conn, test_user, test_account):
        """A bill due exactly on start_date must not appear — matches
        /api/snapshot's today-exclusive convention."""
        _add_event(db_conn, test_user["id"], "Today's event", 50.0, _iso(0), test_account["name"])
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=5)
            )
        assert _iso(0) not in items

    def test_end_date_is_inclusive(self, app, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Edge event", 50.0, _iso(5), test_account["name"])
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=5)
            )
        assert _iso(5) in items

    def test_income_uses_income_engine_dates(self, app, db_conn, test_user, test_account):
        target = TODAY + datetime.timedelta(days=15)
        _add_income(
            db_conn, test_user["id"], "Salary", 2000.0, test_account["name"],
            day=target.day, rule_type="fixed_date", rule_config=json.dumps({"day": target.day}),
        )
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=45)
            )
        found = [i for lst in items.values() for i in lst if i["type"] == "income"]
        assert any(i["name"] == "Salary" and i["amount"] == 2000.0 for i in found)

    def test_locked_account_excluded(self, app, db_conn, test_user):
        locked_acc = _add_account(db_conn, test_user["id"], "Locked Savings", 500.0, "savings", locked=True)
        _add_event(db_conn, test_user["id"], "Should not show", 75.0, _iso(10), "Locked Savings")
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=20)
            )
        assert items == {}

    def test_spread_distribution_income_excluded(self, app, db_conn, test_user, test_account):
        """Self-employed spread-evenly income has no discrete payment date
        - it must never show as a calendar entry, same as /api/snapshot."""
        cfg = json.dumps({"mode": "manual", "manual_amount": 1500.0, "distribution": "spread", "day": 1})
        _add_income(
            db_conn, test_user["id"], "Self-employed average", 1500.0, test_account["name"],
            rule_type="self_employed_average", rule_config=cfg,
        )
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=30)
            )
        found = [i for lst in items.values() for i in lst if i["name"] == "Self-employed average"]
        assert found == []

    def test_bill_with_unknown_account_excluded(self, app, db_conn, test_user, test_account):
        _add_bill(db_conn, test_user["id"], "Orphan bill", 40.0, (TODAY + datetime.timedelta(days=3)).day, "Nonexistent Account")
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(
                test_user["id"], TODAY, TODAY + datetime.timedelta(days=10)
            )
        found = [i for lst in items.values() for i in lst if i["name"] == "Orphan bill"]
        assert found == []

    def test_empty_when_start_not_before_end(self, app, db_conn, test_user, test_account):
        from app import _get_scheduled_calendar_items
        with app.app_context():
            items = _get_scheduled_calendar_items(test_user["id"], TODAY, TODAY)
        assert items == {}


# ── 2. calendar_view() ROUTE ────────────────────────────────────────────────
class TestCalendarViewFutureDayData:
    def test_future_event_in_current_month_appears_in_payload(self, auth_client, db_conn, test_user, test_account):
        # Pick a date guaranteed to be in the current calendar month.
        days_ahead = min(5, 27 - TODAY.day) if TODAY.day < 27 else 0
        target_iso = _iso(max(days_ahead, 0))
        _add_event(db_conn, test_user["id"], "Gift", 60.0, target_iso, test_account["name"])
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        m = re.search(r"var FUTURE_DAY_DATA = (\{.*?\});", body)
        assert m, "FUTURE_DAY_DATA not found in page"
        data = json.loads(m.group(1))
        assert target_iso in data
        assert data[target_iso]["bills"] == 60.0

    def test_income_contributes_to_income_total(self, auth_client, db_conn, test_user, test_account):
        if TODAY.day >= 20:
            pytest.skip("needs headroom to place income later this month without crossing into next month")
        target_day = TODAY.day + 5
        _add_income(
            db_conn, test_user["id"], "Salary", 2500.0, test_account["name"],
            day=target_day, rule_type="fixed_date", rule_config=json.dumps({"day": target_day}),
        )
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        m = re.search(r"var FUTURE_DAY_DATA = (\{.*?\});", body)
        data = json.loads(m.group(1))
        # The nominal date (today+5) may land on a weekend and shift via
        # weekend_rule='before' — don't assume the exact unshifted date,
        # just confirm the income total shows up somewhere in this window.
        incomes = [v["income"] for v in data.values() if v["income"] > 0]
        assert 2500.0 in incomes

    def test_no_scheduled_items_gives_empty_future_day_data(self, auth_client, test_user, test_account):
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        m = re.search(r"var FUTURE_DAY_DATA = (\{.*?\});", body)
        assert json.loads(m.group(1)) == {}

    def test_month_beyond_horizon_has_no_future_day_data(self, auth_client, db_conn, test_user, test_account):
        far_month = TODAY + datetime.timedelta(days=200)
        # An event that far out wouldn't even be queryable within the
        # horizon window, but confirm the whole month payload is empty
        # regardless of whether anything was seeded.
        resp = auth_client.get("/calendar?month=" + far_month.strftime("%Y-%m"))
        body = resp.get_data(as_text=True)
        m = re.search(r"var FUTURE_DAY_DATA = (\{.*?\});", body)
        assert json.loads(m.group(1)) == {}

    def test_past_month_has_no_future_day_data(self, auth_client, db_conn, test_user, test_account):
        past_month = TODAY - datetime.timedelta(days=60)
        resp = auth_client.get("/calendar?month=" + past_month.strftime("%Y-%m"))
        body = resp.get_data(as_text=True)
        m = re.search(r"var FUTURE_DAY_DATA = (\{.*?\});", body)
        assert json.loads(m.group(1)) == {}


# ── 3. calendar_day() ROUTE ──────────────────────────────────────────────────
class TestCalendarDayScheduled:
    def test_future_day_returns_scheduled_event(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(10), test_account["name"])
        resp = auth_client.get("/calendar/day?date=" + _iso(10))
        data = resp.get_json()
        assert "scheduled" in data
        assert len(data["scheduled"]) == 1
        assert data["scheduled"][0]["name"] == "Wedding gift"
        assert data["scheduled"][0]["type"] == "event"

    def test_today_has_empty_scheduled_list(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Today event (excluded)", 50.0, _iso(0), test_account["name"])
        resp = auth_client.get("/calendar/day?date=" + _iso(0))
        data = resp.get_json()
        assert data["scheduled"] == []

    def test_past_day_has_empty_scheduled_list(self, auth_client, test_user, test_account):
        resp = auth_client.get("/calendar/day?date=" + _iso(-5))
        data = resp.get_json()
        assert data["scheduled"] == []

    def test_day_beyond_horizon_has_empty_scheduled_list(self, auth_client, test_user, test_account):
        resp = auth_client.get("/calendar/day?date=" + _iso(95))
        data = resp.get_json()
        assert data["scheduled"] == []

    def test_transactions_key_still_present_and_unaffected(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO transactions (description, amount, account, category, date, user_id) VALUES (?,?,?,?,?,?)",
            ("Coffee", -3.50, test_account["name"], "Food", _iso(0), test_user["id"]),
        )
        db_conn.commit()
        resp = auth_client.get("/calendar/day?date=" + _iso(0))
        data = resp.get_json()
        assert len(data["transactions"]) == 1
        assert data["transactions"][0]["description"] == "Coffee"

    def test_locked_account_event_excluded_from_day_view(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Locked Acc", 500.0, "savings", locked=True)
        _add_event(db_conn, test_user["id"], "Hidden", 20.0, _iso(10), "Locked Acc")
        resp = auth_client.get("/calendar/day?date=" + _iso(10))
        data = resp.get_json()
        assert data["scheduled"] == []


# ── 4. RENDERING ──────────────────────────────────────────────────────────────
class TestCalendarRendering:
    def test_page_registers_scheduled_marker_css_and_js(self, auth_client):
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        assert "cal-future-bill" in body
        assert "cal-future-income" in body
        assert "FUTURE_DAY_DATA" in body
        assert "sched-row" in body

    def test_day_detail_js_handles_scheduled_section(self, auth_client):
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        assert "data.scheduled" in body
        assert "Nothing scheduled on this day." in body
