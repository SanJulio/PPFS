from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Dict, List

from flask import Flask, request, redirect, url_for, render_template

from Tracker import simulate_balances_until, load_future_events, load_scheduled_expenses
import calendar
from datetime import timedelta

from models import (
    add_transaction,
    update_account_balance,
    get_active_accounts,
    get_recent_transactions
)

from database import get_db

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"

DAILY_EXPENSES = DATA_DIR / "Daily_Expenses.csv"

app = Flask(__name__)

from database import init_db
with app.app_context():
    init_db()

import time

@app.get("/ping")
def ping():
    print(">>> /ping hit", flush=True)
    return "pong"

@app.get("/bills-debug")
def bills_debug():
    print(">>> /bills-debug hit (start)", flush=True)
    t0 = time.time()

    try:
        bills = get_all_scheduled_expenses()
        print(f">>> loaded bills: {len(bills)} in {time.time()-t0:.3f}s", flush=True)
        return {
            "ok": True,
            "count": len(bills),
            "first": bills[0] if bills else None
        }
    except Exception as e:
        print(">>> ERROR in /bills-debug:", repr(e), flush=True)
        return {"ok": False, "error": repr(e)}, 500

SCHEDULED_CACHE = None
@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def ensure_csv_header(file_path: Path, header: List[str]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists() or file_path.stat().st_size == 0:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)

import calendar
from datetime import datetime

def load_scheduled_expenses_web():
    from database import get_db

    db = get_db()
    rows = db.execute("SELECT * FROM scheduled_expenses").fetchall()
    db.close()

    expenses = []
    for row in rows:
        expenses.append({
            "name": row["name"],
            "amount": row["amount"],
            "day": row["day"],
            "account": row["account"]
        })
    return expenses

def get_all_scheduled_expenses():
    from database import get_db

    db = get_db()
    rows = db.execute("SELECT * FROM scheduled_expenses ORDER BY day").fetchall()
    db.close()

    bills = []
    for row in rows:
        bills.append({
            "name": row["name"],
            "amount": row["amount"],
            "day": row["day"],
            "account": row["account"]
        })
    return bills

def calculate_financial_overview(accounts):
    today = datetime.today()
    current_day = today.day

    scheduled_expenses = load_scheduled_expenses_web()

    spending_types = {"current", "cash"}
    savings_types = {"savings"}

    spending_balance = 0.0
    savings_balance = 0.0

    # split balances
    for name, info in accounts.items():
        if not info.get("active", True):
            continue

        acc_type = info.get("type")
        balance = float(info.get("balance", 0.0))

        if acc_type in spending_types:
            spending_balance += balance
        elif acc_type in savings_types:
            savings_balance += balance

    # future bills remaining this month
    spending_future_bills = 0.0

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            acc = expense["account"]
            if acc in accounts and accounts[acc]["type"] in spending_types:
                spending_future_bills += expense["amount"]

    safe_spending = spending_balance - spending_future_bills
    net_worth = spending_balance + savings_balance

    return {
        "spending_balance": spending_balance,
        "future_bills": spending_future_bills,
        "safe_spending": safe_spending,
        "savings_balance": savings_balance,
        "net_worth": net_worth
    }

def calculate_monthly_spending():

    today = date.today()
    year = today.year
    month = today.month

    db = get_db()

    rows = db.execute(
        """
        SELECT amount
        FROM transactions
        WHERE strftime('%Y', date) = ?
        AND strftime('%m', date) = ?
        """,
        (str(year), f"{month:02d}")
    ).fetchall()

    db.close()

    normal_spend = 0.0
    income = 0.0

    for r in rows:
        amount = r["amount"]

        if amount < 0:
            normal_spend += abs(amount)
        else:
            income += amount

    return {
        "normal": normal_spend,
        "scheduled": 0,
        "total": normal_spend
    }

@app.get("/")
def home():

    accounts_rows = get_active_accounts()
    accounts = {}

    for r in accounts_rows:
      accounts[r["name"]] = {
          "balance": r["balance"],
          "type": r["type"],
          "active": bool(r["active"])
      }

    overview = calculate_financial_overview(accounts)
    monthly = calculate_monthly_spending()

    active_accounts = [n for n in accounts if accounts[n].get("active", True)]
    active_accounts.sort(key=lambda x: x.lower())

    balances = []
    for n in active_accounts:
        balances.append({
            "name": n,
            "balance": float(accounts[n].get("balance", 0.0)),
            "type": accounts[n].get("type", "")
        })

    return render_template(
        "index.html",
        message=request.args.get("msg", ""),
        accounts=active_accounts,
        overview=overview,
        balances=balances,
        monthly=monthly,
    )

@app.get("/transactions")
def transactions():

    tx = get_recent_transactions()

    return render_template(
        "transactions.html",
        transactions=tx
    )

@app.get("/actions")
def actions():
    accounts_rows = get_active_accounts()
    accounts = [r["name"] for r in accounts_rows]
    return render_template("actions.html", accounts=accounts, message=request.args.get("msg", ""))

@app.get("/bills")
def bills():
    return render_template(
        "bills.html",
        bills=get_all_scheduled_expenses()
    )

@app.post("/add-expense")
def add_expense():

    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    try:
        amount = float(amount_raw)
    except ValueError:
        return redirect(url_for("home", msg="Amount must be a number."))

    amount = -abs(amount)

    today_str = date.today().isoformat()

    add_transaction(today_str, description, amount, account)

    update_account_balance(account, amount)

    return redirect(
        url_for("actions", msg=f"Added {description}: £{abs(amount):.2f} from {account}")
    )

@app.post("/add-income")
def add_income():

    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    try:
        amount = float(amount_raw)
    except ValueError:
        return redirect(url_for("home", msg="Amount must be a number."))

    amount = abs(amount)

    today_str = date.today().isoformat()

    add_transaction(today_str, description, amount, account)

    update_account_balance(account, amount)

    return redirect(
        url_for("actions", msg=f"Added income {description}: £{amount:.2f} to {account}")
    )

@app.post("/transfer")
def transfer():
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()

    if not from_account or not to_account or not amount_raw:
        return redirect(url_for("home", msg="Missing fields."))

    if from_account == to_account:
        return redirect(url_for("home", msg="Cannot transfer to same account."))

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("home", msg="Enter a valid positive amount."))

    today_str = date.today().isoformat()

    add_transaction(today_str, f"Transfer to {to_account}", -amount, from_account)
    add_transaction(today_str, f"Transfer from {from_account}", amount, to_account)

    update_account_balance(from_account, -amount)
    update_account_balance(to_account, amount)

    return redirect(
        url_for("actions", msg=f"Transferred £{amount:.2f} from {from_account} → {to_account}")
    )

@app.post("/afford")
def afford():
    from datetime import date

    desc = (request.form.get("desc") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("home", msg="Invalid purchase amount."))

    accounts_rows = get_active_accounts()

    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"])
        }
    scheduled = load_scheduled_expenses(DATA_DIR)
    future_events = load_future_events(DATA_DIR)

    # end of next month horizon
    today = date.today()
    if today.month == 12:
        next_month = 1
        next_year = today.year + 1
    else:
        next_month = today.month + 1
        next_year = today.year

    last_day = calendar.monthrange(next_year, next_month)[1]
    horizon = date(next_year, next_month, last_day)

    results = []
    spending_accounts = [a for a in accounts if accounts[a]["type"] in ("current","cash") and accounts[a]["active"]]

    for acc in spending_accounts:
        temp_accounts = {k: v.copy() for k, v in accounts.items()}
        temp_accounts[acc]["balance"] -= amount

        final_bal, lowest_bal = simulate_balances_until(horizon, temp_accounts, scheduled, future_events)

        lowest = lowest_bal.get(acc, temp_accounts[acc]["balance"])
        negative = lowest < 0

        results.append({
            "account": acc,
            "after": temp_accounts[acc]["balance"],
            "lowest": lowest,
            "negative": negative
        })

    safe = [r for r in results if not r["negative"]]
    if safe:
        best = sorted(safe, key=lambda x: x["lowest"], reverse=True)[0]
        recommendation = f"Use {best['account']}"
    else:
        worst = sorted(results, key=lambda x: x["lowest"], reverse=True)[0]
        recommendation = f"No safe account — least bad: {worst['account']}"

    return render_template(
        "index.html",
        message="",
        accounts=[a for a in accounts if accounts[a]["active"]],
        balances=[{"name":a,"balance":accounts[a]["balance"],"type":accounts[a]["type"]} for a in accounts if accounts[a]["active"]],
        overview=calculate_financial_overview(accounts),
        afford_results=results,
        recommendation=recommendation,
        monthly=calculate_monthly_spending(),
    )

@app.get("/settings")
def settings():
    from database import get_db
    db = get_db()
    accounts = [dict(r) for r in db.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY LOWER(name)").fetchall()]
    bills = [dict(r) for r in db.execute("SELECT * FROM scheduled_expenses ORDER BY day").fetchall()]
    savings_rules = [dict(r) for r in db.execute("SELECT * FROM savings_rules ORDER BY day").fetchall()]
    future_events = [dict(r) for r in db.execute("SELECT * FROM future_events ORDER BY date").fetchall()]
    income = [dict(r) for r in db.execute("SELECT * FROM income").fetchall()]
    db.close()
    return render_template("settings.html",
        accounts=accounts,
        bills=bills,
        savings_rules=savings_rules,
        future_events=future_events,
        income=income,
        message=request.args.get("msg", "")
    )

@app.post("/settings/add-account")
def settings_add_account():
    name = (request.form.get("name") or "").strip()
    acc_type = (request.form.get("type") or "").strip()
    balance = (request.form.get("balance") or "0").strip()

    if not name or not acc_type:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        balance = float(balance)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid balance."))

    from database import get_db
    db = get_db()
    db.execute("INSERT OR IGNORE INTO accounts (name, balance, type, active) VALUES (?, ?, ?, 1)",
               (name, balance, acc_type))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Account '{name}' created."))

@app.post("/settings/deactivate-account")
def settings_deactivate_account():
    name = (request.form.get("name") or "").strip()
    from database import get_db
    db = get_db()
    db.execute("UPDATE accounts SET active = 0 WHERE name = ?", (name,))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Account '{name}' deactivated."))

@app.post("/settings/add-bill")
def settings_add_bill():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not day or not account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    from database import get_db
    db = get_db()
    db.execute("INSERT INTO scheduled_expenses (name, amount, day, account) VALUES (?, ?, ?, ?)",
               (name, amount, day, account))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Bill '{name}' added."))

@app.post("/settings/delete-bill")
def settings_delete_bill():
    bill_id = request.form.get("id")
    from database import get_db
    db = get_db()
    db.execute("DELETE FROM scheduled_expenses WHERE id = ?", (bill_id,))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Bill deleted."))

@app.post("/settings/add-savings-rule")
def settings_add_savings_rule():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()

    if not name or not amount or not day or not from_account or not to_account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    from database import get_db
    db = get_db()
    db.execute("INSERT INTO savings_rules (name, amount, day, from_account, to_account) VALUES (?, ?, ?, ?, ?)",
               (name, amount, day, from_account, to_account))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Savings rule '{name}' added."))

@app.post("/settings/delete-savings-rule")
def settings_delete_savings_rule():
    rule_id = request.form.get("id")
    from database import get_db
    db = get_db()
    db.execute("DELETE FROM savings_rules WHERE id = ?", (rule_id,))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Savings rule deleted."))

@app.post("/settings/add-future-event")
def settings_add_future_event():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    date_input = (request.form.get("date") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not date_input or not account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    from database import get_db
    db = get_db()
    db.execute("INSERT INTO future_events (name, amount, date, account) VALUES (?, ?, ?, ?)",
               (name, amount, date_input, account))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Future event '{name}' added."))

@app.post("/settings/update-income")
def settings_update_income():
    income_id = request.form.get("id")
    amount = (request.form.get("amount") or "").strip()

    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    from database import get_db
    db = get_db()
    db.execute("UPDATE income SET amount = ? WHERE id = ?", (amount, income_id))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Income updated."))

@app.post("/settings/add-income")
def settings_add_income():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    frequency = (request.form.get("frequency") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not frequency or not account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    from database import get_db
    db = get_db()
    db.execute("INSERT INTO income (name, amount, frequency, account) VALUES (?, ?, ?, ?)",
               (name, amount, frequency, account))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg=f"Income source '{name}' added."))

@app.post("/settings/delete-income")
def settings_delete_income():
    income_id = request.form.get("id")
    from database import get_db
    db = get_db()
    db.execute("DELETE FROM income WHERE id = ?", (income_id,))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Income source deleted."))

@app.post("/settings/edit-bill")
def settings_edit_bill():
    bill_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not day or not account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    from database import get_db
    db = get_db()
    db.execute("UPDATE scheduled_expenses SET name=?, amount=?, day=?, account=? WHERE id=?",
               (name, amount, day, account, bill_id))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Bill updated."))

@app.post("/settings/edit-savings-rule")
def settings_edit_savings_rule():
    rule_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()

    if not name or not amount or not day or not from_account or not to_account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    from database import get_db
    db = get_db()
    db.execute("UPDATE savings_rules SET name=?, amount=?, day=?, from_account=?, to_account=? WHERE id=?",
               (name, amount, day, from_account, to_account, rule_id))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Savings rule updated."))

@app.post("/settings/edit-future-event")
def settings_edit_future_event():
    event_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    date_input = (request.form.get("date") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not date_input or not account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    from database import get_db
    db = get_db()
    db.execute("UPDATE future_events SET name=?, amount=?, date=?, account=? WHERE id=?",
               (name, amount, date_input, account, event_id))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Future event updated."))

@app.post("/settings/edit-account")
def settings_edit_account():
    account_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    acc_type = (request.form.get("type") or "").strip()
    balance = (request.form.get("balance") or "").strip()

    if not name or not acc_type or not balance:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        balance = float(balance)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid balance."))

    from database import get_db
    db = get_db()
    db.execute("UPDATE accounts SET name=?, type=?, balance=? WHERE id=?",
               (name, acc_type, balance, account_id))
    db.commit()
    db.close()
    return redirect(url_for("settings", msg="Account updated."))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)