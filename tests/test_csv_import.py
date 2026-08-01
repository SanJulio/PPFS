"""
Tests for CSV import session-data handling (app.py: import_csv(), import_confirm()).

Privacy-motivated change under test: parsed bank transaction rows used to be
stored directly in session['import_rows'], which persists indefinitely in the
Postgres-backed flask_sessions table if a user abandons the import mid-flow.
Now the session only holds an opaque token; the actual rows live in an
in-memory cache (_pending_imports) with a TTL, so raw bank data never sits in
the database at all.
"""
import time
from datetime import date, timedelta

import pytest

from tests.conftest import csrf


SAMPLE_CSV = "Date,Description,Amount\n" + (date.today() - timedelta(days=1)).isoformat() + ",Coffee Shop,-3.50\n"


@pytest.fixture
def app_module(app):
    """The app.py module, imported only after the `app` fixture has already
    configured database.DB_PATH/USE_POSTGRES for SQLite - importing app.py at
    test-collection time (before that setup runs) would bind app.py's own
    USE_POSTGRES to whatever database.py saw at that earlier, unconfigured
    moment (real DATABASE_URL from .env, since database.py's load_dotenv()
    would populate it), producing Postgres-style SQL against the SQLite test
    DB. Depending on the `app` fixture guarantees import happens after setup."""
    import app as _app_module
    return _app_module


def _upload(auth_client, account_name):
    from io import BytesIO
    data = {
        "csrf_token": csrf()["csrf_token"],
        "account": account_name,
        "csv_file": (BytesIO(SAMPLE_CSV.encode("utf-8")), "statement.csv"),
    }
    return auth_client.post("/import", data=data, content_type="multipart/form-data")


class TestNormalImportFlow:
    def test_upload_then_confirm_adds_transaction(self, auth_client, test_user, test_account, db_conn):
        upload_resp = _upload(auth_client, test_account["name"])
        assert upload_resp.status_code == 200
        assert b"Coffee Shop" in upload_resp.data

        with auth_client.session_transaction() as sess:
            assert "import_token" in sess
            assert "import_rows" not in sess  # raw rows must never sit in the session/DB

        confirm_resp = auth_client.post(
            "/import/confirm",
            data={"csrf_token": csrf()["csrf_token"], "include_0": "1", "category_0": "Food"},
        )
        assert confirm_resp.status_code in (302, 200)

        row = db_conn.execute(
            "SELECT description, amount FROM transactions WHERE user_id = ? AND description = ?",
            (test_user["id"], "Coffee Shop"),
        ).fetchone()
        assert row is not None
        assert row["amount"] == -3.50

    def test_confirm_clears_token_so_it_cannot_be_reused(self, auth_client, test_account, db_conn, app_module):
        _upload(auth_client, test_account["name"])
        with auth_client.session_transaction() as sess:
            token = sess["import_token"]
        assert token in app_module._pending_imports

        auth_client.post(
            "/import/confirm",
            data={"csrf_token": csrf()["csrf_token"], "include_0": "1", "category_0": "Food"},
        )
        assert token not in app_module._pending_imports


class TestAbandonedImport:
    def test_expired_token_does_not_import_anything(self, auth_client, test_account, test_user, db_conn, app_module):
        """Simulates a user re-visiting /import/confirm long after abandoning the
        upload - the cache entry should have expired, so nothing is imported and
        no stale data is used."""
        _upload(auth_client, test_account["name"])
        with auth_client.session_transaction() as sess:
            token = sess["import_token"]

        # Fast-forward the stored timestamp past the TTL instead of sleeping
        stored_at, rows, account = app_module._pending_imports[token]
        app_module._pending_imports[token] = (stored_at - app_module.IMPORT_CACHE_TTL - 1, rows, account)

        confirm_resp = auth_client.post(
            "/import/confirm",
            data={"csrf_token": csrf()["csrf_token"], "include_0": "1", "category_0": "Food"},
        )
        assert confirm_resp.status_code == 302

        row = db_conn.execute(
            "SELECT id FROM transactions WHERE user_id = ? AND description = ?",
            (test_user["id"], "Coffee Shop"),
        ).fetchone()
        assert row is None
        # Expired entry should have been purged from the in-memory cache too
        assert token not in app_module._pending_imports

    def test_purge_expired_imports_removes_stale_entries_only(self, app_module):
        now = time.time()
        app_module._pending_imports.clear()
        app_module._pending_imports["fresh"] = (now, [{"x": 1}], "Current")
        app_module._pending_imports["stale"] = (now - app_module.IMPORT_CACHE_TTL - 100, [{"x": 1}], "Current")

        app_module._purge_expired_imports()

        assert "fresh" in app_module._pending_imports
        assert "stale" not in app_module._pending_imports
        app_module._pending_imports.clear()
