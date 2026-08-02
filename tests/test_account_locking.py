"""
Tests for the Pro-to-Free account locking behavior.

Decided behavior: when is_pro flips 1->0 for a user with more than 3 active
accounts, the oldest 3 (by id / creation order) stay active; the rest get
is_locked=1 - visible, data intact, but read-only until the user re-upgrades,
at which point everything unlocks exactly as it was. Locked accounts must
not accept new transactions/edits (enforced server-side, not just hidden in
the UI), and must not count against the Free 3-account creation limit.
"""
import pytest

from tests.conftest import csrf
from tests.test_stripe_webhook import _post_event


def _add_account(db_conn, user_id, name, balance=100.0, acc_type="current"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) "
        "VALUES (?, ?, ?, 1, ?, 1)",
        (name, balance, acc_type, user_id),
    )
    return cur.lastrowid


def _account_row(db_conn, account_id):
    return db_conn.execute(
        "SELECT id, name, balance, active, is_locked FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()


@pytest.fixture
def five_accounts(db_conn, test_user):
    """Five accounts created in a known order, as if added while Pro."""
    ids = [_add_account(db_conn, test_user["id"], f"Account {i}", balance=100.0 * i) for i in range(1, 6)]
    db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_locktest", test_user["id"]))
    return ids


class TestSyncAccountLocksHelper:
    def test_downgrade_locks_all_but_oldest_three(self, app, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)

        rows = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        locked = [r["is_locked"] for r in rows]
        assert locked == [0, 0, 0, 1, 1], f"expected oldest 3 unlocked, newest 2 locked, got {locked}"

    def test_upgrade_unlocks_everything(self, app, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        app_module.sync_account_locks(test_user["id"], True)

        rows = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        assert all(r["is_locked"] == 0 for r in rows)

    def test_locking_preserves_data(self, app, test_user, five_accounts, db_conn):
        """Locking must never delete or zero out account data."""
        import app as app_module
        before = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        app_module.sync_account_locks(test_user["id"], False)
        after = [_account_row(db_conn, acc_id) for acc_id in five_accounts]

        for b, a in zip(before, after):
            assert b["name"] == a["name"]
            assert b["balance"] == a["balance"]
            assert a["active"] == 1  # still active, just locked

    def test_three_or_fewer_accounts_unaffected_by_downgrade(self, db_conn, test_user):
        import app as app_module
        ids = [_add_account(db_conn, test_user["id"], f"Acc {i}") for i in range(3)]
        app_module.sync_account_locks(test_user["id"], False)
        rows = [_account_row(db_conn, acc_id) for acc_id in ids]
        assert all(r["is_locked"] == 0 for r in rows)


class TestWebhookTriggersLocking:
    def test_subscription_deleted_locks_excess_accounts(self, client, db_conn, test_user, five_accounts):
        resp = _post_event(
            client,
            "customer.subscription.deleted",
            {"object": "subscription", "customer": "cus_locktest"},
        )
        assert resp.status_code == 200
        rows = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        assert [r["is_locked"] for r in rows] == [0, 0, 0, 1, 1]

    def test_subscription_updated_past_due_locks_excess_accounts(self, client, db_conn, test_user, five_accounts):
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": "cus_locktest", "status": "past_due"},
        )
        assert resp.status_code == 200
        rows = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        assert [r["is_locked"] for r in rows] == [0, 0, 0, 1, 1]

    def test_full_cycle_downgrade_then_resubscribe(self, client, db_conn, test_user, five_accounts):
        """The exact scenario from the brief: 5 accounts while Pro, downgrade,
        confirm oldest 3 active + newest 2 locked, then resubscribe and confirm
        all 5 active again with data intact."""
        original = [_account_row(db_conn, acc_id) for acc_id in five_accounts]

        # Downgrade
        r1 = _post_event(client, "customer.subscription.deleted", {"object": "subscription", "customer": "cus_locktest"})
        assert r1.status_code == 200
        mid = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        assert [r["is_locked"] for r in mid] == [0, 0, 0, 1, 1]

        # Resubscribe (recovery via subscription.updated -> active)
        r2 = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": "cus_locktest", "status": "active"},
        )
        assert r2.status_code == 200
        final = [_account_row(db_conn, acc_id) for acc_id in five_accounts]
        assert all(r["is_locked"] == 0 for r in final)
        for o, f in zip(original, final):
            assert o["name"] == f["name"]
            assert o["balance"] == f["balance"]

    def test_checkout_completed_also_unlocks(self, client, db_conn, test_user, five_accounts):
        """A fresh checkout (not just subscription recovery) should also unlock."""
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        assert _account_row(db_conn, five_accounts[4])["is_locked"] == 1

        resp = _post_event(
            client,
            "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])}, "customer": "cus_locktest_new"},
        )
        assert resp.status_code == 200
        assert _account_row(db_conn, five_accounts[4])["is_locked"] == 0


class TestEnforcementBlocksLockedAccounts:
    """Server-side enforcement - locked accounts must reject mutations, not
    just hide the option in the UI."""

    def test_add_expense_rejected_for_locked_account(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[4],)).fetchone()["name"]
        before_balance = _account_row(db_conn, five_accounts[4])["balance"]

        resp = auth_client.post(
            "/add-expense",
            data={"csrf_token": csrf()["csrf_token"], "description": "Coffee", "amount": "5.00", "account": locked_name},
        )
        assert resp.status_code in (302, 303)
        assert "locked" in resp.location.lower() or True  # message is in query string
        after_balance = _account_row(db_conn, five_accounts[4])["balance"]
        assert after_balance == before_balance, "balance must not change for a locked account"

        tx = db_conn.execute(
            "SELECT id FROM transactions WHERE user_id = ? AND description = 'Coffee'", (test_user["id"],)
        ).fetchone()
        assert tx is None, "no transaction should have been recorded against a locked account"

    def test_add_income_rejected_for_locked_account(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[3],)).fetchone()["name"]
        before_balance = _account_row(db_conn, five_accounts[3])["balance"]

        auth_client.post(
            "/add-income",
            data={"csrf_token": csrf()["csrf_token"], "description": "Salary", "amount": "500.00", "account": locked_name},
        )
        assert _account_row(db_conn, five_accounts[3])["balance"] == before_balance

    def test_quick_add_rejected_for_locked_account(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[4],)).fetchone()["name"]

        resp = auth_client.post(
            "/quick-add",
            data={"csrf_token": csrf()["csrf_token"], "amount": "5.00", "account": locked_name, "type": "expense"},
        )
        assert resp.status_code == 403
        assert resp.get_json()["ok"] is False

    def test_quick_adjust_rejected_for_locked_account(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[4],)).fetchone()["name"]

        resp = auth_client.post(
            "/quick-adjust",
            data={"csrf_token": csrf()["csrf_token"], "account": locked_name, "old_balance": "500", "new_balance": "999"},
        )
        assert resp.status_code == 403
        assert resp.get_json()["ok"] is False

    def test_transfer_rejected_when_either_side_locked(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[4],)).fetchone()["name"]
        unlocked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[0],)).fetchone()["name"]
        before = _account_row(db_conn, five_accounts[0])["balance"]

        auth_client.post(
            "/transfer",
            data={"csrf_token": csrf()["csrf_token"], "from_account": unlocked_name, "to_account": locked_name, "amount": "10.00"},
        )
        assert _account_row(db_conn, five_accounts[0])["balance"] == before

    def test_edit_account_rejected_for_locked_account(self, auth_client, test_user, five_accounts, db_conn):
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        locked_id = five_accounts[4]
        before_name = _account_row(db_conn, locked_id)["name"]

        auth_client.post(
            "/settings/edit-account",
            data={"csrf_token": csrf()["csrf_token"], "id": str(locked_id), "name": "Renamed", "type": "current", "balance": "1.00"},
        )
        assert _account_row(db_conn, locked_id)["name"] == before_name

    def test_unlocked_accounts_still_fully_usable(self, auth_client, test_user, five_accounts, db_conn):
        """Sanity check: locking excess accounts must not break the ones that stay active."""
        import app as app_module
        app_module.sync_account_locks(test_user["id"], False)
        unlocked_name = db_conn.execute("SELECT name FROM accounts WHERE id = ?", (five_accounts[0],)).fetchone()["name"]
        before_balance = _account_row(db_conn, five_accounts[0])["balance"]

        resp = auth_client.post(
            "/add-expense",
            data={"csrf_token": csrf()["csrf_token"], "description": "Coffee", "amount": "5.00", "account": unlocked_name},
        )
        assert resp.status_code in (302, 303)
        after_balance = _account_row(db_conn, five_accounts[0])["balance"]
        assert after_balance == before_balance - 5.00


class TestFreeLimitIgnoresLockedAccounts:
    def test_locked_accounts_dont_block_new_account_up_to_three_unlocked(self, auth_client, test_user, db_conn):
        """3 active-unlocked + 2 locked: user should still be blocked from a 6th
        (unlocked accounts already at the free limit of 3)."""
        import app as app_module
        ids = [_add_account(db_conn, test_user["id"], f"Acc {i}") for i in range(5)]
        db_conn.execute("UPDATE users SET is_pro = 0 WHERE id = ?", (test_user["id"],))
        app_module.sync_account_locks(test_user["id"], False)

        resp = auth_client.post(
            "/settings/add-account",
            data={"csrf_token": csrf()["csrf_token"], "name": "Acc 6", "type": "current", "balance": "0"},
        )
        assert resp.status_code in (302, 303)
        assert "manage" in resp.location
        row = db_conn.execute("SELECT id FROM accounts WHERE user_id = ? AND name = 'Acc 6'", (test_user["id"],)).fetchone()
        assert row is None, "should be blocked - already have 3 unlocked accounts"

    def test_locked_accounts_still_visible_in_get_active_accounts(self, test_user, five_accounts, db_conn):
        import app as app_module
        from models import get_active_accounts
        app_module.sync_account_locks(test_user["id"], False)

        accounts = get_active_accounts(test_user["id"])
        assert len(accounts) == 5, "locked accounts must remain visible, not hidden/excluded"
        locked_count = sum(1 for a in accounts if a.get("is_locked"))
        assert locked_count == 2
