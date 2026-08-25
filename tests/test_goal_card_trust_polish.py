"""
Tests for the Goals card bug fix / trust & clarity / polish pass (August
2026).

Stage 1 investigated a reported Safe to Spend mismatch on the goal
slider and found NO bug: _get_safe_to_spend() calls the exact same
calculate_financial_overview() used everywhere else, and the reported
"too high" figure was correct - 5 of 6 bills had already had their due
date pass this cycle (today's the 24th, bills fall on days 1/1/1/15/17),
so only one £40 bill was genuinely still ahead. Reproduced the user's
real account/bill/income numbers directly against _get_safe_to_spend()
and confirmed it returns exactly what the app showed. No fix needed
there, so no regression test for it (there was never a bug to guard
against) - this file covers Stages 2-5, which all assume that number is
trustworthy.

Stage 2 (trust & labeling): the fact-line's real/estimated pace date and
the commitment preview's slider-driven date now carry explicit
"(without this commitment)" / "with this commitment" labels distinguishing
historical velocity from a hypothetical new commitment. A goal's card
also now explains in plain language what its commitment actually DOES -
mirroring settings_set_goal_commitment()'s real to_account resolution,
not a simplified version of it.

Stage 3 (guardrail): confirmed already built and functioning - the
would_go_negative warning was simply never triggering in the reported
screenshot because the slider's own 50%-of-Safe-to-Spend default max
keeps the resulting figure positive by construction; typing past that
max (via the paired number field) does trigger it correctly. No fix
needed, covered here as a regression guard using the real reported
numbers.

Stage 4 (clarity/hierarchy): "your commitment" (amount/account, editable)
vs "the consequence" (Safe to Spend after/projected date) are now
visually separated into a tinted box; a light positive signal appears
when a goal is genuinely ahead of a real target date; "Set commitment"
now redirects with a #goal-card-{id} URL fragment so the page can scroll
to and briefly highlight the goal that changed.

Stage 5 (polish): thousand separators via the new `moneyfmt` filter;
44x44px slider thumb touch target; pause/resume a commitment without
deleting it (new savings_rules.is_paused column, skipped by all three
live engine sites); "reset to suggested pace" JS control.
"""
import datetime

import pytest

from tests.conftest import csrf


TODAY = datetime.date.today()


def _iso(days_ahead=0):
    return (TODAY + datetime.timedelta(days=days_ahead)).isoformat()


def _add_account(db_conn, user_id, name, balance=0.0, acc_type="current", savings_type=None, is_locked=0):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview, savings_type, is_locked) VALUES (?,?,?,1,?,1,?,?)",
        (name, balance, acc_type, user_id, savings_type, is_locked),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_goal(db_conn, user_id, name, target, goal_type="savings", target_date=None,
              linked_account_id=None, starting_balance=None, minimum_payment=None):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance, minimum_payment, status) "
        "VALUES (?,?,?,?,?,?,?,?, 'active')",
        (user_id, name, goal_type, target, target_date, linked_account_id, starting_balance, minimum_payment),
    )
    db_conn.commit()
    return cur.lastrowid


def _add_bill(db_conn, user_id, name, amount, day, account):
    db_conn.execute(
        "INSERT INTO scheduled_expenses (user_id, name, amount, day, frequency, account) VALUES (?,?,?,?, 'monthly', ?)",
        (user_id, name, amount, day, account),
    )
    db_conn.commit()


def _add_income(db_conn, user_id, amount, day=1):
    db_conn.execute(
        "INSERT INTO income (user_id, name, amount, frequency, day, account, is_primary) VALUES (?,?,?, 'monthly', ?, '', 1)",
        (user_id, "Salary", amount, day),
    )
    db_conn.commit()


def _goal_section(body, marker, size=7500):
    """Searches from the Goals tab's own HTML comment marker onward -
    setting a commitment creates a savings_rules row named "{goal name}
    contribution", which renders in the Savings Rules tab (earlier in the
    page than Goals) and would otherwise collide with a plain
    body.find(marker) whenever a commitment exists for that goal."""
    goals_tab_idx = body.find("<!-- TAB: GOALS -->")
    search_from = goals_tab_idx if goals_tab_idx != -1 else 0
    idx = body.find(marker, search_from)
    assert idx != -1, f"{marker!r} not found in page (searched from the Goals tab)"
    return body[idx:idx + size]


# ── STAGE 1 REGRESSION: real reported scenario stays correct ────────────────
class TestStage1RealScenarioRegression:
    def test_house_deposit_repro_safe_to_spend_and_slider_arithmetic(self, app, auth_client, db_conn, test_user):
        """The real account/bill/income data from the bug report: 5 of 6
        bills had already had their due date pass this cycle by the day it
        was checked (bill days 1/1/1/15/17 vs. "today" the 24th), leaving
        only the £40 Life Insurance bill (day 25) genuinely still ahead.

        The original report used literal calendar days, which would make
        this test flake every time it happens to run on a different day of
        the month (bill "day 25" is only "still ahead" relative to a
        specific "today"). Rebuilt here relative to whatever day the test
        actually runs on: 5 bills placed the day before today (always
        already passed, unless today is the 1st) and 1 placed the day
        after (always still ahead, unless today is the 28th-31st) -
        reproduces the same "5 passed, 1 left" shape and the same £1,979
        result regardless of the real calendar date. Guards against a
        regression reintroducing a stale/separate Safe to Spend
        calculation for the goal slider."""
        import flask_login
        import app as app_module

        today_day = datetime.date.today().day
        if today_day >= 28:
            # A bill "the day after today" would need to land in next
            # month to genuinely still be ahead this cycle - real, but
            # unrelated month-boundary complexity this test isn't trying
            # to cover. Skipped on the ~4 days/month this would apply,
            # rather than adding cross-month bill-matching logic just for
            # this regression guard.
            pytest.skip("bill-day arithmetic needs next-month handling this close to month-end")
        passed_day = max(1, today_day - 1)
        ahead_day = today_day + 1

        monzo = _add_account(db_conn, test_user["id"], "Monzo Current", balance=780.00)
        natwest_cur = _add_account(db_conn, test_user["id"], "Natwest Current", balance=1239.00)
        _add_account(db_conn, test_user["id"], "Natwest Savings", balance=18030.00, acc_type="savings", savings_type="fixed")
        for name, amt in [("Rent", 1250.00), ("Car Finance", 350.00), ("Car Insurance", 200.00),
                           ("Spotify", 12.99), ("Ai app", 18.99)]:
            _add_bill(db_conn, test_user["id"], name, amt, passed_day, "Monzo Current")
        _add_bill(db_conn, test_user["id"], "Life Insurance", 40.00, ahead_day, "Monzo Current")
        _add_income(db_conn, test_user["id"], amount=3400.00, day=passed_day)
        _add_goal(db_conn, test_user["id"], "House Deposit", 30000.0, linked_account_id=natwest_cur, starting_balance=1239.0)

        with app.test_request_context():
            user = app_module.load_user(str(test_user["id"]))
            flask_login.login_user(user)
            s2s = app_module._get_safe_to_spend(test_user["id"])
        assert s2s == pytest.approx(1979.00, abs=0.01)

        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "House Deposit")
        m_slider = __import__("re").search(r'id="commitSlider\d+"[^>]*value="([\d.]+)"', section)
        assert m_slider
        slider_amount = float(m_slider.group(1))
        # Slider's own 50%-of-Safe-to-Spend max caps this well under the
        # full 1979 figure - confirms the guardrail (Stage 3) can't be
        # reached by dragging alone in this scenario.
        assert slider_amount <= 990.0


# ── STAGE 2: fact-line labeling distinguishes historical vs commitment ──────
class TestStage2Labeling:
    def test_fact_line_labelled_without_this_commitment_when_active(self, auth_client, db_conn, test_user):
        acc = _add_account(db_conn, test_user["id"], "Savings", balance=3000.0, acc_type="savings")
        gid = _add_goal(db_conn, test_user["id"], "Active goal", 10000.0, target_date=_iso(200),
                         linked_account_id=acc, starting_balance=1000.0)
        # Real recent pace needs >=2 real data points to reach state
        # 'projected' rather than 'insufficient_data' - without this the
        # qualifier (only rendered for 'projected'/'years_away') would
        # never appear regardless of whether the labelling itself works.
        for days_ago in (60, 30):
            db_conn.execute(
                "INSERT INTO transactions (user_id, account, amount, type, category, description, date) VALUES (?,?,?,?,?,?,?)",
                (test_user["id"], "Savings", 500.0, "income", "Savings", "top-up", _iso(-days_ago)),
            )
        db_conn.commit()
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Active goal")
        assert "(without this commitment)" in section

    def test_no_qualifier_for_completed_goal(self, auth_client, db_conn, test_user):
        """A completed goal has no commitment slider at all, so the
        "(without this commitment)" qualifier - which only makes sense
        when there's a commitment section on screen to contrast against -
        should not appear."""
        import app as app_module
        acc = _add_account(db_conn, test_user["id"], "Savings", balance=10000.0, acc_type="savings")
        gid = _add_goal(db_conn, test_user["id"], "Done goal", 1000.0, target_date=_iso(200),
                         linked_account_id=acc, starting_balance=0.0)
        db_conn.execute("UPDATE goals SET status='completed' WHERE id=?", (gid,))
        db_conn.commit()
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Done goal", size=2500)
        assert "(without this commitment)" not in section

    def test_commitment_preview_labelled_with_this_commitment(self, auth_client, db_conn, test_user):
        acc = _add_account(db_conn, test_user["id"], "Savings", balance=3000.0, acc_type="savings")
        # A positive Safe to Spend (needs a real spending-type balance, not
        # just income - see calculate_financial_overview()'s safe_spending
        # formula) gives the fallback pace a real nonzero figure to work
        # with, so the slider's default is nonzero and its initial preview
        # reaches state 'projected' rather than 'no_progress at £0/cycle'.
        _add_account(db_conn, test_user["id"], "Current", balance=2000.0, acc_type="current")
        _add_income(db_conn, test_user["id"], amount=2000.0)
        _add_goal(db_conn, test_user["id"], "Preview label goal", 10000.0,
                  linked_account_id=acc, starting_balance=1000.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Preview label goal")
        assert "With this commitment" in section
        assert "· Projected:" not in section  # old, ambiguous label fully replaced

    def test_commitment_note_positive_when_linked_to_unlocked_savings_account(self, app):
        import app as app_module
        goal = {"linked_account_id": 1, "goal_type": "savings"}
        accounts_by_id = {1: {"name": "ISA", "type": "savings", "is_locked": False}}
        note = app_module._compute_commitment_note(goal, accounts_by_id)
        assert note["tone"] == "positive"
        assert "ISA" in note["text"]

    def test_commitment_note_neutral_when_savings_goal_linked_to_current_account(self, app):
        """The exact real-world case from the bug report: a savings goal
        linked to a current account never gets a to_account wired up (see
        settings_set_goal_commitment()), so the commitment never shows as
        progress on this goal automatically."""
        import app as app_module
        goal = {"linked_account_id": 1, "goal_type": "savings"}
        accounts_by_id = {1: {"name": "Natwest Current", "type": "current", "is_locked": False}}
        note = app_module._compute_commitment_note(goal, accounts_by_id)
        assert note["tone"] == "neutral"
        assert "Natwest Current" in note["text"]
        assert "won't automatically count as progress" in note["text"]

    def test_commitment_note_neutral_when_linked_savings_account_is_locked(self, app):
        import app as app_module
        goal = {"linked_account_id": 1, "goal_type": "savings"}
        accounts_by_id = {1: {"name": "ISA", "type": "savings", "is_locked": True}}
        note = app_module._compute_commitment_note(goal, accounts_by_id)
        assert note["tone"] == "neutral"

    def test_commitment_note_for_debt_goal(self, app):
        import app as app_module
        goal = {"linked_account_id": 1, "goal_type": "debt"}
        accounts_by_id = {1: {"name": "Credit Card", "type": "current", "is_locked": False}}
        note = app_module._compute_commitment_note(goal, accounts_by_id)
        assert note["tone"] == "neutral"
        assert "Credit Card" in note["text"]
        assert "paid off" in note["text"]

    def test_commitment_note_for_standalone_goal(self, app):
        import app as app_module
        goal = {"linked_account_id": None, "goal_type": "savings"}
        note = app_module._compute_commitment_note(goal, {})
        assert note["tone"] == "neutral"
        assert "Standalone" in note["text"]

    def test_note_rendered_on_real_card(self, auth_client, db_conn, test_user):
        natwest_cur = _add_account(db_conn, test_user["id"], "Natwest Current", balance=1239.0)
        _add_goal(db_conn, test_user["id"], "Note render goal", 30000.0,
                  linked_account_id=natwest_cur, starting_balance=1239.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Note render goal")
        # Jinja HTML-escapes the apostrophe (won&#39;t) - check the
        # unambiguous part of the sentence either side of it instead.
        assert "count as progress" in section
        assert "Natwest Current" in section


# ── STAGE 3: negative-Safe-to-Spend guardrail confirmed working ─────────────
class TestStage3GuardrailConfirmed:
    def test_guardrail_does_not_fire_at_slider_default(self, auth_client, db_conn, test_user):
        acc = _add_account(db_conn, test_user["id"], "Current", balance=2000.0)
        _add_income(db_conn, test_user["id"], amount=3000.0)
        gid = _add_goal(db_conn, test_user["id"], "Within range goal", 30000.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Within range goal")
        assert "push Safe to Spend negative" not in section

    def test_guardrail_fires_when_amount_exceeds_safe_to_spend(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Current", balance=500.0)
        _add_income(db_conn, test_user["id"], amount=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Guardrail goal", 30000.0)
        resp = auth_client.post("/api/goal-commitment-preview", json={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid, "amount": 5000,
        })
        data = resp.get_json()
        assert data["would_go_negative"] is True
        assert data["resulting_safe_to_spend"] < 0


# ── STAGE 4: consequence box, positive signal, save confirmation ────────────
class TestStage4ClarityAndHierarchy:
    def test_ahead_of_schedule_shows_positive_signal(self, auth_client, db_conn, test_user):
        """A real recent pace that comfortably beats a distant target date
        should read as reinforced, not neutral grey text."""
        acc = _add_account(db_conn, test_user["id"], "Savings", balance=5000.0, acc_type="savings")
        gid = _add_goal(db_conn, test_user["id"], "Ahead goal", 10000.0, target_date=_iso(3000),
                         linked_account_id=acc, starting_balance=0.0)
        db_conn.execute(
            "INSERT INTO transactions (user_id, account, amount, type, category, description, date) VALUES (?,?,?,?,?,?,?)",
            (test_user["id"], "Savings", 3000.0, "income", "Savings", "top-up", _iso(-30)),
        )
        db_conn.commit()
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Ahead goal")
        assert "ahead of schedule" in section

    def test_commitment_preview_visually_separated_as_own_box(self, auth_client, db_conn, test_user):
        acc = _add_account(db_conn, test_user["id"], "Savings", balance=1000.0, acc_type="savings")
        _add_goal(db_conn, test_user["id"], "Box goal", 5000.0, linked_account_id=acc)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Box goal")
        idx = section.find('id="commitPreview')
        assert idx != -1
        assert "background:#eef2ff" in section[idx:idx + 200]

    def test_set_commitment_redirects_with_goal_card_anchor(self, auth_client, db_conn, test_user):
        acc = _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Anchor goal", 5000.0)
        resp = auth_client.post("/settings/set-goal-commitment", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid, "amount": "50", "from_account": "Current",
        })
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith(f"#goal-card-{gid}")

    def test_goal_card_has_matching_anchor_id(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Card id goal", 5000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert f'id="goal-card-{gid}"' in body

    def test_save_confirmation_js_present(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "JS confirm goal", 5000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert "goal-card-" in body and "scrollIntoView" in body


# ── STAGE 5: thousand separators, touch target, pause/resume, reset ─────────
class TestStage5Polish:
    def test_moneyfmt_filter_adds_thousand_separators(self, app):
        import app as app_module
        assert app_module.moneyfmt_filter(30000.0) == "30,000.00"
        assert app_module.moneyfmt_filter(994.0) == "994.00"
        assert app_module.moneyfmt_filter(1234567.5) == "1,234,567.50"

    def test_moneyfmt_filter_handles_bad_input_gracefully(self, app):
        import app as app_module
        assert app_module.moneyfmt_filter("not a number") == "not a number"

    def test_target_amount_comma_formatted_on_card(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "Big target goal", 30000.0)
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Big target goal", size=2500)
        assert "£30,000.00" in section

    def test_slider_touch_target_css_present(self, auth_client, db_conn, test_user):
        _add_goal(db_conn, test_user["id"], "CSS goal", 5000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert ".goal-commit-slider::-webkit-slider-thumb" in body
        assert "width: 44px; height: 44px;" in body

    def test_reset_to_suggested_js_present(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "Reset goal", 5000.0)
        resp = auth_client.get("/manage?tab=goals")
        body = resp.get_data(as_text=True)
        assert "function resetGoalCommitToSuggested" in body
        assert f"resetGoalCommitToSuggested({gid}," in body


class TestPauseResumeCommitment:
    def _set_commitment(self, auth_client, goal_id, amount, from_account):
        return auth_client.post("/settings/set-goal-commitment", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": goal_id, "amount": str(amount), "from_account": from_account,
        })

    def test_pause_route_toggles_is_paused_without_deleting_row(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Pause goal", 5000.0)
        self._set_commitment(auth_client, gid, 100, "Current")
        row = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert row["is_paused"] in (0, None)

        resp = auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        assert resp.status_code == 302
        row = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert row["is_paused"] == 1
        assert row["amount"] == 100.0  # amount/account preserved, not deleted
        assert row["from_account"] == "Current"

        # Toggling again resumes it
        resp2 = auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        assert resp2.status_code == 302
        row = db_conn.execute("SELECT * FROM savings_rules WHERE goal_id=?", (gid,)).fetchone()
        assert row["is_paused"] == 0

    def test_pause_route_requires_ownership(self, auth_client, db_conn, test_user):
        resp = auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": 999999,
        })
        assert resp.status_code == 302
        assert db_conn.execute("SELECT COUNT(*) as c FROM savings_rules WHERE goal_id=999999").fetchone()["c"] == 0

    def test_paused_rule_excluded_from_safe_to_spend(self, app, auth_client, db_conn, test_user):
        import flask_login
        import app as app_module
        _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        _add_income(db_conn, test_user["id"], amount=2000.0)
        gid = _add_goal(db_conn, test_user["id"], "S2S pause goal", 5000.0)
        self._set_commitment(auth_client, gid, 300, "Current")

        with app.test_request_context():
            user = app_module.load_user(str(test_user["id"]))
            flask_login.login_user(user)
            s2s_active = app_module._get_safe_to_spend(test_user["id"])

        auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })

        with app.test_request_context():
            user = app_module.load_user(str(test_user["id"]))
            flask_login.login_user(user)
            s2s_paused = app_module._get_safe_to_spend(test_user["id"])

        # Whatever the rule's day-in-cycle status, pausing it can only ever
        # raise (never lower) Safe to Spend, since it stops being deducted.
        assert s2s_paused >= s2s_active

    def test_paused_commitment_shows_paused_summary_not_live_slider(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Paused UI goal", 5000.0)
        self._set_commitment(auth_client, gid, 150, "Current")
        auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Paused UI goal")
        assert "⏸ Paused" in section
        assert "Resume commitment" in section
        assert f'id="commitSlider{gid}"' not in section

    def test_resumed_commitment_shows_live_slider_again(self, auth_client, db_conn, test_user):
        _add_account(db_conn, test_user["id"], "Current", balance=1000.0)
        gid = _add_goal(db_conn, test_user["id"], "Resumed UI goal", 5000.0)
        self._set_commitment(auth_client, gid, 150, "Current")
        auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        resp = auth_client.get("/manage?tab=goals")
        section = _goal_section(resp.get_data(as_text=True), "Resumed UI goal")
        assert f'id="commitSlider{gid}"' in section
        assert "⏸ Paused" not in section

    def test_pausing_with_no_existing_commitment_is_a_safe_noop(self, auth_client, db_conn, test_user):
        gid = _add_goal(db_conn, test_user["id"], "No commitment goal", 5000.0)
        resp = auth_client.post("/settings/toggle-goal-commitment-pause", data={
            "csrf_token": csrf()["csrf_token"], "goal_id": gid,
        })
        assert resp.status_code == 302  # redirects with a message, doesn't error
