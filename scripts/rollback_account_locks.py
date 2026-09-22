"""
Rollback reconciliation for the entitlement cutover.

A pure code revert (redeploying the pre-cutover app.py) is NOT sufficient
on its own: if any Basic-tier (1-account) users were locked down further
than the old code's 3-account assumption while the new code was live, the
old code has no trigger that re-fires sync_account_locks() on deploy - it
only calls that function from its own (unchanged) Stripe webhook trigger
points. Run this once, after reverting the code, to restore the old
3-account-based lock state for every user from the still-accurate,
paid-Stripe-only is_pro cache column (see CLAUDE.md - "is_pro cache
semantics": it was kept narrow and genuine specifically so this rollback
path stays reliable).

Usage:
    python scripts/rollback_account_locks.py [--apply]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app as flask_app, USE_POSTGRES, sync_account_locks
from database import get_db, release_db


def main():
    apply_changes = "--apply" in sys.argv

    with flask_app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id, email, is_pro FROM users")
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        users = [dict(zip(cols, r)) for r in rows]
        cursor.close()
        release_db(db)

        print(f"{len(users)} user(s) to reconcile back to the old is_pro-based lock model.")
        for u in users:
            limit = None if u["is_pro"] else 3
            print(f"  user_id={u['id']} email={u['email']} is_pro={u['is_pro']} -> account_limit={limit}")
            if apply_changes:
                sync_account_locks(u["id"], limit)

        if not apply_changes:
            print("Dry run only - re-run with --apply to write these changes.")
        else:
            print("Applied.")


if __name__ == "__main__":
    main()
