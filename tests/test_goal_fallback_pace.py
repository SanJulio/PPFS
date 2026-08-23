"""
Tests for the Safe-to-Spend-based fallback pace estimate — used only when a
goal doesn't yet have enough real contribution/balance-change history for
_compute_goal_recent_pace() to return a genuine rate.

Before this, that case just showed "not enough recent activity yet to
estimate a pace" with no useful number at all. Now it falls back to an
early estimate derived from the user's Safe to Spend figure — clearly
labelled as an estimate (never blended with or mistaken for real tracked
pace), split across however many other active goals are in the same boat so
no single goal's estimate implies the user's entire typical leftover is
available to it alone, and suppressed entirely rather than showing a
fabricated positive number when Safe to Spend itself is zero.
"""
import datetime

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _add_income(db_conn, user_id, amount=3000.0, day=25):
    db_conn.execute(
        "INSERT INTO income (name, amount, frequency, account, user_id, day, is_primary) VALUES (?,?,?,?,?,?,1)",
        ("Salary", amount, "monthly", "", user_id, day),
    )
    db_conn.commit()


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current"):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
        (name, balance, acc_type, user_id),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_goal(db_conn, user_id, name, target, target_date=None, linked_account_id=None, starting_balance=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, name, "savings", target, target_date, linked_account_id, starting_balance),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_contribution(db_conn, goal_id, user_id, amount, days_ago):
    db_conn.execute(
        "INSERT INTO goal_contributions (goal_id, user_id, amount, date) VALUES (?,?,?,?)",
        (goal_id, user_id, amount, (TODAY - datetime.timedelta(days=days_ago)).isoformat()),
    )
    db_conn.commit()


# ── 1. FALLBACK APPEARS FOR A BRAND NEW GOAL ─────────────────────────────────
class TestFallbackReplacesInsufficientData:
    def test_new_goal_shows_estimate_instead_of_blank_message(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        _add_goal(db_conn, test_user["id"], "Brand new goal", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Brand new goal"):body.find("Brand new goal") + 4200]
        assert "Estimated" in section
        assert "not enough recent activity yet to estimate a pace" not in section

    def test_estimate_clearly_labelled_differently_from_real_pace(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        _add_goal(db_conn, test_user["id"], "Fresh goal", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Fresh goal"):body.find("Fresh goal") + 4200]
        assert "Estimated" in section
        assert "At current pace" not in section
        assert "font-style:italic" in section
        assert "Based on your typical Safe to Spend" in section

    def test_estimate_shows_a_real_monthly_figure_and_projected_date(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"], amount=3000.0)
        _add_goal(db_conn, test_user["id"], "Numeric goal", 500.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Numeric goal"):body.find("Numeric goal") + 4200]
        assert "/month" in section
        # Some projected date must appear (either a real ISO date or years-away text)
        assert "<strong>" in section


# ── 2. SWITCHES TO REAL PACE ONCE ENOUGH DATA EXISTS ─────────────────────────
class TestSwitchesToRealPaceOnceDataExists:
    def test_standalone_goal_switches_over(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        gid = _add_goal(db_conn, test_user["id"], "Switching goal", 1000.0)
        for days_ago in (60, 40, 20, 5):
            _add_contribution(db_conn, gid, test_user["id"], 5.0, days_ago)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Switching goal"):body.find("Switching goal") + 4200]
        assert "At current pace" in section
        assert "Estimated" not in section
        assert "font-style:italic" not in section

    def test_linked_goal_switches_over_once_real_transactions_exist(self, auth_client, db_conn, test_user):
        _add_income(db_conn, test_user["id"])
        acc_id = _add_account(db_conn, test_user["id"], "Savings", balance=2000.0)
        _add_goal(db_conn, test_user["id"], "Linked switching goal", 10000.0, linked_account_id=acc_id, starting_balance=1000.0)
        db_conn.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?,?,?,?,?,?,?)",
            ((TODAY - datetime.timedelta(days=20)).isoformat(), "test", 1000.0, "Savings", test_user["id"], "manual", "Other"),
        )
        db_conn.commit()

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Linked switching goal"):body.find("Linked switching goal") + 4200]
        assert "At current pace" in section
        assert "Estimated" not in section

    def test_direct_pace_map_prefers_real_pace_when_available(self, auth_client, db_conn, test_user):
        import app as app_module
        _add_income(db_conn, test_user["id"])
        gid = _add_goal(db_conn, test_user["id"], "Goal", 1000.0)
        for days_ago in (60, 40):
            _add_contribution(db_conn, gid, test_user["id"], 10.0, days_ago)

        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        pace_map, fallback_count = app_module._compute_goal_pace_map([goal], test_user["id"])
        pace, is_estimate = pace_map[gid]
        assert is_estimate is False
        assert pace == pytest.approx(20.0 / 60.0)
        assert fallback_count == 0


# ── 3. MULTIPLE GOALS SHARE THE ESTIMATE ─────────────────────────────────────
class TestMultipleGoalsSplitTheEstimate:
    def test_two_new_goals_split_the_estimate_evenly(self, auth_client, db_conn, test_user, test_account):
        """Goes through the real route (rather than calling
        _compute_goal_pace_map directly) because Safe to Spend's underlying
        calculate_financial_overview() reads Flask-Login's current_user
        global in one of its internal calls rather than the user_id it's
        passed - a pre-existing quirk unrelated to this feature, but one
        that means this needs a real authenticated request context to
        resolve correctly, same as the route itself always has in practice."""
        import re

        # Target amounts are deliberately large here (see
        # TestFallbackNeverImpliesUnrealisticSpeed below) - the fallback
        # estimate is now capped so it can never imply finishing a goal in
        # under ~1 month (see _cap_fallback_rate_for_goal). A £1,000 goal
        # against this account's real surplus would hit that cap in BOTH
        # the single- and two-goal cases and mask the even-split arithmetic
        # this test exists to verify, since both would clamp to the same
        # capped figure regardless of how many goals shared the estimate.
        # A large target keeps this specific test isolated to just the
        # splitting behaviour.
        _add_income(db_conn, test_user["id"], amount=3000.0)
        _add_goal(db_conn, test_user["id"], "GoalOne", 100000.0)

        resp1 = auth_client.get("/manage?tab=goals")
        section1 = resp1.get_data(as_text=True)
        section1 = section1[section1.find("GoalOne"):section1.find("GoalOne") + 4200]
        match1 = re.search(r"around £([\d.]+)/month", section1)
        assert match1, "expected a £X/month estimate with a single goal"
        single_rate = float(match1.group(1))

        _add_goal(db_conn, test_user["id"], "GoalTwo", 100000.0)
        resp2 = auth_client.get("/manage?tab=goals")
        section2 = resp2.get_data(as_text=True)
        section2 = section2[section2.find("GoalOne"):section2.find("GoalOne") + 4200]
        match2 = re.search(r"around £([\d.]+)/month", section2)
        assert match2, "expected a £X/month estimate once a second goal exists"
        split_rate = float(match2.group(1))

        assert split_rate == pytest.approx(single_rate / 2, rel=0.02)

    def test_ui_notes_the_split_across_goals(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        _add_goal(db_conn, test_user["id"], "GoalOne", 1000.0)
        _add_goal(db_conn, test_user["id"], "GoalTwo", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("GoalOne"):body.find("GoalOne") + 4200]
        assert "split evenly across 2 goals" in section

    def test_single_goal_estimate_has_no_split_note(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        _add_goal(db_conn, test_user["id"], "Only goal", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Only goal"):body.find("Only goal") + 4200]
        assert "split evenly across" not in section

    def test_goal_with_real_data_does_not_count_toward_fallback_denominator(self, auth_client, db_conn, test_user):
        """A goal that already has real tracked pace isn't competing for the
        estimate the same way - it shouldn't shrink other goals' share. If
        it wrongly counted, "Brand new" would show a "split evenly across 2
        goals" note; it must not, since only it needs the fallback."""
        _add_income(db_conn, test_user["id"])
        gid_real = _add_goal(db_conn, test_user["id"], "Has real data", 1000.0)
        for days_ago in (60, 40):
            _add_contribution(db_conn, gid_real, test_user["id"], 10.0, days_ago)
        _add_goal(db_conn, test_user["id"], "Brand new", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)

        real_section = body[body.find("Has real data"):body.find("Has real data") + 4200]
        assert "At current pace" in real_section
        assert "Estimated" not in real_section

        new_section = body[body.find("Brand new"):body.find("Brand new") + 4200]
        assert "Estimated" in new_section
        assert "split evenly across" not in new_section

    def test_completed_goal_excluded_from_fallback_denominator(self, auth_client, db_conn, test_user, test_account):
        import app as app_module
        _add_income(db_conn, test_user["id"])
        _add_goal(db_conn, test_user["id"], "Active new goal", 1000.0)
        completed_id = _add_goal(db_conn, test_user["id"], "Completed goal", 1000.0)
        db_conn.execute("UPDATE goals SET status='completed' WHERE id=?", (completed_id,))
        db_conn.commit()

        goals = [dict(r) for r in db_conn.execute("SELECT * FROM goals WHERE user_id=?", (test_user["id"],)).fetchall()]
        for g in goals:
            g["target_amount"] = float(g["target_amount"])
        pace_map, fallback_count = app_module._compute_goal_pace_map(goals, test_user["id"])
        assert fallback_count == 1  # completed goal doesn't count
        assert pace_map[completed_id][0] is None  # and gets no estimate at all


# ── 4. ZERO / NEGATIVE SAFE TO SPEND ─────────────────────────────────────────
class TestNoRealisticEstimateAvailable:
    def test_zero_safe_to_spend_falls_back_to_honest_insufficient_data(self, auth_client, db_conn, test_user):
        """No income, no balance -> Safe to Spend is genuinely £0. Must not
        suggest a positive pace that doesn't exist."""
        _add_account(db_conn, test_user["id"], "Empty account", balance=0.0)
        _add_goal(db_conn, test_user["id"], "Hopeless goal", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Hopeless goal"):body.find("Hopeless goal") + 4200]
        assert "not enough recent activity yet to estimate a pace" in section
        assert "Estimated" not in section
        assert "/month" not in section

    def test_safe_to_spend_daily_rate_is_none_or_zero_produces_no_fallback(self, db_conn, test_user):
        import app as app_module
        _add_account(db_conn, test_user["id"], "Empty account", balance=0.0)
        gid = _add_goal(db_conn, test_user["id"], "Goal", 1000.0)
        goal = dict(db_conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        goal["target_amount"] = float(goal["target_amount"])

        pace_map, fallback_count = app_module._compute_goal_pace_map([goal], test_user["id"])
        assert fallback_count == 1
        pace, is_estimate = pace_map[gid]
        assert pace is None
        assert is_estimate is False

    def test_negative_safe_to_spend_daily_rate_clamped_not_negative_pace(self):
        """calculate_financial_overview already floors safe_spending at 0,
        but the fallback logic must not propagate a negative value even if
        that ever changed - never suggest a pace going backwards."""
        import app as app_module
        # Directly exercise the clamp logic via the pace map with a stubbed
        # negative daily rate to confirm the >0 guard, independent of how
        # Safe to Spend itself is computed.
        import unittest.mock as mock
        with mock.patch.object(app_module, "_safe_to_spend_daily_rate", return_value=-50.0):
            goal = {"id": 1, "target_amount": 1000.0, "linked_account_id": None, "goal_type": "savings", "status": "active"}
            pace_map, fallback_count = app_module._compute_goal_pace_map([goal], 999999, accounts_by_id={})
            assert pace_map[1] == (None, False)


# ── 5. FALLBACK NEVER OVERRIDES REAL DATA (regression guard) ────────────────
class TestFallbackNeverBlendsWithRealPace:
    def test_project_goal_completion_is_estimate_flag_is_explicit_not_inferred(self):
        """_project_goal_completion never decides is_estimate itself - it's
        purely a pass-through flag the caller sets, so there's no risk of it
        silently mislabelling a real pace as an estimate or vice versa."""
        import app as app_module
        result_real = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 10.0, is_estimate=False)
        result_estimate = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 10.0, is_estimate=True)
        assert result_real["is_estimate"] is False
        assert result_estimate["is_estimate"] is True
        # Identical maths either way - only the label differs
        assert result_real["projected_date"] == result_estimate["projected_date"]

    def test_default_is_estimate_is_false(self):
        import app as app_module
        result = app_module._project_goal_completion({"target_amount": 1000, "progress_amount": 0}, 10.0)
        assert result["is_estimate"] is False


# ── 6. HOME PAGE DOT DISTINGUISHES ESTIMATE ──────────────────────────────────
class TestHomeDotDistinguishesEstimate:
    def test_home_dot_shows_tilde_prefix_for_estimate(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        target = (TODAY + datetime.timedelta(days=100)).isoformat()
        _add_goal(db_conn, test_user["id"], "Home estimate goal", 1000.0, target_date=target)

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        section = body[body.find("Home estimate goal"):body.find("Home estimate goal") + 300]
        assert "~" in section

    def test_home_dot_no_tilde_for_real_pace(self, auth_client, db_conn, test_user, test_account):
        _add_income(db_conn, test_user["id"])
        target = (TODAY + datetime.timedelta(days=100)).isoformat()
        gid = _add_goal(db_conn, test_user["id"], "Home real goal", 1000.0, target_date=target)
        for days_ago in (60, 40):
            _add_contribution(db_conn, gid, test_user["id"], 10.0, days_ago)

        resp = auth_client.get("/")
        body = resp.get_data(as_text=True)
        section = body[body.find("Home real goal"):body.find("Home real goal") + 300]
        assert "~" not in section


# ── 7. FALLBACK NEVER IMPLIES UNREALISTIC SPEED ──────────────────────────────
class TestFallbackNeverImpliesUnrealisticSpeed:
    """Regression coverage for a real bug report: the fallback estimate
    (shown as "Based on your typical Safe to Spend — around £X/month") could
    show implausibly large figures - e.g. £8,764.28/month against a modest
    goal. Two compounding causes, both fixed here:

    1. _safe_to_spend_daily_rate() divided the live Safe-to-Spend snapshot
       by days-remaining-until-the-cycle-boundary rather than the cycle's
       full length. Safe to Spend already includes the full current balance
       (not just "typical" recurring income), so dividing that lump by a
       small number of days-left-in-cycle (e.g. checking near a manual
       cycle's month-end) inflated the implied daily/monthly rate by
       several times over, purely depending on which day of the cycle the
       page happened to be viewed on - not a reflection of anything that
       actually changed about the user's finances.
    2. Nothing capped the resulting £/month figure against the goal itself,
       so a real (if large) balance/surplus could imply completing a small
       goal in days rather than months - technically consistent with the
       numbers on screen, but not a meaningful "typical monthly pace".
    """

    def test_repro_scenario_no_longer_produces_an_inflated_figure(self, auth_client, db_conn, test_user):
        """The exact scenario that produced the original bug report's
        absurd figure: a modest account (£1,000 balance) with a normal
        £3,000/month income, viewed partway through a manual cycle. Before
        the fix this rendered a monthly figure many times larger than even
        the account's own balance+income; after the fix it's capped to a
        figure proportionate to what the £1,000 goal itself needs."""
        import re
        _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        _add_income(db_conn, test_user["id"], amount=3000.0)
        _add_goal(db_conn, test_user["id"], "Repro goal", 1000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Repro goal"):body.find("Repro goal") + 4200]
        match = re.search(r"around £([\d,\.]+)/month", section)
        assert match, "expected a £X/month estimate"
        monthly_figure = float(match.group(1).replace(",", ""))
        # Never more than the goal's own target - the whole point of the cap.
        # (The original bug produced £13,528.75 against this exact account.)
        assert monthly_figure <= 1000.0 + 0.5

    def test_cap_never_implies_completion_faster_than_about_a_month(self, auth_client, db_conn, test_user):
        """Direct check of the safeguard's core invariant: whatever the raw
        Safe-to-Spend-derived rate would have been, the capped pace can
        never imply finishing the goal's remaining amount in under
        ~30 days, however large the underlying balance/income is. Goes
        through the real route (see the Flask-Login current_user note on
        the "split evenly" test above) so Safe to Spend resolves correctly."""
        import re
        import app as app_module
        _add_account(db_conn, test_user["id"], "Current", balance=50000.0)
        _add_income(db_conn, test_user["id"], amount=10000.0)
        _add_goal(db_conn, test_user["id"], "Tiny goal, huge surplus", 200.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Tiny goal, huge surplus"):body.find("Tiny goal, huge surplus") + 4200]
        match = re.search(r"around £([\d,\.]+)/month", section)
        assert match, "expected a £X/month estimate"
        monthly_figure = float(match.group(1).replace(",", ""))
        implied_daily_pace = monthly_figure / 30.44
        days_to_complete = 200.0 / implied_daily_pace
        assert days_to_complete >= app_module._FALLBACK_MIN_DAYS_TO_COMPLETE - 0.5

    def test_cap_does_not_suppress_genuine_large_surplus_for_a_large_goal(self, auth_client, db_conn, test_user):
        """The safeguard must only catch implausibly FAST estimates, not
        arbitrarily shrink a large-but-real figure for a goal that's large
        enough to genuinely warrant it - catching misleading numbers isn't
        the same as suppressing every large number."""
        import re
        _add_account(db_conn, test_user["id"], "Current", balance=200000.0)
        _add_income(db_conn, test_user["id"], amount=5000.0)
        _add_goal(db_conn, test_user["id"], "Big real goal", 500000.0)

        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        section = body[body.find("Big real goal"):body.find("Big real goal") + 4200]
        match = re.search(r"around £([\d,\.]+)/month", section)
        assert match, "expected a £X/month estimate"
        monthly_figure = float(match.group(1).replace(",", ""))
        # A genuinely large surplus against a genuinely large goal should
        # NOT be clamped down - the cap only bites when the implied pace
        # would finish the goal in under a month, and this is nowhere near
        # completing a £500,000 goal that fast.
        assert monthly_figure < 500000.0
        assert monthly_figure > 50000.0  # still a substantial, real figure - not suppressed

    def test_daily_rate_no_longer_scales_with_days_remaining_in_cycle(self):
        """The old formula divided by days-until-safe_boundary, so the same
        underlying Safe to Spend figure produced a different (and
        increasingly inflated, the closer to the boundary) daily rate
        purely depending on how many days happened to be left in the cycle
        on whatever day this was checked. The fixed formula divides by the
        cycle's fixed length instead, so the result no longer depends on
        "today" at all - confirmed by holding safe_spending and the cycle's
        start/boundary fixed and checking the rate is exactly
        safe_spending / cycle_length."""
        import datetime as _dt
        import unittest.mock as mock
        import app as app_module

        fixed_cycle = {
            "display_start": _dt.date(2026, 8, 1),
            "display_end": _dt.date(2026, 8, 31),
            "safe_boundary": _dt.date(2026, 8, 31),
            "mode_used": "manual",
            "primary_source_name": None,
        }
        with mock.patch("cycle_engine.get_cycle", return_value=fixed_cycle), \
             mock.patch.object(app_module, "_get_safe_to_spend", return_value=4000.0):
            rate = app_module._safe_to_spend_daily_rate(999999)
        # Cycle length = 31 days (Aug 1 - Aug 31 inclusive) - NOT dependent
        # on what "today" is, unlike the old days-until-boundary formula.
        assert rate == pytest.approx(4000.0 / 31, rel=1e-9)

    def test_non_positive_remaining_returns_fallback_rate_unchanged(self, app):
        """A goal with target_amount 0 (remaining <= 0) is a degenerate
        case the cap doesn't need to touch - _project_goal_completion
        already short-circuits to 'reached' before pace_per_day matters, so
        this just confirms the helper doesn't error and passes the rate
        through untouched rather than dividing by zero."""
        import app as app_module
        goal = {"id": 999999, "target_amount": 0.0, "linked_account_id": None,
                "goal_type": "savings", "status": "active"}
        capped = app_module._cap_fallback_rate_for_goal(goal, 500.0, 999999, {})
        assert capped == 500.0
