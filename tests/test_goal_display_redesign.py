"""
Tests for the Goals card visual redesign (icon tile, type pill, overflow
menu, thicker colored progress bar, target-vs-pace stat boxes). Pure
presentation layer — these tests deliberately do NOT re-verify progress %,
pace, or projection math (already covered by test_goals.py,
test_goal_pace_projection.py, test_goal_fallback_pace.py); they only check
that _pick_goal_icon()/_build_goal_display() derive sensible display values
from already-computed data, and that the redesigned markup renders and
still wires up to the same existing routes.
"""
import datetime

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
              linked_account_id=None, starting_balance=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, name, goal_type, target, target_date, linked_account_id, starting_balance),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_tx(db_conn, user_id, account, amount, days_ago, tx_type="income"):
    db_conn.execute(
        "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
        (_iso(-days_ago), "test", amount, account, user_id, tx_type, "Other"),
    )
    db_conn.commit()


# ── 1. ICON SELECTION ────────────────────────────────────────────────────────
class TestPickGoalIcon:
    # `app` (the session-scoped Flask app fixture) is taken as a dependency
    # purely to force conftest's `database.USE_POSTGRES = False` patch to run
    # BEFORE app.py is first imported here - importing app.py at module level
    # in this file would race that patch and wrongly cache USE_POSTGRES=True
    # for the whole test session (app.py is only ever imported once).
    def test_trip_matches_plane(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("Trip to Japan", "savings") == "✈️"

    def test_house_deposit_matches_house(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("House deposit", "savings") == "🏠"

    def test_emergency_fund_matches_shield(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("Emergency fund", "savings") == "🛡️"

    def test_car_loan_matches_car(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("Car loan", "debt") == "🚗"

    def test_unmatched_savings_falls_back_to_piggy_bank(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("Xyzzy fund", "savings") == "🐷"

    def test_unmatched_debt_falls_back_to_trending_down(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("Xyzzy repayment", "debt") == "📉"

    def test_case_insensitive_and_none_name_safe(self, app):
        import app as app_module
        assert app_module._pick_goal_icon("TRIP TO ROME", "savings") == "✈️"
        assert app_module._pick_goal_icon(None, "savings") == "🐷"


# ── 2. PRESENTATION DERIVATION ───────────────────────────────────────────────
class TestBuildGoalDisplay:
    def _goal(self, **overrides):
        g = {
            "id": 1,
            "name": "Test goal",
            "goal_type": "savings",
            "status": "active",
            "target_date": None,
            "progress": {"progress_amount": 100.0, "target_amount": 1000.0, "progress_pct": 10.0,
                         "raw_ratio": 0.1, "is_linked": False, "account_name": None, "account_locked": False},
            "pace": None,
            "projection": None,
        }
        g.update(overrides)
        return g

    def test_savings_type_pill_is_green_toned(self, app):
        import app as app_module
        d = app_module._build_goal_display(self._goal(goal_type="savings"))
        assert d["type_label"] == "Savings"
        assert d["type_bg"] == "#dcfce7"

    def test_debt_type_pill_is_red_toned(self, app):
        import app as app_module
        d = app_module._build_goal_display(self._goal(goal_type="debt"))
        assert d["type_label"] == "Debt repayment"
        assert d["type_bg"] == "#fee2e2"

    def test_completed_goal_bar_is_green(self, app):
        import app as app_module
        d = app_module._build_goal_display(self._goal(status="completed"))
        assert d["bar_color"] == "#198754"

    def test_on_track_projection_bar_is_green(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": "green", "on_track": True,
                "is_estimate": False, "pace_per_day": 10.0, "days_over_target": None}
        d = app_module._build_goal_display(self._goal(target_date=_iso(200), projection=proj))
        assert d["bar_color"] == "#198754"

    def test_behind_projection_bar_is_amber_or_red(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": "amber", "on_track": False,
                "is_estimate": False, "pace_per_day": 5.0, "days_over_target": 20}
        d = app_module._build_goal_display(self._goal(target_date=_iso(30), projection=proj))
        assert d["bar_color"] == "#f59e0b"

        proj["status_color"] = "red"
        proj["days_over_target"] = 90
        d = app_module._build_goal_display(self._goal(target_date=_iso(30), projection=proj))
        assert d["bar_color"] == "#dc3545"

    def test_no_target_date_bar_uses_brand_color(self, app):
        import app as app_module
        proj = {"state": "insufficient_data", "status_color": None, "on_track": None,
                "is_estimate": False, "pace_per_day": None, "days_over_target": None}
        d = app_module._build_goal_display(self._goal(projection=proj))
        assert d["bar_color"] == "var(--brand)"

    def test_estimate_pace_label_is_estimated(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": "amber", "on_track": False,
                "is_estimate": True, "pace_per_day": 5.0, "days_over_target": 10}
        d = app_module._build_goal_display(self._goal(target_date=_iso(30), projection=proj))
        assert d["pace_label"] == "Estimated"

    def test_real_pace_label_is_at_current_pace(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": "green", "on_track": True,
                "is_estimate": False, "pace_per_day": 20.0, "days_over_target": None}
        d = app_module._build_goal_display(self._goal(target_date=_iso(200), projection=proj))
        assert d["pace_label"] == "At current pace"

    def test_months_behind_and_extra_monthly_needed_math(self, app):
        import app as app_module
        # 61 days over target -> 2.0 months (61/30.44 rounds to 2.0)
        # pace_per_day 2.0/day real -> 60.88/month current vs required 300/month -> gap ~239.12
        proj = {"state": "projected", "status_color": "red", "on_track": False,
                "is_estimate": False, "pace_per_day": 2.0, "days_over_target": 61}
        pace = {"monthly_pace": 300.0, "remaining_amount": 900.0, "days_remaining": 30, "overdue": False}
        d = app_module._build_goal_display(self._goal(target_date=_iso(30), projection=proj, pace=pace))
        assert d["months_behind"] == round(61 / 30.44, 1)
        assert d["extra_monthly_needed"] == round(300.0 - (2.0 * 30.44), 2)

    def test_on_track_has_no_months_behind_or_extra_needed(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": "green", "on_track": True,
                "is_estimate": False, "pace_per_day": 20.0, "days_over_target": None}
        d = app_module._build_goal_display(self._goal(target_date=_iso(200), projection=proj))
        assert d["months_behind"] is None
        assert d["extra_monthly_needed"] is None

    def test_no_target_date_has_no_months_behind(self, app):
        import app as app_module
        proj = {"state": "projected", "status_color": None, "on_track": None,
                "is_estimate": False, "pace_per_day": 5.0, "days_over_target": None}
        d = app_module._build_goal_display(self._goal(target_date=None, projection=proj))
        assert d["months_behind"] is None
        assert d["extra_monthly_needed"] is None


# ── 3. OVERFLOW MENU MARKUP & UNCHANGED ACTIONS ──────────────────────────────
class TestOverflowMenuMarkup:
    def test_menu_contains_all_three_actions_wired_to_existing_routes(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Menu goal", 1000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Menu goal"):body.find("Menu goal") + 2000]

        assert f"toggleGoalMenu(event, {gid})" in section
        assert f"openEditGoalModal({gid})" in section
        assert '/settings/complete-goal' in section
        assert '/settings/delete-goal' in section
        assert 'goal-menu-item text-danger' in section  # Delete styled destructive

    def test_no_bare_edit_mark_achieved_delete_buttons_outside_menu(self, auth_client, db_conn, test_user):
        """Regression guard for the old three-button row - Edit/Mark
        achieved/Delete should only exist inside the overflow dropdown now,
        not as separate visible buttons."""
        _add_goal(db_conn, test_user["id"], "Menu goal 2", 1000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Menu goal 2"):body.find("Menu goal 2") + 2000]
        assert 'btn-outline-secondary" onclick="openEditGoalModal' not in section
        assert 'class="goal-menu-dropdown"' in section

    def test_reopen_label_for_completed_goal_still_in_menu(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Done goal", 1000.0)
        db_conn.execute("UPDATE goals SET status='completed' WHERE id=?", (gid,))
        db_conn.commit()
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Done goal"):body.find("Done goal") + 2000]
        assert ">Reopen<" in section

    def test_edit_action_still_functions(self, auth_client, db_conn, test_user):
        """The overflow menu's Edit button still triggers the same JS
        function/modal - not a route, so verify the edit ROUTE itself
        (unchanged by this redesign) still works end to end."""
        gid = _add_goal(db_conn, test_user["id"], "Editable goal", 1000.0)
        resp = auth_client.post("/settings/edit-goal", data={
            **csrf(), "id": gid, "name": "Renamed goal",
            "goal_type": "savings", "target_amount": "2000",
        }, follow_redirects=True)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Renamed goal" in body

    def test_delete_action_still_functions(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Deletable goal", 1000.0)
        resp = auth_client.post("/settings/delete-goal", data={
            **csrf(), "id": gid,
        }, follow_redirects=True)
        assert resp.status_code == 200
        row = db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        assert row is None

    def test_mark_achieved_action_still_functions(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Achievable goal", 1000.0)
        resp = auth_client.post("/settings/complete-goal", data={
            **csrf(), "id": gid,
        }, follow_redirects=True)
        assert resp.status_code == 200
        row = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        assert row["status"] == "completed"


# ── 4. ICON TILE / TYPE PILL RENDERED PER GOAL TYPE ──────────────────────────
class TestIconAndTypePillRendering:
    def test_savings_goal_gets_green_pill_and_relevant_icon(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "House deposit", 5000.0, goal_type="savings")
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        # The icon tile renders BEFORE the goal name in the markup, so the
        # window has to look back as well as forward from the name.
        idx = body.find("House deposit")
        section = body[max(0, idx - 400):idx + 800]
        assert "#dcfce7" in section
        assert "🏠" in section
        assert "Savings" in section

    def test_debt_goal_gets_red_pill_and_relevant_icon(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-3000.0)
        _add_goal(db_conn, test_user["id"], "Car loan", 3000.0, goal_type="debt", linked_account_id=acc_id, starting_balance=-3000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        idx = body.find("Car loan")
        section = body[max(0, idx - 400):idx + 800]
        assert "#fee2e2" in section
        assert "🚗" in section
        assert "Debt repayment" in section


# ── 5. HOME CARD REFLECTS SAME VISUAL LANGUAGE ───────────────────────────────
class TestHomeCardVisualLanguage:
    def test_home_card_shows_icon_tile_for_goal(self, auth_client, db_conn, test_user, test_account):
        _add_goal(db_conn, test_user["id"], "Emergency fund", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        idx = body.find("Emergency fund")
        section = body[max(0, idx - 400):idx + 600]
        assert "🛡️" in section

    def test_home_card_no_goals_state_unaffected(self, auth_client, test_account):
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "Set a savings or debt goal" in body

    def test_home_card_multiple_goals_still_caps_at_three(self, auth_client, db_conn, test_user, test_account):
        for i in range(5):
            _add_goal(db_conn, test_user["id"], f"Goal {i}", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        assert "+2 more goal" in body
