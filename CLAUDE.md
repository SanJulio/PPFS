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

## Windows-only `%-d` strftime gotcha (verification harnesses only — never touches the actual app)
Some templates use `strftime('%-d %B %Y')` (Linux/macOS-only day-of-month format, no leading zero) — e.g. `templates/settings.html`'s "Next cycle starts" line, fed by `cycle_engine.get_next_cycle_start()`. This works fine on production (Render runs Linux) but **crashes with `ValueError: Invalid format string` on Windows** (the Windows CRT only supports `%#d` for the same effect). This has been hit multiple times during local verification work on this Windows dev machine.
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
- `tests/` — pytest suite (422 tests, all passing); `conftest.py` has SQLite schema matching production, including all August 2026 columns
  - `tests/test_account_locking.py`, `tests/test_locked_account_exclusion.py` — account locking
  - `tests/test_self_employed.py` — employed/self-employed income system (44 tests)
  - `tests/test_spending_alerts.py` — Spending Alert Threshold (34 tests)
  - `tests/test_goals.py` — savings & debt repayment goal tracking (39 tests)

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

**Account locking interaction — a deliberate judgement call**: a goal linked to a now-locked account (Pro-to-Free downgrade, see Account Locking above) is **not** excluded, hidden, or deleted. Its `current_balance` read is already the frozen figure (locked accounts are frozen at the DB level by the existing locking design, so no special handling is needed to "freeze" it a second time) — `_compute_goal_progress()` just surfaces an `account_locked: True` flag alongside the (frozen) progress figure, and `templates/manage.html`'s goal card shows a "🔒 progress may be stale" note next to the linked account name, consistent with how locked accounts are flagged everywhere else in the app (manage.html's account list, every account `<select>`) rather than presenting a frozen number with the same confidence as live data. **A *new* goal can still be linked to an already-locked account** — deliberately allowed rather than blocked, since reading a locked account's balance is read-only and harmless, unlike the bill/income/transfer routes that block against locked accounts because those would attempt to *write* new activity against them.

**Routes** (all in `app.py`, all `@login_required`):
- `POST /settings/add-goal`, `POST /settings/edit-goal` — the latter re-snapshots `starting_balance` on any linked-account change (see above)
- `POST /settings/delete-goal` — also deletes the goal's `goal_contributions`; never touches `accounts`/`transactions`
- `POST /settings/complete-goal` — toggles `active` ⇄ `completed`
- `POST /settings/add-goal-contribution` — rejected (redirect with a message, not a hard error) if the goal is linked, since a linked goal's progress already comes from the real account balance and a logged contribution there would double-count
- `POST /settings/delete-goal-contribution`
- `POST /api/goal-pace-preview` — see above

**UI** (`templates/manage.html`): new "🎯 Goals" tab alongside Accounts/Bills/Income/Rules, following the same list-card pattern as the rest of the page. Uses a **single shared Add/Edit modal** (`#goalModal`) rather than per-row inline edit forms — closer to how the Income modal already works in this file than to the simpler inline-row pattern Bills/Savings-Rules use, because goals have enough fields (name, type, target amount, target date, linked account) to justify it, and it's the natural place to host the live pace-preview panel for both creating and editing. Edit-population uses `var _goalsData = {...}` built via an explicit per-field Jinja loop (**not** `{{ goals|tojson }}` on the whole list) — the `goals` list carries Postgres `NUMERIC` values (which arrive as non-JSON-serialisable `Decimal` via psycopg2) and `TIMESTAMP` columns (which arrive as `datetime` objects), both explicit landmines for `tojson`; the explicit-field approach only ever emits values already cast to plain Python `float`/`str`/`None` in `manage()`, sidestepping the issue entirely rather than needing a fix for it. Standalone goals show an inline logged-contributions list with a "+ Log contribution" button opening its own small modal (`#goalContributionModal`).

**Tests**: `tests/test_goals.py` (39 tests) — creation (both types × linked/standalone × with/without target date, validation), progress calculation for all three modes (including the debt-goal test confirming it works under both negative-balance and positive-"amount-owed" conventions), pace suggestion (present only with a target date, overdue handling, Safe to Spend warning threshold), edit/delete/manual-and-automatic completion (including the starting-balance re-snapshot-on-relink behaviour), and the locked-linked-account interaction. Uncovered a pre-existing test-infrastructure gap along the way: `tests/conftest.py`'s `test_user` fixture teardown deleted from every per-user table *except* the two new ones — fixed by adding `goal_contributions`/`goals` to that cleanup list, which matters because the test DB is session-scoped (shared across the whole test run), so leftover rows from an earlier test with a generic goal name could otherwise be picked up by a later, unrelated test.

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

### August 2026 — three new features, one CI-adjacent config fix
- **Account locking documentation**: the feature itself (`sync_account_locks()`, Stripe webhook triggers, exclusion from calculations) predates this documentation pass but was previously undocumented in this file — now fully captured above.
- **Employed/self-employed income system**: full design as documented above — onboarding via My Money checklist (not signup flow), manual/automatic averaging with adjustable window, lump-sum vs spread-evenly distribution (spread deliberately bypasses `income_engine.py`), `_resolve_income_rows()` as the mandatory read path for any self-employed amount, a real "payday" language leak found and fixed via live browser verification (not just unit tests). 44 new tests.
- **Spending Alert Threshold**: optional user-defined low-balance warning, separate from Safe to Spend, off/overall/per-account modes, locked-account exclusion. 34 new tests.
- **"BETA" pill on all internal page headers**: added next to the logo across all 9 templates sharing the `.top-bar` header (see "No shared base template" above), matching `landing.html`'s nav badge styling adapted for a light background.
- **`.claude/settings.local.json` untracked from git** and added to `.gitignore` (was previously committed — it's meant to be a personal/local override file, same treatment as `.env`). `.claude/settings.json` (project-level, shared, committed) now has `{"permissions": {"allow": ["Bash"]}}` — note that permission config is read once at session start, so this only takes effect in *new* sessions, not one already running when the file was created.
- **Savings & Debt Repayment Goal Tracking**: full design as documented above — new `goals`/`goal_contributions` tables, three progress-calculation modes (linked savings/linked debt/standalone), deterministic (non-AI) pace suggestion cross-checked against Safe to Spend, manual + automatic completion, and a deliberate judgement call on locked-linked-account handling (flagged, not excluded). Explicitly ships without the streak/engagement layer, which is a separate not-yet-designed follow-up. 39 new tests; also fixed a pre-existing gap in `conftest.py`'s test cleanup that the new tests surfaced.
- Test suite grew from 228 → 422 tests across this work, all passing.

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
