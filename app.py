from __future__ import annotations

from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask import session
from werkzeug.security import generate_password_hash, check_password_hash

import traceback
import sys

import os

import csv
from datetime import date
from pathlib import Path
from typing import Dict, List

from flask import Flask, request, redirect, url_for, render_template

from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict
import json, uuid

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

from database import USE_POSTGRES

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"

class PostgresSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None):
        super().__init__(initial or {})
        self.sid = sid
        self.modified = False

class PostgresSessionInterface(SessionInterface):
    def _get_db(self):
        import psycopg2
        return psycopg2.connect(os.environ.get("DATABASE_URL"))

    def open_session(self, app, request):
        sid = request.cookies.get("session")
        if sid and os.environ.get("DATABASE_URL"):
            try:
                db = self._get_db()
                cur = db.cursor()
                cur.execute("SELECT data FROM flask_sessions WHERE sid = %s", (sid,))
                row = cur.fetchone()
                cur.close()
                db.close()
                if row:
                    data = json.loads(row[0])
                    return PostgresSession(data, sid=sid)
            except Exception as e:
                print(f">>> Session open error: {e}", flush=True)
        sid = str(uuid.uuid4())
        return PostgresSession(sid=sid)

    def save_session(self, app, session, response):
        if not session or not os.environ.get("DATABASE_URL"):
            return
        sid = session.sid
        data = json.dumps(dict(session))
        try:
            db = self._get_db()
            cur = db.cursor()
            cur.execute("""
                INSERT INTO flask_sessions (sid, data) VALUES (%s, %s)
                ON CONFLICT (sid) DO UPDATE SET data = EXCLUDED.data
            """, (sid, data))
            db.commit()
            cur.close()
            db.close()
        except Exception as e:
            print(f">>> Session save error: {e}", flush=True)
        response.set_cookie("session", sid, httponly=True, secure=True, samesite="Lax")

app = Flask(__name__)
app.session_interface = PostgresSessionInterface()
app.secret_key = os.environ.get("SECRET_KEY", "waheguruji")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, id, email):
        self.id = id
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    print(f">>> load_user called with: {user_id}", flush=True)
    if not user_id or user_id == "None":
        return None
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    else:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    db.close()
    if row:
        row = dict(zip(cols, row))
        print(f">>> load_user found user: {row['email']}", flush=True)
        return User(row["id"], row["email"])
    print(f">>> load_user found NO user for id: {user_id}", flush=True)
    return None

from database import init_db
try:
    with app.app_context():
        init_db()
    print(">>> init_db completed successfully", flush=True)
except Exception as e:
    print(f">>> init_db FAILED: {e}", flush=True)

import time

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

import calendar
from datetime import datetime

def load_scheduled_expenses_web():
    from database import get_db, USE_POSTGRES
    from flask_login import current_user
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    db.close()
    return rows

def get_all_scheduled_expenses():
    from database import get_db, USE_POSTGRES
    from flask_login import current_user
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s ORDER BY day", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ? ORDER BY day", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    db.close()
    return rows

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
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT amount FROM transactions
            WHERE EXTRACT(YEAR FROM date::date) = %s
            AND EXTRACT(MONTH FROM date::date) = %s
            AND user_id = %s
            """,
            (year, month, current_user.id)
        )
    else:
        cursor.execute(
            """
            SELECT amount FROM transactions
            WHERE strftime('%Y', date) = ?
            AND strftime('%m', date) = ?
            AND user_id = ?
            """,
            (str(year), f"{month:02d}", current_user.id)
        )

    rows = cursor.fetchall()
    cursor.close()
    db.close()

    normal_spend = 0.0

    for r in rows:
        amount = r[0] if USE_POSTGRES else r["amount"]
        if amount < 0:
            normal_spend += abs(amount)

    return {
        "normal": normal_spend,
        "scheduled": 0,
        "total": normal_spend
    }

@app.get("/")
@login_required
def home():

    accounts_rows = get_active_accounts(current_user.id)
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
@login_required
def transactions():

    tx = get_recent_transactions(current_user.id)

    return render_template(
        "transactions.html",
        transactions=tx
    )

@app.get("/actions")
@login_required
def actions():
    accounts_rows = get_active_accounts(current_user.id)
    accounts = [r["name"] for r in accounts_rows]
    return render_template("actions.html", accounts=accounts, message=request.args.get("msg", ""))

@app.get("/bills")
@login_required
def bills():
    return render_template(
        "bills.html",
        bills=get_all_scheduled_expenses()
    )

@app.post("/add-expense")
@login_required
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

    add_transaction(today_str, description, amount, account, current_user.id)

    update_account_balance(account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Added {description}: £{abs(amount):.2f} from {account}")
    )

@app.post("/add-income")
@login_required
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

    add_transaction(today_str, description, amount, account, current_user.id)

    update_account_balance(account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Added income {description}: £{amount:.2f} to {account}")
    )

@app.post("/transfer")
@login_required
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

    add_transaction(today_str, f"Transfer to {to_account}", -amount, from_account, current_user.id)
    add_transaction(today_str, f"Transfer from {from_account}", amount, to_account, current_user.id)

    update_account_balance(from_account, -amount, current_user.id)
    update_account_balance(to_account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Transferred £{amount:.2f} from {from_account} → {to_account}")
    )

@app.post("/afford")
@login_required
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

    accounts_rows = get_active_accounts(current_user.id)

    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"])
        }
    from database import get_db, USE_POSTGRES
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    scheduled = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM future_events WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM future_events WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    future_events_raw = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    db.close()

    from datetime import date as date_type
    future_events = []
    for e in future_events_raw:
        try:
            future_events.append({
                "date": date_type.fromisoformat(e["date"]),
                "name": e["name"],
                "amount": e["amount"],
                "account": e["account"]
            })
        except:
            continue

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
@login_required
def settings():
    from database import get_db, USE_POSTGRES
    db = get_db()
    cursor = db.cursor()

    uid = current_user.id

    def fetch_filtered(query, params):
        cursor.execute(query, params)
        if USE_POSTGRES:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return [dict(r) for r in cursor.fetchall()]

    accounts = fetch_filtered("SELECT * FROM accounts WHERE active = 1 AND user_id = %s ORDER BY LOWER(name)" if USE_POSTGRES else "SELECT * FROM accounts WHERE active = 1 AND user_id = ? ORDER BY LOWER(name)", (uid,))
    bills = fetch_filtered("SELECT * FROM scheduled_expenses WHERE user_id = %s ORDER BY day" if USE_POSTGRES else "SELECT * FROM scheduled_expenses WHERE user_id = ? ORDER BY day", (uid,))
    savings_rules = fetch_filtered("SELECT * FROM savings_rules WHERE user_id = %s ORDER BY day" if USE_POSTGRES else "SELECT * FROM savings_rules WHERE user_id = ? ORDER BY day", (uid,))
    future_events = fetch_filtered("SELECT * FROM future_events WHERE user_id = %s ORDER BY date" if USE_POSTGRES else "SELECT * FROM future_events WHERE user_id = ? ORDER BY date", (uid,))
    income = fetch_filtered("SELECT * FROM income WHERE user_id = %s" if USE_POSTGRES else "SELECT * FROM income WHERE user_id = ?", (uid,))

    cursor.close()
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
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id) VALUES (%s, %s, %s, 1, %s) ON CONFLICT (name) DO NOTHING", (name, balance, acc_type, current_user.id))
    else:
        cursor.execute("INSERT OR IGNORE INTO accounts (name, balance, type, active, user_id) VALUES (?, ?, ?, 1, ?)", (name, balance, acc_type, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Account '{name}' created."))

@app.post("/settings/deactivate-account")
@login_required
def settings_deactivate_account():
    name = (request.form.get("name") or "").strip()
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET active = 0 WHERE name = %s AND user_id = %s", (name, current_user.id))
    else:
        cursor.execute("UPDATE accounts SET active = 0 WHERE name = ? AND user_id = ?", (name, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Account '{name}' deactivated."))

@app.post("/settings/edit-account")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET name=%s, type=%s, balance=%s WHERE id=%s AND user_id=%s", (name, acc_type, balance, account_id, current_user.id))
    else:
        cursor.execute("UPDATE accounts SET name=?, type=?, balance=? WHERE id=? AND user_id=?", (name, acc_type, balance, account_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Account updated."))

@app.post("/settings/add-bill")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id) VALUES (%s, %s, %s, %s, %s)", (name, amount, day, account, current_user.id))
    else:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id) VALUES (?, ?, ?, ?, ?)", (name, amount, day, account, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Bill '{name}' added."))

@app.post("/settings/edit-bill")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE scheduled_expenses SET name=%s, amount=%s, day=%s, account=%s WHERE id=%s AND user_id=%s", (name, amount, day, account, bill_id, current_user.id))
    else:
        cursor.execute("UPDATE scheduled_expenses SET name=?, amount=?, day=?, account=? WHERE id=? AND user_id=?", (name, amount, day, account, bill_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Bill updated."))

@app.post("/settings/delete-bill")
@login_required
def settings_delete_bill():
    bill_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM scheduled_expenses WHERE id = %s AND user_id = %s", (bill_id, current_user.id))
    else:
        cursor.execute("DELETE FROM scheduled_expenses WHERE id = ? AND user_id = ?", (bill_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Bill deleted."))

@app.post("/settings/add-savings-rule")
@login_required
def settings_add_savings_rule():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "1").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()

    if not name or not amount or not from_account or not to_account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    else:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Savings rule '{name}' added."))

@app.post("/settings/edit-savings-rule")
@login_required
def settings_edit_savings_rule():
    rule_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "1").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()

    if not name or not amount or not from_account or not to_account:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        amount = float(amount)
        day = int(day)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount or day."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE savings_rules SET name=%s, amount=%s, day=%s, frequency=%s, from_account=%s, to_account=%s WHERE id=%s AND user_id=%s", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    else:
        cursor.execute("UPDATE savings_rules SET name=?, amount=?, day=?, frequency=?, from_account=?, to_account=? WHERE id=? AND user_id=?", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Savings rule updated."))

@app.post("/settings/delete-savings-rule")
@login_required
def settings_delete_savings_rule():
    rule_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM savings_rules WHERE id = %s AND user_id = %s", (rule_id, current_user.id))
    else:
        cursor.execute("DELETE FROM savings_rules WHERE id = ? AND user_id = ?", (rule_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Savings rule deleted."))

@app.post("/settings/add-future-event")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO future_events (name, amount, date, account, user_id) VALUES (%s, %s, %s, %s, %s)", (name, amount, date_input, account, current_user.id))
    else:
        cursor.execute("INSERT INTO future_events (name, amount, date, account, user_id) VALUES (?, ?, ?, ?, ?)", (name, amount, date_input, account, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Future event '{name}' added."))

@app.post("/settings/edit-future-event")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE future_events SET name=%s, amount=%s, date=%s, account=%s WHERE id=%s AND user_id=%s", (name, amount, date_input, account, event_id, current_user.id))
    else:
        cursor.execute("UPDATE future_events SET name=?, amount=?, date=?, account=? WHERE id=? AND user_id=?", (name, amount, date_input, account, event_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Future event updated."))

@app.post("/settings/add-income")
@login_required
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

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO income (name, amount, frequency, account, user_id) VALUES (%s, %s, %s, %s, %s)", (name, amount, frequency, account, current_user.id))
    else:
        cursor.execute("INSERT INTO income (name, amount, frequency, account, user_id) VALUES (?, ?, ?, ?, ?)", (name, amount, frequency, account, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg=f"Income source '{name}' added."))

@app.post("/settings/update-income")
@login_required
def settings_update_income():
    income_id = request.form.get("id")
    amount = (request.form.get("amount") or "").strip()

    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE income SET amount = %s WHERE id = %s AND user_id = %s", (amount, income_id, current_user.id))
    else:
        cursor.execute("UPDATE income SET amount = ? WHERE id = ? AND user_id = ?", (amount, income_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Income updated."))

@app.post("/settings/delete-income")
@login_required
def settings_delete_income():
    income_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM income WHERE id = %s AND user_id = %s", (income_id, current_user.id))
    else:
        cursor.execute("DELETE FROM income WHERE id = ? AND user_id = ?", (income_id, current_user.id))
    db.commit()
    cursor.close()
    db.close()
    return redirect(url_for("settings", msg="Income source deleted."))

@app.get("/register")
def register():
    return render_template("register.html")

@app.post("/register")
def register_post():
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
    confirm = (request.form.get("confirm") or "").strip()

    if not email or not password:
        return render_template("register.html", error="All fields are required.")

    if password != confirm:
        return render_template("register.html", error="Passwords do not match.")

    if len(password) < 6:
        return render_template("register.html", error="Password must be at least 6 characters.")

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    else:
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))

    existing = cursor.fetchone()

    if existing:
        cursor.close()
        db.close()
        return render_template("register.html", error="An account with that email already exists.")

    hashed = generate_password_hash(password)
    today_str = date.today().isoformat()

    if USE_POSTGRES:
        cursor.execute("INSERT INTO users (email, password, created_at) VALUES (%s, %s, %s) RETURNING id",
                       (email, hashed, today_str))
        user_id = cursor.fetchone()[0]
    else:
        cursor.execute("INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)",
                       (email, hashed, today_str))
        user_id = cursor.lastrowid

    db.commit()
    cursor.close()
    db.close()

    user = User(user_id, email)
    login_user(user, remember=True)
    return redirect(url_for("home"))


@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()

    if not email or not password:
        return render_template("login.html", error="All fields are required.")

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    else:
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))

    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    db.close()

    if not row:
        return render_template("login.html", error="Invalid email or password.")

    row = dict(zip(cols, row))

    if not check_password_hash(row["password"], password):
        print(f">>> Password check failed for {email}", flush=True)
        return render_template("login.html", error="Invalid email or password.")
    
    print(f">>> Password check passed for {email}", flush=True)

    user = User(row["id"], row["email"])
    login_user(user, remember=True)
    return redirect(url_for("home"))


@app.get("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)