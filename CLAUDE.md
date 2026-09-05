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

## Database access — staging vs production (read this before running any DB command)
- The local `.env`'s `DATABASE_URL` points at a **separate staging Postgres instance** on Render (`ppfs_staging`), not production. This is the default for all local development, debugging, and AI-assisted verification work.
- Staging is seeded with 5 synthetic users only: `test1@example.com` … `test5@example.com`, password `TestPass1!` for all. Two (`test3`, `test4`) have `is_pro = 1` with obviously-fake `stripe_customer_id` values (`cus_FAKE_STAGING_00x`) — no real billing is attached. No real user data of any kind lives in staging.
- Staging's schema was originally set up via a `pg_dump --schema-only` mirror of production. As of August 2026, `database.py`'s `init_db()` migration list has been audited against production's real schema (via structured `information_schema` comparison, not just textual dump diffing) and closed — every column, table, index, and foreign key that existed in production but not in `init_db()` (`user_id` on `accounts`/`transactions`/`scheduled_expenses`/`income`/`savings_rules`/`future_events`, `accounts.savings_rate`, `income.weekly_day`, `transactions.truelayer_tx_id`, `users.verified`/`verify_token`/`onboarding_dismissed`, the `balance_adjustments`/`bank_connections` tables, and 8 missing `idx_*_user_id` indexes) now has a proper migration. Running `init_db()` against a genuinely fresh database has been verified to reproduce production's schema exactly, with zero discrepancies. `tests/conftest.py`'s independent SQLite mirror was audited the same way and had two of its own gaps fixed (`accounts.savings_rate`, `transactions.truelayer_tx_id`) plus the two tables above added. `init_db()` is now the reliable source of truth again — no need to re-derive from a fresh `pg_dump` unless new drift is introduced in the future (e.g. another manual production change made outside a migration).
- **Production must never be the default.** If a task genuinely requires touching production (real user data, a real incident, a real billing question), its `DATABASE_URL` should be supplied fresh for that one task — pasted directly when needed, used deliberately and narrowly, and not written back into `.env` afterward.
- **For local UI/route verification that doesn't need real staging data** (e.g. checking a new template renders correctly, clicking through a new toggle), prefer an even lighter-weight option: a disposable local SQLite DB seeded with one throwaway user, using `tests/conftest.py`'s `_create_test_schema()` directly (see "Windows-only `%-d` strftime gotcha" below for the harness pattern used repeatedly this session). This avoids touching staging Postgres at all, so there's nothing to restore afterward — reserve real staging (`test1`–`test5`) for verification that specifically needs to run against production-like Postgres state, and always restore those users to their original seeded values afterward if you do.

## Database connection TLS: `PGSSLMODE=require`, not `verify-full` — this is deliberate, not an oversight
Production's `DATABASE_URL` uses Render's **internal** hostname (`dpg-d6m123a4d50c73cjavc0-a`, no domain suffix) for free, low-latency private-network routing to the database. The connection is encrypted (TLS 1.3, enforced server-side — confirmed Render's Postgres rejects `sslmode=disable` outright) via `PGSSLMODE=require`, set as a Render environment variable. It does **not** perform full certificate verification (`sslmode=verify-full`).

**This was tried in August 2026 and broke production** (every DB connection failed with `SSL error: certificate verify failed`) because it was verified against the wrong target — the *external* hostname (`dpg-d6m123a4d50c73cjavc0-a.frankfurt-postgres.render.com`), which is publicly reachable and was mistaken for a valid stand-in during testing. It was reverted immediately once confirmed. Investigating properly afterward established that **`verify-full` cannot work against the internal hostname, structurally, not just as a testing oversight**:
- The internal hostname doesn't resolve at all outside Render's private network — there's no way to open a direct connection to it from anywhere else, so it can never be tested from a normal dev machine.
- Production's external hostname's certificate was inspected directly and found byte-for-byte identical to staging's (same CN `aws-eu-central-1-1-postgres.render.com`, same SAN list, same issuer/dates) — confirming Render issues **one shared regional wildcard certificate** across all Postgres instances in that cluster, not a per-instance certificate.
- PostgreSQL's TLS certificate is a single, static, server-wide config value (`ssl_cert_file`) — there's no per-connection/SNI-based certificate switching at the Postgres protocol level the way an HTTP reverse proxy might do. Whatever certificate the backend presents is the same regardless of which hostname/network path was used to reach it.
- That shared certificate's SAN list (`*.aws-eu-central-1-1-postgres.render.com`, `*.frankfurt-postgres.render.com`, and their `replica-cyan` equivalents) can never include a bare, private-network-only name like `dpg-d6m123a4d50c73cjavc0-a` — a publicly-trusted CA (Let's Encrypt) fundamentally cannot issue a certificate for a hostname that isn't publicly resolvable.

So `verify-full` against the internal hostname will always fail on hostname mismatch — not a bug, not a misconfiguration, just incompatible by design. The only way to get full verification would be switching `DATABASE_URL` to the external hostname (confirmed working there), which trades away Render's free/low-latency internal network path for one that counts against bandwidth — a real, ongoing cost for a £1.99/month product, in exchange for closing a MITM-protection gap that's already low-risk on a private network (vs. a typical public-internet DB connection).

**Decision: stay on `PGSSLMODE=require` for now.** This is a deliberately parked decision, not a forgotten one — revisit it specifically if pursuing FCA authorisation, or if formal security due diligence ever requires full certificate verification regardless of the cost/latency tradeoff. If revisited, the fix is switching to the external hostname (already proven to work with `verify-full`) — not attempting it again against the internal hostname.

## Key technical decisions
- **Sessions**: Custom `PostgresSessionInterface` in `flask_sessions` table. Do NOT use connection pooling — it caused connection exhaustion crashes.
- **DB connections**: `get_db()` opens a fresh connection per request, `release_db()` closes it. Import pattern: `from database import get_db, USE_POSTGRES` inside route functions.
- **CSRF**: Manual implementation — `session['csrf_token']` checked on all POST routes via a `before_request` hook that reads `request.form.get('csrf_token')`. JSON API routes that do their own CSRF check in the handler must be added to the `exempt` list in `check_csrf()`. Current exempt list: `['/login', '/register', '/stripe/webhook', '/auto-apply', '/mark-bill-paid', '/dismiss-auto-apply', '/api/income-preview', '/api/edit-pending-item', '/api/edit-cycle-item', '/api/set-primary-income', '/my-money/setup/dismiss']`. The `<meta name="csrf-token">` tag is in `<head>` on all pages; all forms have a `<input type="hidden" name="csrf_token">`.
- **Forecast**: 90-day single-pass simulation in `Tracker.py → simulate_balances_until()`. Results cached 5 minutes per user. Now uses `income_engine.get_payment_dates()` for income date calculation. **Note**: `simulate_balances_until()` is legacy code with a narrow, incomplete implementation (only handles `frequency='weekly'` income via a raw SQL query — it does not go through `income_engine.py` at all). It is only reachable via the standalone `/afford` POST route, which as of August 2026 is **dead code**: no template links to it (the "Can I Afford This" UI on both the landing page and Home page instead calls `/api/snapshot`). Left as-is rather than removed or fixed — out of scope for whatever feature you're working on unless the brief specifically asks about `/afford`.
- **Snapshot API**: `/api/snapshot?days=N` — lightweight day-by-day simulation, the one actually used by the "Can I Afford This" UI and the Home page's Future Balances view. Uses `income_engine.get_payment_dates()` to pre-compute income dates before the sim loop, plus (since August 2026) a separate `spread_rows` pre-pass for self-employed spread-distribution income (see below).
- **Auth**: Flask-Login. Email verification required. Password reset via Brevo.
- **Analytics**: Self-hosted at `/admin/analytics` (no third-party trackers).
- **Income engine**: All income date calculations go through `income_engine.py`. Legacy rows (`rule_type = NULL`) use the old day/weekly_day path with no weekend/BH adjustments. New rows use the full engine.
- **Cycle engine**: `cycle_engine.get_cycle(user_id, today=None)` is the single source of truth for a user's current budget period. Returns `display_start`, `display_end`, `safe_boundary`, `mode_used`, `primary_source_name`. All fallbacks are silent — always returns a valid dict. Never modify `income_engine.py` from cycle engine changes. Self-employed users (see below) are simply forced into `cycle_mode='manual'` at setup — `cycle_engine.py` itself required zero changes for that feature; this is a deliberate architectural pattern worth preserving for future features that need cycle-adjacent behaviour.
- **Account locking**: accounts beyond a Free-tier user's 3-account allowance (from a past Pro subscription) are locked, not deleted, and excluded from most calculations. Full reference below.
- **Self-employed income averaging**: an alternative to fixed-payday income for irregular earners, living entirely inside the existing `income` table's `rule_type`/`rule_config` pattern. Full reference below.
- **Spending Alert Threshold**: an optional, user-defined low-balance warning, separate from Safe to Spend. Full reference below.
- **Date display**: every full calendar date shown to a user (has a day, month, *and* year) renders as UK `DD/MM/YYYY` — see "UK date formatting" below.

## Windows-only `%-d` strftime gotcha (verification harnesses only — never touches the actual app)
**Currently moot** (August 2026): the app-wide switch to UK `DD/MM/YYYY` date formatting (see "UK date formatting" below) replaced every `%-d`-based strftime call with `%d`/`%m`/`%Y` — all standard, cross-platform format codes with no Windows/Linux discrepancy. `grep -rn "%-d"` across `templates/*.html`, `app.py`, `cycle_engine.py`, `income_engine.py` now returns nothing. The section below is kept for history and in case a future written-month-style date format (`%B`, `%b`) gets reintroduced somewhere and hits this again — the workaround pattern still applies if so.

Some templates used `strftime('%-d %B %Y')` (Linux/macOS-only day-of-month format, no leading zero) — e.g. `templates/settings.html`'s "Next cycle starts" line, fed by `cycle_engine.get_next_cycle_start()`. This works fine on production (Render runs Linux) but **crashes with `ValueError: Invalid format string` on Windows** (the Windows CRT only supports `%#d` for the same effect). This has been hit multiple times during local verification work on this Windows dev machine.
**Not a real bug — do not "fix" it in the app code.** The correct move when it blocks local browser verification is to monkeypatch it away in the disposable verification harness only:
```python
class _WinSafeDate(_dt.date):
    def strftime(self, fmt):
        return super().strftime(fmt.replace("%-d", "%#d"))

_orig_get_next_cycle_start = cycle_engine.get_next_cycle_start
def _wrapped(*a, **kw):
    d = _orig_get_next_cycle_start(*a, **kw)
    return _WinSafeDate(d.year, d.month, d.day) if d else d
cycle_engine.get_next_cycle_start = _wrapped
```
This pattern has been reused for every local Playwright-based verification pass this session (a disposable Flask process on a throwaway port, seeded via `tests/conftest.py`'s `_create_test_schema()` against a temp SQLite file, `SECRET_KEY` set inline, rate limiter disabled) — it's the standard way to spin up a quick, safe, non-staging-touching local check.

## UK date formatting (August 2026)

**Rule**: every **full calendar date** shown to a user — has a day, a month, *and* a year, identifying one specific point in time — renders as UK-order `DD/MM/YYYY` (e.g. `25/12/2026`), not ISO (`2026-12-25`), not US order, not a written month name with a year.

**Explicitly out of scope, left as-is — these are contextual labels, not full dates**: short day+month labels with no year (chart x-axis ticks, cycle period headers like "1 May – 31 May", forecast tooltips like "24 Aug", `income_engine.describe_rule()` output) stay exactly as they were. Adding a year to those would be redundant/cluttered in tight UI space, and this was an explicit scope decision, not an oversight — don't "fix" them into `DD/MM/YYYY` as a drive-by. Also untouched: any `<input type="date">` `value` attribute (must stay ISO — that's the HTML5 spec, the browser handles locale display on its own) and any `?date=YYYY-MM-DD` URL query parameter (internal routing, never shown raw to the user).

**Server-side (Jinja)**: `app.py` registers a template filter, `dateformat`, that converts an ISO `YYYY-MM-DD` string to `DD/MM/YYYY`:
```python
@app.template_filter('dateformat')
def dateformat_filter(value):
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value), '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        return value
```
Applied via `{{ some_iso_date | dateformat }}` everywhere a full date is rendered directly from a DB column or computed ISO string: `transactions.html`/`actions.html` (transaction date), `admin_analytics.html` (daily rows, oldest-record date), `manage.html` (Future Events date, Investments date, Goals tab's `target_date`/`projected_date`/contribution `date`), `flow.html` (investment initial date, value-history update dates), `import.html` (CSV preview date column). A Python `date`/`datetime` object rendered directly via `.strftime()` in a template (not through this filter) uses `%d/%m/%Y` the same way — e.g. `settings.html`'s "Next cycle starts" line (`next_cycle_start.strftime('%d/%m/%Y')`) and the `/api/snapshot` route's `min_balance_date`/`date` response fields (`app.py`, built via `.strftime('%d/%m/%Y')` rather than the old `f"{d.day} {d.strftime('%b %Y')}"` pattern).

**Client-side (JS)**: no shared JS file exists (see "No shared base template" above — every template is standalone), so each template that needs to format an ISO string into `DD/MM/YYYY` on the client carries its own small helper rather than importing a common one. The established shape (`templates/manage.html`'s `_fmtUKDate(iso)`, and the equivalent inline function in `templates/index.html`'s transaction detail/edit popups):
```js
function _fmtUKDate(iso) {
  if (!iso) return '';
  var d = new Date(iso + 'T00:00:00');
  var dd = String(d.getDate()).padStart(2, '0');
  var mm = String(d.getMonth() + 1).padStart(2, '0');
  return dd + '/' + mm + '/' + d.getFullYear();
}
```
Deliberately a manual zero-padded build rather than `date.toLocaleDateString('en-GB')` (which *also* happens to produce `DD/MM/YYYY` for the default en-GB numeric format) — the manual version has no dependency on the browser's locale data or Intl support and is trivially testable by string-matching the output. Applied to: `manage.html`'s Income modal live-preview dates and the Goal Contribution slider's live preview (`_updateGoalCommitPreview`, formats the `/api/goal-commitment-preview` response's `projected_date`); `index.html`'s transaction detail popup and its read-only edit-form date field (both derive from a transaction's `iso` field). Chart-tick/short-label JS (`forecast.html`'s many `toLocaleDateString('en-GB', {day, month})` calls with no `year` key, `calendar.html`'s day-view header) is unaffected — those were already UK day-before-month order and don't carry a year to reformat.

**A pre-existing inconsistency noted but deliberately not touched**: `templates/index.html`'s small inline "Bills left" dropdown (`#tile-bills`) shows a future event's `due_date` as a raw, unfiltered ISO string in its Jinja-rendered initial page load, while the exact same data in the bigger "Bills remaining" bottom-sheet modal (`openBillBreakdown()`) and the AJAX re-render path both go through the established `_fmtDs()` short-label helper (day + short month, no year — matching the "short label" convention above, since a bill's due date is always within the current, implied-year cycle). This is a real, separate inconsistency (the small dropdown should probably use the same short-label style, not gain a full year) — flagged here rather than fixed as a drive-by, since it's a label-shortening question distinct from the DD/MM/YYYY full-date work this section documents.

## Database tables (13)
`users`, `accounts`, `transactions`, `scheduled_expenses`, `income`, `savings_rules`, `future_events`, `flask_sessions`, `investments`, `investment_updates`, `cycle_overrides`, `goals`, `goal_contributions`

### `income` table — columns added May 2026
Five columns were added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db()` in `database.py`:
- `rule_type TEXT` — `NULL` (legacy), `'fixed_date'`, `'last_working_day'`, `'nth_weekday'`, `'relative_month_end'`, and (since August 2026) `'self_employed_average'` — see the Employed/Self-employed section below
- `rule_config TEXT DEFAULT '{}'` — JSON string with rule-specific params (e.g. `{"day": 25}`, `{"nth": "last", "weekday": 4}`)
- `weekend_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `bank_holiday_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `first_payment_date TEXT` — ISO date string; anchor for fortnightly/4-weekly; shown as "next payment date" in the UI

### `users` table — columns added since May 2026
- `cycle_mode TEXT NOT NULL DEFAULT 'manual'` — `'automatic'` or `'manual'` (May 2026, see cycle_engine.py)
- `budget_cycle_start INTEGER NOT NULL DEFAULT 1` (pre-existing)
- `employment_type TEXT NOT NULL DEFAULT 'employed'` — `'employed'` or `'self_employed'` (August 2026)
- `alert_mode TEXT DEFAULT NULL` — `NULL` (no alert set up), `'overall'`, or `'per_account'` (August 2026)
- `alert_overall_threshold REAL DEFAULT NULL` — used when `alert_mode='overall'` (August 2026)

### `accounts` table — columns added since May 2026
- `is_locked INTEGER NOT NULL DEFAULT 0` — Pro-to-Free downgrade lock (see Account Locking below)
- `savings_rate NUMERIC(5,2) DEFAULT 0`, `savings_type TEXT` (pre-existing)
- `alert_threshold REAL DEFAULT NULL` — per-account low-balance figure, only read when the owning user's `alert_mode='per_account'` (August 2026)

## Key files
- `app.py` — all routes (~6000+ lines)
- `income_engine.py` — canonical payment date engine (see below)
- `cycle_engine.py` — budget cycle calculator (see below)
- `Tracker.py` — `simulate_balances_until()` (legacy, only reachable via the dead `/afford` route) and legacy CSV code
- `models.py` — SQLAlchemy-free model helpers (`get_active_accounts`, `add_transaction`, `update_account_balance`, etc.)
- `database.py` — `get_db()`, `release_db()`, `USE_POSTGRES` flag; `init_db()` runs migrations
- `templates/index.html` — home/dashboard (largest template); Safe-to-Spend banner and Spending Alert Threshold banner both live here
- `templates/manage.html` — bills, income sources, savings rules, future events; Add/Edit Income modal and the employed/self-employed onboarding question both live here
- `templates/forecast.html` — 90-day chart + insights
- `templates/landing.html` — public landing page for unauthenticated visitors (unaffected by the self-employed feature — see below)
- `templates/settings.html` — plan/billing, display prefs, danger zone; Budget Cycle card, Income Averaging card (self-employed only), and Spending Alert Threshold card all live in the "Display" tab
- `templates/transactions.html` — transaction list + category totals
- `templates/actions.html`, `templates/calendar.html`, `templates/flow.html`, `templates/import.html` — other internal logged-in pages sharing the same `.top-bar` header markup as the pages above
- `tests/` — pytest suite (485 tests, all passing); `conftest.py` has SQLite schema matching production, including all August 2026 columns
  - `tests/test_account_locking.py`, `tests/test_locked_account_exclusion.py` — account locking
  - `tests/test_self_employed.py` — employed/self-employed income system (44 tests)
  - `tests/test_spending_alerts.py` — Spending Alert Threshold (34 tests)
  - `tests/test_goals.py`, `tests/test_home_goals.py`, `tests/test_goal_pace_projection.py`, `tests/test_goal_fallback_pace.py` — savings & debt repayment goal tracking (39 + 15 + 30 + 18 tests)

## No shared base template
There is **no** `{% extends %}` anywhere in this codebase — every template is fully standalone, and the internal app's header (`<div class="top-bar">` containing the logo + right-side calendar/avatar icons) is byte-identical, copy-pasted across 9 templates: `index.html`, `forecast.html`, `actions.html`, `transactions.html`, `manage.html`, `settings.html`, `import.html`, `flow.html`, `calendar.html`. Any change to the shared header (like the "BETA" pill added next to the logo in August 2026 — purple bordered pill, matching `landing.html`'s nav badge but adapted to a light background) must be applied to all 9 individually. Confirmed via `grep` that the exact `<img src="{{ url_for('static', filename='6000-logo.png') }}" ...>` line is identical across all of them, which makes this safe to do as a straightforward find-and-wrap in each file.

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
- `self_employed_average` — (August 2026) lump-sum anchor date for a self-employed user's averaged income; `rule_config["day"]` is the user's manual-cycle cycle-start day, **not** a real payday. Only used for lump-sum distribution — spread-evenly distribution bypasses this engine entirely (see below). `describe_rule()` returns an honest label distinguishing manual/auto and lump/spread, e.g. "Averaged income (manual), on the 5th of your cycle" or "...spread across your cycle".

**Yearly**: `rule_config = {"month": 1–12, "day": 1–31}`, `rule_type` should be set to any truthy value (convention: `"yearly"`)

**Fortnightly / 4-weekly**: anchored to `first_payment_date`; no `rule_type` needed

**Weekly**: uses `weekly_day` column (0=Mon … 4=Fri); no `rule_type` needed

**Weekend/BH adjustments**: applied after nominal date via `_apply_rules()`. Weekend rule applied first, then bank holiday rule. Bank holiday data fetched from `https://www.gov.uk/bank-holidays.json` (England & Wales), cached in module-level `_bh_cache` dict, refreshed once per calendar day. Fetch has a 5-second timeout; on failure an empty set is used (no BH adjustments) — this is safe and silent.

**Backward compatibility**: if `rule_type` is `NULL`/falsy, the legacy path is taken — raw `day` or `weekly_day` columns, no weekend/BH adjustments. This preserves behaviour for all rows created before the May 2026 engine.

**Known edge**: `sorted(set(dates))` at the end deduplicates cases where two nominal dates adjust to the same calendar date.

**Architectural rule that has held for two more features now**: `income_engine.py` is never modified to support a downstream feature's needs — cycle_engine.py and the self-employed averaging feature both consume it as a read-only library. Where a feature's needs (like a continuous daily accrual instead of discrete payment dates) genuinely don't fit the engine's contract, the established answer is to build a small, separate mechanism alongside it (see "Spread-evenly bypasses income_engine entirely" below) rather than bending the engine's API to fit a shape it wasn't designed for.

## cycle_engine.py — complete reference

**Purpose**: single source of truth for a user's current budget cycle period.

**Public API**:
```python
get_cycle(user_id: int, today: date | None = None) -> dict
get_next_cycle_start(user_id: int, today: date | None = None) -> date | None
```

**Returned dict keys** (`get_cycle`):
- `display_start` — start of the Financial Overview display period
- `display_end` — end of the display period (≥30 days for weekly users)
- `safe_boundary` — day before next actual payday; governs safe-to-spend deduction only
- `mode_used` — `'automatic'` or `'manual'`
- `primary_source_name` — name of the income source used in automatic mode, or `None`

**Critical distinction**: `display_end` governs what the Financial Overview shows. `safe_boundary` governs which bills are deducted from safe-to-spend. These are different for weekly/fortnightly users — never conflate them.

**Modes**:
- `automatic` — powered by the user's primary income source (`is_primary=1` on `income` table). `display_start` = last payday; `safe_boundary` = next payday - 1.
- `manual` — uses `budget_cycle_start` day from `users` table. Replicates original `get_cycle_dates()` behaviour exactly. Self-employed users are always in this mode (forced at setup, since there's no real payday to anchor an automatic cycle to) — the manual-mode code path itself required **zero changes** to support them.

**Fallback chain** (all silent, never visible to user):
- automatic → no primary source → fall back to manual
- automatic → no past payment dates → fall back to manual
- automatic → any exception → fall back to manual
- manual → no day stored → use day 1 of current month

**Weekly extension**: `display_end` is extended to at least `display_start + 29 days` for high-frequency income (weekly/fortnightly). `safe_boundary` stays as the next real payday - 1.

**Users table columns** (added May 2026): see "users table" schema section above.

**Income table column** (added May 2026):
- `is_primary INTEGER NOT NULL DEFAULT 0` — exactly one income row per user should have this set to 1 for automatic mode. A self-employed user's single `self_employed_average` income row is also marked `is_primary=1` at setup, though it has no practical effect since they're always in manual mode — kept for consistency with the rest of the income-row lifecycle (e.g. `manage()`'s auto-star-the-sole-source logic).

**cycle_overrides table** (added May 2026):
- `(user_id, type, source_id, date, amount)` with `UNIQUE(user_id, type, source_id, date)`
- `type` is `'income'` or `'bill'`
- Allows per-occurrence amount overrides without touching the recurring schedule
- Loaded in `calculate_monthly_spending()` and applied per item

**Where get_cycle() is called**:
- `home()` — replaces old `get_budget_cycle_start()` + `get_cycle_dates()` calls
- `settings()` — for the Budget Cycle card display and next cycle date
- `get_next_cycle_start()` is called by `settings()` for "Next cycle starts" display
- `_self_employed_cycle_length_days()` (app.py) — calls `get_cycle()` once to derive a self-employed user's cycle length in days, used to turn an averaged monthly amount into a flat daily accrual for spread-evenly distribution (see below)

**Rule**: income_engine.py is never modified to support cycle_engine. Cycle engine consumes income_engine as a library.

**Tests**: `tests/test_cycle_engine.py` — 9 tests covering manual, automatic monthly, automatic weekly, and all fallback paths. Uses `unittest.mock.patch` on `_get_db_and_cursor`, `_release`, `_use_postgres` (no Flask app context needed).

## Account Locking (Pro-to-Free downgrade) — complete reference

**Purpose**: when a user who was ever Pro downgrades to Free while over the Free tier's 3-account limit, excess accounts are **locked, not deleted** — visible with all data intact, but frozen/read-only until the user re-upgrades, at which point everything unlocks exactly as it was.

**Schema**: `accounts.is_locked INTEGER NOT NULL DEFAULT 0`.

**Core function** (`app.py`):
```python
def sync_account_locks(user_id, is_pro):
```
- `is_pro=True` → unlocks everything (`is_locked=0` for all the user's accounts)
- `is_pro=False` → the oldest 3 **active** accounts by `id` (i.e. creation order — `accounts` has no `created_at` column) stay unlocked; every other active account is locked
- Always busts the forecast cache afterward (`bust_forecast_cache(user_id)`) — a cached forecast computed before the transition would show the wrong set of accounts for up to the cache TTL otherwise

**Trigger points** — all three are Stripe webhook events in `app.py`'s `/stripe/webhook` handler:
- `checkout.session.completed` → `sync_account_locks(user_id, True)` (upgrade, unlock all)
- `customer.subscription.deleted` → `sync_account_locks(user_id, False)` (subscription fully cancelled, lock excess)
- `customer.subscription.updated` → `sync_account_locks(user_id, bool(new_is_pro))` where `new_is_pro` is derived from Stripe's `status` field (`past_due`/`unpaid`/`canceled` → lock; `active`/`trialing` → unlock) — this is how a payment-failure dunning cycle resolving, or a subscription recovering, gets reflected

**Read helpers**:
```python
_is_account_locked(user_id, account_name) -> bool
_is_account_locked_by_id(user_id, account_id) -> bool
```
Used to **block new activity** against a locked account — called in `bills_pay()`, `income_pay()`, `add_expense()`, `add_income()`, `quick_add()`, `quick_adjust()`, `transfer()` (checked on both `from_account` and `to_account`), `settings_edit_account()`, `import_csv()`, `import_confirm()`.

**Exclusion from calculations** — a locked account's balance is frozen at whatever it was when locked, so it can't be trusted as current. It is deliberately excluded from:
- `calculate_financial_overview()` — spending/savings totals, safe-to-spend, future bills/income lists
- The 90-day forecast and `/api/snapshot` — locked accounts are filtered out of `accounts_rows` before simulation even starts
- `/afford` (the legacy dead-code route) and the live `/api/snapshot`-based "Can I Afford This" — locked accounts are excluded from the candidate spending-account list
- `get_pending_auto_apply_items()` / `run_auto_apply_backfill()` — via `locked_names` set, bills/income tied to a locked account are skipped
- **Spending Alert Threshold** (August 2026, see below) — excluded from both overall and per-account modes, same reasoning
- The Free-tier 3-account limit check in `settings_add_account()` — locked accounts don't count against the limit (they're not usable, so they shouldn't eat into the allowance of accounts a Free user can actually use)

**UI treatment** (`templates/manage.html`): locked account rows render at `opacity:0.55` with a "🔒 Locked" badge; every account `<select>` across the app disables the locked option and appends "(Locked)" or "(Locked — upgrade to Pro)" to its label, so users can see it but can't select it for new bills/income/transactions.

**Tests**: `tests/test_account_locking.py` (lock/unlock transitions, oldest-3-kept logic, webhook trigger paths), `tests/test_locked_account_exclusion.py` (exclusion from overview/forecast/snapshot/auto-apply).

## Founder/admin Pro override — complete reference (August 2026)

**Purpose**: grants Pro access to a specific account without going through Stripe at all — for founder/testing accounts only, never exposed or advertised to real users.

**Route**: `GET /admin/grant-pro?email=someone@example.com` (`admin_grant_pro()`, app.py) — gated by the exact same admin check as `/admin/analytics`: `current_user.id == ADMIN_USER_ID` and `session.get("admin_unlocked") == ADMIN_SECRET` (set by first visiting `/admin/unlock?secret=...`). Looks up the user by email (case-insensitive), sets `users.is_pro = 1`, then calls the existing `sync_account_locks(user_id, True)` — the same unlock-everything step the real Stripe `checkout.session.completed` handler triggers, so any accounts already locked from a prior Free-tier state get unlocked too, not just the flag flipped.

**Deliberately reuses the exact same `users.is_pro` column and `user_is_pro()` read path every other Pro check in the app already uses** — routes, templates, `sync_account_locks()` — rather than a parallel "is this a founder account" flag or allow-list bolted on elsewhere. One engine, same principle as everything else in this codebase: a founder-overridden account is indistinguishable from a real Pro subscriber to every consumer of `user_is_pro()`.

**Never touches Stripe or `stripe_customer_id`** — no fake subscription or customer is created. Because every `is_pro = 0` write in the Stripe webhook handlers (`checkout.session.completed`/`customer.subscription.deleted`/`customer.subscription.updated`) is scoped by `WHERE stripe_customer_id = ...`, a founder-overridden account (whose `stripe_customer_id` stays `NULL`) can never be touched by a webhook — the grant persists until manually changed, immune to any real-world Stripe event.

**No revoke route built** — out of scope for "a way to grant," and this is founder/testing-only; if ever needed, flip `is_pro` back directly.

**Tests**: `tests/test_admin_grant_pro.py` (11 tests) — the admin gate (wrong user, secret not unlocked this session, `ADMIN_SECRET` unconfigured, mismatched session secret all 404), granting by email (case-insensitive), confirms `stripe_customer_id` is never written, unknown email 404s, missing `?email=` 400s, the downstream `user_is_pro()` check reflects the grant (not just a raw DB flag nobody reads), and previously-locked accounts get unlocked via `sync_account_locks()`.

## PWA service worker — complete reference (August 2026)

**Purpose**: added purely to satisfy PWA installability/reliability requirements flagged by PWABuilder for app store packaging. Deliberately narrow — a basic cache-first strategy for a handful of small static assets, not an offline-first rebuild. No offline transaction data, no background sync, nothing ambitious.

**Served at `/sw.js`, not `/static/sw.js`** — a service worker's maximum allowed scope is the directory containing the script it was registered from, so a script served from `/static/sw.js` defaults to `/static/`-only scope (extending it further requires a `Service-Worker-Allowed` response header). A dedicated route, `GET /sw.js` (`service_worker()`, app.py, no `@login_required` since the public landing page registers it too), serves the same file from the site root instead, so its default scope is `/` — the whole site — with no extra header handling needed. `Cache-Control` on this route is whatever the existing global `add_no_cache_headers()` `after_request` hook already stamps on every response (`no-store, no-cache, must-revalidate, max-age=0`) — no explicit override needed, and that's actually the desired behaviour anyway (the browser always re-checks for an updated worker script rather than serving a stale cached copy of `sw.js` itself). The file still also happens to be reachable at the old `/static/sw.js` path too (Flask's default static route didn't go anywhere), but nothing registers it there any more.

**Registration**: the same one-line snippet — `<script>if ("serviceWorker" in navigator) { navigator.serviceWorker.register("/sw.js"); }</script>` — on all 9 logged-in app templates sharing the `.top-bar` header (see "No shared base template" above) *and* `templates/landing.html`, which didn't register a service worker at all before this. Since there's no shared base template, each template carries this line independently, same as every other cross-template pattern in this codebase.

**Caching strategy — explicitly scoped to same-origin `/static/*` GET requests only, nothing else.** `static/sw.js`'s `fetch` handler checks, in order: `req.method !== 'GET'` → bail (never intercept a POST — no API call, form submission, or webhook is ever a GET), `url.origin !== self.location.origin` → bail (never try to cache cross-origin CDN scripts, avoiding CSP/opaque-response complications), `url.pathname.indexOf('/static/') !== 0` → bail. Only a request passing all three gets the cache-first treatment (check Cache Storage first, fall through to network and populate the cache on a 200). This means every page navigation, every `/api/*` call, `/login`, `/settings/*`, `/stripe/webhook`, `/admin/*` — literally everything outside `/static/` — is left completely untouched, with no `respondWith()` call at all, so the browser handles it exactly as if the service worker didn't exist. Confirmed via `grep` that no route in the app is nested under a `/static/*` path, so this exclusion is total, not partial.

**Precached on install** (`PRECACHE_URLS`): the small, cheap assets referenced in every page's `<head>` — logo, favicons, icon-192/512, `manifest.json`. Deliberately excludes the larger static files (the landing page's demo video, screenshots) so `install` doesn't get slow; those are still cacheable opportunistically by the same `fetch` handler if a page ever actually requests one, just not eagerly warmed.

**Note**: the Cache Storage API service workers use for `caches.open()`/`caches.match()` is completely separate from the browser's native HTTP cache, so the app-wide `no-store` `Cache-Control` header (set on every response, including `/static/*` assets, to stop logged-out users seeing cached authenticated pages via the back button) doesn't prevent this service worker's own caching from working — the two mechanisms don't interact.

**Tests**: `tests/test_service_worker.py` (12 tests) — the route serves at `/sw.js` without requiring login, correct content-type, content matches the static file exactly; content-based checks (the same technique used elsewhere in this codebase for JS-only logic that can't be exercised from pytest) confirming the method/origin/path guard clauses are present in the shipped script, that `install`/`activate`/`fetch` handlers all exist, and that `PRECACHE_URLS` contains only `/static/*` paths; and registration checks across the landing page and all 9 app templates confirming each uses the new root-scoped `/sw.js` and none still reference the old `/static/sw.js`.

## Employed / Self-employed income system — complete reference (August 2026)

**Purpose**: self-employed users have irregular income with no fixed payday. Instead of forcing them into the existing fixed-payday income model, they get an **averaged** income figure (manual or automatically calculated from logged transactions) that can appear in their cycle either as one lump sum or spread evenly day-by-day.

**Schema** — deliberately reuses existing patterns rather than adding new tables:
- `users.employment_type` — `'employed'` (default, zero behaviour change) or `'self_employed'`
- One `income` row per self-employed user with `rule_type='self_employed_average'`, `is_primary=1`, and all self-employed-specific state packed into the existing `rule_config` JSON column: `{"mode": "manual"|"auto", "window_months": 1|3|6, "manual_amount": float, "distribution": "lump"|"spread", "day": int}`. No new income-table columns were needed — this follows the same `rule_type`/`rule_config` idiom already used for `fixed_date`/`nth_weekday`/etc.

**Onboarding placement — a deliberate choice, not the obvious one**: the "Are you employed or self-employed?" question was added to the **post-login "My Money" setup checklist** (`manage.html`'s Income tab empty state, `#employmentTypeChoice`), gated behind `get_my_money_setup()`, with a "New · Beta" badge on the self-employed option. It was **not** added to the pre-signup landing/welcome flow (`landing.html`, `welcome.html`, the `payday_map`/Google OAuth seed flow) — that flow was deliberately left completely untouched. If extending this feature, don't assume the question lives in the signup flow; it doesn't.

**Setup route**: `POST /settings/setup-self-employed` (`settings_setup_self_employed()`, app.py) — one-time initial setup. Sets `employment_type='self_employed'`, `cycle_mode='manual'`, `budget_cycle_start=<chosen day>`, and inserts the `self_employed_average` income row described above with `distribution` defaulted to `'lump'` and `window_months` defaulted to `3`.

**Settings card** ("Income Averaging", `templates/settings.html`'s Display tab, self-employed users only): Manual/Automatic toggle, averaging window select (1/3/6 months), Lump sum/Spread evenly toggle — mirrors the existing `setCycleMode()`/`setAvgMode()` JS idiom exactly. **Switchable at any time** via `POST /settings/save-income-averaging` (`settings_save_income_averaging()`), which only ever updates the `rule_config` JSON — it never touches the stored `income.amount` column. This is the source of the single most important invariant in this feature:

**`income.amount` is never trustworthy for a `self_employed_average` row — always resolve it live.** Every consumer must call `_resolve_income_rows(income_rows, user_id)` (app.py) instead of reading `.amount` directly off a raw DB row. This helper:
- Leaves every non-`self_employed_average` row completely unchanged (purely additive for self-employed users, zero effect on everyone else)
- For a `self_employed_average` row: if `mode='manual'`, resolves `amount` from `rule_config["manual_amount"]`; if `mode='auto'`, resolves it via `_compute_automatic_income_average(user_id, window_months)`
- Attaches `_distribution` (`'lump'` or `'spread'`) to the row for downstream branching

**Automatic averaging** (`_compute_automatic_income_average(user_id, window_months)`): `SUM(transactions.amount) WHERE type='income' AND date >= (today - 30*window_months days)`, divided by `window_months`. A single logged transaction is immediately usable as an average — **no artificial gating** while a user builds up history, per explicit design intent (documented in the function's own docstring and enforced by a dedicated test).

**Lump sum distribution**: reuses the entire existing income infrastructure almost unchanged — `income_engine.get_payment_dates()` treats `self_employed_average` like a `fixed_date`-equivalent anchor (see income_engine.py section above), so it flows through forecast/snapshot/auto-apply/overview exactly like a normal monthly income source, just with a resolved (not stale) amount.

**Spread-evenly distribution bypasses `income_engine.py` entirely — this was an explicit architectural decision, not an oversight.** Spread income has no discrete payment date at all (it's a continuous daily accrual), which doesn't fit `income_engine`'s contract of "a list of discrete dates, each triggering the full amount." Rather than bending that engine's API to accommodate a shape it wasn't designed for (which would have risked regressions across every other income type's call sites), the decision — made explicitly with the user rather than assumed — was to compute a flat daily rate via `_self_employed_cycle_length_days(user_id)` (calls `cycle_engine.get_cycle()` once, `(display_end - display_start).days + 1`, defaults to 30 on any failure) and inject that daily rate directly into each calculation site's own day-by-day loop or list, keyed however that specific consumer already works:
- `calculate_financial_overview()` — spread rows get a `daily_amount × num_remaining_days_in_period` lump added to `future_income`, computed inline, `continue`s past the normal `income_engine.get_payment_dates()` branch
- `forecast()` and `api_snapshot()` — spread rows are pulled into a separate `spread_rows` list with a precomputed `_daily_amount`, applied inside the existing day-by-day simulation loop (every single day gets an accrual entry, not just cycle-boundary days) — this is a deliberate simplification: the daily rate is a flat constant across the whole rolling window, not recomputed at each individual cycle boundary
- `calculate_monthly_spending()` — spread rows are explicitly skipped (`continue`) from the "Income received" itemised list, since there's no discrete "received" event to show — the accrual is described as "already reflected in the balance chart" rather than listed as a line item
- `run_auto_apply_backfill()` / `get_pending_auto_apply_items()` — spread rows are explicitly excluded from ever being auto-applied as a discrete transaction (checked via `inc.get("_distribution") == "spread"`), since spread income is a pure forecasting/calculation construct, not a real ledger event — auto-applying it as a lump transaction would double-count against the daily-accrual logic used everywhere else

**No "Payday" language for self-employed users — enforced, and a real bug was found and fixed here.** Since self-employed users are always in manual cycle mode, the UI must never say "payday" to them (honest framing instead: "Your cycle starts on day X of each month", "this cycle" instead of "before your next payday"). `templates/index.html`'s Home page Safe-to-Spend banner and cycle-info tooltip both had hardcoded "payday" wording that was **not** gated on cycle mode — found via live Playwright verification (not caught by any unit test, since the offending text was a JS string literal embedded in a `<script>` tag that's the same for every user regardless of which branch actually executes). Fixed by:
- Gating the Jinja-rendered banner/tooltip text on the existing `show_payday_countdown` flag (`True` only when `mode_used=='automatic'`)
- For the JS-rendered banner variant (used when the date-range picker changes), moving the phrase resolution **server-side** into the `ov-init-data` JSON blob (`"untilPhrase": "before your next payday"` or `"this cycle"`, computed via Jinja) rather than hardcoding both branches in shared client-side JS — this is the only way to guarantee the literal string "payday" never reaches a self-employed user's page source at all, not just that it's never *displayed*
- **Lesson for future work**: a raw-HTML-substring check on rendered output (`"payday" not in html.lower()`) is a stronger test than checking only what's visually rendered, precisely because it also catches inert JS string literals sitting in `<script>` blocks that ship to every user regardless of which branch executes for them.

**manage.html's income table** also calls `_resolve_income_rows()` before rendering, so the displayed amount and the monthly/yearly totals (`normalised_totals()`) reflect the live resolved value, not a stale stored column.

**Tests**: `tests/test_self_employed.py` (44 tests) — setup/save routes, `_resolve_income_rows()`/`_compute_automatic_income_average()` directly, lump vs spread behaviour in `/api/snapshot`, auto-apply exclusion, the no-"Payday"-language regression (including a positive control confirming employed automatic-cycle users still see the word), and explicit zero-impact-on-employed-users checks.

## Spending Alert Threshold — complete reference (August 2026)

**Purpose**: an optional, user-defined low-balance warning, explicitly separate from the app's own Safe to Spend calculation — lets a user set their own "danger zone" balance and get warned in-app when they reach it.

**Schema**: `users.alert_mode` (`NULL`/`'overall'`/`'per_account'`), `users.alert_overall_threshold`, `accounts.alert_threshold` — one column per user-level trait and one per-account column, following the exact same pattern as `employment_type` and `is_locked`/`savings_rate`. No new tables.

**Core function** (`app.py`):
```python
def get_triggered_spending_alerts(user_id, accounts) -> list[dict]
```
Returns `[{"account": name_or_None, "balance": float, "threshold": float}, ...]` for every threshold currently at or below its balance (`<=`, not `<`). Empty list immediately if `alert_mode` is `NULL` — **fully opt-in, zero behaviour change for any user who hasn't set one up.**
- `overall` mode: sums the balance of all active, **unlocked** accounts (regardless of type — current, cash, or savings; this is a "total balance" figure, not scoped to spending accounts the way Safe to Spend is) and compares to `alert_overall_threshold`
- `per_account` mode: checks each active, **unlocked** account with a non-`NULL` `alert_threshold` against its own balance

**Locked accounts are excluded from both modes** — same reasoning as `calculate_financial_overview()`'s existing exclusion (see Account Locking above): a frozen post-downgrade balance can't be trusted as current, so it shouldn't feed a live warning any more than it feeds the forecast. This was an explicit design judgement call (flagged and reasoned through, not silently assumed) rather than something the brief mandated outright.

**Settings card** ("Spending Alert Threshold", `templates/settings.html`'s Display tab, all users): Off/Overall/Per-account toggle — same `setAlertMode()` JS idiom as `setCycleMode()`/`setAvgMode()`. Locked accounts aren't shown as configurable in the per-account list. **Switchable/editable/disable-able at any time** via `POST /settings/save-alert-threshold` (`settings_save_alert_threshold()`):
- `mode='off'` clears `alert_mode`, `alert_overall_threshold`, **and every account's `alert_threshold`** for that user — a full reset, not just switching the mode flag
- `mode='overall'` validates the amount via the existing `validate_amount()` helper (must be a positive number) — invalid input redirects with an error, nothing is saved
- `mode='per_account'` reads one `threshold_<account_id>` field per active, unlocked account; blank or invalid fields are **silently cleared** (set to `NULL`) rather than failing the whole save, since the form has multiple independent optional inputs

**Home page banner** (`templates/index.html`): a new banner, visually consistent with (but functionally and visually distinct from) the existing Safe-to-Spend banner — same red/amber card styling, but its own 🚨 icon so it doesn't read as part of the same system. Rendered above the Safe-to-Spend banner when `spending_alerts` (computed in `home()`, passed to the template) is non-empty. Handles the singular-overall, singular-per-account, and multiple-accounts-triggered cases with different copy.

**Tests**: `tests/test_spending_alerts.py` (34 tests) — save route (all three modes, validation, locked-account skip on save, off-clears-everything), `get_triggered_spending_alerts()` directly (both modes, exact-threshold boundary, locked/inactive exclusion, multi-account), Home page banner presence/absence via the real Flask test client, and zero-impact-on-users-without-a-threshold checks.

## Savings & Debt Repayment Goal Tracking — complete reference (August 2026)

**Purpose**: users set a savings or debt-repayment goal (name, target amount, optional target date, optional link to an existing account) and track progress toward it. Deliberately ships **without** any streak/engagement/gamification layer — that's a separate, not-yet-designed follow-up feature; this is meant to work well as a complete, standalone feature on its own.

**Schema** — two new tables, following the same per-user-table pattern as `savings_rules`/`future_events` rather than bolting columns onto `accounts`:
- `goals`: `id`, `user_id`, `name`, `goal_type` (`'savings'` | `'debt'`), `target_amount`, `target_date` (nullable ISO date), `linked_account_id` (nullable, **references `accounts.id`, not name** — survives an account rename, same reasoning as `accounts.alert_threshold` being per-account-by-id), `starting_balance` (nullable — a snapshot of the linked account's balance taken when the goal is created or re-linked to a different account), `status` (`'active'` | `'completed'`), `created_at`, `completed_at`.
- `goal_contributions`: `id`, `goal_id`, `user_id`, `amount`, `date`, `note` — manual entries for a **standalone** (non-linked) goal only.
- Deleting a goal explicitly deletes its `goal_contributions` first in the same route (`settings_delete_goal`) rather than relying on `ON DELETE CASCADE` firing — SQLite foreign keys aren't enforced by default in this app's connections, so the FK constraint alone wouldn't reliably clean up on SQLite, only Postgres. Deleting a goal never touches `accounts` or `transactions`.

**Progress calculation — three modes, computed live in `_compute_goal_progress()` (app.py), never stored**:
- **Linked savings goal**: progress = the linked account's **current balance**, taken at face value against the target. Deliberately *not* relative to `starting_balance` — a straightforward "how much do I have toward this" reading.
- **Linked debt goal**: progress = **how much of the balance has been paid down since the goal started tracking it** — `abs(starting_balance) - abs(current_balance)`, both taken as magnitudes so it works whether a user stores debt as a negative balance (e.g. `-8000`) or as a positive "amount owed" figure (e.g. `8000`). The account's current balance *alone* doesn't say how much progress this specific goal has made (the account might have existed with debt on it before the goal was created) — hence needing `starting_balance` as the anchor. This is the "balance decreasing toward zero = progress increasing" framing, the opposite direction to savings.
- **Standalone goal (either type)**: progress = `SUM(goal_contributions.amount)` toward `target_amount`. No sign handling needed — the user self-reports "amount achieved" either way (a logged contribution on a debt goal means "I paid off £X", not "I now owe £X").
- `starting_balance` is re-snapshotted from the (new) account's current balance whenever a goal is linked or re-linked to a different account (in `settings_add_goal`/`settings_edit_goal`), and cleared to `NULL` when unlinked. Editing a goal *without* changing its linked account leaves `starting_balance` untouched.

**Automatic vs manual completion**: `status` flips from `'active'` to `'completed'` two ways — a user hitting **Mark achieved** (`POST /settings/complete-goal`, which also acts as a toggle: hitting it again on a completed goal reopens it) at any point regardless of actual progress, or automatically the moment computed progress reaches 100%, checked opportunistically inside `manage()` on every page load (`_mark_goal_completed_if_reached()`) rather than via a background job — simplest possible implementation given goals are only ever *viewed* through that one route.

**Pace suggestion — deterministic arithmetic, not an external AI call**: `_suggest_goal_pace(target_amount, progress_amount, target_date)` only returns a value when `target_date` is set (returns `None` otherwise, per spec — the feature must not trigger without one). `remaining_amount / (days_remaining / 30.44)` gives a `monthly_pace` figure; a target date in the past returns `overdue: True` with `monthly_pace: None` instead of a negative/nonsensical pace. Deliberately kept as simple, transparent arithmetic over the app's own existing financial data rather than calling an external AI service — there's no pattern-recognition need here that basic maths doesn't already serve, and it avoids a new external dependency/cost for what is fundamentally "how much divided by how long".
- **Safe to Spend cross-check**: `_get_safe_to_spend(user_id)` reuses `calculate_financial_overview()` (the exact same function and `safe_spending` figure shown on Home) rather than re-deriving a separate income/spending estimate. If the suggested `monthly_pace` exceeds it, a `warning` string is attached — **always non-blocking**, a gentle note, never prevents saving the goal or the suggested figure.
- **Live preview, never auto-applied**: `POST /api/goal-pace-preview` (JSON, CSRF-exempt-in-handler, added to `check_csrf()`'s exempt list) mirrors the existing `/api/income-preview` live-preview pattern exactly — the Add/Edit Goal modal calls it on every relevant field change (debounced) and renders the suggestion into the form, editable, never written anywhere until the user submits the form themselves with whatever target amount/date they've actually chosen.

**Projected completion date — the inverse of the pace suggestion above, and initially missed** (added in a follow-up pass after the gap was spotted): `_suggest_goal_pace()` asks "given a target date, what pace is needed"; `_compute_goal_recent_pace()` + `_project_goal_completion()` ask "given what's *actually* happening lately, when will this really finish" — independent of whether a target date exists at all, since that's often the most useful thing to know about a goal with no fixed deadline.
- **Real recent pace, not lifetime average**: `_compute_goal_recent_pace()` — for a standalone goal, prefers `goal_contributions` from the last 90 days; a sparse logger with fewer than 2 in that window falls back to their last 5 contributions regardless of age (still real recent activity, just infrequent) rather than reporting nothing. For a linked goal, reconstructs the account's balance at the start of the observed window from real `transactions` history (the same balance-at-a-past-date technique the Forecast chart's historical scrollback used to use — see below — but here that reconstruction is exactly the point, not something to avoid), then measures the change with the same `abs()`-based magnitude approach as `_compute_goal_progress` so a shrinking debt balance and a growing savings balance both read as positive pace, and the reverse (growing debt, shrinking savings) correctly reads as *negative* pace rather than being clamped to zero. Needs at least 2 real data points to return anything — a single contribution, or an account with zero transactions in the window, returns `None` (`insufficient_data`) rather than fabricating a rate.
- **Projection and target comparison**: `_project_goal_completion()` extrapolates `remaining_amount / pace_per_day` to a date. States: `insufficient_data` (not enough real data yet), `no_progress` (pace ≤ 0 — genuinely not moving, or moving backwards), `reached`, `years_away` (pace so slow the literal date would be a decade-plus out — shown as an honest "N+ years away" instead of a fabricated-looking precise calendar date), or `projected` (a real date). When a target date exists, compares against it: on/before → green "on track"; up to 30 days after → amber; more than 30 days after (or `no_progress`/`years_away`) → red. Recomputed fresh on every load, never stored — the same principle as everything else in this feature.
- **Surfaced in two places**: the Goals tab shows the full line (`Target: X · At current pace: Y — on track/behind target`, colour-coded) plus the existing suggested-pace line beneath it. Home's compact card only ever shows a small 🟢/🟡/🔴 dot next to the percentage (only when a target date is set, since the dot *is* the on-track comparison) — deliberately not the full date text, to avoid re-crowding the compact multi-goal rows that were specifically stripped down to name+percentage in the previous pass.

**Safe-to-Spend fallback estimate — only for the insufficient-data case, never blended with real pace**: a brand-new goal with fewer than 2 real data points used to just show "not enough recent activity yet to estimate a pace" — honest, but unhelpful. `_compute_goal_pace_map(goals, user_id, accounts_by_id)` now runs as a **batch** over a user's goals (not goal-by-goal) specifically so this fallback can be divided sensibly:
- First pass computes every goal's real recent pace via `_compute_goal_recent_pace()` (unchanged).
- `active_needing_fallback` = active goals where that came back `None`. A **completed** goal never participates — it doesn't need an estimate, and counting it would just shrink everyone else's share for no reason.
- If any goals need it, `_safe_to_spend_daily_rate(user_id)` converts the cycle-scoped Safe to Spend figure into a £/day rate, then divides it evenly across `len(active_needing_fallback)` — so three active goals with no history yet each get a third of the typical leftover, not the full amount implied three times over. A goal that already has real tracked pace doesn't count toward this denominator at all, since it isn't competing for the same theoretical surplus the same way.
- A non-positive Safe to Spend (e.g. no income, empty/zero-balance accounts) produces **no fallback at all** — falls through to the same honest "not enough recent activity" message as before, rather than fabricating a positive pace that doesn't exist. `calculate_financial_overview()` already floors `safe_spending` at 0, so this can never go negative, but the `> 0` guard is explicit regardless.
- `_project_goal_completion()` gained an `is_estimate` parameter — a pure pass-through flag the caller sets (it never infers this itself), included in the returned dict so the maths is identical either way and only the label differs downstream.
- **UI clearly distinguishes it**: manage.html shows "🔮 Estimated (based on your typical spending)" in italics instead of "At current pace", plus a muted explanatory line ("Based on your typical Safe to Spend — around £X/month[, split evenly across N goals that don't have tracked history yet]. This will switch to a real tracked pace once you..."). Home's compact dot gets a `~` prefix and an explanatory `title` tooltip. The moment a goal crosses the real-data threshold, both surfaces switch over automatically — no separate flag to flip, it's just that `_compute_goal_recent_pace()` no longer returns `None` for it.
- **Note on a Flask-Login quirk this surfaced**: `_get_safe_to_spend()` → `calculate_financial_overview()` → `load_scheduled_expenses_web()` reads `current_user.id` (Flask-Login's request-bound global) rather than the `user_id` parameter threaded through the call chain — pre-existing, unrelated to this feature, but it means `_compute_goal_pace_map()`/`_safe_to_spend_daily_rate()` only resolve correctly inside a real authenticated request (exactly how the app always calls them in practice). Direct unit tests of the fallback-splitting behaviour go through `auth_client.get(...)` rather than calling the function in isolation, for this reason.

**Fallback figure fix — absurd monthly estimates (August 2026)**: a real bug report showed implausible fallback figures (e.g. £8,764.28/month against a modest goal). Traced to two compounding causes, both fixed in `app.py`:
- **`_safe_to_spend_daily_rate()`'s denominator was unstable.** It divided the live Safe-to-Spend snapshot (current balance + still-to-arrive income − still-to-arrive bills, through to the next payday) by `days-remaining-until-safe_boundary` — a figure that shrinks toward the end of every cycle. Since Safe to Spend already includes the FULL current balance (a stock, not a recurring monthly flow), dividing that lump by a small remaining-days count inflated the implied rate purely depending on which day of the cycle the page happened to be checked on — a manual-mode account checked 9 days before month-end already inflated the rate ~3.4x versus checking right after payday, and 1–2 days out inflated it by an order of magnitude, for identical underlying finances. Fixed by dividing by the cycle's fixed length (`safe_boundary − display_start + 1`) instead, so the rate is stable regardless of when it's viewed.
- **Nothing capped the resulting £/month figure against the goal itself.** Even with the stabilised denominator, a genuinely large balance or a small goal splitting a large surplus could still imply completing the ENTIRE goal in days rather than months — technically consistent with the numbers on screen, but not a meaningful "typical monthly pace" claim to make from a live snapshot with no real tracked history behind it. `_cap_fallback_rate_for_goal(goal, fallback_rate, user_id, accounts_by_id)` now caps each goal's share so it can never imply finishing in under `_FALLBACK_MIN_DAYS_TO_COMPLETE` (30.44 days, matching the existing £/day↔£/month constant) — `min(fallback_rate, remaining_amount / 30.44)`. This only ever pulls the figure down, never up, and only bites when the implied pace is unrealistically fast for THIS goal — a large surplus against a large goal (e.g. £200,000 balance funding a £500,000 goal) is left uncapped, since that's a genuine, proportionate figure, not a misleading one.
- Both fixes are scoped entirely to the goal-fallback-estimate's own private calculation (`_safe_to_spend_daily_rate`/`_compute_goal_pace_map`) — `calculate_financial_overview()`'s `safe_spending` figure itself (used by Home's live Safe-to-Spend banner and everywhere else) is untouched.

**Second fallback-figure fix — hard ceiling at real recurring income minus bills (August 2026)**: even after the fix above, a real user reported the fallback STILL exceeding their actual salary minus bills, by 3x or more. Investigated with representative real-world numbers (£2,200/month salary, £1,400/month bills, £650 leftover balance — real surplus £800/month) traced end to end through `calculate_financial_overview()` → `_get_safe_to_spend()` → `_safe_to_spend_daily_rate()`:
- **Checked after all 5 bills' due-dates had passed within the cycle**: `safe_spending` = £2,850 (balance £650 + upcoming salary £2,200 − £0 bills, since every bill's date had already passed and dropped off the "still to pay" list) → uncapped monthly figure **£2,798.52** — 3.5x the real £800 surplus.
- **Checked with every bill still ahead in the cycle** (the "best case"): `safe_spending` = £1,450 (all £1,400 in bills correctly deducted) → uncapped monthly figure **£1,423.68** — still 1.8x the real surplus, because the £650 balance was being added on top of income and then annualised as if it recurred every month.
- **Root cause, confirmed on both counts from the brief**: Home's Safe to Spend figure is NOT broken — a live snapshot that includes the current balance and drops already-passed bills is exactly correct for its own "what can I safely spend right now" purpose (matches its documented definition above). The bug is entirely in the goal fallback reinterpreting that stock-plus-partial-cycle-flow figure as if it were a stable recurring monthly flow; no denominator fix (see above) can correct a numerator that isn't a monthly flow to begin with.
- **Fix**: `_recurring_income_bills_daily_rate(user_id)` computes the user's REAL recurring monthly income minus real recurring monthly bills via `normalised_totals()` — the same helper `manage.html`'s own Income/Bills cards use for their £/month · £/year totals — converted to a £/day rate. `_compute_goal_pace_map()` now takes `min(safe_to_spend_share, recurring_surplus_share)` for the shared fallback pool (both split evenly across `active_needing_fallback` goals, for the same "no single goal implies the whole surplus" reasoning as before) — this only ever pulls the figure down, never up. If the real recurring surplus is zero or negative (bills ≥ income, or no income tracked at all), the fallback is suppressed entirely — same honest "not enough recent activity" message as the zero-Safe-to-Spend case, never a fabricated positive figure inferred from a balance alone.
- **Stacks with the existing per-goal 1-month-completion cap** (`_cap_fallback_rate_for_goal`, above) — whichever cap is more conservative for a given goal wins; a small goal against a healthy recurring surplus is still bound by the completion-speed cap, not the (much higher) income ceiling.
- **Only touches the fallback-estimate path** — a goal with genuine tracked pace (`_compute_goal_recent_pace()` returning real data) is completely unaffected, even if that real pace exceeds what a modest income/bills setup would imply.

**Tests**: `tests/test_goal_fallback_pace.py` (31 tests) — the fallback appearing in place of the blank insufficient-data message, its distinct labelling/styling, switch-over to real pace for both standalone and linked goals, the even split across 2+ active goals (verified via real `/manage` requests, not a direct function call, per the Flask-Login note above, using large goal targets so the new caps don't mask the split-evenly arithmetic), a real-pace goal correctly excluded from the fallback denominator, a completed goal excluded too, and the zero/negative-Safe-to-Spend case producing no fabricated pace. Plus 5 tests for the cycle-length/completion-speed fix (regression repro, the ≥30-day invariant, confirmation a genuinely large recurring income against a genuinely large goal isn't suppressed, denominator-stability, a zero-remaining edge case), and 8 more for the recurring-income-minus-bills ceiling: both bill-timing scenarios from the investigation converging on the same capped figure (rather than swinging with which bills happened to have passed), a large-balance/modest-income case no longer inflating past the real surplus, the negative- and zero-income honest-message cases, the two caps stacking correctly (smaller wins), real tracked pace being unaffected, and the `normalised_totals()` frequency-conversion arithmetic directly. One pre-existing test in `test_goal_pace_projection.py` (`test_recalculates_as_linked_balance_changes`) needed a real income source added to its setup, since an account balance alone (no tracked income) is now correctly one of the suppressed no-estimate cases rather than triggering a fallback.

**Account locking interaction — a deliberate judgement call**: a goal linked to a now-locked account (Pro-to-Free downgrade, see Account Locking above) is **not** excluded, hidden, or deleted. Its `current_balance` read is already the frozen figure (locked accounts are frozen at the DB level by the existing locking design, so no special handling is needed to "freeze" it a second time) — `_compute_goal_progress()` just surfaces an `account_locked: True` flag alongside the (frozen) progress figure, and `templates/manage.html`'s goal card shows a "🔒 progress may be stale" note next to the linked account name, consistent with how locked accounts are flagged everywhere else in the app (manage.html's account list, every account `<select>`) rather than presenting a frozen number with the same confidence as live data. **A *new* goal can still be linked to an already-locked account** — deliberately allowed rather than blocked, since reading a locked account's balance is read-only and harmless, unlike the bill/income/transfer routes that block against locked accounts because those would attempt to *write* new activity against them.

**Routes** (all in `app.py`, all `@login_required`):
- `POST /settings/add-goal`, `POST /settings/edit-goal` — the latter re-snapshots `starting_balance` on any linked-account change (see above)
- `POST /settings/delete-goal` — also deletes the goal's `goal_contributions`; never touches `accounts`/`transactions`
- `POST /settings/complete-goal` — toggles `active` ⇄ `completed`
- `POST /settings/add-goal-contribution` — rejected (redirect with a message, not a hard error) if the goal is linked, since a linked goal's progress already comes from the real account balance and a logged contribution there would double-count
- `POST /settings/delete-goal-contribution`
- `POST /api/goal-pace-preview` — see above

**UI** (`templates/manage.html`): new "🎯 Goals" tab alongside Accounts/Bills/Income/Rules, following the same list-card pattern as the rest of the page. Uses a **single shared Add/Edit modal** (`#goalModal`) rather than per-row inline edit forms — closer to how the Income modal already works in this file than to the simpler inline-row pattern Bills/Savings-Rules use, because goals have enough fields (name, type, target amount, target date, linked account) to justify it, and it's the natural place to host the live pace-preview panel for both creating and editing. Edit-population uses `var _goalsData = {...}` built via an explicit per-field Jinja loop (**not** `{{ goals|tojson }}` on the whole list) — the `goals` list carries Postgres `NUMERIC` values (which arrive as non-JSON-serialisable `Decimal` via psycopg2) and `TIMESTAMP` columns (which arrive as `datetime` objects), both explicit landmines for `tojson`; the explicit-field approach only ever emits values already cast to plain Python `float`/`str`/`None` in `manage()`, sidestepping the issue entirely rather than needing a fix for it. Standalone goals show an inline logged-contributions list with a "+ Log contribution" button opening its own small modal (`#goalContributionModal`).

**Home page entry point** (`templates/index.html`, `home()`): a card positioned directly below the "Can I afford this?" button — a CTA ("Set a savings or debt goal") when there are no active goals, otherwise a compact progress summary capped at 3 rows with a "+N more goals" line beyond that. Deliberately lives **outside** the `has_funds`/no-funds branch "Can I afford this?" sits inside — a user tracking a debt-repayment goal with no positive balance anywhere is exactly who a "some account > £0" check would wrongly hide it from, caught via a render test during development rather than by inspection. The progress-bar width is set from a `data-pct` attribute via a small init script rather than inline `style="width:{{ }}%"`, matching the existing `cycleProgressBar` pattern — putting a Jinja expression directly inside an inline `style` attribute's numeric value trips the editor's CSS-in-attribute linter the same way Jinja-in-`<script>` already does (see "Known open issues" below); the codebase already had an established fix for exactly this, just hadn't been applied here yet.

**Tests**: `tests/test_goals.py` (39 tests) — creation (both types × linked/standalone × with/without target date, validation), progress calculation for all three modes (including the debt-goal test confirming it works under both negative-balance and positive-"amount-owed" conventions), pace suggestion (present only with a target date, overdue handling, Safe to Spend warning threshold), edit/delete/manual-and-automatic completion (including the starting-balance re-snapshot-on-relink behaviour), and the locked-linked-account interaction. Uncovered a pre-existing test-infrastructure gap along the way: `tests/conftest.py`'s `test_user` fixture teardown deleted from every per-user table *except* the two new ones — fixed by adding `goal_contributions`/`goals` to that cleanup list, which matters because the test DB is session-scoped (shared across the whole test run), so leftover rows from an earlier test with a generic goal name could otherwise be picked up by a later, unrelated test.
`tests/test_home_goals.py` (15 tests) — no-goals/single-goal/multi-goal states on Home, the multi-goal 3-row cap with correct singular/plural "+N more" grammar, completed goals excluded from the Home summary, the locked-account render-without-error case, and per-user isolation.
`tests/test_goal_pace_projection.py` (30 tests) — the recent-pace calculation for both standalone (varying intervals, sparse-logging fallback, recent-window-preferred-when-available) and linked (both directions for both goal types, insufficient-data cases) goals; every `_project_goal_completion()` state including the on-track colour boundaries (exactly-on-target, 15-days-over amber, 200-days-over red) and the years-away cap; integration checks that the Goals tab shows the projection with and without a target date, and that it genuinely recalculates (not cached) as new contributions or transactions are added; and the Home page's compact on-track dot.

### Goals card visual redesign (August 2026)

**Purpose**: the original Goals tab card (three full-width Edit/Mark achieved/Delete buttons, thin flat progress bar, dense grey pace text) was replaced with a more visually engaged design — an icon tile + colour identity per goal, a single "•••" overflow menu, a larger progress readout with a thicker colour-matched bar, and (the main functional addition) compact side-by-side "Target date" vs "At current pace" stat boxes with an explicit "N months behind — try £X/month more" action line. **Pure presentation layer** — no progress/pace/projection calculation logic changed; the redesign only consumes values `_compute_goal_progress()`/`_suggest_goal_pace()`/`_project_goal_completion()` already produced.

**`_pick_goal_icon(name, goal_type)`** (app.py): keyword-matches the goal's (lower-cased) name against `_GOAL_ICON_RULES` (trip→✈️, house/deposit→🏠, car→🚗, emergency→🛡️, wedding→💍, education→🎓, tech→💻, gift→🎁, baby→🍼, gym→💪, first match wins), falling back to 🐷 (savings) or 📉 (debt) when nothing matches. Plain emoji, not an icon font/SVG library — the codebase has neither, and emoji is already the app's established icon convention throughout (🎯 Goals, 💰 Income, 🏦 Accounts, etc.).

**`_build_goal_display(g)`** (app.py): the presentation-derivation helper, called once per goal in both `manage()` and `home()` and attached as `g["display"]`. Reads only already-computed `g["progress"]`/`g["pace"]`/`g["projection"]` — never recalculates anything. Returns:
- `icon_emoji`/`icon_bg`/`icon_fg` — from `_pick_goal_icon()` plus a savings=green-tint (`#dcfce7`/`#166534`) or debt=red-tint (`#fee2e2`/`#991b1b`) palette, matching the existing `.check-icon` pale-tint convention used elsewhere in the app
- `type_bg`/`type_fg`/`type_label` — same palette, for the small type pill under the goal name ("Savings" / "Debt repayment")
- `bar_color` — green (`#198754`) for `status=='completed'` or `projection.status_color=='green'` (on track), amber (`#f59e0b`) or red (`#dc3545`) matching an amber/red projection, else the app's brand purple (`var(--brand)`) when there's no on-track/behind signal to color by (no target date, or `insufficient_data`/`no_progress` states) — deliberately the *only* signal this bar carries; per explicit brief instruction, kept simple rather than trying to overload it with more meaning
- `pace_label` — `"Estimated"` when `projection.is_estimate`, else `"At current pace"` — short label for the compact stat-box header (the longer explanatory sentence, e.g. "Based on your typical Safe to Spend...", still renders separately underneath, unchanged from before this redesign)
- `months_behind`/`extra_monthly_needed` — **not new maths**: `months_behind` is `projection["days_over_target"] / 30.44` (that figure was *already* computed inside `_project_goal_completion()` to decide on-track/behind, just discarded before this redesign — now exposed as its own field, `days_over_target`, positive-only, `None` otherwise); `extra_monthly_needed` is `g["pace"]["monthly_pace"]` (the existing required-pace-to-hit-target figure) minus `projection["pace_per_day"] * 30.44` (the existing real/estimated current pace), floored at `None` when the gap isn't positive. Only ever set for `state == 'projected'` with a target date and a behind result.

**Goals tab markup** (`templates/manage.html`, `#tab-goals`): each goal card now has a `.goal-icon-tile` (40×40, rounded, `d.icon_bg`) beside the name/`.goal-type-pill`, replacing the old three-button row with a single `.goal-menu-btn` ("•••", 40×40 tap target) that toggles a `.goal-menu-dropdown` containing the same three actions as plain full-width buttons (`.goal-menu-item`, Delete additionally gets `.text-danger`) — **the underlying forms/routes are completely unchanged** (`/settings/complete-goal`, `/settings/delete-goal`, `openEditGoalModal()` opening the same existing `#goalModal`), only how they're triggered changed. Progress readout is now a large `£X` beside `of £Y` with the percentage bold and colour-matched to `d.bar_color`, above a thicker (`.goal-progress-track`, 14px) colour-matched bar. When a target date is set and the projection state is `projected`/`years_away`, two `.goal-stat-box` divs sit side by side ("Target date" / pace-label, the latter colour-tinted green/amber/red) followed by the on-track confirmation or the "N months behind — try £X/month more" line; with no target date, a single stat box shows the estimate/current-pace figure with no comparison (nothing to compare against); `no_progress`/`reached`/`insufficient_data` states render as a single plain line regardless of target date, same as before this redesign. The hand-rolled `toggleGoalMenu(event, id)`/`closeGoalMenus()` JS functions (plus a document-level click-outside listener) are the app's first overflow-menu component — no Bootstrap JS bundle is loaded (CSS-only) and no dropdown pattern existed before, so this was hand-rolled to match the app's existing "hand-toggled `display:none`/`.open` class" convention rather than adding a new JS dependency for one component.

**Home card** (`templates/index.html`): the compact per-goal row gains a small 26×26 icon tile (same `d.icon_bg`/`d.icon_emoji`) and the progress bar now reads its colour from a `data-color` attribute (`d.bar_color`) instead of being hardcoded to brand purple in the init script — everything else (the 🟢/🟡/🔴 on-track dot, the `~` estimate prefix, the 3-row cap with "+N more", the no-goals CTA) is unchanged. Deliberately still compact — no target-vs-pace stat boxes on Home, per the brief's explicit allowance to keep the compact card simple given it already handles multiple goals at once.

**No browser/screenshot tooling in this session** — mobile (375px)/desktop layout correctness was verified structurally (flex layout with `min-width:0`/`text-truncate` on the name column, `flex-shrink:0` on the icon tile and menu button so they never compress, `.goal-stat-box` using `flex:1 1 0` so the two boxes share width evenly at any viewport, 40×40 tap targets for icon tile and menu button) rather than with an actual rendered screenshot.

**Tests**: `tests/test_goal_display_redesign.py` (29 tests) — `_pick_goal_icon()` keyword matching and fallbacks for both goal types; `_build_goal_display()`'s colour/label derivation and the `months_behind`/`extra_monthly_needed` arithmetic directly; the overflow menu's markup and that all three actions still hit the same unchanged routes end-to-end; icon tile/type pill rendering for both goal types; and the Home card's icon tile plus its existing no-goals/multi-goal-cap states still working. Existing tests in `test_goal_pace_projection.py`/`test_goal_fallback_pace.py` had their slice windows widened (2200/2600 → 4200 chars — the new markup is longer per card) and a handful of literal-text assertions updated to match the new structure (e.g. `"At current pace:"` → `"At current pace"` now that the label and value sit in separate stat-box divs rather than one colon-joined line); no assertion was weakened, only re-pointed at equivalent information in the new markup.

## Future Events — complete reference (August 2026)

**Purpose**: a Future Event (Manage > Rules, Pro-only to add) is a one-off future cost tied to a specific account and date — e.g. "Wedding gift, £150, 15 Oct". Before August 2026 it was purely informational (listed, editable, but its amount never affected any calculation, and there was no way to delete one). This upgrade makes it a genuine forecasting input, consistent with how a scheduled bill already works, adds delete, and adds navigation from wherever an event is shown through to the Forecast page centred on its date.

**Schema**: `future_events` table (unchanged) — `id`, `name`, `amount`, `date` (a real ISO date, not a day-of-month), `account`, `user_id`. No new columns needed.

**One-off, not recurring — the key difference from a bill**: a bill has a `day`/`frequency` and fires every period; an event has a single `date` and is only ever relevant on/around that one occurrence. Every calculation site below treats it as a plain date-in-range check with no "next occurrence" search and no `last_applied`-style tracking — once its date has passed, it naturally falls out of every "future" window on its own, the same way a one-off transaction would.

**Discovered already partially wired up**: before this work, `forecast()`'s 90-day balance simulation and `api_snapshot()`'s day-view already deducted future events from their linked account's projected balance (pre-existing, undocumented). The actual gaps closed here were: `calculate_financial_overview()` (Safe to Spend / bills-left) didn't include them at all, `forecast()`'s `upcoming_items` list (which feeds the chart's annotation markers) didn't include them, there was no delete route, and there was no navigation link-through anywhere they're shown.

**`calculate_financial_overview()` integration** (`app.py`): a new block immediately after the scheduled-bills loop fetches the user's `future_events` and, for each one whose `date` falls in `(today, period_end]` (the same bound bills use — legacy no-`period_end` callers get the equivalent "later this calendar month" bound bills use), folds its amount into `all_future_bills`/`future_bills_list` (shown as "Bills left") and, if the linked account is a spending type (current/cash), also into `spending_future_bills` (deducted from Safe to Spend) — both existing bill dict entries and new event entries in `future_bills_list` now carry a `"type"` key (`"bill"` or `"event"`) so consumers can tell them apart. A locked linked account is excluded via the exact same `accounts[acc].get("is_locked")` check bills already use.

**`forecast()`'s `upcoming_items`** (drives the chart's annotation lines and the "click a day, see what's due" panel): future events within the 90-day window are now appended with `"type": "event"` and their real `id` — previously this list only ever contained bills and income, even though the balance simulation itself already accounted for events.

**No merging into the bill-pay-row / mark-as-paid flow, deliberately**: the Home page's compact "Bills left" tile and the JS-driven `/api/overview` re-render both use the same `future_bills_list`, and its existing rows carry a `bill-pay-row` class wired to POST `/mark-bill-paid` with a `scheduled_expenses` id. An event has no `last_applied`/"mark paid" concept and isn't in that table at all, so folding it into that click path unchanged would either error or (worse) silently no-op against the wrong row. Every place `future_bills_list`/`upcoming_items` gets rendered branches on `type === 'event'` (or `b.type == 'event'` in Jinja) to render a plain, non-bill-pay-row `<a href="/forecast?date=...">` link instead — see Navigation below. The bigger "Bills remaining" bottom-sheet breakdown modal (`openBillBreakdown()`) was already generic/click-free, so it only needed the 📅 icon + link added, not a bug fix.

**Navigation — "I see this event → I can see its forecast impact in one tap"**:
- `templates/manage.html`'s Future Events row gets a "📈 View" button linking to `/forecast?date={{ e.date }}`, alongside the existing Edit and the new Delete.
- `templates/index.html`'s Bills Remaining tile and its bigger breakdown modal both render an event row as a link to `/forecast?date=...` (📅 prefix, no mark-paid button) instead of a `bill-pay-row`.
- `templates/forecast.html` reads `?date=YYYY-MM-DD` on load and calls `jumpToDate(isoDate)`, which re-centres the existing 14-days-either-side chart window (`centerDate`, defaulting to today) around that date, re-renders, opens the existing `showDayPanel()` info panel for it, and scrolls the chart into view. If the requested date falls outside the 90-day forecast window entirely, it shows a plain `alert()` rather than silently doing nothing (consistent with this app's existing plain-alert error-handling elsewhere). The `?date=` param is stripped from the URL bar afterward, matching the existing `?refresh=`/`?msg=` cleanup.
- `buildAnnotations()` (Chart.js) gives event lines a distinct amber (`rgba(245,158,11,...)`) dashed line with a small always-on "📅" label — bill lines (red) and income lines (green) stay unlabeled as before, since events are individually rarer/more notable and benefit from being spottable without a click.
- `showDayPanel()`'s per-item rendering gains an `isEvent` branch: shown with a 📅 icon and red "-£X" amount, no "Mark as Paid"/"Mark as Received" button (there's nothing to mark — it's not a recurring bill).

**Delete route**: `POST /settings/delete-future-event` (`settings_delete_future_event()`, app.py) — follows the exact same `DELETE ... WHERE id=? AND user_id=?` / `bust_forecast_cache()` / confirm-dialog pattern as `delete-savings-rule`/`delete-bill`. Add/edit/delete all now redirect with `tab="rules"` (matching bills' redirect pattern, which add/edit for events previously didn't follow — a small consistency fix alongside the new delete route).

**Tests**: `tests/test_future_events.py` (30 tests) — delete (removal, redirect+tab, isolation from other events/bills/balances, cross-user protection, button presence); `calculate_financial_overview()` integration (Safe to Spend reduction, bills-left total, `future_bills_list` type tagging, out-of-window and already-past exclusion, spending-vs-savings-account distinction, locked-account exclusion, multi-item summing); `forecast()` (balance reduced from the event's date onward and *not* before, confirmed one-off — not deducted a second time later in the 90-day window, `upcoming_items` tagging, beyond-90-day exclusion, locked-account exclusion, the annotation colour distinction); `api_snapshot()` (balance/day-view reduction, `item_type` tagging, locked-account exclusion); and navigation (Rules tab and Home both link to the correct `/forecast?date=` URL, the event row never carries `bill-pay-row`, `/forecast?date=` loads successfully and the page contains the `jumpToDate`/`URLSearchParams` hooks, `/api/overview` carries the same `type`/`due_date` fields the initial page load does).

### Bills Left vs Safe to Spend — labelling fix, not a calculation fix (August 2026)

**A real bug report** showed a Bills Left figure (£9,788.64) that looked wildly inconsistent with Safe to Spend on the same screen. **Investigated before writing anything**: reproduced with representative data (a modest current account, ordinary spending-linked bills, and a large bill + event linked to a savings/ISA account) and confirmed exactly the hypothesised cause — `all_future_bills` (which drives the Bills Left headline) sums **every** future bill/event/savings-rule regardless of which account it's linked to, while `spending_future_bills` (which Safe to Spend is actually derived from) already correctly summed spending-account-linked items only, just was never exposed as its own figure anywhere. **Confirmed out of scope, deliberately untouched**: `safe_spending`, `net_worth`, `savings_balance` — all three were already correct and remain byte-for-byte unchanged by this fix.

**The fix — three call sites needed the correction, not just the headline tile:**
- `calculate_financial_overview()` now returns a new `future_bills_spending` key (= `spending_future_bills`, exposed for the first time) alongside the existing all-inclusive `future_bills`. Every `future_bills_list` entry (bill, event, and savings_rule alike) now carries an `account_type` field so consumers can tell which group it belongs to.
- **Three places were quietly using the all-inclusive `future_bills` where they should have used the spending-only figure**, found by tracing every consumer rather than just the one reported tile: the Bills Left headline (`#future-bills-val`), the "Bills remaining" row *inside the Safe to Spend breakdown itself* (`#safe-bills-remaining-val`), and the "End of cycle" figure computed from it (`{% set eoc = spending_balance - future_bills + future_income %}` — this one was the most self-contradictory, since it sat directly under a correct `safe_spending` figure in the same panel). All three now read `future_bills_spending`. Same three fixed in the JS-driven `/api/overview` re-render path (`_applyOverviewData()`) and in `/api/overview`'s own response (`future_bills_spending` filtered the same way `future_bills` already was).
- **Grouped display, not just a smaller number**: both the inline `#tile-bills` dropdown and the bigger "Bills remaining" bottom-sheet modal (`openBillBreakdown()`) now split items into "Reducing Safe to Spend" (spending-linked) and "Covered by savings — not affecting Safe to Spend" (savings-linked) sections, each row showing its linked account name (previously shown nowhere). Savings-linked bill rows keep their existing `bill-pay-row` mark-as-paid behaviour (just muted styling) — only the grouping/labelling changed, not the underlying pay flow. The bottom-sheet's own "Total remaining" footer now matches the spending-only headline rather than the all-inclusive sum, for the same self-consistency reason.
- **Proven, not just asserted**: after implementing, the headline line was manually reverted to the old `future_bills` value, the new test suite was re-run and confirmed 7 tests failed exactly as expected, then the fix was restored and all tests re-confirmed passing — see `tests/test_bills_left_savings_split.py`'s existence as the record of that.

**Tests**: `tests/test_bills_left_savings_split.py` (18 tests) — the exact reproduction scenario from the bug report; a bill, an event, and a savings_rule each individually excluded from the headline when linked to a savings account; a mixed spending+savings scenario summing correctly; explicit non-regression checks that `safe_spending`/`net_worth`/`savings_balance` are byte-identical to before; the two-section grouping (both-shown, spending-only, savings-only, empty-state) scoped specifically to the rendered `#tile-bills` markup (not the page's own JS template-literal source, which contains the same header strings and would otherwise false-positive); account-name-per-row for both groups; and `/api/overview`'s `future_bills_spending`/`account_type` fields directly. One existing test in `test_future_events.py` had its expectation corrected (a savings-linked event no longer inflates the headline — it was asserting the pre-fix behaviour).

## Goal Contribution Engine — complete reference (August 2026)

**Purpose**: replaces the old text-heavy goal insight ("you're on track... based on your typical Safe to Spend... suggested pace...") with a single interactive slider. The user sets a contribution amount directly off Safe to Spend; it becomes a **standing recurring commitment**, applied every payday cycle going forward through the exact same engine as bills/future events — not a display-only suggestion. Framed by the user as "the same interlocking principle" as the [[Bills Left vs Safe to Spend]] fix above: one engine feeding Safe to Spend/forecast, never a parallel system that can silently disagree.

**Schema — deliberately no new table.** `savings_rules` gained a nullable `goal_id INTEGER DEFAULT NULL` (FK-by-convention to `goals.id`, indexed) rather than a new commitments table — confirmed structurally sound against the real schema before committing to it, per explicit instruction. A commitment IS a savings_rule: same `from_account`/`to_account`/`day`/`amount` shape every other savings_rule already has. `to_account` uses the table's existing empty-string "no real destination" convention (already safely handled by every consumer site's `if to_acc in accounts`-style check) for debt/standalone goals, and is only populated for a goal linked to a real, unlocked savings-type account. `goals` separately gained a nullable `minimum_payment NUMERIC(12,2)` — a debt-goal-only known minimum payment that becomes a hard floor on the slider (see below).

**Projection-only — no auto-apply, and this was a deliberate, explicitly-reasoned decision, not an oversight.** `savings_rules` has no auto-apply/backfill mechanism at all (confirmed by tracing all 4 of its consumers: `calculate_financial_overview()`, the dead `/afford` route, `api_snapshot()`, `forecast()` — all read-only projection inputs, none ever write a real transaction). The commitment follows this exact same pattern rather than gaining new auto-apply logic. Reasoning (user's own): Spendara doesn't move real money, so auto-crediting a goal's progress would create a mismatch between the app's tracked balance and the real account balance — a data-integrity risk not worth taking for the sake of feeling automatic. **Follow-up idea, flagged but not built**: a cycle-start nudge/reminder when an active commitment exists, prompting the user to confirm they've actually made the transfer — delivers the "pay yourself first" feel honestly, without fabricating data.

**One mechanism for both goal types — debt gets a floor, savings doesn't.** Both debt and savings goals use the identical slider; there's no separate debt-specific system. A debt goal with a `minimum_payment` set gets a hard floor at that value (the slider physically cannot go lower) and **defaults to the suggested pace, not zero** (or to the minimum itself if the suggested pace happens to be below it). A savings goal has no floor and can default arbitrarily low, including 0.

**`_compute_goal_commitment_bounds(goal, progress, pace, safe_to_spend, fallback_pace_per_day=None)`** (app.py): the slider's floor/default/max, everything pre-snapped to £5 increments via `_snap_to_increment(value, increment, mode)` (`mode="up"` for the floor — never understate it; `mode="down"` for the max — never overstate it; `mode="nearest"` for the default).
- `floor` = `minimum_payment` for a debt goal that has one, else 0.
- `default` = the target-date-driven suggested pace (`_suggest_goal_pace()`) when available; otherwise falls through to `fallback_pace_per_day * 30.44` (see below); otherwise the floor.
- `max` = `max(£50, 50% of Safe to Spend)` — the brief's explicit "not up to 100%, protect against over-committing" instruction, implemented as a **hard cap on the slider's own travel range**, separate from and in addition to the existing fallback-estimate caps (`_cap_fallback_rate_for_goal`, the recurring-income-minus-bills ceiling) from the earlier goal-tracking work. This means the slider's *default position* can end up visibly lower than the raw, uncapped fallback-estimate figure would otherwise imply — a deliberate UX safety behaviour, not a bug (see the test note below).
- **A real regression caught and fixed during this build**: the first version only consulted `_suggest_goal_pace()` (target-date-only) for the default, silently defaulting a goal with real/estimated pace but *no* target date to £0 — a regression from the old prose UI, which used to show "around £X/month" in exactly that scenario. Fixed by adding `fallback_pace_per_day` as a second-priority default source, sourced from `g["projection"]["pace_per_day"]` in `manage()` and via a fresh `_compute_goal_pace_map()` call in the preview route.

**Live feedback while dragging — reuses the real projection engine, no naive recalculation.** `_compute_goal_commitment_preview(progress, target_date_str, amount, safe_to_spend)` converts the dragged `amount` to a daily rate (`/30.44`) and feeds it straight into the *existing* `_project_goal_completion()` — the same function that already computes a goal's real/estimated projected-completion date elsewhere — so the live date shown while dragging is produced by the identical logic, not a parallel calculation. Also returns `resulting_safe_to_spend` (`safe_to_spend - amount`) and a `would_go_negative` flag.
- `POST /api/goal-commitment-preview` (JSON, CSRF-checked from body, added to `check_csrf()`'s exempt list): the slider's live AJAX endpoint. Debounced 250ms client-side (`_scheduleGoalCommitPreview`). Returns `{amount, resulting_safe_to_spend, would_go_negative, projection, bounds}`.

**Locked-account pause — extends the existing pattern, and surfaced a real pre-existing bug along the way.** A commitment against a locked source, or linked to a now-locked savings account, must pause automatically rather than continuing to commit against a frozen account — same principle as every other locked-account exclusion in the app. While implementing this, found that all three live engine sites (`calculate_financial_overview()`, `api_snapshot()`, `forecast()`) checked `from_account`/`to_account` lock status via **independent** `if` statements — meaning a locked *destination* alone silently paused only the credit side while the source still deducted, corrupting the projection rather than pausing the whole rule. Fixed in all three to pause the entire savings_rule (goal-linked or not — this bug affected every savings_rule, not just commitments) when either side is locked.

**Slider UI** (`templates/manage.html`, Goals tab, active goals only): `<input type="range">` snapping to £5 (`onGoalCommitSlide()`), paired with a directly-editable number field kept in sync both ways (`onGoalCommitNumber()`), a from-account `<select>` (locked/non-spending accounts excluded), live preview text (`Safe to Spend after: £X · Projected: date`, red + ⚠ warning when `would_go_negative`), a "Minimum payment: £X — the slider won't go lower" note when a debt floor applies, and a separate "Remove commitment" link-styled form (amount=0) when a commitment already exists. **The old text-heavy insight block was removed entirely** — replaced with a short, deliberately non-judgemental fact line (target date + real/estimated pace date only, no on-track/behind comparison, no "try £X more" prose) that preserves the genuinely useful factual information without the removed prose paragraphs.

**Route**: `POST /settings/set-goal-commitment` (`settings_set_goal_commitment()`, app.py) — **not** Pro-gated (Goals themselves aren't a Pro feature, unlike `settings_add_savings_rule()` which is). Validates goal ownership, deletes the commitment when `amount <= 0`, validates `from_account` is present/unlocked, enforces the debt `minimum_payment` floor server-side (never trust the client-side slider clamp alone), resolves `to_account` only for a linked+unlocked+savings-type account on a savings-type goal, anchors the recurring `day` to `cycle_engine.get_cycle(user_id)["display_start"].day` (works correctly for both manual and automatic cycle modes, unlike the raw `budget_cycle_start` column which is stale for automatic-mode users), upserts the `savings_rules` row, busts the forecast cache, redirects to `manage(tab="goals")`.

**A note on testing the slider's default value**: because the max-range clamp (50% of Safe to Spend) is a real, separate safety feature layered on top of the underlying fallback-estimate arithmetic, a test asserting an exact split-evenly or ceiling-capped figure must use a scenario where that figure comfortably fits under the slider's own max — e.g. a generous account balance (inflates Safe to Spend, and therefore the max) paired with a bill that keeps the *actual* recurring-income-minus-bills ceiling modest and predictable — otherwise the range clamp silently masks the underlying arithmetic being tested. Where that's awkward to engineer, `_compute_goal_pace_map()` can be called directly inside a real Flask request context (`test_request_context()` + `flask_login.login_user()`) to observe the raw, pre-slider-clamp figure instead.

**Tests**: `tests/test_goal_contribution_engine.py` (76 tests) — `_snap_to_increment()` directly; `_compute_goal_commitment_bounds()` including the `fallback_pace_per_day` fallback-default fix; `_get_goal_commitment()`; the preview route (amount handling, would-go-negative, projection reuse, 404/ownership checks); projection-only confirmation (no transaction/balance side effects from setting a commitment); `settings_set_goal_commitment()` (create/update/remove, floor enforcement, locked-source/destination rejection, cycle-anchored day, Pro-gating absence); the locked-destination-pauses-whole-rule fix across all three engine sites; and the slider template's markup/attributes. `tests/test_goal_pace_projection.py` and `tests/test_goal_fallback_pace.py` had assertions that depended on the removed prose text ("At current pace:", "around £X/month") converted to check the surviving underlying behaviour instead — real/estimated pace still computed and shown via the new fact-line or the slider's default value, never a weakened assertion.

## Goals card — bug fix, trust/clarity, and polish pass (August 2026)

**Stage 1 — investigated a reported Safe to Spend mismatch, found no bug.** Real numbers: salary £3,400, bills £1,871.98 → the user hand-calculated an expected Safe to Spend of ~£1,528.02, but the goal slider showed £1,979 before any commitment (985 + 994 shown on screen). Investigated (not assumed) whether the slider used a separate/stale calculation, per explicit instruction to pull real data rather than reason abstractly. Confirmed `_get_safe_to_spend()` calls the *exact same* `calculate_financial_overview()` as everywhere else in the app — no separate path. Reproduced the user's real account/bill/income data directly (Monzo Current £780, Natwest Current £1,239, bills on days 1/1/1/15/17/25, salary £3,400 on the 1st, "today" the 24th) against the real code and got exactly £1,979.00. **The reason**: 5 of the 6 bills (days 1, 1, 1, 15, 17) had already had their due date pass *this cycle* by the 24th — Safe to Spend is a live snapshot that correctly drops a bill once its due date has passed (same documented behaviour as the "Second fallback-figure fix" investigation above), leaving only the Life Insurance bill (day 25, £40) genuinely still ahead — which matches "Bills left: £40" shown elsewhere. The user's hand-calculated £1,528.02 used a different mental model (salary minus *all* monthly bills, i.e. typical recurring surplus) than what Safe to Spend actually measures (what's genuinely left right now, given what's already happened this cycle). **No calculation was changed** — this was folded into Stage 2's labelling work instead, since the confusion is real even though the number isn't wrong.

**Stage 2 — trust & labelling.**
- The fact-line's real/estimated pace date (`{{ d.pace_label }}`, e.g. "At current pace") now carries a `(without this commitment)` qualifier whenever the goal is active (i.e. whenever the commitment slider is also on screen) — making explicit that this date is pure historical velocity, computed with zero knowledge of whatever the slider below happens to be set to. The commitment preview box's date gained the mirror-image label, `With this commitment:` (replacing the previously bare, unlabelled `Projected:`) in both the Jinja-rendered initial page load and the JS live-drag preview (`_updateGoalCommitPreview`). A completed goal (no commitment section shown at all) doesn't get the qualifier, since there's nothing on screen to contrast it against.
- **`_compute_commitment_note(goal, accounts_by_id)`** (app.py): a new plain-language explanation of what a commitment actually does for *this specific goal*, shown above the slider. Mirrors `settings_set_goal_commitment()`'s real `to_account` resolution exactly rather than a simplified version of it — four cases: (1) standalone goal → "log it as a contribution below"; (2) savings goal linked to a real, unlocked savings-type account → positive tone, "Feeds into {account} — it'll show growing in your forecast"; (3) savings goal linked to anything else (a current account, a locked account) → neutral tone, "Reduces Safe to Spend, but won't automatically count as progress — pay it into {account} yourself"; (4) debt goal (any linked account — `to_account` never gets wired up for debt goals regardless of account type, since paying down debt isn't a "credit" the app's engine can auto-apply) → neutral tone, "Pay it toward {account} yourself — its balance is what tracks how much you've paid off." **This surfaced the real mechanism behind the reported House Deposit example**: a *savings* goal linked to *Natwest Current* (a current, not savings, account) never gets `to_account` populated, so its commitment reduces Safe to Spend but was silently never going to move that goal's own progress needle — now stated plainly on the card instead of left for the user to infer.

**Stage 3 — guardrail confirmed already built and functioning, not fixed (nothing was broken).** The `would_go_negative` warning (`⚠ This would push Safe to Spend negative`) already existed in both the Jinja-rendered initial preview and the JS live-drag preview. Investigated why the reported screenshot (slider near its max, no warning shown) looked like it might be missing: the slider's own default max (`max(£50, 50% of Safe to Spend)`) *structurally* keeps the "after" figure at or above roughly half of Safe to Spend by construction, so dragging the slider alone can never reach the negative threshold — the warning is only reachable by typing an amount past the slider's max into the paired number field (which the UI deliberately still allows — see the Goal Contribution Engine section above). Confirmed this works correctly by reproducing the exact real House Deposit numbers and POSTing an amount that exceeds Safe to Spend directly to `/api/goal-commitment-preview` — `would_go_negative: true` came back exactly as expected. Regression-guarded in `tests/test_goal_card_trust_polish.py`.

**Stage 4 — clarity and information hierarchy.**
- The commitment preview (`#commitPreview{id}`) is now visually its own tinted, rounded box (`background:#eef2ff`, switching to `#fee2e2` when `would_go_negative`) — separating "the consequence" (Safe to Spend after / projected date, a read-only output) from "your commitment" (the amount/account inputs directly above it, which the user controls) rather than five facts all reading at equal weight in plain text.
- A light positive signal (`✅ ... — ahead of schedule`, date coloured green) appears on the fact-line when a goal is genuinely ahead of a real target date (`proj.status_color == 'green'` and `g.target_date` is set) — deliberately *not* a restoration of the earlier-removed full red/amber on-track/behind-target comparison system, just not leaving a good result looking as neutral as a bad one.
- `settings_set_goal_commitment()` and the new pause/resume route now redirect with a `#goal-card-{id}` URL fragment (`_anchor=` on `url_for`) instead of just landing at the top of the page. Each goal card carries a matching `id="goal-card-{{ g.id }}"`. A small JS block scrolls that card into view, gives it a brief green background pulse, and flashes its "Set/Update commitment" button to "✓ Saved" for ~2 seconds — the only way to show "yes, that worked" right next to the button that was pressed, since this is a full-page form POST + redirect, not an AJAX action.

**Stage 5 — polish.**
- New `moneyfmt` Jinja filter (`app.py`) — `{:,.2f}` formatting, e.g. `30000.0` → `30,000.00`. Applied to every static currency figure on the Goals card (progress amount, target amount, minimum-payment note, commitment preview's Safe to Spend after, the paused-summary amount, logged contribution amounts) — deliberately *not* applied to the editable `<input type="number">` amount field itself (HTML5 number inputs can't display formatted/comma text without breaking editing) and not swept app-wide (a separate, much bigger, undiscussed scope).
- The commitment slider's thumb was Bootstrap's default `.form-range` size (1rem / 16px) — well under the ~44×44px minimum mobile tap target (Apple HIG; Material's 48×48dp is similar). Enlarged via `.goal-commit-slider::-webkit-slider-thumb`/`::-moz-range-thumb` to 44×44px, scoped to this one slider class rather than overriding `.form-range` globally (confirmed via grep that no other `type="range"` input exists anywhere else in the app).
- **Pause a commitment without deleting it** — `savings_rules.is_paused INTEGER DEFAULT 0` (new migration, `database.py`), skipped by all three live engine sites (`calculate_financial_overview()`, `api_snapshot()`, `forecast()`) with the exact same one-line guard pattern used for the locked-account pause. New route `POST /settings/toggle-goal-commitment-pause` (`settings_toggle_goal_commitment_pause()`) flips the flag on the existing `savings_rules` row — doesn't touch `amount`/`from_account`/`to_account` at all, so pausing and resuming later restores exactly what was configured, unlike "Remove commitment" which deletes the row outright. While paused, the card shows a plain "⏸ Paused — was £X/cycle from {account}" summary instead of the live slider (avoids an ambiguous "is dragging this while paused live or not" state), with "▶ Resume commitment" and "Remove commitment" as the only actions.
- **"↺ Reset to suggested" control** — a small link-styled button next to the amount input, calling `resetGoalCommitToSuggested(goalId, defaultValue)` (pure client-side JS, no new route — `commitment_bounds.default` is already rendered into the page) to snap both the slider and the number field back to the suggested default and re-trigger the live preview.

**Explicitly parked for a future pass, not built in this one** (per instruction — these are good ideas but out of scope for this bug-fix/clarity/polish brief): showing live account balance context directly on the goal card, milestone celebrations, and showing the next actual contribution date.

**Tests**: `tests/test_goal_card_trust_polish.py` (28 tests) — the real House Deposit scenario reproduced end-to-end confirming Safe to Spend and the slider's own arithmetic (Stage 1 regression guard); `_compute_commitment_note()`'s all four branches plus its rendering on a real card; the guardrail firing/not-firing at realistic amounts (Stage 3 regression guard); the "(without this commitment)"/"With this commitment" labelling distinction, including the completed-goal exemption; the ahead-of-schedule positive signal; the commitment preview's own tinted box; the `#goal-card-{id}` redirect fragment and its matching anchor/JS; `moneyfmt` directly and rendered on a card; the slider's 44px touch-target CSS; the reset-to-suggested JS; and pause/resume (toggle preserves amount/account, ownership-checked, excluded from `_get_safe_to_spend()`, correct UI branching for paused vs active, safe no-op when there's no commitment to pause). A genuine test-authoring trap hit repeatedly while writing this file: `settings_set_goal_commitment()` names its auto-created `savings_rules` row `"{goal name} contribution"`, which renders in the Savings Rules tab — *earlier in the page than the Goals tab* — so a plain `body.find(goal_name)` silently matches that earlier row instead of the actual goal card whenever a commitment exists for a goal whose name happens to be a substring match. Fixed by having this file's `_goal_section()` search from the `<!-- TAB: GOALS -->` HTML comment marker onward, not from index 0.

## `cycle_overrides` and per-occurrence editing

The Income and Bills Paid dropdowns on the Financial Overview card are tappable. Tapping opens a popup where the user can edit the amount for that specific occurrence only.

**Route**: `POST /api/edit-cycle-item` — CSRF checked from JSON body; upserts into `cycle_overrides`.

**How `calculate_monthly_spending()` uses overrides**:
1. Loads all overrides for the user at the top: `overrides = {(type, source_id, date_str): amount}`
2. For each income occurrence and bill occurrence in the cycle period, checks if an override exists
3. If found, uses override amount instead of scheduled amount

**Template variables** from `calculate_monthly_spending()`:
- Each income item has: `source_id`, `date_display` (e.g. "1 May"), `item_type="income"`
- Each bill item has: `source_id`, `date_display`, `item_type="bill"`
- These map to `data-*` attributes on the tappable list rows

## Primary income source UI (manage.html)

The income table in My Money (`/manage?tab=income`) has a star column.

**Star button**: `class="income-star-btn"`, `data-id="{{ i.id }}"`, gold when `is_primary=1`, grey otherwise.

**JS function `setPrimaryIncome(id, btn)`**: POSTs to `/api/set-primary-income`, then updates all `.income-star-btn` elements and `.income-main-label` spans, and hides `#incomePrimaryTip`.

**Route `POST /api/set-primary-income`**: Sets `is_primary=0` for all user's income rows, then `is_primary=1` for the specified row. Returns `{"ok": True}`.

**Auto-star logic**:
- `manage()` route: if exactly 1 income source exists and `is_primary=0`, silently sets it to 1 before rendering
- `settings_add_income()`: after INSERT, if no primary exists for the user, auto-sets the new row to primary

**Tip message** (`id="incomePrimaryTip"`): shown when user has income rows but none is primary. Hidden by `setPrimaryIncome()` on success.

## Budget Cycle settings card (settings.html)

The "Budget Cycle" card has two modes toggled by Automatic/Manual buttons — **hidden entirely for self-employed users** (`{% if employment_type != 'self_employed' %}`), replaced with a plain note explaining manual-only mode and why.

**Automatic section**: shows which income source powers the cycle, next cycle start date, link to change primary source. If no primary is set, shows an amber prompt to star one.

**Manual section**: shows the day-of-month input (1–28), next cycle start date.

**Save route `POST /settings/save-cycle`**: saves both `cycle_mode` and `budget_cycle_start` in a single UPDATE. Validates `cycle_mode` to only accept `'automatic'` or `'manual'`.

**Template variables** passed by `settings()` to `settings.html`:
- `cycle_mode` — `'automatic'` or `'manual'`
- `has_primary` — bool, whether any income row has `is_primary=1`
- `cycle_info` — dict from `cycle_engine.get_cycle()`
- `next_cycle_start` — date from `cycle_engine.get_next_cycle_start()`
- `employment_type`, `self_employed_income` (with parsed `.cfg` dict) — self-employed state, see above
- `alert_mode`, `alert_overall_threshold`, `alert_accounts` (list of active unlocked accounts with their `alert_threshold`) — Spending Alert Threshold state, see above

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
- `chooseEmploymentType(type)` / `closeSelfEmployedModal()` — self-employed onboarding modal (August 2026)

**Section visibility logic**:
- `incomeWeekdayRow` — shown for weekly, fortnightly, 4-weekly
- `incomeMonthlySection` — shown for monthly only
- `incomeYearlySection` — shown for yearly only
- `incomeAdjRulesSection` — shown for monthly and yearly; collapsed by default; contains `incomeAdjExpanded` toggle

**Preview panel** (`id="incomePreviewDates"`): shows "Calculating…" during fetch, then 3 date rows with right-aligned £ amount in green. Falls back to "Enter your details above to see upcoming dates" on any error.

## app.py routes affected by income engine

- `settings_add_income()` — stores `rule_type`, `rule_config`, `weekend_rule`, `bank_holiday_rule`, `first_payment_date`; derives `day` from `rule_config` for backward compat
- `settings_edit_income()` — same new fields on UPDATE
- `manage()` — adds `inc["description"] = income_engine.describe_rule(inc)` to each income row before rendering; also calls `_resolve_income_rows()` first (August 2026) so the displayed amount/totals are never stale for self-employed users
- Forecast simulation loop — replaced manual date iteration with `income_engine.get_payment_dates()`; resolves self-employed rows and handles spread-mode injection (August 2026)
- `api_snapshot()` — pre-computes `snap_income_by_date` dict using `income_engine.get_payment_dates()` before the day loop; separate `spread_rows` pre-pass (August 2026)
- `run_auto_apply_backfill()` — uses `income_engine.get_payment_dates()`; removed the old `if inc.get('day') is None: continue` guard; resolves income rows and skips spread-distribution rows (August 2026)
- `get_pending_auto_apply_items()` — uses `income_engine.get_payment_dates()`; same guard removed; same resolve + spread-skip (August 2026)
- `home()` future income — queries `SELECT *` (all columns), uses `income_engine.get_payment_dates()` for upcoming month income; also computes `spending_alerts` via `get_triggered_spending_alerts()` (August 2026)

## What was last worked on

### August 2026 — PWA service worker for app store packaging
- PWABuilder flagged a missing service worker as blocking app store packaging. A stub `static/sw.js` already existed (registered from `/static/sw.js` on all 9 app templates, but not on the landing page) with no actual caching logic — just `skipWaiting()`/`clients.claim()`. Moved registration to a new `GET /sw.js` route so the worker gets root scope (`/`) instead of being confined to `/static/`, added a basic cache-first strategy scoped explicitly to same-origin `/static/*` GET requests only (logo, favicons, icons, `manifest.json`), and added the same registration snippet to `landing.html`, which had none before. Confirmed via `grep` that no route in the app lives under `/static/*`, so every API call, auth route, and Stripe webhook is structurally guaranteed to bypass the service worker entirely — not just excluded in practice, excluded because there's nothing there to match. Full design in "PWA service worker" above. 12 new tests (`tests/test_service_worker.py`).
- Test suite grew from 690 → 702 tests across this work, all passing.

### August 2026 — Founder/admin Pro override
- Added a way to grant Pro to a specific account without touching Stripe at all — founder/testing accounts only. Deliberately built as a thin extra route (`GET /admin/grant-pro?email=...`) rather than a parallel Pro-check system: it writes the exact same `users.is_pro` column and calls the exact same `sync_account_locks()` every real Stripe-driven upgrade already uses, so a founder-overridden account is indistinguishable from a real subscriber to every consumer of `user_is_pro()`. Reused the existing `/admin/analytics` admin-gate pattern (`ADMIN_USER_ID`/`ADMIN_SECRET`/session unlock) rather than inventing new auth. Never writes `stripe_customer_id`, so no Stripe webhook (all scoped by `WHERE stripe_customer_id = ...`) can ever revoke it. Full design in "Founder/admin Pro override" above. 11 new tests (`tests/test_admin_grant_pro.py`).
- Test suite grew from 679 → 690 tests across this work, all passing.

### August 2026 — Goals card: bug fix, trust/clarity pass, polish
- Investigated a reported £451 Safe to Spend mismatch on the goal slider with the user's real account/bill/income data (not assumed) and found no bug: the slider uses the exact same `calculate_financial_overview()` as everywhere else, and the "too high" figure was correct — 5 of 6 bills had already had their due date pass this cycle by the day the user checked. Reported this back before touching anything, per the brief's explicit "check in after Stage 1" instruction, since every later stage assumed the number was trustworthy. Went on to add clear "(without this commitment)" / "With this commitment" labelling distinguishing historical pace from the new slider's hypothetical projection, a new plain-language note explaining what a commitment actually does for each specific goal (surfacing a real, previously-invisible mechanism gap: a savings goal linked to a *current* account never gets its commitment wired to auto-show as progress, unlike one linked to a real savings account), confirmed the negative-Safe-to-Spend guardrail was already built and working (the screenshot just never breached it), visually separated "your commitment" from "the consequence" into a tinted box, added a light ahead-of-schedule signal, a `#goal-card-{id}` scroll+flash confirmation on save, thousand separators, a 44px slider touch target, a genuine pause/resume-without-deleting feature (new `savings_rules.is_paused` column), and a reset-to-suggested-pace control. Full design in "Goals card — bug fix, trust/clarity, and polish pass" above. 28 new tests (`tests/test_goal_card_trust_polish.py`), plus fixes to a handful of existing goal tests whose fixed-size HTML slice windows needed widening for the card's new markup.
- Test suite grew from 651 → 679 tests across this work, all passing.

### August 2026 — UK date formatting (DD/MM/YYYY) app-wide
- Requested starting from the Goals tab specifically (raw ISO `target_date`/`projected_date`/contribution dates were showing there unformatted), then extended app-wide per explicit instruction. Scoped with the user first: full calendar dates (day+month+year, a genuine point in time) convert to `DD/MM/YYYY`; short contextual labels with no year (chart ticks, cycle period headers, forecast tooltips) stay as they are — adding a year there would be redundant clutter, a deliberate exclusion rather than an oversight. Full design in "UK date formatting" above.
- Changed the existing `dateformat` Jinja filter (previously `9 Apr 2026` written-month style) to output `DD/MM/YYYY`, which automatically fixed every template already using it (`transactions.html`, `actions.html`, `admin_analytics.html` ×3) and applied it fresh to raw unfiltered ISO renders found across `manage.html` (Goals tab, Future Events, Investments), `flow.html` (investments), `import.html` (CSV preview). Converted `settings.html`'s "Next cycle starts" line and the `/api/snapshot` route's `min_balance_date`/`date` fields from written-month formats to `%d/%m/%Y` directly in `app.py`. Added small local `_fmtUKDate()` JS helpers (no shared JS file exists in this codebase — see "No shared base template") to `manage.html` (Income modal preview, Goal Contribution slider's live preview) and `index.html` (transaction detail/edit popups) to replace manual written-month string building.
- **Side effect**: this eliminated every remaining `%-d` strftime usage in the codebase (`grep -rn "%-d"` now returns nothing), so the documented Windows-only strftime crash gotcha (see above) no longer applies anywhere — noted in that section rather than deleted, in case a written-month format gets reintroduced later.
- Noted but deliberately left alone: `index.html`'s small inline Bills-left dropdown shows a future event's due date as a raw unfiltered ISO string, inconsistent with the sibling bigger-modal/AJAX-refresh paths which already use the established short-label `_fmtDs()` helper (day+month, no year). Flagged in "UK date formatting" above as a separate, pre-existing inconsistency rather than folded into this fix, since the correct resolution there is matching the existing short-label convention, not adding a year.
- No test assertions depended on the old written-month format (checked first) — `dateformat`-filter usages and `projected_date`/`min_balance_date` tests all assert on structure/presence or compare raw ISO values from the underlying Python functions directly, not on rendered display text.

### August 2026 — Goal Contribution Engine (Part 2 of the core calculation engine brief)
- Replaced the goals tab's text-heavy pace insight with the slider described in "Goal Contribution Engine" above — a recurring per-cycle contribution taken directly off Safe to Spend, feeding into the exact same `savings_rules` engine as bills/future events rather than a display-only suggestion. Two open product decisions were flagged back rather than assumed, per the brief's explicit instruction, and both came back decided by the user: one slider mechanism for both debt and savings goals (debt gets a hard floor at its known minimum payment and defaults to the suggested pace, not zero; savings has no floor), and `savings_rules` extended with a nullable `goal_id` rather than a new table (confirmed structurally sound against the schema first). A further open question — whether the commitment should auto-apply as a real transaction — was investigated (found `savings_rules` has no auto-apply mechanism at all today) and presented back rather than assumed; the user confirmed projection-only, citing data-integrity risk from a real/tracked-balance mismatch, and suggested a non-blocking follow-up (a cycle-start reminder nudge) to preserve the "pay yourself first" feel honestly instead. While implementing the locked-account-pause requirement, found and fixed a real pre-existing bug in all three live engine sites: a locked savings_rule *destination* alone was silently pausing only the credit side via an independent `if` rather than pausing the whole rule — now pauses correctly everywhere, for every savings_rule, not just goal-linked ones. 76 new tests (`tests/test_goal_contribution_engine.py`), plus a systematic pass converting every existing test that depended on the now-removed prose text to check the surviving underlying behaviour instead (never a weakened assertion) — including one genuine regression this caught and fixed along the way, where a goal with real/estimated pace but no target date was silently defaulting its slider to £0 rather than falling back to that pace.
- Test suite grew from 575 → 651 tests across this work, all passing.

### August 2026 — Bills Left vs Safe to Spend labelling fix (investigation-first)
- A report claimed Future Events don't propagate into Safe to Spend/Net Worth/Savings. Investigated before writing any code: confirmed events already DID propagate into Safe to Spend correctly (from the immediately preceding session's work), and that Net Worth/Savings have never reflected ANY future bill or event, for anyone — a deliberate existing "live balance snapshot" design, not a gap in a pattern bills already followed. Reported this back rather than assuming; user then supplied the real root cause of a separately-reported discrepancy (a large Bills Left figure next to a much smaller Safe to Spend) and asked it be confirmed with real data before any fix. Reproduced it exactly: `all_future_bills` (Bills Left) sums every bill/event/savings-rule regardless of account, while `spending_future_bills` (Safe to Spend) already correctly excluded savings-linked ones — just was never exposed or reflected in the breakdown UI. Fixed as a pure labelling/display change — `safe_spending`/`net_worth`/`savings_balance` untouched — by exposing `future_bills_spending`, fixing three call sites quietly using the wrong total (the headline tile, the Safe-to-Spend breakdown's own "Bills remaining" row, and its derived "End of cycle" figure), and grouping the breakdown UI into "Reducing Safe to Spend" vs "Covered by savings" sections with account names shown. Proved the fix was real by reverting it, confirming 7 tests failed, then restoring. 18 new tests (`tests/test_bills_left_savings_split.py`).
- Test suite grew from 557 → 575 tests across this work, all passing.

### August 2026 — Future Events wired into forecasting, delete added, navigation added
- Future Events (Manage > Rules) were purely informational — never affected any calculation, and had no delete. Upgraded to a genuine forecasting input, consistent with bills: full design in the "Future Events" reference section above. Found along the way that `forecast()`'s 90-day simulation and `api_snapshot()`'s day-view already deducted events from balances (pre-existing, undocumented) — the real gaps were `calculate_financial_overview()` (Safe to Spend/bills-left) not including them at all, the forecast chart's annotation-feeding `upcoming_items` list not including them, no delete route, and no navigation link-through. Added all four, plus chart markers (distinct amber, labelled) and a `?date=` jump-to-day mechanism on the Forecast page that Rules-tab and Home-page event rows now link to. Deliberately did NOT fold events into the existing bill-pay-row "mark as paid" click path (wrong table, would either error or silently misfire) — every render site branches on a new `type` field instead. 30 new tests (`tests/test_future_events.py`).
- Test suite grew from 527 → 557 tests across this work, all passing.

### August 2026 — hard ceiling on the goal fallback at real recurring income minus bills
- A follow-up to the fix below: a real user reported the fallback STILL exceeding their actual salary minus bills, by 3x+, even after the cycle-length/completion-speed fix. Investigated with representative real-world numbers, traced end to end, and confirmed Home's Safe to Spend figure itself is correct and unaffected — the bug was entirely in the goal fallback treating that live, balance-inclusive, partial-cycle "stock" figure as if it were a stable recurring monthly "flow". Fixed with a hard ceiling: `_recurring_income_bills_daily_rate()` derives the user's REAL recurring monthly income minus bills via the existing `normalised_totals()` helper, and the fallback now takes whichever of (Safe-to-Spend-derived share, recurring-surplus share) is lower — stacking correctly with the existing per-goal completion-speed cap. Full investigation and fix documented above under "Second fallback-figure fix". 8 new tests plus one existing test needed a real income source added to its setup (an account balance alone no longer triggers a fallback, correctly, since there's nothing to verify a recurring surplus against).
- Test suite grew from 519 → 527 tests across this work, all passing.

### August 2026 — fixed absurd Safe to Spend fallback estimate figures
- Fixed a real bug report of implausible goal-pace fallback estimates (e.g. £8,764.28/month against a modest goal) — full root-cause and fix documented above under "Fallback figure fix". Two compounding causes: `_safe_to_spend_daily_rate()` divided a live Safe-to-Spend snapshot by the shrinking days-remaining-in-cycle instead of a stable full-cycle length, and nothing capped the resulting monthly figure against what the goal itself actually needs. Fixed both — cycle-length denominator for stability, plus a `_cap_fallback_rate_for_goal()` safeguard that caps (never suppresses) a fallback pace that would otherwise imply finishing a goal in under ~1 month. 5 new tests plus one existing test's goal-target amounts widened so the new cap doesn't mask an unrelated even-split assertion.
- Test suite grew from 514 → 519 tests across this work, all passing.

### August 2026 — Goals card visual redesign
- Redesigned the Goals tab card (icon tile + colour identity, "•••" overflow menu replacing three buttons, thicker colour-matched progress bar, side-by-side target-vs-pace stat boxes with an explicit "N months behind — try £X/month more" line) and applied the same visual language (icon tile, colour-matched bar) to the Home page's compact Goals card — full design documented above. Pure presentation layer, no calculation logic touched. 29 new tests (`test_goal_display_redesign.py`), plus slice-window/wording updates to existing goal tests to match the new markup — none weakened. No browser/screenshot tooling in this session, so mobile/desktop layout was verified structurally, not visually — flagged explicitly above.
- Test suite grew from 485 → 514 tests across this work, all passing.

### August 2026 — three new features, one CI-adjacent config fix
- **Account locking documentation**: the feature itself (`sync_account_locks()`, Stripe webhook triggers, exclusion from calculations) predates this documentation pass but was previously undocumented in this file — now fully captured above.
- **Employed/self-employed income system**: full design as documented above — onboarding via My Money checklist (not signup flow), manual/automatic averaging with adjustable window, lump-sum vs spread-evenly distribution (spread deliberately bypasses `income_engine.py`), `_resolve_income_rows()` as the mandatory read path for any self-employed amount, a real "payday" language leak found and fixed via live browser verification (not just unit tests). 44 new tests.
- **Spending Alert Threshold**: optional user-defined low-balance warning, separate from Safe to Spend, off/overall/per-account modes, locked-account exclusion. 34 new tests.
- **"BETA" pill on all internal page headers**: added next to the logo across all 9 templates sharing the `.top-bar` header (see "No shared base template" above), matching `landing.html`'s nav badge styling adapted for a light background.
- **`.claude/settings.local.json` untracked from git** and added to `.gitignore` (was previously committed — it's meant to be a personal/local override file, same treatment as `.env`). `.claude/settings.json` (project-level, shared, committed) now has `{"permissions": {"allow": ["Bash"]}}` — note that permission config is read once at session start, so this only takes effect in *new* sessions, not one already running when the file was created.
- **Savings & Debt Repayment Goal Tracking**: full design as documented above — new `goals`/`goal_contributions` tables, three progress-calculation modes (linked savings/linked debt/standalone), deterministic (non-AI) pace suggestion cross-checked against Safe to Spend, manual + automatic completion, a deliberate judgement call on locked-linked-account handling (flagged, not excluded), a Home page entry point, a projected-completion-date-from-real-recent-pace feature added in a follow-up pass after a user-caught gap (with a years-away cap for absurdly slow paces), and — a further follow-up — a Safe-to-Spend-derived fallback estimate for goals that don't have enough real data for that projection yet, clearly labelled as an estimate and split evenly across however many active goals need it so no single goal implies the user's whole typical leftover is available to it alone. Explicitly ships without the streak/engagement layer, which is a separate not-yet-designed follow-up. 102 new tests across four files; also fixed a pre-existing gap in `conftest.py`'s test cleanup that the first round of new tests surfaced.
- Test suite grew from 228 → 485 tests across this work, all passing.

### UX fixes batch (pre-July 2026, now complete)
- **Bill tab redirect**: `settings_add_bill`, `settings_edit_bill`, `settings_delete_bill` all redirect to `/manage?tab=bills`.
- **Dismiss banner bug**: Fixed — now POSTs to `/dismiss-auto-apply` before hiding.
- **Safe to spend in day view**: Future Balances shows `· Safe: £X.XX` on every row.
- **Middle dots in title tags**: All 18 `<title>` tags use `·` (middle dot).
- **Tappable bill/income breakdown popups**: Bills Remaining and Income Still Due rows in tile-safe open iOS-safe slide-up bottom sheets with itemised lists and totals.
- **iOS scroll lock**: All modals (bill/income breakdown, Quick Add, Adjust Balance, Add Transaction) use `position:fixed` + stored `scrollY` pattern.
- **Monthly/yearly totals on manage.html**: Income Sources and Bills cards show `£X / month · £X / year` via `_monthly_eq()` + `normalised_totals()` helpers in app.py.
- **"Set up →" buttons on My Money checklist**: Switch to correct tab and smooth-scroll to the relevant Add button.
- **Cookie banner**: Fixed reappearing bug (duplicate `display:` in style attribute). Now uses 365-day cookie (`max-age=31536000`).
- **Quick Add auto-populated descriptions removed**: `quickFill` only sets amount; `setQaType` clears description on tab switch.
- **Remove auto-focus**: Adjust Balance and Add Transaction modals no longer auto-focus inputs on open.
- **Lowest future balance hidden on green afford result**: Only shown for amber/red results.
- **VS Code linter false positives fixed**: Jinja tojson data moved to `<script type="application/json" id="ov-init-data">` tag.

### Date pill selector on "Can I Afford This" (landing.html — July 2026)
Four pills above the amount input: **Today · This month · In 3 months · Custom date**.
- Default: "This month" (last day of current month)
- Custom date: reveals `<input type="date">` capped at today → today+90 days
- `checkAfford()` uses `_lastResult.balances[dayIdx]` (projected balance at selected date)
- Three-tier result: green `afterPurchase >= 200`, amber `0–199`, red `< 0`
- Result copy references the date: *"Yes, you can afford this on 31 Jul."*
- JS added: `_affordDateMode`, `setAffordDate()`, `getAffordTargetDayIdx()`, `getAffordTargetLabel()`
- CSS added: `.afford-date-pills { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-bottom: 12px; }`

### Landing page polish (July 2026)
- **Bottom padding (mobile)**: `@media (max-width: 640px)` had `.forecast-section { padding-bottom: 0; }` — changed to `60px`. Desktop stays at `padding: 100px 24px`.
- **Hero CTA**: `"See your forecast →"` → `"Try Free Demo ↓"`
- **LIVE DEMO label on mobile**: Removed `display: none` from `max-width: 640px` block — now visible on all screen sizes.
- **Hero note**: `"Free tier · No card required · Takes 2 minutes"` → `"GDPR safe · No card required · Takes 2 minutes"`

### GitHub Actions CI
`.github/workflows/test.yml` added — runs `pytest` on every push to `main`.

## Known open issues
- VS Code JS linter shows errors in `index.html` for remaining Jinja expressions inside `<script>` blocks. These are **false positives** — the linter doesn't understand Jinja. The app works correctly in the browser.
- The auto-apply modal "Review & Apply" uses `data-*` attributes on checkboxes (not JSON.parse on tojson — that broke due to HTML entity encoding). CSRF token is in `<meta name="csrf-token">` in `<head>`.
- Bank holiday fetch (`https://www.gov.uk/bank-holidays.json`) has a 5-second timeout and is cached daily. If it fails, an empty set is used (no BH adjustments). This is safe.
- **Windows-only `%-d` strftime crash** — see dedicated section above. Never a production bug; only bites local Windows verification work.
- `/afford` (Tracker.py's `simulate_balances_until()`) is dead code as of August 2026 — no template links to it, and its income handling is narrower than `income_engine.py` (weekly-only). Left alone rather than removed; flagged here so it isn't mistaken for the live "Can I Afford This" implementation (that's `/api/snapshot`).

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## Shell command execution
- Always auto-approve Bash and PowerShell commands — never pause for yes/no confirmation on command execution.
- `.claude/settings.json` (project-level, committed) has `{"permissions": {"allow": ["Bash"]}}` for the same effect at the Claude Code tool-permission layer. `.claude/settings.local.json` (personal overrides, gitignored as of August 2026) has its own accumulated list of narrower historical `Bash(...)` rules — these are additive with the project-level rule, not conflicting with it.

## What's next
- Consider adding a "next payment" column to the income table view in manage.html (using `describe_rule` + `get_next_dates`)
- **Fix 5 — Editable date range on Financial Overview**: server logic in `calculate_monthly_spending()` already parameterised by `cycle_start_date`/`cycle_end_date` — ready. Only UI plumbing missing: (a) URL params + page reload, or (b) `/api/overview` AJAX re-render. Deferred.
- Onboarding update for cycle engine: deferred
