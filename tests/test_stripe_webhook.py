"""
Tests for the Stripe webhook handler (app.py: stripe_webhook()).

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
from unittest.mock import patch

import pytest
import stripe


def _build_event(event_type, data_object):
    """Build a real stripe.Event (nested StripeObject tree), matching the shape
    Stripe's SDK actually hands the webhook handler - not a plain dict."""
    payload = {
        "id": "evt_test",
        "object": "event",
        "type": event_type,
        "data": {"object": data_object},
    }
    return stripe.Event.construct_from(payload, None)


def _post_event(client, event_type, data_object):
    fake_event = _build_event(event_type, data_object)
    with patch("app.stripe.Webhook.construct_event", return_value=fake_event):
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
        "UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?",
        ("cus_test123", test_user["id"]),
    )
    return {**test_user, "stripe_customer_id": "cus_test123"}


def _get_is_pro(db_conn, user_id):
    row = db_conn.execute("SELECT is_pro FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row["is_pro"])


class TestCheckoutSessionCompleted:
    def test_activates_pro_and_saves_customer_id(self, client, db_conn, test_user):
        resp = _post_event(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "metadata": {"user_id": str(test_user["id"])},
                "customer": "cus_new456",
            },
        )
        assert resp.status_code == 200
        row = db_conn.execute(
            "SELECT is_pro, stripe_customer_id FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert bool(row["is_pro"]) is True
        assert row["stripe_customer_id"] == "cus_new456"

    def test_missing_metadata_does_not_crash(self, client, db_conn, test_user):
        """A session with no metadata at all should be a no-op, not a 500."""
        resp = _post_event(
            client,
            "checkout.session.completed",
            {"object": "checkout.session", "customer": "cus_new456"},
        )
        assert resp.status_code == 200
        row = db_conn.execute(
            "SELECT is_pro FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert bool(row["is_pro"]) is False


class TestSubscriptionDeleted:
    def test_deactivates_pro(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client,
            "customer.subscription.deleted",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False


class TestSubscriptionUpdated:
    @pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled"])
    def test_lapsed_status_revokes_pro(self, client, db_conn, pro_user_with_customer, status):
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"], "status": status},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False

    @pytest.mark.parametrize("status", ["active", "trialing"])
    def test_recovered_status_restores_pro(self, client, db_conn, pro_user_with_customer, status):
        # Start the user as if they'd already lapsed
        db_conn.execute(
            "UPDATE users SET is_pro = 0 WHERE id = ?", (pro_user_with_customer["id"],)
        )
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"], "status": status},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_unrecognised_status_does_not_change_is_pro(self, client, db_conn, pro_user_with_customer):
        """e.g. 'incomplete_expired' or any future Stripe status we don't map — leave alone."""
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"], "status": "incomplete_expired"},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_does_not_conflict_with_subscription_deleted(self, client, db_conn, pro_user_with_customer):
        """A full cancellation typically fires both events - they should agree, not race."""
        r1 = _post_event(
            client,
            "customer.subscription.updated",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"], "status": "canceled"},
        )
        r2 = _post_event(
            client,
            "customer.subscription.deleted",
            {"object": "subscription", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False


class TestInvoicePaymentFailed:
    def test_does_not_revoke_pro_access(self, client, db_conn, pro_user_with_customer):
        """Stripe retries failed payments - a single failure shouldn't cut off access."""
        resp = _post_event(
            client,
            "invoice.payment_failed",
            {"object": "invoice", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True


class TestUnhandledEventTypes:
    def test_unknown_event_type_returns_ok_and_changes_nothing(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client,
            "customer.updated",
            {"object": "customer", "customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True
