# Spendara - tested and protected
from __future__ import annotations

# --- IMPORTS ---
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
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
import math

from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template, jsonify, Response
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

# simulate_balances_until is used for forecast and "can I afford it" features
from Tracker import simulate_balances_until
import income_engine
from bill_engine import shift_weekend_to_monday

from models import (
    add_transaction,
    update_account_balance,
    get_active_accounts,
    get_all_accounts,
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

# In-memory holding area for a parsed CSV import awaiting user confirmation.
# Keeps raw bank transaction rows out of the persistent (Postgres-backed)
# flask_sessions table entirely — the session only holds an opaque token, so an
# abandoned import never leaves real transaction data sitting in the database.
_pending_imports = {}
IMPORT_CACHE_TTL = 1800  # 30 minutes in seconds


def _purge_expired_imports():
    """Drop any pending imports older than IMPORT_CACHE_TTL. Called whenever a
    new import is parsed, so the cache can't grow unbounded with abandoned ones."""
    now = time.time()
    stale = [t for t, (stored_at, _, _) in _pending_imports.items() if now - stored_at > IMPORT_CACHE_TTL]
    for t in stale:
        _pending_imports.pop(t, None)


def _get_pending_import(token):
    """Return (rows, account) for a still-valid import token, or (None, None) if
    the token is missing or the entry has expired."""
    entry = _pending_imports.get(token)
    if not entry:
        return None, None
    stored_at, rows, account = entry
    if time.time() - stored_at > IMPORT_CACHE_TTL:
        _pending_imports.pop(token, None)
        return None, None
    return rows, account

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
    """Convert YYYY-MM-DD string to UK 'DD/MM/YYYY' format."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value), '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        return value

@app.template_filter('moneyfmt')
def moneyfmt_filter(value):
    """Formats a number with thousand separators and 2 decimal places
    ('1234.5' -> '1,234.50') - no currency symbol, since every call site
    already prefixes its own £. Not applied app-wide by default (that
    would be a much bigger, separately-scoped sweep); introduced for the
    Goals card, August 2026."""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
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
        exempt = ['/login', '/register', '/stripe/webhook', '/auto-apply', '/mark-bill-paid', '/dismiss-auto-apply', '/api/income-preview', '/api/edit-pending-item', '/api/edit-cycle-item', '/api/set-primary-income', '/my-money/setup/dismiss', '/api/goal-pace-preview', '/api/goal-commitment-preview']
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

# --- GOOGLE OAUTH ---
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID', ''),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

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
    'billing.payment_failed', 'billing.subscription_past_due', 'billing.subscription_recovered',
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
                candidate = shift_weekend_to_monday(_date(y, m, actual_day))
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
                candidate = shift_weekend_to_monday(_date(yr, bill_month, actual_day))
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
    income_items = _resolve_income_rows(income_items, user_id)

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
        if inc.get('_distribution') != 'spread':
            for d in income_engine.get_payment_dates(inc, backfill_start, yesterday):
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
    Amount is negative for bills, positive for income.
    Bills/income tied to a locked account are excluded — a locked account's
    balance is meant to be frozen, so auto-apply must never silently touch it
    (the interactive routes already block this; this is the same rule for the
    background/silent path)."""
    from datetime import date as _date, timedelta

    today = _date.today()
    locked_names = {r["name"] for r in get_active_accounts(user_id) if r.get("is_locked")}

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
    income_items = _resolve_income_rows(income_items, user_id)

    pending = []

    for bill in bills:
        if bill.get('day') is None:
            continue
        if bill.get('account') in locked_names:
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
        if inc.get('account') in locked_names:
            continue
        if inc.get('_distribution') == 'spread':
            continue
        last_applied = _date.fromisoformat(inc['last_applied'])
        search_from = last_applied + timedelta(days=1)
        if search_from > today:
            continue
        for d in income_engine.get_payment_dates(inc, search_from, today):
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

    bust_forecast_cache(user_id)


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


# --- SELF-EMPLOYED INCOME AVERAGING (New — Beta) ---
# A self_employed_average income row's amount is never read directly from the
# income table — it's resolved fresh every time from rule_config, so switching
# Manual/Automatic or the averaging window in Settings takes effect immediately
# everywhere (forecast, overview, snapshot), not just on the next manual edit.
def _compute_automatic_income_average(user_id, window_months):
    """Rolling monthly average from logged income transactions over the trailing
    window. Total received in the window / number of months in the window — so
    irregular timing (e.g. three payments one month, none the next) still nets
    out to a sensible monthly estimate. A single logged transaction is simply
    that transaction's value divided across the window; nothing is blocked
    while a user builds up history, per the design."""
    from datetime import date as _date, timedelta as _timedelta
    cutoff = (_date.today() - _timedelta(days=30 * window_months)).isoformat()
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = %s AND type = 'income' AND date >= %s",
            (user_id, cutoff)
        )
    else:
        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND type = 'income' AND date >= ?",
            (user_id, cutoff)
        )
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    total = float(row[0] if USE_POSTGRES else row[0]) if row else 0.0
    return total / window_months if window_months else 0.0


def _self_employed_cycle_length_days(user_id):
    """Length in days of a self-employed user's current manual cycle - used to
    turn an averaged income amount into a flat daily accrual for spread mode."""
    try:
        import cycle_engine as _ce2
        _c = _ce2.get_cycle(user_id)
        days = (_c["display_end"] - _c["display_start"]).days + 1
        return days if days > 0 else 30
    except Exception:
        return 30


def _resolve_income_rows(income_rows, user_id):
    """Return a copy of income_rows with any self_employed_average row's amount
    resolved to its current effective value (manual figure from rule_config, or
    a live rolling average) and its distribution ('lump'/'spread') attached as
    `_distribution`. Every other income row passes through completely
    unchanged — this is purely additive for self-employed users."""
    resolved = []
    for inc in income_rows:
        inc = dict(inc)
        if inc.get("rule_type") == "self_employed_average":
            try:
                cfg = json.loads(inc.get("rule_config") or "{}")
            except (TypeError, ValueError):
                cfg = {}
            mode = cfg.get("mode", "manual")
            if mode == "auto":
                window_months = int(cfg.get("window_months", 3))
                inc["amount"] = _compute_automatic_income_average(user_id, window_months)
            else:
                inc["amount"] = float(cfg.get("manual_amount", inc.get("amount") or 0))
            inc["_distribution"] = cfg.get("distribution", "lump")
        resolved.append(inc)
    return resolved


# --- SPENDING ALERT THRESHOLD ---
# Optional, user-defined low-balance warning - separate from Safe to Spend.
# Off by default (alert_mode NULL): no logic runs, nothing changes for users
# who haven't set one up. 'overall' checks the combined balance of all active,
# unlocked accounts against a single figure; 'per_account' checks each
# account against its own stored threshold. Locked accounts are excluded from
# both modes - same reasoning as calculate_financial_overview(): a frozen
# balance from a Pro-to-Free downgrade can't be trusted as current, so it
# shouldn't feed a live warning any more than it feeds the forecast.
def get_triggered_spending_alerts(user_id, accounts):
    """Returns a list of {"account": name_or_None, "balance": float, "threshold": float}
    for every threshold currently at or below its balance. Empty list if the
    user has no alert_mode set, or nothing has crossed."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT alert_mode, alert_overall_threshold FROM users WHERE id = %s", (user_id,))
    else:
        cursor.execute("SELECT alert_mode, alert_overall_threshold FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    if not row:
        return []
    alert_mode = row[0] if USE_POSTGRES else row["alert_mode"]
    overall_threshold = row[1] if USE_POSTGRES else row["alert_overall_threshold"]
    if not alert_mode:
        return []

    unlocked = {
        name: info for name, info in accounts.items()
        if info.get("active", True) and not info.get("is_locked")
    }

    alerts = []
    if alert_mode == "overall":
        if overall_threshold is not None:
            total = sum(float(info.get("balance", 0.0)) for info in unlocked.values())
            if total <= float(overall_threshold):
                alerts.append({"account": None, "balance": total, "threshold": float(overall_threshold)})
    elif alert_mode == "per_account":
        for name, info in unlocked.items():
            thr = info.get("alert_threshold")
            if thr is None:
                continue
            bal = float(info.get("balance", 0.0))
            if bal <= float(thr):
                alerts.append({"account": name, "balance": bal, "threshold": float(thr)})
        alerts.sort(key=lambda a: a["account"].lower())
    return alerts


# --- FINANCIAL OVERVIEW CALCULATION ---
# Splits accounts into spending (current/cash) and savings, then calculates:
# - spending balance (total in spending accounts)
# - future bills (bills still to leave this month)
# - safe to spend (spending balance minus future bills)
# - savings balance
# - net worth (spending + savings)
def calculate_financial_overview(accounts, period_end=None, safe_boundary=None):
    """
    period_end    — upper bound for Bills left display (= display_end from cycle engine)
    safe_boundary — upper bound for safe_spending deduction (= next payday - 1)
    If only one is supplied the other defaults to it (backward-compatible).
    """
    from datetime import date
    from dateutil.relativedelta import relativedelta as _rel
    today = datetime.today()
    current_day = today.day
    # Normalise: each defaults to the other so legacy single-param callers still work
    if period_end is None:
        period_end = safe_boundary
    if safe_boundary is None:
        safe_boundary = period_end

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
        # Locked accounts (from a Pro->Free downgrade) are frozen — no new
        # activity can touch them — so their balance can't be trusted to stay
        # current. Counting it here would present a stale figure with the
        # same confidence as live data; excluding it is the honest choice.
        if info.get("is_locked"):
            continue
        acc_type = info.get("type")
        balance = float(info.get("balance", 0.0))
        if acc_type in spending_types:
            spending_balance += balance
            spending_accounts.append({"name": name, "balance": balance})
        elif acc_type in savings_types:
            savings_balance += balance
            savings_accounts.append({"name": name, "balance": balance, "savings_type": info.get("savings_type")})

    all_future_bills = 0.0      # all unpaid bills in period — shown in Bills left
    spending_future_bills = 0.0  # only spending-account bills — used for safe_spending
    future_bills_list = []

    import calendar as _cal2
    today_date = today.date()
    current_day = today.day

    # Build months spanning (today_date, period_end] — used for bills-display iteration
    if period_end is not None:
        future_check_months = []
        _fy, _fm = today_date.year, today_date.month
        while date(_fy, _fm, 1) <= period_end:
            future_check_months.append((_fy, _fm))
            _fm = _fm + 1 if _fm < 12 else 1
            _fy = _fy if _fm > 1 else _fy + 1

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        freq = expense.get("frequency") or "monthly"

        candidates = []
        if period_end is not None:
            if freq == "monthly":
                for (_ey, _em) in future_check_months:
                    dim = _cal2.monthrange(_ey, _em)[1]
                    due = shift_weekend_to_monday(date(_ey, _em, min(expense["day"], dim)))
                    if today_date < due <= period_end:
                        candidates.append(due)
            elif freq == "yearly":
                exp_month = expense.get("month")
                if exp_month:
                    for (_ey, _em) in future_check_months:
                        if _em != exp_month:
                            continue
                        dim = _cal2.monthrange(_ey, _em)[1]
                        due = shift_weekend_to_monday(date(_ey, _em, min(expense["day"], dim)))
                        if today_date < due <= period_end:
                            candidates.append(due)
            else:
                # weekly/fortnightly: single next-occurrence fallback
                days_in_month = _cal2.monthrange(today_date.year, today_date.month)[1]
                due_day = min(expense["day"], days_in_month)
                next_due = date(today_date.year, today_date.month, due_day)
                if next_due <= today_date:
                    next_month = today_date.replace(day=1) + _rel(months=1)
                    days_next = _cal2.monthrange(next_month.year, next_month.month)[1]
                    next_due = next_month.replace(day=min(expense["day"], days_next))
                next_due = shift_weekend_to_monday(next_due)
                if today_date < next_due <= period_end:
                    candidates.append(next_due)
        else:
            # No period_end: legacy path — bill due later this calendar month
            if expense["day"] > current_day:
                dim = _cal2.monthrange(today_date.year, today_date.month)[1]
                candidates.append(shift_weekend_to_monday(date(today_date.year, today_date.month, min(expense["day"], dim))))

        for due in candidates:
            last_applied = expense.get("last_applied")
            if last_applied and last_applied >= due.isoformat():
                continue
            acc = expense["account"]
            if acc not in accounts or accounts[acc].get("is_locked"):
                continue
            all_future_bills += expense["amount"]
            future_bills_list.append({
                "id": expense["id"],
                "name": expense["name"],
                "amount": expense["amount"],
                "day": expense["day"],
                "account": acc,
                "due_date": due.isoformat(),
                "type": "bill",
                "account_type": accounts[acc]["type"],
            })
            if accounts[acc]["type"] in spending_types:
                spending_future_bills += expense["amount"]

    # --- Future events — one-off costs on a specific account, folded into
    # the same "bills left" / Safe to Spend deduction as scheduled bills.
    # Unlike a bill, an event has no recurrence pattern (no day/frequency) -
    # just a single real date - so there's no "next occurrence" search or
    # last_applied tracking needed: it's simply in range or it isn't, and
    # once its date has passed it naturally falls out of the window on its
    # own, the same way a one-off transaction would.
    try:
        _fe_db = get_db()
        _fe_cur = _fe_db.cursor()
        if USE_POSTGRES:
            _fe_cur.execute("SELECT * FROM future_events WHERE user_id = %s", (current_user.id,))
        else:
            _fe_cur.execute("SELECT * FROM future_events WHERE user_id = ?", (current_user.id,))
        _fe_cols = [d[0] for d in _fe_cur.description]
        _future_events_raw = [dict(zip(_fe_cols, r)) for r in _fe_cur.fetchall()]
        _fe_cur.close()
        release_db(_fe_db)
    except Exception as _fe_err:
        logger.debug(f"Could not load future events for overview: {_fe_err}")
        _future_events_raw = []

    for event in _future_events_raw:
        try:
            due = date.fromisoformat(str(event["date"]))
        except (ValueError, TypeError, KeyError):
            continue
        if period_end is not None:
            if not (today_date < due <= period_end):
                continue
        else:
            # No period_end: legacy path — event due later this calendar month,
            # mirroring the bill legacy path immediately above.
            if not (today_date < due and due.year == today_date.year and due.month == today_date.month):
                continue
        acc = event.get("account")
        if acc not in accounts or accounts[acc].get("is_locked"):
            continue
        amt = float(event["amount"])
        all_future_bills += amt
        future_bills_list.append({
            "id": event["id"],
            "name": event["name"],
            "amount": amt,
            "day": due.day,
            "account": acc,
            "due_date": due.isoformat(),
            "type": "event",
            "account_type": accounts[acc]["type"],
        })
        if accounts[acc]["type"] in spending_types:
            spending_future_bills += amt

    # --- Savings rules — treated like bills leaving spending accounts ---
    try:
        _sr_db = get_db()
        _sr_cur = _sr_db.cursor()
        if USE_POSTGRES:
            _sr_cur.execute("SELECT * FROM savings_rules WHERE user_id = %s", (current_user.id,))
        else:
            _sr_cur.execute("SELECT * FROM savings_rules WHERE user_id = ?", (current_user.id,))
        _sr_cols = [d[0] for d in _sr_cur.description]
        _savings_rules = [dict(zip(_sr_cols, r)) for r in _sr_cur.fetchall()]
        _sr_cur.close()
        release_db(_sr_db)
    except Exception:
        _savings_rules = []

    for rule in _savings_rules:
        if rule.get("day") is None or rule.get("is_paused"):
            continue
        freq = rule.get("frequency", "monthly")
        from_acc = rule.get("from_account", "")
        if from_acc not in accounts or accounts[from_acc].get("is_locked"):
            continue
        # A rule's destination can also become locked independently of its
        # source (e.g. a Pro->Free downgrade locking the linked savings
        # account a goal commitment feeds) - forecast()/api_snapshot()
        # already handle this implicitly (their `simulated`/`accounts`
        # dicts are pre-filtered to exclude locked accounts entirely, so
        # `to_acc in simulated` already fails safely), but this function's
        # `accounts` dict still contains locked accounts (with the flag
        # set) for its own balance-aggregation step, so the check has to
        # be explicit here. Pauses the WHOLE rule rather than only skipping
        # the credit side - continuing to deduct from Safe to Spend for a
        # transfer that can't actually land anywhere would be exactly the
        # "committing against a frozen account" the pause is meant to
        # prevent, not a partial fix. Only applies when to_account is a
        # real value - debt/standalone-goal commitments deliberately leave
        # it '' and are unaffected (see database.py's goal_id migration).
        to_acc_check = rule.get("to_account", "")
        if to_acc_check and to_acc_check in accounts and accounts[to_acc_check].get("is_locked"):
            continue

        sr_candidates = []
        if period_end is not None:
            if freq == "monthly":
                import calendar as _cal3
                # Look up to 5 days beyond today to catch rules that cross a month boundary
                _sr_extend = today_date + timedelta(days=5)
                _sr_fy, _sr_fm = today_date.year, today_date.month
                while date(_sr_fy, _sr_fm, 1) <= _sr_extend:
                    dim = _cal3.monthrange(_sr_fy, _sr_fm)[1]
                    due = date(_sr_fy, _sr_fm, min(rule["day"], dim))
                    in_period = today_date < due <= period_end
                    # Also include if exactly 5 days out (rule crosses into next month near month-end)
                    near_term = due > today_date + timedelta(days=4) and due <= _sr_extend
                    if today_date < due and (in_period or near_term):
                        sr_candidates.append(due)
                    _sr_fm = _sr_fm + 1 if _sr_fm < 12 else 1
                    _sr_fy = _sr_fy if _sr_fm > 1 else _sr_fy + 1
        else:
            if rule["day"] > current_day:
                import calendar as _cal3
                dim = _cal3.monthrange(today_date.year, today_date.month)[1]
                sr_candidates.append(date(today_date.year, today_date.month, min(rule["day"], dim)))

        for due in sr_candidates:
            amt = float(rule["amount"])
            all_future_bills += amt
            future_bills_list.append({
                "id": rule["id"],
                "name": rule["name"],
                "amount": amt,
                "day": rule["day"],
                "account": from_acc,
                "due_date": due.isoformat(),
                "account_type": accounts[from_acc]["type"],
            })
            if accounts[from_acc]["type"] in spending_types:
                spending_future_bills += amt

    # --- Pending income arriving later this cycle (for display in breakdown only) ---
    future_income = 0.0
    future_income_list = []
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT * FROM income WHERE user_id = %s", (current_user.id,))
        else:
            cursor.execute("SELECT * FROM income WHERE user_id = ?", (current_user.id,))
        cols = [d[0] for d in cursor.description]
        income_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        cursor.close()
        release_db(db)
        income_rows = _resolve_income_rows(income_rows, current_user.id)

        import calendar as _cal
        month_end = date(today.year, today.month, _cal.monthrange(today.year, today.month)[1])
        tomorrow = today.date() + timedelta(days=1)
        income_period_end = period_end if period_end is not None else month_end

        for inc in income_rows:
            amount = float(inc.get("amount") or 0)
            if inc.get("_distribution") == "spread":
                # Spread-evenly self-employed income: no discrete payment date at
                # all - accrue a flat daily amount for every remaining day in
                # the period instead of crediting the full average at once.
                cycle_len = _self_employed_cycle_length_days(current_user.id)
                daily_amount = amount / cycle_len if cycle_len else 0.0
                num_days = max(0, (income_period_end - tomorrow).days + 1)
                if num_days and daily_amount:
                    spread_total = round(daily_amount * num_days, 2)
                    future_income += spread_total
                    future_income_list.append({"name": inc["name"], "amount": spread_total, "day": None, "date": income_period_end.isoformat()})
                continue
            dates = income_engine.get_payment_dates(inc, tomorrow, income_period_end)
            for d in dates:
                future_income += amount
                future_income_list.append({"name": inc["name"], "amount": amount, "day": d.day, "date": d.isoformat()})
    except Exception as e:
        logger.debug(f"Could not load future income for overview: {e}")

    # Safe to spend = balance + income arriving in period − bills due in period
    safe_spending = max(0.0, spending_balance + future_income - spending_future_bills)
    shortfall = max(0.0, spending_future_bills - spending_balance - future_income)
    net_worth = spending_balance + savings_balance

    return {
        "spending_balance": spending_balance,
        "future_bills": all_future_bills,
        # Spending-account-linked bills/events only - the figure Safe to
        # Spend is actually derived from (see safe_spending above). Exposed
        # separately so the Bills Left headline can show THIS, not the
        # all-inclusive future_bills total, while the breakdown still lists
        # everything (see future_bills_list's account_type field).
        "future_bills_spending": spending_future_bills,
        "future_income": future_income,
        "future_income_list": sorted(future_income_list, key=lambda x: x.get("date", "")),
        "safe_spending": safe_spending,
        "shortfall": shortfall,
        "savings_balance": savings_balance,
        "net_worth": net_worth,
        "spending_accounts": sorted(spending_accounts, key=lambda x: x["name"].lower()),
        "savings_accounts": sorted(savings_accounts, key=lambda x: x["name"].lower()),
        "future_bills_list": sorted(future_bills_list, key=lambda x: x["due_date"]),
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

    today_cap = min(cycle_end_date, date.today())

    # Load per-occurrence amount overrides keyed by (type, source_id, iso_date)
    overrides = {}
    try:
        db_ov = get_db()
        cur_ov = db_ov.cursor()
        if USE_POSTGRES:
            cur_ov.execute(
                "SELECT type, source_id, date, amount FROM cycle_overrides WHERE user_id = %s",
                (current_user.id,)
            )
            for row in cur_ov.fetchall():
                overrides[(row[0], row[1], row[2])] = row[3]
        else:
            cur_ov.execute(
                "SELECT type, source_id, date, amount FROM cycle_overrides WHERE user_id = ?",
                (current_user.id,)
            )
            for row in cur_ov.fetchall():
                overrides[(row["type"], row["source_id"], row["date"])] = row["amount"]
        cur_ov.close()
        release_db(db_ov)
    except Exception as e:
        logger.debug(f"Could not load cycle overrides: {e}")

    # Normal (discretionary) spending from logged transactions
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT amount, description, date, account FROM transactions
            WHERE date::date >= %s AND date::date <= %s AND user_id = %s
            AND amount < 0 AND type != 'transfer' AND type != 'bill'
            """,
            (cycle_start_date.isoformat(), today_cap.isoformat(), current_user.id)
        )
    else:
        cursor.execute(
            """
            SELECT amount, description, date, account FROM transactions
            WHERE date >= ? AND date <= ? AND user_id = ?
            AND amount < 0 AND type != 'transfer' AND type != 'bill'
            """,
            (cycle_start_date.isoformat(), today_cap.isoformat(), current_user.id)
        )
    rows = cursor.fetchall()
    cursor.close()
    release_db(db)

    normal = 0.0
    normal_list = []
    for r in rows:
        if USE_POSTGRES:
            amount = abs(r[0]); description = r[1]; tx_date = r[2]; account = r[3]
        else:
            amount = abs(r["amount"]); description = r["description"]; tx_date = r["date"]; account = r["account"]
        normal += amount
        normal_list.append({"description": description, "amount": amount, "date": tx_date, "account": account})

    # Bills paid: scheduled expenses whose due date falls in [cycle_start_date, today_cap]
    import calendar as _cal
    scheduled = 0.0
    bills_list = []
    scheduled_expenses = load_scheduled_expenses_web()

    check_months = []
    y, m = cycle_start_date.year, cycle_start_date.month
    while date(y, m, 1) <= today_cap:
        check_months.append((y, m))
        m = m + 1 if m < 12 else 1
        y = y if m > 1 else y + 1

    for expense in scheduled_expenses:
        if expense["day"] is None:
            continue
        freq = expense.get("frequency") or "monthly"
        candidates = []
        if freq == "monthly":
            for (ey, em) in check_months:
                dim = _cal.monthrange(ey, em)[1]
                due = shift_weekend_to_monday(date(ey, em, min(expense["day"], dim)))
                if cycle_start_date <= due <= today_cap:
                    candidates.append(due)
        elif freq == "yearly":
            expense_month = expense.get("month")
            if not expense_month:
                continue
            for (ey, em) in check_months:
                if em != expense_month:
                    continue
                dim = _cal.monthrange(ey, em)[1]
                due = shift_weekend_to_monday(date(ey, em, min(expense["day"], dim)))
                if cycle_start_date <= due <= today_cap:
                    candidates.append(due)
        for due in candidates:
            bill_amt = overrides.get(('bill', expense["id"], due.isoformat()), expense["amount"])
            scheduled += bill_amt
            bills_list.append({
                "description": expense["name"],
                "amount": bill_amt,
                "date": due.isoformat(),
                "date_display": f"{due.day} {due.strftime('%b')}",
                "account": expense.get("account", ""),
                "source_id": expense["id"],
                "item_type": "bill",
            })

    # Income received: scheduled income sources via the income engine
    income_received = 0.0
    income_list = []
    try:
        db2 = get_db()
        cursor2 = db2.cursor()
        if USE_POSTGRES:
            cursor2.execute("SELECT * FROM income WHERE user_id = %s", (current_user.id,))
        else:
            cursor2.execute("SELECT * FROM income WHERE user_id = ?", (current_user.id,))
        cols2 = [d[0] for d in cursor2.description]
        income_rows = [dict(zip(cols2, row)) for row in cursor2.fetchall()]
        cursor2.close()
        release_db(db2)
        income_rows = _resolve_income_rows(income_rows, current_user.id)
        for inc in income_rows:
            if inc.get("_distribution") == "spread":
                # Spread-evenly income has no discrete "received" event to list -
                # it's a continuous accrual, not a point-in-time occurrence.
                continue
            base_amount = float(inc.get("amount") or 0)
            dates = income_engine.get_payment_dates(inc, cycle_start_date, today_cap)
            for d in dates:
                inc_amt = overrides.get(('income', inc["id"], d.isoformat()), base_amount)
                income_received += inc_amt
                income_list.append({
                    "description": inc["name"],
                    "amount": inc_amt,
                    "date": d.isoformat(),
                    "date_display": f"{d.day} {d.strftime('%b')}",
                    "account": inc.get("account", ""),
                    "source_id": inc["id"],
                    "item_type": "income",
                })
    except Exception as e:
        logger.debug(f"Could not compute scheduled income: {e}")

    return {
        "normal": normal,
        "scheduled": scheduled,
        "total": normal + scheduled,
        "normal_list": normal_list,
        "bills_list": sorted(bills_list, key=lambda x: x["date"]),
        "income_received": income_received,
        "income_list": sorted(income_list, key=lambda x: x["date"]),
    }

def get_my_money_setup(user_id):
    """Return My Money setup checklist state. Never raises."""
    db = get_db()
    cur = db.cursor()
    ph = '%s' if USE_POSTGRES else '?'
    try:
        cur.execute(f"SELECT COALESCE(setup_dismissed,0) FROM users WHERE id = {ph}", (user_id,))
        row = cur.fetchone()
        if not row or row[0]:
            return {'show': False}

        cur.execute(
            f"SELECT id, balance, COALESCE(is_seeded,0), COALESCE(user_verified,0) "
            f"FROM accounts WHERE user_id = {ph} AND active = 1",
            (user_id,),
        )
        accounts = cur.fetchall()
        seeded = next((a for a in accounts if a[2] == 1), None)

        cur.execute(
            f"SELECT id, amount, frequency, COALESCE(user_verified,0) "
            f"FROM income WHERE user_id = {ph} ORDER BY id LIMIT 1",
            (user_id,),
        )
        inc_row = cur.fetchone()

        cur.execute(
            f"SELECT COUNT(*), COALESCE(SUM(amount),0) "
            f"FROM scheduled_expenses WHERE user_id = {ph}",
            (user_id,),
        )
        bills_row = cur.fetchone()
        bill_count = int(bills_row[0]) if bills_row else 0
        bill_total = float(bills_row[1]) if bills_row else 0.0

        if seeded is not None:
            acct_id, acct_bal, _, acct_uv = seeded
            inc_id  = inc_row[0] if inc_row else None
            inc_amt = float(inc_row[1]) if inc_row else 0.0
            inc_frq = inc_row[2] if inc_row else 'monthly'
            inc_uv  = inc_row[3] if inc_row else 0

            acct_ok  = bool(acct_uv) or abs(acct_bal - 850.0) > 0.01
            inc_ok   = inc_row is not None and (bool(inc_uv) or abs(inc_amt - 2500.0) > 0.01)
            bills_ok = bill_count >= 3

            done     = sum([acct_ok, inc_ok, bills_ok])
            progress = done * 33 if done < 3 else 100

            steps = [
                {
                    'key': 'account',
                    'status': 'verified' if acct_ok else 'review',
                    'label': 'Verify your account balance',
                    'description': f'Current balance: £{acct_bal:,.2f}',
                    'action_tab': f'edit_account_{acct_id}',
                },
                {
                    'key': 'income',
                    'status': 'verified' if inc_ok else 'review',
                    'label': 'Verify your income',
                    'description': f'£{inc_amt:,.2f} {inc_frq}' if inc_row else 'No income set',
                    'action_tab': f'edit_income_{inc_id}' if inc_id else 'income',
                },
                {
                    'key': 'bills',
                    'status': 'verified' if bills_ok else 'review',
                    'label': 'Check your bills',
                    'description': f'{bill_count} bill{"s" if bill_count != 1 else ""} · £{bill_total:,.2f}/month' if bill_count else 'No bills yet',
                    'action_tab': 'bills',
                },
            ]
            return {'show': True, 'version': 'B', 'progress': progress, 'steps': steps}

        else:
            has_acct  = any(float(a[1]) > 0 for a in accounts)
            has_inc   = inc_row is not None
            has_bills = bill_count > 0

            done     = sum([has_acct, has_inc, has_bills])
            progress = done * 33 if done < 3 else 100

            steps = [
                {
                    'key': 'account',
                    'status': 'complete' if has_acct else 'incomplete',
                    'label': 'Add an account',
                    'description': 'Enter your current balance to get started.',
                    'action_tab': 'accounts',
                },
                {
                    'key': 'income',
                    'status': 'complete' if has_inc else ('incomplete' if has_acct else 'locked'),
                    'label': 'Add your income',
                    'description': 'Set your pay so Spendara knows your pay cycle.',
                    'action_tab': 'income',
                },
                {
                    'key': 'bills',
                    'status': 'complete' if has_bills else ('incomplete' if has_acct else 'locked'),
                    'label': 'Add recurring bills',
                    'description': 'Add rent, subscriptions and regular expenses.',
                    'action_tab': 'bills',
                },
            ]
            return {'show': True, 'version': 'A', 'progress': progress, 'steps': steps}

    except Exception:
        return {'show': False}
    finally:
        cur.close()
        release_db(db)


def get_my_money_dot(user_id):
    """Return True if the My Money nav dot should be shown."""
    db = get_db()
    cur = db.cursor()
    ph = '%s' if USE_POSTGRES else '?'
    try:
        cur.execute(
            f"SELECT COALESCE(setup_dismissed,0), created_at FROM users WHERE id = {ph}",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or row[0]:
            return False
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        return str(row[1]) >= cutoff
    except Exception:
        return False
    finally:
        cur.close()
        release_db(db)


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
    locked_accounts = {r["name"] for r in accounts_rows if r.get("is_locked")}

    for r in accounts_rows:
        accounts[r["name"]] = {
            "id": r["id"],
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"]),
            "include_in_overview": bool(r.get("include_in_overview", 1)),
            "savings_type": r.get("savings_type"),
            "is_locked": bool(r.get("is_locked")),
            "alert_threshold": r.get("alert_threshold"),
        }

    import cycle_engine as _ce
    _cycle = _ce.get_cycle(current_user.id)
    cycle_start_date = _cycle["display_start"]
    cycle_end_date = _cycle["display_end"]
    safe_boundary = _cycle["safe_boundary"]

    overview = calculate_financial_overview(accounts, period_end=cycle_end_date, safe_boundary=safe_boundary)

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
    _ob_dismissed = False
    _show_welcome = False
    try:
        if USE_POSTGRES:
            _ob_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_dismissed BOOLEAN DEFAULT FALSE")
            _ob_cur.execute("SELECT onboarding_dismissed, COALESCE(show_welcome_modal, 0) FROM users WHERE id = %s", (current_user.id,))
        else:
            _ob_cur.execute("SELECT onboarding_dismissed, COALESCE(show_welcome_modal, 0) FROM users WHERE id = ?", (current_user.id,))
        _ob_row = _ob_cur.fetchone()
        if _ob_row:
            _ob_dismissed = bool(_ob_row[0])
            _show_welcome = bool(_ob_row[1])
        if _show_welcome:
            _ph = '%s' if USE_POSTGRES else '?'
            _ob_cur.execute(f"UPDATE users SET show_welcome_modal = 0 WHERE id = {_ph}", (current_user.id,))
        _ob_db.commit()
    except Exception:
        _ob_dismissed = False
        _show_welcome = False
    finally:
        _ob_cur.close(); release_db(_ob_db)
    show_onboarding = (len(active_accounts) == 0 and not _ob_dismissed) or _show_welcome or request.args.get('onboarding') == '1'

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
                            "include_in_overview": bool(r.get("include_in_overview", 1)),
                            "savings_type": r.get("savings_type"),
                            "is_locked": bool(r.get("is_locked")),
                            "alert_threshold": r.get("alert_threshold"),
                        }
                    overview = calculate_financial_overview(accounts, period_end=cycle_end_date, safe_boundary=safe_boundary)
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

    # Suppress automation banner for users who signed up within the last 24 hours and
    # have seeded data — the banner is confusing before they've oriented themselves.
    if pending_items:
        try:
            _sb_db = get_db(); _sb_cur = _sb_db.cursor()
            _sb_ph = '%s' if USE_POSTGRES else '?'
            _sb_cur.execute(f"SELECT created_at FROM users WHERE id = {_sb_ph}", (current_user.id,))
            _sb_row = _sb_cur.fetchone()
            if _sb_row:
                _sb_cutoff = (date.today() - timedelta(days=1)).isoformat()
                if str(_sb_row[0]) >= _sb_cutoff:
                    _sb_cur.execute(
                        f"SELECT 1 FROM accounts WHERE user_id = {_sb_ph} AND COALESCE(is_seeded,0)=1 LIMIT 1",
                        (current_user.id,),
                    )
                    if _sb_cur.fetchone():
                        pending_items = []
            _sb_cur.close(); release_db(_sb_db)
        except Exception:
            pass

    days_to_payday = (safe_boundary - date.today()).days + 1
    show_payday_countdown = _cycle["mode_used"] == "automatic"

    try:
        spending_alerts = get_triggered_spending_alerts(current_user.id, accounts)
    except Exception as e:
        logger.debug(f"Spending alert threshold check error: {e}")
        spending_alerts = []

    # Goals entry point on Home — active goals only (a completed goal has
    # nothing left to invite action on), most recent first. Reuses the same
    # _compute_goal_progress() helper the My Money > Goals tab uses, so the
    # percentage shown here can never drift out of sync with the full view.
    try:
        _gdb = get_db()
        _gcur = _gdb.cursor()
        if USE_POSTGRES:
            _gcur.execute("SELECT * FROM goals WHERE user_id = %s AND status = 'active' ORDER BY created_at DESC", (current_user.id,))
        else:
            _gcur.execute("SELECT * FROM goals WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC", (current_user.id,))
        _gcols = [d[0] for d in _gcur.description]
        goals_home = [dict(zip(_gcols, row)) for row in _gcur.fetchall()] if USE_POSTGRES else [dict(row) for row in _gcur.fetchall()]
        _gcur.close()
        release_db(_gdb)
        _accounts_by_id_home = {a["id"]: a for a in get_all_accounts(current_user.id)}
        for _g in goals_home:
            _g["target_amount"] = float(_g["target_amount"] or 0)
            _g["progress"] = _compute_goal_progress(_g, current_user.id, accounts_by_id=_accounts_by_id_home)
        # Batched over every active goal (not just the ones with a target
        # date) so the fallback-estimate split — see manage()'s Goals tab —
        # divides across the same denominator here as it does there.
        _pace_map, _ = _compute_goal_pace_map(goals_home, current_user.id, accounts_by_id=_accounts_by_id_home)
        for _g in goals_home:
            # Only worth attaching a pace/on-track projection when there's a
            # target date to compare against — Home's card is compact and
            # only ever surfaces a small colour cue, not the full projected
            # date text (see templates/index.html), so there's nothing to
            # show here for a goal with no target date anyway.
            if _g.get("target_date"):
                _pace_per_day, _is_estimate = _pace_map[_g["id"]]
                _g["projection"] = _project_goal_completion(_g["progress"], _pace_per_day, _g.get("target_date"), is_estimate=_is_estimate)
            else:
                _g["projection"] = None
            _g["display"] = _build_goal_display(_g)
    except Exception as e:
        logger.debug(f"Home goals summary error: {e}")
        goals_home = []

    return render_template(
        "index.html",
        message=request.args.get("msg", ""),
        goals_home=goals_home,
        accounts=active_accounts,
        locked_accounts=locked_accounts,
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
        show_payday_countdown=show_payday_countdown,
        show_my_money_dot=get_my_money_dot(current_user.id),
        spending_alerts=spending_alerts,
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

# --- MY MONEY SETUP ---
@app.post("/my-money/setup/dismiss")
@login_required
def my_money_setup_dismiss():
    data = request.get_json(silent=True) or {}
    token = data.get('csrf_token') or request.headers.get('X-CSRF-Token')
    if not token or token != session.get('csrf_token'):
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    db = get_db(); cur = db.cursor()
    try:
        ph = '%s' if USE_POSTGRES else '?'
        cur.execute(f"UPDATE users SET setup_dismissed = 1 WHERE id = {ph}", (current_user.id,))
        db.commit()
        return jsonify({'ok': True})
    except Exception:
        db.rollback()
        return jsonify({'ok': False}), 500
    finally:
        cur.close(); release_db(db)


@app.get("/my-money/setup/state")
@login_required
def my_money_setup_state():
    return jsonify(get_my_money_setup(current_user.id))

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
    due_date_str = shift_weekend_to_monday(_date(today.year, today.month, due_day)).isoformat()

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
    bust_forecast_cache(current_user.id)
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


@app.post("/api/edit-pending-item")
@login_required
def api_edit_pending_item():
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"error": "CSRF"}), 403
    item_type = data.get("type")
    item_id = data.get("item_id")
    name = (data.get("name") or "").strip()
    account = (data.get("account") or "").strip()
    try:
        amount = float(data.get("amount") or 0)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    try:
        day = max(1, min(31, int(data.get("day") or 1)))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid day"}), 400
    if not name or not account or not item_id or item_type not in ("bill", "income"):
        return jsonify({"error": "Missing or invalid fields"}), 400
    db = get_db()
    cursor = db.cursor()
    try:
        if item_type == "bill":
            if USE_POSTGRES:
                cursor.execute(
                    "UPDATE scheduled_expenses SET name=%s, amount=%s, account=%s, day=%s WHERE id=%s AND user_id=%s",
                    (name, amount, account, day, item_id, current_user.id)
                )
            else:
                cursor.execute(
                    "UPDATE scheduled_expenses SET name=?, amount=?, account=?, day=? WHERE id=? AND user_id=?",
                    (name, amount, account, day, item_id, current_user.id)
                )
        else:
            if USE_POSTGRES:
                cursor.execute(
                    "UPDATE income SET name=%s, amount=%s, account=%s, day=%s WHERE id=%s AND user_id=%s",
                    (name, amount, account, day, item_id, current_user.id)
                )
            else:
                cursor.execute(
                    "UPDATE income SET name=?, amount=?, account=?, day=? WHERE id=? AND user_id=?",
                    (name, amount, account, day, item_id, current_user.id)
                )
        db.commit()
        bust_forecast_cache(current_user.id)
    except Exception as e:
        logger.error(f"api_edit_pending_item: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        cursor.close()
        release_db(db)
    return jsonify({"ok": True, "name": name, "amount": amount, "account": account, "day": day})


@app.get("/api/overview")
@login_required
def api_overview():
    """Return Financial Overview figures for an arbitrary date range (session-only custom view)."""
    from datetime import date as _date
    try:
        start = _date.fromisoformat(request.args.get("start", ""))
        end = _date.fromisoformat(request.args.get("end", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid dates"}), 400
    today = _date.today()
    if end <= start:
        return jsonify({"error": "end must be after start"}), 400
    if (end - start).days > 366 or abs((start - today).days) > 366 or abs((end - today).days) > 366:
        return jsonify({"error": "dates out of range"}), 400

    monthly = calculate_monthly_spending(start, end)

    accounts_rows = get_active_accounts(current_user.id)
    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "id": r["id"],
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"]),
            "include_in_overview": bool(r.get("include_in_overview", 1)),
            "savings_type": r.get("savings_type"),
            "is_locked": bool(r.get("is_locked")),
        }
    ov = calculate_financial_overview(accounts, period_end=end)

    # Filter future bills/income to those whose date falls on or after the selected start.
    # calculate_financial_overview always uses today as the lower bound; when the custom
    # start is in the future we need to drop items that fall between today and start.
    start_iso = start.isoformat()
    filtered_bills = [b for b in ov["future_bills_list"] if b.get("due_date", "") >= start_iso]
    filtered_bills_total = sum(b["amount"] for b in filtered_bills)
    # Spending-account-linked only - what the Bills Left headline should
    # show (see calculate_financial_overview's future_bills_spending).
    filtered_bills_spending_total = sum(b["amount"] for b in filtered_bills if b.get("account_type") in ("current", "cash"))
    filtered_income = [i for i in ov["future_income_list"] if i.get("date", "") >= start_iso]

    return jsonify({
        "income_received": monthly["income_received"],
        "income_list": monthly["income_list"],
        "scheduled": monthly["scheduled"],
        "bills_list": monthly["bills_list"],
        "future_bills": filtered_bills_total,
        "future_bills_spending": filtered_bills_spending_total,
        "future_bills_list": filtered_bills,
        "future_income": ov["future_income"],
        "future_income_list": filtered_income,
        "safe_spending": ov["safe_spending"],
        "shortfall": ov["shortfall"],
        "display_start": f"{start.day} {start.strftime('%b')}",
        "display_end": f"{end.day} {end.strftime('%b')}",
    })


@app.post("/api/edit-cycle-item")
@login_required
def api_edit_cycle_item():
    from database import get_db, USE_POSTGRES
    import hmac as _hmac
    data = request.get_json(silent=True) or {}
    if not _hmac.compare_digest(str(data.get("csrf_token", "")), str(session.get("csrf_token", ""))):
        return jsonify({"ok": False, "error": "CSRF"}), 403
    item_type = data.get("type", "")
    if item_type not in ("income", "bill"):
        return jsonify({"ok": False, "error": "invalid type"}), 400
    try:
        source_id = int(data.get("source_id"))
        occurrence_date = str(data.get("date", ""))
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid data"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be positive"}), 400
    if len(occurrence_date) != 10:
        return jsonify({"ok": False, "error": "invalid date"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO cycle_overrides (user_id, type, source_id, date, amount)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, type, source_id, date) DO UPDATE SET amount = EXCLUDED.amount
                """,
                (current_user.id, item_type, source_id, occurrence_date, amount)
            )
        else:
            cur.execute(
                """
                INSERT INTO cycle_overrides (user_id, type, source_id, date, amount)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id, type, source_id, date) DO UPDATE SET amount = excluded.amount
                """,
                (current_user.id, item_type, source_id, occurrence_date, amount)
            )
        db.commit()
        cur.close()
        return jsonify({"ok": True, "amount": amount})
    except Exception as e:
        logger.warning(f"api_edit_cycle_item: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        release_db(db)


@app.post("/api/set-primary-income")
@login_required
def api_set_primary_income():
    import hmac as _hmac
    data = request.get_json(silent=True) or {}
    if not _hmac.compare_digest(str(data.get("csrf_token", "")), str(session.get("csrf_token", ""))):
        return jsonify({"ok": False, "error": "CSRF"}), 403
    try:
        income_id = int(data.get("income_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid id"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        # Clear all primary flags for this user, then set the selected one
        if USE_POSTGRES:
            cur.execute("UPDATE income SET is_primary = 0 WHERE user_id = %s", (current_user.id,))
            cur.execute("UPDATE income SET is_primary = 1 WHERE id = %s AND user_id = %s", (income_id, current_user.id))
        else:
            cur.execute("UPDATE income SET is_primary = 0 WHERE user_id = ?", (current_user.id,))
            cur.execute("UPDATE income SET is_primary = 1 WHERE id = ? AND user_id = ?", (income_id, current_user.id))
        db.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning(f"api_set_primary_income: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        release_db(db)


# --- TRANSACTIONS PAGE ---
# Lists all transactions for the current user, newest first
@app.get("/transactions")
@login_required
def transactions():
    track('page_view.transactions')
    tx = get_recent_transactions(current_user.id)

    return render_template(
        "transactions.html",
        transactions=tx,
        show_my_money_dot=get_my_money_dot(current_user.id),
        message=request.args.get("msg", ""),
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


@app.post("/transactions/bulk-delete")
@login_required
def bulk_delete():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return redirect(url_for('transactions'))
    tx_ids = request.form.getlist('tx_ids')
    if not tx_ids:
        return redirect(url_for('transactions'))
    from database import get_db, USE_POSTGRES, release_db
    db = get_db()
    cursor = db.cursor()
    deleted = 0
    for raw_id in tx_ids:
        try:
            tid = int(raw_id)
        except (ValueError, TypeError):
            continue
        if USE_POSTGRES:
            cursor.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tid, current_user.id))
        else:
            cursor.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tid, current_user.id))
        deleted += cursor.rowcount
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for('transactions', msg=f"{deleted} transaction{'s' if deleted != 1 else ''} deleted."))


# --- ACTIONS PAGE ---
# Shows forms to add expenses, income, transfers, and investment updates
@app.get("/actions")
@login_required
def actions():
    track('page_view.actions')
    accounts_rows = get_active_accounts(current_user.id)
    accounts = [r["name"] for r in accounts_rows]
    locked_accounts = {r["name"] for r in accounts_rows if r.get("is_locked")}

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
    return render_template("actions.html", accounts=accounts, locked_accounts=locked_accounts, investments=investments, message=request.args.get("msg", ""), today=date.today().isoformat(), recent_tx=recent_tx, bank_connected=bank_connected, truelayer_live=TRUELAYER_LIVE)

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
        _dim = calendar.monthrange(year, month)[1]
        acc_bills_to_pay = []
        for b in bills:
            if b["account"] != acc_name or b["day"] is None:
                continue
            try:
                nominal_due = shift_weekend_to_monday(date(year, month, min(b["day"], _dim)))
            except ValueError:
                continue
            if nominal_due > today:
                acc_bills_to_pay.append(b)

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

    if _is_account_locked(current_user.id, bill["account"]):
        redirect_to = request.form.get("redirect_to") or url_for("flow")
        return redirect(f"{redirect_to}?msg='{bill['account']}'+is+locked+—+upgrade+to+Pro+to+unlock+it.")

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

    if _is_account_locked(current_user.id, income["account"]):
        redirect_to = request.form.get("redirect_to") or url_for("flow")
        return redirect(f"{redirect_to}?msg='{income['account']}'+is+locked+—+upgrade+to+Pro+to+unlock+it.")

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

    if _is_account_locked(current_user.id, account):
        return redirect(url_for("actions", msg=f"'{account}' is locked — upgrade to Pro to unlock it."))

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

    if _is_account_locked(current_user.id, account):
        return redirect(url_for("actions", msg=f"'{account}' is locked — upgrade to Pro to unlock it."))

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

    if _is_account_locked(current_user.id, account):
        return {"ok": False, "error": f"'{account}' is locked — upgrade to Pro to unlock it."}, 403

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

    if _is_account_locked(current_user.id, account):
        return {"ok": False, "error": f"'{account}' is locked — upgrade to Pro to unlock it."}, 403

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

    locked_account = from_account if _is_account_locked(current_user.id, from_account) else (
        to_account if _is_account_locked(current_user.id, to_account) else None
    )
    if locked_account:
        return redirect(url_for("actions", msg=f"'{locked_account}' is locked — upgrade to Pro to unlock it."))

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
    bust_forecast_cache(current_user.id)

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
    bust_forecast_cache(current_user.id)
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
    bust_forecast_cache(current_user.id)

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
    locked_accounts = {r["name"] for r in accounts_rows if r.get("is_locked")}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": r["balance"],
            "type": r["type"],
            "active": bool(r["active"]),
            "is_locked": bool(r.get("is_locked")),
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

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    afford_savings_rules = [dict(zip(cols, row)) for row in cursor.fetchall()]

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
    # Locked accounts can't actually be used for a purchase (blocked server-side),
    # and their frozen balance shouldn't feed the simulation as if it were live.
    unlocked_accounts = {k: v for k, v in accounts.items() if not v.get("is_locked")}
    spending_accounts = [a for a in unlocked_accounts if unlocked_accounts[a]["type"] in ("current","cash") and unlocked_accounts[a]["active"]]

    for acc in spending_accounts:
        temp_accounts = {k: v.copy() for k, v in unlocked_accounts.items()}
        temp_accounts[acc]["balance"] -= amount

        final_bal, lowest_bal = simulate_balances_until(horizon, temp_accounts, scheduled, future_events, afford_savings_rules)

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
        locked_accounts=locked_accounts,
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
    # Locked accounts are frozen and excluded from this simulation entirely —
    # same reasoning as forecast(): a balance that can't change shouldn't be
    # projected forward as if it were live. Names captured before filtering
    # so a savings_rule whose to_account is locked can be paused entirely
    # below (see the loop) rather than just silently skipping the credit
    # side while still deducting from an unlocked from_account.
    _snap_locked_names = {r["name"] for r in accounts_rows if r.get("is_locked")}
    accounts_rows = [r for r in accounts_rows if not r.get("is_locked")]
    accounts = {}
    for r in accounts_rows:
        accounts[r["name"]] = {
            "balance": float(r["balance"]),
            "type": r["type"],
            "savings_type": r.get("savings_type"),
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
    income_rows = _resolve_income_rows(income_rows, current_user.id)

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

    if USE_POSTGRES:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT * FROM savings_rules WHERE user_id = ?", (current_user.id,))
    cols = [d[0] for d in cursor.description]
    snap_savings_rules = [dict(zip(cols, row)) for row in cursor.fetchall()]

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
    min_balances = {name: float(info["balance"]) for name, info in accounts.items()}
    min_balance_dates = {name: "Today" for name in accounts}
    income_arriving = []
    bills_due = []

    # Pre-compute income dates for the snapshot window. Spread-evenly
    # self-employed income has no discrete payment date - it accrues a flat
    # daily amount instead, tracked separately from the date-keyed dict.
    snap_income_by_date: dict = {}
    spread_rows = []
    for row in income_rows:
        if row.get("_distribution") == "spread":
            cycle_len = _self_employed_cycle_length_days(current_user.id)
            daily_amount = float(row.get("amount") or 0) / cycle_len if cycle_len else 0.0
            if daily_amount:
                spread_rows.append({**row, "_daily_amount": daily_amount})
            continue
        for d in income_engine.get_payment_dates(row, today + timedelta(days=1), target):
            snap_income_by_date.setdefault(d, []).append(row)

    sim_day = today + timedelta(days=1)
    while sim_day <= target:
        day_str = f"{sim_day.day} {sim_day.strftime('%b')}"

        # Income
        for row in snap_income_by_date.get(sim_day, []):
            acc = row.get("account", "")
            if acc not in simulated:
                continue
            amt = float(row["amount"])
            income_arriving.append({"name": row["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc, "item_id": row.get("id"), "item_type": "income"})
            simulated[acc] += amt

        for row in spread_rows:
            acc = row.get("account", "")
            if acc not in simulated:
                continue
            amt = row["_daily_amount"]
            income_arriving.append({"name": row["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc, "item_id": row.get("id"), "item_type": "income"})
            simulated[acc] += amt

        # Scheduled expenses
        for expense in scheduled:
            exp_day = expense.get("day")
            if exp_day is None:
                continue
            freq = expense.get("frequency", "monthly")
            acc = expense.get("account", "")
            if acc not in simulated:
                continue
            amt = float(expense["amount"])
            applies = False
            if freq == "monthly":
                try:
                    nominal = date(sim_day.year, sim_day.month, exp_day)
                except ValueError:
                    nominal = None
                if nominal is not None and shift_weekend_to_monday(nominal) == sim_day:
                    applies = True
            elif freq == "yearly":
                exp_month = expense.get("month")
                if exp_month == sim_day.month:
                    try:
                        nominal = date(sim_day.year, sim_day.month, exp_day)
                    except ValueError:
                        nominal = None
                    if nominal is not None and shift_weekend_to_monday(nominal) == sim_day:
                        applies = True
            if applies:
                bills_due.append({"name": expense["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc, "item_id": expense.get("id"), "item_type": "bill"})
                if acc in simulated:
                    simulated[acc] -= amt

        # Future events
        for event in future_events:
            if event["date"] == sim_day:
                acc = event["account"]
                if acc not in simulated:
                    continue
                amt = float(event["amount"])
                bills_due.append({"name": event["name"], "amount": amt, "date": day_str, "iso": sim_day.isoformat(), "account": acc, "item_id": None, "item_type": "event"})
                simulated[acc] -= amt

        # Savings rules — deduct from source, deposit to destination
        for rule in snap_savings_rules:
            if rule.get("day") is None or rule.get("is_paused"):
                continue
            freq = rule.get("frequency", "monthly")
            if freq == "monthly" and rule["day"] == sim_day.day:
                # Skip next-month occurrences that are less than 5 days away — avoids
                # showing a "recurring" rule prematurely when the month boundary is near.
                if sim_day.month != today.month and sim_day < today + timedelta(days=5):
                    continue
                from_acc = rule.get("from_account", "")
                to_acc = rule.get("to_account", "")
                amt = float(rule["amount"])
                # A non-empty to_account that's locked pauses the WHOLE rule,
                # not just the credit side - continuing to deduct from an
                # unlocked from_account for a transfer that can't land
                # anywhere would be exactly the "committing against a frozen
                # account" a pause is meant to prevent. An empty to_account
                # (debt/standalone goal commitments, by design) is unaffected.
                if to_acc and to_acc in _snap_locked_names:
                    continue
                if from_acc in simulated:
                    bills_due.append({
                        "name": f"Transfer to {to_acc}",
                        "amount": amt,
                        "date": day_str,
                        "iso": sim_day.isoformat(),
                        "account": from_acc,
                        "item_id": rule.get("id"),
                        "item_type": "savings_rule",
                    })
                    simulated[from_acc] -= amt
                if to_acc in simulated:
                    income_arriving.append({
                        "name": f"Transfer from {from_acc}",
                        "amount": amt,
                        "date": day_str,
                        "iso": sim_day.isoformat(),
                        "account": to_acc,
                        "item_id": rule.get("id"),
                        "item_type": "savings_rule",
                    })
                    simulated[to_acc] += amt

        # Track minimum balance per account across the simulation
        for name in simulated:
            if simulated[name] < min_balances[name]:
                min_balances[name] = simulated[name]
                min_balance_dates[name] = sim_day.strftime('%d/%m/%Y')

        sim_day += timedelta(days=1)

    return jsonify({
        "date": target.strftime('%d/%m/%Y'),
        "days": days,
        "accounts": {
            name: {
                "balance_today": round(accounts[name]["balance"], 2),
                "balance_on_date": round(simulated[name], 2),
                "change": round(simulated[name] - accounts[name]["balance"], 2),
                "type": accounts[name]["type"],
                "savings_type": accounts[name].get("savings_type"),
                "min_balance": round(min_balances[name], 2),
                "min_balance_date": min_balance_dates[name],
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
    # Cycle mode and primary income for Budget Cycle card
    cycle_mode = "manual"
    has_primary = False
    primary_income_name = None
    cycle_info = None
    try:
        from database import get_db as _get_db2, USE_POSTGRES as _UP2, release_db as _rel2
        _db2 = _get_db2()
        _cur2 = _db2.cursor()
        if _UP2:
            _cur2.execute("SELECT cycle_mode FROM users WHERE id = %s", (current_user.id,))
        else:
            _cur2.execute("SELECT cycle_mode FROM users WHERE id = ?", (current_user.id,))
        _row2 = _cur2.fetchone()
        if _row2 and _row2[0]:
            cycle_mode = _row2[0]
        if _UP2:
            _cur2.execute("SELECT name FROM income WHERE user_id = %s AND is_primary = 1 LIMIT 1", (current_user.id,))
        else:
            _cur2.execute("SELECT name FROM income WHERE user_id = ? AND is_primary = 1 LIMIT 1", (current_user.id,))
        _row3 = _cur2.fetchone()
        if _row3 and _row3[0]:
            has_primary = True
            primary_income_name = _row3[0]
        _cur2.close()
        _rel2(_db2)
    except Exception:
        pass
    try:
        import cycle_engine as _ce
        cycle_info = _ce.get_cycle(current_user.id)
        next_cycle_start = _ce.get_next_cycle_start(current_user.id)
    except Exception:
        next_cycle_start = None

    # Employment type + self-employed income averaging settings (New — Beta)
    employment_type = "employed"
    self_employed_income = None
    try:
        _db3 = get_db()
        _cur3 = _db3.cursor()
        if USE_POSTGRES:
            _cur3.execute("SELECT employment_type FROM users WHERE id = %s", (current_user.id,))
        else:
            _cur3.execute("SELECT employment_type FROM users WHERE id = ?", (current_user.id,))
        _row4 = _cur3.fetchone()
        if _row4 and _row4[0]:
            employment_type = _row4[0]
        if employment_type == "self_employed":
            if USE_POSTGRES:
                _cur3.execute("SELECT * FROM income WHERE user_id = %s AND rule_type = 'self_employed_average' LIMIT 1", (current_user.id,))
            else:
                _cur3.execute("SELECT * FROM income WHERE user_id = ? AND rule_type = 'self_employed_average' LIMIT 1", (current_user.id,))
            _cols4 = [d[0] for d in _cur3.description]
            _row5 = _cur3.fetchone()
            if _row5:
                self_employed_income = dict(zip(_cols4, _row5))
                self_employed_income["cfg"] = json.loads(self_employed_income.get("rule_config") or "{}")
        _cur3.close()
        release_db(_db3)
    except Exception:
        pass

    # Spending Alert Threshold (off / overall / per-account)
    alert_mode = None
    alert_overall_threshold = None
    alert_accounts = []
    try:
        _db4 = get_db()
        _cur4 = _db4.cursor()
        if USE_POSTGRES:
            _cur4.execute("SELECT alert_mode, alert_overall_threshold FROM users WHERE id = %s", (current_user.id,))
        else:
            _cur4.execute("SELECT alert_mode, alert_overall_threshold FROM users WHERE id = ?", (current_user.id,))
        _row6 = _cur4.fetchone()
        if _row6:
            alert_mode = _row6[0] if USE_POSTGRES else _row6["alert_mode"]
            alert_overall_threshold = _row6[1] if USE_POSTGRES else _row6["alert_overall_threshold"]
        _cur4.close()
        release_db(_db4)
        # Locked accounts aren't shown here - a threshold on a frozen account
        # could never trigger meaningfully, so they're not configurable.
        alert_accounts = [a for a in get_active_accounts(current_user.id) if not a.get("is_locked")]
    except Exception:
        pass

    return render_template("settings.html",
        is_pro=is_pro,
        message=request.args.get("msg", ""),
        auto_apply_enabled=auto_apply_enabled,
        auto_apply_confirm=auto_apply_confirm,
        budget_cycle_start=budget_cycle_start,
        notification_digest=notification_digest,
        cycle_mode=cycle_mode,
        has_primary=has_primary,
        primary_income_name=primary_income_name,
        cycle_info=cycle_info,
        next_cycle_start=next_cycle_start,
        truelayer_live=TRUELAYER_LIVE,
        employment_type=employment_type,
        self_employed_income=self_employed_income,
        alert_mode=alert_mode,
        alert_overall_threshold=alert_overall_threshold,
        alert_accounts=alert_accounts,
    )


@app.post("/settings/save-cycle")
@login_required
def settings_save_cycle():
    from database import get_db, USE_POSTGRES, release_db
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("settings"))
    cycle_mode = request.form.get("cycle_mode", "manual")
    if cycle_mode not in ("automatic", "manual"):
        cycle_mode = "manual"
    try:
        start_day = max(1, min(28, int(request.form.get("budget_cycle_start", 1))))
    except (ValueError, TypeError):
        start_day = 1
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "UPDATE users SET budget_cycle_start = %s, cycle_mode = %s WHERE id = %s",
            (start_day, cycle_mode, current_user.id),
        )
    else:
        cursor.execute(
            "UPDATE users SET budget_cycle_start = ?, cycle_mode = ? WHERE id = ?",
            (start_day, cycle_mode, current_user.id),
        )
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


# --- SPENDING ALERT THRESHOLD SETTINGS ---
# Saves the user's low-balance warning setup: off / overall / per-account.
# Switchable anytime, like income averaging - always reads the current
# rows/mode from scratch rather than trusting stale form state.
@app.post("/settings/save-alert-threshold")
@login_required
def settings_save_alert_threshold():
    if request.form.get("csrf_token") != session.get("csrf_token"):
        return redirect(url_for("settings"))

    mode = (request.form.get("mode") or "off").strip()
    if mode not in ("off", "overall", "per_account"):
        mode = "off"

    db = get_db()
    cursor = db.cursor()

    if mode == "off":
        if USE_POSTGRES:
            cursor.execute("UPDATE users SET alert_mode = NULL, alert_overall_threshold = NULL WHERE id = %s", (current_user.id,))
            cursor.execute("UPDATE accounts SET alert_threshold = NULL WHERE user_id = %s", (current_user.id,))
        else:
            cursor.execute("UPDATE users SET alert_mode = NULL, alert_overall_threshold = NULL WHERE id = ?", (current_user.id,))
            cursor.execute("UPDATE accounts SET alert_threshold = NULL WHERE user_id = ?", (current_user.id,))
        db.commit()
        cursor.close()
        release_db(db)
        return redirect(url_for("settings", msg="Spending alert turned off.", tab="display"))

    if mode == "overall":
        threshold, err = validate_amount(request.form.get("overall_threshold"))
        if err:
            cursor.close()
            release_db(db)
            return redirect(url_for("settings", msg=err, tab="display"))
        if USE_POSTGRES:
            cursor.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = %s WHERE id = %s", (threshold, current_user.id))
        else:
            cursor.execute("UPDATE users SET alert_mode = 'overall', alert_overall_threshold = ? WHERE id = ?", (threshold, current_user.id))
        db.commit()
        cursor.close()
        release_db(db)
        return redirect(url_for("settings", msg="Spending alert saved.", tab="display"))

    # mode == "per_account"
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = %s", (current_user.id,))
    else:
        cursor.execute("UPDATE users SET alert_mode = 'per_account' WHERE id = ?", (current_user.id,))

    accounts_rows = get_active_accounts(current_user.id)
    for acc in accounts_rows:
        # Locked accounts are frozen/read-only - a threshold on one could
        # never be usefully acted on, so they're not configurable here.
        if acc.get("is_locked"):
            continue
        raw = (request.form.get(f"threshold_{acc['id']}") or "").strip()
        value = None
        if raw:
            value, _ = validate_amount(raw)  # invalid/blank/non-positive -> cleared, not a hard error
        if USE_POSTGRES:
            cursor.execute("UPDATE accounts SET alert_threshold = %s WHERE id = %s AND user_id = %s", (value, acc["id"], current_user.id))
        else:
            cursor.execute("UPDATE accounts SET alert_threshold = ? WHERE id = ? AND user_id = ?", (value, acc["id"], current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("settings", msg="Spending alert saved.", tab="display"))


def _monthly_eq(amount, frequency):
    """Return the monthly equivalent of a recurring amount given its frequency."""
    freq = (frequency or 'monthly').lower()
    multipliers = {
        'weekly': 52.0 / 12,
        'fortnightly': 26.0 / 12,
        '4-weekly': 13.0 / 12,
        'yearly': 1.0 / 12,
    }
    return float(amount or 0) * multipliers.get(freq, 1.0)


def normalised_totals(income_rows, bill_rows):
    """Return (income_monthly, income_annual, bills_monthly, bills_annual) totals."""
    inc_m = sum(_monthly_eq(i.get('amount', 0), i.get('frequency')) for i in income_rows)
    bil_m = sum(_monthly_eq(b.get('amount', 0), b.get('frequency')) for b in bill_rows)
    return inc_m, inc_m * 12, bil_m, bil_m * 12


# --- GOALS (savings & debt repayment tracking) ---

def _goal_contributions_sum(goal_id, user_id):
    """Total logged contributions for a standalone goal."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM goal_contributions WHERE goal_id = %s AND user_id = %s", (goal_id, user_id))
    else:
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM goal_contributions WHERE goal_id = ? AND user_id = ?", (goal_id, user_id))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    val = row[0] if row is not None else 0
    return float(val or 0)


def _compute_goal_progress(goal, user_id, accounts_by_id=None):
    """Returns progress info for a single goal.

    - Linked savings goal: progress = the linked account's current balance,
      taken at face value against the target (per spec — this is
      deliberately NOT relative to a starting balance).
    - Linked debt goal: progress = how much of the balance has been paid
      down since the goal started tracking it (starting_balance vs current
      balance, both compared as magnitudes so it works whether debt is
      stored as a negative balance or a positive "amount owed" figure) —
      current balance alone doesn't say how much *this goal* has achieved.
    - Standalone goal (either type): progress = sum of logged contributions.
      No sign handling needed there since the user self-reports "amount
      achieved" either way.

    A locked linked account is not excluded — its balance is already frozen
    at the DB level by the existing account-locking design, so reading it
    naturally returns the frozen figure. account_locked is surfaced so the
    UI can flag it as stale, consistent with how locked accounts are noted
    everywhere else in the app, rather than hiding or erroring on the goal.
    """
    target_amount = float(goal["target_amount"] or 0)
    account_name = None
    account_locked = False
    linked_account_id = goal.get("linked_account_id")

    if linked_account_id:
        if accounts_by_id is None:
            accounts_by_id = {a["id"]: a for a in get_all_accounts(user_id)}
        acc = accounts_by_id.get(linked_account_id)
        if acc is None:
            # Accounts are only ever soft-deactivated in this app, never hard
            # deleted, so this shouldn't normally happen — fall back to
            # contributions rather than erroring if it ever does.
            progress_amount = _goal_contributions_sum(goal["id"], user_id)
        else:
            account_name = acc["name"]
            account_locked = bool(acc.get("is_locked"))
            current_balance = float(acc["balance"] or 0)
            if goal["goal_type"] == "debt":
                starting_raw = goal.get("starting_balance")
                starting = float(starting_raw) if starting_raw is not None else current_balance
                progress_amount = max(0.0, abs(starting) - abs(current_balance))
            else:
                progress_amount = current_balance
    else:
        progress_amount = _goal_contributions_sum(goal["id"], user_id)

    progress_amount = round(progress_amount, 2)
    raw_ratio = (progress_amount / target_amount) if target_amount > 0 else 0.0
    return {
        "progress_amount": progress_amount,
        "target_amount": target_amount,
        "progress_pct": round(min(1.0, max(0.0, raw_ratio)) * 100, 1),
        "raw_ratio": raw_ratio,
        "is_linked": bool(linked_account_id),
        "account_name": account_name,
        "account_locked": account_locked,
    }


def _suggest_goal_pace(target_amount, progress_amount, target_date_str):
    """Deterministic pace suggestion — computed fresh every time, never
    stored. Returns None when there's no target date (the feature is only
    meant to trigger once one is set, per spec)."""
    if not target_date_str:
        return None
    try:
        target_date = date.fromisoformat(str(target_date_str))
    except (ValueError, TypeError):
        return None

    today = date.today()
    remaining_amount = round(max(0.0, float(target_amount) - float(progress_amount)), 2)
    days_remaining = (target_date - today).days

    if days_remaining <= 0:
        return {
            "remaining_amount": remaining_amount,
            "days_remaining": days_remaining,
            "monthly_pace": None,
            "overdue": True,
        }

    months_remaining = max(days_remaining / 30.44, 1 / 30.44)
    monthly_pace = round(remaining_amount / months_remaining, 2)
    return {
        "remaining_amount": remaining_amount,
        "days_remaining": days_remaining,
        "monthly_pace": monthly_pace,
        "overdue": False,
    }


def _compute_goal_recent_pace(goal, user_id, accounts_by_id=None):
    """Real recent £/day pace of progress toward a goal — the inverse of
    _suggest_goal_pace(): that one asks "given a target date, what pace is
    needed"; this asks "given actual recent behaviour, what pace is really
    happening". Returns None when there isn't enough real data to
    responsibly measure a rate, rather than a falsely precise number.

    - Standalone: uses logged goal_contributions. Prefers contributions
      from the last 90 days (genuinely recent); a sparse logger with fewer
      than 2 in that window falls back to their last 5 contributions
      regardless of age, so an infrequent-but-real logger still gets a
      rate instead of nothing. Needs at least 2 total contributions ever —
      a single data point has no time span to measure a rate over.
    - Linked: reconstructs the account's balance at the start of the
      observed window from its real transaction history (the same
      balance-at-a-past-date technique the Forecast chart's historical
      scrollback used to use — but here that's exactly the point: this
      calculation genuinely needs to measure real recent account activity,
      not imply generic chart history), then measures the change using the
      same abs()-based magnitude approach as _compute_goal_progress so it
      works whether the balance is growing (savings) or shrinking (debt).
      A shrinking savings balance or a growing debt balance correctly
      yields a negative pace — a real "things are moving the wrong way"
      signal, not clamped away.

    The span used for the day-rate denominator is the actual span covered
    by the observed activity (today minus the earliest data point used),
    not a fixed 90 days — a lump sum that happened 10 days ago should read
    as a fast recent pace, not be diluted across an artificial 90-day
    window it didn't actually occur across.
    """
    linked_account_id = goal.get("linked_account_id")
    today = date.today()

    if linked_account_id:
        if accounts_by_id is None:
            accounts_by_id = {a["id"]: a for a in get_all_accounts(user_id)}
        acc = accounts_by_id.get(linked_account_id)
        if acc is None:
            return None
        current_balance = float(acc["balance"] or 0)
        account_name = acc["name"]

        db = get_db()
        cursor = db.cursor()
        cutoff = (today - timedelta(days=90)).isoformat()
        if USE_POSTGRES:
            cursor.execute(
                "SELECT amount, date FROM transactions WHERE account=%s AND user_id=%s AND date >= %s ORDER BY date ASC",
                (account_name, user_id, cutoff),
            )
        else:
            cursor.execute(
                "SELECT amount, date FROM transactions WHERE account=? AND user_id=? AND date >= ? ORDER BY date ASC",
                (account_name, user_id, cutoff),
            )
        rows = cursor.fetchall()
        cursor.close()
        release_db(db)
        if not rows:
            return None

        net_in_window = sum(float(r[0] if USE_POSTGRES else r["amount"]) for r in rows)
        earliest_date = date.fromisoformat(str(rows[0][1] if USE_POSTGRES else rows[0]["date"]))
        span_days = max(1, (today - earliest_date).days)
        balance_then = current_balance - net_in_window

        if goal.get("goal_type") == "debt":
            delta = abs(balance_then) - abs(current_balance)
        else:
            delta = current_balance - balance_then
        return delta / span_days

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT amount, date FROM goal_contributions WHERE goal_id=%s AND user_id=%s ORDER BY date DESC", (goal["id"], user_id))
    else:
        cursor.execute("SELECT amount, date FROM goal_contributions WHERE goal_id=? AND user_id=? ORDER BY date DESC", (goal["id"], user_id))
    rows = cursor.fetchall()
    cursor.close()
    release_db(db)

    contributions = [
        (float(r[0] if USE_POSTGRES else r["amount"]), str(r[1] if USE_POSTGRES else r["date"]))
        for r in rows
    ]
    if len(contributions) < 2:
        return None

    cutoff = (today - timedelta(days=90)).isoformat()
    window = [c for c in contributions if c[1] >= cutoff]
    if len(window) < 2:
        window = contributions[:5]

    earliest_date = min(date.fromisoformat(c[1]) for c in window)
    span_days = max(1, (today - earliest_date).days)
    total = sum(c[0] for c in window)
    return total / span_days


def _project_goal_completion(progress, pace_per_day, target_date_str=None, is_estimate=False):
    """Projects a completion date from real recent pace — independent of
    whether a target date exists, since that's often the most useful thing
    to know about a goal with no fixed deadline. If a target date IS set,
    compares the projection against it: on/before target is "on track"
    (green); up to 30 days after is "amber" (slipping but close); more than
    30 days after — or no realistic path to completion at all — is "red".
    Recalculated fresh from current data every call, never stored.

    A genuinely tiny recent pace (e.g. one small contribution logged months
    apart) produces a technically-correct but absurd result if extrapolated
    literally — a specific calendar date decades or centuries out. Past a
    10-year horizon this switches to a "years_away" state (an honest rough
    figure, e.g. "12+ years") instead of a fabricated-looking precise date —
    still real information ("this is going nowhere at the current rate"),
    just not presented with false precision.

    is_estimate is a pass-through flag, not something this function decides —
    the caller sets it when pace_per_day came from the Safe-to-Spend fallback
    (see _compute_goal_pace_map) rather than real contribution/balance
    history, so the UI can label an estimate as an estimate. It never
    changes the maths here, only what gets attached to the result for
    display.
    """
    remaining = progress["target_amount"] - progress["progress_amount"]
    today = date.today()
    FAR_FUTURE_DAYS = 3650  # 10 years

    years_away = None
    if pace_per_day is None:
        state = "insufficient_data"
        projected_date = None
    elif remaining <= 0:
        state = "reached"
        projected_date = today.isoformat()
    elif pace_per_day <= 0:
        state = "no_progress"
        projected_date = None
    else:
        days_needed = remaining / pace_per_day
        if days_needed > FAR_FUTURE_DAYS:
            state = "years_away"
            projected_date = None
            years_away = int(days_needed // 365)
        else:
            projected_date = (today + timedelta(days=round(days_needed))).isoformat()
            state = "projected"

    on_track = None
    status_color = None
    days_over_target = None
    if target_date_str:
        try:
            target_date_obj = date.fromisoformat(str(target_date_str))
        except (ValueError, TypeError):
            target_date_obj = None
        if target_date_obj is not None:
            if state == "reached":
                on_track, status_color = True, "green"
            elif state in ("no_progress", "years_away"):
                on_track, status_color = False, "red"
            elif state == "projected":
                days_over = (date.fromisoformat(projected_date) - target_date_obj).days
                days_over_target = days_over
                if days_over <= 0:
                    on_track, status_color = True, "green"
                elif days_over <= 30:
                    on_track, status_color = False, "amber"
                else:
                    on_track, status_color = False, "red"
            # state == "insufficient_data" -> on_track stays None, can't judge yet

    return {
        "state": state,
        "projected_date": projected_date,
        "years_away": years_away,
        "pace_per_day": round(pace_per_day, 2) if pace_per_day is not None else None,
        "on_track": on_track,
        "status_color": status_color,
        "is_estimate": is_estimate,
        # Already computed above to decide on_track/status_color for the
        # "projected" state - exposed as its own field (rather than left
        # discarded) purely so the UI can show "N months behind target"
        # without re-deriving the same date subtraction itself. Only ever
        # set (and only positive) for state == "projected"; None otherwise.
        "days_over_target": days_over_target if (days_over_target is not None and days_over_target > 0) else None,
    }


# --- Goal Contribution Engine (August 2026) ---
# A goal's recurring contribution slider commitment is stored as an
# ordinary savings_rules row with goal_id set (see database.py's migration
# comment for the full schema reasoning) - one engine feeding Safe to
# Spend/forecast, not a parallel system. Deliberately projection-only, like
# every other savings_rules row: setting a commitment reduces Safe to
# Spend/the forecast from the next cycle onward, but never fabricates a
# real balance change or goal_contributions row - the user still makes the
# real transfer/logs the real contribution themselves.

_GOAL_COMMITMENT_SNAP = 5.0  # slider snaps to the nearest £5
# Default slider max is capped well below 100% of Safe to Spend so the
# starting range itself discourages over-committing - a user who genuinely
# wants to commit more can still type a larger figure into the paired
# number field.
_GOAL_COMMITMENT_DEFAULT_MAX_PCT = 0.5


def _snap_to_increment(value, increment=_GOAL_COMMITMENT_SNAP, mode="nearest"):
    """Snaps a raw £ amount to the nearest (or, for a floor/ceiling bound,
    the safe rounding direction's) multiple of `increment`. mode='up' never
    understates a floor; mode='down' never overstates a cap; 'nearest' is
    for values with no direction requirement (e.g. a default position)."""
    if value is None:
        return None
    value = max(0.0, float(value))
    if increment <= 0:
        return round(value, 2)
    units = value / increment
    if mode == "up":
        units = math.ceil(units - 1e-9)
    elif mode == "down":
        units = math.floor(units + 1e-9)
    else:
        units = round(units)
    return round(units * increment, 2)


def _get_goal_commitment(goal_id, user_id):
    """Returns the existing savings_rules row linked to this goal (via
    goal_id), or None if the goal has no standing recurring commitment
    set up yet."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM savings_rules WHERE goal_id = %s AND user_id = %s", (goal_id, user_id))
    else:
        cursor.execute("SELECT * FROM savings_rules WHERE goal_id = ? AND user_id = ?", (goal_id, user_id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    return dict(zip(cols, row)) if row else None


def _compute_goal_commitment_bounds(goal, progress, pace, safe_to_spend, fallback_pace_per_day=None):
    """Returns the slider's {floor, default, max} in £/cycle, all snapped
    to _GOAL_COMMITMENT_SNAP.

    - Debt goal WITH a known minimum_payment: floor = that minimum (can
      never be dragged below a real required payment); default = the
      target-date-derived suggested pace if it's at least the minimum,
      else the minimum itself - so the default is never a bare 0 when a
      real minimum is known, per spec.
    - Debt goal with no known minimum, or a savings goal: floor = 0 (no
      hard requirement to protect against); default = the suggested pace
      if one exists, else 0 - savings goals are explicitly allowed to
      default lower than debt goals with a real minimum.
    - max: the larger of a modest £50 baseline and
      _GOAL_COMMITMENT_DEFAULT_MAX_PCT of current Safe to Spend, so a user
      with very little Safe to Spend still gets a usable range rather than
      a near-zero max. The default is clamped to sit within [floor, max].

    fallback_pace_per_day is the goal's already-computed real/estimated
    recent £/day pace (g["projection"]["pace_per_day"], itself either real
    tracked velocity or the Safe-to-Spend-derived fallback estimate — see
    _compute_goal_pace_map). _suggest_goal_pace() only returns a figure
    when a target date is set; without one, the slider would otherwise
    default to a bare 0 even though a perfectly good pace figure already
    exists and used to be shown as "around £X/month" — so this is
    consulted second, before finally falling back to 0.
    """
    minimum_payment = goal.get("minimum_payment")
    minimum_payment = float(minimum_payment) if minimum_payment not in (None, "") else None
    suggested = pace.get("monthly_pace") if pace and not pace.get("overdue") else None
    if suggested is None and fallback_pace_per_day is not None and fallback_pace_per_day > 0:
        suggested = fallback_pace_per_day * 30.44

    if goal.get("goal_type") == "debt" and minimum_payment is not None and minimum_payment > 0:
        floor = minimum_payment
        default = suggested if (suggested is not None and suggested >= minimum_payment) else minimum_payment
    else:
        floor = 0.0
        default = suggested if suggested is not None else 0.0

    safe_to_spend = max(0.0, float(safe_to_spend or 0.0))
    max_value = max(50.0, safe_to_spend * _GOAL_COMMITMENT_DEFAULT_MAX_PCT)
    # A floor from a real minimum payment always wins even if it exceeds
    # the "sensible default" max - never hide a real requirement.
    max_value = max(max_value, floor)

    floor_snapped = _snap_to_increment(floor, mode="up")
    max_snapped = _snap_to_increment(max_value, mode="down")
    if max_snapped < floor_snapped:
        max_snapped = floor_snapped
    default_snapped = _snap_to_increment(default, mode="nearest")
    default_snapped = min(max(default_snapped, floor_snapped), max_snapped)

    return {
        "floor": floor_snapped,
        "default": default_snapped,
        "max": max_snapped,
        "has_minimum_payment": minimum_payment is not None and minimum_payment > 0,
    }


def _compute_goal_commitment_preview(progress, target_date_str, amount, safe_to_spend):
    """Shared by /api/goal-commitment-preview (live slider dragging) and
    manage()'s initial server-render (so the card shows real numbers
    before any JS has run, rather than a blank "Calculating…") - one
    calculation, not two copies that could drift apart. Feeds the
    candidate amount into _project_goal_completion() as a hypothetical
    pace, exactly like the real/estimated pace everywhere else."""
    amount = max(0.0, float(amount or 0))
    pace_per_day = amount / 30.44
    projection = _project_goal_completion(progress, pace_per_day, target_date_str, is_estimate=False)
    resulting_safe_to_spend = round(float(safe_to_spend or 0.0) - amount, 2)
    return {
        "amount": round(amount, 2),
        "resulting_safe_to_spend": resulting_safe_to_spend,
        "would_go_negative": resulting_safe_to_spend < 0,
        "projection": projection,
    }


def _compute_commitment_note(goal, accounts_by_id):
    """Plain-language explanation of what a recurring commitment actually
    does for THIS goal, mirroring settings_set_goal_commitment()'s real
    to_account resolution exactly (see database.py's goal_id migration for
    the full reasoning) rather than describing a simplified version of it.

    The commitment always reduces Safe to Spend/forecast from from_account
    - that part is unconditional. Whether it ALSO auto-credits somewhere
    that visibly grows this goal's own progress depends entirely on
    whether the linked account is itself a real, unlocked SAVINGS-type
    account on a SAVINGS goal - the one case the route's to_account logic
    actually wires up. Every other case (debt goal, standalone goal, or a
    savings goal linked to a non-savings/locked account, as when someone
    tracks a savings goal against an everyday current account) is a
    one-sided deduction with no automatic link to this goal's own progress
    figure at all - the user has to move the money into the tracked
    account themselves, same "no fabricated data" principle as the rest of
    this projection-only feature."""
    linked_account_id = goal.get("linked_account_id")
    is_savings_goal = goal.get("goal_type") == "savings"

    if not linked_account_id:
        return {
            "tone": "neutral",
            "text": "Standalone goal — log what you set aside as a contribution below for it to count toward progress.",
        }

    acc = accounts_by_id.get(linked_account_id)
    acc_name = acc["name"] if acc else "the linked account"

    if is_savings_goal and acc and acc.get("type") == "savings" and not acc.get("is_locked"):
        return {
            "tone": "positive",
            "text": f"Feeds into {acc_name} — it'll show growing in your forecast.",
        }

    if is_savings_goal:
        return {
            "tone": "neutral",
            "text": f"Reduces Safe to Spend, but won't automatically count as progress here — pay it into {acc_name} yourself for progress to update.",
        }

    return {
        "tone": "neutral",
        "text": f"Reduces Safe to Spend. Pay it toward {acc_name} yourself — its balance is what tracks how much you've paid off.",
    }


# Keyword -> emoji, checked in order against the goal's (lower-cased) name.
# First match wins. Deliberately plain emoji rather than an icon font/SVG
# library — the app already uses emoji as its icon language everywhere
# (🎯 for the Goals section itself, 💰/💸/🏦 for Income/Bills/Accounts, the
# nav's own icons, etc.), so this reuses that existing visual convention
# instead of introducing a brand-new dependency for one card.
_GOAL_ICON_RULES = [
    (("trip", "holiday", "vacation", "travel"), "✈️"),
    (("house", "home", "deposit", "property", "flat", "mortgage"), "🏠"),
    (("car", "vehicle", "auto"), "🚗"),
    (("emergency", "rainy day", "safety net", "buffer"), "🛡️"),
    (("wedding", "marriage"), "💍"),
    (("education", "university", "college", "course", "tuition", "school", "degree"), "🎓"),
    (("laptop", "computer", "phone", "gadget", "tech"), "💻"),
    (("gift", "present"), "🎁"),
    (("baby", "nursery", "child"), "🍼"),
    (("gym", "fitness", "health"), "💪"),
]


def _pick_goal_icon(name, goal_type):
    """Returns an emoji for the goal's icon tile - a keyword match against
    the goal's name where one reasonably applies, falling back to a generic
    savings (piggy bank) or debt (trending down) icon otherwise."""
    lowered = (name or "").lower()
    for keywords, emoji in _GOAL_ICON_RULES:
        if any(k in lowered for k in keywords):
            return emoji
    return "🐷" if goal_type != "debt" else "📉"


def _build_goal_display(g):
    """Presentation-only derivation from a goal's already-computed progress/
    pace/projection - resolves nothing new about progress or pace itself,
    just display-ready labels, colours, and the "extra £/month needed to
    close the gap" figure for the redesigned Goals card. That figure is
    simply g.pace.monthly_pace (the already-computed REQUIRED pace to hit
    the target) minus the real/estimated current pace already on
    g.projection - no new calculation logic, just arithmetic on two
    existing numbers, kept here rather than in the template so it's
    unit-testable and the template only has to print already-resolved
    values."""
    progress = g["progress"]
    proj = g.get("projection")
    pace = g.get("pace")
    is_debt = g.get("goal_type") == "debt"

    icon_emoji = _pick_goal_icon(g.get("name"), g.get("goal_type"))
    if is_debt:
        icon_bg, icon_fg = "#fee2e2", "#991b1b"
        type_bg, type_fg, type_label = "#fee2e2", "#991b1b", "Debt repayment"
    else:
        icon_bg, icon_fg = "#dcfce7", "#166534"
        type_bg, type_fg, type_label = "#dcfce7", "#166534", "Savings"

    if g.get("status") == "completed":
        bar_color = "#198754"
    elif proj and proj.get("status_color") == "green":
        bar_color = "#198754"
    elif proj and proj.get("status_color") == "red":
        bar_color = "#dc3545"
    elif proj and proj.get("status_color") == "amber":
        bar_color = "#f59e0b"
    else:
        bar_color = "var(--brand)"

    pace_label = "Estimated" if (proj and proj.get("is_estimate")) else "At current pace"

    months_behind = None
    extra_monthly_needed = None
    if proj and g.get("target_date") and proj.get("state") == "projected":
        days_over = proj.get("days_over_target")
        if days_over:
            months_behind = round(days_over / 30.44, 1)
            if pace and proj.get("pace_per_day") is not None:
                current_monthly = proj["pace_per_day"] * 30.44
                gap = pace["monthly_pace"] - current_monthly
                if gap > 0:
                    extra_monthly_needed = round(gap, 2)

    return {
        "icon_emoji": icon_emoji,
        "icon_bg": icon_bg,
        "icon_fg": icon_fg,
        "type_bg": type_bg,
        "type_fg": type_fg,
        "type_label": type_label,
        "bar_color": bar_color,
        "pace_label": pace_label,
        "months_behind": months_behind,
        "extra_monthly_needed": extra_monthly_needed,
    }


def _get_safe_to_spend(user_id):
    """Reuses the same Safe to Spend figure shown on Home, so a goal's
    suggested pace can be gently flagged if it looks unrealistic against
    the user's actual finances."""
    try:
        accounts_rows = get_active_accounts(user_id)
        accounts = {}
        for r in accounts_rows:
            accounts[r["name"]] = {
                "balance": r["balance"],
                "type": r["type"],
                "active": bool(r["active"]),
                "include_in_overview": bool(r.get("include_in_overview", 1)),
                "savings_type": r.get("savings_type"),
                "is_locked": bool(r.get("is_locked")),
            }
        import cycle_engine as _ce
        cyc = _ce.get_cycle(user_id)
        overview = calculate_financial_overview(accounts, period_end=cyc["display_end"], safe_boundary=cyc["safe_boundary"])
        return float(overview.get("safe_spending", 0.0))
    except Exception as e:
        logger.debug(f"_get_safe_to_spend error: {e}")
        return None


def _safe_to_spend_daily_rate(user_id):
    """Converts the cycle-scoped Safe to Spend figure into a £/day rate —
    used only as an early pace ESTIMATE (see _compute_goal_pace_map) for a
    goal with not enough real contribution/balance history yet to calculate
    a genuine pace from. Returns None if Safe to Spend can't be computed;
    the caller is responsible for treating a non-positive result as "no
    realistic estimate" rather than suggesting a pace that doesn't exist.

    The denominator is deliberately the cycle's FULL length (display_start
    to safe_boundary), not "days remaining from today". Safe to Spend is a
    live, point-in-time figure — current balance + income still to arrive
    minus bills still to come, through to the next payday — so it naturally
    shrinks as bills/spending happen and grows again right after payday.
    Dividing that live snapshot by the shrinking days-left-in-cycle instead
    of a stable cycle length means the exact same underlying finances can
    imply a wildly different "monthly pace" purely depending on which day
    of the cycle someone happens to check (e.g. 9 days left in a 31-day
    cycle already inflates the rate ~3.4x; 1-2 days left inflates it by an
    order of magnitude) — not a reflection of anything that actually
    changed. Prorating over the full cycle keeps the estimate stable
    regardless of when it's viewed."""
    try:
        import cycle_engine as _ce
        cyc = _ce.get_cycle(user_id)
        safe_boundary = cyc.get("safe_boundary")
        display_start = cyc.get("display_start")
        safe_spending = _get_safe_to_spend(user_id)
        if safe_spending is None or safe_boundary is None or display_start is None:
            return None
        cycle_len = max(1, (safe_boundary - display_start).days + 1)
        return safe_spending / cycle_len
    except Exception as e:
        logger.debug(f"_safe_to_spend_daily_rate error: {e}")
        return None


def _recurring_income_bills_daily_rate(user_id):
    """Converts the user's REAL recurring monthly income minus real
    recurring monthly bills — via normalised_totals(), the same helper
    manage.html's Income/Bills cards already use for their £/month · £/year
    totals — into a £/day rate. Used as a hard ceiling on the
    Safe-to-Spend-derived fallback estimate (see _compute_goal_pace_map):
    that estimate must never claim a "typical monthly pace" bigger than
    what the user actually, recurringly, brings in minus what they
    actually, recurringly, owe.

    Unlike Safe to Spend (a live point-in-time snapshot that includes the
    current account balance and drops bills once their due-date has passed
    within the cycle — both correct for its own live-spending-buffer
    purpose, see _safe_to_spend_daily_rate), this is a genuine stable
    monthly flow: no balance, no cycle-position sensitivity, no swings
    based on which bills happen to have already fallen due this month.

    Returns None if it can't be computed; the caller is responsible for
    treating a non-positive result as "no realistic estimate", same as
    _safe_to_spend_daily_rate."""
    try:
        db = get_db()
        cursor = db.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT * FROM income WHERE user_id = %s", (user_id,))
        else:
            cursor.execute("SELECT * FROM income WHERE user_id = ?", (user_id,))
        income_cols = [d[0] for d in cursor.description]
        income_rows = [dict(zip(income_cols, row)) for row in cursor.fetchall()]

        if USE_POSTGRES:
            cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = %s", (user_id,))
        else:
            cursor.execute("SELECT * FROM scheduled_expenses WHERE user_id = ?", (user_id,))
        bill_cols = [d[0] for d in cursor.description]
        bill_rows = [dict(zip(bill_cols, row)) for row in cursor.fetchall()]
        cursor.close()
        release_db(db)

        income_rows = _resolve_income_rows(income_rows, user_id)
        income_monthly, _, bills_monthly, _ = normalised_totals(income_rows, bill_rows)
        return (income_monthly - bills_monthly) / 30.44
    except Exception as e:
        logger.debug(f"_recurring_income_bills_daily_rate error: {e}")
        return None


# A fallback pace estimate implying the WHOLE remaining goal would be
# finished in under this many days isn't a meaningful "typical monthly
# pace" - there's no real tracked history backing a claim that fast, only
# a live Safe-to-Spend snapshot. Matches the 30.44-day/month constant used
# everywhere else in this feature for £/day <-> £/month conversions.
_FALLBACK_MIN_DAYS_TO_COMPLETE = 30.44


def _cap_fallback_rate_for_goal(goal, fallback_rate, user_id, accounts_by_id):
    """Caps a goal's share of the Safe-to-Spend fallback rate so it never
    implies completing the goal's entire remaining amount in under
    _FALLBACK_MIN_DAYS_TO_COMPLETE days - regardless of what's driving the
    uncapped figure (a genuinely large balance, a modest goal splitting a
    real surplus among few goals, or anything else). Only ever pulls the
    rate DOWN; a fallback_rate that's already realistic for this goal is
    returned unchanged."""
    try:
        progress = _compute_goal_progress(goal, user_id, accounts_by_id=accounts_by_id)
        remaining = progress["target_amount"] - progress["progress_amount"]
    except Exception as e:
        logger.debug(f"_cap_fallback_rate_for_goal error: {e}")
        return fallback_rate
    if remaining <= 0:
        return fallback_rate
    max_plausible_rate = remaining / _FALLBACK_MIN_DAYS_TO_COMPLETE
    return min(fallback_rate, max_plausible_rate)


def _compute_goal_pace_map(goals, user_id, accounts_by_id=None):
    """For a batch of a user's goals, decides which pace each one should
    project from: real recent pace where there's enough real data, or —
    only for an *active* goal that genuinely has none yet — a share of the
    user's Safe-to-Spend-derived daily rate as an early estimate.

    This has to run as a batch (not goal-by-goal) because the fallback
    estimate is split evenly across however many active goals currently
    need it, so no single goal's estimate implies the user's entire typical
    leftover is available to it alone (see point 4 of the brief). A
    completed goal never participates — it doesn't need an estimate, and
    including it would just shrink everyone else's share for no reason.
    A non-positive Safe to Spend produces no fallback at all — never
    suggest a positive pace that doesn't exist.

    The shared pool itself is first hard-capped at the user's REAL
    recurring monthly income minus real recurring monthly bills (see
    _recurring_income_bills_daily_rate) — Safe to Spend is a live,
    balance-inclusive, cycle-position-sensitive snapshot (see
    _safe_to_spend_daily_rate's own docstring), so on its own it can imply
    a "typical monthly pace" several times larger than what the user
    actually, recurringly, has left over once income and bills are
    genuinely accounted for. This ceiling is computed once for the whole
    batch (a user-level fact, not a per-goal one) and split evenly across
    the same active_needing_fallback denominator as the Safe-to-Spend pool,
    for the same "no single goal implies the whole surplus" reasoning. If
    the real recurring surplus is zero or negative, there's no realistic
    estimate at all — this suppresses the fallback entirely (None) rather
    than showing a capped-to-zero or otherwise fabricated figure.

    Each goal's share is additionally capped so it never implies finishing
    the ENTIRE remaining goal in under about a month (see
    _cap_fallback_rate_for_goal below) — a live Safe-to-Spend-derived
    estimate can be technically consistent with the numbers on screen (a
    genuinely large balance, or simply few goals splitting a real surplus)
    while still being a misleading thing to present as a "typical monthly
    pace": there's no real tracked history yet backing a claim that fast.
    This only ever pulls the number DOWN toward something plausible for
    this specific goal — it never invents a faster pace than what the raw
    fallback share would have given.

    Returns (pace_map, fallback_goal_count) where pace_map is
    {goal_id: (pace_per_day_or_None, is_estimate_bool)}.
    """
    if accounts_by_id is None:
        accounts_by_id = {a["id"]: a for a in get_all_accounts(user_id)}

    real_paces = {g["id"]: _compute_goal_recent_pace(g, user_id, accounts_by_id=accounts_by_id) for g in goals}
    active_needing_fallback = [
        g["id"] for g in goals
        if g.get("status", "active") == "active" and real_paces[g["id"]] is None
    ]

    fallback_rate = None
    if active_needing_fallback:
        n = len(active_needing_fallback)
        safe_daily = _safe_to_spend_daily_rate(user_id)
        ceiling_daily = _recurring_income_bills_daily_rate(user_id)
        if safe_daily is not None and safe_daily > 0 and ceiling_daily is not None and ceiling_daily > 0:
            fallback_rate = min(safe_daily / n, ceiling_daily / n)

    pace_map = {}
    for g in goals:
        real_pace = real_paces[g["id"]]
        if real_pace is not None:
            pace_map[g["id"]] = (real_pace, False)
        elif g.get("status", "active") == "active" and fallback_rate is not None:
            capped_rate = _cap_fallback_rate_for_goal(g, fallback_rate, user_id, accounts_by_id)
            pace_map[g["id"]] = (capped_rate, True)
        else:
            pace_map[g["id"]] = (None, False)

    return pace_map, len(active_needing_fallback)


def _mark_goal_completed_if_reached(goal_id, user_id):
    """Auto-completion: called opportunistically whenever a goal's progress
    is computed. If it's just reached 100% and is still 'active', flips it
    to 'completed' — the automatic counterpart to the manual mark-complete
    action, per spec."""
    db = get_db()
    cursor = db.cursor()
    now_str = datetime.utcnow().isoformat()
    if USE_POSTGRES:
        cursor.execute("UPDATE goals SET status='completed', completed_at=%s WHERE id=%s AND user_id=%s AND status='active'", (now_str, goal_id, user_id))
    else:
        cursor.execute("UPDATE goals SET status='completed', completed_at=? WHERE id=? AND user_id=? AND status='active'", (now_str, goal_id, user_id))
    db.commit()
    cursor.close()
    release_db(db)
    return now_str


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
    goals = fetch_filtered("SELECT * FROM goals WHERE user_id = %s ORDER BY status, created_at DESC" if USE_POSTGRES else "SELECT * FROM goals WHERE user_id = ? ORDER BY status, created_at DESC", (uid,))
    goal_contributions_rows = fetch_filtered("SELECT * FROM goal_contributions WHERE user_id = %s ORDER BY date DESC" if USE_POSTGRES else "SELECT * FROM goal_contributions WHERE user_id = ? ORDER BY date DESC", (uid,))

    cursor.close()
    release_db(db)

    is_pro = user_is_pro()

    # Resolve self-employed averaged rows to their live amount (manual figure or
    # rolling transaction average) — the stored income.amount column goes stale
    # the moment a user switches averaging mode or window in Settings.
    income = _resolve_income_rows(income, uid)

    # Pre-compute human-readable rule descriptions for income rows
    for inc in income:
        inc["description"] = income_engine.describe_rule(inc)

    # Auto-star the sole income source so the cycle engine always has a primary
    if len(income) == 1 and not income[0].get("is_primary"):
        try:
            _db2 = get_db(); _cur2 = _db2.cursor()
            if USE_POSTGRES:
                _cur2.execute("UPDATE income SET is_primary = 1 WHERE id = %s AND user_id = %s", (income[0]["id"], uid))
            else:
                _cur2.execute("UPDATE income SET is_primary = 1 WHERE id = ? AND user_id = ?", (income[0]["id"], uid))
            _db2.commit(); _cur2.close(); release_db(_db2)
            income[0]["is_primary"] = 1
        except Exception:
            pass

    has_primary = any(i.get("is_primary") for i in income)

    income_monthly_total, income_annual_total, bills_monthly_total, bills_annual_total = \
        normalised_totals(income, bills)

    # Goals: compute progress (and, opportunistically, auto-completion) for
    # each, attach a pace suggestion where a target date is set, and group
    # logged contributions by goal for standalone goals' mini-lists.
    # NUMERIC columns come back from psycopg2 as Decimal, not float — cast
    # explicitly (same defensive pattern used for savings_rate elsewhere)
    # so these values are safe wherever the template needs to serialise them.
    for g in goals:
        g["target_amount"] = float(g["target_amount"] or 0)
        if g.get("starting_balance") is not None:
            g["starting_balance"] = float(g["starting_balance"])
    for c in goal_contributions_rows:
        c["amount"] = float(c["amount"] or 0)

    accounts_by_id = {a["id"]: a for a in get_all_accounts(uid)}
    contributions_by_goal = {}
    for c in goal_contributions_rows:
        contributions_by_goal.setdefault(c["goal_id"], []).append(c)

    for g in goals:
        progress = _compute_goal_progress(g, uid, accounts_by_id=accounts_by_id)
        if g["status"] == "active" and progress["raw_ratio"] >= 1.0:
            g["completed_at"] = _mark_goal_completed_if_reached(g["id"], uid)
            g["status"] = "completed"
        g["progress"] = progress
        g["pace"] = _suggest_goal_pace(progress["target_amount"], progress["progress_amount"], g.get("target_date"))
        g["contributions"] = contributions_by_goal.get(g["id"], [])

    # Batched (not per-goal in isolation) so the Safe-to-Spend fallback
    # estimate — used only for a goal without enough real data yet — can be
    # split across however many active goals actually need it right now,
    # rather than each one implying the full amount is available to it alone.
    pace_map, fallback_goal_count = _compute_goal_pace_map(goals, uid, accounts_by_id=accounts_by_id)
    for g in goals:
        pace_per_day, is_estimate = pace_map[g["id"]]
        g["projection"] = _project_goal_completion(g["progress"], pace_per_day, g.get("target_date"), is_estimate=is_estimate)
        if is_estimate:
            g["projection"]["fallback_goal_count"] = fallback_goal_count
        g["display"] = _build_goal_display(g)

    safe_to_spend_for_goals = _get_safe_to_spend(uid) if goals else None

    # Goal Contribution Engine: each active goal's existing standing
    # commitment (if any) plus the slider's floor/default/max, computed
    # from the same progress/pace/Safe-to-Spend values already resolved
    # above — no separate recalculation. Only for active goals; a
    # completed goal has nothing to commit toward.
    for g in goals:
        if g["status"] != "active":
            g["commitment"] = None
            g["commitment_bounds"] = None
            continue
        g["commitment"] = _get_goal_commitment(g["id"], uid)
        g["commitment_note"] = _compute_commitment_note(g, accounts_by_id)
        g["commitment_bounds"] = _compute_goal_commitment_bounds(
            g, g["progress"], g["pace"], safe_to_spend_for_goals or 0.0,
            fallback_pace_per_day=g["projection"].get("pace_per_day") if g.get("projection") else None,
        )
        # Initial slider position: the existing commitment's amount if one
        # is already set, else the bounds' own default - and the preview
        # shown before any JS has run reflects THAT starting amount, via
        # the exact same helper the live drag-preview route uses.
        _initial_amount = g["commitment"]["amount"] if g["commitment"] else g["commitment_bounds"]["default"]
        g["commitment_preview"] = _compute_goal_commitment_preview(
            g["progress"], g.get("target_date"), _initial_amount, safe_to_spend_for_goals or 0.0
        )

    return render_template("manage.html",
        accounts=accounts,
        bills=bills,
        savings_rules=savings_rules,
        future_events=future_events,
        income=income,
        investments=investments,
        goals=goals,
        safe_to_spend_for_goals=safe_to_spend_for_goals,
        is_pro=is_pro,
        has_primary=has_primary,
        message=request.args.get("msg", ""),
        my_money_setup=get_my_money_setup(current_user.id),
        show_my_money_dot=get_my_money_dot(current_user.id),
        income_monthly_total=income_monthly_total,
        income_annual_total=income_annual_total,
        bills_monthly_total=bills_monthly_total,
        bills_annual_total=bills_annual_total,
    )


@app.post("/settings/add-goal")
@login_required
def settings_add_goal():
    name = (request.form.get("name") or "").strip()
    goal_type = (request.form.get("goal_type") or "savings").strip()
    if goal_type not in ("savings", "debt"):
        goal_type = "savings"
    target_amount_raw = (request.form.get("target_amount") or "").strip()
    target_date = (request.form.get("target_date") or "").strip() or None
    linked_account_id_raw = (request.form.get("linked_account_id") or "").strip()

    if not name:
        return redirect(url_for("manage", tab="goals", msg="Goal name is required."))
    try:
        target_amount = float(target_amount_raw)
        if target_amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("manage", tab="goals", msg="Enter a valid target amount."))

    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            return redirect(url_for("manage", tab="goals", msg="Invalid target date."))

    linked_account_id = None
    starting_balance = None
    if linked_account_id_raw:
        try:
            linked_account_id = int(linked_account_id_raw)
        except ValueError:
            linked_account_id = None
        if linked_account_id is not None:
            acc = next((a for a in get_all_accounts(current_user.id) if a["id"] == linked_account_id), None)
            if acc is None:
                return redirect(url_for("manage", tab="goals", msg="Account not found."))
            starting_balance = float(acc["balance"] or 0)

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (current_user.id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance),
        )
    else:
        cursor.execute(
            "INSERT INTO goals (user_id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance) "
            "VALUES (?,?,?,?,?,?,?)",
            (current_user.id, name, goal_type, target_amount, target_date, linked_account_id, starting_balance),
        )
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", tab="goals", msg=f"Goal '{name}' created."))


@app.post("/settings/edit-goal")
@login_required
def settings_edit_goal():
    goal_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    goal_type = (request.form.get("goal_type") or "savings").strip()
    if goal_type not in ("savings", "debt"):
        goal_type = "savings"
    target_amount_raw = (request.form.get("target_amount") or "").strip()
    target_date = (request.form.get("target_date") or "").strip() or None
    linked_account_id_raw = (request.form.get("linked_account_id") or "").strip()

    if not name or not goal_id:
        return redirect(url_for("manage", tab="goals", msg="Missing fields."))
    try:
        target_amount = float(target_amount_raw)
        if target_amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("manage", tab="goals", msg="Enter a valid target amount."))
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            return redirect(url_for("manage", tab="goals", msg="Invalid target date."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT linked_account_id, starting_balance FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("SELECT linked_account_id, starting_balance FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="Goal not found."))
    existing_linked_id = row[0] if USE_POSTGRES else row["linked_account_id"]

    new_linked_id = None
    if linked_account_id_raw:
        try:
            new_linked_id = int(linked_account_id_raw)
        except ValueError:
            new_linked_id = None

    # Re-linking to a different account (or unlinking entirely) resets the
    # starting point — progress-so-far no longer means anything against a
    # different (or no) account.
    if new_linked_id != existing_linked_id:
        if new_linked_id is not None:
            acc = next((a for a in get_all_accounts(current_user.id) if a["id"] == new_linked_id), None)
            if acc is None:
                cursor.close()
                release_db(db)
                return redirect(url_for("manage", tab="goals", msg="Account not found."))
            starting_balance = float(acc["balance"] or 0)
        else:
            starting_balance = None
    else:
        existing_starting = row[1] if USE_POSTGRES else row["starting_balance"]
        starting_balance = float(existing_starting) if existing_starting is not None else None

    if USE_POSTGRES:
        cursor.execute(
            "UPDATE goals SET name=%s, goal_type=%s, target_amount=%s, target_date=%s, "
            "linked_account_id=%s, starting_balance=%s WHERE id=%s AND user_id=%s",
            (name, goal_type, target_amount, target_date, new_linked_id, starting_balance, goal_id, current_user.id),
        )
    else:
        cursor.execute(
            "UPDATE goals SET name=?, goal_type=?, target_amount=?, target_date=?, "
            "linked_account_id=?, starting_balance=? WHERE id=? AND user_id=?",
            (name, goal_type, target_amount, target_date, new_linked_id, starting_balance, goal_id, current_user.id),
        )
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", tab="goals", msg="Goal updated."))


@app.post("/settings/delete-goal")
@login_required
def settings_delete_goal():
    """Removes goal tracking only — never touches the linked account or its
    transactions, which is exactly why goals are their own table rather than
    a column bolted onto accounts."""
    goal_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM goal_contributions WHERE goal_id=%s AND user_id=%s", (goal_id, current_user.id))
        cursor.execute("DELETE FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("DELETE FROM goal_contributions WHERE goal_id=? AND user_id=?", (goal_id, current_user.id))
        cursor.execute("DELETE FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", tab="goals", msg="Goal deleted."))


@app.post("/settings/complete-goal")
@login_required
def settings_complete_goal():
    """Toggles a goal between 'active' and 'completed' — lets a user mark a
    goal achieved early (separate from the automatic completion that fires
    when progress reaches 100%, see _mark_goal_completed_if_reached), and
    just as easily undo that if it was marked by mistake."""
    goal_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT status FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("SELECT status FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="Goal not found."))
    current_status = row[0] if USE_POSTGRES else row["status"]
    new_status = "active" if current_status == "completed" else "completed"
    completed_at = datetime.utcnow().isoformat() if new_status == "completed" else None
    if USE_POSTGRES:
        cursor.execute("UPDATE goals SET status=%s, completed_at=%s WHERE id=%s AND user_id=%s", (new_status, completed_at, goal_id, current_user.id))
    else:
        cursor.execute("UPDATE goals SET status=?, completed_at=? WHERE id=? AND user_id=?", (new_status, completed_at, goal_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    msg = "Goal marked as achieved! \U0001f389" if new_status == "completed" else "Goal reopened."
    return redirect(url_for("manage", tab="goals", msg=msg))


@app.post("/settings/add-goal-contribution")
@login_required
def settings_add_goal_contribution():
    """Manual contribution entries only apply to standalone (non-linked)
    goals — a linked goal's progress already comes straight from the real
    account balance, so a logged contribution there would double-count."""
    goal_id = request.form.get("goal_id")
    amount_raw = (request.form.get("amount") or "").strip()
    date_raw = (request.form.get("date") or "").strip() or date.today().isoformat()
    note = (request.form.get("note") or "").strip() or None
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("manage", tab="goals", msg="Enter a valid contribution amount."))
    try:
        date.fromisoformat(date_raw)
    except ValueError:
        return redirect(url_for("manage", tab="goals", msg="Invalid date."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT linked_account_id FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("SELECT linked_account_id FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="Goal not found."))
    linked = row[0] if USE_POSTGRES else row["linked_account_id"]
    if linked is not None:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="This goal tracks progress automatically from its linked account."))

    if USE_POSTGRES:
        cursor.execute("INSERT INTO goal_contributions (goal_id, user_id, amount, date, note) VALUES (%s,%s,%s,%s,%s)", (goal_id, current_user.id, amount, date_raw, note))
    else:
        cursor.execute("INSERT INTO goal_contributions (goal_id, user_id, amount, date, note) VALUES (?,?,?,?,?)", (goal_id, current_user.id, amount, date_raw, note))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", tab="goals", msg="Contribution logged."))


@app.post("/settings/delete-goal-contribution")
@login_required
def settings_delete_goal_contribution():
    contribution_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM goal_contributions WHERE id=%s AND user_id=%s", (contribution_id, current_user.id))
    else:
        cursor.execute("DELETE FROM goal_contributions WHERE id=? AND user_id=?", (contribution_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    return redirect(url_for("manage", tab="goals", msg="Contribution removed."))


@app.post("/api/goal-pace-preview")
@login_required
def api_goal_pace_preview():
    """Live pace-suggestion preview for the Add/Edit Goal modal, mirroring
    the existing /api/income-preview pattern. Only ever a suggestion — never
    saved server-side, never applied to the goal automatically."""
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"error": "CSRF"}), 403
    try:
        target_amount = float(data.get("target_amount") or 0)
    except (TypeError, ValueError):
        target_amount = 0.0
    target_date_val = (data.get("target_date") or "").strip() or None
    try:
        progress_amount = float(data.get("progress_amount") or 0)
    except (TypeError, ValueError):
        progress_amount = 0.0

    if target_amount <= 0 or not target_date_val:
        return jsonify({"pace": None})

    pace = _suggest_goal_pace(target_amount, progress_amount, target_date_val)
    if pace is None:
        return jsonify({"pace": None})

    safe_to_spend = _get_safe_to_spend(current_user.id)
    warning = None
    if pace.get("monthly_pace") is not None and safe_to_spend is not None and pace["monthly_pace"] > safe_to_spend:
        warning = (
            f"This pace (£{pace['monthly_pace']:.2f}/month) is more than your current "
            f"Safe to Spend (£{safe_to_spend:.2f}) — you may need to adjust your budget "
            f"or extend the target date."
        )
    pace["warning"] = warning
    pace["safe_to_spend"] = safe_to_spend
    return jsonify({"pace": pace})


@app.post("/api/goal-commitment-preview")
@login_required
def api_goal_commitment_preview():
    """Live feedback for the goal contribution slider: given a candidate
    £/cycle amount, returns (a) the resulting Safe to Spend for the current
    cycle and (b) the projected completion date that amount would imply,
    computed by feeding it as a hypothetical pace_per_day into the exact
    same _project_goal_completion() used for the goal's real/estimated pace
    everywhere else — not a bespoke recalculation. Never saved server-side;
    the slider is only persisted when the user explicitly confirms (see
    /settings/set-goal-commitment)."""
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"error": "CSRF"}), 403

    try:
        goal_id = int(data.get("goal_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid goal_id"}), 400
    try:
        amount = max(0.0, float(data.get("amount") or 0))
    except (TypeError, ValueError):
        amount = 0.0

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    if not row:
        return jsonify({"error": "Goal not found"}), 404
    goal = dict(zip(cols, row))
    goal["target_amount"] = float(goal["target_amount"] or 0)

    accounts_by_id = {a["id"]: a for a in get_all_accounts(current_user.id)}
    progress = _compute_goal_progress(goal, current_user.id, accounts_by_id=accounts_by_id)
    safe_to_spend = _get_safe_to_spend(current_user.id) or 0.0

    pace_for_bounds = _suggest_goal_pace(goal["target_amount"], progress["progress_amount"], goal.get("target_date"))
    # Same real/estimated pace manage()'s initial render uses, so the
    # bounds returned here (e.g. after re-fetching on demand) match.
    pace_map, _fallback_count = _compute_goal_pace_map([goal], current_user.id, accounts_by_id=accounts_by_id)
    fallback_pace_per_day, _ = pace_map.get(goal["id"], (None, False))
    bounds = _compute_goal_commitment_bounds(goal, progress, pace_for_bounds, safe_to_spend, fallback_pace_per_day=fallback_pace_per_day)

    preview = _compute_goal_commitment_preview(progress, goal.get("target_date"), amount, safe_to_spend)
    preview["bounds"] = bounds
    return jsonify(preview)


@app.post("/settings/set-goal-commitment")
@login_required
def settings_set_goal_commitment():
    """Creates, updates, or removes the standing recurring commitment
    behind a goal's contribution slider - an ordinary savings_rules row
    with goal_id set (see database.py's migration for the full schema
    reasoning). Deliberately NOT Pro-gated, unlike settings_add_savings_rule
    - Goals themselves aren't a Pro feature, and this is conceptually part
    of Goals reusing the savings_rules engine, not the Savings Rules
    feature itself.

    amount <= 0 removes the commitment entirely (a slider dragged to zero
    means "no standing commitment", not "commit £0/cycle") rather than
    being rejected as an invalid amount."""
    goal_id_raw = (request.form.get("goal_id") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    from_account = (request.form.get("from_account") or "").strip()

    try:
        goal_id = int(goal_id_raw)
    except (ValueError, TypeError):
        return redirect(url_for("manage", tab="goals", msg="Invalid goal."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT * FROM goals WHERE id=%s AND user_id=%s", (goal_id, current_user.id))
    else:
        cursor.execute("SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, current_user.id))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="Goal not found."))
    goal = dict(zip(cols, row))

    try:
        amount = round(float(amount_raw), 2)
    except (ValueError, TypeError):
        amount = 0.0

    existing = _get_goal_commitment(goal_id, current_user.id)

    if amount <= 0:
        if existing:
            if USE_POSTGRES:
                cursor.execute("DELETE FROM savings_rules WHERE id=%s AND user_id=%s", (existing["id"], current_user.id))
            else:
                cursor.execute("DELETE FROM savings_rules WHERE id=? AND user_id=?", (existing["id"], current_user.id))
            db.commit()
        cursor.close()
        release_db(db)
        bust_forecast_cache(current_user.id)
        return redirect(url_for("manage", tab="goals", msg="Recurring commitment removed.", _anchor=f"goal-card-{goal_id}"))

    if not from_account:
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="Choose which account this comes from."))

    # Block against a currently-locked source account, same as bills/
    # income/transfers already do - a fresh commitment shouldn't be set up
    # against an account that can't take new activity.
    if _is_account_locked(current_user.id, from_account):
        cursor.close()
        release_db(db)
        return redirect(url_for("manage", tab="goals", msg="That account is locked."))

    # Server-side floor enforcement - the slider UI already prevents
    # dragging below a real minimum payment, but a form POST could bypass
    # that client-side check entirely.
    minimum_payment = goal.get("minimum_payment")
    if goal.get("goal_type") == "debt" and minimum_payment not in (None, "") and float(minimum_payment) > 0:
        if amount < float(minimum_payment):
            cursor.close()
            release_db(db)
            return redirect(url_for("manage", tab="goals", msg=f"This goal has a minimum payment of £{float(minimum_payment):.2f} — the commitment can't be set below it."))

    # to_account is only ever populated for a goal linked to a real,
    # unlocked SAVINGS account - crediting it is unambiguous and is the
    # whole point (it's what should visibly grow in the forecast chart).
    # Debt-linked and standalone goals leave it '' - a one-sided deduction
    # from from_account only, exactly like a bill. See database.py's
    # goal_id migration comment for the full reasoning.
    to_account = ""
    linked_account_id = goal.get("linked_account_id")
    if linked_account_id and goal.get("goal_type") == "savings":
        if USE_POSTGRES:
            cursor.execute("SELECT name, type, is_locked FROM accounts WHERE id=%s AND user_id=%s", (linked_account_id, current_user.id))
        else:
            cursor.execute("SELECT name, type, is_locked FROM accounts WHERE id=? AND user_id=?", (linked_account_id, current_user.id))
        acc_cols = [d[0] for d in cursor.description]
        acc_row = cursor.fetchone()
        if acc_row:
            acc = dict(zip(acc_cols, acc_row))
            if acc.get("type") == "savings" and not acc.get("is_locked"):
                to_account = acc["name"]

    # Anchored to the user's actual CURRENT cycle start day (not the raw
    # manual-mode budget_cycle_start column, which is unused/stale for a
    # user in automatic cycle mode) so the rule reliably falls inside every
    # future cycle's window regardless of cycle mode.
    import cycle_engine as _ce
    try:
        day = _ce.get_cycle(current_user.id)["display_start"].day
    except Exception:
        day = 1

    if existing:
        if USE_POSTGRES:
            cursor.execute(
                "UPDATE savings_rules SET amount=%s, day=%s, from_account=%s, to_account=%s WHERE id=%s AND user_id=%s",
                (amount, day, from_account, to_account, existing["id"], current_user.id),
            )
        else:
            cursor.execute(
                "UPDATE savings_rules SET amount=?, day=?, from_account=?, to_account=? WHERE id=? AND user_id=?",
                (amount, day, from_account, to_account, existing["id"], current_user.id),
            )
    else:
        rule_name = f"{goal['name']} contribution"
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id, goal_id) VALUES (%s,%s,%s,'monthly',%s,%s,%s,%s)",
                (rule_name, amount, day, from_account, to_account, current_user.id, goal_id),
            )
        else:
            cursor.execute(
                "INSERT INTO savings_rules (name, amount, day, frequency, from_account, to_account, user_id, goal_id) VALUES (?,?,?,'monthly',?,?,?,?)",
                (rule_name, amount, day, from_account, to_account, current_user.id, goal_id),
            )
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", tab="goals", msg=f"Recurring commitment of £{amount:.2f} set for '{goal['name']}'.", _anchor=f"goal-card-{goal_id}"))


@app.post("/settings/toggle-goal-commitment-pause")
@login_required
def settings_toggle_goal_commitment_pause():
    """Pauses or resumes an existing goal commitment without deleting it -
    unlike "Remove commitment" (which deletes the savings_rules row
    outright, losing the configured amount/from_account), this just flips
    is_paused so the same row can be resumed later exactly as it was left.
    A paused rule is skipped by all three live engine sites (see
    database.py's is_paused migration) - it stops reducing Safe to Spend/
    forecast for as long as it's paused, and resumes on its next scheduled
    occurrence once unpaused. Doesn't touch amount/from_account/to_account
    at all, so no validation beyond ownership is needed."""
    goal_id_raw = (request.form.get("goal_id") or "").strip()
    try:
        goal_id = int(goal_id_raw)
    except (ValueError, TypeError):
        return redirect(url_for("manage", tab="goals", msg="Invalid goal."))

    existing = _get_goal_commitment(goal_id, current_user.id)
    if not existing:
        return redirect(url_for("manage", tab="goals", msg="No commitment to pause."))

    new_paused = 0 if existing.get("is_paused") else 1
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE savings_rules SET is_paused=%s WHERE id=%s AND user_id=%s", (new_paused, existing["id"], current_user.id))
    else:
        cursor.execute("UPDATE savings_rules SET is_paused=? WHERE id=? AND user_id=?", (new_paused, existing["id"], current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    msg = "Commitment paused." if new_paused else "Commitment resumed."
    return redirect(url_for("manage", tab="goals", msg=msg, _anchor=f"goal-card-{goal_id}"))


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

    savings_type_raw = (request.form.get("savings_type") or "").strip()
    savings_type = savings_type_raw if acc_type == "savings" and savings_type_raw in ("variable", "fixed") else None

    # Free tier limit: max 3 accounts. Locked accounts (from a past downgrade)
    # don't count against this — they're not usable, so they shouldn't eat
    # into the allowance of usable accounts a Free user gets.
    if not user_is_pro():
        existing = [a for a in get_active_accounts(current_user.id) if not a.get("is_locked")]
        if len(existing) >= 3:
            return redirect(url_for("manage", msg="FREE_LIMIT_ACCOUNTS"))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id, savings_type) VALUES (%s, %s, %s, 1, %s, %s)", (name, balance, acc_type, current_user.id, savings_type))
    else:
        cursor.execute("INSERT INTO accounts (name, balance, type, active, user_id, savings_type) VALUES (?, ?, ?, 1, ?, ?)", (name, balance, acc_type, current_user.id, savings_type))
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
    bust_forecast_cache(current_user.id)
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

    if _is_account_locked_by_id(current_user.id, account_id):
        return redirect(url_for("manage", msg="This account is locked — upgrade to Pro to unlock it."))

    try:
        balance = float(balance)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid balance."))

    savings_rate_raw = request.form.get("savings_rate", "").strip()
    try:
        savings_rate = max(0.0, min(100.0, float(savings_rate_raw))) if savings_rate_raw else 0.0
    except ValueError:
        savings_rate = 0.0

    savings_type_raw = (request.form.get("savings_type") or "").strip()
    savings_type = savings_type_raw if acc_type == "savings" and savings_type_raw in ("variable", "fixed") else None

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
            cursor.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS savings_type TEXT")
            cursor.execute("UPDATE accounts SET name=%s, type=%s, balance=%s, savings_rate=%s, savings_type=%s, user_verified=1 WHERE id=%s AND user_id=%s", (name, acc_type, balance, savings_rate, savings_type, account_id, current_user.id))
        else:
            cursor.execute("UPDATE accounts SET name=?, type=?, balance=?, savings_type=?, user_verified=1 WHERE id=? AND user_id=?", (name, acc_type, balance, savings_type, account_id, current_user.id))
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
    bust_forecast_cache(current_user.id)
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
        return redirect(url_for("manage", msg="Missing fields.", tab="bills"))
    if frequency == "yearly" and not month_raw:
        return redirect(url_for("manage", msg="Please select a month for yearly bills.", tab="bills"))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err, tab="bills"))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err, tab="bills"))
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
    return redirect(url_for("manage", msg=f"Bill '{name}' added.", tab="bills"))

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
        return redirect(url_for("manage", msg="Missing fields.", tab="bills"))
    amount, err = validate_amount(amount)
    if err:
        return redirect(url_for("manage", msg=err, tab="bills"))
    day, err = validate_day(day)
    if err:
        return redirect(url_for("manage", msg=err, tab="bills"))
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
    return redirect(url_for("manage", msg="Bill updated.", tab="bills"))

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
    return redirect(url_for("manage", msg="Bill deleted.", tab="bills"))

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
    return redirect(url_for("manage", msg=f"Future event '{name}' added.", tab="rules"))

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
    return redirect(url_for("manage", msg="Future event updated.", tab="rules"))

@app.post("/settings/delete-future-event")
@login_required
def settings_delete_future_event():
    event_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM future_events WHERE id = %s AND user_id = %s", (event_id, current_user.id))
    else:
        cursor.execute("DELETE FROM future_events WHERE id = ? AND user_id = ?", (event_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Future event deleted.", tab="rules"))

@app.post("/settings/add-income")
@login_required
def settings_add_income():
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    account = (request.form.get("account") or "").strip()
    rule_type = (request.form.get("rule_type") or "").strip() or None
    rule_config = (request.form.get("rule_config") or "{}").strip()
    weekend_rule = (request.form.get("weekend_rule") or "before").strip()
    bh_rule = (request.form.get("bank_holiday_rule") or "before").strip()
    first_payment_date = (request.form.get("first_payment_date") or "").strip() or None

    try:
        weekly_day = max(0, min(6, int(request.form.get("weekly_day") or 4)))
    except ValueError:
        weekly_day = 4

    # derive day from rule_config for fixed_date (backward compat column)
    try:
        cfg = json.loads(rule_config)
    except (TypeError, ValueError):
        cfg = {}
        rule_config = "{}"
    day = int(cfg.get("day") or request.form.get("day") or 1)
    day = max(1, min(31, day))

    if not name or not amount or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()

    # Count existing income sources before insert to detect the first-ever addition
    if USE_POSTGRES:
        cursor.execute("SELECT COUNT(*) FROM income WHERE user_id = %s", (current_user.id,))
    else:
        cursor.execute("SELECT COUNT(*) FROM income WHERE user_id = ?", (current_user.id,))
    prior_income_count = cursor.fetchone()[0]

    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day, rule_type, rule_config, weekend_rule, bank_holiday_rule, first_payment_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (name, amount, frequency, account, current_user.id, day, weekly_day, rule_type, rule_config, weekend_rule, bh_rule, first_payment_date)
        )
    else:
        cursor.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day, rule_type, rule_config, weekend_rule, bank_holiday_rule, first_payment_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, amount, frequency, account, current_user.id, day, weekly_day, rule_type, rule_config, weekend_rule, bh_rule, first_payment_date)
        )
    new_id = cursor.lastrowid
    db.commit()

    # Auto-star this source if no primary exists yet; on the very first income source
    # also silently switch the user to automatic cycle mode.
    try:
        if USE_POSTGRES:
            cursor.execute("SELECT COUNT(*) FROM income WHERE user_id = %s AND is_primary = 1", (current_user.id,))
        else:
            cursor.execute("SELECT COUNT(*) FROM income WHERE user_id = ? AND is_primary = 1", (current_user.id,))
        primary_count = cursor.fetchone()[0]
        if primary_count == 0:
            if USE_POSTGRES:
                cursor.execute("UPDATE income SET is_primary = 1 WHERE id = %s AND user_id = %s", (new_id, current_user.id))
            else:
                cursor.execute("UPDATE income SET is_primary = 1 WHERE id = ? AND user_id = ?", (new_id, current_user.id))
            if prior_income_count == 0:
                if USE_POSTGRES:
                    cursor.execute("UPDATE users SET cycle_mode = 'automatic' WHERE id = %s", (current_user.id,))
                else:
                    cursor.execute("UPDATE users SET cycle_mode = 'automatic' WHERE id = ?", (current_user.id,))
            db.commit()
    except Exception:
        pass

    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg=f"Income source '{name}' added.", tab="income"))


# --- SELF-EMPLOYED INCOME SETUP (New — Beta) ---
# One-time initial setup for a self-employed user's first income source: creates
# a single averaged-income row (rule_type='self_employed_average') and routes the
# user into manual cycle mode instead of the automatic/payday-based flow, since
# there's no real fixed pay day to anchor an automatic cycle on. Manual/automatic
# averaging and lump/spread distribution can be changed anytime in Settings —
# this route only ever runs once, at initial setup.
@app.post("/settings/setup-self-employed")
@login_required
def settings_setup_self_employed():
    manual_amount = (request.form.get("manual_amount") or "").strip()
    account = (request.form.get("account") or "").strip()
    cycle_start_day = (request.form.get("cycle_start_day") or "1").strip()

    if not manual_amount or not account:
        return redirect(url_for("manage", msg="Missing fields.", tab="income"))
    try:
        manual_amount = float(manual_amount)
        if manual_amount <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount.", tab="income"))
    try:
        cycle_start_day = max(1, min(28, int(cycle_start_day)))
    except ValueError:
        cycle_start_day = 1

    rule_config = json.dumps({
        "mode": "manual",
        "window_months": 3,
        "manual_amount": manual_amount,
        "distribution": "lump",
        "day": cycle_start_day,
    })

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE users SET employment_type = 'self_employed', cycle_mode = 'manual', budget_cycle_start = %s WHERE id = %s", (cycle_start_day, current_user.id))
        cursor.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (%s, %s, 'monthly', %s, %s, %s, %s, %s, 1)",
            ("Self-employed income", manual_amount, account, current_user.id, cycle_start_day, "self_employed_average", rule_config)
        )
    else:
        cursor.execute("UPDATE users SET employment_type = 'self_employed', cycle_mode = 'manual', budget_cycle_start = ? WHERE id = ?", (cycle_start_day, current_user.id))
        cursor.execute(
            "INSERT INTO income (name, amount, frequency, account, user_id, day, rule_type, rule_config, is_primary) "
            "VALUES (?, ?, 'monthly', ?, ?, ?, ?, ?, 1)",
            ("Self-employed income", manual_amount, account, current_user.id, cycle_start_day, "self_employed_average", rule_config)
        )
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Your income estimate is set up.", tab="income"))


# --- SELF-EMPLOYED INCOME AVERAGING SETTINGS (New — Beta) ---
# Updates the manual/automatic mode, averaging window, manual amount, and
# lump-sum/spread distribution for the user's self_employed_average income row.
# Switchable anytime, takes effect immediately (forecast cache busted below) —
# unlike settings_setup_self_employed(), this can run any number of times.
@app.post("/settings/save-income-averaging")
@login_required
def settings_save_income_averaging():
    mode = (request.form.get("mode") or "manual").strip()
    if mode not in ("manual", "auto"):
        mode = "manual"
    distribution = (request.form.get("distribution") or "lump").strip()
    if distribution not in ("lump", "spread"):
        distribution = "lump"
    try:
        window_months = int(request.form.get("window_months") or 3)
        if window_months not in (1, 3, 6):
            window_months = 3
    except ValueError:
        window_months = 3
    try:
        manual_amount = float(request.form.get("manual_amount") or 0)
    except ValueError:
        manual_amount = 0.0

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, rule_config FROM income WHERE user_id = %s AND rule_type = 'self_employed_average' LIMIT 1", (current_user.id,))
    else:
        cursor.execute("SELECT id, rule_config FROM income WHERE user_id = ? AND rule_type = 'self_employed_average' LIMIT 1", (current_user.id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return redirect(url_for("settings", msg="No self-employed income source found."))

    inc_id = row[0] if USE_POSTGRES else row["id"]
    try:
        cfg = json.loads((row[1] if USE_POSTGRES else row["rule_config"]) or "{}")
    except (TypeError, ValueError):
        cfg = {}
    cfg["mode"] = mode
    cfg["distribution"] = distribution
    cfg["window_months"] = window_months
    cfg["manual_amount"] = manual_amount

    if USE_POSTGRES:
        cursor.execute("UPDATE income SET rule_config = %s WHERE id = %s AND user_id = %s", (json.dumps(cfg), inc_id, current_user.id))
    else:
        cursor.execute("UPDATE income SET rule_config = ? WHERE id = ? AND user_id = ?", (json.dumps(cfg), inc_id, current_user.id))
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("settings", msg="Income averaging settings saved."))


@app.post("/settings/edit-income")
@login_required
def settings_edit_income():
    income_id = request.form.get("id")
    name = (request.form.get("name") or "").strip()
    amount = (request.form.get("amount") or "").strip()
    frequency = (request.form.get("frequency") or "monthly").strip()
    account = (request.form.get("account") or "").strip()
    rule_type = (request.form.get("rule_type") or "").strip() or None
    rule_config = (request.form.get("rule_config") or "{}").strip()
    weekend_rule = (request.form.get("weekend_rule") or "before").strip()
    bh_rule = (request.form.get("bank_holiday_rule") or "before").strip()
    first_payment_date = (request.form.get("first_payment_date") or "").strip() or None

    try:
        weekly_day = max(0, min(6, int(request.form.get("weekly_day") or 4)))
    except ValueError:
        weekly_day = 4

    try:
        cfg = json.loads(rule_config)
    except (TypeError, ValueError):
        cfg = {}
        rule_config = "{}"
    day = int(cfg.get("day") or request.form.get("day") or 1)
    day = max(1, min(31, day))

    if not name or not amount or not account:
        return redirect(url_for("manage", msg="Missing fields."))
    try:
        amount = float(amount)
    except ValueError:
        return redirect(url_for("manage", msg="Invalid amount."))

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "UPDATE income SET name=%s, amount=%s, frequency=%s, account=%s, day=%s, weekly_day=%s, rule_type=%s, rule_config=%s, weekend_rule=%s, bank_holiday_rule=%s, first_payment_date=%s, user_verified=1 WHERE id=%s AND user_id=%s",
            (name, amount, frequency, account, day, weekly_day, rule_type, rule_config, weekend_rule, bh_rule, first_payment_date, income_id, current_user.id)
        )
    else:
        cursor.execute(
            "UPDATE income SET name=?, amount=?, frequency=?, account=?, day=?, weekly_day=?, rule_type=?, rule_config=?, weekend_rule=?, bank_holiday_rule=?, first_payment_date=?, user_verified=1 WHERE id=? AND user_id=?",
            (name, amount, frequency, account, day, weekly_day, rule_type, rule_config, weekend_rule, bh_rule, first_payment_date, income_id, current_user.id)
        )
    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Income updated.", tab="income"))

@app.post("/settings/delete-income")
@login_required
def settings_delete_income():
    income_id = request.form.get("id")
    db = get_db()
    cursor = db.cursor()

    # Check if the source being deleted is primary before removing it
    if USE_POSTGRES:
        cursor.execute("SELECT is_primary FROM income WHERE id = %s AND user_id = %s", (income_id, current_user.id))
    else:
        cursor.execute("SELECT is_primary FROM income WHERE id = ? AND user_id = ?", (income_id, current_user.id))
    row = cursor.fetchone()
    was_primary = row is not None and row[0] == 1

    if USE_POSTGRES:
        cursor.execute("DELETE FROM income WHERE id = %s AND user_id = %s", (income_id, current_user.id))
    else:
        cursor.execute("DELETE FROM income WHERE id = ? AND user_id = ?", (income_id, current_user.id))

    # If the primary source was removed, revert cycle mode to manual so the user
    # isn't left in automatic mode with no source powering it.
    if was_primary:
        if USE_POSTGRES:
            cursor.execute("UPDATE users SET cycle_mode = 'manual' WHERE id = %s", (current_user.id,))
        else:
            cursor.execute("UPDATE users SET cycle_mode = 'manual' WHERE id = ?", (current_user.id,))

    db.commit()
    cursor.close()
    release_db(db)
    bust_forecast_cache(current_user.id)
    return redirect(url_for("manage", msg="Income source deleted.", tab="income"))


@app.post("/api/income-preview")
@login_required
def api_income_preview():
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify({"error": "CSRF"}), 403
    try:
        inc = {
            "frequency": data.get("frequency", "monthly"),
            "rule_type": data.get("rule_type") or None,
            "rule_config": data.get("rule_config") or "{}",
            "weekend_rule": data.get("weekend_rule", "before"),
            "bank_holiday_rule": data.get("bank_holiday_rule", "before"),
            "weekly_day": data.get("weekly_day", 4),
            "first_payment_date": data.get("first_payment_date") or None,
            "day": data.get("day", 25),
        }
        from_date_str = data.get("from_date")
        from_date = date.fromisoformat(from_date_str) if from_date_str else date.today()
        next_dates = income_engine.get_next_dates(inc, n=3, from_date=from_date)
        return jsonify({"dates": [d.isoformat() for d in next_dates]})
    except Exception as exc:
        logger.warning("income-preview error: %s", exc)
        return jsonify({"dates": []}), 200


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
    bust_forecast_cache(current_user.id)
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
    bust_forecast_cache(current_user.id)
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
    bust_forecast_cache(current_user.id)
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
    bust_forecast_cache(current_user.id)
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
    bust_forecast_cache(current_user.id)
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
    forecast_days = 90
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
                locked_count=cached_data.get("locked_count", 0),
                message=request.args.get("msg", ""),
                today=today.isoformat()
            )

    accounts_rows = get_active_accounts(current_user.id)
    # Locked accounts (from a Pro->Free downgrade) are frozen and excluded from
    # the forecast entirely — including a balance that can't change would
    # present an increasingly stale number with the same confidence as live
    # data. The template shows a note with the count so the user knows why
    # their forecast covers fewer accounts than they actually have.
    locked_count = sum(1 for r in accounts_rows if r.get("is_locked"))
    # Captured before filtering so a savings_rule whose to_account is
    # locked can be paused entirely below (see the savings_rules loop),
    # not just have its credit side silently skipped.
    _fc_locked_names = {r["name"] for r in accounts_rows if r.get("is_locked")}
    accounts_rows = [r for r in accounts_rows if not r.get("is_locked")]
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
    income_rows = _resolve_income_rows(income_rows, current_user.id)

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
                "id": e["id"],
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

    # Scrollback before today: a flat line at today's actual balance, not a
    # reconstruction from transaction history. Honestly represents "this is
    # your balance as of today" rather than implying real past movement -
    # always spans the full 90-day window so the scroll/zoom interaction
    # feels identical whether the account has years of transactions or none.
    hist_cutoff = today - timedelta(days=90)
    hist_snapshots = []
    d_ptr = hist_cutoff
    while d_ptr < today:
        snap = {"date": d_ptr.isoformat(), "historical": True}
        for acc_n in account_names:
            snap[acc_n] = initial_balances[acc_n]
        hist_snapshots.append(snap)
        d_ptr += timedelta(days=1)

    # Start with today's actual balances as day 0
    today_snapshot = {"date": today.isoformat()}
    for acc in account_names:
        today_snapshot[acc] = round(simulated[acc], 2)
    snapshots = [today_snapshot]

    # Pre-compute all income payment dates in the forecast window using income_engine
    forecast_end = today + timedelta(days=forecast_days)
    income_by_date: dict = {}  # date -> list of (account, amount)
    for inc in income_rows:
        if inc.get("_distribution") == "spread":
            # Spread-evenly self-employed income has no discrete payment date -
            # accrue a flat daily amount for every day of the forecast instead.
            cycle_len = _self_employed_cycle_length_days(current_user.id)
            daily_amount = float(inc.get("amount") or 0) / cycle_len if cycle_len else 0.0
            if daily_amount:
                d = today + timedelta(days=1)
                while d <= forecast_end:
                    income_by_date.setdefault(d, []).append((inc["account"], daily_amount))
                    d += timedelta(days=1)
            continue
        for d in income_engine.get_payment_dates(inc, today + timedelta(days=1), forecast_end):
            income_by_date.setdefault(d, []).append((inc["account"], float(inc["amount"])))

    for day_offset in range(1, forecast_days + 1):
        sim_day = today + timedelta(days=day_offset)

        for acc, amt in income_by_date.get(sim_day, []):
            if acc in simulated:
                simulated[acc] += amt

        for expense in scheduled:
            exp_day = expense.get("day")
            if exp_day is None:
                continue
            freq = expense.get("frequency") or "monthly"
            if freq == "yearly":
                # Fire once a year on the specific day+month, shifted off weekends
                if expense.get("month") == sim_day.month:
                    try:
                        nominal = date(sim_day.year, sim_day.month, exp_day)
                    except ValueError:
                        nominal = None
                    if nominal is not None and shift_weekend_to_monday(nominal) == sim_day and expense["account"] in simulated:
                        simulated[expense["account"]] -= float(expense["amount"])
            else:
                # Monthly: fire on the given day every month, shifted off weekends
                try:
                    nominal = date(sim_day.year, sim_day.month, exp_day)
                except ValueError:
                    nominal = None
                if nominal is not None and shift_weekend_to_monday(nominal) == sim_day and expense["account"] in simulated:
                    simulated[expense["account"]] -= float(expense["amount"])

        for event in future_events:
            if event["date"] == sim_day and event["account"] in simulated:
                simulated[event["account"]] -= float(event["amount"])

        for rule in savings_rules:
            if rule.get("is_paused"):
                continue
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
                # See api_snapshot()'s equivalent loop for the full
                # reasoning: a non-empty, locked to_account pauses the
                # whole rule, not just the credit side.
                if to_acc and to_acc in _fc_locked_names:
                    continue
                if from_acc in simulated:
                    simulated[from_acc] -= amt
                if to_acc in simulated:
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
        if bill.get("account") not in accounts:
            continue
        freq = bill.get("frequency") or "monthly"
        if freq == "yearly":
            ann_month = bill.get("month")
            if ann_month:
                for year in [today.year, today.year + 1]:
                    try:
                        d = shift_weekend_to_monday(date(year, ann_month, bill["day"]))
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
                    occurrence = shift_weekend_to_monday(date(m_year, m_month, bill_day))
                    if today <= occurrence <= end_date:
                        upcoming_items.append({"date": occurrence.isoformat(), "name": bill["name"], "amount": float(bill["amount"]), "account": bill["account"], "type": "bill", "id": bill["id"]})
                except ValueError:
                    pass
                m_month += 1
                if m_month > 12:
                    m_month = 1
                    m_year += 1

    for inc in income_rows:
        if inc.get("account") not in accounts:
            continue
        if inc.get("_distribution") == "spread":
            # Spread-evenly income has no discrete "upcoming" event to list -
            # it's a continuous accrual, already reflected in the balance chart.
            continue
        for d in income_engine.get_payment_dates(inc, today, end_date):
            upcoming_items.append({"date": d.isoformat(), "name": inc["name"], "amount": float(inc["amount"]), "account": inc["account"], "type": "income", "id": inc["id"]})

    for event in future_events:
        if event["account"] not in accounts:
            continue
        if today <= event["date"] <= end_date:
            upcoming_items.append({"date": event["date"].isoformat(), "name": event["name"], "amount": float(event["amount"]), "account": event["account"], "type": "event", "id": event["id"]})

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
        "locked_count": locked_count,
    })

    return render_template(
        "forecast.html",
        snapshots=snapshots_json,
        account_names=account_names_json,
        account_types=account_types_json,
        initial_balances=initial_balances_json,
        locked_count=locked_count,
        upcoming=upcoming_json,
        hist_snapshots=hist_snapshots_json,
        savings_rates=savings_rates_json,
        message=request.args.get("msg", ""),
        today=today.isoformat(),
        show_my_money_dot=get_my_money_dot(current_user.id),
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

# --- WELCOME / ONBOARDING JOURNEY ---
@app.get("/welcome")
def welcome():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    return render_template("welcome.html")

# --- REGISTER ---
# GET: shows the registration form
# POST: validates email/password, creates user, sends verification email, logs them in
@app.get("/register")
def register():
    seed_income = request.args.get('income', '')
    seed_payday = request.args.get('payday', '')
    seed_bills  = request.args.get('bills', '')
    return render_template("register.html", seed_income=seed_income, seed_payday=seed_payday, seed_bills=seed_bills)

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
            "INSERT INTO users (email, password, display_name, created_at, verify_token, verify_token_expires_at, show_welcome_modal) VALUES (%s, %s, %s, %s, %s, %s, 1) RETURNING id",
            (email, hashed, display_name, today_str, token, expires_at)
        )
        user_id = cursor.fetchone()[0]
    else:
        cursor.execute(
            "INSERT INTO users (email, password, display_name, created_at, verify_token, verify_token_expires_at, show_welcome_modal) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (email, hashed, display_name, today_str, token, expires_at)
        )
        user_id = cursor.lastrowid

    db.commit()
    cursor.close()
    release_db(db)

    # Seed account, income, and bills from landing page forecast if params were passed
    _apply_seed_data(
        user_id,
        request.form.get('seed_income', ''),
        request.form.get('seed_payday', ''),
        request.form.get('seed_bills', ''),
        request.form.get('seed_balance', ''),
    )

    logger.info(f"New user registered: user_id={user_id}")
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

    if not row["password"] or not check_password_hash(row["password"], password):
        logger.warning(f"Failed login attempt for user_id={row['id']}")
        return render_template("login.html", error="Invalid email or password.")

    logger.info(f"Successful login for user_id={row['id']}")

    user = User(row["id"], row["email"])
    login_user(user, remember=True)
    session.permanent = True
    track_for_user(row["id"], 'auth.login')
    return redirect(url_for("home"))

@app.get("/logout")
def logout():
    if current_user.is_authenticated:
        logger.info(f"User logout: user_id={current_user.id}")
    logout_user()
    return redirect("/")

# --- GOOGLE OAUTH ROUTES ---

def _apply_seed_data(user_id, seed_income, seed_payday, seed_bills, seed_balance=''):
    """Seed income, bills, and starting account for a newly created user from onboarding params."""
    try:
        inc_amt     = float(seed_income)  if seed_income  else 0.0
        bills_amt   = float(seed_bills)   if seed_bills   else 0.0
        balance_amt = max(0.0, float(seed_balance) if seed_balance else 0.0)
        if inc_amt <= 0 or not seed_payday:
            return
        payday_map = {
            'Weekly':           ('weekly',      None, None,              '{}'),
            'Fortnightly':      ('fortnightly', None, None,              '{}'),
            '1st':              ('monthly',     1,    'fixed_date',       '{"day": 1}'),
            '15th':             ('monthly',     15,   'fixed_date',       '{"day": 15}'),
            '25th':             ('monthly',     25,   'fixed_date',       '{"day": 25}'),
            'Last working day': ('monthly',     None, 'last_working_day', '{}'),
        }
        if seed_payday not in payday_map:
            return
        freq, day, rtype, rcfg = payday_map[seed_payday]
        wday = 4 if freq in ('weekly', 'fortnightly') else None
        sdb = get_db()
        sc = sdb.cursor()
        if USE_POSTGRES:
            sc.execute(
                "INSERT INTO accounts (name, balance, type, active, include_in_overview, user_id, is_seeded) VALUES (%s,%s,%s,1,1,%s,1)",
                ('Current Account', balance_amt, 'current', user_id),
            )
            sdb.commit()
            sc.execute(
                "INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day, rule_type, rule_config, weekend_rule, bank_holiday_rule, first_payment_date, is_primary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ('My salary', inc_amt, freq, '', user_id, day, wday, rtype, rcfg, 'before', 'before', None, 1),
            )
            sdb.commit()
            if bills_amt > 0:
                sc.execute(
                    "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, month) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    ('Monthly bills', bills_amt, 1, 'Current Account', user_id, 'monthly', None),
                )
                sdb.commit()
            sc.execute("UPDATE users SET cycle_mode = 'automatic' WHERE id = %s", (user_id,))
        else:
            sc.execute(
                "INSERT INTO accounts (name, balance, type, active, include_in_overview, user_id, is_seeded) VALUES (?,?,?,1,1,?,1)",
                ('Current Account', balance_amt, 'current', user_id),
            )
            sdb.commit()
            sc.execute(
                "INSERT INTO income (name, amount, frequency, account, user_id, day, weekly_day, rule_type, rule_config, weekend_rule, bank_holiday_rule, first_payment_date, is_primary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('My salary', inc_amt, freq, '', user_id, day, wday, rtype, rcfg, 'before', 'before', None, 1),
            )
            sdb.commit()
            if bills_amt > 0:
                sc.execute(
                    "INSERT INTO scheduled_expenses (name, amount, day, account, user_id, frequency, month) VALUES (?,?,?,?,?,?,?)",
                    ('Monthly bills', bills_amt, 1, 'Current Account', user_id, 'monthly', None),
                )
                sdb.commit()
            sc.execute("UPDATE users SET cycle_mode = 'automatic' WHERE id = ?", (user_id,))
        sdb.commit()
        sc.close()
        release_db(sdb)
    except Exception as e:
        logger.warning(f"Seed data error for user {user_id}: {e}")


@app.get('/auth/google')
def auth_google():
    # Stash onboarding seed params before the OAuth redirect loses them
    session['google_seed'] = {
        'income':  request.args.get('income', ''),
        'payday':  request.args.get('payday', ''),
        'bills':   request.args.get('bills', ''),
        'balance': request.args.get('balance', ''),
    }
    redirect_uri = url_for('auth_google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.get('/auth/google/callback')
def auth_google_callback():
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get('userinfo') or {}
    except Exception as e:
        logger.warning(f"Google OAuth callback error: {e}")
        return redirect(url_for('login') + '?google_error=1')

    email = (userinfo.get('email') or '').strip().lower()
    google_sub = (userinfo.get('sub') or '').strip()
    display_name = (userinfo.get('name') or '').strip()

    if not email or not google_sub:
        return redirect(url_for('login') + '?google_error=1')

    db = get_db()
    cursor = db.cursor()
    ph = '%s' if USE_POSTGRES else '?'

    # 1. Returning Google user — look up by google_id
    cursor.execute(f"SELECT id, email FROM users WHERE google_id = {ph}", (google_sub,))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if row:
        row = dict(zip(cols, row))
        cursor.close()
        release_db(db)
        user = User(row['id'], row['email'])
        session.permanent = True
        login_user(user, remember=True)
        track_for_user(row['id'], 'auth.google_login')
        return redirect(url_for('home'))

    # 2. Existing password-based account with same email — link Google ID
    cursor.execute(f"SELECT id, email FROM users WHERE email = {ph}", (email,))
    row = cursor.fetchone()
    if row:
        row = dict(zip(cols, row))
        cursor.execute(f"UPDATE users SET google_id = {ph} WHERE id = {ph}", (google_sub, row['id']))
        db.commit()
        cursor.close()
        release_db(db)
        user = User(row['id'], row['email'])
        session.permanent = True
        login_user(user, remember=True)
        track_for_user(row['id'], 'auth.google_link')
        return redirect(url_for('home'))

    # 3. Brand-new user — create account (Google accounts are inherently verified)
    today_str = date.today().isoformat()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO users (email, password, display_name, created_at, verified, google_id, show_welcome_modal) "
            "VALUES (%s, NULL, %s, %s, 1, %s, 1) RETURNING id",
            (email, display_name or None, today_str, google_sub),
        )
        user_id = cursor.fetchone()[0]
    else:
        cursor.execute(
            "INSERT INTO users (email, password, display_name, created_at, verified, google_id, show_welcome_modal) "
            "VALUES (?, NULL, ?, ?, 1, ?, 1)",
            (email, display_name or None, today_str, google_sub),
        )
        user_id = cursor.lastrowid
    db.commit()
    cursor.close()
    release_db(db)

    seed = session.pop('google_seed', {})
    _apply_seed_data(user_id, seed.get('income', ''), seed.get('payday', ''), seed.get('bills', ''), seed.get('balance', ''))

    logger.info(f"New Google user registered: user_id={user_id}")
    user = User(user_id, email)
    session.permanent = True
    login_user(user, remember=True)
    track_for_user(user_id, 'auth.google_register')
    return redirect(url_for('home', msg="Welcome! Your Google account is now connected."))

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
    locked_accounts = {r["name"] for r in accounts_rows if r.get("is_locked")}

    if request.method == 'GET':
        track('page_view.import')
        return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error=request.args.get('msg'), selected_account=None)

    # Validate CSRF
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error="Invalid request.", selected_account=None)

    selected_account = (request.form.get('account') or '').strip()
    file = request.files.get('csv_file')

    if selected_account and _is_account_locked(current_user.id, selected_account):
        return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error=f"'{selected_account}' is locked — upgrade to Pro to unlock it.", selected_account=selected_account)

    if not file or not file.filename:
        return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error="Please select a CSV file.", selected_account=selected_account)

    try:
        content = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            file.seek(0)
            content = file.read().decode('latin-1')
        except Exception:
            return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error="Could not read the file.", selected_account=selected_account)

    rows, err = parse_bank_csv(content)
    if err:
        return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=None, error=err, selected_account=selected_account)

    _purge_expired_imports()
    token = secrets.token_urlsafe(16)
    _pending_imports[token] = (time.time(), rows, selected_account)
    session['import_token'] = token

    return render_template('import.html', accounts=accounts, locked_accounts=locked_accounts, preview=rows, error=None, selected_account=selected_account)


@app.post('/import/confirm')
@login_required
def import_confirm():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        return redirect(url_for('import_csv'))

    token = session.pop('import_token', None)
    rows, account = _get_pending_import(token)
    _pending_imports.pop(token, None)

    if not rows or not account:
        return redirect(url_for('import_csv'))

    if _is_account_locked(current_user.id, account):
        return redirect(url_for('import_csv', msg=f"'{account}' is locked — upgrade to Pro to unlock it."))

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
# customer.subscription.updated → status changed → is_pro=0 for past_due/unpaid/canceled,
#   is_pro=1 for active/trialing (covers payment-retry recovery)
# invoice.payment_failed → Stripe is still retrying, don't revoke access yet — just log/track
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
        # .to_dict() converts the StripeObject (and nested StripeObjects like
        # metadata) into plain dicts — StripeObject itself has no .get(), only
        # __getitem__/attribute access, so calling .get() directly on it raises
        # AttributeError in live mode (this is exactly what crashed the webhook).
        session_obj = event["data"]["object"].to_dict()
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
            sync_account_locks(int(user_id), True)
            track_for_user(int(user_id), 'billing.upgrade_complete')

    # Subscription cancelled — deactivate Pro
    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].to_dict().get("customer")

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
                sync_account_locks(uid_row[0], False)
                track_for_user(uid_row[0], 'billing.cancel')

    # Subscription status changed — keep is_pro in sync with the subscription's current
    # status. Stripe sends this alongside subscription.deleted when a subscription is
    # fully cancelled (both end up setting is_pro=0, so they agree rather than race),
    # and it's also how we learn a subscription recovered after a failed-payment retry.
    elif event["type"] == "customer.subscription.updated":
        sub_obj = event["data"]["object"].to_dict()
        customer_id = sub_obj.get("customer")
        status = sub_obj.get("status")

        if status in ("past_due", "unpaid", "canceled"):
            new_is_pro = 0
        elif status in ("active", "trialing"):
            new_is_pro = 1
        else:
            new_is_pro = None

        if customer_id and new_is_pro is not None:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
            else:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer_id,))
            uid_row = cursor.fetchone()
            if USE_POSTGRES:
                cursor.execute("UPDATE users SET is_pro = %s WHERE stripe_customer_id = %s", (new_is_pro, customer_id))
            else:
                cursor.execute("UPDATE users SET is_pro = ? WHERE stripe_customer_id = ?", (new_is_pro, customer_id))
            db.commit()
            cursor.close()
            release_db(db)
            logger.info(f"Subscription status '{status}' for customer_id={customer_id} -> is_pro={new_is_pro}")
            if uid_row:
                sync_account_locks(uid_row[0], bool(new_is_pro))
                track_for_user(uid_row[0], 'billing.subscription_recovered' if new_is_pro else 'billing.subscription_past_due')

    # Payment failed — Stripe retries failed payments over a dunning cycle before the
    # subscription actually lapses, so don't revoke access here. Just log it so at-risk
    # subscriptions are visible; the real access change happens via the status update
    # above (past_due/unpaid) or the eventual subscription.deleted.
    elif event["type"] == "invoice.payment_failed":
        invoice_obj = event["data"]["object"].to_dict()
        customer_id = invoice_obj.get("customer")
        logger.warning(f"Stripe payment failed for customer_id={customer_id}")

        if customer_id:
            db = get_db()
            cursor = db.cursor()
            if USE_POSTGRES:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = %s", (customer_id,))
            else:
                cursor.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer_id,))
            uid_row = cursor.fetchone()
            cursor.close()
            release_db(db)
            if uid_row:
                track_for_user(uid_row[0], 'billing.payment_failed')

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


# --- HELPER: lock/unlock accounts on Pro <-> Free transitions ---
# Free tier allows 3 accounts. A user who was ever Pro can have more than
# that; downgrading shouldn't delete anything, but accounts beyond the
# oldest 3 (by id, i.e. creation order — accounts has no created_at column)
# become locked: visible with data intact, but read-only until they
# re-upgrade, at which point everything unlocks exactly as it was.
def sync_account_locks(user_id, is_pro):
    db = get_db()
    cursor = db.cursor()
    try:
        if is_pro:
            if USE_POSTGRES:
                cursor.execute("UPDATE accounts SET is_locked = 0 WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("UPDATE accounts SET is_locked = 0 WHERE user_id = ?", (user_id,))
        else:
            if USE_POSTGRES:
                cursor.execute("SELECT id FROM accounts WHERE user_id = %s AND active = 1 ORDER BY id ASC", (user_id,))
            else:
                cursor.execute("SELECT id FROM accounts WHERE user_id = ? AND active = 1 ORDER BY id ASC", (user_id,))
            active_ids = [row[0] for row in cursor.fetchall()]
            keep_ids = set(active_ids[:3])
            for acc_id in active_ids:
                lock_value = 0 if acc_id in keep_ids else 1
                if USE_POSTGRES:
                    cursor.execute("UPDATE accounts SET is_locked = %s WHERE id = %s", (lock_value, acc_id))
                else:
                    cursor.execute("UPDATE accounts SET is_locked = ? WHERE id = ?", (lock_value, acc_id))
        db.commit()
    finally:
        cursor.close()
        release_db(db)
    # A cached forecast computed before this lock/unlock transition would show
    # the wrong set of accounts for up to FORECAST_CACHE_TTL - bust it so the
    # next load reflects the new lock state immediately.
    bust_forecast_cache(user_id)


def _is_account_locked(user_id, account_name):
    """True if the named account belongs to user_id and is currently locked
    (Free-tier downgrade lock) — used to block adding new activity against it."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT is_locked FROM accounts WHERE user_id = %s AND name = %s", (user_id, account_name))
    else:
        cursor.execute("SELECT is_locked FROM accounts WHERE user_id = ? AND name = ?", (user_id, account_name))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    if not row:
        return False
    return bool(row[0] if USE_POSTGRES else row["is_locked"])


def _is_account_locked_by_id(user_id, account_id):
    """Same as _is_account_locked() but keyed by account id rather than name."""
    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT is_locked FROM accounts WHERE user_id = %s AND id = %s", (user_id, account_id))
    else:
        cursor.execute("SELECT is_locked FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id))
    row = cursor.fetchone()
    cursor.close()
    release_db(db)
    if not row:
        return False
    return bool(row[0] if USE_POSTGRES else row["is_locked"])


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
    for r in signups:
        r["bar_pct"] = max(1, round(r["n"] / signup_max * 100))
    for r in dau_series:
        r["bar_pct"] = max(1, round(r["n"] / dau_max * 100))

    return render_template("admin_analytics.html",
        mau=mau, wau=wau, dau=dau, total_users=total_users,
        signups=signups, signup_max=signup_max,
        dau_series=dau_series, dau_max=dau_max,
        feature_usage=feature_usage,
        retention=retention, retention_pct=retention_pct,
        funnel=funnel, funnel_pct=funnel_pct,
        table_stats=table_stats,
    )


# --- FOUNDER/ADMIN PRO OVERRIDE ---
# Grants Pro to a specific user without going through Stripe at all - for
# founder/testing accounts only. Deliberately reuses the exact same
# users.is_pro column and user_is_pro() read path every other Pro check in
# the app already uses (routes, templates, sync_account_locks) rather than
# a parallel "is this a founder account" check bolted on elsewhere - one
# engine, same principle as everywhere else in this codebase. Never writes
# stripe_customer_id, so no Stripe webhook (which only ever acts on a
# matching stripe_customer_id) can revoke it later. Same admin gate as
# /admin/analytics - requires the admin user to have already unlocked the
# admin area this session via /admin/unlock.
@app.get("/admin/grant-pro")
@login_required
@limiter.limit("30 per hour")
def admin_grant_pro():
    if current_user.id != ADMIN_USER_ID or session.get("admin_unlocked") != ADMIN_SECRET or not ADMIN_SECRET:
        logger.warning(f"Blocked admin access attempt — user_id={current_user.id} ip={request.remote_addr}")
        return render_template("404.html"), 404

    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return "Usage: /admin/grant-pro?email=someone@example.com", 400

    db = get_db()
    cursor = db.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,))
    else:
        cursor.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        release_db(db)
        return f"No user found with email {email}", 404
    target_user_id = row[0] if USE_POSTGRES else row["id"]

    if USE_POSTGRES:
        cursor.execute("UPDATE users SET is_pro = 1 WHERE id = %s", (target_user_id,))
    else:
        cursor.execute("UPDATE users SET is_pro = 1 WHERE id = ?", (target_user_id,))
    db.commit()
    cursor.close()
    release_db(db)

    # Same unlock-everything step the real Stripe upgrade path triggers on
    # checkout.session.completed - without this, any of the founder's
    # accounts already locked from a prior Free-tier state would stay
    # locked despite is_pro now being true.
    sync_account_locks(target_user_id, True)

    logger.info(f"Founder Pro override granted by admin user_id={current_user.id} to user_id={target_user_id} ({email})")
    return f"Pro access granted to {email} (founder override — not tied to any Stripe subscription)."


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


# --- SITEMAP ---
@app.get("/sitemap.xml")
def sitemap():
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += '  <url><loc>https://spendara.co.uk/</loc><priority>1.0</priority><changefreq>weekly</changefreq></url>\n'
    xml += '  <url><loc>https://spendara.co.uk/register</loc><priority>0.9</priority><changefreq>monthly</changefreq></url>\n'
    xml += '  <url><loc>https://spendara.co.uk/login</loc><priority>0.8</priority><changefreq>monthly</changefreq></url>\n'
    xml += '  <url><loc>https://spendara.co.uk/forgot-password</loc><priority>0.3</priority><changefreq>yearly</changefreq></url>\n'
    xml += '  <url><loc>https://spendara.co.uk/privacy</loc><priority>0.3</priority><changefreq>yearly</changefreq></url>\n'
    xml += '</urlset>'
    return Response(xml, mimetype='application/xml')


# --- TRUELAYER OPEN BANKING ---
# Single source of truth for sandbox vs live: TRUELAYER_ENV on Render.
# Defaults to "sandbox" so an unset env var can never accidentally expose
# the live-looking "Connect your bank" flow to real users.
TRUELAYER_ENV           = os.environ.get("TRUELAYER_ENV", "sandbox").strip().lower()
TRUELAYER_LIVE          = TRUELAYER_ENV == "live"

TRUELAYER_CLIENT_ID     = os.environ.get("TRUELAYER_CLIENT_ID", "")
TRUELAYER_CLIENT_SECRET = os.environ.get("TRUELAYER_CLIENT_SECRET", "")
TRUELAYER_AUTH_URL      = "https://auth.truelayer.com" if TRUELAYER_LIVE else "https://auth.truelayer-sandbox.com"
TRUELAYER_API_URL       = "https://api.truelayer.com" if TRUELAYER_LIVE else "https://api.truelayer-sandbox.com"
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
    if not TRUELAYER_LIVE:
        return redirect(url_for("actions", msg="Bank connection is coming soon — not available yet."))
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
    if not TRUELAYER_LIVE:
        return redirect(url_for("actions", msg="Bank connection is coming soon — not available yet."))
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