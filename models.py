from database import get_db, release_db, USE_POSTGRES


# --- ROW CONVERTER ---
# Converts raw database rows into plain Python dicts so we can use row["column"] syntax everywhere
# Postgres returns tuples, SQLite returns sqlite3.Row objects — this normalises both
def _rows_as_dicts(cursor, rows):
    if USE_POSTGRES:
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    else:
        return [dict(row) for row in rows]


# --- GET ALL ACCOUNTS ---
# Returns every account for a user (including inactive ones)
# Used in places where we need the full account list, not just active ones
def get_all_accounts(user_id):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE user_id = %s", (user_id,))
    else:
        cursor.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    release_db(db)
    return rows


# --- GET ACTIVE ACCOUNTS ---
# Returns only accounts marked active=1, sorted alphabetically
# Used in dropdowns and the dashboard — hidden/deactivated accounts are excluded
def get_active_accounts(user_id):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE active = 1 AND user_id = %s ORDER BY LOWER(name)", (user_id,))
    else:
        cursor.execute("SELECT * FROM accounts WHERE active = 1 AND user_id = ? ORDER BY LOWER(name)", (user_id,))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    release_db(db)
    return rows


# --- GET ACCOUNT BY NAME ---
# Looks up a single account by its name for a given user
# Returns the account dict, or None if it doesn't exist
def get_account_by_name(name: str, user_id: int):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM accounts WHERE name = %s AND user_id = %s", (name, user_id))
    else:
        cursor.execute("SELECT * FROM accounts WHERE name = ? AND user_id = ?", (name, user_id))
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    release_db(db)
    return rows[0] if rows else None


# --- UPDATE ACCOUNT BALANCE ---
# Adds or subtracts an amount from an account's balance (delta can be positive or negative)
# Used after every expense, income, transfer, or import to keep balances in sync
def update_account_balance(name: str, delta: float, user_id: int):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = balance + %s WHERE name = %s AND user_id = %s", (delta, name, user_id))
    else:
        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE name = ? AND user_id = ?", (delta, name, user_id))
    db.commit()
    cursor.close()
    release_db(db)


# --- ADD TRANSACTION ---
# Inserts a new transaction row into the database
# type can be: 'manual', 'bill', 'income', 'transfer', 'import'
# category defaults to 'Other' if not provided
def add_transaction(date: str, description: str, amount: float, account: str, user_id: int, type: str = "manual", category: str = "Other"):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (date, description, amount, account, user_id, type, category)
        )
    else:
        cursor.execute(
            "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, description, amount, account, user_id, type, category)
        )
    db.commit()
    cursor.close()
    release_db(db)


# --- GET RECENT TRANSACTIONS ---
# Returns all transactions for a user, newest first
# Includes category column so it can be displayed on the transactions page
def get_recent_transactions(user_id: int):
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT id, date, description, amount, account, category FROM transactions WHERE user_id = %s ORDER BY date DESC",
            (user_id,)
        )
    else:
        cursor.execute(
            "SELECT id, date, description, amount, account, category FROM transactions WHERE user_id = ? ORDER BY date DESC",
            (user_id,)
        )
    rows = _rows_as_dicts(cursor, cursor.fetchall())
    cursor.close()
    release_db(db)
    return rows
