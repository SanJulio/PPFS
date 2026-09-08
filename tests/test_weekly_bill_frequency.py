"""
Tests for weekly scheduled-bill frequency (September 2026).

Scheduled bills previously only supported monthly and annual frequency.
This adds a "weekly" option, storing the day-of-week in a new nullable
scheduled_expenses.weekly_day column (mirroring the income table's exact
day/weekly_day split rather than overloading the existing 1-31 `day`
column). The core risk investigated before implementing: a weekly bill
can genuinely occur 4+ times within a typical ~30-day cycle, and every
engine call site previously assumed a bill fires at most once (sometimes
twice, for the yearly case) per period - several sites either silently
skipped weekly bills entirely, or (a real bug found in forecast()'s
day-by-day simulation) would have mis-treated a weekly bill as monthly,
firing it once a month on a bogus day-of-month instead of every week.

Covers:
  - _bill_has_valid_schedule() / _get_occurrences_between() directly -
    the core weekly multi-occurrence logic every other site relies on
  - _get_scheduled_calendar_items() (Calendar) - multiple occurrences in
    a bounded window
  - api_snapshot() (Future Balances) - multiple bills_due entries
  - forecast() - the day-by-day simulation deducts on every matching
    weekday (not just once, and not mis-fired as if monthly), and
    upcoming_items includes every occurrence across the 90-day window
  - flow() (Cashflow) - a remaining weekly occurrence this month is no
    longer silently dropped (it used to be, unconditionally, for every
    non-monthly/yearly frequency)
  - mark_bill_paid() - uses the row's own due_date rather than crashing
    on a day-of-month computation that doesn't apply to a weekly bill
  - Add/edit bill routes - weekly_day validation, day no longer required
  - normalised_totals()/_monthly_eq() - already correct, confirmed unaffected
  - UI markup - Weekly option, day-of-week picker, "Every <day>" label
"""
import datetime
import html as html_lib
import json
import re

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead)).isoformat()


def _add_weekly_bill(db_conn, user_id, name, amount, weekly_day, account, last_applied=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, weekly_day, last_applied) "
        "VALUES (?,?,NULL,?,?,?,?,?)",
        (name, amount, account, user_id, "weekly", weekly_day, last_applied),
    )
    db_conn.commit()
    return cur.lastrowid


def _get_ov_init_data(body):
    m = re.search(r'<script type="application/json" id="ov-init-data">(.*?)</script>', body, re.DOTALL)
    assert m, "ov-init-data script tag not found"
    return json.loads(html_lib.unescape(m.group(1)))


def _get_data_attr(body, attr_name):
    """Extract and HTML-unescape a data-*='...' attribute's JSON content
    (Jinja auto-escapes it as an attribute value)."""
    m = re.search(re.escape(attr_name) + r"='(.*?)'", body, re.DOTALL)
    assert m, f"{attr_name} attribute not found"
    return json.loads(html_lib.unescape(m.group(1)))


# ── 1. _bill_has_valid_schedule() / _get_occurrences_between() DIRECTLY ─────
class TestCoreOccurrenceLogic:
    def test_bill_has_valid_schedule_weekly_with_day_set(self, app):
        import app as app_module
        with app.app_context():
            assert app_module._bill_has_valid_schedule({"frequency": "weekly", "weekly_day": 2}) is True

    def test_bill_has_valid_schedule_weekly_without_day_is_invalid(self, app):
        import app as app_module
        with app.app_context():
            assert app_module._bill_has_valid_schedule({"frequency": "weekly", "weekly_day": None}) is False

    def test_bill_has_valid_schedule_monthly_unaffected(self, app):
        import app as app_module
        with app.app_context():
            assert app_module._bill_has_valid_schedule({"frequency": "monthly", "day": 15}) is True
            assert app_module._bill_has_valid_schedule({"frequency": "monthly", "day": None}) is False

    def test_weekly_occurrences_exactly_four_in_a_28_day_window(self, app):
        """The core correctness claim: a weekly bill fires every matching
        weekday, not just once - any 28-day (4-week) window contains
        exactly 4 occurrences of a single weekday."""
        import app as app_module
        with app.app_context():
            wd = TODAY.weekday()
            occurrences = app_module._get_occurrences_between(
                {"frequency": "weekly", "weekly_day": wd},
                TODAY, TODAY + datetime.timedelta(days=27),
            )
        assert len(occurrences) == 4
        for d in occurrences:
            assert d.weekday() == wd

    def test_weekly_no_weekend_shift(self, app):
        """Unlike monthly/yearly bills, a weekly bill's chosen weekday is
        never shifted, even if it falls on a Saturday/Sunday - the
        weekday IS the deliberate occurrence."""
        import app as app_module
        with app.app_context():
            occurrences = app_module._get_occurrences_between(
                {"frequency": "weekly", "weekly_day": 5},  # Saturday
                TODAY, TODAY + datetime.timedelta(days=13),
            )
        for d in occurrences:
            assert d.weekday() == 5

    def test_weekly_missing_weekly_day_returns_empty(self, app):
        import app as app_module
        with app.app_context():
            assert app_module._get_occurrences_between(
                {"frequency": "weekly", "weekly_day": None}, TODAY, TODAY + datetime.timedelta(days=30)
            ) == []


# ── 2. _get_scheduled_calendar_items() (Calendar) ────────────────────────────
class TestCalendarWeeklyBills:
    def test_weekly_bill_appears_on_every_matching_day_in_window(self, app, db_conn, test_user, test_account):
        import app as app_module
        wd = TODAY.weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        with app.app_context():
            # start_date is exclusive (see _get_scheduled_calendar_items'
            # own docstring) - pass yesterday so today itself can be the
            # first matching occurrence, same as every real caller does.
            items = app_module._get_scheduled_calendar_items(
                test_user["id"], TODAY - datetime.timedelta(days=1), TODAY + datetime.timedelta(days=26)
            )
        matches = [k for k, v in items.items() if any(i["name"] == "Cleaner" for i in v)]
        assert len(matches) == 4


# ── 3. api_snapshot() (Future Balances) ──────────────────────────────────────
class TestSnapshotWeeklyBills:
    def test_weekly_bill_appears_multiple_times_in_bills_due(self, auth_client, db_conn, test_user, test_account):
        wd = TODAY.weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        resp = auth_client.get("/api/snapshot?days=28")
        data = resp.get_json()
        matches = [b for b in data["bills_due"] if b["name"] == "Cleaner"]
        assert len(matches) == 4
        assert all(b["amount"] == 40.0 for b in matches)


# ── 4. forecast() — day-by-day simulation + upcoming_items ──────────────────
class TestForecastWeeklyBills:
    def test_weekly_bill_deducted_on_every_matching_weekday(self, auth_client, db_conn, test_user, test_account):
        wd = TODAY.weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        resp = auth_client.get("/forecast")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        snapshots = _get_data_attr(body, "data-snapshots")
        # 90-day window, weekly bill: balance for this account must have
        # dropped by at least 12 * 40 by the end (90 // 7 == 12 guaranteed
        # occurrences minimum), not just once (the old monthly-mis-fire
        # bug) or not at all (the old silent-skip gap).
        start_balance = snapshots[0][test_account["name"]]
        end_balance = snapshots[-1][test_account["name"]]
        total_drop = start_balance - end_balance
        assert total_drop >= 12 * 40.0 - 0.01

    def test_weekly_bill_in_upcoming_items_multiple_times(self, auth_client, db_conn, test_user, test_account):
        wd = TODAY.weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        upcoming = _get_data_attr(body, "data-upcoming")
        matches = [i for i in upcoming if i["name"] == "Cleaner"]
        assert len(matches) >= 12


# ── 5. flow() (Cashflow) — a weekly bill is no longer silently dropped ──────
class TestFlowWeeklyBills:
    def test_weekly_bill_shows_up_as_still_to_pay(self, auth_client, db_conn, test_user, test_account):
        # A weekly bill due tomorrow is guaranteed to still be "this
        # month" unless today is the very last day of the month - skip
        # that rare edge rather than adding cross-month logic just for
        # this regression guard (matches the established pattern used
        # elsewhere in this test suite for month-boundary flakiness).
        import calendar as _cal
        last_day_of_month = _cal.monthrange(TODAY.year, TODAY.month)[1]
        if TODAY.day == last_day_of_month:
            pytest.skip("today is the last day of the month - tomorrow would cross into next month")
        tomorrow_wd = (TODAY + datetime.timedelta(days=1)).weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, tomorrow_wd, test_account["name"])
        resp = auth_client.get("/flow")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Cleaner" in body


# ── 6. mark_bill_paid() — uses due_date, not a day-of-month crash ───────────
class TestMarkBillPaidWeekly:
    def test_mark_weekly_bill_paid_using_due_date(self, auth_client, db_conn, test_user, test_account):
        wd = TODAY.weekday()
        bill_id = _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"], last_applied=_iso(-1))
        resp = auth_client.post(
            "/mark-bill-paid",
            json={**csrf(), "bill_id": bill_id, "name": "Cleaner", "amount": 40.0,
                  "account": test_account["name"], "due_date": _iso(0)},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        row = db_conn.execute("SELECT last_applied FROM scheduled_expenses WHERE id=?", (bill_id,)).fetchone()
        assert row["last_applied"] == _iso(0)
        tx = db_conn.execute(
            "SELECT * FROM transactions WHERE user_id=? AND description='Cleaner'", (test_user["id"],)
        ).fetchone()
        assert tx is not None
        assert float(tx["amount"]) == -40.0

    def test_mark_weekly_bill_paid_without_due_date_fails_cleanly(self, auth_client, db_conn, test_user, test_account):
        """No day-of-month to fall back to for a weekly bill and no
        due_date supplied - must fail with a clean 400, not a crash."""
        wd = TODAY.weekday()
        bill_id = _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        resp = auth_client.post(
            "/mark-bill-paid",
            json={**csrf(), "bill_id": bill_id, "name": "Cleaner", "amount": 40.0,
                  "account": test_account["name"]},
        )
        assert resp.status_code == 400


# ── 7. ADD / EDIT BILL ROUTES ────────────────────────────────────────────────
class TestAddEditBillWeekly:
    def test_add_weekly_bill(self, auth_client, db_conn, test_user, test_account):
        resp = auth_client.post(
            "/settings/add-bill",
            data={**csrf(), "name": "Cleaner", "amount": "40", "frequency": "weekly",
                  "weekly_day": "2", "account": test_account["name"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM scheduled_expenses WHERE name='Cleaner'").fetchone()
        assert row is not None
        assert row["frequency"] == "weekly"
        assert row["weekly_day"] == 2
        assert row["day"] is None

    def test_add_weekly_bill_missing_weekly_day_rejected(self, auth_client, db_conn, test_user, test_account):
        resp = auth_client.post(
            "/settings/add-bill",
            data={**csrf(), "name": "Cleaner", "amount": "40", "frequency": "weekly", "account": test_account["name"]},
            follow_redirects=False,
        )
        row = db_conn.execute("SELECT * FROM scheduled_expenses WHERE name='Cleaner'").fetchone()
        assert row is None
        assert "Missing" in resp.headers["Location"]

    def test_add_weekly_bill_invalid_weekly_day_rejected(self, auth_client, db_conn, test_user, test_account):
        resp = auth_client.post(
            "/settings/add-bill",
            data={**csrf(), "name": "Cleaner", "amount": "40", "frequency": "weekly",
                  "weekly_day": "9", "account": test_account["name"]},
            follow_redirects=False,
        )
        row = db_conn.execute("SELECT * FROM scheduled_expenses WHERE name='Cleaner'").fetchone()
        assert row is None

    def test_edit_bill_from_monthly_to_weekly(self, auth_client, db_conn, test_user, test_account):
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) VALUES (?,?,?,?,?,?)",
            ("Rent", 500.0, 1, test_account["name"], test_user["id"], "monthly"),
        )
        bill_id = cur.lastrowid
        db_conn.commit()

        auth_client.post(
            "/settings/edit-bill",
            data={**csrf(), "id": bill_id, "name": "Rent", "amount": "500", "frequency": "weekly",
                  "weekly_day": "0", "account": test_account["name"]},
        )
        row = db_conn.execute("SELECT * FROM scheduled_expenses WHERE id=?", (bill_id,)).fetchone()
        assert row["frequency"] == "weekly"
        assert row["weekly_day"] == 0
        assert row["day"] is None

    def test_edit_bill_from_weekly_to_monthly(self, auth_client, db_conn, test_user, test_account):
        bill_id = _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, 2, test_account["name"])
        auth_client.post(
            "/settings/edit-bill",
            data={**csrf(), "id": bill_id, "name": "Cleaner", "amount": "40", "frequency": "monthly",
                  "day": "15", "account": test_account["name"]},
        )
        row = db_conn.execute("SELECT * FROM scheduled_expenses WHERE id=?", (bill_id,)).fetchone()
        assert row["frequency"] == "monthly"
        assert row["day"] == 15


# ── 8. normalised_totals() ALREADY CORRECT ──────────────────────────────────
class TestMonthlyEquivalentAlreadyCorrect:
    def test_weekly_bill_monthly_equivalent_uses_52_over_12(self, app):
        import app as app_module
        with app.app_context():
            eq = app_module._monthly_eq(100.0, "weekly")
        assert eq == pytest.approx(100.0 * 52 / 12)

    def test_manage_bills_monthly_total_includes_weekly_bill(self, auth_client, db_conn, test_user, test_account):
        wd = TODAY.weekday()
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, wd, test_account["name"])
        resp = auth_client.get("/manage?tab=bills")
        body = resp.get_data(as_text=True)
        expected_monthly = 40.0 * 52 / 12
        assert f"{expected_monthly:,.2f}" in body


# ── 9. UI MARKUP ──────────────────────────────────────────────────────────────
class TestBillFormUIMarkup:
    def test_add_bill_modal_has_weekly_option_and_day_picker(self, auth_client):
        resp = auth_client.get("/manage")
        body = resp.get_data(as_text=True)
        assert '<option value="weekly">Weekly</option>' in body
        assert 'name="weekly_day"' in body
        assert "toggleBillFreqFields" in body

    def test_bill_list_shows_every_weekday_label(self, auth_client, db_conn, test_user, test_account):
        _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, 4, test_account["name"])  # Friday
        resp = auth_client.get("/manage?tab=bills")
        body = resp.get_data(as_text=True)
        assert "Every Friday" in body

    def test_edit_row_prefills_weekly_day(self, auth_client, db_conn, test_user, test_account):
        bill_id = _add_weekly_bill(db_conn, test_user["id"], "Cleaner", 40.0, 3, test_account["name"])  # Thursday
        resp = auth_client.get("/manage?tab=bills")
        body = resp.get_data(as_text=True)
        assert f'edit-weekly-day-{bill_id}' in body
        assert '<option value="3" selected>Thursday</option>' in body
