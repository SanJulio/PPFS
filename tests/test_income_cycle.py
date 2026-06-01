"""
Tests for automatic cycle mode switching triggered by income source management.

Rules under test:
- Adding the FIRST income source sets is_primary=1 and cycle_mode='automatic'.
- Adding a SECOND income source does not change cycle_mode or the existing primary.
- Deleting the only income source reverts cycle_mode to 'manual'.
- Deleting the primary source (when others exist) reverts cycle_mode to 'manual'.
- Deleting a non-primary source leaves cycle_mode unchanged.
"""

import pytest
from tests.conftest import csrf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_cycle_mode(db_conn, user_id):
    row = db_conn.execute(
        "SELECT cycle_mode FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return row["cycle_mode"] if row else None


def _get_primary_income(db_conn, user_id):
    """Return the income row with is_primary=1, or None."""
    return db_conn.execute(
        "SELECT * FROM income WHERE user_id = ? AND is_primary = 1", (user_id,)
    ).fetchone()


def _add_income(auth_client, name="Salary", amount="2000", account="Current"):
    return auth_client.post(
        "/settings/add-income",
        data={**csrf(), "name": name, "amount": amount, "frequency": "monthly",
              "account": account, "day": "25", "rule_type": "fixed_date",
              "rule_config": '{"day":25}', "weekend_rule": "before",
              "bank_holiday_rule": "before"},
        follow_redirects=False,
    )


def _delete_income(auth_client, income_id):
    return auth_client.post(
        "/settings/delete-income",
        data={**csrf(), "id": str(income_id)},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Add income tests
# ---------------------------------------------------------------------------

class TestAddFirstIncome:
    def test_first_income_sets_primary(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client)
        row = _get_primary_income(db_conn, test_user["id"])
        assert row is not None, "First income source should be marked primary"

    def test_first_income_switches_cycle_to_automatic(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client)
        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"

    def test_second_income_does_not_change_primary(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client, name="Salary")
        first_primary = _get_primary_income(db_conn, test_user["id"])
        assert first_primary is not None

        _add_income(auth_client, name="Freelance", amount="500")
        # Primary should still be the first income source
        still_primary = _get_primary_income(db_conn, test_user["id"])
        assert still_primary["id"] == first_primary["id"]

    def test_second_income_does_not_change_cycle_mode(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client, name="Salary")
        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"

        _add_income(auth_client, name="Freelance", amount="500")
        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"


# ---------------------------------------------------------------------------
# Delete income tests
# ---------------------------------------------------------------------------

class TestDeleteIncome:
    def test_deleting_only_income_reverts_to_manual(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client)
        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"

        row = _get_primary_income(db_conn, test_user["id"])
        _delete_income(auth_client, row["id"])

        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "manual"

    def test_deleting_primary_of_multiple_reverts_to_manual(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client, name="Salary")
        _add_income(auth_client, name="Freelance", amount="500")

        primary = _get_primary_income(db_conn, test_user["id"])
        assert primary is not None
        _delete_income(auth_client, primary["id"])

        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "manual"

    def test_deleting_non_primary_leaves_cycle_mode_unchanged(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client, name="Salary")
        _add_income(auth_client, name="Freelance", amount="500")
        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"

        # Find the non-primary income row
        non_primary = db_conn.execute(
            "SELECT id FROM income WHERE user_id = ? AND is_primary = 0", (test_user["id"],)
        ).fetchone()
        assert non_primary is not None
        _delete_income(auth_client, non_primary["id"])

        assert _get_user_cycle_mode(db_conn, test_user["id"]) == "automatic"

    def test_deleting_non_primary_leaves_primary_intact(self, auth_client, test_user, test_account, db_conn):
        _add_income(auth_client, name="Salary")
        _add_income(auth_client, name="Freelance", amount="500")

        primary_before = _get_primary_income(db_conn, test_user["id"])
        non_primary = db_conn.execute(
            "SELECT id FROM income WHERE user_id = ? AND is_primary = 0", (test_user["id"],)
        ).fetchone()
        _delete_income(auth_client, non_primary["id"])

        primary_after = _get_primary_income(db_conn, test_user["id"])
        assert primary_after is not None
        assert primary_after["id"] == primary_before["id"]
