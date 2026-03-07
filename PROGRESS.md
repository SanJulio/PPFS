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

### Next Session
- Build onboarding flow for new users
- Build landing page
- Test with real friends
- Start thinking about Stripe payments