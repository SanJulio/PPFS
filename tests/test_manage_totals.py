"""Tests for _monthly_eq and normalised_totals in app.py."""

import pytest


class TestMonthlyEq:
    def _fn(self, amount, frequency):
        from app import _monthly_eq
        return _monthly_eq(amount, frequency)

    def test_monthly_unchanged(self):
        assert self._fn(900.0, 'monthly') == pytest.approx(900.0)

    def test_weekly_394_30(self):
        # 394.30 × 52 / 12 = 1708.6333...
        assert self._fn(394.30, 'weekly') == pytest.approx(394.30 * 52 / 12, rel=1e-6)

    def test_fortnightly(self):
        assert self._fn(800.0, 'fortnightly') == pytest.approx(800.0 * 26 / 12, rel=1e-6)

    def test_four_weekly(self):
        assert self._fn(600.0, '4-weekly') == pytest.approx(600.0 * 13 / 12, rel=1e-6)

    def test_yearly(self):
        assert self._fn(1200.0, 'yearly') == pytest.approx(100.0, rel=1e-6)

    def test_none_frequency_defaults_to_monthly(self):
        assert self._fn(500.0, None) == pytest.approx(500.0)

    def test_unknown_frequency_defaults_to_monthly(self):
        assert self._fn(500.0, 'biannual') == pytest.approx(500.0)


class TestNormalisedTotals:
    def _fn(self, income_rows, bill_rows):
        from app import normalised_totals
        return normalised_totals(income_rows, bill_rows)

    def test_single_weekly_income(self):
        income = [{'amount': 394.30, 'frequency': 'weekly'}]
        inc_m, inc_a, bil_m, bil_a = self._fn(income, [])
        assert inc_m == pytest.approx(394.30 * 52 / 12, rel=1e-6)
        assert inc_a == pytest.approx(394.30 * 52, rel=1e-6)   # £20,503.60
        assert bil_m == pytest.approx(0.0)
        assert bil_a == pytest.approx(0.0)

    def test_single_monthly_bill(self):
        bills = [{'amount': 900.0, 'frequency': 'monthly'}]
        inc_m, inc_a, bil_m, bil_a = self._fn([], bills)
        assert bil_m == pytest.approx(900.0)
        assert bil_a == pytest.approx(10800.0)
        assert inc_m == pytest.approx(0.0)

    def test_mixed_frequencies(self):
        income = [
            {'amount': 2000.0, 'frequency': 'monthly'},
            {'amount': 100.0, 'frequency': 'weekly'},
        ]
        inc_m, inc_a, _, _ = self._fn(income, [])
        expected_m = 2000.0 + 100.0 * 52 / 12
        assert inc_m == pytest.approx(expected_m, rel=1e-6)
        assert inc_a == pytest.approx(expected_m * 12, rel=1e-6)

    def test_yearly_bill_normalises_to_monthly(self):
        bills = [{'amount': 1200.0, 'frequency': 'yearly'}]
        _, _, bil_m, bil_a = self._fn([], bills)
        assert bil_m == pytest.approx(100.0, rel=1e-6)
        assert bil_a == pytest.approx(1200.0, rel=1e-6)

    def test_empty_lists(self):
        inc_m, inc_a, bil_m, bil_a = self._fn([], [])
        assert inc_m == 0.0
        assert inc_a == 0.0
        assert bil_m == 0.0
        assert bil_a == 0.0
