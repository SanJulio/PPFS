"""
Forecast chart scrollback (dates before today) used to be reconstructed from
real transaction history by walking balances backward day by day. That made
the chart show what looked like real past account movement — but investigation
showed it could reach back up to 90 days into genuinely real (if stale/
forgotten) transaction rows, which reads as misleading "reconstructed history"
rather than an honest forecast.

Replaced with a flat line at today's actual balance for every date before
today: honestly says "this is your balance as of today," not real past
movement, and looks identical regardless of how much transaction history
exists. These tests cover that replacement.
"""
import datetime

from tests.conftest import csrf


def _get_hist_and_snapshots(auth_client):
    import html
    import json

    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)

    def _extract(attr):
        idx = body.find(attr + "='")
        start = idx + len(attr + "='")
        end = body.find("'", start)
        return json.loads(html.unescape(body[start:end]))

    return _extract("data-history"), _extract("data-snapshots")


def test_flat_line_with_no_transaction_history(auth_client, test_account):
    """A brand-new account with zero transactions must still get a full
    90-day flat scrollback line, not an empty array."""
    hist, snaps = _get_hist_and_snapshots(auth_client)

    assert len(hist) == 90
    today_balance = round(test_account["balance"], 2)
    assert all(h[test_account["name"]] == today_balance for h in hist)


def test_flat_line_identical_regardless_of_transaction_history(auth_client, db_conn, test_user, test_account):
    """Old transactions must no longer influence the scrollback line at all -
    same flat value whether the account has lots of history or none."""
    old_date = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    cur = db_conn.cursor()
    for i in range(5):
        cur.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (old_date, f"Old tx {i}", 250.0 * (1 if i % 2 == 0 else -1), test_account["name"], test_user["id"], "manual", "Other"),
        )
    db_conn.commit()

    hist, snaps = _get_hist_and_snapshots(auth_client)

    assert len(hist) == 90
    today_balance = round(test_account["balance"], 2)
    assert all(h[test_account["name"]] == today_balance for h in hist), (
        "old transactions must not cause the scrollback line to vary - it should "
        "be a flat line at today's balance regardless of transaction history"
    )
    # Not literally reconstructing - the old transaction date shouldn't produce
    # any special dip/rise around it.
    values = {h[test_account["name"]] for h in hist}
    assert values == {today_balance}


def test_scrollback_always_spans_exactly_90_days(auth_client, test_account):
    """Requirement: consistent scroll/zoom interaction regardless of history -
    always the full 90-day window, never trimmed to however far back real
    transactions happened to exist."""
    hist, _ = _get_hist_and_snapshots(auth_client)
    today = datetime.date.today()
    expected_dates = [(today - datetime.timedelta(days=90 - i)).isoformat() for i in range(90)]
    actual_dates = [h["date"] for h in hist]
    assert actual_dates == expected_dates
    assert hist[-1]["date"] == (today - datetime.timedelta(days=1)).isoformat()


def test_forward_projection_unaffected_by_flatline_change(auth_client, test_account):
    """The forward 90-day simulation (today onward) must be completely
    unchanged - still starts at today's actual balance and runs 91 entries
    (today + 90 forward days)."""
    hist, snaps = _get_hist_and_snapshots(auth_client)
    today_str = datetime.date.today().isoformat()

    assert len(snaps) == 91
    assert snaps[0]["date"] == today_str
    assert snaps[0][test_account["name"]] == round(test_account["balance"], 2)
    forecast_end = (datetime.date.today() + datetime.timedelta(days=90)).isoformat()
    assert snaps[-1]["date"] == forecast_end


def test_locked_account_still_excluded_from_flatline_scrollback(auth_client, db_conn, test_user, test_account, second_account):
    """Account-locking exclusion happens before the scrollback block and must
    still work unchanged - a locked account appears in neither the flat
    scrollback line nor the forward projection."""
    db_conn.execute("UPDATE accounts SET is_locked = 1 WHERE id = ?", (second_account["id"],))
    db_conn.commit()

    hist, snaps = _get_hist_and_snapshots(auth_client)

    assert all(second_account["name"] not in h for h in hist)
    assert all(second_account["name"] not in s for s in snaps)
    assert all(test_account["name"] in h for h in hist)
