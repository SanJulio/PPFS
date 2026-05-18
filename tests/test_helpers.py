"""
Tests for pure helper functions — no database or HTTP involved.

Covers:
- validate_amount()
- validate_day()
- _get_occurrences_between()
- get_cycle_dates()
"""

import pytest
from datetime import date


# ── validate_amount ────────────────────────────────────────────────────────────
class TestValidateAmount:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import validate_amount
        self.validate_amount = validate_amount

    def test_positive_integer(self):
        val, err = self.validate_amount("50")
        assert val == 50.0
        assert err is None

    def test_positive_float(self):
        val, err = self.validate_amount("12.99")
        assert abs(val - 12.99) < 1e-9
        assert err is None

    def test_zero_is_rejected(self):
        val, err = self.validate_amount("0")
        assert val is None
        assert err is not None

    def test_negative_is_rejected(self):
        val, err = self.validate_amount("-5")
        assert val is None
        assert err is not None

    def test_non_numeric_string(self):
        val, err = self.validate_amount("abc")
        assert val is None
        assert err is not None

    def test_none_input(self):
        val, err = self.validate_amount(None)
        assert val is None
        assert err is not None

    def test_empty_string(self):
        val, err = self.validate_amount("")
        assert val is None
        assert err is not None

    def test_very_large_amount(self):
        val, err = self.validate_amount("999999.99")
        assert val == 999999.99
        assert err is None

    def test_fractional_penny(self):
        val, err = self.validate_amount("0.001")
        assert val is not None
        assert err is None


# ── validate_day ──────────────────────────────────────────────────────────────
class TestValidateDay:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import validate_day
        self.validate_day = validate_day

    def test_day_1(self):
        val, err = self.validate_day("1")
        assert val == 1
        assert err is None

    def test_day_31(self):
        val, err = self.validate_day("31")
        assert val == 31
        assert err is None

    def test_day_15(self):
        val, err = self.validate_day("15")
        assert val == 15
        assert err is None

    def test_day_0_rejected(self):
        val, err = self.validate_day("0")
        assert val is None
        assert err is not None

    def test_day_32_rejected(self):
        val, err = self.validate_day("32")
        assert val is None
        assert err is not None

    def test_negative_rejected(self):
        val, err = self.validate_day("-1")
        assert val is None
        assert err is not None

    def test_string_rejected(self):
        val, err = self.validate_day("monday")
        assert val is None
        assert err is not None

    def test_none_rejected(self):
        val, err = self.validate_day(None)
        assert val is None
        assert err is not None

    def test_float_string_truncates(self):
        # int("15.5") raises ValueError — expect rejection
        val, err = self.validate_day("15.5")
        assert val is None
        assert err is not None


# ── _get_occurrences_between ──────────────────────────────────────────────────
class TestGetOccurrencesBetween:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import _get_occurrences_between
        self.fn = _get_occurrences_between

    # ── monthly ──
    def test_monthly_hits_within_range(self):
        item = {"frequency": "monthly", "day": 15}
        start = date(2026, 4, 1)
        end = date(2026, 4, 30)
        results = self.fn(item, start, end)
        assert date(2026, 4, 15) in results
        assert len(results) == 1

    def test_monthly_spans_two_months(self):
        item = {"frequency": "monthly", "day": 10}
        start = date(2026, 3, 1)
        end = date(2026, 4, 30)
        results = self.fn(item, start, end)
        assert date(2026, 3, 10) in results
        assert date(2026, 4, 10) in results
        assert len(results) == 2

    def test_monthly_day_before_start_excluded(self):
        item = {"frequency": "monthly", "day": 5}
        start = date(2026, 4, 10)
        end = date(2026, 4, 30)
        results = self.fn(item, start, end)
        assert len(results) == 0

    def test_monthly_day_after_end_excluded(self):
        item = {"frequency": "monthly", "day": 25}
        start = date(2026, 4, 1)
        end = date(2026, 4, 20)
        results = self.fn(item, start, end)
        assert len(results) == 0

    def test_monthly_day_31_in_april(self):
        # April only has 30 days — day 31 clamps to 30
        item = {"frequency": "monthly", "day": 31}
        start = date(2026, 4, 1)
        end = date(2026, 4, 30)
        results = self.fn(item, start, end)
        assert date(2026, 4, 30) in results

    def test_monthly_day_29_in_february_non_leap(self):
        # 2026 is not a leap year — day 29 clamps to 28
        item = {"frequency": "monthly", "day": 29}
        start = date(2026, 2, 1)
        end = date(2026, 2, 28)
        results = self.fn(item, start, end)
        assert date(2026, 2, 28) in results

    def test_monthly_default_frequency(self):
        # No 'frequency' key — defaults to monthly
        item = {"day": 1}
        start = date(2026, 5, 1)
        end = date(2026, 5, 31)
        results = self.fn(item, start, end)
        assert date(2026, 5, 1) in results

    # ── yearly ──
    def test_yearly_fires_on_correct_date(self):
        item = {"frequency": "yearly", "day": 15, "month": 6}
        start = date(2026, 1, 1)
        end = date(2026, 12, 31)
        results = self.fn(item, start, end)
        assert date(2026, 6, 15) in results
        assert len(results) == 1

    def test_yearly_fires_across_year_boundary(self):
        item = {"frequency": "yearly", "day": 1, "month": 3}
        start = date(2025, 1, 1)
        end = date(2026, 12, 31)
        results = self.fn(item, start, end)
        assert date(2025, 3, 1) in results
        assert date(2026, 3, 1) in results

    def test_yearly_outside_range_excluded(self):
        item = {"frequency": "yearly", "day": 1, "month": 12}
        start = date(2026, 1, 1)
        end = date(2026, 6, 30)
        results = self.fn(item, start, end)
        assert len(results) == 0

    # ── weekly ──
    def test_weekly_returns_empty(self):
        # Weekly items have no anchor date — _get_occurrences_between skips them
        item = {"frequency": "weekly", "day": 1}
        start = date(2026, 4, 1)
        end = date(2026, 4, 30)
        results = self.fn(item, start, end)
        assert results == []

    def test_empty_range(self):
        item = {"frequency": "monthly", "day": 15}
        start = date(2026, 4, 20)
        end = date(2026, 4, 14)  # end before start
        results = self.fn(item, start, end)
        assert results == []


# ── get_cycle_dates ───────────────────────────────────────────────────────────
class TestGetCycleDates:
    @pytest.fixture(autouse=True)
    def _import(self, app):
        from app import get_cycle_dates
        self.fn = get_cycle_dates

    def test_today_on_start_day(self):
        today = date(2026, 4, 1)
        start, end = self.fn(1, today)
        assert start == date(2026, 4, 1)
        assert end == date(2026, 4, 30)

    def test_today_after_start_day(self):
        today = date(2026, 4, 20)
        start, end = self.fn(15, today)
        assert start == date(2026, 4, 15)
        # cycle_end = start + 1 month - 1 day = May 15 - 1 = May 14
        assert end == date(2026, 5, 14)

    def test_today_before_start_day(self):
        today = date(2026, 4, 10)
        start, end = self.fn(15, today)
        # cycle_start = Mar 15, cycle_end = Apr 14
        assert start == date(2026, 3, 15)
        assert end == date(2026, 4, 14)

    def test_start_day_clamped_to_28(self):
        today = date(2026, 4, 28)
        start, end = self.fn(30, today)  # 30 clamps to 28
        assert start.day == 28

    def test_start_day_clamped_minimum_1(self):
        today = date(2026, 4, 5)
        start, end = self.fn(0, today)  # 0 clamps to 1
        assert start.day == 1

    def test_cycle_covers_one_month(self):
        today = date(2026, 5, 15)
        start, end = self.fn(15, today)
        from dateutil.relativedelta import relativedelta
        assert end == start + relativedelta(months=1) - relativedelta(days=1)

    def test_default_today(self):
        # Called without today — should use date.today() without error
        start, end = self.fn(1)
        assert start <= end
