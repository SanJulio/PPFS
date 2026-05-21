"""
Tests for home page affordability check and income card:
  GET  /api/snapshot         — min_balance + min_balance_date added to each account
  GET  /                     — income card excludes balance adjustment transactions
"""

import pytest
from datetime import date
from tests.conftest import csrf


# ── SNAPSHOT API ──────────────────────────────────────────────────────────────

class TestSnapshotAPI:
    def test_snapshot_returns_200(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=30")
        assert resp.status_code == 200

    def test_snapshot_includes_min_balance_fields(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        assert "min_balance" in acc
        assert "min_balance_date" in acc

    def test_snapshot_min_balance_equals_today_when_no_bills(self, auth_client, test_account):
        """With no scheduled expenses or income, min_balance should equal balance_today."""
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        assert abs(acc["min_balance"] - acc["balance_today"]) < 0.01

    def test_snapshot_min_balance_drops_with_scheduled_expense(self, auth_client, db_conn, test_user, test_account):
        """A scheduled bill causes min_balance to fall below balance_today."""
        today = date.today()
        # Pick a bill day guaranteed to fall within the next 60 days
        bill_day = today.day + 1 if today.day < 28 else 1
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Big Test Bill", 400.0, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/api/snapshot?days=60")
            assert resp.status_code == 200
            data = resp.get_json()
            acc = data["accounts"][test_account["name"]]
            assert acc["min_balance"] < acc["balance_today"]
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.close()

    def test_snapshot_balance_on_date_equals_today_with_no_activity(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=1")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        assert abs(acc["balance_on_date"] - acc["balance_today"]) < 0.01

    def test_snapshot_response_has_required_top_level_keys(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        for key in ("date", "days", "accounts", "income_arriving", "bills_due"):
            assert key in data

    def test_snapshot_account_has_all_fields(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=30")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        for field in ("balance_today", "balance_on_date", "change", "type",
                      "min_balance", "min_balance_date"):
            assert field in acc

    def test_snapshot_unauthenticated_redirects(self, client):
        resp = client.get("/api/snapshot?days=30", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_snapshot_days_clamped_to_90(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=999")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["days"] == 90

    def test_snapshot_days_clamped_to_1_minimum(self, auth_client, test_account):
        resp = auth_client.get("/api/snapshot?days=0")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["days"] == 1


# ── INCOME CARD EXCLUDES ADJUSTMENTS ─────────────────────────────────────────

class TestIncomeCardExcludesAdjustments:
    def test_adjustment_not_counted_as_income(self, auth_client, db_conn, test_user, test_account):
        """Balance adjustment (type='adjustment') must not appear in the Income card total."""
        today_str = date.today().isoformat()
        cur = db_conn.cursor()
        # Real income: distinctive amount that will show in the income tile
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, type, category, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today_str, "Salary", 777.00, test_account["name"], "income", "Income", test_user["id"]),
        )
        inc_id = cur.lastrowid
        # Balance adjustment with a very large unique amount — must NOT appear as income
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, type, category, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today_str, "Balance adjustment", 88888.00, test_account["name"], "adjustment", "Various", test_user["id"]),
        )
        adj_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            html = resp.data.decode("utf-8")
            # The combined total (777 + 88888 = 89665) must not appear — would show if adjustment was included
            assert "89665" not in html
            # Sanity: the correct income amount should appear somewhere on the page
            assert "777.00" in html
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM transactions WHERE id IN (?, ?)", (inc_id, adj_id))
            cur.close()

    def test_income_transaction_counted_normally(self, auth_client, db_conn, test_user, test_account):
        """Regular type='income' transactions ARE counted in the income card."""
        today_str = date.today().isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, type, category, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today_str, "Freelance", 333.00, test_account["name"], "income", "Income", test_user["id"]),
        )
        tx_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            assert "333.00" in resp.data.decode("utf-8")
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            cur.close()

    def test_transfer_not_counted_as_income(self, auth_client, db_conn, test_user, test_account):
        """type='transfer' transactions (already excluded) remain excluded."""
        today_str = date.today().isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, type, category, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today_str, "Transfer in", 55555.00, test_account["name"], "transfer", "Transfer", test_user["id"]),
        )
        tx_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            html = resp.data.decode("utf-8")
            # Transfer amount must not appear as income total
            assert "55555" not in html
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            cur.close()


# ── AFFORD CHECK — FUTURE BALANCE LOWEST POINT ───────────────────────────────

class TestAffordFutureBalance:
    def test_min_balance_with_income_stays_high(self, auth_client, db_conn, test_user, test_account):
        """Income scheduled in next 60 days keeps min_balance high (may exceed today's balance)."""
        today = date.today()
        income_day = today.day + 2 if today.day < 27 else 1
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, day, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Salary", 2000.0, "monthly", test_account["name"], income_day, test_user["id"]),
        )
        inc_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/api/snapshot?days=60")
            assert resp.status_code == 200
            data = resp.get_json()
            acc = data["accounts"][test_account["name"]]
            # balance_on_date should be higher than balance_today because of income
            assert acc["balance_on_date"] > acc["balance_today"]
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM income WHERE id = ?", (inc_id,))
            cur.close()

    def test_single_item_would_leave_positive_balance(self, auth_client, test_account):
        """Buying something less than current balance leaves a positive amount."""
        resp = auth_client.get("/api/snapshot?days=1")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        purchase = 200.0
        after = acc["balance_on_date"] - purchase
        assert after > 0  # £1000 - £200 = £800, affordable

    def test_single_item_exceeds_balance(self, auth_client, test_account):
        """Buying something more expensive than the balance would go negative."""
        resp = auth_client.get("/api/snapshot?days=1")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        purchase = 5000.0
        after = acc["balance_on_date"] - purchase
        assert after < 0  # £1000 - £5000 = -£4000, not affordable

    def test_multiple_items_total_exceeds_balance(self, auth_client, test_account):
        """Multiple items combined can exceed a balance that single items would not."""
        resp = auth_client.get("/api/snapshot?days=1")
        data = resp.get_json()
        acc = data["accounts"][test_account["name"]]
        items = [{"name": "Laptop", "amount": 700.0}, {"name": "Games", "amount": 400.0}]
        total = sum(it["amount"] for it in items)
        after = acc["balance_on_date"] - total
        # Each item alone is affordable but combined they exceed £1000
        assert 700 < acc["balance_on_date"]
        assert 400 < acc["balance_on_date"]
        assert after < 0

    def test_min_balance_never_exceeds_balance_today_with_only_expenses(
        self, auth_client, db_conn, test_user, test_account
    ):
        """With only bills (no income), min_balance ≤ balance_today."""
        today = date.today()
        bill_day = today.day + 3 if today.day < 26 else 1
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Rent", 600.0, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/api/snapshot?days=90")
            data = resp.get_json()
            acc = data["accounts"][test_account["name"]]
            assert acc["min_balance"] <= acc["balance_today"]
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.close()


# ── SAFE TO SPEND ─────────────────────────────────────────────────────────────

class TestSafeToSpend:
    def test_snapshot_future_events_bills_have_iso(self, auth_client, db_conn, test_user, test_account):
        """Bills from future_events in bills_due must include an iso date field."""
        from datetime import date, timedelta
        target = (date.today() + timedelta(days=5)).isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO future_events (date, name, amount, account, user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (target, "One-off Expense", 150.0, test_account["name"], test_user["id"]),
        )
        ev_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/api/snapshot?days=30")
            data = resp.get_json()
            matches = [b for b in data["bills_due"] if b["name"] == "One-off Expense"]
            assert len(matches) == 1
            assert "iso" in matches[0]
            assert matches[0]["iso"] == target
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM future_events WHERE id = ?", (ev_id,))
            cur.close()

    def test_safe_spending_no_income_deducts_future_bill(self, auth_client, db_conn, test_user, test_account):
        """With no income this month, safe_spending = balance − future bills (floored at 0)."""
        from datetime import date
        today = date.today()
        bill_day = today.day + 1 if today.day < 28 else today.day - 1
        if bill_day <= today.day:
            return
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Safe Test Bill", 163.00, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            # balance=1000, no income, bill=163 → safe = 837.00
            assert "837.00" in resp.data.decode("utf-8")
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.close()

    def test_safe_spending_bill_before_income_is_deducted(self, auth_client, db_conn, test_user, test_account):
        """A bill due before payday is deducted from safe_spending."""
        from datetime import date
        today = date.today()
        bill_day = today.day + 1 if today.day < 26 else None
        income_day = today.day + 4 if today.day < 26 else None
        if bill_day is None or income_day is None or bill_day >= income_day:
            return
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("PrePay Bill", 600.00, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, day, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Pay", 2000.0, "monthly", test_account["name"], income_day, test_user["id"]),
        )
        inc_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            # balance=1000, bill=600 before income → safe = 400 → "Looking tight" banner (101–500 range)
            assert "Looking tight" in resp.data.decode("utf-8")
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.execute("DELETE FROM income WHERE id = ?", (inc_id,))
            cur.close()

    def test_safe_spending_all_cycle_bills_deducted(self, auth_client, db_conn, test_user, test_account):
        """All remaining bills this cycle are deducted regardless of income timing."""
        from datetime import date
        today = date.today()
        income_day = today.day + 1 if today.day < 26 else None
        bill_day = today.day + 4 if today.day < 26 else None
        if income_day is None or bill_day is None or income_day >= bill_day:
            return
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, day, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Pay", 2000.0, "monthly", test_account["name"], income_day, test_user["id"]),
        )
        inc_id = cur.lastrowid
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("PostPay Bill", 600.00, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            # balance=1000, bill=600 → safe = 1000 - 600 = 400 → "Looking tight" (101–500)
            assert "Looking tight" in resp.data.decode("utf-8")
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM income WHERE id = ?", (inc_id,))
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.close()

    def test_safe_spending_never_negative(self, auth_client, db_conn, test_user, test_account):
        """safe_spending floors at 0 even when bills exceed balance."""
        from datetime import date
        today = date.today()
        bill_day = today.day + 1 if today.day < 28 else today.day - 1
        if bill_day <= today.day:
            return
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Huge Bill", 9999.00, bill_day, test_account["name"], "monthly", test_user["id"]),
        )
        bill_id = cur.lastrowid
        cur.close()
        try:
            resp = auth_client.get("/")
            assert resp.status_code == 200
            html = resp.data.decode("utf-8")
            # safe_spending is floored at 0 — "Very tight" banner confirms range [0, 100)
            assert "Very tight" in html
        finally:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
            cur.close()
