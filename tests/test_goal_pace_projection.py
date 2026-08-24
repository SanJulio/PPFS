"""
Tests for the previously-missing piece of goal tracking: a projected
completion date derived from a goal's REAL recent pace (recent logged
contributions for a standalone goal, recent real balance-change velocity
for a linked one) — independent of whether a target date was ever set, and
compared against one (on-track/behind, colour-coded) when it was.

This is the inverse of _suggest_goal_pace(), which asks "given a target
date, what pace is needed" — this asks "given what's actually happening,
when will this really finish".
"""
import datetime

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ago=0, days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead) - datetime.timedelta(days=days_ago)).isoformat()


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


def _add_contribution(db_conn, goal_id, user_id, amount, days_ago):
    db_conn.execute(
        "INSERT INTO goal_contributions (goal_id, user_id, amount, date) VALUES (?,?,?,?)",
        (goal_id, user_id, amount, _iso(days_ago)),
    )
    db_conn.commit()


def _add_income(db_conn, user_id, amount=3000.0, day=25):
    db_conn.execute(
        "INSERT INTO income (name, amount, frequency, account, user_id, day, is_primary) VALUES (?,?,?,?,?,?,1)",
        ("Salary", amount, "monthly", "", user_id, day),
    )
    db_conn.commit()


def _add_tx(db_conn, user_id, account, amount, days_ago):
    db_conn.execute(
        "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
        (_iso(days_ago), "test", amount, account, user_id, "manual", "Other"),
    )
    db_conn.commit()


# ── 1. RECENT PACE CALCULATION — STANDALONE ──────────────────────────────────
class TestRecentPaceStandalone:
    def test_multiple_contributions_at_varying_intervals(self, auth_client, db_conn, test_user):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "Goal", 10000.0)
        # £100 at 60 days ago, £100 at 40 days ago, £100 at 20 days ago, £100 at 5 days ago
        for days_ago in (60, 40, 20, 5):
            _add_contribution(db_conn, gid, test_user["id"], 100.0, days_ago)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        # All 4 within the 90-day window; span = today - 60 days ago = 60 days; total = 400
        assert pace == pytest.approx(400.0 / 60.0)

    def test_fewer_than_two_contributions_is_insufficient(self, auth_client, db_conn, test_user):
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "Goal", 1000.0)
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        assert app_module._compute_goal_recent_pace(goal, test_user["id"]) is None

        _add_contribution(db_conn, gid, test_user["id"], 50.0, 10)
        assert app_module._compute_goal_recent_pace(goal, test_user["id"]) is None

    def test_sparse_logging_falls_back_to_last_five_regardless_of_age(self, auth_client, db_conn, test_user):
        """Only 1 contribution in the last 90 days -> falls back to the last
        5 contributions overall rather than reporting no data, since the
        user genuinely has real (if infrequent) history."""
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "Goal", 10000.0)
        _add_contribution(db_conn, gid, test_user["id"], 200.0, 400)  # over a year ago
        _add_contribution(db_conn, gid, test_user["id"], 200.0, 200)
        _add_contribution(db_conn, gid, test_user["id"], 200.0, 10)   # only this one is < 90 days

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        assert pace is not None
        # Falls back to all 3 (< 5 total), spanning from 400 days ago
        assert pace == pytest.approx(600.0 / 400.0)

    def test_recent_window_preferred_over_full_history_when_available(self, auth_client, db_conn, test_user):
        """When there ARE >= 2 contributions in the last 90 days, use only
        those - genuinely recent, not diluted by old history."""
        import app as app_module
        gid = _add_goal(db_conn, test_user["id"], "Goal", 10000.0)
        _add_contribution(db_conn, gid, test_user["id"], 10.0, 400)  # old, should be excluded
        _add_contribution(db_conn, gid, test_user["id"], 300.0, 30)
        _add_contribution(db_conn, gid, test_user["id"], 300.0, 10)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        assert pace == pytest.approx(600.0 / 30.0)


# ── 2. RECENT PACE CALCULATION — LINKED ──────────────────────────────────────
class TestRecentPaceLinked:
    def test_linked_savings_increasing_balance(self, auth_client, db_conn, test_user):
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=5000.0)
        _add_tx(db_conn, test_user["id"], "Savings", 1000.0, 20)
        gid = _add_goal(db_conn, test_user["id"], "Goal", 10000.0, linked_account_id=acc_id, starting_balance=4000.0)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        # balance_then = 5000 - 1000 = 4000; delta = 5000 - 4000 = 1000 over 20 days
        assert pace == pytest.approx(1000.0 / 20.0)

    def test_linked_debt_decreasing_balance_is_positive_pace(self, auth_client, db_conn, test_user):
        """Debt balance shrinking toward zero = positive (good) pace, the
        same magnitude-based direction handling as _compute_goal_progress."""
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-5000.0)
        _add_tx(db_conn, test_user["id"], "Loan", 1000.0, 20)  # paid down £1000
        gid = _add_goal(db_conn, test_user["id"], "Pay off loan", 8000.0, goal_type="debt",
                         linked_account_id=acc_id, starting_balance=-8000.0)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        # balance_then = -5000 - 1000 = -6000; abs(then)=6000, abs(now)=5000 -> delta=1000 over 20 days
        assert pace == pytest.approx(1000.0 / 20.0)

    def test_linked_debt_growing_balance_is_negative_pace(self, auth_client, db_conn, test_user):
        """Debt balance growing (going the wrong way) must show a real
        negative pace, not be clamped to zero."""
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-6000.0)
        _add_tx(db_conn, test_user["id"], "Loan", -1000.0, 20)  # borrowed MORE
        gid = _add_goal(db_conn, test_user["id"], "Pay off loan", 8000.0, goal_type="debt",
                         linked_account_id=acc_id, starting_balance=-5000.0)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace = app_module._compute_goal_recent_pace(goal, test_user["id"])
        assert pace is not None
        assert pace < 0

    def test_no_transactions_in_window_is_insufficient(self, auth_client, db_conn, test_user):
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Goal", 5000.0, linked_account_id=acc_id, starting_balance=1000.0)
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        assert app_module._compute_goal_recent_pace(goal, test_user["id"]) is None


# ── 3. COMPLETION PROJECTION ──────────────────────────────────────────────────
class TestProjectGoalCompletion:
    def test_insufficient_data_state(self):
        import app as app_module
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, None)
        assert result["state"] == "insufficient_data"
        assert result["projected_date"] is None

    def test_no_progress_state_for_zero_or_negative_pace(self):
        import app as app_module
        for pace in (0, -5.0):
            result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 100}, pace)
            assert result["state"] == "no_progress"
            assert result["projected_date"] is None

    def test_reached_state_when_remaining_non_positive(self):
        import app as app_module
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 1000}, 10.0)
        assert result["state"] == "reached"
        assert result["projected_date"] == TODAY.isoformat()

    def test_projected_state_correct_date_math(self):
        import app as app_module
        # £800 remaining at £10/day -> 80 days from today
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 200}, 10.0)
        assert result["state"] == "projected"
        expected = (TODAY + datetime.timedelta(days=80)).isoformat()
        assert result["projected_date"] == expected

    def test_years_away_state_for_extremely_slow_pace(self):
        """A tiny pace must not produce a literal decades-out date."""
        import app as app_module
        result = app_module._project_goal_completion({"target_amount": 8000, "progress_amount": 10}, 0.1)
        assert result["state"] == "years_away"
        assert result["projected_date"] is None
        assert result["years_away"] is not None
        assert result["years_away"] >= 10

    def test_on_track_green_when_projected_before_target(self):
        import app as app_module
        target = _iso(days_ahead=100)
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 20.0, target)
        assert result["state"] == "projected"
        assert result["on_track"] is True
        assert result["status_color"] == "green"

    def test_amber_when_slightly_behind_target(self):
        import app as app_module
        # Projected exactly 15 days after target
        target = _iso(days_ahead=50)
        # remaining/pace = 65 days from today -> 15 days after the 50-day target
        result = app_module._project_goal_completion({"target_amount": 650, "progress_amount": 0}, 10.0, target)
        assert result["state"] == "projected"
        assert result["on_track"] is False
        assert result["status_color"] == "amber"

    def test_red_when_significantly_behind_target(self):
        import app as app_module
        target = _iso(days_ahead=10)
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 5.0, target)
        # remaining/pace = 200 days, way past the 10-day target
        assert result["status_color"] == "red"
        assert result["on_track"] is False

    def test_boundary_exactly_on_target_is_on_track(self):
        import app as app_module
        target = _iso(days_ahead=100)
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 10.0, target)
        assert result["projected_date"] == target
        assert result["on_track"] is True
        assert result["status_color"] == "green"

    def test_no_target_date_leaves_on_track_none(self):
        import app as app_module
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 200}, 10.0, None)
        assert result["state"] == "projected"
        assert result["projected_date"] is not None
        assert result["on_track"] is None
        assert result["status_color"] is None

    def test_insufficient_data_with_target_date_leaves_on_track_none(self):
        """Can't judge on-track-ness without a real pace to compare."""
        import app as app_module
        target = _iso(days_ahead=100)
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, None, target)
        assert result["state"] == "insufficient_data"
        assert result["on_track"] is None


# ── 4. INTEGRATION — GOALS TAB (My Money) ────────────────────────────────────
class TestGoalsTabProjectionDisplay:
    def test_no_target_date_still_shows_projected_date(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Rainy day fund", 1000.0)
        for days_ago in (60, 40, 20, 5):
            _add_contribution(db_conn, gid, test_user["id"], 50.0, days_ago)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert "Rainy day fund" in body
        assert "At current pace" in body
        assert "Target:" not in body.split("Rainy day fund")[1][:600]

    def test_on_track_still_computed_and_projected_date_shown(self, auth_client, db_conn, test_user):
        """The on-track/behind-target colour comparison text was removed
        (August 2026 - replaced by the contribution slider, which shows
        its own live projected date against the target instead), but the
        underlying real-pace calculation is unchanged - the projected date
        it produces must still appear on the card as a bare fact, and
        _project_goal_completion() must still resolve it as on_track under
        the hood (covered directly elsewhere; checked here too since it's
        cheap and this is exactly the scenario that used to render green)."""
        import re
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=5000.0)
        target = _iso(days_ahead=200)
        _add_goal(db_conn, test_user["id"], "House deposit", 10000.0, target_date=target,
                  linked_account_id=acc_id, starting_balance=1000.0)
        _add_tx(db_conn, test_user["id"], "Savings", 3000.0, 30)
        db_conn.execute("UPDATE accounts SET balance = balance + 3000 WHERE id=?", (acc_id,))
        db_conn.commit()

        # Rough approximation of the real span-based pace calc, just to
        # confirm this really is an on-track scenario (the app's actual
        # _compute_goal_recent_pace() reconstructs balance-at-window-start
        # from transaction history, which won't match a naive /30 exactly -
        # that precision is already covered by test_goal_pace_projection's
        # other, more direct tests of that function).
        pace_per_day = 3000.0 / 30
        progress = {"progress_amount": 4000.0, "target_amount": 10000.0}
        expected = app_module._project_goal_completion(progress, pace_per_day, target, is_estimate=False)
        assert expected["on_track"] is True  # confirms this is genuinely the on-track scenario

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("House deposit"):body.find("House deposit") + 4200]
        assert "At current pace" in section
        assert re.search(r"\d{2}/\d{2}/\d{4}", section)  # a real projected date is shown, UK DD/MM/YYYY
        assert app_module.dateformat_filter(target) in section  # target date shown as UK DD/MM/YYYY

    def test_behind_target_projected_date_still_shown(self, auth_client, db_conn, test_user):
        """Same as above for the behind-target case - the colour-coded
        "behind target / try £X more" text is gone (superseded by the
        slider), but the real pace's projected date is still a visible
        fact the user can compare against the target date themselves."""
        import re
        import app as app_module
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-8000.0)
        target = _iso(days_ahead=10)
        _add_goal(db_conn, test_user["id"], "Pay off loan", 8000.0, goal_type="debt", target_date=target,
                  linked_account_id=acc_id, starting_balance=-8000.0)
        # £1200 paid over 30 days -> £40/day -> ~200 days to clear the
        # remaining £8000, well behind a 10-day target but nowhere near the
        # years_away threshold (which is what a truly tiny pace should hit -
        # see test_extremely_slow_pace_shows_years_away_not_absurd_date).
        _add_tx(db_conn, test_user["id"], "Loan", 1200.0, 30)
        db_conn.execute("UPDATE accounts SET balance = balance + 1200 WHERE id=?", (acc_id,))
        db_conn.commit()

        # See test_on_track_still_computed_and_projected_date_shown for why
        # this is a rough approximation rather than an exact match.
        pace_per_day = 1200.0 / 30
        progress = {"progress_amount": 1200.0, "target_amount": 8000.0}
        expected = app_module._project_goal_completion(progress, pace_per_day, target, is_estimate=False)
        assert expected["on_track"] is False  # confirms this is genuinely the behind-target scenario

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Pay off loan"):body.find("Pay off loan") + 4200]
        assert "At current pace" in section
        assert re.search(r"\d{2}/\d{2}/\d{4}", section)  # a real projected date is shown, UK DD/MM/YYYY
        assert app_module.dateformat_filter(target) in section  # target date shown as UK DD/MM/YYYY

    def test_insufficient_data_shows_honest_message_not_a_date(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "Brand new goal", 1000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Brand new goal"):body.find("Brand new goal") + 4200]
        assert "not enough recent activity yet to estimate a pace" in section

    def test_no_progress_state_shown_for_worsening_debt(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-8000.0)
        _add_goal(db_conn, test_user["id"], "Static loan", 8000.0, goal_type="debt",
                  linked_account_id=acc_id, starting_balance=-8000.0)
        _add_tx(db_conn, test_user["id"], "Loan", -500.0, 30)  # debt growing
        db_conn.execute("UPDATE accounts SET balance = balance - 500 WHERE id=?", (acc_id,))
        db_conn.commit()

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Static loan"):body.find("Static loan") + 4200]
        assert "No recent progress" in section

    def test_extremely_slow_pace_shows_years_away_not_absurd_date(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Loan", balance=-8000.0)
        _add_goal(db_conn, test_user["id"], "Slow loan", 8000.0, goal_type="debt",
                  linked_account_id=acc_id, starting_balance=-8000.0)
        _add_tx(db_conn, test_user["id"], "Loan", 1.0, 60)  # £1 paid in 60 days
        db_conn.execute("UPDATE accounts SET balance = balance + 1 WHERE id=?", (acc_id,))
        db_conn.commit()

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Slow loan"):body.find("Slow loan") + 4200]
        assert "years away" in section
        assert "2157" not in body and "21" not in section.split("years away")[0][-6:]

    def test_recalculates_as_new_contributions_are_added(self, auth_client, db_conn, test_user):
        """Not cached/stale - adding a contribution changes the recent pace
        on the very next load."""
        gid = _add_goal(db_conn, test_user["id"], "Dynamic goal", 1000.0)
        _add_contribution(db_conn, gid, test_user["id"], 10.0, 80)
        _add_contribution(db_conn, gid, test_user["id"], 10.0, 70)

        resp1 = auth_client.post(
            "/manage?tab=goals",  # not a real POST endpoint, use GET
            follow_redirects=False,
        ) if False else auth_client.get("/manage?tab=goals")
        body1 = resp1.get_data(as_text=True)
        section1 = body1[body1.find("Dynamic goal"):body1.find("Dynamic goal") + 4200]
        assert "At current pace" in section1

        # A big new contribution should visibly change the projected date
        resp = auth_client.post(
            "/settings/add-goal-contribution",
            data={**csrf(), "goal_id": str(gid), "amount": "500", "date": _iso(1)},
        )
        assert resp.status_code == 302

        resp2 = auth_client.get("/manage?tab=goals")
        body2 = resp2.get_data(as_text=True)
        section2 = body2[body2.find("Dynamic goal"):body2.find("Dynamic goal") + 4200]
        assert section1 != section2

    def test_recalculates_as_linked_balance_changes(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=1000.0)
        # A real income source is required here since the August 2026 fix:
        # the fallback estimate is now also hard-capped at real recurring
        # monthly income minus bills (see test_goal_fallback_pace.py), and
        # suppressed entirely when that can't be verified as positive - an
        # account balance alone (no tracked income) is exactly that
        # unverifiable case, so it wouldn't produce an estimate without this.
        _add_income(db_conn, test_user["id"], amount=3000.0)
        _add_goal(db_conn, test_user["id"], "Live goal", 10000.0, linked_account_id=acc_id, starting_balance=1000.0)

        resp1 = auth_client.get("/manage?tab=goals")
        body1 = resp1.get_data(as_text=True)
        section1 = body1[body1.find("Live goal"):body1.find("Live goal") + 4200]
        # No real transaction history yet -> falls back to the Safe-to-Spend
        # estimate (see tests/test_goal_fallback_pace.py) rather than real
        # tracked pace - this account has a positive balance so Safe to
        # Spend is positive too, meaning a fallback estimate is available.
        assert "Estimated" in section1
        assert "At current pace" not in section1

        _add_tx(db_conn, test_user["id"], "Savings", 2000.0, 10)
        db_conn.execute("UPDATE accounts SET balance = balance + 2000 WHERE id=?", (acc_id,))
        db_conn.commit()

        resp2 = auth_client.get("/manage?tab=goals")
        body2 = resp2.get_data(as_text=True)
        section2 = body2[body2.find("Live goal"):body2.find("Live goal") + 4200]
        # Now has real transaction history -> switches to genuine tracked pace
        assert "At current pace" in section2
        assert "Estimated" not in section2
        assert section1 != section2


# ── 5. HOME PAGE — COMPACT ON-TRACK DOT ──────────────────────────────────────
class TestHomeOnTrackDot:
    def test_dot_shown_for_goal_with_target_date_and_data(self, auth_client, db_conn, test_user):
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=5000.0)
        target = _iso(days_ahead=200)
        _add_goal(db_conn, test_user["id"], "House", 10000.0, target_date=target,
                  linked_account_id=acc_id, starting_balance=1000.0)
        _add_tx(db_conn, test_user["id"], "Savings", 3000.0, 30)
        db_conn.execute("UPDATE accounts SET balance = balance + 3000 WHERE id=?", (acc_id,))
        db_conn.commit()

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        section = body[body.find("House"):body.find("House") + 900]
        assert "\U0001f7e2" in section  # green dot

    def test_no_dot_for_goal_without_target_date(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "No deadline goal", 1000.0)
        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        section = body[body.find("No deadline goal"):body.find("No deadline goal") + 900]
        for dot in ("\U0001f7e2", "\U0001f7e1", "\U0001f534"):
            assert dot not in section

    def test_home_renders_without_error_for_insufficient_data_goal_with_target(self, auth_client, db_conn, test_user):
        target = _iso(days_ahead=100)
        _add_goal(db_conn, test_user["id"], "Fresh goal", 1000.0, target_date=target)
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert "Fresh goal" in resp.get_data(as_text=True)
