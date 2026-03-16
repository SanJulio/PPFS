## PPFS Build Log

06/03/26

### Session 1 — Done
- Fixed type column bug in add_transaction (models.py)
- Added income table to database.py
- Verified all 4 tables exist in ppfs.db

### Session 2 — Done
- Migrated Income.csv → income table
- Migrated Savings_Rules.csv → savings_rules table
- Migrated Future_Events.csv → future_events table
- Migrated Scheduled_Expenses.csv → scheduled_expenses table
- Updated Tracker.py and app.py to read everything from database
- Deleted all migration scripts
- Pushed to GitHub

### Session 3 — Done
- Built /actions page (Add Expense, Add Income, Transfer)
- Cleaned up bills.html and transactions.html
- Fixed navbar on all pages
- Removed forms from homepage
- Pushed to GitHub

### Current Stack
- Flask backend (app.py)
- SQLite database (ppfs.db)
- Deployed on Render via GitHub
- Local testing: python app.py in terminal

07/03/26

### Session 4 — Done
- Added flask-login and multi-user auth
- Built register and login pages
- All routes protected with @login_required
- Every query filters by user_id
- Switched to PostgreSQL — data persists forever
- Fixed afford route to read from database
- Fixed monthly spending to filter by user
- Cleaned up unused code
- Pushed to GitHub — live on Render

07/03/26

### Session 5 — Done
- Built register.html and login.html templates
- Added Flask-Login with register, login, logout routes
- All routes protected with @login_required
- All queries filter by current_user.id — users only see their own data
- Switched from SQLite to PostgreSQL on Render
- Fixed load_dotenv overriding Render environment variables
- Fixed load_user crashing on "None" remember cookie
- Stored sessions in PostgreSQL via custom session interface
- Switched to gunicorn for production serving
- Live on https://ppfs.onrender.com and fully working
- Added all personal data (accounts, bills, income, savings rules)

09/03/26

### Session 6 — Done
- Added Mark as Paid button on bills page
- Redesigned bills page with expandable table rows
- Added Undo and Edit on transactions page
- Fixed VS Code Jinja2/JS warnings
- Set up UptimeRobot to prevent Render spin-down

13/03/26

### Session 7 — Done
- Removed 100 transaction limit — now loads all transactions
- Added search bar and date range filters on transactions page
- Added Mark as Paid button on bills page
- Fixed monthly spending to correctly split bills and normal transactions
- Added undo and edit on transactions page
- Added logo to header on all pages

13/03/26

### Session 8 — Done
- Fixed transfers being counted as spending — now saved as type='transfer' and excluded
- Clickable overview tiles — tap any tile to see full breakdown of what feeds into it
- Account toggles — include/exclude any account from the financial overview calculations
- Added include_in_overview column to accounts table
- Cleaned up database.py — removed duplicate flask_sessions table creation

14/03/26

### Session 9 — Done
- Renamed Bills to Flow in nav bar
- Flow page with Expenses, Income and Accounts tabs
- Mark as Received for income sources
- Accounts tab with per-account breakdown:
  - Bills paid this month
  - Bills to pay this month
  - Income received this month
  - Income to receive
  - Projected end of month balance with traffic light
- Income edit/delete in Account settings (same style as bills)
- Fixed afford section (Tracker.py PostgreSQL compatibility)
- Fix-bills and fix-transfers data migration completed

### Session 10 — Done
- Investments feature:
  - Add investments in Account settings (name, type, initial amount, date)
  - Log value updates in Actions page
  - Investments tab in Flow page showing:
    - Current value, gain/loss in £ and %
    - Value history per investment
    - Portfolio summary (total invested, current value, total gain)
- Flow page now has 4 tabs: Expenses, Income, Accounts, Investments
- Fixed init_db table creation with individual try/catch blocks

16/03/26

Fixes

- Fixed Render deployment failing due to hardcoded port 5000 — changed to $PORT
- Fixed account creation being silently blocked by a global unique constraint on account names — dropped accounts_name_key constraint via temporary route
- Fixed forecast page timing out — rewrote to single pass 90 day simulation instead of 90 separate DB calls
- Fixed VS Code Jinja2 errors in forecast.html — moved variables to data attributes
- Fixed logo not centred on login page

Features Added

- Reset options on Account page — reset balances, transactions, bills, income, full reset
- Balance forecast page at /forecast with 30/60/90 day toggle, per account chart, warnings and summary cards
- Forecast added to navbar, logout moved to Account page header

Bulletproofing

✅ 404 and 500 error pages
✅ Rate limiting on login and register
✅ Database indexes on user_id across all 8 tables
