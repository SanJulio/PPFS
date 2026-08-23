"""
Tests for the Goal Contribution Engine's backend foundations (August 2026,
Stage 1 — no UI/persistence yet, see the follow-up stages for
/settings/set-goal-commitment and the slider template).

Core mechanic under test here: given a candidate £/cycle contribution
amount for a goal, /api/goal-commitment-preview reports (a) the resulting
Safe to Spend for the current cycle and (b) the projected completion date
that amount would imply — reusing _project_goal_completion(), the same
function every other goal-pace display already uses, fed a hypothetical
pace derived from the slider amount rather than real/estimated history.

Decisions this build encodes (confirmed with the user before implementing):
  - One mechanism for both goal types — no separate debt-goal system.
  - A debt goal with a known minimum_payment gets a hard floor at that
    value and defaults to the suggested pace (never a bare 0); a debt goal
    with no known minimum, and every savings goal, floors at 0.
  - The slider's default max is capped well below 100% of Safe to Spend.
  - Snapping is to the nearest £5.
  - Deliberately projection-only: nothing here ever creates a real
    transaction, moves a real balance, or logs a goal_contributions row.
"""
import datetime

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead)).isoformat()


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
        (name, balance, acc_type, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_goal(db_conn, user_id, name, target, goal_type="savings", target_date=None,
              linked_account_id=None, starting_balance=None, minimum_payment=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance, minimum_payment) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, name, goal_type, target, target_date, linked_account_id, starting_balance, minimum_payment),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_savings_rule(db_conn, user_id, name, amount, day, from_account, to_account, goal_id=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id, goal_id) VALUES (?,?,?,?,?,?,?,?)",
        (name, amount, day, "monthly", from_account, to_account, user_id, goal_id),
    )
    db_conn.commit()
    return cur.lastrowid


# ── 1. _snap_to_increment() ──────────────────────────────────────────────────
class TestSnapToIncrement:
    def test_nearest_rounds_to_closer_multiple(self, app):
        import app as app_module
        assert app_module._snap_to_increment(12.0) == 10.0
        assert app_module._snap_to_increment(13.0) == 15.0
        assert app_module._snap_to_increment(17.5) == 20.0  # round-half-up territory

    def test_up_never_understates(self, app):
        import app as app_module
        assert app_module._snap_to_increment(11.0, mode="up") == 15.0
        assert app_module._snap_to_increment(10.0, mode="up") == 10.0  # already exact

    def test_down_never_overstates(self, app):
        import app as app_module
        assert app_module._snap_to_increment(14.0, mode="down") == 10.0
        assert app_module._snap_to_increment(10.0, mode="down") == 10.0  # already exact

    def test_zero_and_none(self, app):
        import app as app_module
        assert app_module._snap_to_increment(0) == 0.0
        assert app_module._snap_to_increment(None) is None

    def test_negative_clamped_to_zero(self, app):
        import app as app_module
        assert app_module._snap_to_increment(-5.0) == 0.0

    def test_custom_increment(self, app):
        import app as app_module
        assert app_module._snap_to_increment(23.0, increment=10.0) == 20.0
        assert app_module._snap_to_increment(27.0, increment=10.0) == 30.0


# ── 2. _compute_goal_commitment_bounds() ─────────────────────────────────────
class TestCommitmentBounds:
    def _progress(self, amount=0.0, target=1000.0):
        return {"progress_amount": amount, "target_amount": target, "progress_pct": 0.0,
                "raw_ratio": 0.0, "is_linked": False, "account_name": None, "account_locked": False}

    def test_debt_goal_with_minimum_floors_at_minimum(self, app):
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 50.0}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["floor"] == 50.0
        assert bounds["has_minimum_payment"] is True

    def test_debt_goal_minimum_wins_when_no_suggested_pace(self, app):
        """No target date -> no _suggest_goal_pace figure -> default falls
        back to the minimum itself, never a bare 0."""
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 75.0}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["default"] == 75.0

    def test_debt_goal_default_uses_suggested_pace_when_above_minimum(self, app):
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 50.0}
        pace = {"monthly_pace": 200.0, "remaining_amount": 1000.0, "days_remaining": 150, "overdue": False}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), pace, safe_to_spend=1000.0)
        assert bounds["default"] == 200.0
        assert bounds["floor"] == 50.0  # still floors at the real minimum

    def test_debt_goal_default_uses_minimum_when_suggested_pace_below_it(self, app):
        """A leisurely suggested pace (e.g. a far-off target date) must
        never suggest committing LESS than a real required minimum."""
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 100.0}
        pace = {"monthly_pace": 20.0, "remaining_amount": 1000.0, "days_remaining": 1500, "overdue": False}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), pace, safe_to_spend=1000.0)
        assert bounds["default"] == 100.0
        assert bounds["floor"] == 100.0

    def test_debt_goal_without_minimum_behaves_like_savings(self, app):
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["floor"] == 0.0
        assert bounds["default"] == 0.0
        assert bounds["has_minimum_payment"] is False

    def test_savings_goal_always_floors_at_zero(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["floor"] == 0.0
        assert bounds["has_minimum_payment"] is False

    def test_savings_goal_defaults_to_suggested_pace_when_available(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        pace = {"monthly_pace": 150.0, "remaining_amount": 1000.0, "days_remaining": 200, "overdue": False}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), pace, safe_to_spend=1000.0)
        assert bounds["default"] == 150.0

    def test_no_target_date_falls_back_to_real_pace_not_zero(self, app):
        """_suggest_goal_pace() only returns a figure when a target date is
        set - without one, the slider used to silently default to 0 even
        when a perfectly good real/estimated pace figure already exists
        (what the old UI showed as "around £X/month"). It should be used
        as the default instead of a bare 0."""
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(
            goal, self._progress(), None, safe_to_spend=1000.0, fallback_pace_per_day=10.0,
        )
        assert bounds["default"] == app_module._snap_to_increment(10.0 * 30.44)
        assert bounds["default"] > 0

    def test_target_date_suggestion_takes_priority_over_fallback_pace(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        pace = {"monthly_pace": 150.0, "remaining_amount": 1000.0, "days_remaining": 200, "overdue": False}
        bounds = app_module._compute_goal_commitment_bounds(
            goal, self._progress(), pace, safe_to_spend=1000.0, fallback_pace_per_day=999.0,
        )
        assert bounds["default"] == 150.0

    def test_no_suggestion_and_no_fallback_pace_defaults_to_zero(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(
            goal, self._progress(), None, safe_to_spend=1000.0, fallback_pace_per_day=None,
        )
        assert bounds["default"] == 0.0

    def test_negative_or_zero_fallback_pace_ignored(self, app):
        """A negative real pace (things moving the wrong way) shouldn't
        become a negative or zero-implying default - falls through to 0
        the same as having no fallback pace at all."""
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(
            goal, self._progress(), None, safe_to_spend=1000.0, fallback_pace_per_day=-5.0,
        )
        assert bounds["default"] == 0.0

    def test_overdue_pace_treated_as_no_suggestion(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        pace = {"monthly_pace": None, "remaining_amount": 1000.0, "days_remaining": -10, "overdue": True}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), pace, safe_to_spend=1000.0)
        assert bounds["default"] == 0.0

    def test_max_is_capped_below_full_safe_to_spend(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["max"] == 500.0  # 50% of 1000, the documented default cap
        assert bounds["max"] < 1000.0

    def test_max_has_a_modest_floor_for_low_safe_to_spend(self, app):
        """Even with very little Safe to Spend, the range shouldn't
        collapse to something unusably small."""
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=10.0)
        assert bounds["max"] == 50.0  # the £50 baseline, not 50% of 10 = 5

    def test_max_extends_to_cover_a_large_real_minimum(self, app):
        """A real minimum payment must never sit above the slider's own
        max - the range has to be able to show it."""
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 800.0}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=1000.0)
        assert bounds["max"] >= bounds["floor"]
        assert bounds["max"] >= 800.0

    def test_bounds_are_snapped(self, app):
        import app as app_module
        goal = {"goal_type": "debt", "minimum_payment": 52.0}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=997.0)
        assert bounds["floor"] % 5 == 0
        assert bounds["max"] % 5 == 0
        assert bounds["default"] % 5 == 0

    def test_negative_safe_to_spend_treated_as_zero(self, app):
        import app as app_module
        goal = {"goal_type": "savings", "minimum_payment": None}
        bounds = app_module._compute_goal_commitment_bounds(goal, self._progress(), None, safe_to_spend=-500.0)
        assert bounds["max"] == 50.0  # falls back to the modest baseline, not negative


# ── 3. _get_goal_commitment() ────────────────────────────────────────────────
class TestGetGoalCommitment:
    def test_returns_none_when_no_commitment_set(self, auth_client, db_conn, test_user, test_account):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        assert app_module._get_goal_commitment(gid, test_user["id"]) is None

    def test_returns_the_linked_rule_when_one_exists(self, auth_client, db_conn, test_user, test_account, second_account):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        _add_savings_rule(db_conn, test_user["id"], "House deposit commitment", 100.0, 1,
                           test_account["name"], second_account["name"], goal_id=gid)
        rule = app_module._get_goal_commitment(gid, test_user["id"])
        assert rule is not None
        assert rule["amount"] == 100.0
        assert rule["goal_id"] == gid

    def test_does_not_return_another_users_commitment(self, db_conn, test_user, app):
        import app as app_module
        import uuid
        from werkzeug.security import generate_password_hash
        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
            (other_email, generate_password_hash("TestPass1!"), "2026-01-01", "Other"),
        )
        other_uid = cur.lastrowid
        db_conn.commit()
        gid = _add_goal(db_conn, other_uid, "Not yours", 1000.0)
        _add_savings_rule(db_conn, other_uid, "Not yours commitment", 50.0, 1, "A", "B", goal_id=gid)
        assert app_module._get_goal_commitment(gid, test_user["id"]) is None

    def test_ordinary_savings_rule_without_goal_id_is_not_returned_for_any_goal(self, auth_client, db_conn, test_user, test_account, second_account):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        _add_savings_rule(db_conn, test_user["id"], "Unrelated rule", 20.0, 1,
                           test_account["name"], second_account["name"], goal_id=None)
        assert app_module._get_goal_commitment(gid, test_user["id"]) is None


# ── 4. POST /api/goal-commitment-preview ─────────────────────────────────────
class TestGoalCommitmentPreviewRoute:
    def test_requires_csrf(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={"goal_id": gid, "amount": 50.0})
        assert resp.status_code == 403

    def test_404_for_nonexistent_goal(self, auth_client, test_account):
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": 999999, "amount": 50.0})
        assert resp.status_code == 404

    def test_404_for_another_users_goal(self, db_conn, test_user, test_account, auth_client):
        import uuid
        from werkzeug.security import generate_password_hash
        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
            (other_email, generate_password_hash("TestPass1!"), "2026-01-01", "Other"),
        )
        other_uid = cur.lastrowid
        db_conn.commit()
        gid = _add_goal(db_conn, other_uid, "Not yours", 1000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 50.0})
        assert resp.status_code == 404

    def test_resulting_safe_to_spend_is_current_minus_amount(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 150.0})
        data = resp.get_json()
        assert data["resulting_safe_to_spend"] == pytest.approx(test_account["balance"] - 150.0)

    def test_would_go_negative_flag(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp_ok = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 10.0})
        resp_over = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": test_account["balance"] + 100.0})
        assert resp_ok.get_json()["would_go_negative"] is False
        assert resp_over.get_json()["would_go_negative"] is True

    def test_zero_amount_gives_no_progress_state(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 0})
        data = resp.get_json()
        assert data["projection"]["state"] == "no_progress"

    def test_positive_amount_produces_a_projected_date(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 100.0})
        data = resp.get_json()
        assert data["projection"]["state"] == "projected"
        assert data["projection"]["projected_date"] is not None
        assert data["projection"]["is_estimate"] is False

    def test_larger_amount_projects_a_sooner_completion(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        small = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 50.0}).get_json()
        large = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 500.0}).get_json()
        assert large["projection"]["projected_date"] < small["projection"]["projected_date"]

    def test_response_includes_bounds(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 50.0})
        data = resp.get_json()
        assert "floor" in data["bounds"]
        assert "default" in data["bounds"]
        assert "max" in data["bounds"]

    def test_debt_goal_with_minimum_payment_reflected_in_bounds(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0, minimum_payment=40.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 40.0})
        data = resp.get_json()
        assert data["bounds"]["floor"] == 40.0
        assert data["bounds"]["has_minimum_payment"] is True

    def test_reuses_project_goal_completion_directly(self, auth_client, db_conn, test_user, test_account):
        """Cross-check: calling _project_goal_completion() directly with the
        same inputs the route derives must give byte-identical output -
        proof the route isn't duplicating date-math of its own."""
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 120.0})
        data = resp.get_json()

        progress = {"progress_amount": 0.0, "target_amount": 5000.0, "progress_pct": 0.0,
                    "raw_ratio": 0.0, "is_linked": False, "account_name": None, "account_locked": False}
        expected = app_module._project_goal_completion(progress, 120.0 / 30.44, None, is_estimate=False)
        assert data["projection"] == expected


# ── 5. Nothing here is persisted or executed (projection-only, confirmed) ────
class TestProjectionOnlyNoSideEffects:
    def test_preview_does_not_create_a_savings_rule(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        before = db_conn.execute("SELECT COUNT(*) as c FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()["c"]
        auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 100.0})
        after = db_conn.execute("SELECT COUNT(*) as c FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()["c"]
        assert before == after == 0

    def test_preview_does_not_log_a_contribution(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        before = db_conn.execute("SELECT COUNT(*) as c FROM goal_contributions WHERE goal_id=?", (gid,)).fetchone()["c"]
        auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 100.0})
        after = db_conn.execute("SELECT COUNT(*) as c FROM goal_contributions WHERE goal_id=?", (gid,)).fetchone()["c"]
        assert before == after == 0

    def test_preview_does_not_change_account_balance(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        auth_client.post("/api/goal-commitment-preview", json={**csrf(), "goal_id": gid, "amount": 100.0})
        row = db_conn.execute("SELECT balance FROM accounts WHERE id=?", (test_account["id"],)).fetchone()
        assert float(row["balance"]) == test_account["balance"]


# ── 6. POST /settings/set-goal-commitment (Stage 2: persistence) ────────────
class TestSetGoalCommitment:
    def test_creates_commitment_for_linked_savings_goal_credits_destination(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "tab=goals" in resp.headers["Location"]
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule is not None
        assert rule["amount"] == 150.0
        assert rule["from_account"] == test_account["name"]
        assert rule["to_account"] == second_account["name"]

    def test_creates_commitment_for_debt_goal_no_destination_credited(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule["to_account"] == ""

    def test_creates_commitment_for_standalone_goal_no_destination_credited(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "Emergency fund", 1000.0)  # no linked_account_id
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule["to_account"] == ""

    def test_updates_existing_commitment_rather_than_duplicating(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": test_account["name"],
        })
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "200", "from_account": test_account["name"],
        })
        rules = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchall()
        assert len(rules) == 1
        assert rules[0]["amount"] == 200.0

    def test_zero_amount_removes_existing_commitment(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is not None
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "0", "from_account": test_account["name"],
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_zero_amount_with_no_existing_commitment_is_a_no_op(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "0", "from_account": test_account["name"],
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_missing_from_account_rejected(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": "",
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_locked_from_account_rejected(self, auth_client, db_conn, test_user, test_account):
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (test_account["id"],))
        db_conn.commit()
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": test_account["name"],
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_debt_goal_amount_below_minimum_payment_rejected(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0, minimum_payment=50.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "30", "from_account": test_account["name"],
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_debt_goal_amount_at_minimum_payment_accepted(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0, minimum_payment=50.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule is not None
        assert rule["amount"] == 50.0

    def test_nonexistent_goal_rejected(self, auth_client, db_conn, test_account):
        resp = auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": 999999, "amount": "100", "from_account": test_account["name"],
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=999999").fetchone() is None

    def test_another_users_goal_rejected(self, auth_client, db_conn, test_user, test_account):
        import uuid
        from werkzeug.security import generate_password_hash
        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
            (other_email, generate_password_hash("TestPass1!"), "2026-01-01", "Other"),
        )
        other_uid = cur.lastrowid
        db_conn.commit()
        gid = _add_goal(db_conn, other_uid, "Not yours", 1000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": test_account["name"],
        })
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is None

    def test_not_pro_gated_unlike_ordinary_savings_rules(self, auth_client, db_conn, test_user, test_account):
        """Contrast with /settings/add-savings-rule, which redirects
        non-Pro users with PRO_REQUIRED - Goals themselves aren't a Pro
        feature, so neither is their commitment slider."""
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        resp = auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        }, follow_redirects=False)
        assert "PRO_REQUIRED" not in resp.headers.get("Location", "")
        assert db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone() is not None

    def test_linked_account_not_savings_type_leaves_destination_empty(self, auth_client, db_conn, test_user, test_account, second_account):
        """A savings-type goal linked to a non-savings account (e.g. the
        user picked a current account) shouldn't credit it automatically -
        only a real savings account is an unambiguous, safe destination."""
        other_current = _add_account(db_conn, test_user["id"], "Other Current", 200.0, "current")
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=other_current)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule["to_account"] == ""

    def test_locked_linked_savings_account_leaves_destination_empty_at_set_time(self, auth_client, db_conn, test_user, test_account, second_account):
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (second_account["id"],))
        db_conn.commit()
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert rule["to_account"] == ""

    def test_day_anchored_to_current_cycle_start(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "50", "from_account": test_account["name"],
        })
        rule = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        # Default manual cycle start is day 1 for a fresh test user
        assert rule["day"] == 1


# ── 7. Locked-destination pause, all three live engine sites ────────────────
class TestLockedDestinationPausesWholeRule:
    """A goal commitment's destination (the linked savings account) can
    become locked independently of its source - confirms the WHOLE rule
    pauses everywhere it's read, not just the credit side silently
    no-op'ing while the source keeps deducting."""

    def test_overview_safe_to_spend_unaffected_when_destination_locked(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (second_account["id"],))
        db_conn.commit()

        import re
        resp = auth_client.get("/")
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', resp.get_data(as_text=True))
        assert float(m.group(1).replace(",", "")) == pytest.approx(test_account["balance"])

    def test_overview_bills_left_excludes_paused_rule(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (second_account["id"],))
        db_conn.commit()

        import re
        resp = auth_client.get("/")
        m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', resp.get_data(as_text=True))
        assert float(m.group(1).replace(",", "")) == pytest.approx(0.0)

    def test_forecast_balance_unaffected_when_destination_locked(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (second_account["id"],))
        db_conn.commit()

        import re, json, html
        resp = auth_client.get("/forecast")
        body = resp.get_data(as_text=True)
        m = re.search(r'data-snapshots=[\'"](.*?)[\'"]', body, re.DOTALL)
        snapshots = json.loads(html.unescape(m.group(1)))
        # 40 days out, well past a monthly day=1 recurrence, balance should
        # still equal the untouched starting balance since the rule paused.
        future_snap = snapshots[-1]
        assert future_snap[test_account["name"]] == pytest.approx(test_account["balance"])

    def test_snapshot_balance_unaffected_when_destination_locked(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (second_account["id"],))
        db_conn.commit()

        resp = auth_client.get("/api/snapshot?days=40")
        data = resp.get_json()
        assert data["accounts"][test_account["name"]]["balance_on_date"] == pytest.approx(test_account["balance"])
        assert not [b for b in data["bills_due"] if b.get("item_type") == "savings_rule"]

    def test_locked_source_still_pauses_as_before_no_regression(self, auth_client, db_conn, test_user, test_account, second_account):
        """The pre-existing from_account lock behaviour (unrelated to this
        change) must still work after these edits."""
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "150", "from_account": test_account["name"],
        })
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (test_account["id"],))
        db_conn.commit()

        import re
        resp = auth_client.get("/")
        # test_account itself is locked and excluded from spending_balance
        # entirely, so safe_spending should be 0 (no unlocked spending
        # accounts left), not reduced by a still-active commitment.
        m = re.search(r'id="safe-spending-val"[^>]*>£([\d,\.]+)', resp.get_data(as_text=True))
        assert float(m.group(1).replace(",", "")) == pytest.approx(0.0)

    def test_debt_commitment_with_empty_to_account_unaffected_by_this_check(self, auth_client, db_conn, test_user, test_account, second_account):
        """A debt/standalone commitment's to_account is '' by design - the
        new locked-destination check must never mistake that for a locked
        real account and pause a perfectly healthy commitment."""
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0)
        auth_client.post("/settings/set-goal-commitment", data={
            **csrf(), "goal_id": gid, "amount": "100", "from_account": test_account["name"],
        })

        import re
        resp = auth_client.get("/")
        m = re.search(r'id="future-bills-val"[^>]*>£([\d,\.]+)', resp.get_data(as_text=True))
        # The commitment IS active (from_account unlocked, to_account
        # legitimately empty) - it should show up in bills-left if its
        # cycle day falls in-window. Just confirm no crash / sane value.
        assert float(m.group(1).replace(",", "")) >= 0.0


def _goal_section(body, marker, size=5200):
    idx = body.find(marker)
    assert idx != -1, f"{marker!r} not found in page"
    return body[idx:idx + size]


# ── 8. Slider template (Stage 3) ─────────────────────────────────────────────
class TestSliderTemplate:
    def test_slider_renders_for_active_goal(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert 'id="commitSlider' in section
        assert 'id="commitNumber' in section
        assert 'Recurring contribution' in section
        assert '/settings/set-goal-commitment' in section

    def test_no_slider_for_completed_goal(self, auth_client, db_conn, test_user, test_account):
        gid = _add_goal(db_conn, test_user["id"], "Done goal", 500.0)
        db_conn.execute("UPDATE goals SET status='completed' WHERE id=?", (gid,))
        db_conn.commit()
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Done goal", size=2000)
        assert "commitSlider" not in section
        assert "set-goal-commitment" not in section

    def test_old_text_heavy_insight_fully_removed(self, auth_client, db_conn, test_user, test_account, second_account):
        """The exact phrases the brief named for removal must be gone from
        the goal card itself. Scoped to the card (not the whole page body)
        because the separate Add/Edit Goal modal's target-date pace
        suggestion - a distinct, still-existing, correctly untouched
        feature - has its own unrelated "Suggested pace:" JS template
        string that would otherwise false-positive this check."""
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert "Based on your typical Safe to Spend" not in section
        assert "Suggested pace:" not in section
        assert "You're on track to hit your target" not in section
        assert "month more" not in section
        assert "months behind target" not in section

    def test_slider_bounds_match_computed_bounds(self, auth_client, db_conn, test_user, test_account, second_account):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                         linked_account_id=second_account["id"], starting_balance=-2000.0, minimum_payment=40.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Pay off card")
        assert f'min="{app_module._snap_to_increment(40.0, mode="up")}"' in section

    def test_minimum_payment_note_shown_when_applicable(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_goal(db_conn, test_user["id"], "Pay off card", 2000.0, goal_type="debt",
                  linked_account_id=second_account["id"], starting_balance=-2000.0, minimum_payment=40.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Pay off card")
        assert "Minimum payment: £40.00" in section

    def test_minimum_payment_note_absent_for_savings_goal(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert "Minimum payment" not in section

    def test_existing_commitment_prefills_slider_value_and_account(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        _add_savings_rule(db_conn, test_user["id"], "Weekly savings top-up", 175.0, 1,
                           test_account["name"], second_account["name"], goal_id=gid)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert 'value="175.0"' in section
        assert f'value="{test_account["name"]}" selected' in section
        assert ">Update commitment<" in section

    def test_no_existing_commitment_shows_set_label_and_no_remove_button(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert ">Set commitment<" in section
        assert "Remove commitment" not in section

    def test_remove_button_shown_when_commitment_exists(self, auth_client, db_conn, test_user, test_account, second_account):
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        _add_savings_rule(db_conn, test_user["id"], "Weekly savings top-up", 175.0, 1,
                           test_account["name"], second_account["name"], goal_id=gid)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert "Remove commitment" in section

    def test_from_account_select_excludes_locked_accounts(self, auth_client, db_conn, test_user, test_account, second_account):
        locked_id = _add_account(db_conn, test_user["id"], "Locked Current", 100.0, "current")
        db_conn.execute("UPDATE accounts SET is_locked=1 WHERE id=?", (locked_id,))
        db_conn.commit()
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert "Locked Current" not in section

    def test_from_account_select_excludes_savings_accounts(self, auth_client, db_conn, test_user, test_account, second_account):
        """Only current/cash accounts can fund a commitment - savings
        accounts (including the goal's own linked one) shouldn't appear as
        a source option."""
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")
        assert f'value="{second_account["name"]}"' not in section.split('name="from_account"')[1].split("</select>")[0]

    def test_server_rendered_initial_preview_matches_helper_directly(self, auth_client, db_conn, test_user, test_account, second_account):
        """The initial (pre-JS) preview text shown on page load must be
        computed by the exact same helper the live-drag route uses, not a
        separately hand-written value that could drift."""
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House deposit")

        bounds = app_module._compute_goal_commitment_bounds(
            {"goal_type": "savings", "minimum_payment": None},
            {"progress_amount": second_account["balance"], "target_amount": 5000.0},
            None, test_account["balance"],
        )
        progress = {"progress_amount": second_account["balance"], "target_amount": 5000.0,
                    "progress_pct": 0.0, "raw_ratio": 0.0, "is_linked": True,
                    "account_name": second_account["name"], "account_locked": False}
        expected = app_module._compute_goal_commitment_preview(progress, None, bounds["default"], test_account["balance"])
        assert f'£{expected["resulting_safe_to_spend"]:.2f}' in section

    def test_new_helpers_present_in_page_js(self, auth_client, db_conn, test_user, test_account, second_account):
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, linked_account_id=second_account["id"])
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert "function onGoalCommitSlide" in body
        assert "function onGoalCommitNumber" in body
        assert "function _updateGoalCommitPreview" in body
        assert "/api/goal-commitment-preview" in body
