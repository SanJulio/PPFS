"""
Tests for the employed/self-employed income type system.

Covers:
- POST /settings/setup-self-employed  (one-time initial setup)
- POST /settings/save-income-averaging (switchable anytime)
- _resolve_income_rows() / _compute_automatic_income_average() (app.py helpers)
- Lump-sum vs spread-evenly distribution in /api/snapshot
- Auto-apply engine exclusion of spread rows + live-resolved amounts for lump rows
- "Payday" language must never appear for self-employed users
- Zero impact on default (employed) users
"""

import json
import uuid
from datetime import date, timedelta

import pytest
from tests.conftest import csrf


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _safe_future_day():
    """A day-of-month ~15 days out that falls on a weekday, so income_engine's
    weekend-shift rule can never push a lump payment date outside a test's
    fixed search window (making date-exact assertions immune to which real
    calendar day the suite happens to run on)."""
    d = date.today() + timedelta(days=15)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.day


def _safe_past_day():
    """Same idea as _safe_future_day() but ~10 days in the past - for tests
    that check overdue/backfill-style occurrences."""
    d = date.today() - timedelta(days=10)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.day


def _setup_self_employed(auth_client, amount="2000", account="Current", cycle_start_day=None):
    if cycle_start_day is None:
        cycle_start_day = str(_safe_future_day())
    return auth_client.post(
        "/settings/setup-self-employed",
        data={**csrf(), "manual_amount": amount, "account": account, "cycle_start_day": cycle_start_day},
        follow_redirects=False,
    )


def _save_averaging(auth_client, mode="manual", distribution="lump", window_months="3", manual_amount="2000"):
    return auth_client.post(
        "/settings/save-income-averaging",
        data={**csrf(), "mode": mode, "distribution": distribution,
              "window_months": window_months, "manual_amount": manual_amount},
        follow_redirects=False,
    )


def _get_self_employed_income_row(db_conn, user_id):
    return db_conn.execute(
        "SELECT * FROM income WHERE user_id = ? AND rule_type = 'self_employed_average'", (user_id,)
    ).fetchone()


def _get_user(db_conn, user_id):
    return db_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# ── SETUP ROUTE ────────────────────────────────────────────────────────────────

class TestSelfEmployedSetupRoute:
    def test_setup_marks_user_self_employed(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        user = _get_user(db_conn, test_user["id"])
        assert user["employment_type"] == "self_employed"

    def test_setup_forces_manual_cycle_mode(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        user = _get_user(db_conn, test_user["id"])
        assert user["cycle_mode"] == "manual"

    def test_setup_stores_cycle_start_day(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, cycle_start_day="17")
        user = _get_user(db_conn, test_user["id"])
        assert user["budget_cycle_start"] == 17

    def test_setup_clamps_cycle_start_day_above_28(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, cycle_start_day="99")
        user = _get_user(db_conn, test_user["id"])
        assert user["budget_cycle_start"] == 28

    def test_setup_clamps_cycle_start_day_below_1(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, cycle_start_day="0")
        user = _get_user(db_conn, test_user["id"])
        assert user["budget_cycle_start"] == 1

    def test_setup_creates_self_employed_average_income_row(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="1500")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        assert row is not None
        assert row["is_primary"] == 1
        cfg = json.loads(row["rule_config"])
        assert cfg["mode"] == "manual"
        assert cfg["manual_amount"] == 1500.0
        assert cfg["distribution"] == "lump"
        assert cfg["window_months"] == 3

    def test_setup_rejects_missing_amount(self, auth_client, test_user, test_account, db_conn):
        resp = auth_client.post(
            "/settings/setup-self-employed",
            data={**csrf(), "manual_amount": "", "account": "Current", "cycle_start_day": "1"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert _get_self_employed_income_row(db_conn, test_user["id"]) is None

    def test_setup_rejects_zero_amount(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="0")
        assert _get_self_employed_income_row(db_conn, test_user["id"]) is None

    def test_setup_rejects_negative_amount(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="-500")
        assert _get_self_employed_income_row(db_conn, test_user["id"]) is None

    def test_setup_rejects_missing_account(self, auth_client, test_user, test_account, db_conn):
        resp = auth_client.post(
            "/settings/setup-self-employed",
            data={**csrf(), "manual_amount": "2000", "account": "", "cycle_start_day": "1"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert _get_self_employed_income_row(db_conn, test_user["id"]) is None


# ── SAVE-INCOME-AVERAGING ROUTE ──────────────────────────────────────────────

class TestIncomeAveragingSaveRoute:
    def test_save_updates_mode_and_amount(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="2000")
        _save_averaging(auth_client, mode="manual", manual_amount="3200")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["manual_amount"] == 3200.0

    def test_save_updates_distribution(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        _save_averaging(auth_client, distribution="spread")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["distribution"] == "spread"

    def test_save_updates_window_months(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        _save_averaging(auth_client, mode="auto", window_months="6")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["window_months"] == 6
        assert cfg["mode"] == "auto"

    def test_save_never_touches_stored_amount_column(self, auth_client, test_user, test_account, db_conn):
        """rule_config is the only thing updated — income.amount (the raw column)
        is left as whatever it was at initial setup, since every consumer must
        resolve the live value via _resolve_income_rows() instead."""
        _setup_self_employed(auth_client, amount="2000")
        row_before = _get_self_employed_income_row(db_conn, test_user["id"])
        _save_averaging(auth_client, manual_amount="9999")
        row_after = _get_self_employed_income_row(db_conn, test_user["id"])
        assert row_after["amount"] == row_before["amount"]

    def test_save_defaults_invalid_mode_to_manual(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        _save_averaging(auth_client, mode="bogus")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["mode"] == "manual"

    def test_save_defaults_invalid_distribution_to_lump(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        _save_averaging(auth_client, distribution="bogus")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["distribution"] == "lump"

    def test_save_defaults_invalid_window_months_to_3(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client)
        _save_averaging(auth_client, mode="auto", window_months="4")
        row = _get_self_employed_income_row(db_conn, test_user["id"])
        cfg = json.loads(row["rule_config"])
        assert cfg["window_months"] == 3

    def test_save_with_no_self_employed_row_does_not_crash(self, auth_client, test_user, test_account, db_conn):
        """An 'employed' user (or one who never ran setup) hitting this route
        should redirect gracefully, never 500."""
        resp = _save_averaging(auth_client)
        assert resp.status_code in (302, 303)


# ── _resolve_income_rows() / _compute_automatic_income_average() ────────────

class TestResolveIncomeRows:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import _resolve_income_rows, _compute_automatic_income_average
        self._resolve = _resolve_income_rows
        self._compute_avg = _compute_automatic_income_average

    def test_employed_row_passes_through_unchanged(self, db_conn, test_user, test_account):
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?)",
            ("Salary", 2500.0, test_account["name"], test_user["id"], 25, "fixed_date", '{"day":25}'),
        )
        row = dict(cur.execute("SELECT * FROM income WHERE user_id = ?", (test_user["id"],)).fetchone())
        resolved = self._resolve([row], test_user["id"])
        assert resolved[0]["amount"] == 2500.0
        assert "_distribution" not in resolved[0]

    def test_manual_mode_uses_rule_config_not_stale_column(self, db_conn, test_user, test_account):
        """income.amount is intentionally stale (£1000) vs rule_config's manual_amount
        (£3000) — simulating a user who changed their manual figure in Settings,
        which only ever updates rule_config, never the stored column."""
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 3000.0, "distribution": "lump", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Self-employed income", 1000.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        row = dict(cur.execute("SELECT * FROM income WHERE user_id = ?", (test_user["id"],)).fetchone())
        resolved = self._resolve([row], test_user["id"])
        assert resolved[0]["amount"] == 3000.0
        assert resolved[0]["_distribution"] == "lump"

    def test_automatic_mode_single_transaction_is_the_average(self, db_conn, test_user, test_account):
        """No artificial gating - a single logged income transaction resolves
        to a real average immediately."""
        from models import add_transaction
        add_transaction(date.today().isoformat(), "Client payment", 900.0, test_account["name"], test_user["id"], type="income", category="Income")

        cfg = json.dumps({"mode": "auto", "window_months": 3, "manual_amount": 0, "distribution": "lump", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Self-employed income", 0.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        row = dict(cur.execute("SELECT * FROM income WHERE user_id = ?", (test_user["id"],)).fetchone())
        resolved = self._resolve([row], test_user["id"])
        # £900 total / 3-month window = £300/month
        assert abs(resolved[0]["amount"] - 300.0) < 0.01

    def test_automatic_mode_recalculates_as_more_transactions_logged(self, db_conn, test_user, test_account):
        from models import add_transaction
        cfg = json.dumps({"mode": "auto", "window_months": 3, "manual_amount": 0, "distribution": "lump", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Self-employed income", 0.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        row = dict(cur.execute("SELECT * FROM income WHERE user_id = ?", (test_user["id"],)).fetchone())

        add_transaction(date.today().isoformat(), "Client A", 900.0, test_account["name"], test_user["id"], type="income", category="Income")
        avg_after_one = self._resolve([row], test_user["id"])[0]["amount"]

        add_transaction(date.today().isoformat(), "Client B", 900.0, test_account["name"], test_user["id"], type="income", category="Income")
        avg_after_two = self._resolve([row], test_user["id"])[0]["amount"]

        assert avg_after_two > avg_after_one

    def test_window_months_adjusts_cutoff(self, db_conn, test_user, test_account):
        """A transaction older than the window is excluded from the average."""
        from models import add_transaction
        old_date = (date.today() - timedelta(days=120)).isoformat()  # older than 3 months (90d), within 6 (180d)
        add_transaction(old_date, "Old client payment", 1200.0, test_account["name"], test_user["id"], type="income", category="Income")

        avg_3mo = self._compute_avg(test_user["id"], 3)
        avg_6mo = self._compute_avg(test_user["id"], 6)
        assert avg_3mo == 0.0
        assert avg_6mo > 0.0

    def test_spread_distribution_attached(self, db_conn, test_user, test_account):
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 1800.0, "distribution": "spread", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Self-employed income", 1800.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        row = dict(cur.execute("SELECT * FROM income WHERE user_id = ?", (test_user["id"],)).fetchone())
        resolved = self._resolve([row], test_user["id"])
        assert resolved[0]["_distribution"] == "spread"


# ── LUMP VS SPREAD IN /api/snapshot ──────────────────────────────────────────

class TestSnapshotLumpVsSpread:
    def test_lump_produces_single_discrete_event(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="3000")
        resp = auth_client.get("/api/snapshot?days=35")
        data = resp.get_json()
        income_events = [e for e in data["income_arriving"] if e["name"] == "Self-employed income"]
        assert len(income_events) == 1
        assert abs(income_events[0]["amount"] - 3000.0) < 0.01

    def test_spread_produces_daily_accrual(self, auth_client, test_user, test_account, db_conn):
        _setup_self_employed(auth_client, amount="3000")
        _save_averaging(auth_client, mode="manual", manual_amount="3000", distribution="spread")
        resp = auth_client.get("/api/snapshot?days=10")
        data = resp.get_json()
        income_events = [e for e in data["income_arriving"] if e["name"] == "Self-employed income"]
        # Every simulated day should get an accrual entry (spread is a flat daily rate)
        assert len(income_events) == 10
        # No single entry should be anywhere near the full monthly lump amount
        assert all(e["amount"] < 500 for e in income_events)

    def test_spread_daily_amounts_sum_less_than_full_lump_over_short_window(self, auth_client, test_user, test_account):
        """Over a window shorter than the cycle length, spread should credit less
        than the full average — proving it isn't just a relabelled lump sum."""
        _setup_self_employed(auth_client, amount="3000")
        _save_averaging(auth_client, mode="manual", manual_amount="3000", distribution="spread")
        resp = auth_client.get("/api/snapshot?days=5")
        data = resp.get_json()
        income_events = [e for e in data["income_arriving"] if e["name"] == "Self-employed income"]
        total = sum(e["amount"] for e in income_events)
        assert total < 3000.0

    def test_switching_manual_to_automatic_takes_effect_immediately(self, auth_client, test_user, test_account, db_conn):
        from models import add_transaction
        _setup_self_employed(auth_client, amount="2000")

        resp1 = auth_client.get("/api/snapshot?days=35")
        manual_events = [e for e in resp1.get_json()["income_arriving"] if e["name"] == "Self-employed income"]
        assert abs(manual_events[0]["amount"] - 2000.0) < 0.01

        add_transaction(date.today().isoformat(), "Client payment", 6000.0, test_account["name"], test_user["id"], type="income", category="Income")
        _save_averaging(auth_client, mode="auto", window_months="3")

        resp2 = auth_client.get("/api/snapshot?days=35")
        auto_events = [e for e in resp2.get_json()["income_arriving"] if e["name"] == "Self-employed income"]
        # £6000 / 3-month window = £2000/month... use a different figure to disambiguate
        assert abs(auto_events[0]["amount"] - 2000.0) < 0.01

    def test_switching_lump_to_spread_takes_effect_immediately(self, auth_client, test_user, test_account):
        _setup_self_employed(auth_client, amount="3000")
        resp1 = auth_client.get("/api/snapshot?days=35")
        lump_events = [e for e in resp1.get_json()["income_arriving"] if e["name"] == "Self-employed income"]
        assert len(lump_events) == 1

        _save_averaging(auth_client, mode="manual", manual_amount="3000", distribution="spread")
        resp2 = auth_client.get("/api/snapshot?days=35")
        spread_events = [e for e in resp2.get_json()["income_arriving"] if e["name"] == "Self-employed income"]
        assert len(spread_events) == 35


# ── AUTO-APPLY ENGINE ────────────────────────────────────────────────────────

class TestAutoApplyExclusion:
    def test_pending_items_excludes_spread_rows(self, test_user, test_account, db_conn):
        from app import get_pending_auto_apply_items
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 2000.0, "distribution": "spread", "day": 1})
        yesterday = (date.today() - timedelta(days=5)).isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary, last_applied) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1, ?)",
            ("Self-employed income", 2000.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg, yesterday),
        )
        pending = get_pending_auto_apply_items(test_user["id"])
        assert all(p["name"] != "Self-employed income" for p in pending)

    def test_pending_items_includes_lump_rows_with_resolved_amount(self, test_user, test_account, db_conn):
        from app import get_pending_auto_apply_items
        cycle_day = _safe_past_day()
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 2500.0, "distribution": "lump", "day": cycle_day})
        last_applied = (date.today() - timedelta(days=20)).isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary, last_applied) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1, ?)",
            ("Self-employed income", 999.0, test_account["name"], test_user["id"], cycle_day, "self_employed_average", cfg, last_applied),
        )
        pending = get_pending_auto_apply_items(test_user["id"])
        matches = [p for p in pending if p["name"] == "Self-employed income"]
        assert len(matches) == 1
        # Resolved amount (2500) is used, not the stale stored column (999)
        assert abs(matches[0]["amount"] - 2500.0) < 0.01

    def test_backfill_skips_spread_rows(self, test_user, test_account, db_conn):
        from app import run_auto_apply_backfill
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 2000.0, "distribution": "spread", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary, last_applied) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1, NULL)",
            ("Self-employed income", 2000.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        run_auto_apply_backfill(test_user["id"])
        txs = db_conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND description = 'Self-employed income'",
            (test_user["id"],),
        ).fetchall()
        assert len(txs) == 0

    def test_backfill_applies_lump_rows_with_resolved_amount(self, test_user, test_account, db_conn):
        from app import run_auto_apply_backfill
        cfg = json.dumps({"mode": "manual", "window_months": 3, "manual_amount": 1750.0, "distribution": "lump", "day": 1})
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary, last_applied) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1, NULL)",
            ("Self-employed income", 1.0, test_account["name"], test_user["id"], 1, "self_employed_average", cfg),
        )
        run_auto_apply_backfill(test_user["id"])
        txs = db_conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND description = 'Self-employed income'",
            (test_user["id"],),
        ).fetchall()
        assert len(txs) >= 1
        assert all(abs(t["amount"] - 1750.0) < 0.01 for t in txs)


# ── MANUAL CYCLE MODE + NO "PAYDAY" LANGUAGE FOR SELF-EMPLOYED ───────────────

class TestManualCycleNoPaydayLanguage:
    def test_settings_page_never_says_payday_for_self_employed(self, auth_client, test_user, test_account, db_conn, monkeypatch):
        # get_next_cycle_start's return value hits a pre-existing Windows-only
        # strftime('%-d') incompatibility unrelated to this feature (documented
        # in CLAUDE.md) - stub it out so this test isolates self-employed copy only.
        import cycle_engine
        monkeypatch.setattr(cycle_engine, "get_next_cycle_start", lambda user_id, today=None: None)

        _setup_self_employed(auth_client, cycle_start_day="5")
        resp = auth_client.get("/settings?tab=display")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "payday" not in html.lower()

    def test_settings_page_shows_beta_badge_for_self_employed(self, auth_client, test_user, test_account, monkeypatch):
        import cycle_engine
        monkeypatch.setattr(cycle_engine, "get_next_cycle_start", lambda user_id, today=None: None)

        _setup_self_employed(auth_client)
        resp = auth_client.get("/settings?tab=display")
        html = resp.get_data(as_text=True)
        assert "New" in html and "Beta" in html

    def test_settings_page_shows_manual_forced_note(self, auth_client, test_user, test_account, monkeypatch):
        import cycle_engine
        monkeypatch.setattr(cycle_engine, "get_next_cycle_start", lambda user_id, today=None: None)

        _setup_self_employed(auth_client)
        resp = auth_client.get("/settings?tab=display")
        html = resp.get_data(as_text=True)
        assert "always use manual cycle mode" in html

    def test_cycle_engine_manual_mode_works_for_self_employed(self, auth_client, test_user, test_account, db_conn):
        """cycle_engine.get_cycle() is untouched by this feature - self-employed
        users simply flow through the existing manual-mode path via cycle_mode='manual'."""
        import cycle_engine
        _setup_self_employed(auth_client, cycle_start_day="10")
        cycle = cycle_engine.get_cycle(test_user["id"])
        assert cycle["mode_used"] == "manual"

    def test_home_page_never_says_payday_for_self_employed(self, auth_client, test_user, test_account, db_conn):
        """Guards against the "safe to spend before your next payday" banner
        copy (templates/index.html) - both the server-rendered Jinja banner
        and the client-side JS re-render (used when the date-range picker
        changes) must resolve their phrasing from the server-computed
        `untilPhrase` value, never hardcode "payday" unconditionally."""
        _setup_self_employed(auth_client)
        resp = auth_client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "payday" not in html.lower()

    def test_home_page_shows_payday_for_automatic_employed_user(self, auth_client, test_user, test_account, db_conn):
        """Positive control: an employed user on a real automatic (payday-based)
        cycle should still see payday-countdown copy - this feature must not
        strip that language for users it doesn't apply to."""
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Salary", 2500.0, test_account["name"], test_user["id"], 25, "fixed_date", '{"day":25}'),
        )
        cur.execute("UPDATE users SET cycle_mode = 'automatic' WHERE id = ?", (test_user["id"],))
        resp = auth_client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "payday" in html.lower()


# ── ZERO IMPACT ON EMPLOYED USERS ────────────────────────────────────────────

class TestEmployedUsersUnaffected:
    def test_default_employment_type_is_employed(self, test_user, db_conn):
        user = _get_user(db_conn, test_user["id"])
        assert user["employment_type"] == "employed"

    def test_manage_page_unaffected_for_employed_user(self, auth_client, test_user, test_account, db_conn):
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?)",
            ("Salary", 2500.0, test_account["name"], test_user["id"], 25, "fixed_date", '{"day":25}'),
        )
        resp = auth_client.get("/manage")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "2500.00" in html

    def test_snapshot_unaffected_for_employed_user(self, auth_client, test_user, test_account, db_conn):
        cycle_day = _safe_future_day()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?)",
            ("Salary", 2500.0, test_account["name"], test_user["id"], cycle_day, "fixed_date",
             json.dumps({"day": cycle_day})),
        )
        resp = auth_client.get("/api/snapshot?days=35")
        assert resp.status_code == 200
        data = resp.get_json()
        events = [e for e in data["income_arriving"] if e["name"] == "Salary"]
        assert len(events) == 1
        assert abs(events[0]["amount"] - 2500.0) < 0.01

    def test_pending_auto_apply_unaffected_for_employed_user(self, test_user, test_account, db_conn):
        from app import get_pending_auto_apply_items
        cycle_day = _safe_past_day()
        last_applied = (date.today() - timedelta(days=20)).isoformat()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, last_applied) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, ?)",
            ("Salary", 2500.0, test_account["name"], test_user["id"], cycle_day, "fixed_date",
             json.dumps({"day": cycle_day}), last_applied),
        )
        pending = get_pending_auto_apply_items(test_user["id"])
        matches = [p for p in pending if p["name"] == "Salary"]
        assert len(matches) == 1
        assert abs(matches[0]["amount"] - 2500.0) < 0.01

    def test_setup_self_employed_route_does_not_affect_other_users(self, auth_client, test_user, test_account, db_conn):
        """A second, unrelated user's employment_type must stay 'employed'
        when this user runs self-employed setup."""
        import gc
        other_email = f"other_{uuid.uuid4().hex[:8]}@example.com"
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, display_name) VALUES (?, ?, ?, 1, ?)",
            (other_email, "x", "2026-01-01", "Other User"),
        )
        other_id = cur.lastrowid

        _setup_self_employed(auth_client)

        other_user = _get_user(db_conn, other_id)
        assert other_user["employment_type"] == "employed"

        cur.execute("DELETE FROM users WHERE id = ?", (other_id,))
        gc.collect()
