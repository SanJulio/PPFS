"""Tests for get_my_money_setup() and the My Money setup routes."""

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


class TestGetMyMoneySetup:
    def test_dismissed_returns_show_false(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET setup_dismissed = 1 WHERE id = ?", (test_user["id"],))
        from app import get_my_money_setup
        state = get_my_money_setup(test_user["id"])
        assert state == {"show": False}
        db_conn.execute("UPDATE users SET setup_dismissed = 0 WHERE id = ?", (test_user["id"],))

    def test_version_a_no_accounts_all_incomplete_or_locked(self, app, db_conn, test_user):
        from app import get_my_money_setup
        state = get_my_money_setup(test_user["id"])
        assert state["show"] is True
        assert state["version"] == "A"
        assert state["steps"][0]["status"] == "incomplete"
        assert state["steps"][1]["status"] == "locked"
        assert state["steps"][2]["status"] == "locked"
        assert state["progress"] == 0

    def test_version_a_all_steps_complete(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=1000.0)
        inc_id = _insert_income(db_conn, test_user["id"])
        bill_id = _insert_bill(db_conn, test_user["id"])
        try:
            from app import get_my_money_setup
            state = get_my_money_setup(test_user["id"])
            assert state["show"] is True
            assert state["version"] == "A"
            assert state["steps"][0]["status"] == "complete"
            assert state["steps"][1]["status"] == "complete"
            assert state["steps"][2]["status"] == "complete"
            assert state["progress"] == 100
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
            db_conn.execute("DELETE FROM income WHERE id = ?", (inc_id,))
            db_conn.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))

    def test_version_b_seeded_account_triggers_b(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1)
        try:
            from app import get_my_money_setup
            state = get_my_money_setup(test_user["id"])
            assert state["show"] is True
            assert state["version"] == "B"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))

    def test_version_b_account_user_verified(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1, user_verified=1)
        try:
            from app import get_my_money_setup
            state = get_my_money_setup(test_user["id"])
            assert state["steps"][0]["status"] == "verified"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))

    def test_version_b_income_verified_by_non_default_amount(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1)
        inc_id = _insert_income(db_conn, test_user["id"], amount=3000.0)
        try:
            from app import get_my_money_setup
            state = get_my_money_setup(test_user["id"])
            assert state["steps"][1]["status"] == "verified"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
            db_conn.execute("DELETE FROM income WHERE id = ?", (inc_id,))

    def test_version_b_bills_verified_at_three(self, app, db_conn, test_user):
        acct_id = _insert_account(db_conn, test_user["id"], balance=850.0, is_seeded=1)
        bill_ids = [_insert_bill(db_conn, test_user["id"]) for _ in range(3)]
        try:
            from app import get_my_money_setup
            state = get_my_money_setup(test_user["id"])
            assert state["steps"][2]["status"] == "verified"
        finally:
            db_conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
            for bid in bill_ids:
                db_conn.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bid,))


class TestMyMoneySetupRoutes:
    def test_dismiss_sets_flag(self, auth_client, test_user, db_conn):
        resp = auth_client.post(
            "/my-money/setup/dismiss",
            json={"csrf_token": CSRF},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        row = db_conn.execute(
            "SELECT setup_dismissed FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert row["setup_dismissed"] == 1
        db_conn.execute("UPDATE users SET setup_dismissed = 0 WHERE id = ?", (test_user["id"],))

    def test_state_returns_json_with_show_key(self, auth_client):
        resp = auth_client.get("/my-money/setup/state")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "show" in data
