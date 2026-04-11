# Spendara — Claude Code Context

## What the app is
Spendara is a personal finance web app at https://spendara.co.uk (launched 16 March 2026).
Users track account balances, log transactions, set recurring bills and income, and get a 90-day forecast of their finances.
There is a Free tier (3 accounts) and a Pro tier (£1.99/month, unlimited accounts) via Stripe.
GitHub: https://github.com/SanJulio/PPFS.git

## Tech stack
- **Backend**: Flask + Gunicorn on Render (free tier, 1 worker)
- **Database**: PostgreSQL on Render
- **Frontend**: Bootstrap 5, vanilla JS, Chart.js (forecast page only)
- **Email**: Brevo API (300/day free tier), domain spendara.co.uk authenticated
- **Payments**: Stripe (subscriptions)
- **Uptime**: UptimeRobot (5-min pings to keep free tier alive)

## Key technical decisions
- **Sessions**: Custom `PostgresSessionInterface` in `flask_sessions` table. Do NOT use connection pooling — it caused connection exhaustion crashes.
- **DB connections**: `get_db()` opens a fresh connection per request, `release_db()` closes it. Import pattern: `from database import get_db, USE_POSTGRES` inside route functions.
- **CSRF**: Manual implementation — `session['csrf_token']` checked on all POST routes.
- **Forecast**: 90-day single-pass simulation in `Tracker.py → simulate_balances_until()`. Results cached 5 minutes per user.
- **Snapshot API**: `/api/snapshot?days=N` — lightweight day-by-day simulation returning projected balances, income arriving, and bills due per account up to day N. Used by the Financial Position card on home.
- **Auth**: Flask-Login. Email verification required. Password reset via Brevo.
- **Analytics**: Self-hosted at `/admin/analytics` (no third-party trackers).

## Database tables (10)
`users`, `accounts`, `transactions`, `scheduled_expenses`, `income`, `savings_rules`, `future_events`, `flask_sessions`, `investments`, `investment_updates`

## Key files
- `app.py` — all routes (~2500 lines)
- `Tracker.py` — `simulate_balances_until()` and legacy CSV code (only the simulate function is used)
- `models.py` — SQLAlchemy-free model helpers
- `database.py` — `get_db()`, `release_db()`, `USE_POSTGRES` flag
- `templates/index.html` — home/dashboard (largest template)
- `templates/forecast.html` — 90-day chart + insights
- `templates/landing.html` — public landing page for unauthenticated visitors
- `templates/settings.html` — plan/billing, display prefs, danger zone
- `templates/transactions.html` — transaction list + category totals

## What was last worked on (April 2026)
- **Auto-apply scheduled items** — built a feature where recurring income/bills auto-post to accounts and the transaction log on their due date. Users see a banner ("X scheduled items ready to apply") with a Review & Apply modal. Settings > Display has a toggle to enable/disable and a payday cycle start day (1–28).
- **Budget cycle** — `calculate_monthly_spending()` uses a user-defined cycle start day (stored in `users` table as `budget_cycle_start`, default 1). Home page shows "Cycle: Apr 1 – Apr 30" style dates.
- **Profile panel** — slide-in panel from top-right avatar button: personal details, avatar picker, feedback form. Works across all pages via fixed header.
- **Calendar widget** — small date widget (month/day) in the top bar, always shows today's date.

## Known open issues (as of session end Apr 2026)
- VS Code JS linter shows errors in `index.html` for Jinja expressions inside `<script>` blocks (e.g. `{{ pending_items | tojson }}`). These are **false positives** — the linter doesn't understand Jinja. The app works fine in the browser. Add a `.claudeignore` if linter noise becomes a problem.
- The auto-apply modal "Review & Apply" was fixed to use `data-*` attributes on checkboxes (not JSON.parse on tojson — that broke due to HTML entity encoding). CSRF token moved to `<meta name="csrf-token">` in `<head>`.

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## What's next
- Continue any remaining polish on auto-apply feature if needed
- Landing page: still has Hero → Features → How it works → CTA → Footer; goal is to tighten further
