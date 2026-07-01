"""Tests for the 'Your Setup' card — get_setup_state() and POST /setup/dismiss."""

import pytest

CSRF = "test-csrf-fixed-token"


def _insert_account(db_conn, user_id, balance=1000.0, is_seeded=0, user_verified=0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, include_in_overview, user_id, is_seeded, user_verified) "
        "VALUES (?, ?, 'current', 1, 1, ?, ?, ?)",
        ("Current Account", balance, user_id, is_seeded, user_verified),
    )
    acct_id = cur.lastrowid
    cur.close()
    return acct_id


def _insert_income(db_conn, user_id, amount=2500.0, user_verified=0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO income (name, amount, frequency, account, user_id, day, user_verified) "
        "VALUES (?, ?, 'monthly', '', ?, 25, ?)",
        ("My salary", amount, user_id, user_verified),
    )
    inc_id = cur.lastrowid
    cur.close()
    return inc_id


def _insert_bill(db_conn, user_id, amount=100.0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency) "
        "VALUES (?, ?, 1, 'Current Account', ?, 'monthly')",
        ("Monthly bill", amount, user_id),
    )
    bill_id = cur.lastrowid
    cur.close()
    return bill_id


class TestGetSetupState:
    def test_dismissed_returns_show_false(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET setup_dismissed = 1 WHERE id = ?", (test_user["id"],))
        from app import get_setup_state
        state = get_setup_state(test_user["id"])
        assert state == {"show": False}
        db_conn.execute("UPDATE users SET setup_dismissed = 0 WHERE id = ?", (test_user["id"],))

    def test_version_a_no_accounts_all_incomplete(self, app, db_conn, test_user):
        from app import get_setup_state
        state = get_setup_state(test_user["id"])
        assert state["show"] is True
        assert state["version"] == "A"
        assert state["steps"]["account"]["status"] == "incomplete"
        assert state["steps"]["income"]["status"] == "locked"
        assert state["steps"]["bills"]["status"] == "locked"
        assert state["steps"]["forecast"]["status"] == "locked"

    def test_version_a_all_steps_complete(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=1000.0)
        inc_id = _insert_income(db_conn, test_user["id"])
        bill_id = _insert_bill(db_conn, test_user["id"])
        try:
            from app import get_setup_state
            state = get_setup_state(test_user["id"])
            assert state["show"] is True
            assert state["version"] == "A"
            assert state["steps"]["account"]["status"] == "complete"
            assert state["steps"]["income"]["status"] == "complete"
            assert state["steps"]["bills"]["status"] == "complete"
            assert state["steps"]["forecast"]["status"] == "complete"
            assert state["progress_percent"] == 100
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
            db_conn.execute("DELETE FROM income WHERE id = ?", (inc_id,))
            db_conn.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))

    def test_version_b_seeded_account(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1)
        try:
            from app import get_setup_state
            state = get_setup_state(test_user["id"])
            assert state["show"] is True
            assert state["version"] == "B"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))

    def test_version_b_account_user_verified(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1, user_verified=1)
        try:
            from app import get_setup_state
            state = get_setup_state(test_user["id"])
            assert state["steps"]["account"]["status"] == "verified"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))

    def test_version_b_bills_verified_at_three(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1)
        bill_ids = [_insert_bill(db_conn, test_user["id"]) for _ in range(3)]
        try:
            from app import get_setup_state
            state = get_setup_state(test_user["id"])
            assert state["steps"]["bills"]["status"] == "verified"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
            for bid in bill_ids:
                db_conn.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bid,))


class TestSetupDismissRoute:
    def test_setup_dismiss_sets_flag(self, auth_client, test_user, db_conn):
        resp = auth_client.post(
            "/setup/dismiss",
            json={"csrf_token": CSRF},
            content_type="application/json",
        )
        assert resp.status_code == 200
        row = db_conn.execute(
            "SELECT setup_dismissed FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert row["setup_dismissed"] == 1
        db_conn.execute("UPDATE users SET setup_dismissed = 0 WHERE id = ?", (test_user["id"],))
