"""
Tests for the public landing page's (templates/landing.html) pricing
section and CTA copy - rewritten (September 2026) to reflect the real
30-day trial model (Legacy Free / Trialing / Basic / Pro) instead of the
old permanent Free/Pro binary. Regression coverage for a real stale-copy
audit finding - see CLAUDE.md's "Pricing/entitlement model" section.

landing.html is served for any unauthenticated GET / (app.py's
unauthorized_handler) - the `client` fixture (no login) is the correct
way to exercise it.
"""


class TestLandingPageNoStaleFreeProBinary:
    def test_old_permanent_free_pro_heading_is_gone(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Simple pricing." not in body

    def test_old_ctas_are_gone(self, client):
        body = client.get("/").get_data(as_text=True)
        for old in ("Get started free", "Create your free account", "Save my forecast", "Free tier available"):
            assert old not in body


class TestLandingPagePricingSectionContent:
    def test_new_trial_led_heading_present(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Try everything free. Decide after." in body

    def test_trial_callout_present(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "30-day free trial, no card required." in body

    def test_pro_card_has_trial_includes_this_badge(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Trial includes this" in body

    def test_pro_card_keeps_real_price_and_key_features(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "£4.99" in body
        assert "Unlimited accounts" in body
        assert "Full 90-day forecast" in body

    def test_basic_card_relabeled_from_free_to_basic(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "pricing-price-new\">Basic<" in body
        assert "what your trial becomes if you don't subscribe" in body

    def test_basic_card_shows_its_real_limits(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "1 account" in body
        assert "30-day forecast" in body
        assert "Manual transaction tracking" in body
        assert "Your data, always retained" in body

    def test_mechanics_footnote_covers_all_four_required_points(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "full Pro access from day one" in body
        assert "automatically becomes" in body and "Basic" in body
        assert "no card required" in body.lower()
        assert "no surprise charge" in body


class TestLandingPageCTAsUpdated:
    def test_nav_cta_says_start_free_trial(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Start your free trial" in body  # full (desktop) nav CTA
        assert "Start trial" in body  # short (mobile) nav CTA

    def test_mid_page_signup_cta_updated(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Start my free trial" in body

    def test_pricing_card_cta_updated(self, client):
        body = client.get("/").get_data(as_text=True)
        assert 'class="btn-pricing">Start your free trial' in body

    def test_final_cta_updated(self, client):
        body = client.get("/").get_data(as_text=True)
        assert 'class="btn-close reveal reveal-delay-2">Start your free trial' in body

    def test_hero_demo_button_deliberately_left_unchanged(self, client):
        """The hero's own button ('Watch the Demo') scrolls to the video
        section - it never linked to signup, so it's correctly NOT
        renamed to trial language (that would misrepresent what clicking
        it does)."""
        body = client.get("/").get_data(as_text=True)
        assert "Watch the Demo" in body
