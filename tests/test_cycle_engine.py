"""
Tests for cycle_engine.get_cycle().

Strategy: patch database calls so the engine can run without Flask app context.
All tests use the public get_cycle() function and check the returned dict.
"""

import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock


# ── HELPERS ──────────────────────────────────────────────────────────────────────

def _make_income_row(
    id=1, name="Salary", amount=2000.0, frequency="monthly",
    day=15, weekly_day=4, rule_type="fixed_date", rule_config='{"day":15}',
    weekend_rule="before", bank_holiday_rule="before",
    first_payment_date=None, is_primary=1, user_id=1,
):
    return {
        "id": id, "name": name, "amount": amount, "frequency": frequency,
        "day": day, "weekly_day": weekly_day, "rule_type": rule_type,
        "rule_config": rule_config, "weekend_rule": weekend_rule,
        "bank_holiday_rule": bank_holiday_rule,
        "first_payment_date": first_payment_date,
        "is_primary": is_primary, "user_id": user_id,
    }


def _patch_db(cycle_mode="manual", budget_cycle_start=1, income_rows=None):
    """
    Return a context-manager that patches cycle_engine's DB calls.
    income_rows: list of dicts to return for SELECT FROM income WHERE is_primary=1.
    """
    if income_rows is None:
        income_rows = []

    calls = []

    def fake_get_db():
        db = MagicMock()
        cur = MagicMock()
        db.cursor.return_value = cur

        def execute_side_effect(sql, params=()):
            calls.append(sql.strip().lower()[:40])
            if "cycle_mode" in sql:
                cur.fetchone.return_value = (cycle_mode,)
                cur.description = [("cycle_mode",)]
            elif "budget_cycle_start" in sql:
                cur.fetchone.return_value = (budget_cycle_start,)
                cur.description = [("budget_cycle_start",)]
            elif "is_primary" in sql and income_rows:
                row = income_rows[0]
                keys = list(row.keys())
                cur.description = [(k,) for k in keys]
                cur.fetchall.return_value = [tuple(row[k] for k in keys)]
            elif "is_primary" in sql:
                cur.description = [("id",)]
                cur.fetchall.return_value = []

        cur.execute.side_effect = execute_side_effect
        return db

    return patch.multiple(
        "cycle_engine",
        _get_db_and_cursor=lambda: (fake_get_db(), fake_get_db().cursor()),
        _release=lambda db: None,
        _use_postgres=lambda: False,
    )


# ── MANUAL MODE TESTS ────────────────────────────────────────────────────────────

class TestManualCycle:
    def test_manual_day15_mid_month(self):
        """Paid on 15th, today is the 22nd → cycle 15 May – 14 Jun."""
        import cycle_engine
        today = date(2026, 5, 22)

        with patch.object(cycle_engine, "_manual_cycle", wraps=cycle_engine._manual_cycle):
            # Call _manual_cycle directly, bypassing DB
            with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
                 patch("cycle_engine._release"), \
                 patch("cycle_engine._use_postgres", return_value=False):
                # Mock for cycle_mode fetch
                db1, cur1 = MagicMock(), MagicMock()
                cur1.fetchone.return_value = ("manual",)
                db1.cursor.return_value = cur1

                # Mock for budget_cycle_start fetch inside _manual_cycle
                db2, cur2 = MagicMock(), MagicMock()
                cur2.fetchone.return_value = (15,)
                db2.cursor.return_value = cur2

                mock_gdb.side_effect = [
                    (db1, cur1),  # get_cycle reads cycle_mode
                    (db2, cur2),  # _manual_cycle reads budget_cycle_start
                ]

                result = cycle_engine.get_cycle(1, today=today)

        assert result["mode_used"] == "manual"
        assert result["display_start"] == date(2026, 5, 15)
        assert result["display_end"] == date(2026, 6, 14)
        assert result["safe_boundary"] == date(2026, 6, 14)
        assert result["primary_source_name"] is None

    def test_manual_day15_before_payday(self):
        """Paid on 15th, today is the 10th → cycle 15 Apr – 14 May."""
        import cycle_engine
        today = date(2026, 5, 10)

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            db1, cur1 = MagicMock(), MagicMock()
            cur1.fetchone.return_value = ("manual",)
            db1.cursor.return_value = cur1
            db2, cur2 = MagicMock(), MagicMock()
            cur2.fetchone.return_value = (15,)
            db2.cursor.return_value = cur2
            mock_gdb.side_effect = [(db1, cur1), (db2, cur2)]

            result = cycle_engine.get_cycle(1, today=today)

        assert result["display_start"] == date(2026, 4, 15)
        assert result["display_end"] == date(2026, 5, 14)

    def test_manual_day1_uses_calendar_month(self):
        """Default day=1 → calendar month cycle."""
        import cycle_engine
        today = date(2026, 5, 22)

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            db1, cur1 = MagicMock(), MagicMock()
            cur1.fetchone.return_value = ("manual",)
            db1.cursor.return_value = cur1
            db2, cur2 = MagicMock(), MagicMock()
            cur2.fetchone.return_value = (1,)
            db2.cursor.return_value = cur2
            mock_gdb.side_effect = [(db1, cur1), (db2, cur2)]

            result = cycle_engine.get_cycle(1, today=today)

        assert result["display_start"] == date(2026, 5, 1)
        assert result["display_end"] == date(2026, 5, 31)

    def test_manual_missing_day_falls_back_to_day1(self):
        """If budget_cycle_start is NULL, fall back to day 1."""
        import cycle_engine
        today = date(2026, 5, 22)

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            db1, cur1 = MagicMock(), MagicMock()
            cur1.fetchone.return_value = ("manual",)
            db1.cursor.return_value = cur1
            db2, cur2 = MagicMock(), MagicMock()
            cur2.fetchone.return_value = (None,)
            db2.cursor.return_value = cur2
            mock_gdb.side_effect = [(db1, cur1), (db2, cur2)]

            result = cycle_engine.get_cycle(1, today=today)

        assert result["display_start"].day == 1


# ── AUTOMATIC MODE TESTS ─────────────────────────────────────────────────────────

def _setup_auto_mocks(mock_gdb, today, income_row):
    """Wire up mock_gdb calls for automatic mode: cycle_mode + is_primary income row."""
    db1, cur1 = MagicMock(), MagicMock()
    cur1.fetchone.return_value = ("automatic",)
    db1.cursor.return_value = cur1

    db2, cur2 = MagicMock(), MagicMock()
    keys = list(income_row.keys())
    cur2.description = [(k,) for k in keys]
    cur2.fetchall.return_value = [tuple(income_row[k] for k in keys)]
    db2.cursor.return_value = cur2

    mock_gdb.side_effect = [(db1, cur1), (db2, cur2)]


class TestAutomaticMonthly:
    def test_monthly_fixed_date_mid_cycle(self):
        """Paid on 15th monthly (fixed_date), today 22 May → start 15 May, end 14 Jun."""
        import cycle_engine
        today = date(2026, 5, 22)
        inc = _make_income_row(day=15, rule_type="fixed_date", rule_config='{"day":15}')

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            _setup_auto_mocks(mock_gdb, today, inc)
            result = cycle_engine.get_cycle(1, today=today)

        assert result["mode_used"] == "automatic"
        assert result["display_start"] == date(2026, 5, 15)
        assert result["display_end"] == date(2026, 6, 14)
        assert result["safe_boundary"] == date(2026, 6, 14)
        assert result["primary_source_name"] == "Salary"

    def test_monthly_payday_is_today(self):
        """When today IS payday, cycle starts today."""
        import cycle_engine
        today = date(2026, 5, 15)
        inc = _make_income_row(day=15, rule_type="fixed_date", rule_config='{"day":15}')

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            _setup_auto_mocks(mock_gdb, today, inc)
            result = cycle_engine.get_cycle(1, today=today)

        assert result["display_start"] == date(2026, 5, 15)
        assert result["safe_boundary"] == date(2026, 6, 14)


class TestAutomaticWeekly:
    def test_weekly_extends_display_to_30_days(self):
        """Weekly paid (Friday), display period extended to ≥30 days; safe_boundary = next Friday - 1."""
        import cycle_engine
        # Last Friday was May 15, next Friday May 22, today May 18
        today = date(2026, 5, 18)  # Monday
        inc = _make_income_row(
            name="Weekly Wage",
            frequency="weekly",
            rule_type=None,
            rule_config="{}",
            weekly_day=4,  # Friday
        )

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            _setup_auto_mocks(mock_gdb, today, inc)
            result = cycle_engine.get_cycle(1, today=today)

        assert result["mode_used"] == "automatic"
        # Display period must be at least 30 days
        assert (result["display_end"] - result["display_start"]).days >= 29
        # safe_boundary is the day before next Friday (May 22 - 1 = May 21)
        assert result["safe_boundary"] == date(2026, 5, 21)
        # safe_boundary must be less than display_end (which is extended)
        assert result["safe_boundary"] < result["display_end"]


class TestAutomaticFallbacks:
    def test_no_primary_falls_back_to_manual(self):
        """Automatic mode but no primary income → falls back to manual."""
        import cycle_engine
        today = date(2026, 5, 22)

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            # automatic mode
            db1, cur1 = MagicMock(), MagicMock()
            cur1.fetchone.return_value = ("automatic",)
            db1.cursor.return_value = cur1
            # no primary income row
            db2, cur2 = MagicMock(), MagicMock()
            cur2.description = [("id",)]
            cur2.fetchall.return_value = []
            db2.cursor.return_value = cur2
            # manual fallback: budget_cycle_start
            db3, cur3 = MagicMock(), MagicMock()
            cur3.fetchone.return_value = (1,)
            db3.cursor.return_value = cur3
            mock_gdb.side_effect = [(db1, cur1), (db2, cur2), (db3, cur3)]

            result = cycle_engine.get_cycle(1, today=today)

        assert result["mode_used"] == "manual"

    def test_db_error_falls_back_to_manual(self):
        """Any DB exception during automatic path → silent fallback to manual."""
        import cycle_engine
        today = date(2026, 5, 22)

        def boom():
            raise RuntimeError("DB down")

        with patch("cycle_engine._get_db_and_cursor") as mock_gdb, \
             patch("cycle_engine._release"), \
             patch("cycle_engine._use_postgres", return_value=False):
            db1, cur1 = MagicMock(), MagicMock()
            cur1.fetchone.return_value = ("automatic",)
            db1.cursor.return_value = cur1
            # second call raises
            db2, cur2 = MagicMock(), MagicMock()
            cur2.execute.side_effect = RuntimeError("DB down")
            db2.cursor.return_value = cur2
            # manual fallback
            db3, cur3 = MagicMock(), MagicMock()
            cur3.fetchone.return_value = (1,)
            db3.cursor.return_value = cur3
            mock_gdb.side_effect = [(db1, cur1), (db2, cur2), (db3, cur3)]

            result = cycle_engine.get_cycle(1, today=today)

        assert result["mode_used"] == "manual"
