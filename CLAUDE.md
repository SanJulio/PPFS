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

## Key technical decisions
- **Sessions**: Custom `PostgresSessionInterface` in `flask_sessions` table. Do NOT use connection pooling — it caused connection exhaustion crashes.
- **DB connections**: `get_db()` opens a fresh connection per request, `release_db()` closes it. Import pattern: `from database import get_db, USE_POSTGRES` inside route functions.
- **CSRF**: Manual implementation — `session['csrf_token']` checked on all POST routes via a `before_request` hook that reads `request.form.get('csrf_token')`. JSON API routes that do their own CSRF check in the handler must be added to the `exempt` list in `check_csrf()`. Current exempt list: `['/login', '/register', '/stripe/webhook', '/auto-apply', '/mark-bill-paid', '/dismiss-auto-apply', '/api/income-preview', '/api/edit-pending-item', '/api/edit-cycle-item', '/api/set-primary-income']`. The `<meta name="csrf-token">` tag is in `<head>` on all pages; all forms have a `<input type="hidden" name="csrf_token">`.
- **Forecast**: 90-day single-pass simulation in `Tracker.py → simulate_balances_until()`. Results cached 5 minutes per user. Now uses `income_engine.get_payment_dates()` for income date calculation.
- **Snapshot API**: `/api/snapshot?days=N` — lightweight day-by-day simulation. Uses `income_engine.get_payment_dates()` to pre-compute income dates before the sim loop.
- **Auth**: Flask-Login. Email verification required. Password reset via Brevo.
- **Analytics**: Self-hosted at `/admin/analytics` (no third-party trackers).
- **Income engine**: All income date calculations go through `income_engine.py`. Legacy rows (`rule_type = NULL`) use the old day/weekly_day path with no weekend/BH adjustments. New rows use the full engine.
- **Cycle engine**: `cycle_engine.get_cycle(user_id, today=None)` is the single source of truth for a user's current budget period. Returns `display_start`, `display_end`, `safe_boundary`, `mode_used`, `primary_source_name`. All fallbacks are silent — always returns a valid dict. Never modify `income_engine.py` from cycle engine changes.

## Database tables (11)
`users`, `accounts`, `transactions`, `scheduled_expenses`, `income`, `savings_rules`, `future_events`, `flask_sessions`, `investments`, `investment_updates`, `cycle_overrides`

### `income` table — columns added May 2026
Five columns were added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db()` in `database.py`:
- `rule_type TEXT` — `NULL` (legacy), `'fixed_date'`, `'last_working_day'`, `'nth_weekday'`, `'relative_month_end'`
- `rule_config TEXT DEFAULT '{}'` — JSON string with rule-specific params (e.g. `{"day": 25}`, `{"nth": "last", "weekday": 4}`)
- `weekend_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `bank_holiday_rule TEXT DEFAULT 'before'` — `'before'` | `'after'` | `'nearest'`
- `first_payment_date TEXT` — ISO date string; anchor for fortnightly/4-weekly; shown as "next payment date" in the UI

## Key files
- `app.py` — all routes (~4800+ lines)
- `income_engine.py` — canonical payment date engine (see below)
- `cycle_engine.py` — **NEW (May 2026)**: budget cycle calculator (see below)
- `Tracker.py` — `simulate_balances_until()` and legacy CSV code (only simulate function is used)
- `models.py` — SQLAlchemy-free model helpers
- `database.py` — `get_db()`, `release_db()`, `USE_POSTGRES` flag; `init_db()` runs migrations
- `templates/index.html` — home/dashboard (largest template)
- `templates/manage.html` — bills, income sources, savings rules, future events; Add/Edit Income modal lives here
- `templates/forecast.html` — 90-day chart + insights
- `templates/landing.html` — public landing page for unauthenticated visitors
- `templates/settings.html` — plan/billing, display prefs, danger zone; Budget Cycle card has Automatic/Manual toggle
- `templates/transactions.html` — transaction list + category totals
- `tests/` — pytest suite (228 tests, all passing); `conftest.py` has SQLite schema matching production

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
- `manual` — uses `budget_cycle_start` day from `users` table. Replicates original `get_cycle_dates()` behaviour exactly.

**Fallback chain** (all silent, never visible to user):
- automatic → no primary source → fall back to manual
- automatic → no past payment dates → fall back to manual
- automatic → any exception → fall back to manual
- manual → no day stored → use day 1 of current month

**Weekly extension**: `display_end` is extended to at least `display_start + 29 days` for high-frequency income (weekly/fortnightly). `safe_boundary` stays as the next real payday - 1.

**Users table columns** (added May 2026):
- `cycle_mode TEXT NOT NULL DEFAULT 'manual'` — `'automatic'` or `'manual'`
- (existing) `budget_cycle_start INTEGER NOT NULL DEFAULT 1`

**Income table column** (added May 2026):
- `is_primary INTEGER NOT NULL DEFAULT 0` — exactly one income row per user should have this set to 1 for automatic mode

**cycle_overrides table** (added May 2026):
- `(user_id, type, source_id, date, amount)` with `UNIQUE(user_id, type, source_id, date)`
- `type` is `'income'` or `'bill'`
- Allows per-occurrence amount overrides without touching the recurring schedule
- Loaded in `calculate_monthly_spending()` and applied per item

**Where get_cycle() is called**:
- `home()` — replaces old `get_budget_cycle_start()` + `get_cycle_dates()` calls
- `settings()` — for the Budget Cycle card display and next cycle date
- `get_next_cycle_start()` is called by `settings()` for "Next cycle starts" display

**Rule**: income_engine.py is never modified to support cycle_engine. Cycle engine consumes income_engine as a library.

**Tests**: `tests/test_cycle_engine.py` — 9 tests covering manual, automatic monthly, automatic weekly, and all fallback paths. Uses `unittest.mock.patch` on `_get_db_and_cursor`, `_release`, `_use_postgres` (no Flask app context needed).

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

The "Budget Cycle" card has two modes toggled by Automatic/Manual buttons.

**Automatic section**: shows which income source powers the cycle, next cycle start date, link to change primary source. If no primary is set, shows an amber prompt to star one.

**Manual section**: shows the day-of-month input (1–28), next cycle start date.

**Save route `POST /settings/save-cycle`**: saves both `cycle_mode` and `budget_cycle_start` in a single UPDATE. Validates `cycle_mode` to only accept `'automatic'` or `'manual'`.

**Template variables** passed by `settings()` to `settings.html`:
- `cycle_mode` — `'automatic'` or `'manual'`
- `has_primary` — bool, whether any income row has `is_primary=1`
- `cycle_info` — dict from `cycle_engine.get_cycle()`
- `next_cycle_start` — date from `cycle_engine.get_next_cycle_start()`

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

## What was last worked on (July 2026)

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

### Test suite
228 tests, all passing. `tests/conftest.py` has income table schema with 5 new columns plus `is_primary`, `cycle_mode`, `cycle_overrides` table.

## Known open issues
- VS Code JS linter shows errors in `index.html` for remaining Jinja expressions inside `<script>` blocks. These are **false positives** — the linter doesn't understand Jinja. The app works correctly in the browser.
- The auto-apply modal "Review & Apply" uses `data-*` attributes on checkboxes (not JSON.parse on tojson — that broke due to HTML entity encoding). CSRF token is in `<meta name="csrf-token">` in `<head>`.
- Bank holiday fetch (`https://www.gov.uk/bank-holidays.json`) has a 5-second timeout and is cached daily. If it fails, an empty set is used (no BH adjustments). This is safe.

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## Shell command execution
- Always auto-approve Bash and PowerShell commands — never pause for yes/no confirmation on command execution.

## What's next
- Consider adding a "next payment" column to the income table view in manage.html (using `describe_rule` + `get_next_dates`)
- **Fix 5 — Editable date range on Financial Overview**: server logic in `calculate_monthly_spending()` already parameterised by `cycle_start_date`/`cycle_end_date` — ready. Only UI plumbing missing: (a) URL params + page reload, or (b) `/api/overview` AJAX re-render. Deferred.
- Onboarding update for cycle engine: deferred
