from database import get_db, USE_POSTGRES


def _rows_as_dicts(cursor, rows):
    if USE_POSTGRES:
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    else:
        return [dict(row) for row in rows]


def get_all_accounts():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM accounts")
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows


def get_active_accounts():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY LOWER(name)")
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows


def get_account_by_name(name: str):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE name = %s", (name,))
    else:
        cursor.execute("SELECT * FROM accounts WHERE name = ?", (name,))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows[0] if rows else None


def update_account_balance(name: str, delta: float):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = balance + %s WHERE name = %s", (delta, name))
    else:
        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE name = ?", (delta, name))
    db.commit()
    cursor.close()
    db.close()


def add_transaction(date: str, description: str, amount: float, account: str, type: str = "manual"):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, type) VALUES (%s, %s, %s, %s, %s)",
            (date, description, amount, account, type)
        )
    else:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, type) VALUES (?, ?, ?, ?, ?)",
            (date, description, amount, account, type)
        )
    db.commit()
    cursor.close()
    db.close()


def get_recent_transactions(limit=100):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT date, description, amount, account FROM transactions ORDER BY date DESC LIMIT %s",
            (limit,)
        )
    else:
        cursor.execute(
            "SELECT date, description, amount, account FROM transactions ORDER BY date DESC LIMIT ?",
            (limit,)
        )
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows