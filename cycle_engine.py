"""
cycle_engine.py — Single source of truth for a user's current budget cycle.

Public API
----------
get_cycle(user_id, today=None) -> dict

Returned dict keys
------------------
display_start          : date  — start of the Financial Overview display period
display_end            : date  — end of the display period (≥30 days for weekly users)
safe_boundary          : date  — next actual payday; used for safe-to-spend calculation only
mode_used              : str   — 'automatic' or 'manual'
primary_source_name    : str | None — name of the income source used in automatic mode

Separation principle
--------------------
display_end  — governs what period the Financial Overview shows (Income received,
               Bills paid, Bills left tile).  Extended to 30 days min for weekly users.
safe_boundary — governs which bills are deducted from safe-to-spend.  Always the
               next real payday; never extended.

Fallback chain
--------------
automatic → no primary source      : fall back to manual
automatic → no past payment dates  : fall back to manual
automatic → any exception          : fall back to manual
manual → no day stored             : use day 1 of current month
"""

import logging
from datetime import date, timedelta

import income_engine

logger = logging.getLogger(__name__)

# Minimum display period length for high-frequency income (weekly / fortnightly).
_MIN_DISPLAY_DAYS = 29  # display_end = max(next_payday-1, display_start + 29)
_LOOKBACK_DAYS = 90     # how far back to search for the last payday
_LOOKAHEAD_DAYS = 90    # how far forward to search for the next payday


# ── HELPERS ─────────────────────────────────────────────────────────────────────

def _get_db_and_cursor():
    from database import get_db
    db = get_db()
    cur = db.cursor()
    return db, cur


def _release(db):
    from database import release_db
    release_db(db)


def _use_postgres():
    from database import USE_POSTGRES
    return USE_POSTGRES


# ── MANUAL PATH ─────────────────────────────────────────────────────────────────

def _manual_cycle(user_id: int, today: date) -> dict:
    """Return cycle dict computed from the user's stored budget_cycle_start day."""
    try:
        db, cur = _get_db_and_cursor()
        if _use_postgres():
            cur.execute("SELECT budget_cycle_start FROM users WHERE id = %s", (user_id,))
        else:
            cur.execute("SELECT budget_cycle_start FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        cur.close()
        _release(db)
        start_day = int(row[0]) if row and row[0] else 1
    except Exception as e:
        logger.debug(f"cycle_engine: could not load budget_cycle_start: {e}")
        start_day = 1

    start_day = max(1, min(28, start_day))

    from dateutil.relativedelta import relativedelta
    if today.day >= start_day:
        display_start = today.replace(day=start_day)
        display_end = (display_start + relativedelta(months=1)) - timedelta(days=1)
    else:
        display_start = (today.replace(day=1) - timedelta(days=1)).replace(day=start_day)
        display_end = today.replace(day=start_day) - timedelta(days=1)

    # safe_boundary = display_end inclusive means all bills in the display period are
    # counted toward safe-to-spend, identical to current behaviour.
    safe_boundary = display_end

    return {
        "display_start": display_start,
        "display_end": display_end,
        "safe_boundary": safe_boundary,
        "mode_used": "manual",
        "primary_source_name": None,
    }


# ── AUTOMATIC PATH ───────────────────────────────────────────────────────────────

def _automatic_cycle(user_id: int, today: date) -> dict | None:
    """
    Return cycle dict from the primary income source, or None to trigger fallback.
    """
    try:
        db, cur = _get_db_and_cursor()
        if _use_postgres():
            cur.execute("SELECT * FROM income WHERE user_id = %s AND is_primary = 1", (user_id,))
        else:
            cur.execute("SELECT * FROM income WHERE user_id = ? AND is_primary = 1", (user_id,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        _release(db)

        if not rows:
            return None  # no primary source → fall back

        primary_inc = dict(zip(cols, rows[0]))
    except Exception as e:
        logger.debug(f"cycle_engine: could not load primary income: {e}")
        return None

    try:
        # Most recent payment on or before today
        past_dates = income_engine.get_payment_dates(
            primary_inc,
            today - timedelta(days=_LOOKBACK_DAYS),
            today,
        )
        if not past_dates:
            return None  # brand-new source with no past payments yet → fall back

        display_start = past_dates[-1]

        # Next payment strictly after today
        future_dates = income_engine.get_payment_dates(
            primary_inc,
            today + timedelta(days=1),
            today + timedelta(days=_LOOKAHEAD_DAYS),
        )
        if not future_dates:
            return None  # can't determine next payday → fall back

        next_payday = future_dates[0]
        safe_boundary = next_payday - timedelta(days=1)   # day before next payday
        display_end = safe_boundary

        # Extend display window to at least _MIN_DISPLAY_DAYS for high-frequency income
        if (display_end - display_start).days < _MIN_DISPLAY_DAYS:
            display_end = display_start + timedelta(days=_MIN_DISPLAY_DAYS)
            # safe_boundary stays as-is (next real payday - 1)

        return {
            "display_start": display_start,
            "display_end": display_end,
            "safe_boundary": safe_boundary,
            "mode_used": "automatic",
            "primary_source_name": primary_inc.get("name"),
        }
    except Exception as e:
        logger.debug(f"cycle_engine: automatic cycle computation failed: {e}")
        return None


# ── PUBLIC API ───────────────────────────────────────────────────────────────────

def get_cycle(user_id: int, today: date | None = None) -> dict:
    """
    Return the current budget cycle for user_id.

    Always returns a valid dict — all errors fall back to manual mode silently.
    """
    if today is None:
        today = date.today()

    try:
        # Load cycle_mode
        db, cur = _get_db_and_cursor()
        if _use_postgres():
            cur.execute("SELECT cycle_mode FROM users WHERE id = %s", (user_id,))
        else:
            cur.execute("SELECT cycle_mode FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        cur.close()
        _release(db)
        cycle_mode = (row[0] if row and row[0] else "manual") or "manual"
    except Exception as e:
        logger.debug(f"cycle_engine: could not load cycle_mode: {e}")
        cycle_mode = "manual"

    if cycle_mode == "automatic":
        result = _automatic_cycle(user_id, today)
        if result is not None:
            return result
        # Fall through to manual

    return _manual_cycle(user_id, today)


def get_next_cycle_start(user_id: int, today: date | None = None) -> date | None:
    """
    Return the date when the NEXT cycle starts (useful for settings display).
    Returns None if it cannot be determined.
    """
    if today is None:
        today = date.today()

    cycle = get_cycle(user_id, today)
    mode = cycle["mode_used"]

    if mode == "automatic":
        # Next cycle starts on safe_boundary + 1 day (= next_payday)
        return cycle["safe_boundary"] + timedelta(days=1)

    # Manual: next cycle start = display_end + 1 = next occurrence of the start day
    return cycle["display_end"] + timedelta(days=1)
