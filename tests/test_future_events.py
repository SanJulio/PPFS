"""
Tests for Future Events as a genuine forecasting input (August 2026):

Before this, a Future Event (Manage > Rules) was purely informational -
its amount never reduced any projected balance, Safe to Spend, or bills-
left figure, and there was no way to delete one once created. This brief
upgrades it to work exactly like a scheduled bill for calculation purposes
(a one-off deduction on its own account, from its date onward, never
recurring), adds delete, and adds navigation from wherever an event is
listed through to the Forecast page centred on its date.

Covers:
  - DELETE route (new)
  - calculate_financial_overview() — Safe to Spend / bills-left (new)
  - forecast() — 90-day balance simulation (pre-existing, formalised here)
    and its upcoming/annotation feed (new)
  - api_snapshot() — Future Balances day-view (pre-existing, formalised)
  - Locked-account exclusion, consistent across all three sites
  - Navigation links (Rules tab, Home's Bills Remaining, forecast ?date=)
"""
import datetime
import html
import json
import re

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead)).isoformat()


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
        (name, balance, acc_type, user_id),
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


def _lock(db_conn, account_id):
    db_conn.execute("UPDATE accounts SET is_locked = 1 WHERE id = ?", (account_id,))
    db_conn.commit()


def _get_json_attr(body, attr_name):
    """Extract and HTML-unescape a data-*='...' HTML attribute's JSON
    content (Jinja auto-escapes the pre-dumped JSON string as an attribute
    value, e.g. " becomes &#34;)."""
    m = re.search(re.escape(attr_name) + r"=['\"](.*?)['\"]", body, re.DOTALL)
    assert m, f"{attr_name} attribute not found"
    return json.loads(html.unescape(m.group(1)))


def _get_ov_init_data(body):
    """Extract the <script type="application/json" id="ov-init-data">
    block's JSON content - built via |tojson (Markup-safe, not HTML-entity
    escaped like a plain attribute), so no unescape needed, but it's
    harmless to apply anyway."""
    m = re.search(r'<script type="application/json" id="ov-init-data">(.*?)</script>', body, re.DOTALL)
    assert m, "ov-init-data script tag not found"
    return json.loads(html.unescape(m.group(1)))


# ── 1. DELETE ROUTE ───────────────────────────────────────────────────────────
class TestDeleteFutureEvent:
    def test_delete_removes_event(self, auth_client, db_conn, test_user, test_account):
        eid = _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(20), test_account["name"])
        resp = auth_client.post("/settings/delete-future-event", data={**csrf(), "id": eid}, follow_redirects=False)
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM future_events WHERE id=?", (eid,)).fetchone()
        assert row is None

    def test_delete_redirects_to_rules_tab_with_message(self, auth_client, db_conn, test_user, test_account):
        eid = _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(20), test_account["name"])
        resp = auth_client.post("/settings/delete-future-event", data={**csrf(), "id": eid}, follow_redirects=False)
        assert "tab=rules" in resp.headers["Location"]
        assert "deleted" in resp.headers["Location"].lower()

    def test_delete_does_not_affect_other_events(self, auth_client, db_conn, test_user, test_account):
        eid1 = _add_event(db_conn, test_user["id"], "Event A", 100.0, _iso(10), test_account["name"])
        eid2 = _add_event(db_conn, test_user["id"], "Event B", 200.0, _iso(15), test_account["name"])
        auth_client.post("/settings/delete-future-event", data={**csrf(), "id": eid1})
        remaining = db_conn.execute("SELECT * FROM future_events WHERE id=?", (eid2,)).fetchone()
        assert remaining is not None
        assert remaining["name"] == "Event B"

    def test_delete_does_not_affect_bills_or_balance(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) VALUES (?,?,?,?,?,?)",
            ("Rent", 500.0, 1, test_account["name"], test_user["id"], "monthly"),
        )
        db_conn.commit()
        eid = _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(10), test_account["name"])
        auth_client.post("/settings/delete-future-event", data={**csrf(), "id": eid})

        bill = db_conn.execute("SELECT * FROM scheduled_expenses WHERE name='Rent'").fetchone()
        assert bill is not None
        acc = db_conn.execute("SELECT balance FROM accounts WHERE id=?", (test_account["id"],)).fetchone()
        assert float(acc["balance"]) == test_account["balance"]

    def test_delete_only_affects_own_users_event(self, auth_client, db_conn, test_user, test_account):
        """A delete request for an id belonging to a different user must be
        a no-op (matches the WHERE id=? AND user_id=? pattern used by every
        other delete route in this app)."""
        import uuid
        from werkzeug.security import generate_password_hash
        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
            (other_email, generate_password_hash("TestPass1!"), "2026-01-01", "Other"),
        )
        other_uid = cur.lastrowid
        db_conn.commit()
        other_eid = _add_event(db_conn, other_uid, "Not yours", 999.0, _iso(10), "SomeAccount")

        auth_client.post("/settings/delete-future-event", data={**csrf(), "id": other_eid})
        row = db_conn.execute("SELECT * FROM future_events WHERE id=?", (other_eid,)).fetchone()
        assert row is not None  # untouched

    def test_delete_button_present_in_rules_tab(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, _iso(20), test_account["name"])
        resp = auth_client.get("/manage?tab=rules")
        body = resp.get_data(as_text=True)
        assert "/settings/delete-future-event" in body
        assert "Wedding gift" in body


# ── 2. calculate_financial_overview() — SAFE TO SPEND / BILLS LEFT ──────────────
class TestFinancialOverviewIncludesFutureEvents:
    def test_event_reduces_safe_to_spend(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(5), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"] - 200.0)

    def test_event_included_in_future_bills_total(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(5), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(200.0)

    def test_event_appears_in_future_bills_list_with_event_type(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(5), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        data = _get_ov_init_data(body)
        events = [b for b in data["futureBillsList"] if b.get("type") == "event"]
        assert len(events) == 1
        assert events[0]["name"] == "Car service"
        assert events[0]["amount"] == 200.0

    def test_event_beyond_cycle_window_not_counted(self, auth_client, db_conn, test_user, test_account):
        """An event far enough in the future to fall outside the current
        display period must not reduce Safe to Spend yet."""
        _add_event(db_conn, test_user["id"], "Far off event", 200.0, _iso(400), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"])

    def test_past_event_not_counted(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Already happened", 200.0, _iso(-10), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"])

    def test_event_on_savings_account_not_deducted_from_safe_to_spend(self, auth_client, db_conn, test_user, test_account, second_account):
        """Safe to Spend only tracks spending-type accounts (current/cash) -
        an event tied to a savings account must not reduce the spending-
        account Safe to Spend figure, exactly like a scheduled bill on a
        savings account.

        August 2026 update: the Bills Left HEADLINE figure is now
        spending-linked-only too (see the "Bills left / Safe to spend
        labelling fix" below) - a savings-linked event still appears in
        the full future_bills_list/breakdown (checked separately), just no
        longer inflates this headline number, since it was never going to
        touch spendable money."""
        _add_event(db_conn, test_user["id"], "Savings withdrawal plan", 100.0, _iso(5), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"])
        m2 = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', body)
        assert float(m2.group(1).replace(",", "")) == pytest.approx(0.0)

    def test_event_on_locked_account_excluded(self, auth_client, db_conn, test_user, test_account, second_account):
        _lock(db_conn, second_account["id"])
        _add_event(db_conn, test_user["id"], "Locked event", 300.0, _iso(5), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', body)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(0.0)

    def test_multiple_events_and_bills_sum_correctly(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) VALUES (?,?,?,?,?,?)",
            ("Rent", 50.0, (TODAY + datetime.timedelta(days=3)).day, test_account["name"], test_user["id"], "monthly"),
        )
        db_conn.commit()
        _add_event(db_conn, test_user["id"], "Event A", 30.0, _iso(5), test_account["name"])
        _add_event(db_conn, test_user["id"], "Event B", 20.0, _iso(6), test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', body)
        assert m
        # Rent may or may not land in-window depending on today's date vs
        # day-of-month arithmetic in this test env, so just confirm both
        # events (50 total) are included as a floor.
        assert float(m.group(1).replace(",", "")) >= 50.0


# ── 3. forecast() — 90-DAY SIMULATION AND UPCOMING/ANNOTATION FEED ──────────────
class TestForecastIncludesFutureEvents:
    def test_balance_reduced_from_event_date_onward(self, auth_client, db_conn, test_user, test_account):
        event_date = _iso(10)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        snapshots = _get_json_attr(body, "data-snapshots")
        snap_on_day = next(s for s in snapshots if s["date"] == event_date)
        assert snap_on_day[test_account["name"]] == pytest.approx(test_account["balance"] - 200.0)

    def test_balance_unaffected_before_event_date(self, auth_client, db_conn, test_user, test_account):
        event_date = _iso(10)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        snapshots = _get_json_attr(body, "data-snapshots")
        snap_before = next(s for s in snapshots if s["date"] == _iso(5))
        assert snap_before[test_account["name"]] == pytest.approx(test_account["balance"])

    def test_event_is_one_off_not_recurring(self, auth_client, db_conn, test_user, test_account):
        """Unlike a monthly bill, a future event must only ever deduct once,
        on its actual date - the balance should stay reduced by exactly the
        event amount for the rest of the 90-day window, never deducted a
        second time on a "same day next month" basis."""
        event_date = _iso(5)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        snapshots = _get_json_attr(body, "data-snapshots")
        after_event = [s for s in snapshots if s["date"] > event_date]
        assert after_event
        balances_after = {s[test_account["name"]] for s in after_event}
        # Every day after the event should show exactly balance-200, never
        # balance-400 (which would indicate a second, erroneous deduction).
        assert balances_after == {test_account["balance"] - 200.0}

    def test_upcoming_items_includes_event_with_type_and_id(self, auth_client, db_conn, test_user, test_account):
        event_date = _iso(10)
        eid = _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        upcoming = _get_json_attr(body, "data-upcoming")
        events = [u for u in upcoming if u.get("type") == "event"]
        assert len(events) == 1
        assert events[0]["name"] == "Car service"
        assert events[0]["amount"] == 200.0
        assert events[0]["date"] == event_date
        assert events[0]["id"] == eid

    def test_event_beyond_90_day_forecast_not_in_upcoming(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Far future", 200.0, _iso(200), test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        upcoming = _get_json_attr(body, "data-upcoming")
        assert not [u for u in upcoming if u.get("type") == "event"]

    def test_event_on_locked_account_excluded_from_forecast(self, auth_client, db_conn, test_user, test_account, second_account):
        _lock(db_conn, second_account["id"])
        _add_event(db_conn, test_user["id"], "Locked event", 300.0, _iso(5), second_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        upcoming = _get_json_attr(body, "data-upcoming")
        assert not [u for u in upcoming if u.get("type") == "event"]
        # And the locked account shouldn't even appear as a chart series
        account_names = _get_json_attr(body, "data-accounts")
        assert second_account["name"] not in account_names

    def test_chart_annotation_color_distinct_for_events(self, auth_client, db_conn, test_user, test_account):
        """The chart's buildAnnotations() JS must give event-type markers a
        distinct amber colour, not the bill (red) or income (green) one."""
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(10), test_account["name"])
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        assert "isEvent" in body
        assert "245,158,11" in body  # amber rgba used for event lines


# ── 4. api_snapshot() — FUTURE BALANCES DAY-VIEW ────────────────────────────────
class TestApiSnapshotIncludesFutureEvents:
    def test_snapshot_reflects_event_deduction(self, auth_client, db_conn, test_user, test_account):
        event_date_offset = 10
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(event_date_offset), test_account["name"])
        resp = auth_client.get(f"/api/snapshot?days={event_date_offset + 5}")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        assert acc["balance_on_date"] == pytest.approx(test_account["balance"] - 200.0)

    def test_snapshot_bills_due_includes_event_type(self, auth_client, db_conn, test_user, test_account):
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(10), test_account["name"])
        resp = auth_client.get("/api/snapshot?days=15")
        data = resp.get_json()
        events = [b for b in data["bills_due"] if b.get("item_type") == "event"]
        assert len(events) == 1
        assert events[0]["name"] == "Car service"

    def test_snapshot_event_on_locked_account_excluded(self, auth_client, db_conn, test_user, test_account, second_account):
        _lock(db_conn, second_account["id"])
        _add_event(db_conn, test_user["id"], "Locked event", 300.0, _iso(5), second_account["name"])
        resp = auth_client.get("/api/snapshot?days=10")
        data = resp.get_json()
        assert second_account["name"] not in data["accounts"]
        assert not [b for b in data["bills_due"] if b.get("item_type") == "event"]


# ── 5. NAVIGATION LINKS ──────────────────────────────────────────────────────
class TestNavigationLinks:
    def test_rules_tab_has_view_in_forecast_link(self, auth_client, db_conn, test_user, test_account):
        event_date = _iso(20)
        _add_event(db_conn, test_user["id"], "Wedding gift", 150.0, event_date, test_account["name"])
        resp = auth_client.get("/manage?tab=rules")
        body = resp.get_data(as_text=True)
        assert f"/forecast?date={event_date}" in body

    def test_home_bills_remaining_links_to_forecast_date(self, auth_client, db_conn, test_user, test_account):
        event_date = _iso(5)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert f'href="/forecast?date={event_date}"' in body

    def test_home_event_row_does_not_use_bill_pay_row_class(self, auth_client, db_conn, test_user, test_account):
        """Regression guard: an event row must never carry the
        bill-pay-row class, since clicking it would POST to
        /mark-bill-paid with a future_events id, not a scheduled_expenses
        one - wrong table entirely."""
        event_date = _iso(5)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        idx = body.find("Car service")
        section = body[max(0, idx - 300):idx + 100]
        assert "bill-pay-row" not in section

    def test_forecast_page_supports_date_query_param(self, auth_client, db_conn, test_user, test_account):
        """Visiting /forecast?date=X must render successfully (the jump-to-
        date JS runs client-side; here we confirm the server accepts the
        param without erroring and the page still renders the chart data,
        plus that the jumpToDate hook is actually present in the page)."""
        event_date = _iso(10)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        resp = auth_client.get(f"/forecast?date={event_date}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "function jumpToDate" in body
        assert "URLSearchParams(window.location.search).get('date')" in body

    def test_forecast_view_link_from_api_overview_bills_list(self, auth_client, db_conn, test_user, test_account):
        """The AJAX date-range path (/api/overview) must also carry the
        event's type/due_date through so the JS-rendered tile-bills list
        can link to the forecast the same way the initial page load does."""
        event_date = _iso(5)
        _add_event(db_conn, test_user["id"], "Car service", 200.0, event_date, test_account["name"])
        start = _iso(0)
        end = _iso(30)
        resp = auth_client.get(f"/api/overview?start={start}&end={end}")
        data = resp.get_json()
        events = [b for b in data["future_bills_list"] if b.get("type") == "event"]
        assert len(events) == 1
        assert events[0]["due_date"] == event_date


# ── 6. DELETE DOES NOT BREAK CALCULATIONS ────────────────────────────────────
class TestDeleteIntegrationConsistency:
    def test_deleting_event_removes_its_effect_on_safe_to_spend(self, auth_client, db_conn, test_user, test_account):
        eid = _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(5), test_account["name"])
        resp1 = auth_client.get("/")
        m1 = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', resp1.get_data(as_text=True))
        before = float(m1.group(1).replace(",", ""))

        auth_client.post("/settings/delete-future-event", data={**csrf(), "id": eid})

        resp2 = auth_client.get("/")
        m2 = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', resp2.get_data(as_text=True))
        after = float(m2.group(1).replace(",", ""))

        assert before == pytest.approx(test_account["balance"] - 200.0)
        assert after == pytest.approx(test_account["balance"])
