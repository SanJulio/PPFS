## PPFS Build Log

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

### Next Session
- Build settings page containing:
  - Create new account
  - Deactivate account
  - Add/delete bills
  - Add/delete savings rules
  - Add future event
  - Edit income source

### Current Stack
- Flask backend (app.py)
- SQLite database (ppfs.db)
- Deployed on Render via GitHub
- Local testing: python app.py in terminal