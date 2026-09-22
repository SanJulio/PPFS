"""
Legacy Free grandfathering backfill - Migrate phase of the entitlement
cutover.

Run this AFTER reconcile_stripe_subscribers.py has reported complete
coverage, and BEFORE ENTITLEMENT_RESOLVER_ACTIVE is ever set to true.
Must run while the OLD signup code is still the only thing creating
users (i.e. before Activation) - at that point every existing row in
the table predates any new-model signup, so this can run once, cleanly,
against the full table with no possible race against a concurrently
created new-model account.

One transaction: the UPDATE and the schema_migrations version record
commit together, or neither commits at all. A crash mid-run leaves zero
rows changed - safe to simply re-run. Re-running after a successful run
is a fast no-op (the version-row guard short-circuits before touching
any data).

The WHERE clause is deliberately not just a date comparison:

    WHERE created_at <= :cutoff_date AND trial_started_at IS NULL

`trial_started_at IS NULL` is the real discriminator - it identifies
"this row predates the new signup code" precisely, immune to same-day
ambiguity around deploy time (created_at is date-only). Any row the new
signup code already initialised (trial_started_at IS NOT NULL) is never
touched here, regardless of what its created_at date says - see
CLAUDE.md's "Pricing/entitlement model" section for the full reasoning.

`created_at` is TEXT on both Postgres and SQLite in this schema (storing
plain ISO date strings, e.g. "2026-09-21") - ISO-8601 date strings sort
correctly under a plain string comparison, so this query needs no
engine-specific casting (no ::date) and runs identically on both.

Usage:
    python scripts/grandfather_legacy_free.py --cutoff 2026-09-21 [--apply]
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app as flask_app, USE_POSTGRES
from database import get_db, release_db

MIGRATION_VERSION = "grandfather_legacy_free_v1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", required=True, help="Cutoff date (YYYY-MM-DD), inclusive - everyone created on or before this date is Legacy Free.")
    parser.add_argument("--apply", action="store_true", help="Without this, dry-run only.")
    args = parser.parse_args()

    with flask_app.app_context():
        db = get_db()
        cursor = db.cursor()

        ph = "%s" if USE_POSTGRES else "?"
        cursor.execute(f"SELECT 1 FROM schema_migrations WHERE version = {ph}", (MIGRATION_VERSION,))
        if cursor.fetchone():
            print(f"'{MIGRATION_VERSION}' already applied - nothing to do.")
            cursor.close()
            release_db(db)
            sys.exit(0)

        cursor.execute(
            f"SELECT COUNT(*) FROM users WHERE created_at <= {ph} AND trial_started_at IS NULL",
            (args.cutoff,),
        )
        count_row = cursor.fetchone()
        matched = count_row[0] if USE_POSTGRES else count_row[0]
        print(f"{matched} user(s) match created_at <= {args.cutoff} AND trial_started_at IS NULL — would be set to fallback_entitlement='legacy_free'.")

        if not args.apply:
            print("Dry run only - re-run with --apply to write this change.")
            cursor.close()
            release_db(db)
            sys.exit(0)

        try:
            cursor.execute(
                f"UPDATE users SET fallback_entitlement = 'legacy_free' "
                f"WHERE created_at <= {ph} AND trial_started_at IS NULL",
                (args.cutoff,),
            )
            updated = cursor.rowcount
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            now_param = now if USE_POSTGRES else now.isoformat()
            cursor.execute(
                f"INSERT INTO schema_migrations (version, applied_at) VALUES ({ph}, {ph})",
                (MIGRATION_VERSION, now_param),
            )
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"ERROR - transaction rolled back, nothing was changed: {e}")
            cursor.close()
            release_db(db)
            sys.exit(1)

        cursor.close()
        release_db(db)
        print(f"Applied: {updated} user(s) set to Legacy Free. Recorded '{MIGRATION_VERSION}' in schema_migrations.")
        sys.exit(0)


if __name__ == "__main__":
    main()
