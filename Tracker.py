import json
import csv
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"

RUNNING_AS_LIBRARY = __name__ != "__main__"

def _db_fetch(query, params=None):
    from database import get_db, USE_POSTGRES
    db = get_db()
    cursor = db.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    db.close()
    return rows

from datetime import date
if not RUNNING_AS_LIBRARY:
    today = date.today()
    current_day = today.day
    current_month = today.month
    current_year = today.year
from datetime import timedelta
import calendar

# --- UTILITY FUNCTION ---
def section(title):
    print("\n" + "=" * 60)
    print(title.upper())
    print("=" * 60)

def pause():
    input("\nPress Enter to return to menu...")
    print()
    print()

# --- CSV HEADER CHECK ---
def ensure_csv_header(file_path, header):
    if not file_path.exists() or file_path.stat().st_size == 0:
        with open(file_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)

# --- LOAD SCHEDULED EXPENSES DATA ---
def load_scheduled_expenses(data_dir=None):
    from database import get_db

    rows = _db_fetch("SELECT * FROM scheduled_expenses")

    scheduled_expenses = []
    for row in rows:
        scheduled_expenses.append({
            "name": row["name"],
            "amount": row["amount"],
            "day": row["day"],
            "account": row["account"]
        })

    return scheduled_expenses

# --- LOAD FUTURE EVENTS DATA ---
def load_future_events(data_dir=None):
    from database import get_db
    from datetime import date

    rows = _db_fetch("SELECT * FROM future_events")

    future_events = []
    for row in rows:
        try:
            future_events.append({
                "date": date.fromisoformat(row["date"]),
                "name": row["name"],
                "amount": row["amount"],
                "account": row["account"]
            })
        except:
            continue

    return future_events

# --- COUNT FUTURE FRIDAYS ---
def count_fridays_between(start_date, end_date):
    fridays = 0
    check_day = start_date

    while check_day <= end_date:
        if check_day.weekday() == 4: # Friday
            fridays += 1
        check_day += timedelta(days=1)

    return fridays

# --- ACCOUNT SELECTION FUNCTION ---
def choose_account(accounts):
    print("\nSelect an account:")

    account_names = [name for name in accounts if accounts[name].get("active", True)]

    for i, name in enumerate(account_names, start=1):
        balance = accounts[name]["balance"]
        print(f"{i}. {name} (Balance: £{balance:.2f})")

    while True:
        choice = input("Choice: ").strip()

        if not choice.isdigit():
            print("Please enter a number.")
            continue

        choice = int(choice)

        if 1 <= choice <= len(account_names):
            return account_names[choice - 1]
        
        print("Invalid selection.")

# --- EXPENSE-WRITING FUNCTION (CHANGE ACCOUNT BALANCES) ---
import os
import tempfile

def save_accounts(accounts, data_dir):
    file_path = data_dir / "Accounts.json"

    # write to temporary file first
    with tempfile.NamedTemporaryFile("w", delete=False, dir=data_dir) as tmp:
        json.dump(accounts, tmp, indent=4)
        temp_name = tmp.name

    # replace original file atomically
    os.replace(temp_name, file_path)
    
def update_account_balance(accounts, account_name, amount, data_dir):
    if account_name not in accounts:
        print(f"⚠ Account '{account_name}' does not exist.")
        return
    accounts[account_name]["balance"] += amount
    save_accounts(accounts, data_dir)

    print(f"✔ Updated {account_name} balance: £{accounts[account_name]['balance']:.2f}")
    print()
    print()  

# --- EXPENSE-WRITING FUNCTION ---
file_path = DATA_DIR / "Daily_Expenses.csv"
ensure_csv_header(file_path, ["date", "description", "amount", "account"])

def add_daily_expense(data_dir, accounts):
    print("\nAdd a new daily expense")

    description = input("Description: ").strip()

    while True:
        try:
            amount = float(input("Amount (£): "))
            break
        except ValueError:
            print("Please enter a valid number.")
        
    account = choose_account(accounts)

    today = date.today().isoformat()

    with open(file_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([today, description, -abs(amount), account])

    print("✔ Expense recorded")
    update_account_balance(accounts, account, -abs(amount), data_dir)

# --- INCOME-WRITING FUNCTION ---
def add_manual_income(data_dir, accounts):
    print("\nAdd a manual income entry")

    description = input("Description: ").strip()

    while True:
        try:
            amount = float(input("Amount (£): "))
            break
        except ValueError:
            print("Please enter a valid number.")

    account = choose_account(accounts)

    today = date.today().isoformat()

    with open(data_dir / "Payments_Log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([today, description, abs(amount), account, "manual_income"])

    print("✔ Income recorded")
    update_account_balance(accounts, account, abs(amount), data_dir)

# --- ADD FUTURE EVENT ---
def add_future_event(data_dir, accounts):
    print("\nAdd Future Event")

    name = input("Event name: ").strip()

    while True:
        date_input = input("Date (DD/MM/YYYY): ").strip()
        try:
            day, month, year = map(int, date_input.split("/"))
            event_date = date(year, month, day)
            break
        except:
            print("Invalid date format.")

    while True:
        try:
            amount = float(input("Amount (£): "))
            break
        except ValueError:
            print("Enter a valid number.")

    account = choose_account(accounts)

    with open(data_dir / "Future_Events.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([event_date.isoformat(), name, amount, account])

    print("✔ Future event added")
    pause()

# --- TRANSFER BETWEEN ACCOUNTS ---
def transfer_between_accounts(data_dir, accounts):
    print("\nTransfer Money Between Accounts")

    print("\nFROM account:")
    from_account = choose_account(accounts)

    print("\nTO account")
    to_account = choose_account(accounts)

    if from_account == to_account:
        print("Cannot transfer to the same account.")
        pause()
        return
    
    while True:
        try:
            amount = float(input("Amount (£): "))
            if amount <= 0:
                raise ValueError
            break
        except ValueError:
            print("Enter a valid positive number.")
    
    # subtract from source
    update_account_balance(accounts, from_account, -amount, data_dir)

    # add to destination
    update_account_balance(accounts, to_account, amount, data_dir)

    print(f"\n✔ Transferred £{amount:.2f} from {from_account} → {to_account}")
    pause()

# --- CREATE NEW ACCOUNT ---
def create_new_account(data_dir, accounts):
    section("Create New Account")

    base_name = input("Enter account name: ").strip()

    if not base_name:
        print("Account name cannot be empty.")
        pause()
        return
    
    # ensure unique name
    new_name = base_name
    counter = 2

    while new_name in accounts:
        new_name = f"{base_name}_{counter}"
        counter += 1

    print("\nSelect account type:")
    print("1 - current")
    print("2 - savings")
    print("3 - cash")

    type_choice = input("Choice: ").strip()

    if type_choice == "1":
        acc_type = "current"
    elif type_choice == "2":
        acc_type = "savings"
    elif type_choice == "3":
        acc_type = "cash"
    else:
        print("Invalid type.")
        pause()
        return

    # starting balance
    while True:
        try:
            balance = float(input("Starting balance (£): "))
            break
        except ValueError:
            print("Enter a valid number.")

    # create account
    accounts[new_name] = {
        "balance": balance,
        "type": acc_type,
        "active": True
    }

    save_accounts(accounts, data_dir)

    print(f"\n✔ Account '{new_name}' created successfully.")
    pause()

# --- DEACTIVATE ACCOUNT ---
def deactivate_account(data_dir, accounts):
    section("Deactivate Account")

    active_accounts = [name for name in accounts if accounts[name].get("active", True)]

    if not active_accounts:
        print("No active accounts available.")
        pause()
        return

    print("Select account to deactivate:")

    for i, name in enumerate(active_accounts, start=1):
        print(f"{i}. {name}")

    choice = input("Choice: ").strip()

    if not choice.isdigit():
        print("Invalid selection.")
        pause()
        return

    choice = int(choice)

    if not (1 <= choice <= len(active_accounts)):
        print("Invalid selection.")
        pause()
        return

    acc_name = active_accounts[choice - 1]

    confirm = input(f"Type YES to deactivate '{acc_name}': ")

    if confirm != "YES":
        print("Cancelled.")
        pause()
        return

    accounts[acc_name]["active"] = False
    save_accounts(accounts, data_dir)

    print(f"✔ Account '{acc_name}' has been deactivated.")
    pause()

# --- LOAD ACCOUNTS ---
with open(DATA_DIR / "Accounts.json", "r") as f:
    accounts = json.load(f)
    # ensure all accounts have active flag
    for acc in accounts:
        if "active" not in accounts[acc]:
            accounts[acc]["active"] = True
if not RUNNING_AS_LIBRARY:
    scheduled_expenses = load_scheduled_expenses(DATA_DIR)
    future_events = load_future_events(DATA_DIR)


def show_accounts(accounts):
    section("Accounts")
    for name, info in accounts.items():
        if not info.get("active", True):
            continue
        print(f"{name:<30}: £{info['balance']:>10.2f} ({info['type']})")
    print()
    print()
    pause()

# --- LOAD INCOME ---
def show_monthly_analysis(accounts, scheduled_expenses):
    section("Remaining Bills This Month")

    remaining_bills = []
    total_remaining = 0.0

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            remaining_bills.append(expense)
    if not remaining_bills:
        print("No more direct debits remaining this month.")
        return
    
    print("Upcoming Direct Debits:\n")

    for bill in sorted(remaining_bills, key=lambda x: x["day"]):
        print(f"Day {bill['day']:>2} {bill['name']:<20} £{bill['amount']:>7.2f} → {bill['account']}")
        total_remaining += bill["amount"]

    print("\n-----------------------------------------------------")
    print(f"Total Still To Leave Accounts: £{total_remaining:,.2f}")
    print()
    print()
    pause()

# --- MONTHLY SPENDING ---
def show_monthly_spending(accounts, scheduled_expenses):
    section("Monthly Spending Breakdown")

    # --- PART 1: SCHEDULED MONTHLY EXPENSES ---
    print("RECURRING MONTHLY BILLS\n")

    monthly_total = 0.0
    recurring_bills = []

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue

        recurring_bills.append(expense)
        monthly_total += expense["amount"]

    if recurring_bills:
        for bill in sorted(recurring_bills, key=lambda x: x["day"]):
            print(f"Day {bill['day']:>2}  {bill['name']:<20} £{bill['amount']:>8.2f} → {bill['account']}")
    else:
        print("No recurring bills found.")

    print("\n-----------------------------------------")
    print(f"Total Monthly Bills: £{monthly_total:,.2f}")

    # --- PART 2: REAL SPENDING THIS MONTH ---
    print("\n\nSPENDING SO FAR THIS MONTH\n")

    spending_total = 0.0
    expenses_this_month = []

    file_path = DATA_DIR / "Daily_Expenses.csv"
    ensure_csv_header(file_path, ["date","description","amount","account"])

    with open(file_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                exp_date = date.fromisoformat(row["date"])

                if exp_date.month == current_month and exp_date.year == current_year:
                    amount = float(row["amount"])
                    description = row["description"]
                    account = row["account"]

                    expenses_this_month.append((exp_date, description, amount, account))
                    spending_total += abs(amount)

            except:
                continue

    if expenses_this_month:
        for exp in sorted(expenses_this_month, key=lambda x: x[0]):
            print(f"{exp[0].day:>2}/{exp[0].month:>2}  {exp[1]:<20} £{abs(exp[2]):>8.2f} → {exp[3]}")
    else:
        print("No expenses logged this month.")

    print("\n-----------------------------------------")
    print(f"Total Spent This Month: £{spending_total:,.2f}")

    print("\n=========================================")
    print(f"Combined Outflow (Bills + Spending): £{(monthly_total + spending_total):,.2f}")
    print("=========================================")

    pause()

# --- ACCOUNT CASHFLOW FORECAST ---
def show_cashflow_forecast(accounts, scheduled_expenses):
    section("Account Cashflow Forecast")

    account_future_bills = {}
    for account_name in accounts:
        account_future_bills[account_name] = 0.0

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            account_future_bills[expense["account"]] += expense["amount"]

    for account_name, future_bill in account_future_bills.items():
        balance = accounts[account_name]["balance"]
        projected = balance - future_bill

        print(f"\n{account_name}")
        print(f" Current Balance : £{balance:,.2f}")
        print(f" Bills Remaining : £{future_bill:,.2f}")

        if projected < 0:
            print(f" ⚠ SHORTFALL : £{projected:,.2f}")
        else:
            print(f" Safe After Bills : £{projected:,.2f}")
    print()
    print()
    pause()

# --- FINANCIAL OVERVIEW ---
# define categories
def show_financial_overview(accounts, scheduled_expenses):
    spending_types = {"current", "cash"}
    savings_types = {"savings"}

    spending_balance = 0.0
    savings_balance = 0.0

    # split balances into spending vs savings
    for name, info in accounts.items():
        acc_type = info["type"]
        balance = info["balance"]

        if acc_type in spending_types:
            spending_balance += balance
        elif acc_type in savings_types:
            savings_balance += balance

    # calculate future bills locally for this overview
    account_future_bills = {}
    for account_name in accounts:
        account_future_bills[account_name] = 0.0

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            account_future_bills[expense["account"]] += expense["amount"]

    spending_future_bills = 0.0
    for account_name, future_bill in account_future_bills.items():
        if accounts[account_name]["type"] in spending_types:
            spending_future_bills += future_bill


    safe_spending = spending_balance - spending_future_bills
    net_worth = spending_balance + savings_balance

    print("SPENDING")
    print(f"  Available Now       : £{spending_balance:,.2f}")
    print(f"  Bills Remaining     : £{spending_future_bills:,.2f}")
    print(f"  Safe To Spend       : £{safe_spending:,.2f}")

    print("\nSAVINGS")
    print(f"  Protected Savings   : £{savings_balance:,.2f}")

    print("\nTOTAL")
    print(f"  Net Worth           : £{net_worth:,.2f}")

    print()
    print()
    pause()

# --- END OF MONTH PROJECTION ---
def show_month_projection(accounts, scheduled_expenses):
    month_name = calendar.month_name[current_month]
    section(f"End of Month Projection — {month_name} {current_year}")

    projected_accounts = {}
    for name, info in accounts.items():
        projected_accounts[name] = info["balance"]

    last_day = calendar.monthrange(current_year, current_month)[1]
    end_of_month = date(current_year, current_month, last_day)

    friday_count = 0
    check_day = today

    while check_day <= end_of_month:
        if check_day.weekday() == 4:
            friday_count += 1
        check_day += timedelta(days=1)

    from database import get_db
    db = get_db()
    income_rows = db.execute("SELECT * FROM income WHERE frequency = 'weekly'").fetchall()
    db.close()
    for row in income_rows:
        projected_accounts[row["account"]] += row["amount"] * friday_count

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            projected_accounts[expense["account"]] -= expense["amount"]

    print("\nProjected balances at end of month:\n")
    for name, balance in projected_accounts.items():
        print(f"{name:<30} £{balance:,.2f}")
    print()
    print()
    pause()

# --- SIMULATE BALANCES ---
def simulate_balances_until(target_date, accounts, scheduled_expenses, future_events):
    today = date.today()
    """
    Simulates balances from tomorrow up to target_date (inclusive),
    applying weekly income (Fridays), scheduled expenses, future events, and savings rules.
    Returns (final_balances_dict, lowest_balances_dict).
    """

    # working copy of balances for ACTIVE accounts only
    simulated = {}
    for name, info in accounts.items():
        if not info.get("active", True):
            continue
        simulated[name] = info["balance"]

    lowest = simulated.copy()

    # Load savings rules ONCE (not every day)
    savings_rules = _db_fetch("SELECT * FROM savings_rules")

    sim_day = today + timedelta(days=1)

    while sim_day <= target_date:

        # ---------- WEEKLY INCOME ----------
        if sim_day.weekday() == 4:  # Friday
            income_rows = _db_fetch("SELECT * FROM income WHERE frequency = 'weekly'")
            for row in income_rows:
                acc = row["account"]
                if acc in simulated:
                    simulated[acc] += row["amount"]

        # ---------- SCHEDULED EXPENSES ----------
        for expense in scheduled_expenses:
            if expense["day"] is None:
                continue
            if expense["day"] == sim_day.day:
                acc = expense["account"]
                if acc in simulated:
                    simulated[acc] -= expense["amount"]

        # ---------- FUTURE EVENTS ----------
        for event in future_events:
            if event["date"] == sim_day:
                acc = event["account"]
                if acc in simulated:
                    simulated[acc] -= event["amount"]

        # ---------- SAVINGS RULES ----------
        for rule in savings_rules:
            freq = rule.get("frequency", "monthly")
            apply_rule = False

            if freq == "monthly" and rule["day"] == sim_day.day:
                apply_rule = True
            elif freq == "weekly" and sim_day.weekday() == 4:  # Friday
                apply_rule = True
            elif freq == "daily":
                apply_rule = True

            if apply_rule:
                from_acc = rule["from_account"]
                to_acc = rule["to_account"]
                amt = rule["amount"]

                if from_acc in simulated and to_acc in simulated:
                    if simulated[from_acc] >= amt:
                        simulated[from_acc] -= amt
                        simulated[to_acc] += amt

        # track lowest balances
        for acc in simulated:
            if simulated[acc] < lowest[acc]:
                lowest[acc] = simulated[acc]

        sim_day += timedelta(days=1)

    return simulated, lowest

# --- CAN I AFFORD IT FUNCTION ---
def can_i_afford_purchase(accounts, scheduled_expenses, future_events):
    section("Can I afford this?")

    desc = input("What is it? (optional): ").strip()

    while True:
        try:
            amount = float(input("Purchase amount (£): "))
            if amount <= 0:
                raise ValueError
            break
        except ValueError:
            print("Enter a valid positive number.")

    # Horizon = end of next month (same idea as your forward outlook)
    if current_month == 12:
        next_month = 1
        next_year = current_year + 1
    else:
        next_month = current_month + 1
        next_year = current_year

    last_day_next_month = calendar.monthrange(next_year, next_month)[1]
    horizon_date = date(next_year, next_month, last_day_next_month)

    # Spending accounts only (active)
    spending_accounts = []
    for name, info in accounts.items():
        if not info.get("active", True):
            continue
        if info.get("type") in {"current", "cash"}:
            spending_accounts.append(name)

    if not spending_accounts:
        print("No active spending accounts (current/cash) found.")
        pause()
        return

    print(f"\nTesting purchase: £{amount:.2f}" + (f" — {desc}" if desc else ""))
    print(f"Horizon: {horizon_date.strftime('%d/%m/%Y')}\n")

    results = []

    for acc in spending_accounts:
        # Copy accounts balances (active only), then apply purchase immediately
        temp_accounts = {}
        for n, info in accounts.items():
            temp_accounts[n] = info.copy()
        temp_accounts[acc]["balance"] -= amount

        final_bal, lowest_bal = simulate_balances_until(horizon_date, temp_accounts, scheduled_expenses, future_events)

        balance_now = accounts[acc]["balance"]
        after_purchase = balance_now - amount
        lowest = lowest_bal.get(acc, after_purchase)
        goes_negative = lowest < 0

        results.append({
            "account": acc,
            "now": balance_now,
            "after": after_purchase,
            "lowest": lowest,
            "negative": goes_negative
        })

    # Print results nicely
    for r in results:
        print(f"{r['account']}")
        print(f"  Now           : £{r['now']:,.2f}")
        print(f"  After Purchase: £{r['after']:,.2f}")
        print(f"  Lowest Before {horizon_date.strftime('%d/%m/%Y')}: £{r['lowest']:,.2f}")

        if r["negative"]:
            print("  ⚠ Risk: goes NEGATIVE")
        else:
            print("  ✅ Safe: stays NON-negative")
        print()

    # Recommend safest account:
    # Prefer non-negative, then highest lowest-balance
    safe = [r for r in results if not r["negative"]]
    if safe:
        best = sorted(safe, key=lambda x: x["lowest"], reverse=True)[0]
        print(f"RECOMMENDATION: Use '{best['account']}' (best safety buffer).")
    else:
        worst = sorted(results, key=lambda x: x["lowest"], reverse=True)[0]
        print("⚠ No account stays safe for this purchase.")
        print(f"Least bad option: '{worst['account']}' (smallest predicted shortfall).")

    pause()

# --- LOAD SYSTEM STATE ---
def load_system_state(data_dir):
    path = data_dir / "System_State.json"
    if not path.exists():
        return {"last_processed": today.isoformat()}
    with open(path, "r") as f:
        return json.load(f)

def save_system_state(state, data_dir):
    path = data_dir / "System_State.json"
    with tempfile.NamedTemporaryFile("w", delete=False, dir=data_dir) as tmp:
        json.dump(state, tmp, indent=4)
        temp_name = tmp.name
    os.replace(temp_name, path)

# --- INITIALIZE SYSTEM STATE ---
def apply_day(sim_day, accounts, scheduled_expenses, future_events):
    """
    Applies all automatic movements that happen on sim_day to accounts in-place.
    """

    # ---------- WEEKLY INCOME ----------
    if sim_day.weekday() == 4:  # Friday
        with open(DATA_DIR / "Income.csv", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["frequency"].lower() == "weekly":
                    amt = float(row["amount"])
                    acc = row["account"]
                    if acc in accounts and accounts[acc].get("active", True):
                        accounts[acc]["balance"] += amt

    # ---------- SCHEDULED EXPENSES ----------
    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] == sim_day.day:
            acc = expense["account"]
            if acc in accounts and accounts[acc].get("active", True):
                accounts[acc]["balance"] -= expense["amount"]

    # ---------- FUTURE EVENTS ----------
    for event in future_events:
        if event["date"] == sim_day:
            acc = event["account"]
            if acc in accounts and accounts[acc].get("active", True):
                accounts[acc]["balance"] -= event["amount"]

    # ---------- SAVINGS RULES ----------
    savings_rules = _db_fetch("SELECT * FROM savings_rules")

    for rule in savings_rules:
        if rule["day"] == sim_day.day:
            amt = rule["amount"]
            from_acc = rule["from_account"]
            to_acc = rule["to_account"]

            if from_acc in accounts and to_acc in accounts:
                if accounts[from_acc]["balance"] >= amt:
                    accounts[from_acc]["balance"] -= amt
                    accounts[to_acc]["balance"] += amt

# --- AUTO ADVANCE ---
def auto_advance_to_today(data_dir, accounts, scheduled_expenses, future_events):
    """
    Applies all missed days from last_processed+1 up to today (inclusive).
    Updates Accounts.json and System_State.json.
    Prints a message if any days were applied.
    """

    state = load_system_state(data_dir)

    try:
        last_processed = date.fromisoformat(state.get("last_processed", today.isoformat()))
    except:
        last_processed = today

    if last_processed >= today:
        return  # nothing to do

    sim_day = last_processed + timedelta(days=1)
    days_applied = 0

    while sim_day <= today:
        apply_day(sim_day, accounts, scheduled_expenses, future_events)
        days_applied += 1
        sim_day += timedelta(days=1)

    # persist balances + new last_processed
    save_accounts(accounts, data_dir)
    state["last_processed"] = today.isoformat()
    save_system_state(state, data_dir)

    # message
    print(f"\n✔ Auto-applied {days_applied} day(s) of activity up to {today.strftime('%d/%m/%Y')}/\n")

# --- PREDICT DATE BALANCES ---
def predict_date_balances(accounts, scheduled_expenses):
    section("Balance Predictor")

    user_input = input("Enter a date (DD/MM/YYYY): ").strip()

    try:
        day, month, year = map(int, user_input.split("/"))
        target_date = date(year, month, day)
    except ValueError:
        print("Invalid date format. Use DD/MM/YYYY")
        return

    if target_date <= today:
        print("Date must be in the future.")
        return

    # make a working copy of balances
    simulated_accounts = {}
    for name, info in accounts.items():
        simulated_accounts[name] = info["balance"]
    lowest_balances = simulated_accounts.copy()

    sim_day = today + timedelta(days=1)

    while sim_day <= target_date:

        # ---------- WEEKLY INCOME ----------
        if sim_day.weekday() == 4:  # Friday
            with open(DATA_DIR / "Income.csv", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["frequency"].lower() == "weekly":
                        amt = float(row["amount"])
                        acc = row["account"]
                        simulated_accounts[acc] += amt

        # ---------- SCHEDULED EXPENSES ----------
        for expense in scheduled_expenses:
            if expense["day"] is None:
                continue

            if expense["day"] == sim_day.day:
                simulated_accounts[expense["account"]] -= expense["amount"]
        
        # ---------- FUTURE EVENTS ----------
        for event in future_events:
            if event["date"] == sim_day:
                simulated_accounts[event["account"]] -= event["amount"]

        # ---------- SAVINGS RULES ----------
        with open(DATA_DIR / "Savings_Rules.csv", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row["day"]) == sim_day.day:
                    amt = float(row["amount"])
                    from_acc = row["from_account"]
                    to_acc = row["to_account"]

                    if simulated_accounts[from_acc] >= amt:
                        simulated_accounts[from_acc] -= amt
                        simulated_accounts[to_acc] += amt
        
        # track lowest balance reached
        for acc in simulated_accounts:
            if simulated_accounts[acc] < lowest_balances[acc]:
                lowest_balances[acc] = simulated_accounts[acc]

        sim_day += timedelta(days=1)

    # ----- OUTPUT -----
    section(f"Balances on {target_date.isoformat()}")

    for name, balance in simulated_accounts.items():
        print(f"{name:<30} £{balance:,.2f}")
    print("\nLowest balance reached before this date:\n")

    risk_detected = False

    for acc, bal in lowest_balances.items():
        print(f"{acc:<30} £{bal:,.2f}")
        if bal < 0:
            risk_detected = True
    if risk_detected:
        print("\n⚠ WARNING: Negative balance predicted before this date!")
    else:
        print("\n✅ No negative balances predicted before this date.")
    print()
    print()

def main():
    auto_advance_to_today(DATA_DIR, accounts, scheduled_expenses, future_events)

    # --- Prompt ---
    while True:
        print("1 - Accounts Overview")
        print("2 - Financial Overview")
        print("3 - Cashflow Forecast")
        print("4 - End-of-Month Projection")
        print("5 - Monthly Bills Analysis")
        print("6 - Add Expense")
        print("7 - Add Income")
        print("8 - Transfer Between Accounts")
        print("9 - Predict Date Balances")
        print("10 - Create New Account")
        print("11 - Deactivate Account")
        print("12 - Add Future Event")
        print("13 - Monthly Spending Breakdown")
        print("14 - Can I Afford This?")
        print("15 - Exit")

        choice = input("Select option: ").strip()

        print()
        print()

        if choice == "1":
            show_accounts(accounts)

        elif choice == "2":
            show_financial_overview(accounts, scheduled_expenses)

        elif choice == "3":
            show_cashflow_forecast(accounts, scheduled_expenses)

        elif choice == "4":
            show_month_projection(accounts, scheduled_expenses)

        elif choice == "5":
            show_monthly_analysis(accounts, scheduled_expenses)

        elif choice == "6":
            add_daily_expense(DATA_DIR, accounts)

        elif choice == "7":
            add_manual_income(DATA_DIR, accounts)

        elif choice == "8":
            transfer_between_accounts(DATA_DIR, accounts)   

        elif choice == "9":
            predict_date_balances(accounts, scheduled_expenses) 
        
        elif choice == "10":
            create_new_account(DATA_DIR, accounts)

        elif choice == "11":
            deactivate_account(DATA_DIR, accounts)

        elif choice == "12":
            add_future_event(DATA_DIR, accounts)

        elif choice == "13":
            show_monthly_spending(accounts, scheduled_expenses)

        elif choice == "14":
            can_i_afford_purchase(accounts, scheduled_expenses, future_events)

        elif choice == "15":
            break

if __name__ == "__main__":
    main()