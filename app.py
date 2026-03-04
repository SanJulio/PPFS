from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Dict, List

from flask import Flask, request, redirect, url_for, render_template_string

from Tracker import simulate_balances_until, load_future_events, load_scheduled_expenses
import calendar
from datetime import timedelta

from models import (
    add_transaction,
    update_account_balance,
    get_active_accounts
)

from database import get_db

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"

DAILY_EXPENSES = DATA_DIR / "Daily_Expenses.csv"
ACCOUNTS_JSON = DATA_DIR / "Accounts.json"
PAYMENTS_LOG = DATA_DIR / "Payments_Log.csv"

app = Flask(__name__)

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

def load_accounts() -> Dict:
    import json
    if not ACCOUNTS_JSON.exists():
        return {}
    with open(ACCOUNTS_JSON, "r", encoding="utf-8") as f:
        accounts = json.load(f)
    # ensure active default
    for acc in accounts:
        if "active" not in accounts[acc]:
            accounts[acc]["active"] = True
    return accounts

def save_accounts(accounts: Dict) -> None:
    import json, os, tempfile
    # atomic write
    with tempfile.NamedTemporaryFile("w", delete=False, dir=DATA_DIR, encoding="utf-8") as tmp:
        json.dump(accounts, tmp, indent=4)
        temp_name = tmp.name
    os.replace(temp_name, ACCOUNTS_JSON)

import calendar
from datetime import datetime

def load_scheduled_expenses_web():
    path = DATA_DIR / "Scheduled_Expenses.csv"
    expenses = []

    if not path.exists():
        return expenses

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                expenses.append({
                    "name": row["name"],
                    "amount": float(row["amount"]),
                    "day": int(row["day"]) if row["day"] else None,
                    "account": row["account"]
                })
            except:
                continue
    return expenses

def get_all_scheduled_expenses():
    global SCHEDULED_CACHE

    if SCHEDULED_CACHE is not None:
        return SCHEDULED_CACHE

    path = DATA_DIR / "Scheduled_Expenses.csv"
    bills = []

    if not path.exists():
        SCHEDULED_CACHE = bills
        return bills

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bills.append({
                    "name": row["name"],
                    "amount": float(row["amount"]),
                    "day": row["day"],
                    "account": row["account"]
                })
            except:
                continue

    bills.sort(key=lambda x: int(x["day"]) if x["day"] else 99)

    SCHEDULED_CACHE = bills
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

BILLS_PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<title>Scheduled Bills</title>

<style>
body { background:#f2f4f7; padding:20px; }
.card { border-radius:18px; box-shadow:0 6px 18px rgba(0,0,0,0.08); border:none; }
</style>
</head>

<body>

<div class="container">
<div class="card p-3">
<h4 class="mb-3">Scheduled Expenses</h4>

<div class="table-responsive">
<table class="table table-sm align-middle mb-0">
<thead>
<tr>
<th>Name</th>
<th class="text-end">Amount</th>
<th class="text-end">Day</th>
<th class="text-end">Account</th>
</tr>
</thead>

<tbody>
{% for b in bills %}
<tr>
<td>{{ b.name }}</td>
<td class="text-end">£{{ "%.2f"|format(b.amount) }}</td>
<td class="text-end">{{ b.day }}</td>
<td class="text-end">{{ b.account }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>

<div class="mt-3">
<a href="/" class="btn btn-dark w-100">← Back to Home</a>
</div>

</div>
</div>

</body>
</html>
"""

PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#ffffff">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<title>PPFS</title>

<style>

html, body {
    min-height: 100vh;
}
html {
    scroll-padding-bottom: 110px;
}
html {
    scroll-behavior: smooth;
}
.navbar {
    height: 70px;
}

.fixed-bottom {
    position: fixed !important;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 9999;
}

body {
    background: #f2f4f7;
    padding-bottom: 110px;
}

.card {
    border-radius: 18px;
    border: none;
    box-shadow: 0 6px 18px rgba(0,0,0,0.08);
}

h1 {
    font-weight: 700;
}

.btn-primary {
    background: black;
    border: none;
    border-radius: 14px;
    padding: 12px;
    font-size: 16px;
}

input, select {
    border-radius: 12px !important;
    padding: 12px !important;
    font-size: 16px !important;
}

/* ...keep your existing styles... */

.section-card { padding: 16px; margin-bottom: 16px; }

.form-label { font-weight: 600; }

.table td, .table th { vertical-align: middle; }

.table-wide {
    min-width: 520px;
}

.ellipsis {
  max-width: 220px;          /* adjust if you want */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.money { font-variant-numeric: tabular-nums; }

.badge-soft {
  background: rgba(0,0,0,0.06);
  color: #111;
  border-radius: 999px;
  padding: 6px 10px;
  font-weight: 600;
  font-size: 12px;
}
</style>
</head>
<body>
<div class="container py-4">
<div class="mb-4" id="home">
  <h1 class="display-6 fw-bold">PPFS</h1>
  <div class="text-muted">Personal Finance System</div>
</div>

  {% if message %}
    <div class="msg">{{ message }}</div>
  {% endif %}

  <div class="card p-3 mb-4">
  <h5 class="mb-3">Financial Overview</h5>

  <div class="row g-3">

    <div class="col-6">
      <div class="p-3 bg-white rounded-4 shadow-sm">
        <div class="text-muted small">Available</div>
        <div class="fs-4 fw-bold">£{{ "%.2f"|format(overview.spending_balance) }}</div>
      </div>
    </div>

    <div class="col-6">
      <div class="p-3 bg-white rounded-4 shadow-sm">
        <div class="text-muted small">Bills left</div>
        <div class="fs-4 fw-bold text-danger">£{{ "%.2f"|format(overview.future_bills) }}</div>
      </div>
    </div>

    <div class="col-6">
      <div class="p-3 bg-white rounded-4 shadow-sm">
        <div class="text-muted small">Safe to spend</div>
        <div class="fs-4 fw-bold text-success">£{{ "%.2f"|format(overview.safe_spending) }}</div>
      </div>
    </div>

    <div class="col-6">
      <div class="p-3 bg-white rounded-4 shadow-sm">
        <div class="text-muted small">Savings</div>
        <div class="fs-4 fw-bold">£{{ "%.2f"|format(overview.savings_balance) }}</div>
      </div>
    </div>

    <div class="col-12">
      <div class="p-3 bg-dark text-white rounded-4 text-center">
        <div class="small">Net Worth</div>
        <div class="fs-3 fw-bold">£{{ "%.2f"|format(overview.net_worth) }}</div>
      </div>
    </div>

  </div>
</div>

<div class="card p-3 mb-4">
  <h5 class="mb-3">This Month's Spending</h5>

  <div class="row g-3">

    <div class="col-4">
      <div class="p-3 bg-white rounded-4 shadow-sm text-center">
        <div class="text-muted small">Normal spending</div>
        <div class="fs-5 fw-bold">£{{ "%.2f"|format(monthly.normal) }}</div>
      </div>
    </div>

    <div class="col-4">
      <div class="p-3 bg-white rounded-4 shadow-sm text-center">
        <div class="text-muted small">Bills paid</div>
        <div class="fs-5 fw-bold">£{{ "%.2f"|format(monthly.scheduled) }}</div>
      </div>
    </div>

    <div class="col-4">
      <div class="p-3 bg-dark text-white rounded-4 text-center">
        <div class="small">Total outflow</div>
        <div class="fs-5 fw-bold">£{{ "%.2f"|format(monthly.total) }}</div>
      </div>
    </div>

  </div>
</div>

  <div class="card p-3 mb-4" id="afford">
  <div class="d-flex align-items-center justify-content-between mb-2">
    <h5 class="mb-0">Can I afford this?</h5>
    <span class="badge-soft">Forecast to end of next month</span>
  </div>

  <form method="POST" action="{{ url_for('afford') }}">
    <div class="mb-3">
      <label class="form-label">Item (optional)</label>
      <input class="form-control" name="desc" placeholder="e.g., Trainers" />
    </div>

    <div class="mb-3">
      <label class="form-label">Purchase amount (£)</label>
      <input class="form-control" name="amount" inputmode="decimal" placeholder="e.g., 180" required />
    </div>

    <button type="submit" class="btn btn-dark w-100 py-3">Check</button>
  </form>

  {% if afford_results %}
    <hr class="my-3">

    <div class="small text-muted mb-2">Results</div>

    <div class="vstack gap-2">
      {% for r in afford_results %}
        <div class="border rounded-4 p-3 bg-white">
          <div class="d-flex justify-content-between align-items-center">
            <div class="fw-semibold">{{ r.account }}</div>
            {% if r.negative %}
              <span class="badge text-bg-danger">Risk</span>
            {% else %}
              <span class="badge text-bg-success">Safe</span>
            {% endif %}
          </div>

          <div class="small text-muted mt-2">
            After purchase: <span class="money">£{{ "%.2f"|format(r.after) }}</span><br>
            Lowest predicted: <span class="money">£{{ "%.2f"|format(r.lowest) }}</span>
          </div>
        </div>
      {% endfor %}
    </div>

    <div class="mt-3 small text-muted">Recommendation</div>
    <div class="fw-bold">{{ recommendation }}</div>
  {% endif %}
</div>

  <div class="card p-3 mb-4" id="expense">
  <h5 class="mb-3">Add Expense</h5>

  <form method="POST" action="{{ url_for('add_expense') }}">

    <div class="mb-3">
      <label class="form-label">Description</label>
      <input class="form-control" name="description" placeholder="Tesco meal deal" required>
    </div>

    <div class="mb-3">
      <label class="form-label">Amount (£)</label>
      <input class="form-control" name="amount" inputmode="decimal" placeholder="4.50" required>
    </div>

    <div class="mb-3">
      <label class="form-label">Account</label>
      <select class="form-select" name="account" required>
        {% for a in accounts %}
        <option value="{{ a }}">{{ a }}</option>
        {% endfor %}
      </select>
    </div>

    <button type="submit" class="btn btn-dark w-100 py-3">
      Add Expense
    </button>

  </form>
</div>

  <div class="card p-3 mb-4" id="income">
  <h5 class="mb-3">Add income</h5>

  <form method="POST" action="{{ url_for('add_income') }}">
    <div class="mb-3">
      <label class="form-label">Description</label>
      <input class="form-control" name="description" placeholder="e.g., Salary / Refund" required />
    </div>

    <div class="mb-3">
      <label class="form-label">Amount (£)</label>
      <input class="form-control" name="amount" inputmode="decimal" placeholder="e.g., 1200" required />
    </div>

    <div class="mb-2">
      <label class="form-label">Account</label>
      <select class="form-select" name="account" required>
        {% for a in accounts %}
          <option value="{{ a }}">{{ a }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="small text-muted mb-3">
      Recorded in transaction ledger
    </div>

    <button type="submit" class="btn btn-dark w-100 py-3">Add income</button>
  </form>
</div>

  <div class="card p-3 mb-4" id="transfer">
  <h5 class="mb-3">Transfer between accounts</h5>

  <form method="POST" action="{{ url_for('transfer') }}">
    <div class="mb-3">
      <label class="form-label">From account</label>
      <select class="form-select" name="from_account" required>
        {% for a in accounts %}
          <option value="{{ a }}">{{ a }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="mb-3">
      <label class="form-label">To account</label>
      <select class="form-select" name="to_account" required>
        {% for a in accounts %}
          <option value="{{ a }}">{{ a }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="mb-3">
      <label class="form-label">Amount (£)</label>
      <input class="form-control" name="amount" inputmode="decimal" placeholder="e.g., 100" required />
    </div>

    <button type="submit" class="btn btn-dark w-100 py-3">Transfer</button>
  </form>
</div>
  
  <div class="card p-3 mb-4" id="accounts">
  <div class="d-flex justify-content-between align-items-center mb-2">
    <h5 class="mb-0">Active account balances</h5>
    <span class="badge-soft">{{ balances|length }} accounts</span>
  </div>

  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0 table-wide">
      <thead>
        <tr>
          <th>Account</th>
          <th class="text-end">Balance</th>
          <th class="text-end">Type</th>
        </tr>
      </thead>
      <tbody>
        {% for row in balances %}
          <tr>
            <td>
              <span class="ellipsis d-inline-block">{{ row.name }}</span>
            </td>
            <td class="text-end money">£{{ "%.2f"|format(row.balance) }}</td>
            <td class="text-end">
              <span class="badge text-bg-light">{{ row.type }}</span>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

</div>

</div>
<nav class="navbar fixed-bottom bg-white border-top">
  <div class="container-fluid d-flex justify-content-around">

    <a href="#home" class="text-center text-decoration-none text-dark">
      <div style="font-size:20px">🏠</div>
      <small>Home</small>
    </a>

    <a href="#afford" class="text-center text-decoration-none text-dark">
      <div style="font-size:20px">🧠</div>
      <small>Afford</small>
    </a>

    <a href="#expense" class="text-center text-decoration-none text-dark">
      <div style="font-size:20px">➖</div>
      <small>Expense</small>
    </a>

    <a href="#income" class="text-center text-decoration-none text-dark">
      <div style="font-size:20px">➕</div>
      <small>Income</small>
    </a>

    <a href="#transfer" class="text-center text-decoration-none text-dark">
        <div style="font-size:20px">🔁</div>
        <small>Transfer</small>
    </a>

    <a href="{{ url_for('bills') }}" class="text-center text-decoration-none text-dark">
        <div style="font-size:20px">💳</div>
        <small>Bills</small>
    </a>

    <a href="#accounts" class="text-center text-decoration-none text-dark">
      <div style="font-size:20px">💼</div>
      <small>Accounts</small>
    </a>

  </div>
</nav>
</body>
</html>
"""

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

    return render_template_string(
        PAGE,
        message=request.args.get("msg", ""),
        accounts=active_accounts,
        overview=overview,
        balances=balances,
        monthly=monthly,
    )

@app.get("/bills")
def bills():
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

    balances = []
    for n in active_accounts:
        balances.append({
            "name": n,
            "balance": float(accounts[n].get("balance", 0.0)),
            "type": accounts[n].get("type", "")
        })

    return render_template_string(
        BILLS_PAGE,
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
        url_for("home", msg=f"Added {description}: £{abs(amount):.2f} from {account}")
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
        url_for("home", msg=f"Added income {description}: £{amount:.2f} to {account}")
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
        url_for("home", msg=f"Transferred £{amount:.2f} from {from_account} → {to_account}")
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

    return render_template_string(
        PAGE,
        accounts=[a for a in accounts if accounts[a]["active"]],
        balances=[{"name":a,"balance":accounts[a]["balance"],"type":accounts[a]["type"]} for a in accounts if accounts[a]["active"]],
        overview=calculate_financial_overview(accounts),
        afford_results=results,
        recommendation=recommendation
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)