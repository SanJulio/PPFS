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

    def test_trial_callout_makes_the_choice_explicit(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "30 days of Pro free. Then choose Pro for £4.99/month or continue with Basic for £0." in body

    def test_pro_card_has_trial_includes_this_badge(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "Trial includes this" in body

    def test_pro_card_has_a_plan_name_label(self, client):
        body = client.get("/").get_data(as_text=True)
        assert 'pricing-plan-name">Pro<' in body

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
        assert "Your data, always retained" in body

    def test_mechanics_footnote_covers_all_four_required_points(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "full Pro access from day one" in body
        assert "automatically becomes" in body and "Basic" in body
        assert "no card required" in body.lower()
        assert "no surprise charge" in body


def _pro_and_basic_card_html(body):
    """Splits the pricing section into (pro_card_html, basic_card_html) so
    assertions can check each card's OWN feature list, not just whether a
    phrase appears anywhere in the page."""
    section = body.split("<!-- ── ACT 4: PRICING ── -->")[1].split("<!-- ── ACT 5: CLOSE ── -->")[0]
    pro_html, basic_html = section.split('class="pricing-card-new pricing-card-secondary')
    return pro_html, basic_html


class TestLandingPageFeatureSplitMatchesRealGating:
    """Regression coverage for a real accuracy bug: the pricing card
    originally listed several universally-available features (transaction
    tracking & categories, recurring bills & income, the 'Can I Afford
    This?' checker, payday-aligned budget cycle, privacy blur mode) only
    on the Pro card, implying they were Pro-exclusive - confirmed false by
    tracing every actual user_is_pro()/PRO_REQUIRED gate in app.py (only
    3 exist: savings rules, future events, investments). Basic's own list
    was also too sparse, understating what it actually includes."""

    def test_universal_features_appear_on_basic_not_implied_as_pro_only(self, client):
        _, basic_html = _pro_and_basic_card_html(client.get("/").get_data(as_text=True))
        for feature in (
            "Transaction tracking &amp; categories",
            "Recurring bills &amp; income",
            '"Can I Afford This?" checker',
            "Payday-aligned budget cycle",
            "Privacy blur mode",
        ):
            assert feature in basic_html, f"{feature!r} should be listed on the Basic card - it's not Pro-gated"

    def test_genuinely_pro_exclusive_features_are_on_the_pro_card(self, client):
        pro_html, basic_html = _pro_and_basic_card_html(client.get("/").get_data(as_text=True))
        for feature in ("Savings rules &amp; automation", "Future events &amp; one-off planning", "Investment tracking"):
            assert feature in pro_html
            assert feature not in basic_html

    def test_manual_tracking_framing_removed_from_basic(self, client):
        """'Manual transaction tracking' wrongly implied Pro gets
        automated/bank-synced tracking - confirmed no tier has that today
        (TRUELAYER_ENV defaults to sandbox, and it's listed as 'Coming
        soon' on this same page's hero section, for every tier)."""
        body = client.get("/").get_data(as_text=True)
        assert "Manual transaction tracking" not in body

    def test_quantity_scaled_features_differ_correctly_between_cards(self, client):
        pro_html, basic_html = _pro_and_basic_card_html(client.get("/").get_data(as_text=True))
        assert "Unlimited accounts" in pro_html and "Unlimited accounts" not in basic_html
        assert "Full 90-day forecast" in pro_html and "Full 90-day forecast" not in basic_html
        assert "1 account" in basic_html and "30-day forecast" in basic_html


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
