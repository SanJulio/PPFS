"""
Tests for the Stripe webhook handler (app.py: stripe_webhook()).

Stripe's signature verification (stripe.Webhook.construct_event) is mocked so these
tests can post arbitrary event payloads without a real signing secret. Each test
supplies a python dict shaped like the real event Stripe would send.
"""
from unittest.mock import patch

import pytest


def _post_event(client, event_type, data_object):
    fake_event = {"type": event_type, "data": {"object": data_object}}
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
            {"metadata": {"user_id": str(test_user["id"])}, "customer": "cus_new456"},
        )
        assert resp.status_code == 200
        row = db_conn.execute(
            "SELECT is_pro, stripe_customer_id FROM users WHERE id = ?", (test_user["id"],)
        ).fetchone()
        assert bool(row["is_pro"]) is True
        assert row["stripe_customer_id"] == "cus_new456"


class TestSubscriptionDeleted:
    def test_deactivates_pro(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client,
            "customer.subscription.deleted",
            {"customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False


class TestSubscriptionUpdated:
    @pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled"])
    def test_lapsed_status_revokes_pro(self, client, db_conn, pro_user_with_customer, status):
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"customer": pro_user_with_customer["stripe_customer_id"], "status": status},
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
            {"customer": pro_user_with_customer["stripe_customer_id"], "status": status},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_unrecognised_status_does_not_change_is_pro(self, client, db_conn, pro_user_with_customer):
        """e.g. 'incomplete_expired' or any future Stripe status we don't map — leave alone."""
        resp = _post_event(
            client,
            "customer.subscription.updated",
            {"customer": pro_user_with_customer["stripe_customer_id"], "status": "incomplete_expired"},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True

    def test_does_not_conflict_with_subscription_deleted(self, client, db_conn, pro_user_with_customer):
        """A full cancellation typically fires both events - they should agree, not race."""
        r1 = _post_event(
            client,
            "customer.subscription.updated",
            {"customer": pro_user_with_customer["stripe_customer_id"], "status": "canceled"},
        )
        r2 = _post_event(
            client,
            "customer.subscription.deleted",
            {"customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is False


class TestInvoicePaymentFailed:
    def test_does_not_revoke_pro_access(self, client, db_conn, pro_user_with_customer):
        """Stripe retries failed payments - a single failure shouldn't cut off access."""
        resp = _post_event(
            client,
            "invoice.payment_failed",
            {"customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True


class TestUnhandledEventTypes:
    def test_unknown_event_type_returns_ok_and_changes_nothing(self, client, db_conn, pro_user_with_customer):
        resp = _post_event(
            client,
            "customer.updated",
            {"customer": pro_user_with_customer["stripe_customer_id"]},
        )
        assert resp.status_code == 200
        assert _get_is_pro(db_conn, pro_user_with_customer["id"]) is True
