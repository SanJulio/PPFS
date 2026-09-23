"""
Tests for /welcome (the pre-signup onboarding carousel) - specifically
that its registration-step copy accurately reflects the 30-day trial
model rather than the old permanent-Free-tier "forever" claim.
Regression test for a real stale-copy bug found in a post-Activation
audit (September 2026) - see CLAUDE.md's "Pricing/entitlement model"
section.
"""


class TestWelcomeRegistrationCopy:
    def test_no_longer_claims_free_forever(self, client):
        body = client.get("/welcome").get_data(as_text=True)
        assert "Free forever" not in body

    def test_mentions_the_real_30_day_trial(self, client):
        body = client.get("/welcome").get_data(as_text=True)
        assert "30-day free trial" in body
