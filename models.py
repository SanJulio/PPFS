from database import get_db, USE_POSTGRES


def _rows_as_dicts(cursor, rows):
    if USE_POSTGRES:
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    else:
        return [dict(row) for row in rows]


def get_all_accounts(user_id):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE user_id = %s", (user_id,))
    else:
        cursor.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows


def get_active_accounts(user_id):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE active = 1 AND user_id = %s ORDER BY LOWER(name)", (user_id,))
    else:
        cursor.execute("SELECT * FROM accounts WHERE active = 1 AND user_id = ? ORDER BY LOWER(name)", (user_id,))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows


def get_account_by_name(name: str, user_id: int):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE name = %s AND user_id = %s", (name, user_id))
    else:
        cursor.execute("SELECT * FROM accounts WHERE name = ? AND user_id = ?", (name, user_id))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows[0] if rows else None


def update_account_balance(name: str, delta: float, user_id: int):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = balance + %s WHERE name = %s AND user_id = %s", (delta, name, user_id))
    else:
        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE name = ? AND user_id = ?", (delta, name, user_id))
    db.commit()
    cursor.close()
    db.close()


def add_transaction(date: str, description: str, amount: float, account: str, user_id: int, type: str = "manual"):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type) VALUES (%s, %s, %s, %s, %s, %s)",
            (date, description, amount, account, user_id, type)
        )
    else:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type) VALUES (?, ?, ?, ?, ?, ?)",
            (date, description, amount, account, user_id, type)
        )
    db.commit()
    cursor.close()
    db.close()


def get_recent_transactions(user_id: int, limit=100):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT id, date, description, amount, account FROM transactions WHERE user_id = %s ORDER BY date DESC LIMIT %s",
            (user_id, limit)
        )
    else:
        cursor.execute(
            "SELECT id, date, description, amount, account FROM transactions WHERE user_id = ? ORDER BY date DESC LIMIT ?",
            (user_id, limit)
        )
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    db.close()
    return rows