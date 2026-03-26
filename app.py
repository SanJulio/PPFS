from __future__ import annotations

# --- IMPORTS ---
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# simulate_balances_until is used for forecast and "can I afford it" features
from Tracker import simulate_balances_until, load_future_events, load_scheduled_expenses
import calendar
from datetime import timedelta

from models import (
    add_transaction,
    update_account_balance,
    get_active_accounts,
    get_recent_transactions
)

from database import get_db, release_db

from database import USE_POSTGRES

from functools import lru_cache
import hashlib
# In-memory cache for the 90-day forecast — expensive to compute so we cache for 5 minutes
forecast_cache = {}
FORECAST_CACHE_TTL = 300  # 5 minutes in seconds

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "Data"

# --- CUSTOM SESSION BACKEND ---
# Flask's default sessions use signed cookies. We use Postgres instead so sessions
# survive server restarts and work correctly on Render's single-worker setup.
# Each session is stored as a JSON row in the flask_sessions table, keyed by a UUID cookie.
class PostgresSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None):
        super().__init__(initial or {})
        self.sid = sid
        self.modified = False

class PostgresSessionInterface(SessionInterface):
    def _get_db(self):
        import psycopg2
        return psycopg2.connect(os.environ.get("DATABASE_URL"))

    def _release_db(self, conn):
        try:
            conn.close()
        except Exception as e:
            logger.debug(f"Error closing session DB connection: {e}")

    def open_session(self, app, request):
        sid = request.cookies.get("session")
        if sid and os.environ.get("DATABASE_URL"):
            try:
                db = self._get_db()
                cur = db.cursor()
                cur.execute("SELECT data FROM flask_sessions WHERE sid = %s", (sid,))
                row = cur.fetchone()
                cur.close()
                self._release_db(db)
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
            self._release_db(db)
        except Exception as e:
            print(f">>> Session save error: {e}", flush=True)
        response.set_cookie("session", sid, httponly=True, secure=True, samesite="Lax")

# --- FLASK APP SETUP ---
app = Flask(__name__)

import secrets

# Generate a CSRF token for every new session — embedded as a hidden field in all forms
@app.before_request
def set_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)

# Validate the CSRF token on every POST request (except login/register which don't have it yet)
@app.before_request
def check_csrf():
    if request.method == 'POST':
        exempt = ['/login', '/register']
        if request.path not in exempt:
            token = request.form.get('csrf_token')
            if not token or token != session.get('csrf_token'):
                return 'CSRF token invalid', 403

# Rate limiter — limits are applied per-route (e.g. login, register, password reset)
# Stored in memory (not Redis) which is fine for a single-worker deployment
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://"
)

# Use our custom Postgres session backend instead of signed cookies
app.session_interface = PostgresSessionInterface()

# SECRET_KEY must be set as an env var — used to sign cookies
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise ValueError("SECRET_KEY environment variable must be set for production security")
app.secret_key = secret_key
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

# Trust X-Forwarded-Proto and X-Forwarded-Host headers from Render's reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Flask-Login setup — redirects unauthenticated users to /login by default
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.unauthorized_handler
def unauthorized():
    """Handle unauthorized requests - show landing page for root path, login for others"""
    if request.path == "/":
        return render_template("landing.html"), 200
    return redirect(url_for("login"))

# --- USER MODEL ---
# Minimal User class required by Flask-Login — just stores id and email
class User(UserMixin):
    def __init__(self, id, email):
        self.id = id
        self.email = email

# Tells Flask-Login how to reload a user from their ID stored in the session
@login_manager.user_loader
def load_user(user_id):
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
    release_db(db)
    if row:
        row = dict(zip(cols, row))
        return User(row["id"], row["email"])
    return None

# Run database migrations on startup — creates tables and adds any missing columns
from database import init_db
try:
    with app.app_context():
        init_db()
except Exception as e:
    print(f">>> init_db FAILED: {e}", flush=True)

import time

# --- SECURITY HEADERS ---
# Added to every response: disables caching (so logged-out users can't go back),
# prevents clickjacking (DENY), and sets a strict Content-Security-Policy
@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.plot.ly; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none';"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

import calendar
from datetime import datetime

# --- HELPER FUNCTIONS ---

# Loads scheduled bills for the current logged-in user (used in financial overview calculation)
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
    release_db(db)
    return rows

# Same as above but ordered by day — used on the Flow page to show upcoming bills
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
    release_db(db)
    return rows

# --- EMAIL SENDING ---
# Sends a verification email to a newly registered user via the Brevo API
# Token is a random URL-safe string stored on the user row and cleared after use
def send_verification_email(to_email, token):
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.environ.get('BREVO_API_KEY')

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    verify_url = f"https://spendara.co.uk/verify-email/{token}"

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to_email}],
        sender={"email": "noreply@spendara.co.uk", "name": "Spendara"},
        subject="Verify your Spendara account",
        html_content=f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
            <h2 style="color: #111;">Welcome to Spendara! 👋</h2>
            <p>Thanks for signing up. Please verify your email address to get started.</p>
            <a href="{verify_url}" style="display:inline-block; background:#111; color:#fff; padding:12px 24px; border-radius:12px; text-decoration:none; font-weight:bold;">
                Verify Email
            </a>
            <p style="color:#999; font-size:12px; margin-top:24px;">If you didn't sign up for Spendara, you can ignore this email.</p>
        </div>
        """
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        return True
    except ApiException as e:
        print(f">>> Email error: {e}", flush=True)
        return False

# Sends a password reset link via Brevo — link expires after 24 hours
def send_reset_email(to_email, reset_url):
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.environ.get('BREVO_API_KEY')

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to_email}],
        sender={"email": "noreply@spendara.co.uk", "name": "Spendara"},
        subject="Reset your Spendara password",
        html_content=f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
            <h2 style="color: #111;">Reset your password 🔑</h2>
            <p>We received a request to reset your Spendara password. Click below to choose a new one.</p>
            <a href="{reset_url}" style="display:inline-block; background:#111; color:#fff; padding:12px 24px; border-radius:12px; text-decoration:none; font-weight:bold;">
                Reset Password
            </a>
            <p style="color:#999; font-size:12px; margin-top:24px;">If you didn't request this, you can safely ignore this email.</p>
        </div>
        """
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        return True
    except ApiException as e:
        print(f">>> Reset email error: {e}", flush=True)
        return False

# --- INPUT VALIDATION HELPERS ---
# Returns (value, None) on success or (None, error_message) on failure
# Used before inserting amounts and days into the database

def validate_amount(amount_raw):
    try:
        amount = float(amount_raw)
        if amount <= 0:
            return None, "Amount must be a positive number."
        return amount, None
    except (ValueError, TypeError):
        return None, "Amount must be a valid number."

def validate_day(day_raw):
    try:
        day = int(day_raw)
        if day < 1 or day > 31:
            return None, "Day must be between 1 and 31."
        return day, None
    except (ValueError, TypeError):
        return None, "Day must be a valid number."

# --- FINANCIAL OVERVIEW CALCULATION ---
# Splits accounts into spending (current/cash) and savings, then calculates:
# - spending balance (total in spending accounts)
# - future bills (bills still to leave this month)
# - safe to spend (spending balance minus future bills)
# - savings balance
# - net worth (spending + savings)
def calculate_financial_overview(accounts):
    today = datetime.today()
    current_day = today.day

    scheduled_expenses = load_scheduled_expenses_web()

    spending_types = {"current", "cash"}
    savings_types = {"savings"}

    spending_balance = 0.0
    savings_balance = 0.0
    spending_accounts = []
    savings_accounts = []

    for name, info in accounts.items():
        if not info.get("active", True):
            continue
        if not info.get("include_in_overview", 1):
            continue
        acc_type = info.get("type")
        balance = float(info.get("balance", 0.0))
        if acc_type in spending_types:
            spending_balance += balance
            spending_accounts.append({"name": name, "balance": balance})
        elif acc_type in savings_types:
            savings_balance += balance
            savings_accounts.append({"name": name, "balance": balance})

    spending_future_bills = 0.0
    future_bills_list = []

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        if expense["day"] > current_day:
            acc = expense["account"]
            if acc in accounts and accounts[acc]["type"] in spending_types:
                spending_future_bills += expense["amount"]
                future_bills_list.append({
                    "name": expense["name"],
                    "amount": expense["amount"],
                    "day": expense["day"],
                    "account": expense["account"]
                })

    safe_spending = spending_balance - spending_future_bills
    net_worth = spending_balance + savings_balance

    return {
        "spending_balance": spending_balance,
        "future_bills": spending_future_bills,
        "safe_spending": safe_spending,
        "savings_balance": savings_balance,
        "net_worth": net_worth,
        "spending_accounts": sorted(spending_accounts, key=lambda x: x["name"].lower()),
        "savings_accounts": sorted(savings_accounts, key=lambda x: x["name"].lower()),
        "future_bills_list": sorted(future_bills_list, key=lambda x: x["day"]),
    }

# --- MONTHLY SPENDING CALCULATION ---
# Queries all negative (outgoing) transactions this month for the current user
# Splits them into normal spending vs bills (type='bill'), returns totals and line items
def calculate_monthly_spending():
    today = date.today()
    year = today.year
    month = today.month

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT amount, type, description, date, account FROM transactions
            WHERE EXTRACT(YEAR FROM date::date) = %s
            AND EXTRACT(MONTH FROM date::date) = %s
            AND user_id = %s
            AND amount < 0
            AND type != 'transfer'
            """,
            (year, month, current_user.id)
        )
    else:
        cursor.execute(
            """
            SELECT amount, type, description, date, account FROM transactions
            WHERE strftime('%Y', date) = ?
            AND strftime('%m', date) = ?
            AND user_id = ?
            AND amount < 0
            AND type != 'transfer'
            """,
            (str(year), f"{month:02d}", current_user.id)
        )

    rows = cursor.fetchall()
    cursor.close()
    release_db(db)

    normal = 0.0
    scheduled = 0.0
    normal_list = []
    bills_list = []

    for r in rows:
        if USE_POSTGRES:
            amount = abs(r[0])
            tx_type = r[1]
            description = r[2]
            tx_date = r[3]
            account = r[4]
        else:
            amount = abs(r["amount"])
            tx_type = r["type"]
            description = r["description"]
            tx_date = r["date"]
            account = r["account"]

        if tx_type == "bill":
            scheduled += amount
            bills_list.append({"description": description, "amount": amount, "date": tx_date, "account": account})
        else:
            normal += amount
            normal_list.append({"description": description, "amount": amount, "date": tx_date, "account": account})

    return {
        "normal": normal,
        "scheduled": scheduled,
        "total": normal + scheduled,
        "normal_list": normal_list,
        "bills_list": bills_list,
    }

# =============================================================================
# ROUTES
# =============================================================================

# --- HOME / DASHBOARD ---
# Shows the main dashboard: financial overview, account balances, monthly spending
# If the user has no accounts yet, triggers the onboarding modal
@app.get("/")
@login_required
def home():
    # Dashboard for authenticated users
    # Check email verification
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT verified FROM users WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT verified FROM users WHERE id = ?", (current_user.id,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    verified = bool(row[0] if USE_POSTGRES else row["verified"]) if row else False

    accounts_rows = get_active_accounts(current_user.id)
    accounts = {}

    for r in accounts_rows:
        accounts[r["name"]] = {
            "id": r["id"],
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"]),
            "include_in_overview": bool(r.get("include_in_overview", 1))
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
            "type": accounts[n].get("type", ""),
            "id": accounts[n].get("id"),
            "include_in_overview": accounts[n].get("include_in_overview", True)
        })

    # Check if user has no accounts (show onboarding)
    show_onboarding = len(active_accounts) == 0

    return render_template(
        "index.html",
        message=request.args.get("msg", ""),
        accounts=active_accounts,
        overview=overview,
        balances=balances,
        monthly=monthly,
        show_onboarding=show_onboarding,
        verified=verified,
    )

# --- TRANSACTIONS PAGE ---
# Lists all transactions for the current user, newest first
@app.get("/transactions")
@login_required
def transactions():

    tx = get_recent_transactions(current_user.id)

    return render_template(
        "transactions.html",
        transactions=tx
    )

# --- ACTIONS PAGE ---
# Shows forms to add expenses, income, transfers, and investment updates
@app.get("/actions")
@login_required
def actions():
    accounts_rows = get_active_accounts(current_user.id)
    accounts = [r["name"] for r in accounts_rows]

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM investments WHERE user_id = %s ORDER BY name", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM investments WHERE user_id = ? ORDER BY name", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    investments = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    release_db(db)

    return render_template("actions.html", accounts=accounts, investments=investments, message=request.args.get("msg", ""))

# --- FLOW PAGE ---
# Shows each account's monthly cash flow: bills paid, bills still to pay,
# income received, income still to receive, and a projected end-of-month balance
# Traffic light colour: green (safe), amber (<£100), red (goes negative)
@app.get("/flow")
@login_required
def flow():
    from datetime import date as date_type
    today = date_type.today()
    year = today.year
    month = today.month
    current_day = today.day

    db = get_db()
    cursor = db.cursor()

    # get income
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM income WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    income = [dict(zip(cols, row)) for row in cursor.fetchall()]

    # get bills paid this month
    if USE_POSTGRES:
        cursor.execute("""
            SELECT description, amount, account FROM transactions
            WHERE user_id = %s AND type = 'bill'
            AND EXTRACT(YEAR FROM date::date) = %s
            AND EXTRACT(MONTH FROM date::date) = %s
        """, (current_user.id, year, month))
    else:
        cursor.execute("""
            SELECT description, amount, account FROM transactions
            WHERE user_id = ? AND type = 'bill'
            AND strftime('%Y', date) = ?
            AND strftime('%m', date) = ?
        """, (current_user.id, str(year), f"{month:02d}"))
    cols = [d[0] for d in cursor.description]
    bills_paid_this_month = [dict(zip(cols, row)) for row in cursor.fetchall()]

    # get income received this month
    if USE_POSTGRES:
        cursor.execute("""
            SELECT description, amount, account FROM transactions
            WHERE user_id = %s AND type = 'income'
            AND EXTRACT(YEAR FROM date::date) = %s
            AND EXTRACT(MONTH FROM date::date) = %s
        """, (current_user.id, year, month))
    else:
        cursor.execute("""
            SELECT description, amount, account FROM transactions
            WHERE user_id = ? AND type = 'income'
            AND strftime('%Y', date) = ?
            AND strftime('%m', date) = ?
        """, (current_user.id, str(year), f"{month:02d}"))
    cols = [d[0] for d in cursor.description]
    income_received_this_month = [dict(zip(cols, row)) for row in cursor.fetchall()]

    cursor.close()
    release_db(db)

    bills = get_all_scheduled_expenses()
    accounts_rows = get_active_accounts(current_user.id)

    # build account data
    account_data = []
    for acc in accounts_rows:
        acc_name = acc["name"]

        # bills paid from this account this month
        acc_bills_paid = [b for b in bills_paid_this_month if b["account"] == acc_name]

        # bills still to pay from this account this month
        acc_bills_to_pay = [b for b in bills if b["account"] == acc_name and b["day"] > current_day]

        # income received to this account this month
        acc_income_received = [i for i in income_received_this_month if i["account"] == acc_name]

        # income still to receive to this account this month
        acc_income_to_receive = [i for i in income if i["account"] == acc_name]
        # remove ones already received this month
        received_names = [i["description"] for i in acc_income_received]
        acc_income_to_receive = [i for i in acc_income_to_receive if i["name"] not in received_names]

        # projected end of month balance
        bills_still_out = sum(b["amount"] for b in acc_bills_to_pay)
        income_still_in = sum(i["amount"] for i in acc_income_to_receive)
        projected = acc["balance"] - bills_still_out + income_still_in

        # traffic light
        if projected < 0:
            traffic = "red"
        elif projected < 100:
            traffic = "amber"
        else:
            traffic = "green"

        account_data.append({
            "id": acc["id"],
            "name": acc_name,
            "balance": acc["balance"],
            "type": acc["type"],
            "bills_paid": acc_bills_paid,
            "bills_to_pay": acc_bills_to_pay,
            "income_received": acc_income_received,
            "income_to_receive": acc_income_to_receive,
            "projected": projected,
            "traffic": traffic,
        })

    # get investments with their updates
    db2 = get_db()
    cursor2 = db2.cursor()
    if USE_POSTGRES:
        cursor2.execute("SELECT * FROM investments WHERE user_id = %s ORDER BY name", (current_user.id,))
    else:
        cursor2.execute("SELECT * FROM investments WHERE user_id = ? ORDER BY name", (current_user.id,))
    cols2 = [d[0] for d in cursor2.description]
    investments_raw = [dict(zip(cols2, row)) for row in cursor2.fetchall()]

    investments = []
    for inv in investments_raw:
        if USE_POSTGRES:
            cursor2.execute("SELECT * FROM investment_updates WHERE investment_id = %s AND user_id = %s ORDER BY date ASC",
                           (inv["id"], current_user.id))
        else:
            cursor2.execute("SELECT * FROM investment_updates WHERE investment_id = ? AND user_id = ? ORDER BY date ASC",
                           (inv["id"], current_user.id))
        cols3 = [d[0] for d in cursor2.description]
        updates = [dict(zip(cols3, row)) for row in cursor2.fetchall()]

        current_value = updates[-1]["value"] if updates else inv["initial_amount"]
        gain = current_value - inv["initial_amount"]
        gain_pct = (gain / inv["initial_amount"] * 100) if inv["initial_amount"] else 0

        investments.append({
            "id": inv["id"],
            "name": inv["name"],
            "type": inv["type"],
            "initial_amount": inv["initial_amount"],
            "date": inv["date"],
            "current_value": current_value,
            "gain": gain,
            "gain_pct": gain_pct,
            "updates": updates,
        })

    cursor2.close()
    db2.close()

    return render_template(
        "flow.html",
        bills=bills,
        income=income,
        account_data=account_data,
        investments=investments,
        message=request.args.get("msg", ""),
    )

# --- PAY BILL (manual) ---
# Marks a scheduled bill as paid: logs a transaction and deducts from account balance
@app.post("/flow/pay-bill")
@login_required
def bills_pay():
    bill_id = request.form.get("bill_id")

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE id = %s AND user_id = %s", (bill_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE id = ? AND user_id = ?", (bill_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if not row:
        return redirect(url_for("bills", msg="Bill not found."))

    bill = dict(zip(cols, row))
    today_str = date.today().isoformat()

    add_transaction(today_str, bill["name"], -bill["amount"], bill["account"], current_user.id, type="bill")
    update_account_balance(bill["account"], -bill["amount"], current_user.id)

    return redirect(url_for("flow", msg=f"{bill['name']} — £{bill['amount']:.2f} paid."))

# --- RECEIVE INCOME (manual) ---
# Marks an income source as received: logs a transaction and adds to account balance
@app.post("/flow/pay-income")
@login_required
def income_pay():
    income_id = request.form.get("income_id")

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE id = %s AND user_id = %s", (income_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM income WHERE id = ? AND user_id = ?", (income_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if not row:
        return redirect(url_for("flow", msg="Income not found."))

    income = dict(zip(cols, row))
    today_str = date.today().isoformat()

    add_transaction(today_str, income["name"], income["amount"], income["account"], current_user.id, type="income")
    update_account_balance(income["account"], income["amount"], current_user.id)

    return redirect(url_for("flow", msg=f"{income['name']} — £{income['amount']:.2f} received."))

# --- ADD EXPENSE ---
# Records a manual expense: negative amount stored in transactions, balance deducted
@app.post("/add-expense")
@login_required
def add_expense():

    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()
    category = (request.form.get("category") or "Other").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    amount, err = validate_amount(amount_raw)
    if err:
        return redirect(url_for("actions", msg=err))

    amount = -abs(amount)

    today_str = date.today().isoformat()

    add_transaction(today_str, description, amount, account, current_user.id, category=category)

    update_account_balance(account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Added {description}: £{abs(amount):.2f} from {account}")
    )

# --- ADD INCOME ---
# Records a manual income entry: positive amount stored in transactions, balance increased
@app.post("/add-income")
@login_required
def add_income():

    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    amount, err = validate_amount(amount_raw)
    if err:
        return redirect(url_for("actions", msg=err))

    amount = abs(amount)

    today_str = date.today().isoformat()

    add_transaction(today_str, description, amount, account, current_user.id)

    update_account_balance(account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Added income {description}: £{amount:.2f} to {account}")
    )

# --- TRANSFER BETWEEN ACCOUNTS ---
# Moves money from one account to another: logs two transactions (out + in) and updates both balances
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

    amount, err = validate_amount(amount_raw)
    if err:
        return redirect(url_for("actions", msg=err))

    today_str = date.today().isoformat()

    add_transaction(today_str, f"Transfer to {to_account}", -amount, from_account, current_user.id, type="transfer")
    add_transaction(today_str, f"Transfer from {from_account}", amount, to_account, current_user.id, type="transfer")

    update_account_balance(from_account, -amount, current_user.id)
    update_account_balance(to_account, amount, current_user.id)

    return redirect(
        url_for("actions", msg=f"Transferred £{amount:.2f} from {from_account} → {to_account}")
    )

# --- UNDO TRANSACTION ---
# Reverses a transaction: re-adds the amount back to the account balance, then deletes the row
@app.post("/transactions/undo")
@login_required
def transaction_undo():
    tx_id = request.form.get("tx_id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM transactions WHERE id = %s AND user_id = %s", (tx_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ?", (tx_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if not row:
        return redirect(url_for("transactions", msg="Transaction not found."))

    tx = dict(zip(cols, row))

    update_account_balance(tx["account"], -tx["amount"], current_user.id)

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tx_id, current_user.id))
    else:
        cursor.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)

    return redirect(url_for("transactions", msg="Transaction reversed."))


# --- EDIT TRANSACTION ---
# Updates a transaction's description, amount, and account
# Calculates the diff between old and new amount and adjusts the account balance accordingly
@app.post("/transactions/edit")
@login_required
def transaction_edit():
    tx_id = request.form.get("tx_id")
    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("transactions", msg="Missing fields."))

    try:
        new_amount = float(amount_raw)
    except ValueError:
        return redirect(url_for("transactions", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM transactions WHERE id = %s AND user_id = %s", (tx_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ?", (tx_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if not row:
        return redirect(url_for("transactions", msg="Transaction not found."))

    tx = dict(zip(cols, row))
    diff = new_amount - tx["amount"]

    update_account_balance(tx["account"], diff, current_user.id)

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE transactions SET description=%s, amount=%s, account=%s WHERE id=%s AND user_id=%s",
                       (description, new_amount, account, tx_id, current_user.id))
    else:
        cursor.execute("UPDATE transactions SET description=?, amount=?, account=? WHERE id=? AND user_id=?",
                       (description, new_amount, account, tx_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)

    return redirect(url_for("transactions", msg="Transaction updated."))

# --- TOGGLE ACCOUNT IN OVERVIEW ---
# Flips include_in_overview between 0 and 1 for an account
# Lets users hide investment or secondary accounts from the main dashboard totals
@app.post("/toggle-account-overview")
@login_required
def toggle_account_overview():
    account_id = request.form.get("account_id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT include_in_overview FROM accounts WHERE id = %s AND user_id = %s", (account_id, current_user.id))
    else:
        cursor.execute("SELECT include_in_overview FROM accounts WHERE id = ? AND user_id = ?", (account_id, current_user.id))
    row = cursor.fetchone()
    if row:
        current_val = row[0] if USE_POSTGRES else row["include_in_overview"]
        new_val = 0 if current_val else 1
        if USE_POSTGRES:
            cursor.execute("UPDATE accounts SET include_in_overview = %s WHERE id = %s AND user_id = %s", (new_val, account_id, current_user.id))
        else:
            cursor.execute("UPDATE accounts SET include_in_overview = ? WHERE id = ? AND user_id = ?", (new_val, account_id, current_user.id))
        db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("home"))

# --- CAN I AFFORD IT ---
# Simulates the impact of a purchase on each spending account over the next ~2 months
# Uses simulate_balances_until to check if any account goes negative during that period
# Returns a recommendation for the safest account to use
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
    release_db(db)

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
        except (ValueError, KeyError) as ex:
            logger.debug(f"Invalid future event data: {e}, error: {ex}")
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

# --- SETTINGS PAGE ---
# Loads all user data for the 5-tab settings page:
# Accounts, Bills, Income, Savings Rules, Investments (+ Danger zone)
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
    investments = fetch_filtered("SELECT * FROM investments WHERE user_id = %s ORDER BY date DESC" if USE_POSTGRES else "SELECT * FROM investments WHERE user_id = ? ORDER BY date DESC", (uid,))

    cursor.close()
    release_db(db)

    is_pro = user_is_pro()

    return render_template("settings.html",
        accounts=accounts,
        bills=bills,
        savings_rules=savings_rules,
        future_events=future_events,
        income=income,
        investments=investments,
        is_pro=is_pro,
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

    # Free tier limit: max 3 accounts
    # Check current account count before inserting
    if not user_is_pro():
        existing = get_active_accounts(current_user.id)
        if len(existing) >= 3:
            return redirect(url_for("settings", msg="FREE_LIMIT_ACCOUNTS"))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id) VALUES (%s, %s, %s, 1, %s)", (name, balance, acc_type, current_user.id))
    else:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id) VALUES (?, ?, ?, 1, ?)", (name, balance, acc_type, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    release_db(db)
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
    release_db(db)
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
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("settings", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("settings", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id) VALUES (%s, %s, %s, %s, %s)", (name, amount, day, account, current_user.id))
    else:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id) VALUES (?, ?, ?, ?, ?)", (name, amount, day, account, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("settings", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("settings", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE scheduled_expenses SET name=%s, amount=%s, day=%s, account=%s WHERE id=%s AND user_id=%s", (name, amount, day, account, bill_id, current_user.id))
    else:
        cursor.execute("UPDATE scheduled_expenses SET name=?, amount=?, day=?, account=? WHERE id=? AND user_id=?", (name, amount, day, account, bill_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    release_db(db)
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
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("settings", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("settings", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    else:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("settings", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("settings", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE savings_rules SET name=%s, amount=%s, day=%s, frequency=%s, from_account=%s, to_account=%s WHERE id=%s AND user_id=%s", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    else:
        cursor.execute("UPDATE savings_rules SET name=?, amount=?, day=?, frequency=?, from_account=?, to_account=? WHERE id=? AND user_id=?", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    release_db(db)
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
    release_db(db)
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
    release_db(db)
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
    release_db(db)
    return redirect(url_for("settings", msg=f"Income source '{name}' added."))

@app.post("/settings/edit-income")
@login_required
def settings_edit_income():
    income_id = request.form.get("id")
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
        cursor.execute("UPDATE income SET name=%s, amount=%s, frequency=%s, account=%s WHERE id=%s AND user_id=%s",
                       (name, amount, frequency, account, income_id, current_user.id))
    else:
        cursor.execute("UPDATE income SET name=?, amount=?, frequency=?, account=? WHERE id=? AND user_id=?",
                       (name, amount, frequency, account, income_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
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
    release_db(db)
    return redirect(url_for("settings", msg="Income source deleted."))

@app.post("/settings/add-investment")
@login_required
def settings_add_investment():
    name = (request.form.get("name") or "").strip()
    inv_type = (request.form.get("type") or "").strip()
    initial_amount = (request.form.get("initial_amount") or "").strip()
    inv_date = (request.form.get("date") or "").strip()

    if not name or not inv_type or not initial_amount or not inv_date:
        return redirect(url_for("settings", msg="Missing fields."))
    try:
        initial_amount = float(initial_amount)
    except ValueError:
        return redirect(url_for("settings", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO investments (user_id, name, type, initial_amount, date) VALUES (%s, %s, %s, %s, %s)",
                       (current_user.id, name, inv_type, initial_amount, inv_date))
    else:
        cursor.execute("INSERT INTO investments (user_id, name, type, initial_amount, date) VALUES (?, ?, ?, ?, ?)",
                       (current_user.id, name, inv_type, initial_amount, inv_date))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg=f"Investment '{name}' added."))


@app.post("/settings/delete-investment")
@login_required
def settings_delete_investment():
    inv_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM investments WHERE id = %s AND user_id = %s", (inv_id, current_user.id))
        cursor.execute("DELETE FROM investment_updates WHERE investment_id = %s AND user_id = %s", (inv_id, current_user.id))
    else:
        cursor.execute("DELETE FROM investments WHERE id = ? AND user_id = ?", (inv_id, current_user.id))
        cursor.execute("DELETE FROM investment_updates WHERE investment_id = ? AND user_id = ?", (inv_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Investment deleted."))


@app.post("/actions/update-investment")
@login_required
def actions_update_investment():
    inv_id = request.form.get("investment_id")
    value = (request.form.get("value") or "").strip()
    inv_date = (request.form.get("date") or "").strip()

    if not inv_id or not value or not inv_date:
        return redirect(url_for("actions", msg="Missing fields."))
    try:
        value = float(value)
    except ValueError:
        return redirect(url_for("actions", msg="Invalid value."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO investment_updates (investment_id, user_id, value, date) VALUES (%s, %s, %s, %s)",
                       (inv_id, current_user.id, value, inv_date))
    else:
        cursor.execute("INSERT INTO investment_updates (investment_id, user_id, value, date) VALUES (?, ?, ?, ?)",
                       (inv_id, current_user.id, value, inv_date))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("actions", msg="Investment updated."))

# --- DANGER ZONE: RESET ACTIONS ---
# These wipe data for the current user only (never touch other users)
# Accessible from the Danger tab in Settings

# Clears all transaction history
@app.post("/settings/reset-transactions")
@login_required
def reset_transactions():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM transactions WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("DELETE FROM transactions WHERE user_id = ?", (current_user.id,))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="All transactions cleared."))


@app.post("/settings/reset-balances")
@login_required
def reset_balances():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = 0 WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("UPDATE accounts SET balance = 0 WHERE user_id = ?", (current_user.id,))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="All account balances reset to £0."))


@app.post("/settings/reset-bills")
@login_required
def reset_bills():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM scheduled_expenses WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("DELETE FROM scheduled_expenses WHERE user_id = ?", (current_user.id,))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="All scheduled bills deleted."))


@app.post("/settings/reset-income")
@login_required
def reset_income():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM income WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("DELETE FROM income WHERE user_id = ?", (current_user.id,))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="All income sources deleted."))


@app.post("/settings/reset-all")
@login_required
def reset_all():
    db = get_db()
    cursor = db.cursor()
    uid = current_user.id
    tables = [
        "transactions",
        "scheduled_expenses",
        "income",
        "savings_rules",
        "future_events",
        "investment_updates",
        "investments",
    ]
    for table in tables:
        if USE_POSTGRES:
            cursor.execute(f"DELETE FROM {table} WHERE user_id = %s", (uid,))
        else:
            cursor.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
    # Zero out balances but keep accounts
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = 0 WHERE user_id = %s", (uid,))
    else:
        cursor.execute("UPDATE accounts SET balance = 0 WHERE user_id = ?", (uid,))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Account fully reset. Fresh start! 🌱"))

# --- 90-DAY FORECAST ---
# Simulates account balances day by day for the next 90 days
# Applies weekly/monthly income, scheduled bills, future events, and savings rules each day
# Results are cached for 5 minutes (per user per day) to avoid recomputing on every page load
# Passes JSON snapshots to the frontend for Chart.js to render
@app.get("/forecast")
@login_required
def forecast():
    from datetime import date as date_type, timedelta
    import json
    import time

    today = date_type.today()
    cache_key = f"forecast_{current_user.id}_{today.isoformat()}"

    # return cached result if still fresh
    if cache_key in forecast_cache:
        cached_at, cached_data = forecast_cache[cache_key]
        if time.time() - cached_at < FORECAST_CACHE_TTL:
            return render_template(
                "forecast.html",
                snapshots=cached_data["snapshots"],
                account_names=cached_data["account_names"],
                today=today.isoformat()
            )

    accounts_rows = get_active_accounts(current_user.id)
    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": float(r["balance"]),
            "type": r["type"],
            "active": True
        }

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

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM income WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    income_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    savings_rules = [dict(zip(cols, row)) for row in cursor.fetchall()]

    cursor.close()
    release_db(db)

    future_events = []
    for e in future_events_raw:
        try:
            future_events.append({
                "date": date_type.fromisoformat(str(e["date"])),
                "name": e["name"],
                "amount": e["amount"],
                "account": e["account"]
            })
        except (ValueError, KeyError) as ex:
            logger.debug(f"Invalid future event data in forecast: {e}, error: {ex}")
            continue

    simulated = {}
    for name, info in accounts.items():
        simulated[name] = info["balance"]

    account_names = list(accounts.keys())
    snapshots = []

    for day_offset in range(1, 91):
        sim_day = today + timedelta(days=day_offset)

        if sim_day.weekday() == 4:
            for inc in income_rows:
                if inc["frequency"] == "weekly" and inc["account"] in simulated:
                    simulated[inc["account"]] += float(inc["amount"])

        for inc in income_rows:
            if inc["frequency"] == "monthly" and inc["account"] in simulated:
                if sim_day.day == 1:
                    simulated[inc["account"]] += float(inc["amount"])

        for expense in scheduled:
            if expense["day"] == sim_day.day and expense["account"] in simulated:
                simulated[expense["account"]] -= float(expense["amount"])

        for event in future_events:
            if event["date"] == sim_day and event["account"] in simulated:
                simulated[event["account"]] -= float(event["amount"])

        for rule in savings_rules:
            freq = rule.get("frequency", "monthly")
            apply_rule = False
            if freq == "monthly" and rule["day"] == sim_day.day:
                apply_rule = True
            elif freq == "weekly" and sim_day.weekday() == 4:
                apply_rule = True
            elif freq == "daily":
                apply_rule = True

            if apply_rule:
                from_acc = rule["from_account"]
                to_acc = rule["to_account"]
                amt = float(rule["amount"])
                if from_acc in simulated and to_acc in simulated:
                    if simulated[from_acc] >= amt:
                        simulated[from_acc] -= amt
                        simulated[to_acc] += amt

        snapshot = {"date": sim_day.isoformat()}
        for acc in account_names:
            snapshot[acc] = round(simulated[acc], 2)
        snapshots.append(snapshot)

    snapshots_json = json.dumps(snapshots)
    account_names_json = json.dumps(account_names)

    # store in cache
    forecast_cache[cache_key] = (time.time(), {
        "snapshots": snapshots_json,
        "account_names": account_names_json
    })

    return render_template(
        "forecast.html",
        snapshots=snapshots_json,
        account_names=account_names_json,
        today=today.isoformat()
    )

@app.get("/verify-email/<token>")
@limiter.limit("10 per minute")
def verify_email(token):
    from datetime import datetime
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, verify_token_expires_at FROM users WHERE verify_token = %s", (token,))
    else:
        cursor.execute("SELECT id, verify_token_expires_at FROM users WHERE verify_token = ?", (token,))
    row = cursor.fetchone()

    invalid_msg = "Invalid or expired verification link. Please sign up again."

    if row:
        user_id = row[0] if USE_POSTGRES else row["id"]
        expires_at_str = row[1] if USE_POSTGRES else row["verify_token_expires_at"]

        # Check if token is expired
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now() > expires_at:
                cursor.close()
                release_db(db)
                # Use generic message for token expiration too
                return redirect(url_for("login", msg=invalid_msg))

        if USE_POSTGRES:
            cursor.execute("UPDATE users SET verified = 1, verify_token = NULL, verify_token_expires_at = NULL WHERE id = %s", (user_id,))
        else:
            cursor.execute("UPDATE users SET verified = 1, verify_token = NULL, verify_token_expires_at = NULL WHERE id = ?", (user_id,))
        db.commit()
        cursor.close()
        release_db(db)
        logger.info(f"Email verified for user ID: {user_id}")
        return redirect(url_for("home", msg="✅ Email verified! Welcome to Spendara."))
    cursor.close()
    release_db(db)
    return redirect(url_for("login", msg=invalid_msg))

@app.context_processor
def inject_user_verified():
    if current_user.is_authenticated:
        try:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("SELECT verified FROM users WHERE id = %s", (current_user.id,))
            else:
                cursor.execute("SELECT verified FROM users WHERE id = ?", (current_user.id,))
            row = cursor.fetchone()
            cursor.close()
            release_db(db)
            verified = bool(row[0] if USE_POSTGRES else row["verified"]) if row else False
            return {"user_verified": verified}
        except Exception as e:
            logger.error(f"Error checking user verification status: {e}")
            return {"user_verified": True}
    return {"user_verified": True}

# --- REGISTER ---
# GET: shows the registration form
# POST: validates email/password, creates user, sends verification email, logs them in
@app.get("/register")
def register():
    return render_template("register.html")

@app.post("/register")
@limiter.limit("5 per minute")
def register_post():
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
    confirm = (request.form.get("confirm") or "").strip()

    if not email or not password:
        return render_template("register.html", error="All fields are required.")

    if password != confirm:
        return render_template("register.html", error="Passwords do not match.")

    if len(password) < 8:
        return render_template("register.html", error="Password must be at least 8 characters.")

    if not any(c.isupper() for c in password):
        return render_template("register.html", error="Password must contain at least one uppercase letter.")

    if not any(c.isdigit() for c in password):
        return render_template("register.html", error="Password must contain at least one number.")

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    else:
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))

    existing = cursor.fetchone()

    if existing:
        cursor.close()
        release_db(db)
        return render_template("register.html", error="An account with that email already exists.")

    hashed = generate_password_hash(password)
    today_str = date.today().isoformat()
    token = secrets.token_urlsafe(32)

    from datetime import datetime, timedelta
    expires_at = (datetime.now() + timedelta(days=7)).isoformat()

    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO users (email, password, created_at, verify_token, verify_token_expires_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (email, hashed, today_str, token, expires_at)
        )
        user_id = cursor.fetchone()[0]
    else:
        cursor.execute(
            "INSERT INTO users (email, password, created_at, verify_token, verify_token_expires_at) VALUES (?, ?, ?, ?, ?)",
            (email, hashed, today_str, token, expires_at)
        )
        user_id = cursor.lastrowid

    db.commit()
    cursor.close()
    release_db(db)

    logger.info(f"New user registered: {email}")
    send_verification_email(email, token)

    user = User(user_id, email)
    session.permanent = True
    login_user(user, remember=True)
    return redirect(url_for("home", msg="Welcome! Please check your email to verify your account."))


# --- LOGIN ---
# GET: shows the login form
# POST: checks email/password hash, creates session on success
@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
@limiter.limit("10 per minute")
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
    release_db(db)

    if not row:
        return render_template("login.html", error="Invalid email or password.")

    row = dict(zip(cols, row))

    if not check_password_hash(row["password"], password):
        logger.warning(f"Failed login attempt for email: {email}")
        return render_template("login.html", error="Invalid email or password.")

    logger.info(f"Successful login for user: {email}")

    user = User(row["id"], row["email"])
    login_user(user, remember=True)
    session.permanent = True
    return redirect(url_for("home"))

@app.get("/logout")
def logout():
    if current_user.is_authenticated:
        logger.info(f"User logout: {current_user.email}")
    logout_user()
    return redirect(url_for("login"))

# --- FORGOT PASSWORD ---
# Sends a reset link to the user's email (if it exists in the database)
# Always shows the same success message whether the email exists or not (prevents email enumeration)
@app.get("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html", message="")

@app.post("/forgot-password")
@limiter.limit("5 per minute")
def forgot_password_post():
    email = (request.form.get("email") or "").strip().lower()

    if not email:
        return render_template("forgot_password.html", message="Please enter your email.")

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    else:
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if row:
        user_id = row[0] if USE_POSTGRES else row["id"]
        token = secrets.token_urlsafe(32)

        from datetime import datetime, timedelta
        expires_at = (datetime.now() + timedelta(hours=24)).isoformat()

        db2 = get_db()
        cursor2 = db2.cursor()
        if USE_POSTGRES:
            cursor2.execute("UPDATE users SET verify_token = %s, verify_token_expires_at = %s WHERE id = %s", (token, expires_at, user_id))
        else:
            cursor2.execute("UPDATE users SET verify_token = ?, verify_token_expires_at = ? WHERE id = ?", (token, expires_at, user_id))
        db2.commit()
        cursor2.close()
        release_db(db2)

        reset_url = f"https://spendara.co.uk/reset-password/{token}"
        send_reset_email(email, reset_url)

    return render_template("forgot_password.html", message="If that email exists you'll receive a reset link shortly.")


# --- RESET PASSWORD ---
# GET: shows the new password form (token passed in URL)
# POST: validates token, checks expiry, saves new hashed password, clears the token
@app.get("/reset-password/<token>")
@limiter.limit("10 per minute")
def reset_password(token):
    return render_template("reset_password.html", token=token, message="")

@app.post("/reset-password/<token>")
@limiter.limit("10 per minute")
def reset_password_post(token):
    password = (request.form.get("password") or "").strip()
    confirm = (request.form.get("confirm") or "").strip()

    if not password or not confirm:
        return render_template("reset_password.html", token=token, message="All fields are required.")

    if password != confirm:
        return render_template("reset_password.html", token=token, message="Passwords do not match.")

    if len(password) < 8:
        return render_template("reset_password.html", token=token, message="Password must be at least 8 characters.")

    if not any(c.isupper() for c in password):
        return render_template("reset_password.html", token=token, message="Password must contain at least one uppercase letter.")

    if not any(c.isdigit() for c in password):
        return render_template("reset_password.html", token=token, message="Password must contain at least one number.")

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, verify_token_expires_at FROM users WHERE verify_token = %s", (token,))
    else:
        cursor.execute("SELECT id, verify_token_expires_at FROM users WHERE verify_token = ?", (token,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    invalid_msg = "Invalid or expired reset link. Please request a new one."

    if not row:
        return render_template("reset_password.html", token=token, message=invalid_msg)

    user_id = row[0] if USE_POSTGRES else row["id"]
    expires_at_str = row[1] if USE_POSTGRES else row["verify_token_expires_at"]

    # Check if token is expired
    if expires_at_str:
        from datetime import datetime
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now() > expires_at:
            # Use generic message for token expiration too
            return render_template("reset_password.html", token=token, message=invalid_msg)

    hashed = generate_password_hash(password)

    db2 = get_db()
    cursor2 = db2.cursor()
    if USE_POSTGRES:
        cursor2.execute("UPDATE users SET password = %s, verify_token = NULL, verify_token_expires_at = NULL WHERE id = %s", (hashed, user_id))
    else:
        cursor2.execute("UPDATE users SET password = ?, verify_token = NULL, verify_token_expires_at = NULL WHERE id = ?", (hashed, user_id))
    db2.commit()
    cursor2.close()
    release_db(db2)

    logger.info(f"Password reset successful for user ID: {user_id}")
    return redirect(url_for("login", msg="Password reset successfully! Please log in."))

@app.get("/resend-verification")
@limiter.limit("5 per minute")
@login_required
def resend_verification():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT email, verified FROM users WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT email, verified FROM users WHERE id = ?", (current_user.id,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    if not row:
        return redirect(url_for("home"))

    email = row[0] if USE_POSTGRES else row["email"]
    verified = row[1] if USE_POSTGRES else row["verified"]

    if verified:
        return redirect(url_for("home", msg="Your email is already verified!"))

    token = secrets.token_urlsafe(32)

    from datetime import datetime, timedelta
    expires_at = (datetime.now() + timedelta(days=7)).isoformat()

    db2 = get_db()
    cursor2 = db2.cursor()
    if USE_POSTGRES:
        cursor2.execute("UPDATE users SET verify_token = %s, verify_token_expires_at = %s WHERE id = %s", (token, expires_at, current_user.id))
    else:
        cursor2.execute("UPDATE users SET verify_token = ?, verify_token_expires_at = ? WHERE id = ?", (token, expires_at, current_user.id))
    db2.commit()
    cursor2.close()
    release_db(db2)

    send_verification_email(email, token)

    return redirect(url_for("home", msg="Verification email resent! Check your inbox."))

# --- CSV IMPORT ---
# Parses a bank CSV file and returns a list of transaction dicts, plus an import route
# Supports Monzo, Barclays, HSBC, Nationwide, Starling, NatWest (auto-detects column names)
def parse_bank_csv(content: str):
    """
    Parse a bank CSV and return (rows, error).
    rows = list of {date, description, amount} dicts.
    Handles Monzo, Barclays, HSBC, Nationwide, Starling, NatWest formats.
    """
    import io
    from datetime import datetime as dt

    try:
        dialect = csv.Sniffer().sniff(content[:2000], delimiters=',;\t')
    except Exception:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    try:
        rows = list(reader)
    except Exception:
        return None, "Could not read CSV file."

    if not rows:
        return None, "CSV file is empty."

    fieldnames = reader.fieldnames or []

    # Detect date column
    date_col = next((h for h in fieldnames if h and h.strip().lower() in
        ['date', 'transaction date', 'posted date', 'value date']), None)

    # Detect description column
    desc_candidates = ['description', 'memo', 'name', 'narrative', 'details',
                        'payee', 'counter party', 'counterparty', 'transactions',
                        'transaction details', 'merchant name', 'reference']
    desc_col = next((h for h in fieldnames if h and h.strip().lower() in desc_candidates), None)
    if not desc_col:
        desc_col = next((h for h in fieldnames if h and any(
            c in h.strip().lower() for c in ['desc', 'memo', 'narr', 'detail', 'payee', 'merchant'])), None)

    # Detect amount columns (single or split debit/credit)
    amount_col = next((h for h in fieldnames if h and h.strip().lower() in
        ['amount', 'value', 'transaction amount', 'amount (gbp)']), None)
    debit_col = next((h for h in fieldnames if h and h.strip().lower() in
        ['debit', 'debits', 'money out', 'paid out']), None)
    credit_col = next((h for h in fieldnames if h and h.strip().lower() in
        ['credit', 'credits', 'money in', 'paid in']), None)

    if not date_col or not desc_col or (not amount_col and not (debit_col and credit_col)):
        found = ', '.join(str(h) for h in fieldnames if h)
        return None, f"Could not detect required columns. Columns found: {found}"

    date_formats = ['%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%d %b %Y', '%d %B %Y', '%m/%d/%Y']
    parsed = []

    for row in rows:
        try:
            date_str = (row.get(date_col) or '').strip().strip('"')
            desc = (row.get(desc_col) or '').strip().strip('"')
            if not date_str or not desc:
                continue

            parsed_date = None
            for fmt in date_formats:
                try:
                    parsed_date = dt.strptime(date_str, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            if not parsed_date:
                continue

            if amount_col:
                raw = (row.get(amount_col) or '').strip().strip('"').replace(',', '').replace('£', '').replace('$', '')
                if not raw:
                    continue
                amount = float(raw)
            else:
                debit_raw = (row.get(debit_col) or '').strip().strip('"').replace(',', '').replace('£', '')
                credit_raw = (row.get(credit_col) or '').strip().strip('"').replace(',', '').replace('£', '')
                debit = float(debit_raw) if debit_raw else 0.0
                credit = float(credit_raw) if credit_raw else 0.0
                amount = round(credit - debit, 2)

            # Auto-detect internal transfers by common keywords in the description
            transfer_keywords = [
                'transfer', 'internal', 'from pot', 'to pot', 'pot transfer',
                'savings pot', 'roundup', 'round up', 'moneybox', 'sweep',
                'between accounts', 'own account', 'joint account'
            ]
            desc_lower = desc.lower()
            is_transfer = any(kw in desc_lower for kw in transfer_keywords)

            parsed.append({'date': parsed_date, 'description': desc, 'amount': round(amount, 2), 'is_transfer': is_transfer})
        except Exception:
            continue

    if not parsed:
        return None, "No valid transactions found in the CSV."

    return parsed[:500], None


@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_csv():
    accounts_rows = get_active_accounts(current_user.id)
    accounts = [r["name"] for r in accounts_rows]

    if request.method == 'GET':
        return render_template('import.html', accounts=accounts, preview=None, error=None, selected_account=None)

    # Validate CSRF
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return render_template('import.html', accounts=accounts, preview=None, error="Invalid request.", selected_account=None)

    selected_account = (request.form.get('account') or '').strip()
    file = request.files.get('csv_file')

    if not file or not file.filename:
        return render_template('import.html', accounts=accounts, preview=None, error="Please select a CSV file.", selected_account=selected_account)

    try:
        content = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            file.seek(0)
            content = file.read().decode('latin-1')
        except Exception:
            return render_template('import.html', accounts=accounts, preview=None, error="Could not read the file.", selected_account=selected_account)

    rows, err = parse_bank_csv(content)
    if err:
        return render_template('import.html', accounts=accounts, preview=None, error=err, selected_account=selected_account)

    session['import_rows'] = rows
    session['import_account'] = selected_account

    return render_template('import.html', accounts=accounts, preview=rows, error=None, selected_account=selected_account)


@app.post('/import/confirm')
@login_required
def import_confirm():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return redirect(url_for('import_csv'))

    rows = session.pop('import_rows', None)
    account = session.pop('import_account', None)

    if not rows or not account:
        return redirect(url_for('import_csv'))

    # Only import rows the user checked (sent as include_0, include_1, etc.)
    selected_rows = [rows[i] for i in range(len(rows)) if request.form.get(f'include_{i}') == '1']

    if not selected_rows:
        return redirect(url_for('import_csv'))

    total_delta = 0.0
    for row in selected_rows:
        add_transaction(row['date'], row['description'], row['amount'], account, current_user.id, type='import')
        total_delta += row['amount']

    # Single balance update for all rows
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE accounts SET balance = balance + %s WHERE name = %s AND user_id = %s",
                       (total_delta, account, current_user.id))
    else:
        cursor.execute("UPDATE accounts SET balance = balance + ? WHERE name = ? AND user_id = ?",
                       (total_delta, account, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)

    return redirect(url_for('transactions', msg=f"Imported {len(selected_rows)} transactions to {account}"))


# =============================================================================
# STRIPE / BILLING ROUTES
# =============================================================================

import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

# --- UPGRADE TO PRO ---
# Creates a Stripe Checkout session and redirects the user to Stripe's hosted payment page
# On success, Stripe redirects back to /billing/success
# On cancel, Stripe redirects back to /settings
@app.get("/billing/upgrade")
@login_required
def billing_upgrade():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            customer_email=current_user.email,
            success_url="https://spendara.co.uk/billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://spendara.co.uk/settings",
            metadata={"user_id": current_user.id},
        )
        return redirect(checkout_session.url)
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return redirect(url_for("settings", msg="Could not start checkout. Please try again."))


# --- BILLING SUCCESS ---
# User lands here after successful Stripe payment
# The webhook will have already (or will soon) set is_pro=1 — this just shows a nice message
@app.get("/billing/success")
@login_required
def billing_success():
    return redirect(url_for("settings", msg="You're now on Pro! Unlimited accounts unlocked."))


# --- MANAGE SUBSCRIPTION (Stripe Customer Portal) ---
# Opens Stripe's hosted billing portal so users can cancel, update card, etc.
# Requires the user to have a stripe_customer_id saved from the webhook
@app.get("/billing/portal")
@login_required
def billing_portal():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT stripe_customer_id FROM users WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT stripe_customer_id FROM users WHERE id = ?", (current_user.id,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)

    customer_id = (row[0] if USE_POSTGRES else row["stripe_customer_id"]) if row else None

    if not customer_id:
        return redirect(url_for("settings", msg="No billing account found."))

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url="https://spendara.co.uk/settings",
        )
        return redirect(portal_session.url)
    except Exception as e:
        logger.error(f"Stripe portal error: {e}")
        return redirect(url_for("settings", msg="Could not open billing portal. Please try again."))


# --- STRIPE WEBHOOK ---
# Stripe calls this endpoint when subscription events happen
# We verify the signature to make sure it's genuinely from Stripe (not a forged request)
# checkout.session.completed → user paid → set is_pro=1, save stripe_customer_id
# customer.subscription.deleted → user cancelled → set is_pro=0
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Stripe webhook signature error: {e}")
        return "Invalid signature", 400

    # Payment completed — activate Pro
    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        user_id = session_obj.get("metadata", {}).get("user_id")
        customer_id = session_obj.get("customer")

        if user_id:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("UPDATE users SET is_pro = 1, stripe_customer_id = %s WHERE id = %s", (customer_id, user_id))
            else:
                cursor.execute("UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))
            db.commit()
            cursor.close()
            release_db(db)
            logger.info(f"Pro activated for user_id={user_id}")

    # Subscription cancelled — deactivate Pro
    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")

        if customer_id:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("UPDATE users SET is_pro = 0 WHERE stripe_customer_id = %s", (customer_id,))
            else:
                cursor.execute("UPDATE users SET is_pro = 0 WHERE stripe_customer_id = ?", (customer_id,))
            db.commit()
            cursor.close()
            release_db(db)
            logger.info(f"Pro deactivated for customer_id={customer_id}")

    return "OK", 200


# --- HELPER: check if current user is Pro ---
def user_is_pro():
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT is_pro FROM users WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT is_pro FROM users WHERE id = ?", (current_user.id,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    return bool(row[0] if USE_POSTGRES else row["is_pro"]) if row else False


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template("500.html"), 500

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)