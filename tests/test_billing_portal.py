"""
Tests for /billing/portal (Stripe Customer Portal) - specifically that
introducing STRIPE_PORTAL_CONFIGURATION_ID (September 2026, entitlement
cutover) cannot break this already-live, already-working feature.

No test existed for this route before the entitlement work touched it.
The specific risk: STRIPE_PORTAL_CONFIGURATION_ID is a new piece of
config with no value in most environments until the one-time Stripe
Portal setup step is done - if the route unconditionally passed it to
Stripe (even as None), or if it were accidentally gated behind
ENTITLEMENT_RESOLVER_ACTIVE, a real user's ability to manage their
existing subscription could break independently of - and before - the
entitlement resolver ever activates. Locking in: unset means the
`configuration` kwarg is omitted entirely (Stripe's own "use the
account's default configuration" behaviour, byte-identical to the
pre-cutover call), and this has nothing to do with the resolver flag.
"""
from unittest.mock import MagicMock, patch

import pytest


class TestBillingPortalConfigurationIdSafety:
    def test_unset_configuration_id_omits_the_kwarg_entirely(self, auth_client, test_user, db_conn):
        """The core regression this guards against: Session.create() must
        never receive configuration=None - the key must be absent."""
        db_conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", ("cus_portal_test", test_user["id"]))
        fake_session = MagicMock(url="https://billing.stripe.com/fake-session")
        with patch("app.STRIPE_PORTAL_CONFIGURATION_ID", None), \
             patch("app.stripe.billing_portal.Session.create", return_value=fake_session) as mock_create:
            resp = auth_client.get("/billing/portal", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert resp.location == "https://billing.stripe.com/fake-session"
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert "configuration" not in call_kwargs
        assert call_kwargs["customer"] == "cus_portal_test"

    def test_set_configuration_id_is_passed_through(self, auth_client, test_user, db_conn):
        db_conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", ("cus_portal_test2", test_user["id"]))
        fake_session = MagicMock(url="https://billing.stripe.com/fake-session-2")
        with patch("app.STRIPE_PORTAL_CONFIGURATION_ID", "bpc_real_config_id"), \
             patch("app.stripe.billing_portal.Session.create", return_value=fake_session) as mock_create:
            resp = auth_client.get("/billing/portal", follow_redirects=False)

        assert resp.status_code in (302, 303)
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["configuration"] == "bpc_real_config_id"

    @pytest.mark.parametrize("resolver_active", [False, True])
    def test_behaviour_is_independent_of_entitlement_resolver_flag(self, auth_client, test_user, db_conn, resolver_active):
        """Billing Portal access must work identically regardless of
        ENTITLEMENT_RESOLVER_ACTIVE - it's an unrelated, already-live
        Stripe integration point, not part of the entitlement cutover."""
        db_conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", ("cus_portal_test3", test_user["id"]))
        fake_session = MagicMock(url="https://billing.stripe.com/fake-session-3")
        with patch("app.ENTITLEMENT_RESOLVER_ACTIVE", resolver_active), \
             patch("app.STRIPE_PORTAL_CONFIGURATION_ID", None), \
             patch("app.stripe.billing_portal.Session.create", return_value=fake_session) as mock_create:
            resp = auth_client.get("/billing/portal", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert resp.location == "https://billing.stripe.com/fake-session-3"
        assert "configuration" not in mock_create.call_args.kwargs

    def test_no_stripe_customer_id_redirects_without_calling_stripe(self, auth_client, test_user):
        with patch("app.stripe.billing_portal.Session.create") as mock_create:
            resp = auth_client.get("/billing/portal", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "No+billing+account+found" in resp.location
        mock_create.assert_not_called()

    def test_stripe_error_does_not_crash_the_request(self, auth_client, test_user, db_conn):
        db_conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", ("cus_portal_test4", test_user["id"]))
        with patch("app.stripe.billing_portal.Session.create", side_effect=Exception("Stripe API down")):
            resp = auth_client.get("/billing/portal", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "Could+not+open+billing+portal" in resp.location
