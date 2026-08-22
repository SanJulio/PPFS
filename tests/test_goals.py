"""
Tests for savings & debt repayment goal tracking (core feature — no
streak/engagement mechanics, that's a separate follow-up).

Covers: goal creation (both types, linked/standalone, with/without a target
date), progress calculation for all three tracking modes, the deterministic
pace suggestion (only when a target date is set) with its Safe to Spend
cross-check warning, editing/deleting/manual-and-automatic completion, and
how a goal behaves when its linked account gets locked (Pro-to-Free
downgrade) — the account's frozen balance is used as-is (consistent with
how locked accounts are already handled everywhere else in the app) and the
goal surfaces an account_locked flag so the UI can note progress may be
stale, rather than hiding the goal or erroring.
"""
import datetime

import pytest

from tests.conftest import csrf


# ── HELPERS ──────────────────────────────────────────────────────────────────
def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current", is_locked=0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, is_locked, include_in_overview) "
        "VALUES (?, ?, ?, 1, ?, ?, 1)",
        (name, balance, acc_type, user_id, is_locked),
    )
    db_conn.commit()
    return cur.lastrowid


def _get_goal(db_conn, goal_id):
    return db_conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()


def _create_goal_via_route(auth_client, **overrides):
    data = {
        "name": "Test goal",
        "goal_type": "savings",
        "target_amount": "1000",
    }
    data.update(overrides)
    return auth_client.post("/settings/add-goal", data={**csrf(), **data}, follow_redirects=False)


# ── 1. GOAL CREATION ─────────────────────────────────────────────────────────
class TestGoalCreation:
    def test_create_savings_goal_standalone_no_target_date(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(auth_client, name="Emergency fund", goal_type="savings", target_amount="2000")
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("Emergency fund", test_user["id"])).fetchone()
        assert row["goal_type"] == "savings"
        assert row["target_amount"] == 2000.0
        assert row["target_date"] is None
        assert row["linked_account_id"] is None
        assert row["status"] == "active"

    def test_create_debt_goal_standalone_with_target_date(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(
            auth_client, name="Pay off loan", goal_type="debt", target_amount="5000", target_date="2027-06-01"
        )
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("Pay off loan", test_user["id"])).fetchone()
        assert row["goal_type"] == "debt"
        assert row["target_date"] == "2027-06-01"

    def test_create_savings_goal_linked_to_account(self, auth_client, db_conn, test_user, test_account):
        resp = _create_goal_via_route(
            auth_client, name="House deposit", target_amount="10000", linked_account_id=str(test_account["id"])
        )
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("House deposit", test_user["id"])).fetchone()
        assert row["linked_account_id"] == test_account["id"]
        # Starting balance snapshot captured at creation
        assert row["starting_balance"] == test_account["balance"]

    def test_create_debt_goal_linked_to_account(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Car Loan", balance=-8000.0)
        resp = _create_goal_via_route(
            auth_client, name="Pay off car", goal_type="debt", target_amount="8000", linked_account_id=str(acc_id)
        )
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("Pay off car", test_user["id"])).fetchone()
        assert row["linked_account_id"] == acc_id
        assert row["starting_balance"] == -8000.0

    def test_create_goal_requires_name(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(auth_client, name="")
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (test_user["id"],)).fetchone()["c"] == 0

    def test_create_goal_rejects_non_positive_target(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(auth_client, target_amount="0")
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (test_user["id"],)).fetchone()["c"] == 0

    def test_create_goal_rejects_invalid_target_date(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(auth_client, target_date="not-a-date")
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (test_user["id"],)).fetchone()["c"] == 0

    def test_create_goal_rejects_unknown_linked_account(self, auth_client, db_conn, test_user):
        resp = _create_goal_via_route(auth_client, linked_account_id="999999")
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (test_user["id"],)).fetchone()["c"] == 0

    def test_add_goal_requires_csrf(self, client):
        resp = client.post("/settings/add-goal", data={"name": "x", "goal_type": "savings", "target_amount": "10"})
        assert resp.status_code in (302, 401, 403)


# ── 2. PROGRESS CALCULATION ──────────────────────────────────────────────────
class TestProgressCalculation:
    def test_linked_savings_progress_is_current_balance_over_target(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=3000.0)
        _create_goal_via_route(
            auth_client, name="House deposit", target_amount="10000", linked_account_id=str(acc_id)
        )
        import app as app_module
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("House deposit", test_user["id"])).fetchone())
        progress = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress["progress_amount"] == 3000.0
        assert progress["progress_pct"] == 30.0

        # Balance grows -> progress tracks it automatically, live
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (6000.0, acc_id))
        db_conn.commit()
        progress2 = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress2["progress_amount"] == 6000.0
        assert progress2["progress_pct"] == 60.0

    def test_linked_debt_progress_is_amount_paid_down(self, auth_client, db_conn, test_user):
        """Debt balance decreasing toward zero = progress increasing, the
        opposite direction to a savings goal's balance increasing."""
        acc_id = _add_account(db_conn, test_user["id"], "Car Loan", balance=-8000.0)
        _create_goal_via_route(
            auth_client, name="Pay off car", goal_type="debt", target_amount="8000", linked_account_id=str(acc_id)
        )
        import app as app_module
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("Pay off car", test_user["id"])).fetchone())

        # No payments made yet -> 0% progress even though balance is -8000
        progress0 = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress0["progress_amount"] == 0.0
        assert progress0["progress_pct"] == 0.0

        # Paid down to -5000 (3000 paid off)
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (-5000.0, acc_id))
        db_conn.commit()
        progress1 = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress1["progress_amount"] == 3000.0
        assert progress1["progress_pct"] == 37.5

        # Fully paid off (balance reaches 0)
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (0.0, acc_id))
        db_conn.commit()
        progress2 = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress2["progress_amount"] == 8000.0
        assert progress2["progress_pct"] == 100.0

    def test_linked_debt_progress_works_with_positive_owed_convention_too(self, auth_client, db_conn, test_user):
        """Some users might track 'amount still owed' as a positive figure
        instead of a negative balance — abs() on both sides makes the paid-
        down calculation convention-agnostic."""
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=8000.0)
        _create_goal_via_route(
            auth_client, name="Pay off loan", goal_type="debt", target_amount="8000", linked_account_id=str(acc_id)
        )
        import app as app_module
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("Pay off loan", test_user["id"])).fetchone())

        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (2000.0, acc_id))
        db_conn.commit()
        progress = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress["progress_amount"] == 6000.0
        assert progress["progress_pct"] == 75.0

    def test_standalone_progress_is_sum_of_contributions(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Holiday fund", target_amount="1000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Holiday fund", test_user["id"])).fetchone()["id"]

        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "100", "date": "2026-01-01"})
        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "250", "date": "2026-02-01"})

        import app as app_module
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
        progress = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress["progress_amount"] == 350.0
        assert progress["progress_pct"] == 35.0

    def test_standalone_debt_progress_also_just_sums_contributions(self, auth_client, db_conn, test_user):
        """Standalone contributions never need sign-flipping - the user
        self-reports 'amount achieved' either way."""
        _create_goal_via_route(auth_client, name="Pay off card", goal_type="debt", target_amount="500")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Pay off card", test_user["id"])).fetchone()["id"]
        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "200", "date": "2026-01-01"})

        import app as app_module
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
        progress = app_module._compute_goal_progress(goal, test_user["id"])
        assert progress["progress_amount"] == 200.0

    def test_contribution_rejected_for_linked_goal(self, auth_client, db_conn, test_user, test_account):
        _create_goal_via_route(auth_client, name="Linked goal", target_amount="1000", linked_account_id=str(test_account["id"]))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Linked goal", test_user["id"])).fetchone()["id"]
        resp = auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "50", "date": "2026-01-01"}, follow_redirects=False)
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goal_contributions WHERE goal_id=?", (goal_id,)).fetchone()["c"] == 0

    def test_delete_contribution(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Fund", target_amount="1000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Fund", test_user["id"])).fetchone()["id"]
        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "100", "date": "2026-01-01"})
        contrib_id = db_conn.execute("SELECT id FROM goal_contributions WHERE goal_id=?", (goal_id,)).fetchone()["id"]

        resp = auth_client.post("/settings/delete-goal-contribution", data={**csrf(), "id": str(contrib_id)}, follow_redirects=False)
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) c FROM goal_contributions WHERE id=?", (contrib_id,)).fetchone()["c"] == 0


# ── 3. AI-ASSISTED PACE SUGGESTION ───────────────────────────────────────────
class TestPaceSuggestion:
    def test_pace_calculates_correctly_with_target_date(self):
        import app as app_module
        # £800 remaining over 100 days -> 100/30.44 ≈ 3.285 months -> ≈ £243.53/month
        pace = app_module._suggest_goal_pace(1000, 200, (datetime.date.today() + datetime.timedelta(days=100)).isoformat())
        assert pace is not None
        assert pace["remaining_amount"] == 800.0
        assert pace["overdue"] is False
        expected_months = 100 / 30.44
        expected_pace = round(800 / expected_months, 2)
        assert pace["monthly_pace"] == expected_pace

    def test_pace_is_none_without_target_date(self):
        import app as app_module
        assert app_module._suggest_goal_pace(1000, 200, None) is None
        assert app_module._suggest_goal_pace(1000, 200, "") is None

    def test_pace_flags_overdue_target_date(self):
        import app as app_module
        past_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        pace = app_module._suggest_goal_pace(1000, 200, past_date)
        assert pace["overdue"] is True
        assert pace["monthly_pace"] is None
        assert pace["remaining_amount"] == 800.0

    def test_pace_preview_api_returns_none_without_target_date(self, auth_client):
        resp = auth_client.post("/api/goal-pace-preview", json={**csrf(), "target_amount": 1000, "progress_amount": 0})
        assert resp.status_code == 200
        assert resp.get_json()["pace"] is None

    def test_pace_preview_api_calculates_with_target_date(self, auth_client):
        target_date = (datetime.date.today() + datetime.timedelta(days=200)).isoformat()
        resp = auth_client.post("/api/goal-pace-preview", json={**csrf(), "target_amount": 2000, "target_date": target_date, "progress_amount": 500})
        assert resp.status_code == 200
        pace = resp.get_json()["pace"]
        assert pace is not None
        assert pace["remaining_amount"] == 1500.0
        assert pace["monthly_pace"] > 0

    def test_pace_preview_flags_warning_when_exceeding_safe_to_spend(self, auth_client, db_conn, test_user, test_account):
        # Very tight target -> huge monthly pace, guaranteed to exceed any
        # plausible Safe to Spend figure for a bare test_account with no income.
        target_date = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        resp = auth_client.post("/api/goal-pace-preview", json={**csrf(), "target_amount": 100000, "target_date": target_date, "progress_amount": 0})
        assert resp.status_code == 200
        pace = resp.get_json()["pace"]
        assert pace["warning"] is not None
        assert "Safe to Spend" in pace["warning"]

    def test_pace_preview_no_warning_for_modest_pace(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (5000.0, test_account["id"]))
        db_conn.commit()
        target_date = (datetime.date.today() + datetime.timedelta(days=3000)).isoformat()
        resp = auth_client.post("/api/goal-pace-preview", json={**csrf(), "target_amount": 10, "target_date": target_date, "progress_amount": 0})
        assert resp.status_code == 200
        pace = resp.get_json()["pace"]
        assert pace["warning"] is None

    def test_pace_preview_requires_csrf(self, auth_client):
        resp = auth_client.post("/api/goal-pace-preview", json={"target_amount": 1000, "target_date": "2027-01-01"})
        assert resp.status_code == 403


# ── 4. EDITING, DELETING, COMPLETING ─────────────────────────────────────────
class TestEditDeleteComplete:
    def test_edit_goal_updates_all_fields(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Old name", target_amount="1000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Old name", test_user["id"])).fetchone()["id"]

        resp = auth_client.post(
            "/settings/edit-goal",
            data={**csrf(), "id": str(goal_id), "name": "New name", "goal_type": "debt", "target_amount": "2000", "target_date": "2027-01-01"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        row = _get_goal(db_conn, goal_id)
        assert row["name"] == "New name"
        assert row["goal_type"] == "debt"
        assert row["target_amount"] == 2000.0
        assert row["target_date"] == "2027-01-01"

    def test_edit_goal_relinking_account_resets_starting_balance(self, auth_client, db_conn, test_user):
        acc1 = _add_account(db_conn, test_user["id"], "Acc1", balance=1000.0)
        acc2 = _add_account(db_conn, test_user["id"], "Acc2", balance=500.0)
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000", linked_account_id=str(acc1))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]
        assert _get_goal(db_conn, goal_id)["starting_balance"] == 1000.0

        auth_client.post(
            "/settings/edit-goal",
            data={**csrf(), "id": str(goal_id), "name": "Goal", "goal_type": "savings", "target_amount": "1000", "linked_account_id": str(acc2)},
        )
        row = _get_goal(db_conn, goal_id)
        assert row["linked_account_id"] == acc2
        assert row["starting_balance"] == 500.0

    def test_edit_goal_unlinking_clears_starting_balance(self, auth_client, db_conn, test_user, test_account):
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000", linked_account_id=str(test_account["id"]))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]

        auth_client.post(
            "/settings/edit-goal",
            data={**csrf(), "id": str(goal_id), "name": "Goal", "goal_type": "savings", "target_amount": "1000"},
        )
        row = _get_goal(db_conn, goal_id)
        assert row["linked_account_id"] is None
        assert row["starting_balance"] is None

    def test_edit_goal_keeping_same_account_preserves_starting_balance(self, auth_client, db_conn, test_user, test_account):
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000", linked_account_id=str(test_account["id"]))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]
        original_starting = _get_goal(db_conn, goal_id)["starting_balance"]

        # Balance moves after creation - editing without changing the linked
        # account must NOT re-snapshot it.
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (9999.0, test_account["id"]))
        db_conn.commit()

        auth_client.post(
            "/settings/edit-goal",
            data={**csrf(), "id": str(goal_id), "name": "Goal renamed", "goal_type": "savings", "target_amount": "1000", "linked_account_id": str(test_account["id"])},
        )
        row = _get_goal(db_conn, goal_id)
        assert row["starting_balance"] == original_starting
        assert row["name"] == "Goal renamed"

    def test_delete_goal_does_not_touch_account_or_transactions(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
            ("2026-01-01", "Some tx", 50.0, test_account["name"], test_user["id"], "manual", "Other"),
        )
        db_conn.commit()
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000", linked_account_id=str(test_account["id"]))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]

        resp = auth_client.post("/settings/delete-goal", data={**csrf(), "id": str(goal_id)}, follow_redirects=False)
        assert resp.status_code == 302
        assert _get_goal(db_conn, goal_id) is None
        # Account and its transaction are completely untouched
        acc = db_conn.execute("SELECT * FROM accounts WHERE id=?", (test_account["id"],)).fetchone()
        assert acc is not None
        assert acc["balance"] == test_account["balance"]
        tx_count = db_conn.execute("SELECT COUNT(*) c FROM transactions WHERE account=?", (test_account["name"],)).fetchone()["c"]
        assert tx_count == 1

    def test_delete_goal_also_deletes_its_contributions(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]
        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "50", "date": "2026-01-01"})

        auth_client.post("/settings/delete-goal", data={**csrf(), "id": str(goal_id)})
        assert db_conn.execute("SELECT COUNT(*) c FROM goal_contributions WHERE goal_id=?", (goal_id,)).fetchone()["c"] == 0

    def test_manual_complete_and_reopen_toggle(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]

        resp = auth_client.post("/settings/complete-goal", data={**csrf(), "id": str(goal_id)}, follow_redirects=False)
        assert resp.status_code == 302
        row = _get_goal(db_conn, goal_id)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

        # Toggling again reopens it
        auth_client.post("/settings/complete-goal", data={**csrf(), "id": str(goal_id)})
        row2 = _get_goal(db_conn, goal_id)
        assert row2["status"] == "active"
        assert row2["completed_at"] is None

    def test_manual_complete_works_even_far_from_target(self, auth_client, db_conn, test_user):
        """Marking achieved is independent of actual progress - a user can
        close a goal out early."""
        _create_goal_via_route(auth_client, name="Goal", target_amount="1000000")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Goal", test_user["id"])).fetchone()["id"]
        auth_client.post("/settings/complete-goal", data={**csrf(), "id": str(goal_id)})
        assert _get_goal(db_conn, goal_id)["status"] == "completed"

    def test_automatic_completion_when_linked_balance_reaches_target(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=90.0)
        _create_goal_via_route(auth_client, name="Small goal", target_amount="100", linked_account_id=str(acc_id))
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Small goal", test_user["id"])).fetchone()["id"]
        assert _get_goal(db_conn, goal_id)["status"] == "active"

        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (100.0, acc_id))
        db_conn.commit()

        # Auto-completion happens the next time progress is computed, i.e.
        # on the next /manage load.
        resp = auth_client.get("/manage?tab=goals")
        assert resp.status_code == 200
        row = _get_goal(db_conn, goal_id)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_automatic_completion_for_standalone_via_contributions(self, auth_client, db_conn, test_user):
        _create_goal_via_route(auth_client, name="Small goal", target_amount="100")
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Small goal", test_user["id"])).fetchone()["id"]

        auth_client.post("/settings/add-goal-contribution", data={**csrf(), "goal_id": str(goal_id), "amount": "100", "date": "2026-01-01"})
        auth_client.get("/manage?tab=goals")
        assert _get_goal(db_conn, goal_id)["status"] == "completed"

    def test_edit_goal_requires_csrf(self, client):
        resp = client.post("/settings/edit-goal", data={"id": "1", "name": "x", "goal_type": "savings", "target_amount": "10"})
        assert resp.status_code in (302, 401, 403)

    def test_delete_goal_requires_csrf(self, client):
        resp = client.post("/settings/delete-goal", data={"id": "1"})
        assert resp.status_code in (302, 401, 403)


# ── 5. ACCOUNT LOCKING INTERACTION ───────────────────────────────────────────
class TestAccountLockingInteraction:
    def test_goal_linked_to_locked_account_flags_locked(self, auth_client, db_conn, test_user):
        """A locked account (Pro-to-Free downgrade) is frozen and excluded
        from most calculations elsewhere in the app. A goal linked to one
        isn't deleted or hidden - its progress still reads the account's
        (frozen) balance as-is, but the UI is told the account is locked so
        it can flag progress as potentially stale, matching how locked
        accounts are surfaced everywhere else (e.g. manage.html's account
        list, dropdowns) rather than silently pretending nothing changed."""
        acc_id = _add_account(db_conn, test_user["id"], "Locked Savings", balance=4000.0, is_locked=1)
        # Route-level creation would reject nothing here since linking to a
        # locked account is read-only and harmless (unlike bills/income/
        # transfers, which would try to write against it) - insert directly
        # to simulate a goal that was linked before the account got locked,
        # which is the realistic path this normally happens via anyway.
        db_conn.execute(
            "INSERT INTO goals (user_id, name, goal_type, target_amount, linked_account_id, starting_balance) "
            "VALUES (?,?,?,?,?,?)",
            (test_user["id"], "Locked goal", "savings", 10000.0, acc_id, 4000.0),
        )
        db_conn.commit()
        goal_id = db_conn.execute("SELECT id FROM goals WHERE name=? AND user_id=?", ("Locked goal", test_user["id"])).fetchone()["id"]

        import app as app_module
        goal = dict(_get_goal(db_conn, goal_id))
        progress = app_module._compute_goal_progress(goal, test_user["id"])

        assert progress["account_locked"] is True
        assert progress["is_linked"] is True
        # Progress is still computed from the frozen balance, not hidden/errored
        assert progress["progress_amount"] == 4000.0
        assert progress["progress_pct"] == 40.0

    def test_locked_goal_renders_without_error_on_manage_page(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Locked Savings", balance=4000.0, is_locked=1)
        db_conn.execute(
            "INSERT INTO goals (user_id, name, goal_type, target_amount, linked_account_id, starting_balance) "
            "VALUES (?,?,?,?,?,?)",
            (test_user["id"], "Locked goal", "savings", 10000.0, acc_id, 4000.0),
        )
        db_conn.commit()

        resp = auth_client.get("/manage?tab=goals")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Locked goal" in body
        assert "locked" in body.lower()

    def test_goal_can_be_newly_linked_to_an_already_locked_account(self, auth_client, db_conn, test_user):
        """Deliberate judgement call: linking a NEW goal to an already-locked
        account is allowed, since it's read-only against that account
        (unlike spending/transferring), and immediately surfaces the locked
        flag rather than being blocked outright."""
        acc_id = _add_account(db_conn, test_user["id"], "Locked Savings", balance=2500.0, is_locked=1)
        resp = _create_goal_via_route(auth_client, name="New locked-linked goal", target_amount="5000", linked_account_id=str(acc_id))
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM goals WHERE name=? AND user_id=?", ("New locked-linked goal", test_user["id"])).fetchone()
        assert row is not None
        assert row["linked_account_id"] == acc_id
