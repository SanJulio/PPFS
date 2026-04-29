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
import calendar
import json
import uuid
import random

from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template, jsonify
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

# simulate_balances_until is used for forecast and "can I afford it" features
from Tracker import simulate_balances_until

from models import (
    add_transaction,
    update_account_balance,
    get_active_accounts,
    get_recent_transactions
)

from database import get_db, release_db

from database import USE_POSTGRES

# In-memory cache for the 90-day forecast — expensive to compute so we cache for 5 minutes
forecast_cache = {}
FORECAST_CACHE_TTL = 300  # 5 minutes in seconds


def bust_forecast_cache(user_id):
    """Remove all forecast cache entries for a user so the next page load recomputes."""
    prefix = f"forecast_{user_id}_"
    stale = [k for k in list(forecast_cache.keys()) if k.startswith(prefix)]
    for k in stale:
        forecast_cache.pop(k, None)

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
                logger.error(f"Session open error: {e}")
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
            logger.error(f"Session save error: {e}")
        response.set_cookie("session", sid, httponly=True, secure=True, samesite="Lax")

# --- FLASK APP SETUP ---
app = Flask(__name__)

@app.template_filter('dateformat')
def dateformat_filter(value):
    """Convert YYYY-MM-DD string to '9 Apr 2026' format."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value), '%Y-%m-%d').strftime('%-d %b %Y')
    except Exception:
        return value

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
        exempt = ['/login', '/register', '/stripe/webhook', '/auto-apply', '/mark-bill-paid', '/dismiss-auto-apply']
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
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", 0))
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
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
    def __init__(self, id, email, display_name=None, avatar=None):
        self.id = id
        self.email = email
        self.display_name = display_name
        self.avatar = avatar

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
        return User(row["id"], row["email"], row.get("display_name"), row.get("avatar"))
    return None

# Run database migrations on startup — creates tables and adds any missing columns
from database import init_db
try:
    with app.app_context():
        init_db()
except Exception as e:
    logger.error(f"init_db FAILED: {e}")

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
        sender={"email": "hello@spendara.co.uk", "name": "Spendara"},
        reply_to={"email": "hello@spendara.co.uk", "name": "Spendara"},
        subject="Confirm your Spendara account",
        html_content=f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f2f4f7;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7;padding:40px 16px;">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:16px;overflow:hidden;">
      <!-- Body -->
      <tr><td style="padding:32px 32px 32px;">
        <h1 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#111;">Welcome to Spendara 👋</h1>
        <p style="margin:0 0 24px;font-size:15px;color:#555;line-height:1.6;">
          Thanks for signing up. Click the button below to verify your email and get started with Spendara.
        </p>
        <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;"><tr><td>
          <a href="{verify_url}" style="display:inline-block;background:#6366f1;color:#ffffff;padding:14px 36px;border-radius:12px;text-decoration:none;font-weight:700;font-size:15px;">
            Confirm email
          </a>
        </td></tr></table>
        <p style="margin:0;font-size:12px;color:#aaa;line-height:1.6;">
          This link expires in 7 days.<br>
          If you didn't create a Spendara account, you can safely ignore this email.
        </p>
      </td></tr>
      <!-- Footer -->
      <tr><td style="padding:16px 32px;background:#f8f9fa;border-top:1px solid #eee;">
        <p style="margin:0;font-size:11px;color:#bbb;text-align:center;">
          Spendara &middot; <a href="https://spendara.co.uk" style="color:#bbb;text-decoration:none;">spendara.co.uk</a>
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>""",
        text_content=f"""Confirm your email – Spendara

Thanks for signing up to Spendara, your personal finance tracker.

Verify your email address by visiting this link:
{verify_url}

This link expires in 7 days. If you didn't create a Spendara account you can safely ignore this email.

— Spendara · https://spendara.co.uk
"""
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        return True
    except ApiException as e:
        logger.error(f"Email send error: {e}")
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
        logger.error(f"Reset email send error: {e}")
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

VALID_EVENTS = {
    'auth.register', 'auth.login',
    'page_view.dashboard', 'page_view.forecast', 'page_view.transactions',
    'page_view.flow', 'page_view.actions', 'page_view.settings', 'page_view.import',
    'action.add_expense', 'action.add_income', 'action.transfer', 'action.pay_bill',
    'action.receive_income', 'action.import_csv', 'action.afford_check', 'action.investment_update',
    'billing.upgrade_start', 'billing.upgrade_complete', 'billing.cancel',
}

def track(event: str):
    """Fire-and-forget analytics. Silently swallows errors so tracking never breaks routes."""
    if event not in VALID_EVENTS:
        return
    if not current_user.is_authenticated:
        return
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO analytics_events (user_id, event) VALUES (%s, %s)",
                (current_user.id, event)
            )
            if random.random() < 0.01:
                cursor.execute("DELETE FROM analytics_events WHERE ts < NOW() - INTERVAL '180 days'")
        else:
            cursor.execute(
                "INSERT INTO analytics_events (user_id, event) VALUES (?, ?)",
                (current_user.id, event)
            )
            if random.random() < 0.01:
                cursor.execute("DELETE FROM analytics_events WHERE ts < datetime('now', '-180 days')")
        db.commit()
        cursor.close()
        release_db(db)
    except Exception as e:
        logger.debug(f"Analytics track error: {e}")

def track_for_user(user_id: int, event: str):
    """Same as track() but with explicit user_id — used for auth events before current_user is set."""
    if event not in VALID_EVENTS:
        return
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO analytics_events (user_id, event) VALUES (%s, %s)",
                (user_id, event)
            )
        else:
            cursor.execute(
                "INSERT INTO analytics_events (user_id, event) VALUES (?, ?)",
                (user_id, event)
            )
        db.commit()
        cursor.close()
        release_db(db)
    except Exception as e:
        logger.debug(f"Analytics track_for_user error: {e}")

# --- AUTO-APPLY HELPERS ---

def _get_occurrences_between(item, start_date, end_date):
    """Return all dates a scheduled item fires between start_date and end_date (inclusive).
    Handles monthly and yearly frequencies. Weekly is skipped (no fixed anchor date)."""
    import calendar as _cal
    from datetime import date as _date

    freq = item.get('frequency') or 'monthly'
    day = int(item.get('day') or 1)
    results = []

    if freq == 'monthly':
        y, m = start_date.year, start_date.month
        while (y, m) <= (end_date.year, end_date.month):
            actual_day = min(day, _cal.monthrange(y, m)[1])
            try:
                candidate = _date(y, m, actual_day)
                if start_date <= candidate <= end_date:
                    results.append(candidate)
            except ValueError:
                pass
            m += 1
            if m > 12:
                m = 1
                y += 1

    elif freq == 'yearly':
        bill_month = int(item.get('month') or 1)
        for yr in range(start_date.year, end_date.year + 1):
            actual_day = min(day, _cal.monthrange(yr, bill_month)[1])
            try:
                candidate = _date(yr, bill_month, actual_day)
                if start_date <= candidate <= end_date:
                    results.append(candidate)
            except ValueError:
                pass

    return results


def run_auto_apply_backfill(user_id):
    """One-time backfill: insert transactions for April 1 to yesterday for items with
    last_applied=NULL. Does NOT update account balances. Sets last_applied=yesterday."""
    from datetime import date as _date, timedelta

    today = _date.today()
    backfill_start = _date(2026, 4, 1)
    yesterday = today - timedelta(days=1)

    if yesterday < backfill_start:
        return  # Nothing to backfill yet

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s AND last_applied IS NULL", (user_id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ? AND last_applied IS NULL", (user_id,))
    cols = [d[0] for d in cursor.description]
    bills = [dict(zip(cols, r)) for r in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE user_id = %s AND last_applied IS NULL", (user_id,))
    else:
        cursor.execute("SELECT * FROM income WHERE user_id = ? AND last_applied IS NULL", (user_id,))
    cols = [d[0] for d in cursor.description]
    income_items = [dict(zip(cols, r)) for r in cursor.fetchall()]

    yesterday_str = yesterday.isoformat()

    cursor.close()
    release_db(db)

    # Use the shared helpers so we get the correct user_id scoping and no dependency on auto_generated
    for bill in bills:
        if bill.get('day') is None:
            continue
        for d in _get_occurrences_between(bill, backfill_start, yesterday):
            try:
                add_transaction(d.isoformat(), bill['name'], -abs(float(bill['amount'])), bill['account'], user_id, type='bill', category='Bills')
            except Exception as e:
                logger.debug(f"Backfill bill insert error: {e}")
        # Update last_applied in a fresh connection
        try:
            _db = get_db()
            _c = _db.cursor()
            if USE_POSTGRES:
                _c.execute("UPDATE scheduled_expenses SET last_applied = %s WHERE id = %s", (yesterday_str, bill['id']))
            else:
                _c.execute("UPDATE scheduled_expenses SET last_applied = ? WHERE id = ?", (yesterday_str, bill['id']))
            _db.commit()
            _c.close()
            release_db(_db)
        except Exception as e:
            logger.debug(f"Backfill bill last_applied error: {e}")

    for inc in income_items:
        if inc.get('day') is None:
            continue
        for d in _get_occurrences_between(inc, backfill_start, yesterday):
            try:
                add_transaction(d.isoformat(), inc['name'], abs(float(inc['amount'])), inc['account'], user_id, type='income', category='Income')
            except Exception as e:
                logger.debug(f"Backfill income insert error: {e}")
        try:
            _db = get_db()
            _c = _db.cursor()
            if USE_POSTGRES:
                _c.execute("UPDATE income SET last_applied = %s WHERE id = %s", (yesterday_str, inc['id']))
            else:
                _c.execute("UPDATE income SET last_applied = ? WHERE id = ?", (yesterday_str, inc['id']))
            _db.commit()
            _c.close()
            release_db(_db)
        except Exception as e:
            logger.debug(f"Backfill income last_applied error: {e}")


def get_pending_auto_apply_items(user_id):
    """Returns list of items due today (or overdue since last_applied) that need applying.
    Each entry: {type, item_id, name, amount, account, due_date}
    Amount is negative for bills, positive for income."""
    from datetime import date as _date, timedelta

    today = _date.today()

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s AND last_applied IS NOT NULL", (user_id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ? AND last_applied IS NOT NULL", (user_id,))
    cols = [d[0] for d in cursor.description]
    bills = [dict(zip(cols, r)) for r in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE user_id = %s AND last_applied IS NOT NULL", (user_id,))
    else:
        cursor.execute("SELECT * FROM income WHERE user_id = ? AND last_applied IS NOT NULL", (user_id,))
    cols = [d[0] for d in cursor.description]
    income_items = [dict(zip(cols, r)) for r in cursor.fetchall()]

    cursor.close()
    release_db(db)

    pending = []

    for bill in bills:
        if bill.get('day') is None:
            continue
        last_applied = _date.fromisoformat(bill['last_applied'])
        search_from = last_applied + timedelta(days=1)
        if search_from > today:
            continue
        for d in _get_occurrences_between(bill, search_from, today):
            pending.append({
                'type': 'bill',
                'item_id': bill['id'],
                'name': bill['name'],
                'amount': -abs(float(bill['amount'])),
                'account': bill['account'],
                'due_date': d.isoformat(),
            })

    for inc in income_items:
        if inc.get('day') is None:
            continue
        last_applied = _date.fromisoformat(inc['last_applied'])
        search_from = last_applied + timedelta(days=1)
        if search_from > today:
            continue
        for d in _get_occurrences_between(inc, search_from, today):
            pending.append({
                'type': 'income',
                'item_id': inc['id'],
                'name': inc['name'],
                'amount': abs(float(inc['amount'])),
                'account': inc['account'],
                'due_date': d.isoformat(),
            })

    return sorted(pending, key=lambda x: (x['due_date'], x['name']))


def apply_auto_items(user_id, items):
    """Apply a list of pending items: insert transactions, update balances, update last_applied.
    Uses the shared add_transaction / update_account_balance helpers so each operation
    is in its own committed connection — a failure on one item doesn't abort the rest."""
    from datetime import date as _date

    today_str = _date.today().isoformat()

    for item in items:
        try:
            tx_type = 'bill' if item['type'] == 'bill' else 'income'
            category = 'Bills' if item['type'] == 'bill' else 'Income'
            add_transaction(item['due_date'], item['name'], item['amount'], item['account'], user_id, type=tx_type, category=category)
            update_account_balance(item['account'], item['amount'], user_id)
        except Exception as e:
            logger.error(f"Auto-apply item error ({item.get('name')}): {e}")

    # Update last_applied for each unique item_id — separate connections so a transaction
    # error above doesn't block these from committing
    applied_bills = {i['item_id'] for i in items if i['type'] == 'bill'}
    applied_income = {i['item_id'] for i in items if i['type'] == 'income'}

    if applied_bills or applied_income:
        db = get_db()
        cursor = db.cursor()
        for item_id in applied_bills:
            if USE_POSTGRES:
                cursor.execute("UPDATE scheduled_expenses SET last_applied = %s WHERE id = %s", (today_str, item_id))
            else:
                cursor.execute("UPDATE scheduled_expenses SET last_applied = ? WHERE id = ?", (today_str, item_id))
        for item_id in applied_income:
            if USE_POSTGRES:
                cursor.execute("UPDATE income SET last_applied = %s WHERE id = %s", (today_str, item_id))
            else:
                cursor.execute("UPDATE income SET last_applied = ? WHERE id = ?", (today_str, item_id))
        db.commit()
        cursor.close()
        release_db(db)


def get_auto_apply_settings(user_id):
    """Returns (auto_apply_enabled, auto_apply_confirm) booleans for the user."""
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT auto_apply_enabled, auto_apply_confirm FROM users WHERE id = %s", (user_id,))
        else:
            cursor.execute("SELECT auto_apply_enabled, auto_apply_confirm FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        release_db(db)
        if row:
            return bool(row[0]), bool(row[1])
    except Exception:
        pass
    return True, True


# --- FINANCIAL OVERVIEW CALCULATION ---
# Splits accounts into spending (current/cash) and savings, then calculates:
# - spending balance (total in spending accounts)
# - future bills (bills still to leave this month)
# - safe to spend (spending balance minus future bills)
# - savings balance
# - net worth (spending + savings)
def calculate_financial_overview(accounts):
    from datetime import date
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

    current_month = today.month
    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        freq = expense.get("frequency") or "monthly"
        if freq == "yearly":
            if expense.get("month") != current_month:
                continue
        if expense["day"] > current_day:
            # Skip if already marked as paid this cycle
            last_applied = expense.get("last_applied")
            if last_applied:
                import calendar as _cal2
                days_in_month = _cal2.monthrange(today.year, today.month)[1]
                due_day = min(expense["day"], days_in_month)
                due_str = date(today.year, today.month, due_day).isoformat()
                if last_applied >= due_str:
                    continue
            acc = expense["account"]
            if acc in accounts and accounts[acc]["type"] in spending_types:
                spending_future_bills += expense["amount"]
                future_bills_list.append({
                    "id": expense["id"],
                    "name": expense["name"],
                    "amount": expense["amount"],
                    "day": expense["day"],
                    "account": expense["account"]
                })

    # --- Pending income arriving later this month ---
    # Accounts for mid-month or end-of-month pay days so safe_spending is accurate
    future_income = 0.0
    future_income_list = []
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT name, amount, frequency, account, day FROM income WHERE user_id = %s", (current_user.id,))
        else:
            cursor.execute("SELECT name, amount, frequency, account, day FROM income WHERE user_id = ?", (current_user.id,))
        cols = [d[0] for d in cursor.description]
        income_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        cursor.close()
        release_db(db)

        import calendar as _cal
        days_in_month = _cal.monthrange(today.year, today.month)[1]

        for inc in income_rows:
            freq = inc.get("frequency") or "monthly"
            acc = inc.get("account") or ""
            amount = float(inc.get("amount") or 0)
            if freq == "monthly":
                day = int(inc.get("day") or 1)
                if day > current_day:
                    future_income += amount
                    future_income_list.append({"name": inc["name"], "amount": amount, "day": day})
            elif freq == "weekly":
                # Count how many weekly payments fall on remaining days this month
                day = int(inc.get("day") or 1)  # day of week (1=Mon, 7=Sun) — stored as day of month for weekly
                # For weekly, count remaining full weeks
                remaining_days = days_in_month - current_day
                weekly_count = remaining_days // 7
                if weekly_count > 0:
                    future_income += amount * weekly_count
                    future_income_list.append({"name": inc["name"], "amount": amount * weekly_count, "day": current_day + 7})
    except Exception as e:
        logger.debug(f"Could not load future income for overview: {e}")

    safe_spending = spending_balance - spending_future_bills
    net_worth = spending_balance + savings_balance

    return {
        "spending_balance": spending_balance,
        "future_bills": spending_future_bills,
        "future_income": future_income,
        "future_income_list": sorted(future_income_list, key=lambda x: x["day"]),
        "safe_spending": safe_spending,
        "savings_balance": savings_balance,
        "net_worth": net_worth,
        "spending_accounts": sorted(spending_accounts, key=lambda x: x["name"].lower()),
        "savings_accounts": sorted(savings_accounts, key=lambda x: x["name"].lower()),
        "future_bills_list": sorted(future_bills_list, key=lambda x: x["day"]),
    }

# --- CYCLE DATE HELPER ---
# Given a cycle start day (1-28) and today's date, returns (cycle_start, cycle_end) as date objects.
# e.g. start_day=15, today=20 Apr → cycle_start=15 Apr, cycle_end=14 May
# e.g. start_day=15, today=10 Apr → cycle_start=15 Mar, cycle_end=14 Apr
def get_cycle_dates(start_day, today=None):
    from datetime import date as _date
    from dateutil.relativedelta import relativedelta
    if today is None:
        today = _date.today()
    start_day = max(1, min(28, int(start_day)))
    if today.day >= start_day:
        cycle_start = today.replace(day=start_day)
        cycle_end = (cycle_start + relativedelta(months=1)) - relativedelta(days=1)
    else:
        cycle_start = (today.replace(day=1) - relativedelta(days=1)).replace(day=start_day)
        cycle_end = today.replace(day=start_day) - relativedelta(days=1)
    return cycle_start, cycle_end


def get_budget_cycle_start(user_id):
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT budget_cycle_start FROM users WHERE id = %s", (user_id,))
        else:
            cursor.execute("SELECT budget_cycle_start FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        release_db(db)
        if row:
            return int(row[0] or 1)
    except Exception:
        pass
    return 1


# --- CYCLE SPENDING CALCULATION ---
# Queries outgoing transactions within the current budget cycle for the user.
# Cycle is defined by budget_cycle_start day (1-28). Default is 1 (calendar month).
def calculate_monthly_spending(cycle_start_date=None, cycle_end_date=None):
    if cycle_start_date is None:
        cycle_start_date = date.today().replace(day=1)
    if cycle_end_date is None:
        import calendar as _cal
        today = date.today()
        cycle_end_date = today.replace(day=_cal.monthrange(today.year, today.month)[1])

    db = get_db()
    cursor = db.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT amount, type, description, date, account FROM transactions
            WHERE date::date >= %s
            AND date::date <= %s
            AND user_id = %s
            AND amount < 0
            AND type != 'transfer'
            """,
            (cycle_start_date.isoformat(), cycle_end_date.isoformat(), current_user.id)
        )
    else:
        cursor.execute(
            """
            SELECT amount, type, description, date, account FROM transactions
            WHERE date >= ?
            AND date <= ?
            AND user_id = ?
            AND amount < 0
            AND type != 'transfer'
            """,
            (cycle_start_date.isoformat(), cycle_end_date.isoformat(), current_user.id)
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

    # Query income received this cycle
    income_received = 0.0
    income_list = []
    try:
        if USE_POSTGRES:
            cursor2 = db.cursor() if not db.closed else get_db().cursor()
            db2 = get_db()
            cursor2 = db2.cursor()
            cursor2.execute(
                "SELECT amount, description, date, account FROM transactions WHERE date::date >= %s AND date::date <= %s AND user_id = %s AND amount > 0 AND type != 'transfer'",
                (cycle_start_date.isoformat(), cycle_end_date.isoformat(), current_user.id)
            )
        else:
            db2 = get_db()
            cursor2 = db2.cursor()
            cursor2.execute(
                "SELECT amount, description, date, account FROM transactions WHERE date >= ? AND date <= ? AND user_id = ? AND amount > 0 AND type != 'transfer'",
                (cycle_start_date.isoformat(), cycle_end_date.isoformat(), current_user.id)
            )
        for r2 in cursor2.fetchall():
            if USE_POSTGRES:
                amt = float(r2[0]); desc2 = r2[1]; d2 = r2[2]; acc2 = r2[3]
            else:
                amt = float(r2["amount"]); desc2 = r2["description"]; d2 = r2["date"]; acc2 = r2["account"]
            income_received += amt
            income_list.append({"description": desc2, "amount": amt, "date": d2, "account": acc2})
        cursor2.close()
        release_db(db2)
    except Exception:
        pass

    return {
        "normal": normal,
        "scheduled": scheduled,
        "total": normal + scheduled,
        "normal_list": normal_list,
        "bills_list": bills_list,
        "income_received": income_received,
        "income_list": income_list,
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
    track('page_view.dashboard')
    # Check email verification
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT verified FROM users WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT verified FROM users WHERE id = ?", (current_user.id,))
    row = cursor.fetchone()
    verified = bool(row[0] if USE_POSTGRES else row["verified"]) if row else False

    today_str = date.today().isoformat()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0), COUNT(*) FROM transactions WHERE user_id = %s AND date = %s AND amount < 0",
            (current_user.id, today_str)
        )
    else:
        cursor.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0), COUNT(*) FROM transactions WHERE user_id = ? AND date = ? AND amount < 0",
            (current_user.id, today_str)
        )
    r = cursor.fetchone()
    today_spent = float(r[0] or 0)
    today_count = int(r[1] or 0)

    from datetime import timedelta
    week_start = date.today() - timedelta(days=date.today().weekday())
    week_start_str = week_start.isoformat()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0), COUNT(*) FROM transactions WHERE user_id = %s AND date >= %s AND date <= %s AND amount < 0 AND type != 'transfer'",
            (current_user.id, week_start_str, today_str)
        )
    else:
        cursor.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0), COUNT(*) FROM transactions WHERE user_id = ? AND date >= ? AND date <= ? AND amount < 0 AND type != 'transfer'",
            (current_user.id, week_start_str, today_str)
        )
    r = cursor.fetchone()
    this_week_spent = float(r[0] or 0)
    this_week_count = int(r[1] or 0)

    cursor.close()
    release_db(db)

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

    # Net worth trend — approximate monthly balance by walking backwards from current total
    nw_trend = []
    try:
        from dateutil.relativedelta import relativedelta as _rdelta
        current_nw = sum(float(accounts[a]['balance']) for a in accounts if accounts[a].get('active'))
        running = current_nw
        today_d = date.today()
        _nw_db = get_db()
        _nw_cur = _nw_db.cursor()
        for i in range(0, 4):
            m_start = today_d.replace(day=1) - _rdelta(months=i)
            m_end = today_d if i == 0 else (m_start + _rdelta(months=1)).replace(day=1) - timedelta(days=1)
            if USE_POSTGRES:
                _nw_cur.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = %s AND date >= %s AND date <= %s AND type != 'transfer'",
                    (current_user.id, m_start.isoformat(), m_end.isoformat())
                )
            else:
                _nw_cur.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND date >= ? AND date <= ? AND type != 'transfer'",
                    (current_user.id, m_start.isoformat(), m_end.isoformat())
                )
            net = float(_nw_cur.fetchone()[0] or 0)
            nw_trend.insert(0, {'month': m_start.strftime('%b'), 'value': round(running, 2)})
            running -= net
        _nw_cur.close()
        release_db(_nw_db)
    except Exception as _e:
        logger.debug(f"nw_trend error: {_e}")
        nw_trend = []

    cycle_start_day = get_budget_cycle_start(current_user.id)
    cycle_start_date, cycle_end_date = get_cycle_dates(cycle_start_day)
    monthly = calculate_monthly_spending(cycle_start_date, cycle_end_date)

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

    # Check if user has no accounts (show onboarding), or manually triggered via ?onboarding=1
    # Also check server-side dismissed flag so closing the modal persists across devices/browsers
    _ob_db = get_db(); _ob_cur = _ob_db.cursor()
    try:
        if USE_POSTGRES:
            _ob_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_dismissed BOOLEAN DEFAULT FALSE")
            _ob_cur.execute("SELECT onboarding_dismissed FROM users WHERE id = %s", (current_user.id,))
        else:
            _ob_cur.execute("SELECT onboarding_dismissed FROM users WHERE id = ?", (current_user.id,))
        _ob_row = _ob_cur.fetchone()
        _ob_dismissed = bool(_ob_row[0] if _ob_row else False)
        _ob_db.commit()
    except Exception:
        _ob_dismissed = False
    finally:
        _ob_cur.close(); release_db(_ob_db)
    show_onboarding = (len(active_accounts) == 0 and not _ob_dismissed) or request.args.get('onboarding') == '1'

    # --- Auto-apply scheduled bills/income ---
    pending_items = []
    try:
        auto_apply_enabled, auto_apply_confirm = get_auto_apply_settings(current_user.id)
        if auto_apply_enabled:
            # Run one-time backfill silently (inserts history, no balance change)
            run_auto_apply_backfill(current_user.id)
            # Get items due today (or overdue since last applied)
            pending = get_pending_auto_apply_items(current_user.id)
            if pending:
                if auto_apply_confirm:
                    # Pass to template for user confirmation
                    pending_items = pending
                else:
                    # Silent mode: apply immediately
                    apply_auto_items(current_user.id, pending)
                    # Refresh accounts/overview after applying
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
                    monthly = calculate_monthly_spending(cycle_start_date, cycle_end_date)
                    balances = []
                    for n in sorted(accounts, key=lambda x: x.lower()):
                        if accounts[n].get("active", True):
                            balances.append({
                                "name": n,
                                "balance": float(accounts[n].get("balance", 0.0)),
                                "type": accounts[n].get("type", ""),
                                "id": accounts[n].get("id"),
                                "include_in_overview": accounts[n].get("include_in_overview", True)
                            })
    except Exception as e:
        logger.debug(f"Auto-apply home check error: {e}")

    days_to_payday = (cycle_end_date - date.today()).days + 1

    return render_template(
        "index.html",
        message=request.args.get("msg", ""),
        accounts=active_accounts,
        overview=overview,
        balances=balances,
        monthly=monthly,
        show_onboarding=show_onboarding,
        user_verified=verified,
        today_spent=today_spent,
        today_count=today_count,
        this_week_spent=this_week_spent,
        this_week_count=this_week_count,
        nw_trend=nw_trend,
        pending_items=pending_items,
        cycle_start_date=cycle_start_date,
        cycle_end_date=cycle_end_date,
        days_to_payday=days_to_payday,
    )

# --- ONBOARDING DISMISS ---
@app.post("/onboarding/dismiss")
@login_required
def onboarding_dismiss():
    db = get_db(); cursor = db.cursor()
    try:
        if USE_POSTGRES:
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_dismissed BOOLEAN DEFAULT FALSE")
            cursor.execute("UPDATE users SET onboarding_dismissed = TRUE WHERE id = %s", (current_user.id,))
        else:
            cursor.execute("UPDATE users SET onboarding_dismissed = 1 WHERE id = ?", (current_user.id,))
        db.commit()
    except Exception as e:
        logger.debug(f"onboarding_dismiss error: {e}")
    finally:
        cursor.close(); release_db(db)
    return {"ok": True}

# --- AUTO-APPLY ROUTE ---
# Called via AJAX when user confirms pending scheduled items from the home page banner
@app.post("/auto-apply")
@login_required
def auto_apply():
    from flask import request as _req
    if _req.json is None:
        logger.error("auto_apply: request.json is None (bad Content-Type?)")
        return {"error": "Invalid request"}, 400
    if _req.json.get("csrf_token") != session.get("csrf_token"):
        logger.error("auto_apply: CSRF mismatch for user %s (session token: %s)", current_user.id, bool(session.get("csrf_token")))
        return {"error": "Invalid CSRF token"}, 403

    items = _req.json.get("items", [])
    if not items:
        return {"ok": True}

    # Validate structure — only accept keys we expect
    safe_items = []
    for item in items:
        try:
            safe_items.append({
                "type": str(item["type"]),
                "item_id": int(item["item_id"]),
                "name": str(item["name"]),
                "amount": float(item["amount"]),
                "account": str(item["account"]),
                "due_date": str(item["due_date"]),
            })
        except (KeyError, ValueError, TypeError):
            continue

    apply_auto_items(current_user.id, safe_items)
    return {"ok": True}


@app.post("/mark-bill-paid")
@login_required
def mark_bill_paid():
    from flask import request as _req
    from datetime import date as _date
    import calendar as _cal
    if _req.json is None or _req.json.get("csrf_token") != session.get("csrf_token"):
        return {"error": "Invalid CSRF token"}, 403
    try:
        bill_id = int(_req.json["bill_id"])
        name = str(_req.json["name"])
        amount = float(_req.json["amount"])
        account = str(_req.json["account"])
        day = int(_req.json["day"])
    except (KeyError, ValueError, TypeError):
        return {"error": "Invalid request"}, 400

    today = _date.today()
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    due_day = min(day, days_in_month)
    due_date_str = _date(today.year, today.month, due_day).isoformat()

    add_transaction(today.isoformat(), name, -abs(amount), account, current_user.id, type='bill', category='Bills')
    update_account_balance(account, -abs(amount), current_user.id)

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE scheduled_expenses SET last_applied = %s WHERE id = %s AND user_id = %s",
                       (due_date_str, bill_id, current_user.id))
    else:
        cursor.execute("UPDATE scheduled_expenses SET last_applied = ? WHERE id = ? AND user_id = ?",
                       (due_date_str, bill_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return {"ok": True}


@app.post("/dismiss-auto-apply")
@login_required
def dismiss_auto_apply():
    from flask import request as _req
    from datetime import date as _date
    if _req.json is None or _req.json.get("csrf_token") != session.get("csrf_token"):
        return {"error": "Invalid CSRF token"}, 403
    items = _req.json.get("items", [])
    today_str = _date.today().isoformat()
    db = get_db()
    cursor = db.cursor()
    for item in items:
        try:
            item_id = int(item["item_id"])
            item_type = str(item["type"])
        except (KeyError, ValueError, TypeError):
            continue
        if item_type == "bill":
            if USE_POSTGRES:
                cursor.execute("UPDATE scheduled_expenses SET last_applied = %s WHERE id = %s AND user_id = %s",
                               (today_str, item_id, current_user.id))
            else:
                cursor.execute("UPDATE scheduled_expenses SET last_applied = ? WHERE id = ? AND user_id = ?",
                               (today_str, item_id, current_user.id))
        elif item_type == "income":
            if USE_POSTGRES:
                cursor.execute("UPDATE income SET last_applied = %s WHERE id = %s AND user_id = %s",
                               (today_str, item_id, current_user.id))
            else:
                cursor.execute("UPDATE income SET last_applied = ? WHERE id = ? AND user_id = ?",
                               (today_str, item_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return {"ok": True}


# --- TRANSACTIONS PAGE ---
# Lists all transactions for the current user, newest first
@app.get("/transactions")
@login_required
def transactions():
    track('page_view.transactions')
    tx = get_recent_transactions(current_user.id)

    return render_template(
        "transactions.html",
        transactions=tx
    )

# --- BULK CATEGORIZE ---
@app.post("/transactions/bulk-categorize")
@login_required
def bulk_categorize():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return redirect(url_for('transactions'))
    tx_ids = request.form.getlist('tx_ids')
    category = request.form.get('category', '').strip()
    if not tx_ids or not category:
        return redirect(url_for('transactions'))
    from database import get_db, USE_POSTGRES, release_db
    db = get_db()
    cursor = db.cursor()
    for raw_id in tx_ids:
        try:
            tid = int(raw_id)
        except (ValueError, TypeError):
            continue
        if USE_POSTGRES:
            cursor.execute("UPDATE transactions SET category = %s WHERE id = %s AND user_id = %s", (category, tid, current_user.id))
        else:
            cursor.execute("UPDATE transactions SET category = ? WHERE id = ? AND user_id = ?", (category, tid, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for('transactions', msg='Categories updated'))


# --- ACTIONS PAGE ---
# Shows forms to add expenses, income, transfers, and investment updates
@app.get("/actions")
@login_required
def actions():
    track('page_view.actions')
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

    from models import get_recent_transactions
    all_tx = get_recent_transactions(current_user.id)
    recent_tx = all_tx[:5]
    try:
        bank_connected = _get_bank_connection(current_user.id) is not None
    except Exception:
        bank_connected = False
    return render_template("actions.html", accounts=accounts, investments=investments, message=request.args.get("msg", ""), today=date.today().isoformat(), recent_tx=recent_tx, bank_connected=bank_connected)

# --- FLOW PAGE ---
# Shows each account's monthly cash flow: bills paid, bills still to pay,
# income received, income still to receive, and a projected end-of-month balance
# Traffic light colour: green (safe), amber (<£100), red (goes negative)
@app.get("/flow")
@login_required
def flow():
    track('page_view.flow')
    today = date.today()
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
    paid_date_raw = (request.form.get("paid_date") or "").strip()
    try:
        paid_date_str = date.fromisoformat(paid_date_raw).isoformat()
    except ValueError:
        paid_date_str = date.today().isoformat()

    amount_override_raw = (request.form.get("amount_override") or "").strip()
    try:
        override = float(amount_override_raw)
        if override > 0:
            bill["amount"] = round(override, 2)
    except (ValueError, TypeError):
        pass

    add_transaction(paid_date_str, bill["name"], -bill["amount"], bill["account"], current_user.id, type="bill")
    update_account_balance(bill["account"], -bill["amount"], current_user.id)
    bust_forecast_cache(current_user.id)
    track('action.pay_bill')
    redirect_to = request.form.get("redirect_to") or url_for("flow")
    return redirect(f"{redirect_to}?msg={bill['name']}+—+£{bill['amount']:.2f}+paid.")

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
    paid_date_raw = (request.form.get("paid_date") or "").strip()
    try:
        paid_date_str = date.fromisoformat(paid_date_raw).isoformat()
    except ValueError:
        paid_date_str = date.today().isoformat()

    amount_override_raw = (request.form.get("amount_override") or "").strip()
    try:
        override = float(amount_override_raw)
        if override > 0:
            income["amount"] = round(override, 2)
    except (ValueError, TypeError):
        pass

    add_transaction(paid_date_str, income["name"], income["amount"], income["account"], current_user.id, type="income")
    update_account_balance(income["account"], income["amount"], current_user.id)
    bust_forecast_cache(current_user.id)
    track('action.receive_income')
    redirect_to = request.form.get("redirect_to") or url_for("flow")
    return redirect(f"{redirect_to}?msg={income['name']}+—+£{income['amount']:.2f}+received.")

# --- ADD EXPENSE ---
# Records a manual expense: negative amount stored in transactions, balance deducted
@app.post("/add-expense")
@login_required
def add_expense():

    description = (request.form.get("description") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    account = (request.form.get("account") or "").strip()
    category = (request.form.get("category") or "Other").strip()
    date_raw = (request.form.get("date") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    amount, err = validate_amount(amount_raw)
    if err:
        return redirect(url_for("actions", msg=err))

    amount = -abs(amount)

    try:
        from datetime import datetime as _dt
        tx_date = _dt.strptime(date_raw, '%Y-%m-%d').date().isoformat() if date_raw else date.today().isoformat()
    except ValueError:
        tx_date = date.today().isoformat()

    add_transaction(tx_date, description, amount, account, current_user.id, category=category)
    update_account_balance(account, amount, current_user.id)
    bust_forecast_cache(current_user.id)
    track('action.add_expense')
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
    date_raw = (request.form.get("date") or "").strip()

    if not description or not amount_raw or not account:
        return redirect(url_for("home", msg="Missing fields. Try again."))

    amount, err = validate_amount(amount_raw)
    if err:
        return redirect(url_for("actions", msg=err))

    amount = abs(amount)

    try:
        from datetime import datetime as _dt
        tx_date = _dt.strptime(date_raw, '%Y-%m-%d').date().isoformat() if date_raw else date.today().isoformat()
    except ValueError:
        tx_date = date.today().isoformat()

    add_transaction(tx_date, description, amount, account, current_user.id, type='income', category='Income')
    update_account_balance(account, amount, current_user.id)
    bust_forecast_cache(current_user.id)
    track('action.add_income')
    return redirect(
        url_for("actions", msg=f"Added income {description}: £{amount:.2f} to {account}")
    )

# --- QUICK ADD (AJAX) ---
# Minimal expense/income log from the home screen floating button — returns JSON
@app.post("/quick-add")
@login_required
def quick_add():
    amount_raw = (request.form.get("amount") or "").strip()
    description = (request.form.get("description") or "").strip() or "Quick expense"
    account = (request.form.get("account") or "").strip()
    tx_type = (request.form.get("type") or "expense").strip()
    category = (request.form.get("category") or "Other").strip()

    if not amount_raw or not account:
        return {"ok": False, "error": "Missing amount or account"}, 400

    amount, err = validate_amount(amount_raw)
    if err:
        return {"ok": False, "error": err}, 400

    today_str = date.today().isoformat()
    if tx_type == "income":
        amount = abs(amount)
        add_transaction(today_str, description, amount, account, current_user.id, type="income")
        update_account_balance(account, amount, current_user.id)
        track('action.quick_add_income')
    else:
        amount = -abs(amount)
        add_transaction(today_str, description, amount, account, current_user.id, category=category)
        update_account_balance(account, amount, current_user.id)
        track('action.quick_add_expense')

    bust_forecast_cache(current_user.id)
    return {"ok": True, "amount": abs(amount), "account": account, "type": tx_type}


# --- QUICK ADJUST (AJAX) ---
# Balance adjustment from the home screen A-button.
# Accepts new_balance + old_balance, sets the account to new_balance,
# logs the delta as a transaction, and records in balance_adjustments for hourly forecast.
@app.post("/quick-adjust")
@login_required
def quick_adjust():
    from datetime import datetime as dt
    account = (request.form.get("account") or "").strip()
    category = (request.form.get("category") or "Various").strip()

    try:
        new_balance = float(request.form.get("new_balance", ""))
        old_balance = float(request.form.get("old_balance", ""))
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid balance values"}, 400

    if not account:
        return {"ok": False, "error": "Missing account"}, 400

    delta = round(new_balance - old_balance, 2)
    if abs(delta) < 0.001:
        return {"ok": False, "error": "Balance is unchanged"}, 400

    db = get_db()
    cursor = db.cursor()
    try:
        # Ensure balance_adjustments table exists
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    account TEXT NOT NULL,
                    old_balance NUMERIC(12,2) NOT NULL,
                    new_balance NUMERIC(12,2) NOT NULL,
                    delta NUMERIC(12,2) NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various',
                    recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account TEXT NOT NULL,
                    old_balance REAL NOT NULL,
                    new_balance REAL NOT NULL,
                    delta REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various',
                    recorded_at TEXT NOT NULL
                )
            """)

        now_str = dt.utcnow().isoformat()

        if USE_POSTGRES:
            cursor.execute("SELECT id FROM accounts WHERE name=%s AND user_id=%s", (account, current_user.id))
        else:
            cursor.execute("SELECT id FROM accounts WHERE name=? AND user_id=?", (account, current_user.id))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            release_db(db)
            return {"ok": False, "error": "Account not found"}, 400

        # Set account to new_balance directly
        if USE_POSTGRES:
            cursor.execute("UPDATE accounts SET balance=%s WHERE name=%s AND user_id=%s",
                           (new_balance, account, current_user.id))
            cursor.execute("""
                INSERT INTO balance_adjustments (user_id, account, old_balance, new_balance, delta, category)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (current_user.id, account, old_balance, new_balance, delta, category))
        else:
            cursor.execute("UPDATE accounts SET balance=? WHERE name=? AND user_id=?",
                           (new_balance, account, current_user.id))
            cursor.execute("""
                INSERT INTO balance_adjustments (user_id, account, old_balance, new_balance, delta, category, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (current_user.id, account, old_balance, new_balance, delta, category, now_str))

        # Log as transaction for forecast history
        today_str = date.today().isoformat()
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (today_str, "Balance adjustment", delta, account, current_user.id, "adjustment", category)
            )
        else:
            cursor.execute(
                "INSERT INTO transactions (date, description, amount, account, user_id, type, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (today_str, "Balance adjustment", delta, account, current_user.id, "adjustment", category)
            )

        db.commit()
    except Exception as e:
        db.rollback()
        logger.debug(f"quick_adjust error: {e}")
        cursor.close()
        release_db(db)
        return {"ok": False, "error": "Server error"}, 500

    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    track('action.balance_adjust')
    return {"ok": True, "old_balance": old_balance, "new_balance": new_balance, "delta": delta, "account": account}


# --- BALANCE ADJUSTMENTS API ---
# Returns timestamped balance adjustments for the current user — used by forecast chart for hourly markers
@app.get("/api/balance-adjustments")
@login_required
def api_balance_adjustments():
    days = min(int(request.args.get("days", 90)), 365)
    from datetime import datetime as dt
    since = (dt.utcnow().date() - timedelta(days=days)).isoformat()
    db = get_db()
    cursor = db.cursor()
    try:
        if USE_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL,
                    account TEXT NOT NULL, old_balance NUMERIC(12,2) NOT NULL,
                    new_balance NUMERIC(12,2) NOT NULL, delta NUMERIC(12,2) NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various',
                    recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                SELECT account, old_balance, new_balance, delta, category, recorded_at
                FROM balance_adjustments
                WHERE user_id=%s AND recorded_at >= %s
                ORDER BY recorded_at ASC
            """, (current_user.id, since))
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    account TEXT NOT NULL, old_balance REAL NOT NULL,
                    new_balance REAL NOT NULL, delta REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Various', recorded_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                SELECT account, old_balance, new_balance, delta, category, recorded_at
                FROM balance_adjustments
                WHERE user_id=? AND recorded_at >= ?
                ORDER BY recorded_at ASC
            """, (current_user.id, since))
        rows = cursor.fetchall()
        db.commit()
    except Exception as e:
        logger.debug(f"api_balance_adjustments error: {e}")
        rows = []
    cursor.close()
    release_db(db)
    result = [
        {"account": r[0], "old_balance": float(r[1]), "new_balance": float(r[2]),
         "delta": float(r[3]), "category": r[4], "recorded_at": str(r[5])}
        for r in rows
    ]
    return {"ok": True, "adjustments": result}


# --- CALENDAR PAGE ---
# Shows monthly transaction calendar — day totals rendered client-side, detail loaded via AJAX
@app.get("/calendar")
@login_required
def calendar_view():
    track('page_view.calendar')

    month_str = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        year, month = int(month_str[:4]), int(month_str[5:7])
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, IndexError):
        year, month = date.today().year, date.today().month

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("""
            SELECT date,
                   COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS spent,
                   COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income,
                   COUNT(*) AS count
            FROM transactions
            WHERE user_id = %s AND date >= %s AND date <= %s
            GROUP BY date ORDER BY date
        """, (current_user.id, first_day.isoformat(), last_day.isoformat()))
    else:
        cursor.execute("""
            SELECT date,
                   COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS spent,
                   COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income,
                   COUNT(*) AS count
            FROM transactions
            WHERE user_id = ? AND date >= ? AND date <= ?
            GROUP BY date ORDER BY date
        """, (current_user.id, first_day.isoformat(), last_day.isoformat()))
    cols = [d[0] for d in cursor.description]
    day_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    release_db(db)

    day_data = {}
    for row in day_rows:
        day_data[str(row["date"])] = {
            "spent": round(float(row["spent"]), 2),
            "income": round(float(row["income"]), 2),
            "count": int(row["count"])
        }

    # Day-of-week averages: look back 12 weeks for enough data
    twelve_weeks_ago = (date.today() - timedelta(weeks=12)).isoformat()
    db2 = get_db()
    cursor2 = db2.cursor()
    if USE_POSTGRES:
        cursor2.execute("""
            SELECT EXTRACT(DOW FROM date::date) AS dow,
                   AVG(ABS(amount)) AS avg_spent,
                   COUNT(*) AS occurrences
            FROM transactions
            WHERE user_id = %s AND amount < 0 AND date >= %s
            GROUP BY dow ORDER BY dow
        """, (current_user.id, twelve_weeks_ago))
    else:
        cursor2.execute("""
            SELECT CAST(strftime('%w', date) AS INTEGER) AS dow,
                   AVG(ABS(amount)) AS avg_spent,
                   COUNT(*) AS occurrences
            FROM transactions
            WHERE user_id = ? AND amount < 0 AND date >= ?
            GROUP BY dow ORDER BY dow
        """, (current_user.id, twelve_weeks_ago))
    dow_rows = cursor2.fetchall()
    cursor2.close()
    release_db(db2)

    # Postgres DOW: 0=Sun, 1=Mon ... 6=Sat — remap to Mon=0..Sun=6 to match JS Date
    # SQLite strftime %w: 0=Sun, 1=Mon ... 6=Sat — same remap
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_avgs = [0.0] * 7
    for r in dow_rows:
        dow_raw = int(r[0])  # 0=Sun in both Postgres and SQLite
        avg = round(float(r[1]), 2)
        # Convert Sun=0 → index 6, Mon=1 → index 0, ..., Sat=6 → index 5
        idx = (dow_raw - 1) % 7
        dow_avgs[idx] = avg

    prev_month = f"{year-1}-12" if month == 1 else f"{year}-{month-1:02d}"
    next_month = f"{year+1}-01" if month == 12 else f"{year}-{month+1:02d}"

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_name=first_day.strftime("%B %Y"),
        first_weekday=first_day.weekday(),
        days_in_month=calendar.monthrange(year, month)[1],
        day_data=json.dumps(day_data),
        dow_labels=json.dumps(dow_labels),
        dow_avgs=json.dumps(dow_avgs),
        prev_month=prev_month,
        next_month=next_month,
        today=date.today().isoformat()
    )


# --- CALENDAR DAY DETAIL (AJAX) ---
@app.get("/calendar/day")
@login_required
def calendar_day():
    day_str = request.args.get("date", "")
    try:
        day = date.fromisoformat(day_str)
    except ValueError:
        return {"transactions": []}, 400

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT description, amount, account, category FROM transactions WHERE user_id = %s AND date = %s ORDER BY id DESC",
            (current_user.id, day.isoformat())
        )
    else:
        cursor.execute(
            "SELECT description, amount, account, category FROM transactions WHERE user_id = ? AND date = ? ORDER BY id DESC",
            (current_user.id, day.isoformat())
        )
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    release_db(db)

    return {"transactions": [
        {"description": r["description"], "amount": float(r["amount"]), "account": r["account"], "category": r["category"] or "Other"}
        for r in rows
    ]}


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
    bust_forecast_cache(current_user.id)
    track('action.transfer')
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


# --- DELETE TRANSACTION ---
# Removes a transaction record only — does NOT touch account balances
@app.post("/transactions/delete")
@login_required
def transaction_delete():
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("transactions"))
    tx_id = request.form.get("tx_id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tx_id, current_user.id))
    else:
        cursor.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("transactions", msg="Transaction deleted."))


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

    future_events = []
    for e in future_events_raw:
        try:
            future_events.append({
                "date": date.fromisoformat(e["date"]),
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

    track('action.afford_check')
    return render_template(
        "index.html",
        message="",
        accounts=[a for a in accounts if accounts[a]["active"]],
        balances=[{"name":a,"balance":accounts[a]["balance"],"type":accounts[a]["type"]} for a in accounts if accounts[a]["active"]],
        overview=calculate_financial_overview(accounts),
        afford_results=results,
        afford_amount=amount,
        recommendation=recommendation,
        monthly=calculate_monthly_spending(),
    )

# --- FINANCIAL SNAPSHOT API ---
# Returns projected balances, income arriving, and bills due up to a given number of days ahead.
# Used by the Financial Position card on the home page.
@app.get("/api/snapshot")
@login_required
def api_snapshot():
    try:
        days = int(request.args.get('days', 30))
    except (ValueError, TypeError):
        days = 30
    days = max(1, min(90, days))

    today = date.today()
    target = today + timedelta(days=days)

    from database import get_db, USE_POSTGRES
    db = get_db()
    cursor = db.cursor()

    accounts_rows = get_active_accounts(current_user.id)
    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": float(r["balance"]),
            "type": r["type"],
        }

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    scheduled = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM income WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM income WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    income_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if USE_POSTGRES:
        cursor.execute(
            "SELECT * FROM future_events WHERE user_id = %s AND date >= %s AND date <= %s",
            (current_user.id, today.isoformat(), target.isoformat())
        )
    else:
        cursor.execute(
            "SELECT * FROM future_events WHERE user_id = ? AND date >= ? AND date <= ?",
            (current_user.id, today.isoformat(), target.isoformat())
        )
    cols = [d[0] for d in cursor.description]
    future_events_raw = [dict(zip(cols, row)) for row in cursor.fetchall()]
    cursor.close()
    release_db(db)

    future_events = []
    for e in future_events_raw:
        try:
            future_events.append({
                "date": date.fromisoformat(str(e["date"])),
                "name": e["name"],
                "amount": float(e["amount"]),
                "account": e["account"]
            })
        except (ValueError, KeyError):
            continue

    simulated = {name: float(info["balance"]) for name, info in accounts.items()}
    income_arriving = []
    bills_due = []

    sim_day = today + timedelta(days=1)
    while sim_day <= target:
        day_str = f"{sim_day.day} {sim_day.strftime('%b')}"

        # Income
        for row in income_rows:
            freq = row.get("frequency", "monthly")
            acc = row.get("account", "")
            amt = float(row["amount"])
            applies = False
            if freq == "monthly" and row.get("day") == sim_day.day:
                applies = True
            elif freq == "weekly" and sim_day.weekday() == int(row.get("weekly_day") if row.get("weekly_day") is not None else 4):
                applies = True
            if applies:
                income_arriving.append({"name": row["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc})
                if acc in simulated:
                    simulated[acc] += amt

        # Scheduled expenses
        for expense in scheduled:
            exp_day = expense.get("day")
            if exp_day is None:
                continue
            freq = expense.get("frequency", "monthly")
            acc = expense.get("account", "")
            amt = float(expense["amount"])
            applies = False
            if freq == "monthly" and exp_day == sim_day.day:
                applies = True
            elif freq == "yearly":
                exp_month = expense.get("month")
                if exp_day == sim_day.day and exp_month == sim_day.month:
                    applies = True
            if applies:
                bills_due.append({"name": expense["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc})
                if acc in simulated:
                    simulated[acc] -= amt

        # Future events
        for event in future_events:
            if event["date"] == sim_day:
                acc = event["account"]
                amt = float(event["amount"])
                bills_due.append({"name": event["name"], "amount": amt, "date": day_str, "account": acc})
                if acc in simulated:
                    simulated[acc] -= amt

        sim_day += timedelta(days=1)

    return jsonify({
        "date": f"{target.day} {target.strftime('%b %Y')}",
        "days": days,
        "accounts": {
            name: {
                "balance_today": round(accounts[name]["balance"], 2),
                "balance_on_date": round(simulated[name], 2),
                "change": round(simulated[name] - accounts[name]["balance"], 2),
                "type": accounts[name]["type"]
            }
            for name in accounts
        },
        "income_arriving": income_arriving,
        "bills_due": bills_due
    })


# --- PROFILE PANEL ROUTES ---

@app.post("/profile/update-name")
@login_required
def profile_update_name():
    from database import get_db, USE_POSTGRES, release_db
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return jsonify({'error': 'Invalid request'}), 403
    name = request.form.get('display_name', '').strip()
    if not name or len(name) > 60:
        return jsonify({'error': 'Name must be 1–60 characters'}), 400
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET display_name=%s WHERE id=%s", (name, current_user.id))
    else:
        cursor.execute("UPDATE users SET display_name=? WHERE id=?", (name, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return jsonify({'ok': True, 'display_name': name})

@app.post("/profile/update-avatar")
@login_required
def profile_update_avatar():
    from database import get_db, USE_POSTGRES, release_db
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return jsonify({'error': 'Invalid request'}), 403
    avatar = request.form.get('avatar', '').strip()
    allowed = ['🐻','🦊','🐼','🐨','🦁','🐯','🐸','🐧','🦋','🌸','⭐','🌙','🔥','💎','🚀','🎯','🎸','🎨','🏔️','🌊']
    if avatar not in allowed:
        return jsonify({'error': 'Invalid avatar'}), 400
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET avatar=%s WHERE id=%s", (avatar, current_user.id))
    else:
        cursor.execute("UPDATE users SET avatar=? WHERE id=?", (avatar, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return jsonify({'ok': True, 'avatar': avatar})

@app.post("/profile/send-feedback")
@login_required
def profile_send_feedback():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return jsonify({'error': 'Invalid request'}), 403
    message = request.form.get('message', '').strip()
    if not message or len(message) > 2000:
        return jsonify({'error': 'Message must be 1–2000 characters'}), 400
    import requests as _req_lib
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
    payload = {
        "sender": {"name": "Spendara Feedback", "email": "noreply@spendara.co.uk"},
        "to": [{"email": "hello@spendara.co.uk"}],
        "replyTo": {"email": current_user.email},
        "subject": f"Feedback from {current_user.email}",
        "textContent": f"From: {current_user.email}\nUser ID: #{current_user.id:05d}\n\n{message}"
    }
    try:
        resp = _req_lib.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            timeout=8
        )
        if resp.status_code in (200, 201):
            return jsonify({'ok': True})
        return jsonify({'error': 'Could not send, please try again'}), 500
    except Exception:
        return jsonify({'error': 'Could not send, please try again'}), 500


# --- SETTINGS PAGE ---
# Plan (billing) and Danger zone only — day-to-day management moved to /manage
@app.get("/settings")
@login_required
def settings():
    track('page_view.settings')
    is_pro = user_is_pro()
    auto_apply_enabled, auto_apply_confirm = get_auto_apply_settings(current_user.id)
    budget_cycle_start = get_budget_cycle_start(current_user.id)
    # Notification digest preference (column added on first save if missing)
    notification_digest = 'off'
    try:
        from database import get_db, USE_POSTGRES, release_db
        _db = get_db()
        _cur = _db.cursor()
        if USE_POSTGRES:
            _cur.execute("SELECT notification_digest FROM users WHERE id = %s", (current_user.id,))
        else:
            _cur.execute("SELECT notification_digest FROM users WHERE id = ?", (current_user.id,))
        _row = _cur.fetchone()
        if _row and _row[0]:
            notification_digest = _row[0]
        _cur.close()
        release_db(_db)
    except Exception:
        pass
    return render_template("settings.html",
        is_pro=is_pro,
        message=request.args.get("msg", ""),
        auto_apply_enabled=auto_apply_enabled,
        auto_apply_confirm=auto_apply_confirm,
        budget_cycle_start=budget_cycle_start,
        notification_digest=notification_digest,
    )


@app.post("/settings/save-cycle")
@login_required
def settings_save_cycle():
    from database import get_db, USE_POSTGRES
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("settings"))
    try:
        start_day = max(1, min(28, int(request.form.get("budget_cycle_start", 1))))
    except (ValueError, TypeError):
        start_day = 1
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET budget_cycle_start = %s WHERE id = %s", (start_day, current_user.id))
    else:
        cursor.execute("UPDATE users SET budget_cycle_start = ? WHERE id = ?", (start_day, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Budget cycle updated.", tab="display"))


@app.post("/settings/save-automation")
@login_required
def settings_save_automation():
    from database import get_db, USE_POSTGRES
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("settings"))
    enabled = 1 if request.form.get("auto_apply_enabled") else 0
    confirm = 1 if request.form.get("auto_apply_confirm") else 0
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET auto_apply_enabled = %s, auto_apply_confirm = %s WHERE id = %s",
                       (enabled, confirm, current_user.id))
    else:
        cursor.execute("UPDATE users SET auto_apply_enabled = ?, auto_apply_confirm = ? WHERE id = ?",
                       (enabled, confirm, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Automation settings saved.", tab="display"))


@app.post("/settings/save-notifications")
@login_required
def settings_save_notifications():
    from database import get_db, USE_POSTGRES, release_db
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("settings"))
    digest = request.form.get("notification_digest", "off")
    if digest not in ("off", "weekly", "monthly"):
        digest = "off"
    if digest != "off" and not user_is_pro():
        digest = "off"
    db = get_db()
    cursor = db.cursor()
    try:
        if USE_POSTGRES:
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_digest VARCHAR(10) DEFAULT 'off'")
            cursor.execute("UPDATE users SET notification_digest = %s WHERE id = %s", (digest, current_user.id))
        else:
            cursor.execute("UPDATE users SET notification_digest = ? WHERE id = ?", (digest, current_user.id))
        db.commit()
    except Exception as e:
        logger.debug(f"save_notifications error: {e}")
        db.rollback()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Notification preferences saved.", tab="display"))


@app.get("/manage")
@login_required
def manage():
    track('page_view.settings')
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

    return render_template("manage.html",
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
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        balance = float(balance)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid balance."))

    # Free tier limit: max 2 accounts
    if not user_is_pro():
        existing = get_active_accounts(current_user.id)
        if len(existing) >= 2:
            return redirect(url_for("manage", msg="FREE_LIMIT_ACCOUNTS"))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id) VALUES (%s, %s, %s, 1, %s)", (name, balance, acc_type, current_user.id))
    else:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id) VALUES (?, ?, ?, 1, ?)", (name, balance, acc_type, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Account '{name}' created."))

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
    return redirect(url_for("manage", msg=f"Account '{name}' deactivated."))

@app.post("/settings/edit-account")
@login_required
def settings_edit_account():
    account_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    acc_type = (request.form.get("type") or "").strip()
    balance = (request.form.get("balance") or "").strip()

    if not name or not acc_type or not balance:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        balance = float(balance)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid balance."))

    savings_rate_raw = request.form.get("savings_rate", "").strip()
    try:
        savings_rate = max(0.0, min(100.0, float(savings_rate_raw))) if savings_rate_raw else 0.0
    except ValueError:
        savings_rate = 0.0

    db = get_db()
    cursor = db.cursor()
    try:
        # Fetch current balance to detect changes
        if USE_POSTGRES:
            cursor.execute("SELECT balance FROM accounts WHERE id=%s AND user_id=%s", (account_id, current_user.id))
        else:
            cursor.execute("SELECT balance FROM accounts WHERE id=? AND user_id=?", (account_id, current_user.id))
        row = cursor.fetchone()
        old_balance = float(row[0]) if row else None

        if USE_POSTGRES:
            cursor.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS savings_rate DECIMAL(5,2) DEFAULT 0")
            cursor.execute("UPDATE accounts SET name=%s, type=%s, balance=%s, savings_rate=%s WHERE id=%s AND user_id=%s", (name, acc_type, balance, savings_rate, account_id, current_user.id))
        else:
            cursor.execute("UPDATE accounts SET name=?, type=?, balance=? WHERE id=? AND user_id=?", (name, acc_type, balance, account_id, current_user.id))
        db.commit()

        # Log balance change as a transaction for forecast tracking
        if old_balance is not None:
            delta = round(balance - old_balance, 2)
            if abs(delta) > 0.001:
                today_str = date.today().isoformat()
                add_transaction(today_str, "Balance adjustment (manage)", delta, name, current_user.id, type="adjustment", category="Various")
                bust_forecast_cache(current_user.id)
    except Exception as e:
        db.rollback()
        logger.debug(f"edit_account error: {e}")
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", msg="Account updated."))

@app.post("/settings/add-bill")
@login_required
def settings_add_bill():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    account = (request.form.get("account") or "").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    month_raw = (request.form.get("month") or "").strip()

    if not name or not amount or not day or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    if frequency == "yearly" and not month_raw:
        return redirect(url_for("manage", msg="Please select a month for yearly bills."))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err))
    bill_month = int(month_raw) if month_raw and frequency == "yearly" else None

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, month) VALUES (%s, %s, %s, %s, %s, %s, %s)", (name, amount, day, account, current_user.id, frequency, bill_month))
    else:
        cursor.execute("INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, month) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, amount, day, account, current_user.id, frequency, bill_month))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Bill '{name}' added."))

@app.post("/settings/edit-bill")
@login_required
def settings_edit_bill():
    bill_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "").strip()
    account = (request.form.get("account") or "").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    month_raw = (request.form.get("month") or "").strip()

    if not name or not amount or not day or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err))
    bill_month = int(month_raw) if month_raw and frequency == "yearly" else None

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE scheduled_expenses SET name=%s, amount=%s, day=%s, account=%s, frequency=%s, month=%s WHERE id=%s AND user_id=%s", (name, amount, day, account, frequency, bill_month, bill_id, current_user.id))
    else:
        cursor.execute("UPDATE scheduled_expenses SET name=?, amount=?, day=?, account=?, frequency=?, month=? WHERE id=? AND user_id=?", (name, amount, day, account, frequency, bill_month, bill_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Bill updated."))

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
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Bill deleted."))

@app.post("/settings/add-savings-rule")
@login_required
def settings_add_savings_rule():
    if not user_is_pro():
        return redirect(url_for("manage", msg="PRO_REQUIRED"))
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    day = (request.form.get("day") or "1").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()

    if not name or not amount or not from_account or not to_account:
        return redirect(url_for("manage", msg="Missing fields."))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    else:
        cursor.execute("INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, amount, day, frequency, from_account, to_account, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Savings rule '{name}' added."))

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
        return redirect(url_for("manage", msg="Missing fields."))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE savings_rules SET name=%s, amount=%s, day=%s, frequency=%s, from_account=%s, to_account=%s WHERE id=%s AND user_id=%s", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    else:
        cursor.execute("UPDATE savings_rules SET name=?, amount=?, day=?, frequency=?, from_account=?, to_account=? WHERE id=? AND user_id=?", (name, amount, day, frequency, from_account, to_account, rule_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Savings rule updated."))

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
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Savings rule deleted."))

@app.post("/settings/add-future-event")
@login_required
def settings_add_future_event():
    if not user_is_pro():
        return redirect(url_for("manage", msg="PRO_REQUIRED"))
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    date_input = (request.form.get("date") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not date_input or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO future_events (name, amount, date, account, user_id) VALUES (%s, %s, %s, %s, %s)", (name, amount, date_input, account, current_user.id))
    else:
        cursor.execute("INSERT INTO future_events (name, amount, date, account, user_id) VALUES (?, ?, ?, ?, ?)", (name, amount, date_input, account, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Future event '{name}' added."))

@app.post("/settings/edit-future-event")
@login_required
def settings_edit_future_event():
    event_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    date_input = (request.form.get("date") or "").strip()
    account = (request.form.get("account") or "").strip()

    if not name or not amount or not date_input or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE future_events SET name=%s, amount=%s, date=%s, account=%s WHERE id=%s AND user_id=%s", (name, amount, date_input, account, event_id, current_user.id))
    else:
        cursor.execute("UPDATE future_events SET name=?, amount=?, date=?, account=? WHERE id=? AND user_id=?", (name, amount, date_input, account, event_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Future event updated."))

@app.post("/settings/add-income")
@login_required
def settings_add_income():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    frequency = (request.form.get("frequency") or "").strip()
    account = (request.form.get("account") or "").strip()
    day_raw = (request.form.get("day") or "1").strip()
    try:
        weekly_day = max(0, min(6, int(request.form.get("weekly_day") or 4)))
    except ValueError:
        weekly_day = 4

    if not name or not amount or not frequency or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))
    try:
        day = max(1, min(31, int(day_raw)))
    except ValueError:
        day = 1

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("ALTER TABLE income ADD COLUMN IF NOT EXISTS weekly_day INTEGER DEFAULT 4")
        cursor.execute("INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day) VALUES (%s, %s, %s, %s, %s, %s, %s)", (name, amount, frequency, account, current_user.id, day, weekly_day))
    else:
        try:
            cursor.execute("ALTER TABLE income ADD COLUMN weekly_day INTEGER DEFAULT 4")
        except Exception:
            pass
        cursor.execute("INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, amount, frequency, account, current_user.id, day, weekly_day))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Income source '{name}' added."))

@app.post("/settings/edit-income")
@login_required
def settings_edit_income():
    income_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    frequency = (request.form.get("frequency") or "").strip()
    account = (request.form.get("account") or "").strip()
    day_raw = (request.form.get("day") or "1").strip()
    try:
        weekly_day = max(0, min(6, int(request.form.get("weekly_day") or 4)))
    except ValueError:
        weekly_day = 4

    if not name or not amount or not frequency or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))
    try:
        day = max(1, min(31, int(day_raw)))
    except ValueError:
        day = 1

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("ALTER TABLE income ADD COLUMN IF NOT EXISTS weekly_day INTEGER DEFAULT 4")
        cursor.execute("UPDATE income SET name=%s, amount=%s, frequency=%s, account=%s, day=%s, weekly_day=%s WHERE id=%s AND user_id=%s",
                       (name, amount, frequency, account, day, weekly_day, income_id, current_user.id))
    else:
        try:
            cursor.execute("ALTER TABLE income ADD COLUMN weekly_day INTEGER DEFAULT 4")
        except Exception:
            pass
        cursor.execute("UPDATE income SET name=?, amount=?, frequency=?, account=?, day=?, weekly_day=? WHERE id=? AND user_id=?",
                       (name, amount, frequency, account, day, weekly_day, income_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Income updated."))

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
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Income source deleted."))

@app.post("/settings/add-investment")
@login_required
def settings_add_investment():
    if not user_is_pro():
        return redirect(url_for("manage", msg="PRO_REQUIRED"))
    name = (request.form.get("name") or "").strip()
    inv_type = (request.form.get("type") or "").strip()
    initial_amount = (request.form.get("initial_amount") or "").strip()
    inv_date = (request.form.get("date") or "").strip()

    if not name or not inv_type or not initial_amount or not inv_date:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        initial_amount = float(initial_amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))

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
    return redirect(url_for("manage", msg=f"Investment '{name}' added."))


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
    return redirect(url_for("manage", msg="Investment deleted."))


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
    track('action.investment_update')
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
    import json
    import time

    track('page_view.forecast')
    today = date.today()
    is_pro = user_is_pro()
    forecast_days = 90 if is_pro else 30
    cache_key = f"forecast_{current_user.id}_{today.isoformat()}_{forecast_days}"
    force_refresh = request.args.get("refresh") == "1"

    # return cached result if still fresh (skip if ?refresh=1 after marking paid)
    if not force_refresh and cache_key in forecast_cache:
        cached_at, cached_data = forecast_cache[cache_key]
        if time.time() - cached_at < FORECAST_CACHE_TTL:
            return render_template(
                "forecast.html",
                snapshots=cached_data["snapshots"],
                account_names=cached_data["account_names"],
                account_types=cached_data.get("account_types", "{}"),
                initial_balances=cached_data.get("initial_balances", "{}"),
                upcoming=cached_data.get("upcoming", "[]"),
                hist_snapshots=cached_data.get("hist_snapshots", "[]"),
                savings_rates=cached_data.get("savings_rates", "{}"),
                is_pro=cached_data.get("is_pro", True),
                message=request.args.get("msg", ""),
                today=today.isoformat()
            )

    accounts_rows = get_active_accounts(current_user.id)
    accounts = {}
    savings_rates = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": float(r["balance"]),
            "type": r["type"],
            "active": True
        }
        try:
            savings_rates[r["name"]] = float(r["savings_rate"] or 0)
        except (KeyError, TypeError):
            savings_rates[r["name"]] = 0.0

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

    # Historical transactions for chart scrollback (up to 90 days)
    hist_cutoff = today - timedelta(days=90)
    if USE_POSTGRES:
        cursor.execute("""
            SELECT date::text, account, SUM(amount) as net
            FROM transactions
            WHERE user_id = %s AND date >= %s AND date <= %s
            GROUP BY date, account
        """, (current_user.id, hist_cutoff.isoformat(), today.isoformat()))
    else:
        cursor.execute("""
            SELECT date, account, SUM(amount) as net
            FROM transactions
            WHERE user_id = ? AND date >= ? AND date <= ?
            GROUP BY date, account
        """, (current_user.id, hist_cutoff.isoformat(), today.isoformat()))
    hist_tx_rows = cursor.fetchall()

    cursor.close()
    release_db(db)

    future_events = []
    for e in future_events_raw:
        try:
            future_events.append({
                "date": date.fromisoformat(str(e["date"])),
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
    initial_balances = {name: round(simulated[name], 2) for name in account_names}

    # Build historical snapshots by walking backwards from today's current balances
    from collections import defaultdict
    daily_nets: dict = defaultdict(dict)
    earliest_date_str = None
    for row in hist_tx_rows:
        d_str = str(row[0])
        acc_name = row[1]
        net_val = float(row[2])
        daily_nets[d_str][acc_name] = daily_nets[d_str].get(acc_name, 0.0) + net_val
        if earliest_date_str is None or d_str < earliest_date_str:
            earliest_date_str = d_str

    hist_snapshots = []
    if earliest_date_str:
        hist_balances = {name: info["balance"] for name, info in accounts.items()}
        earliest_date_obj = date.fromisoformat(earliest_date_str)
        d_ptr = today - timedelta(days=1)
        while d_ptr >= earliest_date_obj:
            next_d_str = (d_ptr + timedelta(days=1)).isoformat()
            for acc_name, net_val in daily_nets.get(next_d_str, {}).items():
                if acc_name in hist_balances:
                    hist_balances[acc_name] -= net_val
            snap = {"date": d_ptr.isoformat(), "historical": True}
            for acc_n in account_names:
                snap[acc_n] = round(hist_balances.get(acc_n, 0.0), 2)
            hist_snapshots.append(snap)
            d_ptr -= timedelta(days=1)
        hist_snapshots.reverse()

    # Start with today's actual balances as day 0
    today_snapshot = {"date": today.isoformat()}
    for acc in account_names:
        today_snapshot[acc] = round(simulated[acc], 2)
    snapshots = [today_snapshot]

    for day_offset in range(1, forecast_days + 1):
        sim_day = today + timedelta(days=day_offset)

        for inc in income_rows:
            if inc["frequency"] == "weekly" and inc["account"] in simulated:
                pay_weekday = int(inc.get("weekly_day") if inc.get("weekly_day") is not None else 4)
                if sim_day.weekday() == pay_weekday:
                    simulated[inc["account"]] += float(inc["amount"])

        for inc in income_rows:
            if inc["frequency"] == "monthly" and inc["account"] in simulated:
                pay_day = int(inc.get("day") or 1)
                if sim_day.day == pay_day:
                    simulated[inc["account"]] += float(inc["amount"])

        for expense in scheduled:
            freq = expense.get("frequency") or "monthly"
            if freq == "yearly":
                # Fire once a year on the specific day+month
                if expense["day"] == sim_day.day and expense.get("month") == sim_day.month and expense["account"] in simulated:
                    simulated[expense["account"]] -= float(expense["amount"])
            else:
                # Monthly: fire on the given day every month
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
    # Map of account name -> type so the frontend can filter by current/savings/cash
    account_types = {name: accounts[name]["type"] for name in account_names}
    account_types_json = json.dumps(account_types)
    initial_balances_json = json.dumps(initial_balances)

    # Build upcoming bills/income for the forecast horizon
    upcoming_items = []
    end_date = today + timedelta(days=forecast_days)

    for bill in scheduled:
        if bill.get("day") is None:
            continue
        freq = bill.get("frequency") or "monthly"
        if freq == "yearly":
            ann_month = bill.get("month")
            if ann_month:
                for year in [today.year, today.year + 1]:
                    try:
                        d = date(year, ann_month, bill["day"])
                        if today <= d <= end_date:
                            upcoming_items.append({"date": d.isoformat(), "name": bill["name"], "amount": float(bill["amount"]), "account": bill["account"], "type": "bill", "id": bill["id"]})
                    except ValueError:
                        pass
        else:
            m_year, m_month = today.year, today.month
            for _ in range(4):
                max_day = calendar.monthrange(m_year, m_month)[1]
                bill_day = min(bill["day"], max_day)
                try:
                    occurrence = date(m_year, m_month, bill_day)
                    if today <= occurrence <= end_date:
                        upcoming_items.append({"date": occurrence.isoformat(), "name": bill["name"], "amount": float(bill["amount"]), "account": bill["account"], "type": "bill", "id": bill["id"]})
                except ValueError:
                    pass
                m_month += 1
                if m_month > 12:
                    m_month = 1
                    m_year += 1

    for inc in income_rows:
        freq = inc.get("frequency") or "monthly"
        if freq == "weekly":
            pay_weekday = int(inc.get("weekly_day") if inc.get("weekly_day") is not None else 4)
            d = today
            while d <= end_date:
                if d.weekday() == pay_weekday:
                    upcoming_items.append({"date": d.isoformat(), "name": inc["name"], "amount": float(inc["amount"]), "account": inc["account"], "type": "income", "id": inc["id"]})
                d += timedelta(days=1)
        else:  # monthly — use user's specified pay day
            pay_day = int(inc.get("day") or 1)
            m_year, m_month = today.year, today.month
            for _ in range(4):
                max_day = calendar.monthrange(m_year, m_month)[1]
                actual_day = min(pay_day, max_day)
                try:
                    occurrence = date(m_year, m_month, actual_day)
                    if today <= occurrence <= end_date:
                        upcoming_items.append({"date": occurrence.isoformat(), "name": inc["name"], "amount": float(inc["amount"]), "account": inc["account"], "type": "income", "id": inc["id"]})
                except ValueError:
                    pass
                m_month += 1
                if m_month > 12:
                    m_month = 1
                    m_year += 1

    upcoming_items.sort(key=lambda x: x["date"])
    upcoming_json = json.dumps(upcoming_items)
    hist_snapshots_json = json.dumps(hist_snapshots)
    savings_rates_json = json.dumps(savings_rates)

    # store in cache
    forecast_cache[cache_key] = (time.time(), {
        "snapshots": snapshots_json,
        "account_names": account_names_json,
        "account_types": account_types_json,
        "initial_balances": initial_balances_json,
        "upcoming": upcoming_json,
        "hist_snapshots": hist_snapshots_json,
        "savings_rates": savings_rates_json,
        "is_pro": is_pro
    })

    return render_template(
        "forecast.html",
        snapshots=snapshots_json,
        account_names=account_names_json,
        account_types=account_types_json,
        initial_balances=initial_balances_json,
        upcoming=upcoming_json,
        hist_snapshots=hist_snapshots_json,
        savings_rates=savings_rates_json,
        is_pro=is_pro,
        message=request.args.get("msg", ""),
        today=today.isoformat()
    )

@app.get("/verify-email/<token>")
@limiter.limit("10 per minute")
def verify_email(token):
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
    display_name = (request.form.get("display_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
    confirm = (request.form.get("confirm") or "").strip()

    if not display_name:
        return render_template("register.html", error="Please enter your name.", submitted_name=display_name)

    if not email or not password:
        return render_template("register.html", error="All fields are required.", submitted_name=display_name)

    # Block disposable / throwaway email domains
    _DISPOSABLE_DOMAINS = {
        "mailinator.com","guerrillamail.com","guerrillamail.net","guerrillamail.org",
        "guerrillamail.biz","guerrillamail.de","guerrillamailblock.com","grr.la",
        "sharklasers.com","spam4.me","tempmail.com","temp-mail.org","temp-mail.io",
        "throwam.com","throwam.net","throwaway.email","dispostable.com","maildrop.cc",
        "yopmail.com","yopmail.fr","cool.fr.nf","jetable.fr.nf","nospam.ze.tc",
        "nomail.xl.cx","mega.zik.dj","speed.1s.fr","courriel.fr.nf","moncourrier.fr.nf",
        "monemail.fr.nf","monmail.fr.nf","trashmail.com","trashmail.at","trashmail.io",
        "trashmail.me","trashmail.net","trashmail.org","trashmail.xyz","discard.email",
        "fakeinbox.com","mailnull.com","spamgourmet.com","spamgourmet.net","spamgourmet.org",
        "getairmail.com","filzmail.com","spamfree24.org","spamfree24.de","spamfree24.info",
        "spamfree24.net","spamfree.eu","spammotel.com","spamslicer.com","trashdevil.com",
        "trashdevil.de","wegwerfmail.de","wegwerfmail.net","wegwerfmail.org",
        "crazymailing.com","spambox.us","spam.la","binkmail.com","bobmail.info",
        "mailinatar.com","mailinator2.com","mailinator.us","notmailinator.com",
        "getnada.com","mohmal.com","burnermail.io","10minutemail.com","10minutemail.net",
        "10minutemail.org","10minutemail.de","minutemail.com","tempinbox.com",
        "throwam.com","spamhereplease.com","spamherelots.com","emailondeck.com",
        "inoutmail.de","inoutmail.eu","inoutmail.info","inoutmail.net",
        "anonaddy.com","simplelogin.io",
    }
    email_domain = email.split("@")[-1] if "@" in email else ""
    if email_domain in _DISPOSABLE_DOMAINS:
        return render_template("register.html", error="Please use a real email address — disposable addresses aren't accepted.", submitted_name=display_name)

    if password != confirm:
        return render_template("register.html", error="Passwords do not match.", submitted_name=display_name)

    if len(password) < 8:
        return render_template("register.html", error="Password must be at least 8 characters.", submitted_name=display_name)

    if not any(c.isupper() for c in password):
        return render_template("register.html", error="Password must contain at least one uppercase letter.", submitted_name=display_name)

    if not any(c.islower() for c in password):
        return render_template("register.html", error="Password must contain at least one lowercase letter.", submitted_name=display_name)

    if not any(c.isdigit() for c in password):
        return render_template("register.html", error="Password must contain at least one number.", submitted_name=display_name)

    if not any(not c.isalnum() for c in password):
        return render_template("register.html", error="Password must contain at least one symbol (e.g. !@#$).", submitted_name=display_name)

    if not request.form.get("age_confirm"):
        return render_template("register.html", error="You must confirm you are 16 or over.", submitted_name=display_name)

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
        return render_template("register.html", error="An account with that email already exists.", submitted_name=display_name)

    hashed = generate_password_hash(password)
    today_str = date.today().isoformat()
    token = secrets.token_urlsafe(32)

    expires_at = (datetime.now() + timedelta(days=7)).isoformat()

    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO users (email, password, display_name, created_at, verify_token, verify_token_expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (email, hashed, display_name, today_str, token, expires_at)
        )
        user_id = cursor.fetchone()[0]
    else:
        cursor.execute(
            "INSERT INTO users (email, password, display_name, created_at, verify_token, verify_token_expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, hashed, display_name, today_str, token, expires_at)
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
    track_for_user(user_id, 'auth.register')
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
    track_for_user(row["id"], 'auth.login')
    return redirect(url_for("home"))

@app.get("/logout")
def logout():
    if current_user.is_authenticated:
        logger.info(f"User logout: {current_user.email}")
    logout_user()
    return redirect("/")

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
CATEGORY_KEYWORDS = {
    'Food & Drink': ['tesco', 'sainsbury', 'asda', 'waitrose', 'morrisons', 'aldi', 'lidl', 'co-op', 'coop', 'iceland', 'greggs', 'mcdonald', 'kfc', 'subway', 'pizza', 'burger', 'nando', 'deliveroo', 'uber eats', 'just eat', 'cafe', 'coffee', 'costa', 'starbucks', 'pret', 'restaurant', 'takeaway', 'pub', 'bar', 'supermarket', 'marks & spencer', 'waitrose'],
    'Transport': ['tfl', 'uber', 'bolt', 'taxi', 'rail', 'train', 'national rail', 'southern', 'thameslink', 'great western', 'avanti', 'bus', 'oyster', 'petrol', 'fuel', 'parking', 'halfords', 'kwikfit'],
    'Housing': ['rent', 'mortgage', 'council tax', 'letting', 'estate agent'],
    'Bills & Utilities': ['electricity', 'gas', 'water', 'broadband', 'internet', 'bt ', 'sky', 'virgin media', 'ee ', 'o2 ', 'vodafone', 'three', 'talktalk', 'octopus', 'utility', 'phone', 'mobile', 'insurance', 'direct line', 'aviva', 'admiral'],
    'Entertainment': ['netflix', 'spotify', 'amazon prime', 'disney+', 'now tv', 'cinema', 'odeon', 'vue', 'cineworld', 'ticketmaster', 'youtube premium', 'twitch', 'playstation', 'xbox', 'steam', 'nintendo'],
    'Shopping': ['amazon', 'ebay', 'asos', 'next ', 'h&m', 'zara', 'primark', 'john lewis', 'argos', 'ikea', 'currys', 'apple store', 'app store', 'google play', 'etsy'],
    'Health': ['pharmacy', 'boots', 'superdrug', 'nhs', 'dentist', 'doctor', 'hospital', 'gym', 'puregym', 'david lloyd', 'anytime fitness', 'nuffield', 'optician', 'specsavers'],
    'Personal Care': ['haircut', 'hairdresser', 'barber', 'salon', 'spa', 'beauty', 'nail'],
}

def suggest_category(description: str) -> str:
    """Suggest a category based on keywords in the transaction description."""
    desc_lower = description.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return category
    return 'Other'


def parse_bank_csv(content: str):
    """
    Parse a bank CSV and return (rows, error).
    rows = list of {date, description, amount} dicts.
    Handles Monzo, Barclays, HSBC, Nationwide, Starling, NatWest formats.
    """
    import io

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
                    parsed_date = datetime.strptime(date_str, fmt).date().isoformat()
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
            category = suggest_category(desc)

            parsed.append({'date': parsed_date, 'description': desc, 'amount': round(amount, 2), 'is_transfer': is_transfer, 'category': category})
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
        track('page_view.import')
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

    # Only import rows the user checked — collect category per original index
    selected_rows = []
    for i in range(len(rows)):
        if request.form.get(f'include_{i}') == '1':
            row = rows[i]
            row['category'] = request.form.get(f'category_{i}') or row.get('category', 'Other')
            selected_rows.append(row)

    if not selected_rows:
        return redirect(url_for('import_csv'))

    total_delta = 0.0
    for row in selected_rows:
        add_transaction(row['date'], row['description'], row['amount'], account, current_user.id, type='import', category=row['category'])
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

    bust_forecast_cache(current_user.id)
    track('action.import_csv')
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
        track('billing.upgrade_start')
        return redirect(checkout_session.url)
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return redirect(url_for("settings", msg="Could not start checkout. Please try again."))


# --- BILLING SUCCESS ---
# User lands here after successful Stripe payment
# We verify the session directly here so is_pro=1 is set immediately (no webhook race condition)
# The webhook still fires later and is a reliable backup
@app.get("/billing/success")
@login_required
def billing_success():
    session_id = request.args.get("session_id")
    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status in ("paid", "no_payment_required"):
                customer_id = checkout_session.customer
                db = get_db()
                cursor = db.cursor()
                if USE_POSTGRES:
                    cursor.execute(
                        "UPDATE users SET is_pro = 1, stripe_customer_id = %s WHERE id = %s",
                        (customer_id, current_user.id)
                    )
                else:
                    cursor.execute(
                        "UPDATE users SET is_pro = 1, stripe_customer_id = ? WHERE id = ?",
                        (customer_id, current_user.id)
                    )
                db.commit()
                cursor.close()
                release_db(db)
        except Exception as e:
            logger.error(f"Billing success session retrieval error: {e}")
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
            track_for_user(int(user_id), 'billing.upgrade_complete')

    # Subscription cancelled — deactivate Pro
    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")

        if customer_id:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
            else:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer_id,))
            uid_row = cursor.fetchone()
            if USE_POSTGRES:
                cursor.execute("UPDATE users SET is_pro = 0 WHERE stripe_customer_id = %s", (customer_id,))
            else:
                cursor.execute("UPDATE users SET is_pro = 0 WHERE stripe_customer_id = ?", (customer_id,))
            db.commit()
            cursor.close()
            release_db(db)
            logger.info(f"Pro deactivated for customer_id={customer_id}")
            if uid_row:
                track_for_user(uid_row[0], 'billing.cancel')

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


# --- ADMIN ANALYTICS ---

@app.get("/admin/unlock")
@login_required
@limiter.limit("5 per hour")
def admin_unlock():
    secret = request.args.get("secret", "")
    if current_user.id == ADMIN_USER_ID and secret == ADMIN_SECRET and ADMIN_SECRET:
        session["admin_unlocked"] = ADMIN_SECRET
        return redirect("/admin/analytics")
    logger.warning(f"Failed admin unlock attempt — user_id={current_user.id} ip={request.remote_addr}")
    return render_template("404.html"), 404

@app.get("/admin/analytics")
@login_required
@limiter.limit("30 per hour")
def admin_analytics():
    if current_user.id != ADMIN_USER_ID or session.get("admin_unlocked") != ADMIN_SECRET or not ADMIN_SECRET:
        logger.warning(f"Blocked admin access attempt — user_id={current_user.id} ip={request.remote_addr}")
        return render_template("404.html"), 404

    db = get_db()
    cursor = db.cursor()

    def q(sql):
        cursor.execute(sql)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    if USE_POSTGRES:
        mau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= NOW() - INTERVAL '30 days'")[0]["n"]
        wau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= NOW() - INTERVAL '7 days'")[0]["n"]
        dau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= NOW() - INTERVAL '1 day'")[0]["n"]
        total_users = q("SELECT COUNT(*) AS n FROM users")[0]["n"]
        signups = q("SELECT DATE(created_at::date) AS day, COUNT(*) AS n FROM users WHERE created_at::date >= NOW() - INTERVAL '30 days' GROUP BY day ORDER BY day")
        dau_series = q("SELECT DATE(ts) AS day, COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= NOW() - INTERVAL '30 days' GROUP BY day ORDER BY day")
        feature_usage = q("SELECT event, COUNT(*) AS hits, COUNT(DISTINCT user_id) AS users FROM analytics_events GROUP BY event ORDER BY hits DESC")
        retention = q("SELECT COUNT(DISTINCT u.id) AS total, COUNT(DISTINCT CASE WHEN a.ts >= u.created_at::date + INTERVAL '7 days' THEN a.user_id END) AS returned FROM users u LEFT JOIN analytics_events a ON a.user_id = u.id WHERE u.created_at::date <= NOW() - INTERVAL '14 days'")[0]
        funnel = q("SELECT COUNT(DISTINCT u.id) AS registered, COUNT(DISTINCT a.user_id) AS took_action FROM users u LEFT JOIN analytics_events a ON a.user_id = u.id AND a.event LIKE 'action.%%'")[0]
        table_stats = q("SELECT COUNT(*) AS total, MIN(ts) AS oldest FROM analytics_events")[0]
    else:
        mau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= datetime('now', '-30 days')")[0]["n"]
        wau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= datetime('now', '-7 days')")[0]["n"]
        dau    = q("SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= datetime('now', '-1 day')")[0]["n"]
        total_users = q("SELECT COUNT(*) AS n FROM users")[0]["n"]
        signups = q("SELECT DATE(created_at) AS day, COUNT(*) AS n FROM users WHERE created_at >= datetime('now', '-30 days') GROUP BY day ORDER BY day")
        dau_series = q("SELECT DATE(ts) AS day, COUNT(DISTINCT user_id) AS n FROM analytics_events WHERE ts >= datetime('now', '-30 days') GROUP BY day ORDER BY day")
        feature_usage = q("SELECT event, COUNT(*) AS hits, COUNT(DISTINCT user_id) AS users FROM analytics_events GROUP BY event ORDER BY hits DESC")
        retention = q("SELECT COUNT(DISTINCT u.id) AS total, COUNT(DISTINCT CASE WHEN a.ts >= datetime(u.created_at, '+7 days') THEN a.user_id END) AS returned FROM users u LEFT JOIN analytics_events a ON a.user_id = u.id WHERE u.created_at <= datetime('now', '-14 days')")[0]
        funnel = q("SELECT COUNT(DISTINCT u.id) AS registered, COUNT(DISTINCT a.user_id) AS took_action FROM users u LEFT JOIN analytics_events a ON a.user_id = u.id AND a.event LIKE 'action.%'")[0]
        table_stats = q("SELECT COUNT(*) AS total, MIN(ts) AS oldest FROM analytics_events")[0]

    cursor.close()
    release_db(db)

    retention_pct = round(100 * retention["returned"] / retention["total"]) if retention["total"] else 0
    funnel_pct    = round(100 * funnel["took_action"] / funnel["registered"]) if funnel["registered"] else 0
    signup_max    = max((r["n"] for r in signups), default=1) or 1
    dau_max       = max((r["n"] for r in dau_series), default=1) or 1

    return render_template("admin_analytics.html",
        mau=mau, wau=wau, dau=dau, total_users=total_users,
        signups=signups, signup_max=signup_max,
        dau_series=dau_series, dau_max=dau_max,
        feature_usage=feature_usage,
        retention=retention, retention_pct=retention_pct,
        funnel=funnel, funnel_pct=funnel_pct,
        table_stats=table_stats,
    )


# --- DELETE ACCOUNT ---
# Permanently deletes all user data and the account itself.
# Requires the user to confirm by typing their email address.
# Cancels any active Stripe subscription before deleting.
@app.post("/settings/delete-account")
@login_required
@limiter.limit("5 per hour")
def delete_account():
    typed_email = (request.form.get("confirm_email") or "").strip().lower()
    if typed_email != current_user.email.lower():
        return redirect(url_for("settings", tab="danger", msg="DELETE_EMAIL_MISMATCH"))

    user_id = current_user.id

    # Cancel Stripe subscription if Pro
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT stripe_customer_id FROM users WHERE id = %s", (user_id,))
        else:
            cursor.execute("SELECT stripe_customer_id FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        release_db(db)
        customer_id = (row[0] if USE_POSTGRES else row["stripe_customer_id"]) if row else None
        if customer_id:
            import stripe as _stripe
            subscriptions = _stripe.Subscription.list(customer=customer_id, status="active")
            for sub in subscriptions.auto_paging_iter():
                _stripe.Subscription.cancel(sub["id"])
    except Exception as e:
        logger.warning(f"Could not cancel Stripe subscription for user_id={user_id}: {e}")

    # Log out before deleting so Flask-Login doesn't hold a reference
    logout_user()

    # Delete all user data in dependency order
    try:
        db = get_db()
        cursor = db.cursor()
        ph = "%s" if USE_POSTGRES else "?"
        tables = [
            "investment_updates",
            "investments",
            "analytics_events",
            "flask_sessions",
            "future_events",
            "savings_rules",
            "income",
            "scheduled_expenses",
            "transactions",
            "accounts",
        ]
        for table in tables:
            if table == "flask_sessions":
                # flask_sessions uses sid not user_id — delete by matching session data
                cursor.execute(f"DELETE FROM flask_sessions WHERE data::text LIKE {ph}", (f'%"_user_id": "{user_id}"%',)) if USE_POSTGRES else cursor.execute(f"DELETE FROM flask_sessions WHERE data LIKE {ph}", (f'%"_user_id": "{user_id}"%',))
            else:
                cursor.execute(f"DELETE FROM {table} WHERE user_id = {ph}", (user_id,))
        cursor.execute(f"DELETE FROM users WHERE id = {ph}", (user_id,))
        db.commit()
        cursor.close()
        release_db(db)
        logger.info(f"Account deleted for user_id={user_id}")
    except Exception as e:
        logger.error(f"Error deleting account for user_id={user_id}: {e}")
        return redirect(url_for("login"))

    return redirect(url_for("login") + "?msg=account_deleted")


# --- DATA EXPORT ---
# Returns all the user's transactions as a downloadable CSV file (right to portability)
@app.get("/export-data")
@login_required
def export_data():
    from flask import Response
    import io

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("""
            SELECT date, description, amount, type, account
            FROM transactions WHERE user_id = %s ORDER BY date DESC
        """, (current_user.id,))
    else:
        cursor.execute("""
            SELECT date, description, amount, type, account
            FROM transactions WHERE user_id = ? ORDER BY date DESC
        """, (current_user.id,))
    rows = cursor.fetchall()
    cursor.close()
    release_db(db)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Description", "Amount", "Type", "Account"])
    for row in rows:
        if USE_POSTGRES:
            writer.writerow([row[0], row[1], row[2], row[3], row[4]])
        else:
            writer.writerow([row["date"], row["description"], row["amount"], row["type"], row["account"]])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=spendara-transactions.csv"}
    )


# --- PRIVACY POLICY ---
@app.get("/privacy")
def privacy():
    return render_template("privacy.html")


# --- TRUELAYER OPEN BANKING ---

TRUELAYER_CLIENT_ID     = os.environ.get("TRUELAYER_CLIENT_ID", "")
TRUELAYER_CLIENT_SECRET = os.environ.get("TRUELAYER_CLIENT_SECRET", "")
TRUELAYER_AUTH_URL      = "https://auth.truelayer-sandbox.com"
TRUELAYER_API_URL       = "https://api.truelayer-sandbox.com"
TRUELAYER_REDIRECT_URI  = os.environ.get("TRUELAYER_REDIRECT_URI", "https://spendara.co.uk/truelayer/callback")
TRUELAYER_SCOPES        = "info accounts balance cards transactions direct_debits standing_orders offline_access"

def _ensure_bank_connections_table():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bank_connections (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            token_expiry TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    db.commit()
    cursor.close()
    release_db(db)

def _get_bank_connection(user_id):
    """Return the most recent bank_connections row for user, or None."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT id, access_token, refresh_token, token_expiry FROM bank_connections WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
    else:
        cursor.execute(
            "SELECT id, access_token, refresh_token, token_expiry FROM bank_connections WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    if not row:
        return None
    return {"id": row[0], "access_token": row[1], "refresh_token": row[2], "token_expiry": row[3]}

def _refresh_access_token(conn):
    """Exchange refresh_token for a new access_token. Returns updated conn dict or None on failure."""
    import requests as _req
    resp = _req.post(
        f"{TRUELAYER_AUTH_URL}/connect/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     TRUELAYER_CLIENT_ID,
            "client_secret": TRUELAYER_CLIENT_SECRET,
            "refresh_token": conn["refresh_token"],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "UPDATE bank_connections SET access_token=%s, refresh_token=%s, token_expiry=%s WHERE id=%s",
            (data["access_token"], data.get("refresh_token", conn["refresh_token"]), expiry, conn["id"])
        )
    else:
        cursor.execute(
            "UPDATE bank_connections SET access_token=?, refresh_token=?, token_expiry=? WHERE id=?",
            (data["access_token"], data.get("refresh_token", conn["refresh_token"]), expiry, conn["id"])
        )
    db.commit()
    cursor.close()
    release_db(db)
    conn["access_token"]  = data["access_token"]
    conn["refresh_token"] = data.get("refresh_token", conn["refresh_token"])
    conn["token_expiry"]  = expiry
    return conn

def _get_valid_token(user_id):
    """Return a valid access token, refreshing if needed. None if no connection."""
    conn = _get_bank_connection(user_id)
    if not conn:
        return None
    expiry = conn["token_expiry"]
    if expiry and datetime.utcnow() >= expiry - timedelta(minutes=5):
        conn = _refresh_access_token(conn)
        if not conn:
            return None
    return conn["access_token"]



@app.get("/connect-bank")
@login_required
def connect_bank():
    _ensure_bank_connections_table()
    import urllib.parse
    params = {
        "response_type": "code",
        "client_id":     TRUELAYER_CLIENT_ID,
        "scope":         TRUELAYER_SCOPES,
        "redirect_uri":  TRUELAYER_REDIRECT_URI,
        "providers":     "uk-cs-mock uk-ob-all uk-oauth-all",
    }
    auth_url = f"{TRUELAYER_AUTH_URL}/?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
    return redirect(auth_url)


@app.get("/truelayer/callback")
@login_required
def truelayer_callback():
    import requests as _req
    _ensure_bank_connections_table()
    error = request.args.get("error")
    if error:
        return redirect(url_for("actions", msg=f"Bank connection cancelled: {error}"))

    code = request.args.get("code")
    if not code:
        return redirect(url_for("actions", msg="Bank connection failed — no code received."))

    resp = _req.post(
        f"{TRUELAYER_AUTH_URL}/connect/token",
        data={
            "grant_type":    "authorization_code",
            "client_id":     TRUELAYER_CLIENT_ID,
            "client_secret": TRUELAYER_CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  TRUELAYER_REDIRECT_URI,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        logger.error("TrueLayer token exchange failed: %s", resp.text)
        return redirect(url_for("actions", msg="Bank connection failed — could not exchange token."))

    data         = resp.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in   = data.get("expires_in", 3600)
    token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)

    # Try to get the provider name from the /me endpoint
    provider = "Unknown"
    try:
        me_resp = _req.get(
            f"{TRUELAYER_API_URL}/data/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if me_resp.status_code == 200:
            results = me_resp.json().get("results", [])
            if results:
                provider = results[0].get("provider_id", "Unknown")
    except Exception:
        pass

    db = get_db()
    cursor = db.cursor()
    # Remove any previous connection for this user and replace
    if USE_POSTGRES:
        cursor.execute("DELETE FROM bank_connections WHERE user_id = %s", (current_user.id,))
        cursor.execute(
            "INSERT INTO bank_connections (user_id, provider, access_token, refresh_token, token_expiry) VALUES (%s, %s, %s, %s, %s)",
            (current_user.id, provider, access_token, refresh_token, token_expiry)
        )
    else:
        cursor.execute("DELETE FROM bank_connections WHERE user_id = ?", (current_user.id,))
        cursor.execute(
            "INSERT INTO bank_connections (user_id, provider, access_token, refresh_token, token_expiry) VALUES (?, ?, ?, ?, ?)",
            (current_user.id, provider, access_token, refresh_token, token_expiry)
        )
    db.commit()
    cursor.close()
    release_db(db)
    track('action.bank_connected')
    return redirect(url_for("actions", msg="Bank connected successfully! Tap 'Sync transactions' to import your data."))


@app.get("/sync-bank")
@login_required
def sync_bank():
    import requests as _req
    token = _get_valid_token(current_user.id)
    if not token:
        return redirect(url_for("actions", msg="No bank connected. Please connect your bank first."))

    headers = {"Authorization": f"Bearer {token}"}

    # Fetch accounts from TrueLayer
    accounts_resp = _req.get(f"{TRUELAYER_API_URL}/data/v1/accounts", headers=headers, timeout=15)
    if accounts_resp.status_code != 200:
        logger.error("TrueLayer accounts fetch failed: %s", accounts_resp.text)
        return redirect(url_for("actions", msg="Sync failed — could not fetch accounts from your bank."))

    tl_accounts = accounts_resp.json().get("results", [])
    total_imported = 0
    accounts_synced = 0

    db = get_db()
    cursor = db.cursor()

    # Load existing Spendara account names for this user
    if USE_POSTGRES:
        cursor.execute("SELECT id, name, balance FROM accounts WHERE user_id = %s AND active = 1", (current_user.id,))
    else:
        cursor.execute("SELECT id, name, balance FROM accounts WHERE user_id = ? AND active = 1", (current_user.id,))
    existing_accounts = {row[1]: {"id": row[0], "balance": row[1]} for row in cursor.fetchall()}

    for tl_acc in tl_accounts:
        tl_acc_id   = tl_acc.get("account_id")
        tl_acc_name = tl_acc.get("display_name") or tl_acc.get("account_type", "Bank Account")

        # Match to a Spendara account by name (case-insensitive), or skip if no match
        matched_name = None
        for sp_name in existing_accounts:
            if sp_name.lower() == tl_acc_name.lower():
                matched_name = sp_name
                break

        # Update balance if we have a matched account
        if matched_name:
            bal_resp = _req.get(f"{TRUELAYER_API_URL}/data/v1/accounts/{tl_acc_id}/balance", headers=headers, timeout=10)
            if bal_resp.status_code == 200:
                bal_results = bal_resp.json().get("results", [])
                if bal_results:
                    new_balance = float(bal_results[0].get("available", bal_results[0].get("current", 0)))
                    if USE_POSTGRES:
                        cursor.execute("UPDATE accounts SET balance = %s WHERE user_id = %s AND name = %s", (new_balance, current_user.id, matched_name))
                    else:
                        cursor.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND name = ?", (new_balance, current_user.id, matched_name))
                    accounts_synced += 1

        # Fetch transactions for this TrueLayer account
        tx_resp = _req.get(f"{TRUELAYER_API_URL}/data/v1/accounts/{tl_acc_id}/transactions", headers=headers, timeout=15)
        if tx_resp.status_code != 200:
            continue

        tl_txns = tx_resp.json().get("results", [])
        target_account = matched_name or tl_acc_name

        for tx in tl_txns:
            tx_id          = tx.get("transaction_id", "")
            description    = tx.get("description") or tx.get("merchant_name") or "Bank transaction"
            amount         = float(tx.get("amount", 0))
            tx_date_str    = (tx.get("timestamp") or tx.get("booking_date_time") or "")[:10]
            try:
                tx_date = date.fromisoformat(tx_date_str)
            except ValueError:
                tx_date = date.today()

            # Skip duplicates — check by truelayer_tx_id if column exists, else by description+amount+date+account
            already_exists = False
            try:
                if USE_POSTGRES:
                    cursor.execute(
                        "SELECT id FROM transactions WHERE user_id = %s AND truelayer_tx_id = %s",
                        (current_user.id, tx_id)
                    )
                else:
                    cursor.execute(
                        "SELECT id FROM transactions WHERE user_id = ? AND truelayer_tx_id = ?",
                        (current_user.id, tx_id)
                    )
                already_exists = cursor.fetchone() is not None
            except Exception:
                # Column doesn't exist yet — fall back to description+amount+date duplicate check
                db.rollback()
                try:
                    if USE_POSTGRES:
                        cursor.execute(
                            "SELECT id FROM transactions WHERE user_id=%s AND description=%s AND amount=%s AND date=%s AND account=%s",
                            (current_user.id, description, amount, tx_date, target_account)
                        )
                    else:
                        cursor.execute(
                            "SELECT id FROM transactions WHERE user_id=? AND description=? AND amount=? AND date=? AND account=?",
                            (current_user.id, description, amount, tx_date, target_account)
                        )
                    already_exists = cursor.fetchone() is not None
                except Exception:
                    db.rollback()
                    already_exists = True

            if already_exists:
                continue

            category = "Income" if amount > 0 else "Other"
            try:
                if USE_POSTGRES:
                    cursor.execute(
                        "INSERT INTO transactions (user_id, description, amount, date, account, category) VALUES (%s, %s, %s, %s, %s, %s)",
                        (current_user.id, description, amount, tx_date, target_account, category)
                    )
                else:
                    cursor.execute(
                        "INSERT INTO transactions (user_id, description, amount, date, account, category) VALUES (?, ?, ?, ?, ?, ?)",
                        (current_user.id, description, amount, tx_date, target_account, category)
                    )
                total_imported += 1
            except Exception as e:
                logger.error("Failed to insert TrueLayer transaction: %s", e)
                db.rollback()

    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    track('action.bank_synced')

    parts = []
    if total_imported:
        parts.append(f"{total_imported} transaction{'s' if total_imported != 1 else ''} imported")
    if accounts_synced:
        parts.append(f"{accounts_synced} account balance{'s' if accounts_synced != 1 else ''} updated")
    msg = ", ".join(parts) + "." if parts else "Already up to date — no new transactions found."
    return redirect(url_for("actions", msg=msg))


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