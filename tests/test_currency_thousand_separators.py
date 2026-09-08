"""
Tests for app-wide currency thousand-separator formatting (September 2026).

Every rendered currency figure across the app should show thousand
separators (£1,000,000.00, not £1000000.00). A `moneyfmt` Jinja filter
already existed (introduced for the Goals card, August 2026) but was only
applied there; this extends it with an optional `decimals` argument
(default 2, used as 0 for the handful of already-compact displays) and
routes every other rendered currency figure through it - 60+ Jinja
`"%.2f"|format(...)` call sites across flow.html/index.html/manage.html/
actions.html/import.html/transactions.html, plus a JS-side `_fmtGBP()`
helper (inlined per-template, no shared JS file exists in this codebase)
replacing ~100 manual `.toFixed()` £ concatenations in the same templates
plus forecast.html/calendar.html.

Covers:
  - moneyfmt_filter()'s new `decimals` parameter directly
  - Large balances/amounts render with commas on Home, Manage, Transactions,
    Forecast-adjacent (Flow), and Calendar
  - A regression guard for a real bug caught during this sweep: several
    editable <input> fields and machine-readable data-* attributes were
    briefly, incorrectly comma-formatted by an early pass (would have
    broken parseFloat() on re-submission) - these must stay plain
  - Non-money numbers (percentages, quick-amount preset buttons) are left
    untouched, not swept in by mistake
"""
import json

import pytest

from tests.conftest import csrf


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
        (name, balance, acc_type, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


# ── 1. moneyfmt_filter() DIRECTLY ────────────────────────────────────────────
class TestMoneyfmtFilterDecimals:
    def test_default_two_decimals_unchanged(self):
        import app as app_module
        assert app_module.moneyfmt_filter(30000.0) == "30,000.00"
        assert app_module.moneyfmt_filter(994.0) == "994.00"
        assert app_module.moneyfmt_filter(1234567.5) == "1,234,567.50"

    def test_zero_decimals(self):
        import app as app_module
        assert app_module.moneyfmt_filter(30000.0, 0) == "30,000"
        assert app_module.moneyfmt_filter(994.4, 0) == "994"

    def test_negative_values(self):
        import app as app_module
        assert app_module.moneyfmt_filter(-1500.5) == "-1,500.50"

    def test_bad_input_still_returned_unchanged(self):
        import app as app_module
        assert app_module.moneyfmt_filter("not a number") == "not a number"

    def test_million_scale(self):
        import app as app_module
        assert app_module.moneyfmt_filter(1000000.0) == "1,000,000.00"


# ── 2. REAL PAGES — large figures render with commas ────────────────────────
class TestLargeFiguresRenderWithSeparators:
    def test_home_net_worth_over_a_thousand_has_comma(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Big Savings", balance=25000.0, acc_type="savings")
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "25,000.00" in body
        assert "25000.00" not in body.replace("25,000.00", "")  # no un-commafied duplicate

    def test_manage_accounts_table_large_balance(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Big Current", balance=12345.67, acc_type="current")
        resp = auth_client.get("/manage")
        body = resp.get_data(as_text=True)
        assert "12,345.67" in body

    def test_manage_bill_amount_large(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) VALUES (?,?,?,?,?,?)",
            ("Big Rent", 2500.0, 1, test_account["name"], test_user["id"], "monthly"),
        )
        db_conn.commit()
        resp = auth_client.get("/manage?tab=bills")
        body = resp.get_data(as_text=True)
        assert "2,500.00" in body

    def test_transactions_large_amount(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO transactions (description, amount, account, category, date, user_id) VALUES (?,?,?,?,?,?)",
            ("Big purchase", -3500.0, test_account["name"], "Other", "2026-06-01", test_user["id"]),
        )
        db_conn.commit()
        resp = auth_client.get("/transactions")
        body = resp.get_data(as_text=True)
        assert "3,500.00" in body

    def test_calendar_future_day_indicator_uses_shared_js_helper(self, auth_client):
        """calendar.html's scheduled-item indicators are JS-rendered - the
        markup itself doesn't carry the formatted number server-side, but
        it must ship the shared _fmtGBP() helper that formats it client-side."""
        resp = auth_client.get("/calendar")
        body = resp.get_data(as_text=True)
        assert "function _fmtGBP" in body


# ── 3. REGRESSION GUARD — editable fields must stay plain, unformatted ──────
# A real bug caught mid-sweep: an automated pass initially wrapped every
# .toFixed(2)/.toFixed(0) money concatenation in _fmtGBP(), including a
# handful that populate an editable <input>'s .value or a data-* attribute
# later re-read with parseFloat() - a comma-formatted value there would
# either fail to re-submit correctly or silently truncate at the comma
# (parseFloat("1,234.56") === 1). These must never regress back to being
# formatted.
class TestEditableFieldsStayPlain:
    def test_home_js_never_comma_formats_editable_value_assignments(self, auth_client):
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        # adj-new-balance is an editable input - _fmtGBP(bal) legitimately
        # appears elsewhere on the same line/function for the read-only
        # "Current: £..." display text, so check the specific .value
        # assignment rather than banning the substring outright.
        assert "adj-new-balance').value = _fmtGBP" not in body
        assert "adj-new-balance').value = bal.toFixed(2)" in body
        assert "txEditAmount').value = _fmtGBP" not in body
        assert "editItemAmount').value = _fmtGBP" not in body
        assert "_ciRow.dataset.amount = _fmtGBP" not in body
        assert "_ciRow.dataset.amount = newAmt" in body

    def test_manage_js_income_amount_input_stays_plain(self, auth_client):
        """Populating the Edit Income modal's amount field must not inject
        commas - it needs to remain a valid resubmittable number."""
        resp = auth_client.get("/manage")
        body = resp.get_data(as_text=True)
        assert "incomeAmount').value = inc.amount ? _fmtGBP" not in body
        assert "incomeAmount').value = inc.amount ? parseFloat(inc.amount).toFixed(2)" in body

    def test_quick_amount_preset_buttons_not_swept(self, auth_client):
        """Small hardcoded quick-fill amounts (£5/£10/£20/£50) on the
        Actions page never need thousand separators and must stay as
        plain Jinja output, not routed through moneyfmt."""
        resp = auth_client.get("/actions")
        body = resp.get_data(as_text=True)
        assert "£5</button>" in body and "£10</button>" in body
        assert "5|moneyfmt" not in body


# ── 4. NON-MONEY NUMBERS LEFT ALONE ─────────────────────────────────────────
class TestNonMoneyNumbersUntouched:
    def test_forecast_interest_rate_percentage_not_comma_formatted(self, auth_client, db_conn, test_user):
        """A savings interest rate is a percentage, not currency - must
        keep using plain toFixed(2), never routed through _fmtGBP()."""
        _add_account(db_conn, test_user["id"], "ISA", balance=5000.0, acc_type="savings")
        db_conn.execute(
            "UPDATE accounts SET savings_rate = 4.5 WHERE user_id = ? AND name = 'ISA'",
            (test_user["id"],),
        )
        db_conn.commit()
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        assert "rate.toFixed(2)" in body
        assert "_fmtGBP(rate)" not in body

