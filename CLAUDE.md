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
- **Financial Position card** on home page — replaces the old "Can I afford this?" card. Has a date slider (1–90 days) + manual date input that sync bidirectionally. Account filter pills. Per-account projection cards showing balance on date + net change. Income and bills mixed in chronological order inside each account card (green 💰 for income, red 📋 for bills). Integrated afford check with account dropdown at the bottom.
- **Landing page cleanup** — bigger logo (80px), added Log In button alongside Get Started Free in hero, removed "Real results from real users" benefits section and "Join others who are taking control" social proof section.
- **Login page** — logo bumped to 80px to match landing.

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## What's next (landing page focus)
The user wants to keep shortening the landing page so it almost fits on one screen. In progress:
- Landing page still has: Hero → Features (3 cards) → How it works (3 steps) → CTA section → Footer
- Goal: tighten it further, potentially collapsing or removing more sections
