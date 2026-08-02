"""
income_engine.py — Canonical payment date calculation for all income recurrence types.

Frequencies:
  weekly      — every 7 days on a chosen weekday (Mon–Fri)
  fortnightly — every 14 days anchored to first_payment_date
  4-weekly    — every 28 days anchored to first_payment_date
  monthly     — rule-based: fixed date / relative to month end /
                last working day / nth weekday of month
  yearly      — once a year on a specific month + day

Weekend and UK (England & Wales) bank holiday adjustments are applied after
computing the nominal date.  Bank holiday data is fetched from gov.uk and
cached in memory, refreshed at most once per calendar day.
"""

import json
import logging
import calendar
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── BANK HOLIDAY CACHE ──────────────────────────────────────────────────────────

_bh_cache: dict = {"fetched_on": None, "dates": set()}


def _fetch_bank_holidays() -> set:
    """Fetch England & Wales bank holidays from gov.uk.  Returns a set of dates."""
    try:
        import requests
        r = requests.get("https://www.gov.uk/bank-holidays.json", timeout=5)
        r.raise_for_status()
        events = r.json()["england-and-wales"]["events"]
        return {date.fromisoformat(e["date"]) for e in events}
    except Exception as exc:
        logger.warning("income_engine: bank holiday fetch failed: %s", exc)
        return set()


def get_bank_holidays() -> set:
    """Return cached England & Wales bank holidays, refreshed once per calendar day."""
    today = date.today()
    if _bh_cache["fetched_on"] != today:
        _bh_cache["dates"] = _fetch_bank_holidays()
        _bh_cache["fetched_on"] = today
    return _bh_cache["dates"]


# ── DATE ADJUSTMENT ─────────────────────────────────────────────────────────────

def _is_working_day(d: date, bh: set) -> bool:
    return d.weekday() < 5 and d not in bh


def _adjust_weekend(d: date, rule: str) -> date:
    """Shift d if it falls on a weekend according to rule (before/after/nearest)."""
    if d.weekday() < 5:
        return d
    if rule == "after":
        while d.weekday() >= 5:
            d += timedelta(days=1)
    elif rule == "nearest":
        # Saturday → Friday, Sunday → Monday
        d = d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1)
    else:  # "before" (default)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d


def _adjust_bank_holiday(d: date, rule: str, bh: set) -> date:
    """Shift d if it falls on a bank holiday according to rule (before/after/nearest)."""
    if d not in bh:
        return d
    if rule == "after":
        d += timedelta(days=1)
        while not _is_working_day(d, bh):
            d += timedelta(days=1)
    elif rule == "nearest":
        before, after = d - timedelta(days=1), d + timedelta(days=1)
        while not _is_working_day(before, bh):
            before -= timedelta(days=1)
        while not _is_working_day(after, bh):
            after += timedelta(days=1)
        d = before if (d - before) <= (after - d) else after
    else:  # "before" (default)
        d -= timedelta(days=1)
        while not _is_working_day(d, bh):
            d -= timedelta(days=1)
    return d


def _apply_rules(d: date, weekend_rule: str, bh_rule: str, bh: set) -> date:
    """Apply weekend adjustment then bank holiday adjustment."""
    d = _adjust_weekend(d, weekend_rule)
    d = _adjust_bank_holiday(d, bh_rule, bh)
    return d


# ── MONTHLY NOMINAL DATE ────────────────────────────────────────────────────────

def _monthly_nominal(rule_type: str, cfg: dict, year: int, month: int) -> Optional[date]:
    """Return the raw (unadjusted) payment date for the given rule and month."""
    last = calendar.monthrange(year, month)[1]

    if rule_type == "fixed_date":
        day = int(cfg.get("day", 25))
        return date(year, month, min(day, last))

    elif rule_type == "self_employed_average":
        # Lump-sum anchor date for a self-employed user's averaged income —
        # "day" here is their manual-cycle cycle-start day, not a real payday.
        # (Spread-evenly distribution bypasses get_payment_dates() entirely —
        # see app.py's forecast/overview call sites — so this branch only
        # ever matters for the lump-sum case.)
        day = int(cfg.get("day", 1))
        return date(year, month, min(day, last))

    elif rule_type == "relative_month_end":
        offset = int(cfg.get("offset", 0))
        direction = str(cfg.get("direction", "before"))
        working_only = bool(cfg.get("working_days_only", False))
        d = date(year, month, last)
        if offset == 0:
            return d
        if working_only:
            bh = get_bank_holidays()
            steps = 0
            while steps < offset:
                d += timedelta(days=-1 if direction == "before" else 1)
                if _is_working_day(d, bh):
                    steps += 1
        else:
            d += timedelta(days=-offset if direction == "before" else offset)
        return d

    elif rule_type == "last_working_day":
        bh = get_bank_holidays()
        d = date(year, month, last)
        while not _is_working_day(d, bh):
            d -= timedelta(days=1)
        return d

    elif rule_type == "nth_weekday":
        nth = str(cfg.get("nth", "last"))
        weekday = int(cfg.get("weekday", 4))  # 0=Mon … 4=Fri
        if nth == "last":
            d = date(year, month, last)
            while d.weekday() != weekday:
                d -= timedelta(days=1)
            return d
        else:
            target = {"first": 1, "second": 2, "third": 3, "fourth": 4}.get(nth, 1)
            count = 0
            d = date(year, month, 1)
            while d.month == month:
                if d.weekday() == weekday:
                    count += 1
                    if count == target:
                        return d
                d += timedelta(days=1)
            return None  # pattern doesn't exist this month

    return None


# ── PUBLIC API ──────────────────────────────────────────────────────────────────

def get_payment_dates(income: dict, from_date: date, to_date: date) -> list:
    """
    Return a sorted list of payment dates within [from_date, to_date] inclusive.

    Backward-compatible: income rows without rule_type use the pre-2026 logic
    (raw day/weekly_day, no weekend or bank-holiday adjustment).
    """
    bh = get_bank_holidays()
    freq = (income.get("frequency") or "monthly").lower().strip()
    weekend_rule = income.get("weekend_rule") or "before"
    bh_rule = income.get("bank_holiday_rule") or "before"
    rule_type = income.get("rule_type") or None

    try:
        cfg = json.loads(income.get("rule_config") or "{}")
    except (TypeError, ValueError):
        cfg = {}

    # ── Legacy path: rows created before this system ──────────────────────────
    if not rule_type:
        if freq == "weekly":
            wd = int(income.get("weekly_day") if income.get("weekly_day") is not None else 4)
            days_to = (wd - from_date.weekday()) % 7
            d = from_date + timedelta(days=days_to)
            result = []
            while d <= to_date:
                result.append(d)
                d += timedelta(days=7)
            return result
        else:  # monthly legacy
            pay_day = int(income.get("day") or 1)
            result = []
            yr, mo = from_date.year, from_date.month
            while date(yr, mo, 1) <= to_date:
                last = calendar.monthrange(yr, mo)[1]
                d = date(yr, mo, min(pay_day, last))
                if from_date <= d <= to_date:
                    result.append(d)
                mo += 1
                if mo > 12:
                    mo, yr = 1, yr + 1
            return result

    # ── New recurrence system ─────────────────────────────────────────────────
    dates: list = []

    if freq == "weekly":
        wd = int(income.get("weekly_day") if income.get("weekly_day") is not None else 4)
        days_to = (wd - from_date.weekday()) % 7
        d = from_date + timedelta(days=days_to)
        # Start a week early to catch adjustment-shifted dates
        d -= timedelta(days=7)
        while d <= to_date + timedelta(days=7):
            adj = _apply_rules(d, weekend_rule, bh_rule, bh)
            if from_date <= adj <= to_date:
                dates.append(adj)
            d += timedelta(days=7)

    elif freq == "fortnightly":
        anchor_str = income.get("first_payment_date")
        if not anchor_str:
            return []
        anchor = date.fromisoformat(str(anchor_str))
        d = anchor
        # Wind forward to just before from_date, then step back one period
        while d + timedelta(days=14) < from_date:
            d += timedelta(days=14)
        d -= timedelta(days=14)
        while d <= to_date + timedelta(days=7):
            adj = _apply_rules(d, weekend_rule, bh_rule, bh)
            if from_date <= adj <= to_date:
                dates.append(adj)
            d += timedelta(days=14)

    elif freq == "4-weekly":
        anchor_str = income.get("first_payment_date")
        if not anchor_str:
            return []
        anchor = date.fromisoformat(str(anchor_str))
        d = anchor
        while d + timedelta(days=28) < from_date:
            d += timedelta(days=28)
        d -= timedelta(days=28)
        while d <= to_date + timedelta(days=7):
            adj = _apply_rules(d, weekend_rule, bh_rule, bh)
            if from_date <= adj <= to_date:
                dates.append(adj)
            d += timedelta(days=28)

    elif freq == "monthly":
        # Ensure cfg carries the day for fixed_date rules built from legacy columns
        if rule_type == "fixed_date" and not cfg.get("day"):
            cfg["day"] = int(income.get("day") or 25)
        yr, mo = from_date.year, from_date.month
        # Start one month early to catch adjustments that cross month boundaries
        mo -= 1
        if mo == 0:
            mo, yr = 12, yr - 1
        while date(yr, mo, 1) <= to_date:
            nominal = _monthly_nominal(rule_type, cfg, yr, mo)
            if nominal:
                adj = _apply_rules(nominal, weekend_rule, bh_rule, bh)
                if from_date <= adj <= to_date:
                    dates.append(adj)
            mo += 1
            if mo > 12:
                mo, yr = 1, yr + 1

    elif freq == "yearly":
        rule_day = int(cfg.get("day", 1))
        rule_month = int(cfg.get("month", 1))
        for yr in range(from_date.year - 1, to_date.year + 2):
            last = calendar.monthrange(yr, rule_month)[1]
            nominal = date(yr, rule_month, min(rule_day, last))
            adj = _apply_rules(nominal, weekend_rule, bh_rule, bh)
            if from_date <= adj <= to_date:
                dates.append(adj)

    return sorted(set(dates))


def get_next_dates(income: dict, n: int = 3, from_date: Optional[date] = None) -> list:
    """Return the next n payment dates starting from from_date (default: today)."""
    start = from_date or date.today()
    return get_payment_dates(income, start, start + timedelta(days=730))[:n]


def describe_rule(income: dict) -> str:
    """Return a short human-readable summary of the income recurrence rule."""
    freq = (income.get("frequency") or "monthly").lower().strip()
    rule_type = income.get("rule_type") or None

    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    try:
        cfg = json.loads(income.get("rule_config") or "{}")
    except (TypeError, ValueError):
        cfg = {}

    wd = int(income.get("weekly_day") if income.get("weekly_day") is not None else 4)

    if freq == "weekly":
        return f"Every {DAYS[wd]}"

    elif freq == "fortnightly":
        return f"Every other {DAYS[wd]}"

    elif freq == "4-weekly":
        return f"Every 4 weeks ({DAYS[wd]})"

    elif freq == "monthly":
        if not rule_type or rule_type == "fixed_date":
            day = int(cfg.get("day") or income.get("day") or 1)
            suffix = "th"
            if day % 100 not in (11, 12, 13):
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            return f"{day}{suffix} of each month"
        elif rule_type == "relative_month_end":
            offset = int(cfg.get("offset", 0))
            direction = cfg.get("direction", "before")
            wo = " (working days)" if cfg.get("working_days_only") else ""
            return f"{offset} days {direction} month end{wo}"
        elif rule_type == "last_working_day":
            return "Last working day of month"
        elif rule_type == "nth_weekday":
            nth = str(cfg.get("nth", "last")).capitalize()
            w = int(cfg.get("weekday", 4))
            return f"{nth} {DAYS[w]} of month"
        elif rule_type == "self_employed_average":
            mode = cfg.get("mode", "manual")
            mode_label = "auto-calculated" if mode == "auto" else "manual"
            if cfg.get("distribution") == "spread":
                return f"Averaged income ({mode_label}), spread across your cycle"
            day = int(cfg.get("day") or 1)
            suffix = "th"
            if day % 100 not in (11, 12, 13):
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            return f"Averaged income ({mode_label}), on the {day}{suffix} of your cycle"

    elif freq == "yearly":
        day = int(cfg.get("day", 1))
        month = int(cfg.get("month", 1))
        return f"{day} {MONTHS[month - 1]} each year"

    return freq.capitalize()
