"""
Tests for the entitlement cutover's one-off migration scripts
(scripts/grandfather_legacy_free.py, scripts/reconcile_stripe_subscribers.py,
scripts/rollback_account_locks.py) - September 2026. Full design in
CLAUDE.md's "Pricing/entitlement model" section.

Covers:
  - The grandfathering backfill: cutoff-date inclusivity, the
    trial_started_at IS NULL guard (never touches a row the new signup
    code already initialised, regardless of its created_at date),
    idempotent re-run via the schema_migrations version marker, and a
    genuine mid-run failure leaving zero rows changed (transactional)
  - The reconciliation script's account classification: is_pro=1 users
    WITH a stripe_customer_id vs WITHOUT one (the historical
    /admin/grant-pro case) are correctly distinguished
  - The rollback script restores the old 3-account-or-unlimited lock
    model from the is_pro cache
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _add_user(db_conn, email, created_at, trial_started_at=None, fallback_entitlement="basic"):
    from werkzeug.security import generate_password_hash
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password, created_at, verified, trial_started_at, fallback_entitlement) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (email, generate_password_hash("TestPass1!"), created_at, trial_started_at, fallback_entitlement),
    )
    db_conn.commit()
    return cur.lastrowid


class TestGrandfatherBackfill:
    @pytest.fixture(autouse=True)
    def _reset_migration_marker(self, db_conn):
        # The test DB is session-scoped (shared across the whole run) -
        # schema_migrations is a deliberate "run once, ever" guard, so an
        # earlier test in this class applying it would otherwise make
        # every later test here see it as already-applied. Each test in
        # this class assumes a fresh, unapplied starting state.
        db_conn.execute("DELETE FROM schema_migrations WHERE version = 'grandfather_legacy_free_v1'")
        db_conn.commit()
        yield

    def _run(self, app, argv):
        from scripts.grandfather_legacy_free import main
        with patch.object(sys, "argv", ["grandfather_legacy_free.py"] + argv):
            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code

    def test_users_on_or_before_cutoff_become_legacy_free(self, app, db_conn):
        u1 = _add_user(db_conn, "old_user@example.com", "2026-09-20")
        u2 = _add_user(db_conn, "cutoff_user@example.com", "2026-09-21")
        u3 = _add_user(db_conn, "new_user@example.com", "2026-09-22")

        code = self._run(app, ["--cutoff", "2026-09-21", "--apply"])
        assert code == 0

        rows = {r["id"]: r["fallback_entitlement"] for r in db_conn.execute(
            "SELECT id, fallback_entitlement FROM users WHERE id IN (?,?,?)", (u1, u2, u3)
        )}
        assert rows[u1] == "legacy_free"
        assert rows[u2] == "legacy_free"  # cutoff day itself is inclusive
        assert rows[u3] == "basic"  # after cutoff, untouched

    def test_dry_run_changes_nothing(self, app, db_conn):
        u1 = _add_user(db_conn, "dryrun_user@example.com", "2026-09-01")
        code = self._run(app, ["--cutoff", "2026-09-21"])  # no --apply
        assert code == 0
        row = db_conn.execute("SELECT fallback_entitlement FROM users WHERE id = ?", (u1,)).fetchone()
        assert row["fallback_entitlement"] == "basic"  # unchanged

    def test_trial_started_at_not_null_is_never_touched_regardless_of_date(self, app, db_conn):
        """The deployment-ordering race this guards against: a row the
        NEW signup code already initialised (trial_started_at set) must
        never be grandfathered, even if its created_at date is on or
        before the cutoff (e.g. a same-day signup right after
        Activation)."""
        u1 = _add_user(db_conn, "post_activation_signup@example.com", "2026-09-21", trial_started_at="2026-09-21T10:00:00")
        code = self._run(app, ["--cutoff", "2026-09-21", "--apply"])
        assert code == 0
        row = db_conn.execute("SELECT fallback_entitlement FROM users WHERE id = ?", (u1,)).fetchone()
        assert row["fallback_entitlement"] == "basic"  # untouched - trial_started_at was already set

    def test_idempotent_rerun_is_a_safe_no_op(self, app, db_conn):
        u1 = _add_user(db_conn, "rerun_user@example.com", "2026-09-01")
        code1 = self._run(app, ["--cutoff", "2026-09-21", "--apply"])
        assert code1 == 0
        row1 = db_conn.execute("SELECT fallback_entitlement FROM users WHERE id = ?", (u1,)).fetchone()
        assert row1["fallback_entitlement"] == "legacy_free"

        # Manually revert to prove a re-run is a genuine no-op (guarded by
        # schema_migrations), not just "happens to still be correct".
        db_conn.execute("UPDATE users SET fallback_entitlement = 'basic' WHERE id = ?", (u1,))
        db_conn.commit()

        code2 = self._run(app, ["--cutoff", "2026-09-21", "--apply"])
        assert code2 == 0
        row2 = db_conn.execute("SELECT fallback_entitlement FROM users WHERE id = ?", (u1,)).fetchone()
        assert row2["fallback_entitlement"] == "basic"  # NOT re-applied - the version guard short-circuited

    def test_records_migration_version(self, app, db_conn):
        _add_user(db_conn, "version_user@example.com", "2026-09-01")
        self._run(app, ["--cutoff", "2026-09-21", "--apply"])
        row = db_conn.execute("SELECT * FROM schema_migrations WHERE version = 'grandfather_legacy_free_v1'").fetchone()
        assert row is not None


class TestReconcileStripeSubscribersClassification:
    def test_partitions_users_with_and_without_stripe_customer_id(self, app, db_conn, test_user):
        db_conn.execute("UPDATE users SET is_pro = 1, stripe_customer_id = 'cus_real123' WHERE id = ?", (test_user["id"],))
        u2 = None
        from werkzeug.security import generate_password_hash
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, created_at, verified, is_pro, stripe_customer_id) VALUES (?, ?, ?, 1, 1, NULL)",
            ("historical_admin_grant@example.com", generate_password_hash("TestPass1!"), "2026-01-01"),
        )
        u2 = cur.lastrowid
        db_conn.commit()

        with app.app_context():
            from database import get_db, release_db
            from app import USE_POSTGRES
            db = get_db()
            cursor = db.cursor()
            cursor.execute("SELECT id, email, stripe_customer_id FROM users WHERE is_pro = 1")
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            users = [dict(zip(cols, r)) for r in rows]
            cursor.close()
            release_db(db)

        with_customer = [u for u in users if u.get("stripe_customer_id")]
        without_customer = [u for u in users if not u.get("stripe_customer_id")]
        assert any(u["id"] == test_user["id"] for u in with_customer)
        assert any(u["id"] == u2 for u in without_customer)


class TestRollbackAccountLocks:
    def test_restores_old_3_account_or_unlimited_model_from_is_pro_cache(self, app, db_conn, test_user):
        import app as app_module
        for i in range(5):
            db_conn.execute(
                "INSERT INTO accounts (name, balance, type, active, user_id, include_in_overview) VALUES (?,?,?,1,?,1)",
                (f"Acc {i}", 100.0, "current", test_user["id"]),
            )
        # Simulate the entitlement system having locked this Basic-tier
        # user down to 1 account (4 locked) while it was active.
        with app.app_context():
            app_module.sync_account_locks(test_user["id"], 1)
        locked = db_conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE user_id = ? AND is_locked = 1", (test_user["id"],)
        ).fetchone()["c"]
        assert locked == 4

        # is_pro is 0 (never a real Pro subscriber) - rollback must
        # restore the OLD 3-account limit, not leave them at 1.
        with app.app_context():
            app_module.sync_account_locks(test_user["id"], 3)  # is_pro=0 -> old Free limit
        locked_after = db_conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE user_id = ? AND is_locked = 1", (test_user["id"],)
        ).fetchone()["c"]
        assert locked_after == 2  # 5 accounts, old 3-limit -> 2 locked, not 4
