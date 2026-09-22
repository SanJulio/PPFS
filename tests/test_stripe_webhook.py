"""
Tests for the Stripe webhook handler (app.py: stripe_webhook()).

Full design in CLAUDE.md's "Pricing/entitlement model" section. Key
properties this file proves, not just documents:

  - Every subscription-state event (checkout completion, update, delete)
    retrieves and applies Stripe's LIVE subscription state
    (stripe.Subscription.retrieve), not the event payload's own snapshot
    - so two events in any order, or sharing a timestamp, converge on the
    same result. Tests mock the retrieval separately from the event
    payload specifically to prove this: the event payload can say one
    thing, the "live" mocked state says another, and the live state wins.
  - Dual-write: is_pro (paid-Stripe-only cache) and the new stripe_*
    mirror fields are both written on every event, regardless of
    ENTITLEMENT_RESOLVER_ACTIVE.
  - Dedup: redelivering the exact same event id is a no-op.
  - past_due and paused retain access (a real behaviour change from the
    pre-cutover webhook, which revoked immediately on past_due).
  - 'incomplete' is only ever written on checkout completion, never
    overwrites an existing subscription's stored state on update/delete.
  - A retrieval failure on an already-deleted subscription resolves to
    'canceled' rather than erroring.

Stripe's signature verification (stripe.Webhook.construct_event) is mocked so these
tests can post arbitrary events without a real signing secret - but the *event itself*
is a real stripe.Event/StripeObject tree (built via stripe.Event.construct_from()),
not a plain dict. This matters: StripeObject does not implement .get() (only
__getitem__ and attribute access), so a handler that does
`event["data"]["object"].get("customer")` works fine against a plain-dict mock but
raises AttributeError against a real Stripe object - which is exactly the bug that
crashed checkout.session.completed handling in live mode. Using real StripeObject
fixtures here means this class of bug gets caught by the suite going forward.
"""
import contextlib
import itertools
from unittest.mock import patch

import pytest
import stripe


_event_counter = itertools.count(1)


class _FakeSub:
    """Minimal stand-in for what stripe.Subscription.retrieve() returns -
    only the attributes _apply_subscription_state() actually reads. This
    is a plain object with a real (legacy-shaped) current_period_end
    attribute, NOT a stand-in for Stripe's actual post-basil object shape
    - see tests/test_stripe_period_end_extraction.py for tests against a
    real, production-shaped stripe.StripeObject. Defaults to a non-None
    placeholder (rather than None) so tests that don't care about the
    period-end value specifically don't trip the "no silent None for an
    access-granting status" guard added for the basil incident."""
    def __init__(self, status, cancel_at_period_end=False, current_period_end=2_000_000_000):
        self.status = status
        self.cancel_at_period_end = cancel_at_period_end
        self.current_period_end = current_period_end


def _build_event(event_type, data_object, event_id=None, created=1_700_000_000):
    """Build a real stripe.Event (nested StripeObject tree), matching the shape
    Stripe's SDK actually hands the webhook handler - not a plain dict."""
    payload = {
        "id": event_id or f"evt_test_{next(_event_counter)}",
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {"object": data_object},
    }
    return stripe.Event.construct_from(payload, None)


def _post_event(client, event_type, data_object, live_sub=None, event_id=None, created=1_700_000_000, retrieve_error=None):
    fake_event = _build_event(event_type, data_object, event_id=event_id, created=created)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.stripe.Webhook.construct_event", return_value=fake_event))
        if retrieve_error is not None:
            stack.enter_context(patch("app.stripe.Subscription.retrieve", side_effect=retrieve_error))
        elif live_sub is not None:
            stack.enter_context(patch("app.stripe.Subscription.retrieve", return_value=live_sub))
        return client.post(
            "/stripe/webhook",
            data=b"{}",
            headers={"Stripe-Signature": "test-sig"},
            content_type="application/json",
        )


@pytest.fixture
def pro_user_with_customer(db_conn, test_user):
    """Give the test user a stripe_customer_id and mark them Pro, as if checkout already completed."""
    db_conn.execute(
        "UPDATE users SET is_pro = 1, stripe_customer_id = ?, stripe_subscription_status = 'active' WHERE id = ?",
        ("cus_test123", test_user["id"]),
    )
    return {**test_user, "stripe_customer_id": "cus_test123"}


def _get_user_row(db_conn, user_id):
    return db_conn.execute(
        "SELECT is_pro, stripe_customer_id, stripe_subscription_status, stripe_cancel_at_period_end, "
        "stripe_current_period_end FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def _get_is_pro(db_conn, user_id):
    return bool(_get_user_row(db_conn, user_id)["is_pro"])


class TestCheckoutSessionCompleted:
    def test_activates_pro_and_saves_customer_id(self, client, db_conn, test_user):
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_new456", "subscription": "sub_new456"},
            live_sub=_FakeSub("active"),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, test_user["id"])
        assert bool(row["is_pro"]) is True
        assert row["stripe_customer_id"] == "cus_new456"
        assert row["stripe_subscription_status"] == "active"

    def test_missing_metadata_does_not_crash(self, client, db_conn, test_user):
        """A session with no metadata at all should be a no-op, not a 500."""
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "customer": "cus_new456", "subscription": "sub_new456"},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, test_user["id"]) is False

    def test_uses_live_retrieved_state_not_the_event_payload(self, client, db_conn, test_user):
        """checkout.session.completed carries no subscription status of its
        own in this app's handling - the live retrieval is authoritative.
        Simulates the out-of-order case: by the time this checkout event is
        actually processed, the subscription has already been cancelled."""
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_new456", "subscription": "sub_new456"},
            live_sub=_FakeSub("canceled"),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, test_user["id"])
        assert bool(row["is_pro"]) is False
        assert row["stripe_subscription_status"] == "canceled"

    def test_incomplete_is_written_on_brand_new_checkout(self, client, db_conn, test_user):
        """Nothing pre-existing to protect for a brand-new subscription -
        pending 3DS/SCA is a legitimate 'incomplete' at this exact moment."""
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_new456", "subscription": "sub_new456"},
            live_sub=_FakeSub("incomplete"),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, test_user["id"])
        assert row["stripe_subscription_status"] == "incomplete"
        assert bool(row["is_pro"]) is False


class TestSubscriptionDeleted:
    def test_deactivates_pro(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client, "customer.subscription.deleted",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("canceled"),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False
        assert _get_user_row(db_conn, pro_user_with_customer["id"])["stripe_subscription_status"] == "canceled"

    def test_retrieval_failure_on_already_gone_subscription_resolves_canceled(self, client, db_conn, pro_user_with_customer):
        """A .deleted event with nothing left to retrieve is itself the
        authoritative terminal signal - must not 500 or leave stale state."""
        resp = _post_event(
            client, "customer.subscription.deleted",
            {"object": "subscription", "id": "sub_gone", "customer": pro_user_with_customer["stripe_customer_id"]},
            retrieve_error=stripe.error.InvalidRequestError("No such subscription", "id"),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False
        assert _get_user_row(db_conn, pro_user_with_customer["id"])["stripe_subscription_status"] == "canceled"


class TestSubscriptionUpdated:
    @pytest.mark.parametrize("status", ["unpaid", "canceled", "incomplete_expired"])
    def test_terminal_status_revokes_pro(self, client, db_conn, pro_user_with_customer, status):
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub(status),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due", "paused"])
    def test_access_granting_status_retains_or_restores_pro(self, client, db_conn, pro_user_with_customer, status):
        """past_due and paused are the real behaviour change from the
        pre-cutover webhook (which revoked immediately on past_due) -
        Stripe's own dunning retries get a grace period, and paused is
        treated as a deliberate, non-terminal state (see CLAUDE.md)."""
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub(status),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_recovered_status_restores_pro_from_lapsed(self, client, db_conn, pro_user_with_customer):
        db_conn.execute("UPDATE users SET is_pro = 0, stripe_subscription_status = 'canceled' WHERE id = ?", (pro_user_with_customer["id"],))
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("active"),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_anomalous_incomplete_on_existing_subscription_is_ignored(self, client, db_conn, pro_user_with_customer):
        """Stripe's state machine doesn't really transition an established
        subscription back to 'incomplete' - if it somehow reports this,
        existing stored state must not be overwritten."""
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("incomplete"),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, pro_user_with_customer["id"])
        assert row["stripe_subscription_status"] == "active"  # unchanged from the fixture
        assert bool(row["is_pro"]) is True

    def test_does_not_conflict_with_subscription_deleted(self, client, db_conn, pro_user_with_customer):
        """A full cancellation typically fires both events - they should agree, not race."""
        r1 = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("canceled"),
        )
        r2 = _post_event(
            client, "customer.subscription.deleted",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("canceled"),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False

    def test_ordinary_renewal_never_downgrades(self, client, db_conn, pro_user_with_customer):
        """cancel_at_period_end=False, regardless of how far in the past
        current_period_end is - an ordinary renewing subscriber must never
        be downgraded by this event."""
        resp = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("active", cancel_at_period_end=False, current_period_end=1_000_000_000),  # long past
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True


class TestEqualTimestampAndOutOfOrderEvents:
    """The webhook doesn't rely on event ordering/timestamps for
    correctness - every event retrieves Stripe's live state and applies
    that, so two events (any order, same or different timestamps)
    converge on the same result."""

    def test_two_events_sharing_a_timestamp_converge_on_live_state(self, client, db_conn, pro_user_with_customer):
        r1 = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("past_due"), event_id="evt_a", created=1_700_000_500,
        )
        r2 = _post_event(
            client, "customer.subscription.updated",
            {"object": "subscription", "id": "sub_test123", "customer": pro_user_with_customer["stripe_customer_id"]},
            live_sub=_FakeSub("canceled"), event_id="evt_b", created=1_700_000_500,
        )
        assert r1.status_code == 200 and r2.status_code == 200
        # Both retrieved live state at the moment they ran - final DB state
        # reflects whichever was actually processed last, not an ordering
        # assumption based on the (identical) timestamps.
        assert _get_user_row(db_conn, pro_user_with_customer["id"])["stripe_subscription_status"] == "canceled"

    def test_checkout_delivered_after_a_later_cancellation_does_not_resurrect_pro(self, client, db_conn, test_user):
        """checkout.session.completed processed 'late' (after the
        subscription was already cancelled) must not blindly assert
        active - its live retrieval reflects the real, already-cancelled
        state."""
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_late", "subscription": "sub_late"},
            live_sub=_FakeSub("canceled"),
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, test_user["id"]) is False


class TestWebhookDedup:
    def test_duplicate_event_id_is_a_no_op(self, client, db_conn, test_user):
        event_id = "evt_dup_test"
        r1 = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_dup", "subscription": "sub_dup"},
            live_sub=_FakeSub("active"), event_id=event_id,
        )
        assert r1.status_code == 200
        assert _get_is_pro(db_conn, test_user["id"]) is True

        # Redeliver the exact same event id, with a DIFFERENT live state -
        # if dedup works, this must NOT be applied.
        r2 = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_dup", "subscription": "sub_dup"},
            live_sub=_FakeSub("canceled"), event_id=event_id,
        )
        assert r2.status_code == 200
        assert _get_is_pro(db_conn, test_user["id"]) is True  # unchanged - the second delivery was a no-op

    def test_processed_event_is_recorded(self, client, db_conn, test_user):
        _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_rec", "subscription": "sub_rec"},
            live_sub=_FakeSub("active"), event_id="evt_record_test",
        )
        row = db_conn.execute("SELECT event_type FROM processed_stripe_events WHERE event_id = ?", ("evt_record_test",)).fetchone()
        assert row is not None
        assert row["event_type"] == "checkout.session.completed"


class TestInvoicePaymentFailed:
    def test_does_not_revoke_pro_access(self, client, db_conn, pro_user_with_customer):
        """Stripe retries failed payments - a single failure shouldn't cut off access."""
        resp = _post_event(
            client, "invoice.payment_failed",
            {"object": "invoice", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True


class TestUnhandledEventTypes:
    def test_unknown_event_type_returns_ok_and_changes_nothing(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client, "customer.updated",
            {"object": "customer", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True


class TestDualWriteWhileFlagOff:
    """ENTITLEMENT_RESOLVER_ACTIVE is false in the test environment by
    default (see conftest.py) - these confirm the webhook keeps the new
    stripe_* fields current in real time even while old is_pro-based
    authorization is what's actually live, so they're never stale by the
    time a later Activation flips the flag."""

    def test_new_stripe_fields_populated_while_flag_is_off(self, client, db_conn, test_user):
        resp = _post_event(
            client, "checkout.session.completed",
            {"object": "checkout.session", "metadata": {"user_id": str(test_user["id"])},
             "customer": "cus_dualwrite", "subscription": "sub_dualwrite"},
            live_sub=_FakeSub("active", cancel_at_period_end=False, current_period_end=1_800_000_000),
        )
        assert resp.status_code == 200
        row = _get_user_row(db_conn, test_user["id"])
        assert row["stripe_subscription_status"] == "active"
        assert row["stripe_current_period_end"] is not None
        # And old-model authorization (is_pro) is also correctly updated -
        # both happen together, unconditionally.
        assert bool(row["is_pro"]) is True
