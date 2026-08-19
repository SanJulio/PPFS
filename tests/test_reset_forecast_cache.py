"""
All five Settings "reset" routes (reset-balances, reset-transactions,
reset-bills, reset-income, reset-all) genuinely wipe the database, but
until this fix none of them called bust_forecast_cache(current_user.id) -
unlike every other mutating route in app.py. That meant a /forecast page
visited within the 5-minute cache TTL right after a reset kept rendering
the pre-reset snapshot (old account balances) even though the database
was already correct underneath.

These tests populate forecast_cache the same way a real visit to /forecast
does, trigger each reset route, and confirm the cache entry is gone
immediately (not after waiting out the TTL) and that /forecast reflects
the reset state on the very next load.
"""
import datetime

import pytest

from tests.conftest import csrf


def _forecast_cache_key(user_id):
    today = datetime.date.today().isoformat()
    return f"forecast_{user_id}_{today}_90"


@pytest.mark.parametrize("route", [
    "/settings/reset-balances",
    "/settings/reset-transactions",
    "/settings/reset-bills",
    "/settings/reset-income",
    "/settings/reset-all",
])
def test_reset_route_busts_forecast_cache(auth_client, test_user, test_account, route):
    import app as app_module

    cache_key = _forecast_cache_key(test_user["id"])
    app_module.forecast_cache.pop(cache_key, None)

    # Populate the cache exactly as a real visit to Forecast would.
    resp = auth_client.get("/forecast")
    assert resp.status_code == 200
    assert cache_key in app_module.forecast_cache, (
        "test premise failed: /forecast should populate forecast_cache"
    )

    resp = auth_client.post(route, data=csrf(), follow_redirects=False)
    assert resp.status_code == 302

    # The stale entry must be gone immediately, not after the 5-minute TTL expires.
    assert cache_key not in app_module.forecast_cache, (
        f"{route} did not bust forecast_cache - Forecast would keep showing "
        "pre-reset data for up to 5 minutes"
    )

    # Revisiting Forecast right away must recompute cleanly and re-cache fresh data.
    resp = auth_client.get("/forecast")
    assert resp.status_code == 200
    assert cache_key in app_module.forecast_cache


def test_reset_balances_forecast_reflects_zero_immediately(auth_client, test_user, test_account):
    """End-to-end: after Reset Account Balances, the very next /forecast load
    must show £0 for the account, not the stale pre-reset balance."""
    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-initial='{&#34;Current&#34;: 1000.0}'" in body

    resp = auth_client.post("/settings/reset-balances", data=csrf(), follow_redirects=False)
    assert resp.status_code == 302

    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-initial='{&#34;Current&#34;: 1000.0}'" not in body
    assert "data-initial='{&#34;Current&#34;: 0.0}'" in body


def test_reset_all_forecast_reflects_zero_immediately(auth_client, test_user, test_account):
    """Same guarantee for Full Reset, which also zeroes balances."""
    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-initial='{&#34;Current&#34;: 1000.0}'" in body

    resp = auth_client.post("/settings/reset-all", data=csrf(), follow_redirects=False)
    assert resp.status_code == 302

    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-initial='{&#34;Current&#34;: 1000.0}'" not in body
    assert "data-initial='{&#34;Current&#34;: 0.0}'" in body


def test_reset_routes_do_not_touch_other_users_cache(auth_client, test_user, test_account):
    """Narrow-fix guard: busting the acting user's cache must not disturb a
    different user's cached forecast entry."""
    import app as app_module

    other_key = _forecast_cache_key(test_user["id"] + 999999)
    app_module.forecast_cache[other_key] = (0, {"snapshots": "[]", "account_names": "[]"})

    resp = auth_client.post("/settings/reset-balances", data=csrf(), follow_redirects=False)
    assert resp.status_code == 302

    assert other_key in app_module.forecast_cache
    app_module.forecast_cache.pop(other_key, None)
