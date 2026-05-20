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
- **CI**: GitHub Actions — runs `pytest` on every push (`.github/workflows/test.yml`)

## Key technical decisions
- **Sessions**: Custom `PostgresSessionInterface` in `flask_sessions` table. Do NOT use connection pooling — it caused connection exhaustion crashes.
- **DB connections**: `get_db()` opens a fresh connection per request, `release_db()` closes it. Import pattern: `from database import get_db, USE_POSTGRES` inside route functions.
- **CSRF**: Manual implementation — `session['csrf_token']` checked on all POST routes via a `before_request` hook that reads `request.form.get('csrf_token')`. JSON API routes that do their own CSRF check in the handler must be added to the `exempt` list in `check_csrf()`. Current exempt list: `['/login', '/register', '/stripe/webhook', '/auto-apply', '/mark-bill-paid', '/dismiss-auto-apply', '/api/income-preview']`. The `<meta name="csrf-token">` tag is in `<head>` on all pages; all forms have a `<input type="hidden" name="csrf_token">`.
- **Forecast**: 90-day single-pass simulation in `Tracker.py → simulate_balances_until()`. Results cached 5 minutes per user. Now uses `income_engine.get_payment_dates()` for income date calculation.
- **Snapshot API**: `/api/snapshot?days=N` — lightweight day-by-day simulation. Uses `income_engine.get_payment_dates()` to pre-compute income dates before the sim loop.
- **Auth**: Flask-Login. Email verification required. Password reset via Brevo.
- **Analytics**: Self-hosted at `/admin/analytics` (no third-party trackers).
- **Income engine**: All income date calculations go through `income_engine.py`. Legacy rows (`rule_type = NULL`) use the old day/weekly_day path with no weekend/BH adjustments. New rows use the full engine.

## Database tables (10)
`users`, `accounts`, `transactions`, `scheduled_expenses`, `income`, `savings_rules`, `future_events`, `flask_sessions`, `investments`, `investment_updates`

### `income` table — columns added May 2026
Five columns were added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db()` in `database.py`:
- `rule_type TEXT` — `NULL` (legacy), `'fixed_date'`, `'last_working_day'`, `'nth_weekday'`, `'relative_month_end'`
- `rule_config TEXT DEFAULT '{}'` — JSON string with rule-specific params (e.g. `{"day": 25}`, `{"nth": "last", "weekday": 4}`)
- `weekend_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `bank_holiday_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `first_payment_date TEXT` — ISO date string; anchor for fortnightly/4-weekly; shown as "next payment date" in the UI

## Key files
- `app.py` — all routes (~4700+ lines)
- `income_engine.py` — **NEW (May 2026)**: canonical payment date engine (see below)
- `Tracker.py` — `simulate_balances_until()` and legacy CSV code (only simulate function is used)
- `models.py` — SQLAlchemy-free model helpers
- `database.py` — `get_db()`, `release_db()`, `USE_POSTGRES` flag; `init_db()` runs migrations
- `templates/index.html` — home/dashboard (largest template)
- `templates/manage.html` — bills, income sources, savings rules, future events; Add/Edit Income modal lives here
- `templates/forecast.html` — 90-day chart + insights
- `templates/landing.html` — public landing page for unauthenticated visitors
- `templates/settings.html` — plan/billing, display prefs, danger zone
- `templates/transactions.html` — transaction list + category totals
- `tests/` — pytest suite (142 tests, all passing); `conftest.py` has SQLite schema matching production

## income_engine.py — complete reference

**Purpose**: single source of truth for "on which dates does this income row pay out?"

**Public API**:
```python
get_payment_dates(income: dict, from_date: date, to_date: date) -> list[date]
get_next_dates(income: dict, n: int = 3, from_date: date | None = None) -> list[date]
describe_rule(income: dict) -> str   # human-readable summary, e.g. "Last Friday of month"
```

**Frequencies**: `weekly`, `fortnightly`, `4-weekly`, `monthly`, `yearly`

**Monthly rule types** (`rule_type` column):
- `fixed_date` — `rule_config = {"day": N}` where N is 1–28
- `last_working_day` — last Mon–Fri of month, skipping bank holidays
- `nth_weekday` — `{"nth": "first"|"second"|"third"|"fourth"|"last", "weekday": 0–4}`
- `relative_month_end` — `{"offset": N, "direction": "before"|"after", "working_days_only": bool}`

**Yearly**: `rule_config = {"month": 1–12, "day": 1–31}`, `rule_type` should be set to any truthy value (convention: `"yearly"`)

**Fortnightly / 4-weekly**: anchored to `first_payment_date`; no `rule_type` needed

**Weekly**: uses `weekly_day` column (0=Mon … 4=Fri); no `rule_type` needed

**Weekend/BH adjustments**: applied after nominal date via `_apply_rules()`. Weekend rule applied first, then bank holiday rule. Bank holiday data fetched from `https://www.gov.uk/bank-holidays.json` (England & Wales), cached in module-level `_bh_cache` dict, refreshed once per calendar day.

**Backward compatibility**: if `rule_type` is `NULL`/falsy, the legacy path is taken — raw `day` or `weekly_day` columns, no weekend/BH adjustments. This preserves behaviour for all rows created before the May 2026 engine.

**Known edge**: `sorted(set(dates))` at the end deduplicates cases where two nominal dates adjust to the same calendar date.

## `/api/income-preview` endpoint

- **Route**: `POST /api/income-preview` (`@app.post`, `@login_required`)
- **Auth**: login required; CSRF checked manually from JSON body (`data.get("csrf_token")`)
- **CSRF exempt list**: must be in `check_csrf()` exempt list (it is — see above)
- **Request body** (JSON):
  ```json
  {
    "csrf_token": "...",
    "frequency": "monthly",
    "rule_type": "fixed_date",
    "rule_config": "{\"day\": 25}",
    "weekend_rule": "before",
    "bank_holiday_rule": "before",
    "weekly_day": 4,
    "first_payment_date": "2026-06-01",
    "day": 25,
    "from_date": "2026-05-20"
  }
  ```
- **Response**: `{"dates": ["2026-05-25", "2026-06-25", "2026-07-25"]}`
- **On error**: returns `{"dates": []}` with 200 (exception logged as warning)
- **Used by**: the live preview panel in the Add/Edit Income modal

## Add/Edit Income modal (templates/manage.html)

The income modal (`id="incomeModal"`) is a full-screen overlay with scrollable content. It handles both add and edit via `openAddIncomeModal()` / `openEditIncomeModal(id)`.

**Form fields and hidden inputs**:
- `incomeFreqValue` (hidden) — synced by `incomeFreqChanged(freq)`
- `incomeRuleType` (hidden) — synced by `buildIncomeRuleConfig()`
- `incomeRuleConfig` (hidden) — JSON string, synced by `buildIncomeRuleConfig()`
- `incomeWeekendRule` (hidden) — synced by `setWeekendRule(rule)`
- `incomeBHRule` (hidden) — synced by `setBHRule(rule)`

**Key JS functions**:
- `openAddIncomeModal()` / `openEditIncomeModal(id)` — open and populate modal
- `resetIncomeModal()` — reset all fields to defaults (monthly, fixed_date, day 25)
- `incomeFreqChanged(freq)` — shows/hides sections, updates hidden field, triggers preview
- `incomeRuleTypeChanged(ruleType)` — shows/hides fixed/nth/relative config divs
- `buildIncomeRuleConfig()` — reads all UI fields, sets `incomeRuleType` and `incomeRuleConfig` hidden inputs
- `incomeFieldChanged()` — calls `buildIncomeRuleConfig()` then `updateIncomePreview()`
- `updateIncomePreview()` — debounced (350ms) wrapper around `_doUpdateIncomePreview()`
- `_doUpdateIncomePreview()` — fetches `/api/income-preview`, renders next 3 dates with £ amount
- `togglePaymentRules()` — expand/collapse the weekend/BH adjustment section
- `setWeekendRule(rule)` / `setBHRule(rule)` — update hidden inputs and pill styling
- `_updateAdjSummary()` — updates the one-line summary "Weekends: X · Bank holidays: Y"
- `_incomeData` — JS variable containing `{{ income | tojson }}`, used by `openEditIncomeModal`

**Section visibility logic**:
- `incomeWeekdayRow` — shown for weekly, fortnightly, 4-weekly
- `incomeMonthlySection` — shown for monthly only
- `incomeYearlySection` — shown for yearly only
- `incomeAdjRulesSection` — shown for monthly and yearly; collapsed by default; contains `incomeAdjExpanded` toggle

**Preview panel** (`id="incomePreviewDates"`): shows "Calculating…" during fetch, then 3 date rows with right-aligned £ amount in green. Falls back to "Enter your details above to see upcoming dates" on any error.

## app.py routes affected by income engine

- `settings_add_income()` — stores `rule_type`, `rule_config`, `weekend_rule`, `bank_holiday_rule`, `first_payment_date`; derives `day` from `rule_config` for backward compat
- `settings_edit_income()` — same new fields on UPDATE
- `manage()` — adds `inc["description"] = income_engine.describe_rule(inc)` to each income row before rendering
- Forecast simulation loop — replaced manual date iteration with `income_engine.get_payment_dates()`
- `api_snapshot()` — pre-computes `snap_income_by_date` dict using `income_engine.get_payment_dates()` before the day loop
- `run_auto_apply_backfill()` — uses `income_engine.get_payment_dates()`; removed the old `if inc.get('day') is None: continue` guard
- `get_pending_auto_apply_items()` — uses `income_engine.get_payment_dates()`; same guard removed
- `home()` future income — queries `SELECT *` (all columns), uses `income_engine.get_payment_dates()` for upcoming month income

## What was last worked on (May 2026)

### Income recurrence engine (income_engine.py)
Built from scratch. Handles all frequency/rule combinations. Weekend and UK bank holiday adjustments. Backward-compatible legacy path for old rows. All existing routes (forecast, snapshot, auto-apply, home, manage) migrated to use it.

### Add/Edit Income modal UI polish
- **Frequency pills**: 3-column CSS grid instead of flex-wrap
- **Progressive disclosure**: weekend/BH adjustment rules collapsed behind "Customise payment rules ▼" toggle; summary line always visible
- **Label softening**: "How often?", "Which day?" replacing verbose variants
- **Live preview**: `min-height:64px`, shows "Calculating…" state, formats dates as "25 May 2026 — £2,000"
- **CSRF bug fixed**: `/api/income-preview` was missing from the `check_csrf` exempt list — every JSON POST was returning 403 before the route handler ran

### GitHub Actions CI
`.github/workflows/test.yml` added — runs `pytest` on every push to `main`.

### Test suite
`tests/conftest.py` income table schema updated with 5 new columns. 142 tests, all passing.

## Known open issues (as of session end May 2026)
- VS Code JS linter shows errors in `index.html` for Jinja expressions inside `<script>` blocks (e.g. `{{ pending_items | tojson }}`). These are **false positives** — the linter doesn't understand Jinja. The app works fine in the browser.
- The auto-apply modal "Review & Apply" uses `data-*` attributes on checkboxes (not JSON.parse on tojson — that broke due to HTML entity encoding). CSRF token is in `<meta name="csrf-token">` in `<head>`.
- Bank holiday fetch (`https://www.gov.uk/bank-holidays.json`) has a 5-second timeout and is cached daily. If it fails, an empty set is used (no BH adjustments). This is safe.

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## What's next
- Income modal is feature-complete and polished — no known remaining issues
- Consider adding a "next payment" column to the income table view in manage.html (using `describe_rule` + `get_next_dates`)
- Landing page: Hero → Features → How it works → CTA → Footer; goal is to tighten further
- Any remaining polish on auto-apply feature if needed
