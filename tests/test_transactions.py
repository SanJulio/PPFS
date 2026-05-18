"""
Tests for transaction-related routes:
  POST /add-expense
  POST /add-income
  POST /transfer
  POST /transactions/undo
  POST /transactions/delete
  POST /transactions/edit
"""

import pytest
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


def _latest_transaction(db_conn, user_id):
    return db_conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


# ── ADD EXPENSE ───────────────────────────────────────────────────────────────
class TestAddExpense:
    def test_expense_decreases_balance(self, auth_client, db_conn, test_user, test_account):
        before = _get_balance(db_conn, test_account["id"])

        auth_client.post(
            "/add-expense",
            data={
                **csrf(),
                "description": "Lunch",
                "amount": "12.50",
                "account": test_account["name"],
                "category": "Food",
            },
            follow_redirects=False,
        )

        db_conn.commit()  # force sqlite to see committed rows
        after = _get_balance(db_conn, test_account["id"])
        assert abs((before - after) - 12.50) < 0.01

    def test_expense_creates_transaction_record(self, auth_client, db_conn, test_user, test_account):
        before_count = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/add-expense",
            data={
                **csrf(),
                "description": "Coffee",
                "amount": "3.00",
                "account": test_account["name"],
                "category": "Food",
            },
        )

        after_count = _count_transactions(db_conn, test_user["id"])
        assert after_count == before_count + 1

        tx = _latest_transaction(db_conn, test_user["id"])
        assert tx["description"] == "Coffee"
        assert float(tx["amount"]) == -3.00

    def test_expense_amount_stored_negative(self, auth_client, db_conn, test_user, test_account):
        auth_client.post(
            "/add-expense",
            data={
                **csrf(),
                "description": "Books",
                "amount": "25.00",
                "account": test_account["name"],
            },
        )
        tx = _latest_transaction(db_conn, test_user["id"])
        assert float(tx["amount"]) < 0

    def test_expense_missing_description_redirects(self, auth_client, test_account):
        resp = auth_client.post(
            "/add-expense",
            data={
                **csrf(),
                "description": "",
                "amount": "10.00",
                "account": test_account["name"],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert b"Missing" in resp.data or "msg" in resp.location

    def test_expense_missing_amount_redirects(self, auth_client, test_account):
        resp = auth_client.post(
            "/add-expense",
            data={**csrf(), "description": "Something", "amount": "", "account": test_account["name"]},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_expense_invalid_amount_redirects(self, auth_client, test_account):
        resp = auth_client.post(
            "/add-expense",
            data={**csrf(), "description": "X", "amount": "abc", "account": test_account["name"]},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_expense_zero_amount_rejected(self, auth_client, db_conn, test_user, test_account):
        before_count = _count_transactions(db_conn, test_user["id"])
        auth_client.post(
            "/add-expense",
            data={**csrf(), "description": "X", "amount": "0", "account": test_account["name"]},
        )
        assert _count_transactions(db_conn, test_user["id"]) == before_count

    def test_expense_requires_auth(self, csrf_client):
        resp = csrf_client.post(
            "/add-expense",
            data={**csrf(), "description": "X", "amount": "5", "account": "Current"},
            follow_redirects=False,
        )
        # Unauthenticated → redirect to login
        assert resp.status_code in (302, 303)

    def test_expense_custom_date(self, auth_client, db_conn, test_user, test_account):
        auth_client.post(
            "/add-expense",
            data={
                **csrf(),
                "description": "Old purchase",
                "amount": "15.00",
                "account": test_account["name"],
                "date": "2026-01-15",
            },
        )
        tx = _latest_transaction(db_conn, test_user["id"])
        assert tx["date"] == "2026-01-15"


# ── ADD INCOME ────────────────────────────────────────────────────────────────
class TestAddIncome:
    def test_income_increases_balance(self, auth_client, db_conn, test_user, test_account):
        before = _get_balance(db_conn, test_account["id"])

        auth_client.post(
            "/add-income",
            data={
                **csrf(),
                "description": "Salary",
                "amount": "2000.00",
                "account": test_account["name"],
            },
        )

        after = _get_balance(db_conn, test_account["id"])
        assert abs((after - before) - 2000.00) < 0.01

    def test_income_amount_stored_positive(self, auth_client, db_conn, test_user, test_account):
        auth_client.post(
            "/add-income",
            data={
                **csrf(),
                "description": "Freelance",
                "amount": "500.00",
                "account": test_account["name"],
            },
        )
        tx = _latest_transaction(db_conn, test_user["id"])
        assert float(tx["amount"]) > 0

    def test_income_type_stored_as_income(self, auth_client, db_conn, test_user, test_account):
        auth_client.post(
            "/add-income",
            data={
                **csrf(),
                "description": "Bonus",
                "amount": "100.00",
                "account": test_account["name"],
            },
        )
        tx = _latest_transaction(db_conn, test_user["id"])
        assert tx["type"] == "income"

    def test_income_missing_fields_redirects(self, auth_client, test_account):
        resp = auth_client.post(
            "/add-income",
            data={**csrf(), "description": "", "amount": "", "account": ""},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)


# ── TRANSFER ──────────────────────────────────────────────────────────────────
class TestTransfer:
    def test_transfer_adjusts_both_balances(
        self, auth_client, db_conn, test_user, test_account, second_account
    ):
        before_from = _get_balance(db_conn, test_account["id"])
        before_to = _get_balance(db_conn, second_account["id"])

        auth_client.post(
            "/transfer",
            data={
                **csrf(),
                "from_account": test_account["name"],
                "to_account": second_account["name"],
                "amount": "200.00",
            },
        )

        after_from = _get_balance(db_conn, test_account["id"])
        after_to = _get_balance(db_conn, second_account["id"])

        assert abs((before_from - after_from) - 200.00) < 0.01
        assert abs((after_to - before_to) - 200.00) < 0.01

    def test_transfer_creates_two_transactions(
        self, auth_client, db_conn, test_user, test_account, second_account
    ):
        before = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/transfer",
            data={
                **csrf(),
                "from_account": test_account["name"],
                "to_account": second_account["name"],
                "amount": "50.00",
            },
        )

        assert _count_transactions(db_conn, test_user["id"]) == before + 2

    def test_transfer_same_account_rejected(
        self, auth_client, db_conn, test_user, test_account
    ):
        before = _get_balance(db_conn, test_account["id"])
        before_count = _count_transactions(db_conn, test_user["id"])

        resp = auth_client.post(
            "/transfer",
            data={
                **csrf(),
                "from_account": test_account["name"],
                "to_account": test_account["name"],
                "amount": "100.00",
            },
            follow_redirects=False,
        )

        assert resp.status_code in (302, 303)
        assert _get_balance(db_conn, test_account["id"]) == before
        assert _count_transactions(db_conn, test_user["id"]) == before_count

    def test_transfer_missing_amount(self, auth_client, test_account, second_account):
        resp = auth_client.post(
            "/transfer",
            data={
                **csrf(),
                "from_account": test_account["name"],
                "to_account": second_account["name"],
                "amount": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_transfer_transaction_types(
        self, auth_client, db_conn, test_user, test_account, second_account
    ):
        auth_client.post(
            "/transfer",
            data={
                **csrf(),
                "from_account": test_account["name"],
                "to_account": second_account["name"],
                "amount": "10.00",
            },
        )
        rows = db_conn.execute(
            "SELECT type FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 2",
            (test_user["id"],),
        ).fetchall()
        assert all(r["type"] == "transfer" for r in rows)


# ── UNDO TRANSACTION ──────────────────────────────────────────────────────────
class TestTransactionUndo:
    def _insert_tx(self, db_conn, user_id, account_name, amount):
        from datetime import date
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date.today().isoformat(), "Test tx", amount, account_name, user_id, "manual", "Other"),
        )
        db_conn.commit()
        tx_id = cur.lastrowid
        cur.close()
        return tx_id

    def test_undo_reverses_balance(self, auth_client, db_conn, test_user, test_account):
        # Insert a -50 transaction (expense) and then undo it
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -50.00)
        # Apply the expense delta manually
        db_conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE id = ?",
            (-50.00, test_account["id"]),
        )
        db_conn.commit()
        before_balance = _get_balance(db_conn, test_account["id"])

        auth_client.post(
            "/transactions/undo",
            data={**csrf(), "tx_id": str(tx_id)},
        )

        after_balance = _get_balance(db_conn, test_account["id"])
        # Undo adds back +50
        assert abs((after_balance - before_balance) - 50.00) < 0.01

    def test_undo_removes_transaction(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -30.00)
        before_count = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/transactions/undo",
            data={**csrf(), "tx_id": str(tx_id)},
        )

        assert _count_transactions(db_conn, test_user["id"]) == before_count - 1

    def test_undo_other_users_tx_fails(self, auth_client, db_conn, test_user, test_account):
        # Insert a transaction for a different user_id
        cur = db_conn.cursor()
        from datetime import date
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date.today().isoformat(), "Other user tx", -10.00, "Current", 99999, "manual", "Other"),
        )
        db_conn.commit()
        tx_id = cur.lastrowid
        cur.close()

        before_count = _count_transactions(db_conn, test_user["id"])
        auth_client.post(
            "/transactions/undo",
            data={**csrf(), "tx_id": str(tx_id)},
        )
        assert _count_transactions(db_conn, test_user["id"]) == before_count

        # Cleanup foreign tx
        db_conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        db_conn.commit()


# ── DELETE TRANSACTION ────────────────────────────────────────────────────────
class TestTransactionDelete:
    def _insert_tx(self, db_conn, user_id, account_name, amount):
        from datetime import date
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date.today().isoformat(), "Delete test", amount, account_name, user_id, "manual", "Other"),
        )
        db_conn.commit()
        tx_id = cur.lastrowid
        cur.close()
        return tx_id

    def test_delete_removes_record(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -20.00)
        before_count = _count_transactions(db_conn, test_user["id"])

        auth_client.post(
            "/transactions/delete",
            data={**csrf(), "tx_id": str(tx_id)},
        )

        assert _count_transactions(db_conn, test_user["id"]) == before_count - 1

    def test_delete_does_not_change_balance(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -20.00)
        before_balance = _get_balance(db_conn, test_account["id"])

        auth_client.post(
            "/transactions/delete",
            data={**csrf(), "tx_id": str(tx_id)},
        )

        assert _get_balance(db_conn, test_account["id"]) == before_balance


# ── EDIT TRANSACTION ──────────────────────────────────────────────────────────
class TestTransactionEdit:
    def _insert_tx(self, db_conn, user_id, account_name, amount):
        from datetime import date
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date.today().isoformat(), "Edit test", amount, account_name, user_id, "manual", "Other"),
        )
        db_conn.commit()
        tx_id = cur.lastrowid
        cur.close()
        return tx_id

    def test_edit_updates_amount_and_balance(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -40.00)
        db_conn.execute(
            "UPDATE accounts SET balance = balance - 40 WHERE id = ?", (test_account["id"],)
        )
        db_conn.commit()
        before_balance = _get_balance(db_conn, test_account["id"])

        # Change -40 to -30 (save £10)
        auth_client.post(
            "/transactions/edit",
            data={
                **csrf(),
                "tx_id": str(tx_id),
                "description": "Edited tx",
                "amount": "-30.00",
                "account": test_account["name"],
            },
        )

        after_balance = _get_balance(db_conn, test_account["id"])
        # diff = -30 - (-40) = +10 → balance increases by 10
        assert abs((after_balance - before_balance) - 10.00) < 0.01

    def test_edit_updates_description(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -10.00)

        auth_client.post(
            "/transactions/edit",
            data={
                **csrf(),
                "tx_id": str(tx_id),
                "description": "New description",
                "amount": "-10.00",
                "account": test_account["name"],
            },
        )

        tx = db_conn.execute(
            "SELECT description FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        assert tx["description"] == "New description"

    def test_edit_missing_fields_redirects(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -10.00)

        resp = auth_client.post(
            "/transactions/edit",
            data={**csrf(), "tx_id": str(tx_id), "description": "", "amount": "", "account": ""},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_edit_invalid_amount_redirects(self, auth_client, db_conn, test_user, test_account):
        tx_id = self._insert_tx(db_conn, test_user["id"], test_account["name"], -10.00)

        resp = auth_client.post(
            "/transactions/edit",
            data={
                **csrf(),
                "tx_id": str(tx_id),
                "description": "X",
                "amount": "not_a_number",
                "account": test_account["name"],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
