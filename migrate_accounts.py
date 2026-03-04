import json
from pathlib import Path

from database import init_db, get_db

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"
ACCOUNTS_JSON = DATA_DIR / "Accounts.json"


def main():
    init_db()

    if not ACCOUNTS_JSON.exists():
        print("Accounts.json not found:", ACCOUNTS_JSON)
        return

    with open(ACCOUNTS_JSON, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    db = get_db()

    for name, info in accounts.items():
        db.execute(
            """
            INSERT OR REPLACE INTO accounts (name, balance, type, active)
            VALUES (?, ?, ?, ?)
            """,
            (
                name,
                float(info.get("balance", 0.0)),
                info.get("type", "current"),
                1 if info.get("active", True) else 0,
            ),
        )

    db.commit()
    db.close()

    print("Accounts migrated to SQLite.")


if __name__ == "__main__":
    main()