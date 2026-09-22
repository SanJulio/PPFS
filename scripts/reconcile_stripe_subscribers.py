"""
Mandatory Stripe reconciliation - Migrate phase of the entitlement cutover.

Run this AFTER the Expand-phase schema/code deploy, BEFORE the Legacy Free
backfill, and BEFORE ENTITLEMENT_RESOLVER_ACTIVE is ever set to true. Not
optional, not best-effort: no user who is genuinely paying for Pro today
may resolve to Legacy Free or Basic once the resolver activates, and the
only way to guarantee that is for every is_pro=1 user's stripe_* mirror
fields to be populated from Stripe's own live state before the resolver
can ever read them.

Usage:
    python scripts/reconcile_stripe_subscribers.py [--apply]

Without --apply, runs as a dry run: reports what it WOULD do, changes
nothing. With --apply, writes the reconciled state.

Exit code is 0 only when 100% of is_pro=1 users with a stripe_customer_id
now have a non-NULL stripe_subscription_status - use this as the release
gate before Activation. Re-running is safe (idempotent: re-fetching and
overwriting with current Stripe truth is harmless) - if this reports a
failure, fix the underlying issue (network, API key, rate limit) and
re-run rather than proceeding with partial coverage.

Users with is_pro=1 and NO stripe_customer_id (e.g. historical grants via
the now-retired /admin/grant-pro) cannot be reconciled from Stripe - there
is nothing to look up. These are explicitly enumerated and logged here,
not silently swept into the general reconciliation, and not silently
dropped either: see the Migrate-phase grandfathering step, which is what
actually classifies them (as Legacy Free, same as every other pre-cutover
user - no payment was ever behind this grant, so this isn't downgrading a
paying customer).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import stripe
from app import app as flask_app, ACCESS_GRANTING_STATUSES, _apply_subscription_state, USE_POSTGRES
from database import get_db, release_db


def _live_subscription_for_customer(customer_id):
    """Returns the most relevant live Subscription object for a Stripe
    customer, or None if they have none. Prefers an access-granting
    status if more than one subscription exists; otherwise the most
    recently created."""
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
    candidates = list(subs.auto_paging_iter())
    if not candidates:
        return None
    for sub in candidates:
        if sub.status in ACCESS_GRANTING_STATUSES:
            return sub
    return max(candidates, key=lambda s: s.created)


def main():
    apply_changes = "--apply" in sys.argv

    with flask_app.app_context():
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT id, email, stripe_customer_id FROM users WHERE is_pro = 1")
        else:
            cursor.execute("SELECT id, email, stripe_customer_id FROM users WHERE is_pro = 1")
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        users = [dict(zip(cols, r)) for r in rows]
        cursor.close()
        release_db(db)

        with_customer = [u for u in users if u.get("stripe_customer_id")]
        without_customer = [u for u in users if not u.get("stripe_customer_id")]

        print(f"is_pro=1 users: {len(users)} total — {len(with_customer)} with a Stripe customer, {len(without_customer)} without.")

        if without_customer:
            print("\n--- Users with is_pro=1 and NO stripe_customer_id (cannot be Stripe-reconciled) ---")
            print("These will be grandfathered to Legacy Free by the backfill step, same as every other pre-cutover user:")
            for u in without_customer:
                print(f"  user_id={u['id']} email={u['email']}")

        failures = []
        reconciled = 0
        for u in with_customer:
            try:
                live_sub = _live_subscription_for_customer(u["stripe_customer_id"])
                if live_sub is None:
                    print(f"  user_id={u['id']} email={u['email']}: no subscriptions found for customer {u['stripe_customer_id']} — SKIPPING")
                    failures.append(u["id"])
                    continue
                print(f"  user_id={u['id']} email={u['email']}: live status={live_sub.status}, "
                      f"cancel_at_period_end={live_sub.cancel_at_period_end}, current_period_end={live_sub.current_period_end}")
                if apply_changes:
                    db2 = get_db()
                    cursor2 = db2.cursor()
                    try:
                        _apply_subscription_state(
                            cursor2, user_id=u["id"], customer_id=u["stripe_customer_id"],
                            status=live_sub.status, cancel_at_period_end=live_sub.cancel_at_period_end,
                            current_period_end=live_sub.current_period_end, allow_incomplete_write=True,
                        )
                        db2.commit()
                        reconciled += 1
                    except Exception:
                        db2.rollback()
                        raise
                    finally:
                        cursor2.close()
                        release_db(db2)
            except Exception as e:
                print(f"  user_id={u['id']} email={u['email']}: ERROR — {e}")
                failures.append(u["id"])

        print(f"\n{'APPLIED' if apply_changes else 'DRY RUN'}: {reconciled}/{len(with_customer)} reconciled, {len(failures)} failures.")
        if failures:
            print(f"FAILED user_ids: {failures}")
            print("Coverage is NOT complete. Fix the issue and re-run before proceeding to the backfill/Activation.")
            sys.exit(1)
        if not apply_changes:
            print("Dry run only - re-run with --apply to write these changes.")
            sys.exit(0)
        print("Coverage complete - safe to proceed to the Legacy Free backfill.")
        sys.exit(0)


if __name__ == "__main__":
    main()
