"""
Tests for the auto-apply idempotency guard (August 2026).

A real report showed Salary/Rent/Car Insurance/Car Finance each logged as
3 separate real transactions on the same day, despite Manage/My Money
showing only one recurring rule for each. Investigated end to end:
add_transaction()/update_account_balance() (called from apply_auto_items())
both go through get_db(), which since the earlier August 2026 per-request
connection fix returns the same connection for a whole request, but each
call still commits individually — so a LATER failure (specifically the
last_applied UPDATE, which was entirely unguarded and could silently
propagate into home()'s broad debug-level catch) can't roll back an
EARLIER item's already-committed transaction insert. If last_applied fails
to update for any reason, the next page load recomputes the exact same
occurrence as still-pending and re-inserts it — exactly matching the
observed symptom (only the most-recently-applied day affected, historical
backfilled months untouched, silent auto-apply mode).

Fix: apply_auto_items() now checks _auto_apply_transaction_exists() before
inserting each item, so re-applying the same occurrence — for whatever
reason — can never duplicate it. The last_applied update is also now
wrapped in its own try/except with proper error-level logging, instead of
silently vanishing into the outer catch.

Covers:
  - _auto_apply_transaction_exists() directly
  - apply_auto_items() called twice (or more) with the same items never
    double-inserts or double-credits the balance
  - A genuinely new item in the same batch still applies normally
  - last_applied still advances correctly through the guard
  - The full home()-driven silent auto-apply path, called twice in a row,
    matches the real-world repro exactly
"""
import datetime

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


@pytest.fixture(autouse=True)
def _skip_on_weekend():
    # These tests anchor a bill's day-of-month to TODAY.day so its nominal
    # due date is exactly today; shift_weekend_to_monday() would push a
    # weekend nominal date forward, which would make it stop landing on
    # "today" and break the setup entirely (unrelated to what's being
    # tested here — see the CI weekend-shift flake fix elsewhere in this
    # test suite for the same class of issue).
    if TODAY.weekday() >= 5:
        pytest.skip("bill-day arithmetic anchored to today needs a weekday to avoid shift_weekend_to_monday()")


def _iso(days_ago=0):
    return (TODAY - datetime.timedelta(days=days_ago)).isoformat()


def _add_bill(db_conn, user_id, name, amount, day, account, last_applied=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, last_applied) VALUES (?,?,?,?,?,?,?)",
        (name, amount, day, account, user_id, "monthly", last_applied),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_income(db_conn, user_id, name, amount, account, day, last_applied=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO income (name, amount, frequency, account, day, weekly_day, last_applied, is_primary, user_id) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        (name, amount, "monthly", account, day, 4, last_applied, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


def _count_transactions(db_conn, user_id, description):
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND description=?", (user_id, description))
    return cur.fetchone()[0]


def _get_balance(db_conn, account_id):
    cur = db_conn.cursor()
    cur.execute("SELECT balance FROM accounts WHERE id=?", (account_id,))
    return float(cur.fetchone()[0])


# ── 1. _auto_apply_transaction_exists() DIRECTLY ────────────────────────────
class TestAutoApplyTransactionExists:
    def test_false_when_no_matching_transaction(self, app, test_user, test_account):
        from app import _auto_apply_transaction_exists
        with app.app_context():
            assert _auto_apply_transaction_exists(
                test_user["id"], _iso(0), "Rent", -1250.0, test_account["name"]
            ) is False

    def test_true_when_exact_match_exists(self, app, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
            (_iso(0), "Rent", -1250.0, test_account["name"], test_user["id"], "bill", "Bills"),
        )
        db_conn.commit()
        from app import _auto_apply_transaction_exists
        with app.app_context():
            assert _auto_apply_transaction_exists(
                test_user["id"], _iso(0), "Rent", -1250.0, test_account["name"]
            ) is True

    def test_false_when_amount_differs(self, app, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
            (_iso(0), "Rent", -1250.0, test_account["name"], test_user["id"], "bill", "Bills"),
        )
        db_conn.commit()
        from app import _auto_apply_transaction_exists
        with app.app_context():
            assert _auto_apply_transaction_exists(
                test_user["id"], _iso(0), "Rent", -1300.0, test_account["name"]
            ) is False


# ── 2. apply_auto_items() IDEMPOTENCY ────────────────────────────────────────
class TestApplyAutoItemsIdempotent:
    def _pending_item(self, bill_id, name, amount, account, due_date):
        return {
            "type": "bill",
            "item_id": bill_id,
            "name": name,
            "amount": -abs(amount),
            "account": account,
            "due_date": due_date,
        }

    def test_calling_twice_does_not_double_insert(self, app, db_conn, test_user, test_account):
        bill_id = _add_bill(db_conn, test_user["id"], "Rent", 1250.0, TODAY.day, test_account["name"], last_applied=_iso(1))
        item = self._pending_item(bill_id, "Rent", 1250.0, test_account["name"], _iso(0))

        from app import apply_auto_items
        with app.app_context():
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])  # simulates a stale last_applied re-triggering the same occurrence

        assert _count_transactions(db_conn, test_user["id"], "Rent") == 1

    def test_calling_three_times_still_only_one_transaction(self, app, db_conn, test_user, test_account):
        """Directly matches the real report: the same occurrence re-applied
        3 times must still only ever produce one real transaction."""
        bill_id = _add_bill(db_conn, test_user["id"], "Car Finance", 350.0, TODAY.day, test_account["name"], last_applied=_iso(1))
        item = self._pending_item(bill_id, "Car Finance", 350.0, test_account["name"], _iso(0))

        from app import apply_auto_items
        with app.app_context():
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])

        assert _count_transactions(db_conn, test_user["id"], "Car Finance") == 1

    def test_balance_only_credited_once_across_repeat_calls(self, app, db_conn, test_user, test_account):
        starting_balance = test_account["balance"]
        bill_id = _add_bill(db_conn, test_user["id"], "Rent", 1250.0, TODAY.day, test_account["name"], last_applied=_iso(1))
        item = self._pending_item(bill_id, "Rent", 1250.0, test_account["name"], _iso(0))

        from app import apply_auto_items
        with app.app_context():
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])

        assert _get_balance(db_conn, test_account["id"]) == round(starting_balance - 1250.0, 2)

    def test_a_genuinely_new_item_in_the_same_batch_still_applies(self, app, db_conn, test_user, test_account):
        """The guard must only skip the specific duplicate occurrence, not
        anything else in the same batch."""
        existing_bill_id = _add_bill(db_conn, test_user["id"], "Rent", 1250.0, TODAY.day, test_account["name"], last_applied=_iso(1))
        new_bill_id = _add_bill(db_conn, test_user["id"], "Gym membership", 30.0, TODAY.day, test_account["name"], last_applied=_iso(1))

        existing_item = self._pending_item(existing_bill_id, "Rent", 1250.0, test_account["name"], _iso(0))
        new_item = self._pending_item(new_bill_id, "Gym membership", 30.0, test_account["name"], _iso(0))

        from app import apply_auto_items
        with app.app_context():
            apply_auto_items(test_user["id"], [existing_item])  # first application
            apply_auto_items(test_user["id"], [existing_item, new_item])  # re-trigger + a real new item

        assert _count_transactions(db_conn, test_user["id"], "Rent") == 1
        assert _count_transactions(db_conn, test_user["id"], "Gym membership") == 1

    def test_last_applied_still_advances_through_the_guard(self, app, db_conn, test_user, test_account):
        """The guard must not prevent last_applied from moving forward —
        otherwise a genuinely-skipped duplicate would keep showing up as
        'pending' forever."""
        bill_id = _add_bill(db_conn, test_user["id"], "Rent", 1250.0, TODAY.day, test_account["name"], last_applied=_iso(1))
        item = self._pending_item(bill_id, "Rent", 1250.0, test_account["name"], _iso(0))

        from app import apply_auto_items
        with app.app_context():
            apply_auto_items(test_user["id"], [item])
            apply_auto_items(test_user["id"], [item])

        row = db_conn.execute("SELECT last_applied FROM scheduled_expenses WHERE id=?", (bill_id,)).fetchone()
        assert row["last_applied"] == TODAY.isoformat()


# ── 3. FULL home() SILENT AUTO-APPLY PATH ────────────────────────────────────
class TestHomeSilentAutoApplyRepeatLoad:
    def test_loading_home_repeatedly_does_not_duplicate_transactions(self, auth_client, db_conn, test_user, test_account):
        """End-to-end repro of the reported bug: silent auto-apply enabled,
        a bill due today with a stale last_applied (yesterday), loading
        Home 3 times in a row (as if 3 page loads happened before the
        fix) must still only ever produce one real transaction."""
        db_conn.execute(
            "UPDATE users SET auto_apply_enabled=1, auto_apply_confirm=0 WHERE id=?", (test_user["id"],)
        )
        _add_bill(db_conn, test_user["id"], "Rent", 1250.0, TODAY.day, test_account["name"], last_applied=_iso(1))

        auth_client.get("/")
        auth_client.get("/")
        auth_client.get("/")

        assert _count_transactions(db_conn, test_user["id"], "Rent") == 1
