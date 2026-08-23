"""
Tests for the "Bills Left vs Safe to Spend" labelling fix (August 2026).

Root cause (confirmed by reproduction before any code changed): the Bills
Left headline (and the "Bills remaining"/"End of cycle" figures inside the
Safe to Spend breakdown) summed EVERY future bill/event regardless of which
account it was linked to, while Safe to Spend itself already correctly
summed spending-account-linked items only. A bill/event linked to a savings
account was never going to touch spendable money, so lumping it into the
Bills Left headline made the screen look self-contradictory (a large Bills
Left figure next to a much larger Safe to Spend) even though the underlying
maths for Safe to Spend was already correct.

This is a display/labelling fix, NOT a calculation fix: safe_spending,
net_worth, and savings_balance are untouched. The fix:
  - calculate_financial_overview() now also returns `future_bills_spending`
    (spending-account-linked bills/events only - the same figure Safe to
    Spend was already privately computing as `spending_future_bills`).
  - Every future_bills_list item now carries `account_type` so the UI can
    tell which group it belongs to.
  - The Bills Left headline, the Safe-to-Spend breakdown's "Bills
    remaining" row, and "End of cycle" all now read future_bills_spending
    instead of the all-inclusive future_bills.
  - Both the inline tile-bills dropdown and the bigger bottom-sheet
    breakdown modal group items into "Reducing Safe to Spend" (spending-
    linked) and "Covered by savings" (savings-linked) sections, showing
    the linked account name on every row.
"""
import datetime
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


def _add_bill(db_conn, user_id, name, amount, day, account, frequency="monthly"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) VALUES (?,?,?,?,?,?)",
        (name, amount, day, account, user_id, frequency),
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


def _add_savings_rule(db_conn, user_id, name, amount, day, from_account, to_account):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (?,?,?,?,?,?,?)",
        (name, amount, day, "monthly", from_account, to_account, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


def _headline_val(body):
    m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', body)
    assert m, "future-bills-val not found"
    return float(m.group(1).replace(",", ""))


def _safe_to_spend_val(body):
    m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', body)
    assert m, "safe-spending-val not found"
    return float(m.group(1).replace(",", ""))


def _bills_remaining_val(body):
    m = re.search(r'id="safe-bills-remaining-val"[^>]*>−£([\d,\.]+)', body)
    assert m, "safe-bills-remaining-val not found"
    return float(m.group(1).replace(",", ""))


def _eoc_val(body):
    m = re.search(r'id="safe-eoc-val"[^>]*>£(-?[\d,\.]+)', body)
    assert m, "safe-eoc-val not found"
    return float(m.group(1).replace(",", ""))


# ── 1. HEADLINE REFLECTS SPENDING-LINKED ITEMS ONLY ──────────────────────────
class TestBillsLeftHeadlineIsSpendingOnly:
    def test_reproduction_scenario_from_the_bug_report(self, auth_client, db_conn, test_user):
        """The exact scenario that produced the reported £9,788.64 figure:
        a modest current-account balance, ordinary spending-linked bills,
        and a large savings-linked bill + event. Confirms the headline is
        now the spending-only total, not the all-inclusive one."""
        _add_account(db_conn, test_user["id"], "Current", 400.0, "current")
        _add_account(db_conn, test_user["id"], "ISA", 12000.0, "savings")
        _add_bill(db_conn, test_user["id"], "Rent", 900.0, min(TODAY.day + 3, 28), "Current")
        _add_bill(db_conn, test_user["id"], "Utilities", 150.0, min(TODAY.day + 4, 28), "Current")
        _add_bill(db_conn, test_user["id"], "ISA top-up", 5000.0, min(TODAY.day + 5, 28), "ISA")
        _add_event(db_conn, test_user["id"], "ISA lump sum transfer", 3738.64, _iso(6), "ISA")

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)

        assert _headline_val(body) == pytest.approx(1050.0)  # Rent + Utilities only
        assert _bills_remaining_val(body) == pytest.approx(1050.0)
        assert _eoc_val(body) == pytest.approx(400.0 - 1050.0)

    def test_bill_on_savings_account_excluded_from_headline(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "ISA contribution", 200.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert _headline_val(body) == pytest.approx(0.0)

    def test_event_on_savings_account_excluded_from_headline(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_event(db_conn, test_user["id"], "Withdrawal plan", 150.0, _iso(5), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert _headline_val(body) == pytest.approx(0.0)

    def test_savings_rule_from_savings_account_excluded_from_headline(self, auth_client, db_conn, test_user, test_account, second_account):
        """A savings_rule's from_account is normally a spending account, but
        the code doesn't prevent a savings->savings sweep - the same
        exclusion must apply if it ever is one."""
        third_id = _add_account(db_conn, test_user["id"], "Cash ISA", 1000.0, "savings")
        _add_savings_rule(db_conn, test_user["id"], "ISA sweep", 50.0, min(TODAY.day + 3, 28), second_account["name"], "Cash ISA")
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert _headline_val(body) == pytest.approx(0.0)

    def test_mixed_spending_and_savings_items_headline_is_spending_sum(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "Phone", 30.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_event(db_conn, test_user["id"], "Car service", 200.0, _iso(4), test_account["name"])
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert _headline_val(body) == pytest.approx(230.0)

    def test_safe_to_spend_and_headline_stay_consistent(self, auth_client, db_conn, test_user, test_account, second_account):
        """The core symptom from the report: Bills Left should never look
        bigger than what Safe to Spend implies is being deducted - both
        figures must now be derived from the same spending-only total."""
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_event(db_conn, test_user["id"], "Big savings transfer", 50000.0, _iso(3), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        headline = _headline_val(body)
        safe_to_spend = _safe_to_spend_val(body)
        # Safe to spend = balance(1000) - headline(300) = 700, never negative
        # or absurdly small because of the £50,000 savings-linked event.
        assert headline == pytest.approx(300.0)
        assert safe_to_spend == pytest.approx(700.0)


# ── 2. CALCULATIONS THEMSELVES ARE UNCHANGED (explicit non-regression) ──────
class TestCalculationsUntouched:
    def test_safe_spending_unchanged_by_this_fix(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        # Safe to spend was already correctly spending-only before this fix
        assert _safe_to_spend_val(body) == pytest.approx(test_account["balance"] - 300.0)

    def test_net_worth_unchanged_by_this_fix(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_event(db_conn, test_user["id"], "ISA transfer", 999.0, _iso(3), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        # Net worth is a live-balance figure untouched by future items -
        # confirm it still equals the raw sum of current account balances.
        m = re.search(r'"small">Net Worth</div>.*?fs-3 fw-bold amount-private">£([\d,\.]+)', body, re.DOTALL)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"] + second_account["balance"])

    def test_savings_balance_unchanged_by_this_fix(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "ISA contribution", 200.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        m = re.search(r'text-muted small">Savings</div>.*?fs-4 fw-bold amount-private">£([\d,\.]+)', body, re.DOTALL)
        assert m
        assert float(m.group(1).replace(",", "")) == pytest.approx(second_account["balance"])


# ── 3. BREAKDOWN GROUPING AND ACCOUNT-NAME DISPLAY ───────────────────────────
def _tile_bills_section(body):
    """Isolate the server-rendered #tile-bills div's content only - the
    page's own <script> block also contains these same header strings as
    JS template literals (for the AJAX re-render path), so a plain
    substring search against the whole body would false-positive on that
    JS source rather than the actually-rendered markup."""
    start = body.find('id="tile-bills"')
    assert start != -1, "#tile-bills not found"
    end = body.find('<!-- Row 2', start)
    if end == -1:
        end = start + 4000
    return body[start:end]


class TestBreakdownGrouping:
    def test_both_sections_shown_when_mixed(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        section = _tile_bills_section(resp.get_data(as_text=True))
        assert "Reducing Safe to Spend" in section
        assert "Covered by savings" in section

    def test_only_reducing_section_when_no_savings_items(self, auth_client, db_conn, test_user, test_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        resp = auth_client.get("/")
        section = _tile_bills_section(resp.get_data(as_text=True))
        assert "Reducing Safe to Spend" in section
        assert "Covered by savings" not in section

    def test_only_covered_section_when_no_spending_items(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        section = _tile_bills_section(body)
        assert "Reducing Safe to Spend" not in section
        assert "Covered by savings" in section
        assert _headline_val(body) == pytest.approx(0.0)

    def test_account_name_shown_for_spending_linked_bill(self, auth_client, db_conn, test_user, test_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        resp = auth_client.get("/")
        section = _tile_bills_section(resp.get_data(as_text=True))
        idx = section.find("Rent")
        assert test_account["name"] in section[idx:idx + 200]

    def test_account_name_shown_for_savings_linked_bill(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        section = _tile_bills_section(resp.get_data(as_text=True))
        idx = section.find("ISA contribution")
        assert second_account["name"] in section[idx:idx + 200]

    def test_no_items_shows_empty_state(self, auth_client, test_account):
        resp = auth_client.get("/")
        section = _tile_bills_section(resp.get_data(as_text=True))
        assert "No bills remaining this month" in section
        assert "Reducing Safe to Spend" not in section
        assert "Covered by savings" not in section


# ── 4. DATA EXPOSED TO CONSUMERS (JSON / AJAX path) ──────────────────────────
class TestDataExposedForConsumers:
    def test_future_bills_list_items_carry_account_type(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        import html, json
        m = re.search(r'<script type="application/json" id="ov-init-data">(.*?)</script>', body, re.DOTALL)
        assert m
        data = json.loads(html.unescape(m.group(1)))
        by_name = {b["name"]: b for b in data["futureBillsList"]}
        assert by_name["Rent"]["account_type"] == "current"
        assert by_name["ISA contribution"]["account_type"] == "savings"

    def test_api_overview_exposes_future_bills_spending(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "Rent", 300.0, min(TODAY.day + 2, 28), test_account["name"])
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        start = _iso(0)
        end = _iso(30)
        resp = auth_client.get(f"/api/overview?start={start}&end={end}")
        data = resp.get_json()
        assert "future_bills_spending" in data
        assert data["future_bills_spending"] == pytest.approx(300.0)
        assert data["future_bills"] == pytest.approx(800.0)  # all-inclusive, unchanged

    def test_api_overview_bills_list_items_carry_account_type(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_bill(db_conn, test_user["id"], "ISA contribution", 500.0, min(TODAY.day + 3, 28), second_account["name"])
        start = _iso(0)
        end = _iso(30)
        resp = auth_client.get(f"/api/overview?start={start}&end={end}")
        data = resp.get_json()
        item = next(b for b in data["future_bills_list"] if b["name"] == "ISA contribution")
        assert item["account_type"] == "savings"
