"""
Audit of every route that mutates accounts/transactions/bills in a way that
should be visible on the Forecast page. Prompted by a report that removing an
account in My Money left it still showing on Forecast — caused by
settings_deactivate_account never calling bust_forecast_cache(), the same
class of bug fixed for the five Settings reset routes previously.

An audit of every bust_forecast_cache() call site against every route that
writes to a forecast-relevant table (accounts, transactions, scheduled_expenses)
turned up seven more gaps with the identical symptom - the database write is
correct, but a forecast_cache entry populated within the previous 5 minutes
keeps rendering pre-change data:

  - settings_deactivate_account  - account removed from My Money, stayed on Forecast
  - settings_edit_account        - only busted the cache on a balance change; a
                                    rename or type change alone left the cache stale
  - mark_bill_paid                - changes accounts.balance directly
  - transaction_undo              - reverses accounts.balance
  - transaction_edit              - adjusts accounts.balance by the amount delta
  - transaction_delete            - removes a row Forecast's historical chart reads
  - transactions/bulk-delete       - same, for multiple rows at once
  - apply_auto_items (used by both the silent Home auto-apply and the
    /auto-apply confirm-modal route) - changes accounts.balance for every
    applied item

Routes that mutate data NOT read anywhere in forecast()/api_snapshot() -
is_primary, cycle_overrides, include_in_overview, alert_threshold,
investments, last_applied on its own - were checked and confirmed to have no
effect on what Forecast renders, so they're intentionally left alone.
"""
import datetime

import pytest

from tests.conftest import csrf


def _forecast_cache_key(user_id):
    today = datetime.date.today().isoformat()
    return f"forecast_{user_id}_{today}_90"


def _insert_tx(db_conn, user_id, account_name, amount):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO transactions (date, description, amount, account, user_id, type, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.date.today().isoformat(), "Test tx", amount, account_name, user_id, "manual", "Other"),
    )
    db_conn.commit()
    tx_id = cur.lastrowid
    cur.close()
    return tx_id


def _insert_bill(db_conn, user_id, account_name, amount=50.0, day=1):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_expenses (name, amount, day, account, frequency, user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Test bill", amount, day, account_name, "monthly", user_id),
    )
    db_conn.commit()
    bill_id = cur.lastrowid
    cur.close()
    return bill_id


def _prime_cache(auth_client, user_id):
    """Visit /forecast once so forecast_cache is populated, same as a real user would."""
    import app as app_module

    key = _forecast_cache_key(user_id)
    app_module.forecast_cache.pop(key, None)
    resp = auth_client.get("/forecast")
    assert resp.status_code == 200
    assert key in app_module.forecast_cache, "test premise failed: /forecast should populate forecast_cache"
    return key


def test_deactivate_account_busts_forecast_cache(auth_client, test_user, test_account):
    import app as app_module

    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post(
        "/settings/deactivate-account",
        data={**csrf(), "name": test_account["name"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache


def test_deactivate_account_forecast_shows_no_stale_account(auth_client, test_user, test_account):
    """End-to-end reproduction of the reported bug: revisiting Forecast right
    after removing an account must not still show it."""
    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-accounts='[&#34;Current&#34;]'" in body

    resp = auth_client.post(
        "/settings/deactivate-account",
        data={**csrf(), "name": test_account["name"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    resp = auth_client.get("/forecast")
    body = resp.get_data(as_text=True)
    assert "data-accounts='[]'" in body
    assert "Current" not in body.split("data-accounts")[1][:50]


def test_edit_account_rename_only_busts_forecast_cache(auth_client, test_user, test_account):
    """The gap this fix also covers: renaming an account (no balance change)
    must still invalidate the cache, not just balance-changing edits."""
    import app as app_module

    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post(
        "/settings/edit-account",
        data={
            **csrf(),
            "id": str(test_account["id"]),
            "name": "Renamed Account",
            "type": "current",
            "balance": str(test_account["balance"]),  # unchanged
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache


def test_mark_bill_paid_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    bill_id = _insert_bill(db_conn, test_user["id"], test_account["name"])
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post(
        "/mark-bill-paid",
        json={
            **csrf(),
            "bill_id": bill_id,
            "name": "Test bill",
            "amount": 50.0,
            "account": test_account["name"],
            "day": 1,
        },
    )
    assert resp.status_code == 200
    assert key not in app_module.forecast_cache


def test_auto_apply_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    bill_id = _insert_bill(db_conn, test_user["id"], test_account["name"])
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post(
        "/auto-apply",
        json={
            **csrf(),
            "items": [{
                "type": "bill",
                "item_id": bill_id,
                "name": "Test bill",
                "amount": -50.0,
                "account": test_account["name"],
                "due_date": datetime.date.today().isoformat(),
            }],
        },
    )
    assert resp.status_code == 200
    assert key not in app_module.forecast_cache


def test_transaction_undo_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    tx_id = _insert_tx(db_conn, test_user["id"], test_account["name"], -50.00)
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post("/transactions/undo", data={**csrf(), "tx_id": str(tx_id)}, follow_redirects=False)
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache


def test_transaction_delete_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    tx_id = _insert_tx(db_conn, test_user["id"], test_account["name"], -50.00)
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post("/transactions/delete", data={**csrf(), "tx_id": str(tx_id)}, follow_redirects=False)
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache


def test_transaction_edit_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    tx_id = _insert_tx(db_conn, test_user["id"], test_account["name"], -50.00)
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post(
        "/transactions/edit",
        data={**csrf(), "tx_id": str(tx_id), "description": "Edited", "amount": "-75.00", "account": test_account["name"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache


def test_bulk_delete_busts_forecast_cache(auth_client, db_conn, test_user, test_account):
    import app as app_module

    tx_id = _insert_tx(db_conn, test_user["id"], test_account["name"], -50.00)
    key = _prime_cache(auth_client, test_user["id"])

    resp = auth_client.post("/transactions/bulk-delete", data={**csrf(), "tx_ids": [str(tx_id)]}, follow_redirects=False)
    assert resp.status_code == 302
    assert key not in app_module.forecast_cache
