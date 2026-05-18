"""
Tests for AJAX/JSON API endpoints:
  POST /quick-add   — minimal expense/income from home FAB
  POST /quick-adjust — balance adjustment with delta logging

Also covers the models layer:
  update_account_balance()
  add_transaction()
  get_active_accounts()
  get_recent_transactions()
"""

import pytest
from datetime import date
from tests.conftest import csrf


def _get_balance(db_conn, account_id):
    row = db_conn.execute(
        "SELECT balance FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return float(row["balance"])


def _count_transactions(db_conn, user_id):
    row = db_conn.execute(
        "SELECT COUNT(*) as c FROM transactions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["c"]


# ── QUICK-ADD ─────────────────────────────────────────────────────────────────
class TestQuickAdd:
    def test_expense_decreases_balance(self, auth_client, db_conn, test_account):
        before = _get_balance(db_conn, test_account["id"])

        resp = auth_client.post(
            "/quick-add",
            data={
                **csrf(),
                "amount": "25.00",
                "description": "Fast food",
                "account": test_account["name"],
                "type": "expense",
                "category": "Food",
            },
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        after = _get_balance(db_conn, test_account["id"])
        assert abs((before - after) - 25.00) < 0.01

    def test_income_increases_balance(self, auth_client, db_conn, test_account):
        before = _get_balance(db_conn, test_account["id"])

        resp = auth_client.post(
            "/quick-add",
            data={
                **csrf(),
                "amount": "500.00",
                "description": "Freelance",
                "account": test_account["name"],
                "type": "income",
            },
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["type"] == "income"

        after = _get_balance(db_conn, test_account["id"])
        assert abs((after - before) - 500.00) < 0.01

    def test_missing_amount_returns_400(self, auth_client, test_account):
        resp = auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "", "account": test_account["name"]},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_missing_account_returns_400(self, auth_client):
        resp = auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "10.00", "account": ""},
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_zero_amount_returns_400(self, auth_client, test_account):
        resp = auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "0", "account": test_account["name"]},
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_negative_amount_returns_400(self, auth_client, test_account):
        resp = auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "-50", "account": test_account["name"]},
        )
        assert resp.status_code == 400

    def test_invalid_amount_returns_400(self, auth_client, test_account):
        resp = auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "abc", "account": test_account["name"]},
        )
        assert resp.status_code == 400

    def test_unauthenticated_redirects(self, csrf_client):
        resp = csrf_client.post(
            "/quick-add",
            data={**csrf(), "amount": "10", "account": "Current"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_response_includes_amount_and_account(self, auth_client, db_conn, test_account):
        resp = auth_client.post(
            "/quick-add",
            data={
                **csrf(),
                "amount": "7.50",
                "account": test_account["name"],
                "type": "expense",
            },
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert abs(data["amount"] - 7.50) < 0.01
        assert data["account"] == test_account["name"]

    def test_default_type_is_expense(self, auth_client, db_conn, test_user, test_account):
        before = _get_balance(db_conn, test_account["id"])

        auth_client.post(
            "/quick-add",
            data={
                **csrf(),
                "amount": "10.00",
                "account": test_account["name"],
                # no 'type' field
            },
        )

        after = _get_balance(db_conn, test_account["id"])
        assert after < before  # balance went down → treated as expense

    def test_creates_transaction_record(self, auth_client, db_conn, test_user, test_account):
        before = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/quick-add",
            data={**csrf(), "amount": "5.00", "account": test_account["name"]},
        )

        assert _count_transactions(db_conn, test_user["id"]) == before + 1


# ── QUICK-ADJUST ──────────────────────────────────────────────────────────────
class TestQuickAdjust:
    def test_adjust_sets_balance_to_new_value(self, auth_client, db_conn, test_account):
        resp = auth_client.post(
            "/quick-adjust",
            data={
                **csrf(),
                "account": test_account["name"],
                "new_balance": "1500.00",
                "old_balance": "1000.00",
            },
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        after = _get_balance(db_conn, test_account["id"])
        assert abs(after - 1500.00) < 0.01

    def test_adjust_logs_delta_as_transaction(self, auth_client, db_conn, test_user, test_account):
        before_count = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/quick-adjust",
            data={
                **csrf(),
                "account": test_account["name"],
                "new_balance": "800.00",
                "old_balance": "1000.00",
            },
        )

        assert _count_transactions(db_conn, test_user["id"]) == before_count + 1

    def test_adjust_invalid_new_balance_returns_400(self, auth_client, test_account):
        resp = auth_client.post(
            "/quick-adjust",
            data={
                **csrf(),
                "account": test_account["name"],
                "new_balance": "not_a_number",
                "old_balance": "1000.00",
            },
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_adjust_missing_account_returns_400(self, auth_client):
        resp = auth_client.post(
            "/quick-adjust",
            data={
                **csrf(),
                "account": "",
                "new_balance": "800.00",
                "old_balance": "1000.00",
            },
        )
        assert resp.status_code == 400

    def test_adjust_unauthenticated_redirects(self, csrf_client):
        resp = csrf_client.post(
            "/quick-adjust",
            data={**csrf(), "account": "Current", "new_balance": "500", "old_balance": "1000"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)


# ── MODEL LAYER ───────────────────────────────────────────────────────────────
class TestModels:
    def test_update_account_balance_adds_delta(self, app, db_conn, test_user, test_account):
        from models import update_account_balance

        before = _get_balance(db_conn, test_account["id"])
        with app.app_context():
            update_account_balance(test_account["name"], 100.0, test_user["id"])

        after = _get_balance(db_conn, test_account["id"])
        assert abs((after - before) - 100.0) < 0.01

    def test_update_account_balance_subtracts_negative_delta(self, app, db_conn, test_user, test_account):
        from models import update_account_balance

        before = _get_balance(db_conn, test_account["id"])
        with app.app_context():
            update_account_balance(test_account["name"], -50.0, test_user["id"])

        after = _get_balance(db_conn, test_account["id"])
        assert abs((before - after) - 50.0) < 0.01

    def test_add_transaction_inserts_row(self, app, db_conn, test_user, test_account):
        from models import add_transaction

        before = _count_transactions(db_conn, test_user["id"])
        with app.app_context():
            add_transaction(
                date.today().isoformat(),
                "Model test tx",
                -20.0,
                test_account["name"],
                test_user["id"],
                type="manual",
                category="Other",
            )

        assert _count_transactions(db_conn, test_user["id"]) == before + 1

    def test_get_active_accounts_returns_only_active(self, app, db_conn, test_user, test_account):
        from models import get_active_accounts

        # Insert an inactive account
        db_conn.execute(
            "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Inactive Acct", 100.0, "savings", 0, test_user["id"], 1),
        )
        db_conn.commit()

        with app.app_context():
            accounts = get_active_accounts(test_user["id"])

        names = [a["name"] for a in accounts]
        assert "Inactive Acct" not in names
        assert test_account["name"] in names

        db_conn.execute("DELETE FROM accounts WHERE name = 'Inactive Acct' AND user_id = ?", (test_user["id"],))
        db_conn.commit()

    def test_get_recent_transactions_newest_first(self, app, db_conn, test_user, test_account):
        from models import add_transaction, get_recent_transactions

        with app.app_context():
            add_transaction("2026-01-01", "Old tx", -10.0, test_account["name"], test_user["id"])
            add_transaction("2026-06-01", "New tx", -20.0, test_account["name"], test_user["id"])
            rows = get_recent_transactions(test_user["id"])

        dates = [r["date"] for r in rows]
        # Newest first
        assert dates[0] >= dates[-1]

    def test_get_recent_transactions_only_for_user(self, app, db_conn, test_user, test_account):
        from models import add_transaction, get_recent_transactions

        # Insert tx for a different user
        db_conn.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-01", "Other user tx", -5.0, "Current", 99998, "manual", "Other"),
        )
        db_conn.commit()

        with app.app_context():
            rows = get_recent_transactions(test_user["id"])

        user_ids = set(r.get("user_id", test_user["id"]) for r in rows)
        # All rows should belong to this user (get_recent_transactions filters by user_id)
        for row in rows:
            assert row.get("description") != "Other user tx"

        db_conn.execute("DELETE FROM transactions WHERE user_id = 99998")
        db_conn.commit()


# ── CSRF ENFORCEMENT ──────────────────────────────────────────────────────────
class TestCSRF:
    def test_post_without_csrf_returns_403(self, auth_client):
        resp = auth_client.post(
            "/add-expense",
            data={"description": "X", "amount": "10", "account": "Current"},
            # no csrf_token field
        )
        assert resp.status_code == 403

    def test_post_with_wrong_csrf_returns_403(self, auth_client, test_account):
        resp = auth_client.post(
            "/add-expense",
            data={
                "csrf_token": "totally-wrong-token",
                "description": "X",
                "amount": "10",
                "account": test_account["name"],
            },
        )
        assert resp.status_code == 403

    def test_login_exempt_from_csrf(self, client):
        resp = client.post(
            "/login",
            data={"email": "x@x.com", "password": "wrong"},
        )
        # Should get 200 (error message) not 403
        assert resp.status_code == 200

    def test_register_exempt_from_csrf(self, client):
        resp = client.post(
            "/register",
            data={
                "display_name": "",
                "email": "bad",
                "password": "",
                "confirm": "",
            },
        )
        assert resp.status_code != 403
