"""
Tests for the September 2026 production incident: the 2025-03-31.basil
Stripe API change removed the top-level current_period_end field from
the Subscription object (moved to items.data[].current_period_end).
Code reading live_sub.current_period_end directly crashed with
AttributeError on every real subscription-state webhook event and every
reconciliation dry-run against a live, paid Stripe subscriber. Full
investigation and design in CLAUDE.md's "Pricing/entitlement model"
section.

Unlike tests/test_stripe_webhook.py's _FakeSub (a plain object with a
real current_period_end attribute - fine for testing status/dedup/
atomicity logic, but incapable of ever reproducing this bug), every
subscription object here is built via stripe.StripeObject.construct_from()
against a real payload dict - genuinely production-shaped, so a missing
top-level field raises AttributeError the same way it does against a
real stripe.Subscription, and a regression here fails loudly again
instead of being silently invisible to the suite.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import stripe
from stripe._stripe_object import StripeObject

sys.path.insert(0, str(Path(__file__).parent.parent))

from stripe_helpers import extract_subscription_period_end
from tests.test_stripe_webhook import _post_event, _get_user_row, _get_is_pro, pro_user_with_customer


def _stripe_sub(
    id="sub_prod_shaped",
    status="active",
    cancel_at_period_end=False,
    cancel_at=None,
    created=1_700_000_000,
    current_period_end=None,       # legacy top-level field - omitted entirely unless given
    item_period_ends=None,         # e.g. [123] for one item, [1,2] for two, [] for zero
):
    """Builds a real stripe.StripeObject (not a plain mock) matching an
    actual Subscription API response shape. When current_period_end is
    None, the key is omitted from the payload entirely (matching a real
    basil+ response) rather than set to None (which would be a different,
    unrealistic shape - Stripe never returns the key with a null value,
    it simply isn't present)."""
    payload = {
        "id": id, "object": "subscription", "status": status,
        "cancel_at_period_end": cancel_at_period_end, "cancel_at": cancel_at,
        "created": created,
        "customer": "cus_prod_shaped",
    }
    if current_period_end is not None:
        payload["current_period_end"] = current_period_end
    if item_period_ends is not None:
        payload["items"] = {
            "object": "list",
            "data": [
                {"id": f"si_{i}", "object": "subscription_item", "current_period_end": e}
                for i, e in enumerate(item_period_ends)
            ],
        }
    return StripeObject.construct_from(payload, "sk_test_x")


class TestExtractSubscriptionPeriodEnd:
    """Pure unit tests against the shared, dependency-free extractor."""

    def test_legacy_top_level_field_is_used(self):
        sub = _stripe_sub(current_period_end=111, item_period_ends=[999])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) == 111

    def test_basil_shape_single_item_is_used(self):
        """The real post-basil shape: no top-level field, one item."""
        sub = _stripe_sub(item_period_ends=[222])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) == 222

    def test_cancel_at_takes_priority_when_flag_true(self):
        sub = _stripe_sub(cancel_at_period_end=True, cancel_at=333, item_period_ends=[999])
        assert extract_subscription_period_end(sub, cancel_at_period_end=True) == 333

    def test_cancel_at_ignored_when_flag_false(self):
        """cancel_at can be set on the object without cancel_at_period_end
        being true in some Stripe states - must not be used then."""
        sub = _stripe_sub(cancel_at_period_end=False, cancel_at=333, item_period_ends=[444])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) == 444

    def test_cancel_at_period_end_true_but_cancel_at_unset_falls_through(self):
        sub = _stripe_sub(cancel_at_period_end=True, cancel_at=None, item_period_ends=[555])
        assert extract_subscription_period_end(sub, cancel_at_period_end=True) == 555

    def test_zero_items_and_no_legacy_field_returns_none(self):
        sub = _stripe_sub(item_period_ends=[])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) is None

    def test_no_items_key_at_all_returns_none(self):
        """A subscription object with no 'items' key whatsoever (not even
        an empty list) must not raise - getattr chains through safely."""
        sub = _stripe_sub(item_period_ends=None)
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) is None

    def test_multiple_items_same_value_returns_that_value(self):
        sub = _stripe_sub(item_period_ends=[666, 666])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) == 666

    def test_multiple_items_differing_values_returns_the_maximum(self):
        """Deterministic and safe: never silently pick items.data[0] -
        take the max so paid access is never under-estimated."""
        sub = _stripe_sub(item_period_ends=[100, 999, 500])
        assert extract_subscription_period_end(sub, cancel_at_period_end=False) == 999

    def test_warning_logs_contain_no_customer_identifiers(self, caplog):
        """The multi-item and zero-item warning paths must never leak
        emails/names - only the subscription id, which is not itself
        customer-identifying PII."""
        import logging
        with caplog.at_level(logging.WARNING, logger="stripe_helpers"):
            extract_subscription_period_end(_stripe_sub(id="sub_warn1", item_period_ends=[]), cancel_at_period_end=False)
            extract_subscription_period_end(_stripe_sub(id="sub_warn2", item_period_ends=[1, 2]), cancel_at_period_end=False)
        text = "\n".join(r.message for r in caplog.records)
        assert "sub_warn1" in text and "sub_warn2" in text
        assert "@" not in text  # no email-shaped content anywhere in the warnings


class TestWebhookProductionShapedObjects:
    """Webhook handler integration tests against real StripeObject
    fixtures - not _FakeSub - for exactly the scenarios that crashed
    production."""

    def test_checkout_completed_basil_shaped_object(self, client, db_conn, test_user):
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_basil1", "subscription": "sub_basil1"},
            live_sub=_stripe_sub(id="sub_basil1", status="active", item_period_ends=[2_100_000_000]),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, test_user["id"])
        assert bool(row["is_pro"]) is True
        assert row["stripe_current_period_end"] is not None

    def test_subscription_updated_basil_shaped_object(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_basil2", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_stripe_sub(id="sub_basil2", status="active", item_period_ends=[2_100_000_000]),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_subscription_deleted_basil_shaped_object(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client, "customer.subscription.deleted",
            {"object": "subscription", "id": "sub_basil3", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_stripe_sub(id="sub_basil3", status="canceled", item_period_ends=[]),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False

    def test_scheduled_cancellation_uses_cancel_at_not_item_period_end(self, client, db_conn, pro_user_with_customer):
        """cancel_at_period_end=true with a real cancel_at set - the
        stored value must be cancel_at, not the item's current_period_end
        (which is a different, later value here on purpose, to prove
        priority)."""
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_basil4", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_stripe_sub(
                id="sub_basil4", status="active", cancel_at_period_end=True,
                cancel_at=1_900_000_000, item_period_ends=[2_500_000_000],
            ),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, pro_user_with_customer["id"])
        assert row["stripe_current_period_end"] is not None
        # Stored via psycopg2/sqlite as a TIMESTAMP/TEXT - compare against
        # the exact expected value derived from cancel_at (1_900_000_000),
        # not the item's current_period_end (2_500_000_000), to directly
        # prove priority rather than a loose date-range guess.
        from datetime import datetime, timezone
        expected = datetime.fromtimestamp(1_900_000_000, tz=timezone.utc).replace(tzinfo=None)
        stored = row["stripe_current_period_end"]
        stored_dt = stored if not isinstance(stored, str) else datetime.fromisoformat(stored)
        assert stored_dt == expected
        assert bool(row["is_pro"]) is True

    def test_zero_items_active_status_rolls_back_and_returns_500(self, client, db_conn, pro_user_with_customer):
        """The core new guard: an access-granting subscription with an
        unresolvable period end must NOT be silently committed - the
        webhook must roll back, return 5xx, and leave the event
        unprocessed so Stripe retries."""
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_zero_items", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_stripe_sub(id="sub_zero_items", status="active", item_period_ends=[]),
            event_id="evt_zero_items",
        )
        assert resp.status_code == 500
        # Original state (from the pro_user_with_customer fixture) must be
        # untouched - the transaction rolled back, not partially applied.
        row = _get_user_row(db_conn, pro_user_with_customer["id"])
        assert bool(row["is_pro"]) is True
        assert row["stripe_subscription_status"] == "active"
        # And the event must NOT be recorded as processed, so a Stripe
        # retry (after the fix) would actually reprocess it.
        processed = db_conn.execute(
            "SELECT 1 FROM processed_stripe_events WHERE event_id = ?", ("evt_zero_items",)
        ).fetchone()
        assert processed is None

    def test_terminal_canceled_with_zero_items_does_not_require_period_end(self, client, db_conn, pro_user_with_customer):
        """A terminal status legitimately has no future billing period -
        must succeed without a resolvable current_period_end."""
        resp = _post_event(
            client, "customer.subscription.deleted",
            {"object": "subscription", "id": "sub_terminal_zero", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_stripe_sub(id="sub_terminal_zero", status="canceled", item_period_ends=[]),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False

    def test_multiple_items_resolves_deterministically_without_crashing(self, client, db_conn, test_user):
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_multi", "subscription": "sub_multi"},
            live_sub=_stripe_sub(id="sub_multi", status="active", item_period_ends=[100, 999, 500]),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, test_user["id"]) is True


class TestReconciliationScriptProductionShapes:
    """scripts/reconcile_stripe_subscribers.py against real StripeObject
    fixtures, covering the same scenarios as the webhook tests above but
    through the reconciliation script's own retrieval path
    (stripe.Subscription.list -> auto_paging_iter)."""

    class _FakeSubList:
        def __init__(self, subs):
            self._subs = subs

        def auto_paging_iter(self):
            return iter(self._subs)

    def _run(self, app, customer_subs, apply=False):
        from scripts.reconcile_stripe_subscribers import main

        def _list_side_effect(customer, status="all", limit=10):
            return self._FakeSubList(customer_subs.get(customer, []))

        argv = ["reconcile_stripe_subscribers.py"] + (["--apply"] if apply else [])
        with patch.object(sys, "argv", argv), \
             patch("scripts.reconcile_stripe_subscribers.stripe.Subscription.list", side_effect=_list_side_effect):
            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code

    def test_basil_shaped_single_item_reconciles_successfully(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_basil", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_basil", status="active", item_period_ends=[2_100_000_000])
        code = self._run(app, {"cus_recon_basil": [sub]}, apply=True)
        assert code == 0
        row = db_conn.execute("SELECT stripe_current_period_end FROM users WHERE id = ?", (test_user["id"],)).fetchone()
        assert row["stripe_current_period_end"] is not None

    def test_legacy_shaped_reconciles_successfully(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_legacy", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_legacy", status="active", current_period_end=2_100_000_000)
        code = self._run(app, {"cus_recon_legacy": [sub]}, apply=True)
        assert code == 0

    def test_zero_items_active_status_counts_as_failure_and_exits_nonzero(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_zero", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_zero", status="active", item_period_ends=[])
        code = self._run(app, {"cus_recon_zero": [sub]}, apply=True)
        assert code == 1
        # Must not have written anything for this unresolved user.
        row = db_conn.execute("SELECT stripe_current_period_end FROM users WHERE id = ?", (test_user["id"],)).fetchone()
        assert row["stripe_current_period_end"] is None

    def test_terminal_canceled_zero_items_does_not_count_as_failure(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_term", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_term", status="canceled", item_period_ends=[])
        code = self._run(app, {"cus_recon_term": [sub]}, apply=True)
        assert code == 0

    def test_multiple_items_reconciles_using_the_maximum(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_multi", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_multi", status="active", item_period_ends=[100, 999, 500])
        code = self._run(app, {"cus_recon_multi": [sub]}, apply=True)
        assert code == 0

    def test_dry_run_does_not_write_even_for_resolvable_subscription(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", ("cus_recon_dry", test_user["id"]))
        sub = _stripe_sub(id="sub_recon_dry", status="active", item_period_ends=[2_100_000_000])
        code = self._run(app, {"cus_recon_dry": [sub]}, apply=False)
        assert code == 0
        row = db_conn.execute("SELECT stripe_current_period_end FROM users WHERE id = ?", (test_user["id"],)).fetchone()
        assert row["stripe_current_period_end"] is None  # dry run - nothing written
