"""
Tests for the Goals entry point added to the Home page, positioned directly
below the existing "Can I afford this?" button.

Three states:
  - No active goals -> a full-width CTA button matching the exact visual
    style of "Can I afford this?" ("Set a savings or debt goal").
  - One active goal -> a card (matching Home's other card styling) showing
    the goal's name, £ progress, and percentage.
  - 2+ active goals -> the same card style, capped at 3 rows (name + % only,
    no £ amounts, to stay compact) with a "+N more goals" line for the rest.

Deliberately positioned OUTSIDE the has_funds/no_funds branch that "Can I
afford this?" lives inside — a user with an active debt-repayment goal but
no positive account balance anywhere is exactly the audience a "hide behind
any account > £0" check would wrongly exclude, so this was moved out after
a render test caught it during development.
"""
from tests.conftest import csrf


def _add_goal(db_conn, user_id, name, target, goal_type="savings", linked_account_id=None,
               starting_balance=None, status="active"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO goals (user_id, name, goal_type, target_amount, linked_account_id, starting_balance, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, name, goal_type, target, linked_account_id, starting_balance, status),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current", is_locked=0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, is_locked, include_in_overview) "
        "VALUES (?, ?, ?, 1, ?, ?, 1)",
        (name, balance, acc_type, user_id, is_locked),
    )
    db_conn.commit()
    return cur.lastrowid


class TestNoGoalsState:
    def test_shows_cta_with_funded_account(self, auth_client, test_account):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Set a savings or debt goal" in body
        assert '/manage?tab=goals' in body

    def test_shows_cta_with_no_accounts_at_all(self, auth_client):
        """The has_funds=False onboarding branch must not hide the Goals CTA."""
        resp = auth_client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Set a savings or debt goal" in body

    def test_cta_styled_like_can_i_afford_this_button(self, auth_client, test_account):
        """Spec: match the existing Can I afford this button's visual style."""
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        afford_idx = body.find("💬 Can I afford this?")
        goal_idx = body.find("🎯 Set a savings or debt goal")
        assert afford_idx != -1 and goal_idx != -1
        # Walk back to the start of the enclosing opening tag (the last '<' before the text)
        afford_tag = body[body.rfind("<", 0, afford_idx):afford_idx]
        goal_tag = body[body.rfind("<", 0, goal_idx):goal_idx]
        # Same button classes and brand background treatment
        assert "btn w-100 rounded-4 py-2 fw-bold" in afford_tag
        assert "btn w-100 rounded-4 py-2 fw-bold" in goal_tag
        assert "background:var(--brand)" in afford_tag
        assert "background:var(--brand)" in goal_tag


class TestSingleGoalState:
    def test_shows_name_amount_and_percentage(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (3000.0, test_account["id"]))
        db_conn.commit()
        _add_goal(db_conn, test_user["id"], "House deposit", 10000.0, linked_account_id=test_account["id"], starting_balance=3000.0)

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "House deposit" in body
        assert "30.0%" in body
        assert "3000" in body and "10000" in body
        assert '/manage?tab=goals' in body

    def test_debt_goal_renders_correctly_on_home(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Car Loan", balance=-5000.0)
        _add_goal(db_conn, test_user["id"], "Pay off car", 8000.0, goal_type="debt",
                  linked_account_id=acc_id, starting_balance=-8000.0)

        resp = auth_client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Pay off car" in body
        assert "37.5%" in body  # (8000 - 5000) / 8000

    def test_standalone_goal_with_contributions_renders_on_home(self, auth_client, db_conn, test_user):
        goal_id = _add_goal(db_conn, test_user["id"], "Holiday fund", 500.0)
        db_conn.execute(
            "INSERT INTO goal_contributions (goal_id, user_id, amount, date) VALUES (?,?,?,?)",
            (goal_id, test_user["id"], 100.0, "2026-01-01"),
        )
        db_conn.commit()

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "Holiday fund" in body
        assert "20.0%" in body


class TestMultiGoalState:
    def test_two_goals_no_overflow_line(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "GoalA", 1000.0)
        _add_goal(db_conn, test_user["id"], "GoalB", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "GoalA" in body
        assert "GoalB" in body
        assert "more goal" not in body

    def test_exactly_three_goals_no_overflow_line(self, auth_client, db_conn, test_user):
        for name in ("GoalA", "GoalB", "GoalC"):
            _add_goal(db_conn, test_user["id"], name, 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert all(n in body for n in ("GoalA", "GoalB", "GoalC"))
        assert "more goal" not in body

    def test_five_goals_caps_at_three_with_overflow_count(self, auth_client, db_conn, test_user):
        for i in range(5):
            _add_goal(db_conn, test_user["id"], f"Goal{i}", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Goal0" in body
        assert "Goal1" in body
        assert "Goal2" in body
        assert "Goal3" not in body
        assert "Goal4" not in body
        assert "+2 more goals" in body

    def test_multi_goal_rows_omit_pound_amounts_to_stay_compact(self, auth_client, db_conn, test_user):
        """Only the single-goal case shows full £X / £Y detail - 2+ goals
        show name + percentage only, to avoid crowding/wrapping on mobile."""
        _add_goal(db_conn, test_user["id"], "GoalA", 1234.0)
        _add_goal(db_conn, test_user["id"], "GoalB", 5678.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "1234" not in body
        assert "5678" not in body
        assert "0.0%" in body  # both at 0% progress, shown compactly

    def test_singular_more_goal_grammar(self, auth_client, db_conn, test_user):
        for i in range(4):
            _add_goal(db_conn, test_user["id"], f"Goal{i}", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "+1 more goal" in body
        assert "+1 more goals" not in body


class TestGoalStatusFiltering:
    def test_completed_goals_excluded_from_home_summary(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "Done goal", 100.0, status="completed")
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        # No active goals -> CTA state, not the completed one shown as a summary row
        assert "Set a savings or debt goal" in body
        assert "Done goal" not in body

    def test_mix_of_active_and_completed_only_shows_active(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "Active goal", 100.0, status="active")
        _add_goal(db_conn, test_user["id"], "Done goal", 100.0, status="completed")
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "Active goal" in body
        assert "Done goal" not in body


class TestLockedAccountOnHome:
    def test_goal_linked_to_locked_account_renders_without_error(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Locked Savings", balance=4000.0, is_locked=1)
        _add_goal(db_conn, test_user["id"], "Locked goal", 10000.0, linked_account_id=acc_id, starting_balance=4000.0)

        resp = auth_client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Locked goal" in body
        assert "40.0%" in body


class TestHomeGoalsIsolatedFromOtherUsers:
    def test_only_shows_current_users_goals(self, auth_client, db_conn, test_user):
        import uuid
        from werkzeug.security import generate_password_hash

        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?,?,?,?,?)",
            (other_email, generate_password_hash("x"), "2026-01-01", 1, "Other"),
        )
        db_conn.commit()
        other_id = cur.lastrowid
        _add_goal(db_conn, other_id, "Someone else's goal", 100.0)

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "Someone else's goal" not in body
        assert "Set a savings or debt goal" in body

        db_conn.execute("DELETE FROM goals WHERE user_id=?", (other_id,))
        db_conn.execute("DELETE FROM users WHERE id=?", (other_id,))
        db_conn.commit()
