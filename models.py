from database import get_db


def get_all_accounts():
    db = get_db()
    rows = db.execute("SELECT * FROM accounts").fetchall()
    db.close()
    return rows


def get_active_accounts():
    db = get_db()
    rows = db.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY LOWER(name)").fetchall()
    db.close()
    return rows


def get_account_by_name(name: str):
    db = get_db()
    row = db.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
    db.close()
    return row


def update_account_balance(name: str, delta: float):
    db = get_db()
    db.execute("UPDATE accounts SET balance = balance + ? WHERE name = ?", (delta, name))
    db.commit()
    db.close()