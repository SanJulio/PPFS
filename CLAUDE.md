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
- `tests/` — pytest suite (174 tests, all passing); `conftest.py` has SQLite schema matching production

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

## What was last worked on (May 2026)

### UX fixes batch (May 2026)
- **Fix 1 — Bill tab redirect**: `settings_add_bill`, `settings_edit_bill`, `settings_delete_bill` all now redirect to `/manage?tab=bills` (was going to the default Accounts tab).
- **Fix 2 — Landing page SEO**: `<meta name="description">` updated to `"Spendara · See the future of your money"` (under 155 chars). No OG tags existed.
- **Fix 3 — Dismiss banner bug**: Root cause was the banner's "Dismiss" button doing `style.display='none'` only — no DB write. Fixed to call `dismissAutoApply()` which POSTs to `/dismiss-auto-apply` (updates `last_applied` in DB). Added `.then()` to hide the banner only after server confirms success.
- **Fix 4 — Safe to spend in day view**: The "See transactions" flow in Future Balances now shows `· Safe: £X.XX` alongside the balance on every row. Computed as `running_balance − remaining_bills_in_period`; turns green/red based on sign.
- **Fix 6 — Middle dots in title tags**: All 18 `<title>` tags across all templates updated from `—` (em-dash) to `·` (middle dot).
- **Fix 5 — Editable date range on Financial Overview**: Parked. The server logic (`calculate_monthly_spending`) is already parameterised by `cycle_start_date`/`cycle_end_date` — it's ready. Only the UI plumbing is missing. Options when resuming: (a) URL params + page reload (simplest), (b) `/api/overview` JSON endpoint + AJAX re-render (no flash).

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
`tests/conftest.py` income table schema updated with 5 new columns plus `is_primary`, `cycle_mode`, `cycle_overrides` table. 174 tests, all passing.

## Known open issues (as of session end May 2026)
- VS Code JS linter shows errors in `index.html` for Jinja expressions inside `<script>` blocks (e.g. `{{ pending_items | tojson }}`). These are **false positives** — the linter doesn't understand Jinja. The app works fine in the browser.
- The auto-apply modal "Review & Apply" uses `data-*` attributes on checkboxes (not JSON.parse on tojson — that broke due to HTML entity encoding). CSRF token is in `<meta name="csrf-token">` in `<head>`.
- Bank holiday fetch (`https://www.gov.uk/bank-holidays.json`) has a 5-second timeout and is cached daily. If it fails, an empty set is used (no BH adjustments). This is safe.

## Commit style
- No "Co-Authored-By: Claude..." trailer in commits — omit it always.

## Shell command execution
- Always auto-approve Bash and PowerShell commands — never pause for yes/no confirmation on command execution.

## What's next
- Income modal is feature-complete and polished — no known remaining issues
- Budget Cycle rebuild complete (May 2026): cycle_engine.py, primary income star UI, automatic/manual settings toggle, all calculations wired. 174 tests passing.
- Consider adding a "next payment" column to the income table view in manage.html (using `describe_rule` + `get_next_dates`)
- **Fix 5 — Editable date range on Financial Overview**: server logic ready, UI plumbing needed (URL params + reload, or AJAX re-render). Deferred.
- Landing page: further tightening if needed
- Onboarding update for cycle engine: deferred
