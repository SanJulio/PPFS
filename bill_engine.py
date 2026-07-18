"""
Shifts a scheduled bill's calculated occurrence date forward to the next
working day when it lands on a Saturday or Sunday — matching how most UK
Direct Debits and standing orders actually behave.

This never touches the stored `day` (or `month`) config on a bill; it only
adjusts the date that gets calculated/displayed for a given occurrence.

Bank holidays are not accounted for (weekday-only shift for now).
"""

from datetime import date, timedelta

SATURDAY = 5
SUNDAY = 6


def shift_weekend_to_monday(d: date) -> date:
    """If d falls on a Saturday or Sunday, return the following Monday. Otherwise return d unchanged."""
    if d.weekday() == SATURDAY:
        return d + timedelta(days=2)
    if d.weekday() == SUNDAY:
        return d + timedelta(days=1)
    return d
