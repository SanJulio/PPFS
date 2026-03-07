from database import get_db

db = get_db()
cur = db.cursor()

tables = ["accounts", "transactions", "scheduled_expenses", "income", "savings_rules", "future_events"]

for table in tables:
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id INTEGER NOT NULL DEFAULT 1")
        print(f"✔ Added user_id to {table}")
    except Exception as e:
        print(f"✘ {table}: {e}")

db.commit()
cur.close()
db.close()
print("Done")