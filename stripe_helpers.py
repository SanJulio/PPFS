"""
Stripe Subscription helpers - deliberately dependency-free (no `app`, no
`database`, no Flask) so both app.py and the one-off migration scripts
can import this without triggering app.py's module-level init_db() call.

Written for the September 2026 production incident documented in
CLAUDE.md's "Pricing/entitlement model" section: the 2025-03-31.basil
Stripe API change removed the top-level current_period_end/
current_period_start fields from the Subscription object and moved them
to each SubscriptionItem (items.data[].current_period_end). Code that
read live_sub.current_period_end directly - the webhook handler and the
Stripe reconciliation script - crashed with AttributeError on every real
subscription-state event once production's (unpinned) stripe package
resolved to a basil-or-later default API version.
"""
import logging

logger = logging.getLogger(__name__)


def extract_subscription_period_end(live_sub, *, cancel_at_period_end):
    """
    Returns the Unix timestamp that should be stored as a subscription's
    relevant billing-period-end, or None if it genuinely cannot be
    determined. Callers decide whether None is acceptable given the
    subscription's status (see ACCESS_GRANTING_STATUSES in app.py) - this
    function never guesses at that policy, it only ever extracts.

    Every attribute read here uses getattr(..., default) specifically
    because accessing a genuinely-missing field via dot access on a real
    stripe.StripeObject raises AttributeError, not None - confirmed
    empirically against stripe's own StripeObject implementation. This is
    exactly what crashed production: live_sub.current_period_end raised
    AttributeError once Stripe stopped returning that field.

    Priority:
      1. cancel_at (a top-level Subscription field, unaffected by the
         basil change - it's a cancellation-scheduling concept, not a
         billing-period one) when cancel_at_period_end is true and
         cancel_at is set. This is more directly authoritative than a
         billing period's end for "when will paid access actually stop" -
         it's literally the field named for that question, and it's
         immune to the items-shape change entirely.
      2. Legacy top-level current_period_end, if present (pre-basil API
         versions, or any object shape that happens to still carry it -
         never assumed absent, always checked first before falling
         through to the items path).
      3. The subscription's item(s) current_period_end:
         - exactly one item (Spendara's own checkout - see
           billing_upgrade() in app.py - always creates exactly one line
           item, so this is the expected shape for every real Spendara
           subscription): use it.
         - zero items: unresolvable - log a warning (subscription id
           only, no customer/email/name) and return None. The caller
           decides whether that's fatal for this status.
         - multiple items with differing current_period_end values: take
           the MAXIMUM - never under-estimate how long paid access should
           last - and log a warning. Never silently pick items.data[0];
           an arbitrary choice here could wrongly shorten a paying
           customer's access.
    """
    sub_id = getattr(live_sub, "id", "?")

    cancel_at = getattr(live_sub, "cancel_at", None)
    if cancel_at_period_end and cancel_at:
        return cancel_at

    legacy = getattr(live_sub, "current_period_end", None)
    if legacy is not None:
        return legacy

    items = getattr(live_sub, "items", None)
    data = list(getattr(items, "data", None) or [])
    ends = [
        e for e in (getattr(item, "current_period_end", None) for item in data)
        if e is not None
    ]

    if not ends:
        logger.warning(
            f"Subscription {sub_id}: no resolvable current_period_end "
            f"(0 items, or item(s) missing the field)"
        )
        return None

    if len(set(ends)) > 1:
        logger.warning(
            f"Subscription {sub_id}: {len(ends)} items have differing "
            f"current_period_end values - using the maximum"
        )

    return max(ends)
