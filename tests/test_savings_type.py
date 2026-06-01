"""
Tests for the savings_type column on the accounts table.

Rules under test:
- Adding a savings account with type 'variable' stores savings_type='variable'.
- Adding a savings account with type 'fixed' stores savings_type='fixed'.
- Adding a non-savings account stores savings_type=NULL.
- Editing a savings account can change its savings_type.
- Editing a non-savings account always sets savings_type=NULL even if sent.
"""

import pytest
from tests.conftest import csrf


def _add_account(auth_client, name, acc_type, balance="1000", savings_type=None):
    data = {**csrf(), "name": name, "type": acc_type, "balance": balance}
    if savings_type:
        data["savings_type"] = savings_type
    return auth_client.post("/settings/add-account", data=data, follow_redirects=False)


def _get_account(db_conn, user_id, name):
    return db_conn.execute(
        "SELECT * FROM accounts WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()


def _edit_account(auth_client, db_conn, user_id, name, acc_type, balance="1000", savings_type=None):
    row = _get_account(db_conn, user_id, name)
    assert row is not None, f"Account '{name}' not found"
    data = {**csrf(), "id": str(row["id"]), "name": name, "type": acc_type, "balance": balance}
    if savings_type:
        data["savings_type"] = savings_type
    return auth_client.post("/settings/edit-account", data=data, follow_redirects=False)


class TestAddAccountSavingsType:
    def test_savings_variable_stored(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "My ISA", "savings", savings_type="variable")
        row = _get_account(db_conn, test_user["id"], "My ISA")
        assert row is not None
        assert row["savings_type"] == "variable"

    def test_savings_fixed_stored(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Fixed ISA", "savings", savings_type="fixed")
        row = _get_account(db_conn, test_user["id"], "Fixed ISA")
        assert row is not None
        assert row["savings_type"] == "fixed"

    def test_current_account_savings_type_null(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Main Current", "current")
        row = _get_account(db_conn, test_user["id"], "Main Current")
        assert row is not None
        assert row["savings_type"] is None

    def test_non_savings_ignores_savings_type_param(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Cash Wallet", "cash", savings_type="fixed")
        row = _get_account(db_conn, test_user["id"], "Cash Wallet")
        assert row is not None
        assert row["savings_type"] is None


class TestEditAccountSavingsType:
    def test_edit_changes_savings_type_to_fixed(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Flex Saver", "savings", savings_type="variable")
        _edit_account(auth_client, db_conn, test_user["id"], "Flex Saver", "savings", savings_type="fixed")
        row = _get_account(db_conn, test_user["id"], "Flex Saver")
        assert row["savings_type"] == "fixed"

    def test_edit_changes_savings_type_to_variable(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Lock ISA", "savings", savings_type="fixed")
        _edit_account(auth_client, db_conn, test_user["id"], "Lock ISA", "savings", savings_type="variable")
        row = _get_account(db_conn, test_user["id"], "Lock ISA")
        assert row["savings_type"] == "variable"

    def test_edit_non_savings_clears_savings_type(self, auth_client, test_user, db_conn):
        _add_account(auth_client, "Mixed", "savings", savings_type="fixed")
        _edit_account(auth_client, db_conn, test_user["id"], "Mixed", "current", savings_type="fixed")
        row = _get_account(db_conn, test_user["id"], "Mixed")
        assert row["savings_type"] is None
