"""
Tests for bill_engine.shift_weekend_to_monday().
"""

from datetime import date

from bill_engine import shift_weekend_to_monday


def test_saturday_shifts_to_following_monday():
    # 1 Aug 2026 is a Saturday
    assert shift_weekend_to_monday(date(2026, 8, 1)) == date(2026, 8, 3)


def test_sunday_shifts_to_following_monday():
    # 2 Aug 2026 is a Sunday
    assert shift_weekend_to_monday(date(2026, 8, 2)) == date(2026, 8, 3)


def test_weekday_is_unchanged():
    # 3 Aug 2026 is a Monday
    assert shift_weekend_to_monday(date(2026, 8, 3)) == date(2026, 8, 3)
    # Friday
    assert shift_weekend_to_monday(date(2026, 7, 31)) == date(2026, 7, 31)


def test_month_end_saturday_shifts_into_next_month():
    # 31 Oct 2026 is a Saturday - shift crosses into November
    assert shift_weekend_to_monday(date(2026, 10, 31)) == date(2026, 11, 2)


def test_does_not_mutate_input():
    d = date(2026, 8, 1)
    shift_weekend_to_monday(d)
    assert d == date(2026, 8, 1)
