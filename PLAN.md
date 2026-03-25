# Spendara — Road to £10k/month

## What To Build — Session by Session

| Session | Feature | Why |
|---------|---------|-----|
| ✅ Done | Empty state screens | New users see blank pages right now |
| ✅ Done | PWA manifest | Makes it feel like a real app |
| ✅ Done | Transaction categories | Most impactful UX improvement |
| ✅ Done | CSV import | Removes biggest onboarding barrier |
| **Next** | Stripe | Start making money |

> **The order matters:** Empty states → PWA → CSV import → Stripe → Market it
> There's no point marketing until new users can understand it, install it like an app, and bring their data with them.

---

## Immediate Priorities

### 🥇 Priority 1 — App Experience (Do These First)
- ✅ Onboarding flow — already built, make sure it's solid
- ⬜ **Empty state screens** — when a new user has no accounts/bills/transactions, show a helpful prompt instead of a blank page
- ⬜ **Better error messages** — friendly, helpful messages that tell users exactly what to do
- ⬜ **Transaction categories** — tag transactions (food, transport, bills, entertainment) — most requested feature in every budgeting app

### 🥈 Priority 2 — Mobile Experience
- ⬜ **PWA** — add `manifest.json` and service worker so users can install Spendara on their home screen. No App Store needed, ~1 day to build
- ⬜ **Mobile UI polish** — go through every screen on your actual phone and fix anything cramped, hard to tap, or awkward

### 🥉 Priority 3 — CSV Import
- ⬜ **CSV import** — upload a bank statement CSV and auto-populate transactions. Most UK banks (Barclays, HSBC, Nationwide, Monzo) export standard CSVs

### 🏁 Priority 4 — Stripe
- ⬜ **Stripe payments** — freemium model, £4.99/month premium
- ⬜ **Free vs Premium feature gating** — lock premium features behind paywall

---

## Where You Are Now

- ✅ Solid Flask/PostgreSQL backend
- ✅ Core features built and working
- ✅ Live at spendara.co.uk
- ✅ Security bulletproofed
- ✅ Landing page and onboarding flow built
- 2 known users (you and your girlfriend)
- £0/month revenue

---

## The Model

**Freemium** — free tier gets people in, premium converts them

- **Free**: Core app, basic forecasting, up to 3 accounts
- **Premium — £4.99/month**: Unlimited accounts, advanced forecasting, CSV import, investment tracking, savings rules, future events, insights

### To hit £10k/month:
- £10,000 ÷ £4.99 = **2,004 paying users**
- At 15% free-to-paid conversion = **13,360 total users needed**
- That's very achievable for a niche fintech app

---

## Phase 1 — Foundation (Now → 100 users)

**Goal: Get the first 100 real users and learn from them**

### App Development
- ✅ Landing page
- ✅ Onboarding flow
- ⬜ Stripe payments — can't monetise without this
- ⬜ CSV import — biggest barrier to new users (they want their history)
- ⬜ Usage analytics — need to understand how people use the app
- ⬜ Free vs Premium feature gating — decide what's free and what's paid
- ⬜ Mobile PWA — make it installable on phone home screen
- ⬜ Delete account option — legal requirement (GDPR)
- ⬜ Privacy policy and terms of service pages

### Marketing
- Post on Reddit — r/personalfinance, r/UKPersonalFinance, r/SideProject
- Post on Twitter/X — build in public, show progress
- Tell friends and family — get first 20 users this way
- Product Hunt launch — when you're ready for visibility
- TikTok/Instagram — short videos showing the app in action

### Success Metric
- 100 active users
- 10 paying users (£49.90/month)
- NPS score — are users happy?

---

## Phase 2 — Growth (100 → 1,000 users)

**Goal: Find what makes users stay and double down on it**

### App Development
- ⬜ Smart spending insights — "You spent 20% more on food this month"
- ⬜ Budget goals — users set monthly targets per category
- ⬜ Spending categories — tag transactions (food, transport, entertainment)
- ⬜ Recurring transaction detection — auto-suggest bills from transaction history
- ⬜ Net worth tracking over time — graph showing growth month by month
- ⬜ Email digests — weekly summary of spending sent to users
- ⬜ Dark mode — users always ask for this
- ⬜ Account sharing for couples — joint finance tracking
- ⬜ Better mobile experience — optimise every screen for phone
- ⬜ In-app notifications — "Your forecast shows a shortfall next week"

### Infrastructure
- ⬜ Move to Render paid tier — no spin-down, faster responses
- ⬜ Proper error tracking — Sentry.io to catch bugs before users report them
- ⬜ Automated backups — daily DB backups
- ⬜ Performance monitoring — track page load times

### Marketing
- Content marketing — blog posts about budgeting, saving, UK personal finance
- SEO — target keywords like "personal finance app UK", "budget tracker UK"
- Referral programme — "Invite a friend, both get 1 month free"
- App Store listing — if you build a PWA this becomes possible
- Partner with UK finance influencers — send them free premium access

### Success Metric
- 1,000 active users
- 150 paying users (£748.50/month)
- Churn rate under 5% monthly

---

## Phase 3 — Scale (1,000 → 5,000 users)

**Goal: Make growth systematic and reduce churn**

### App Development
- ⬜ AI spending analysis — GPT-powered insights on spending patterns
- ⬜ Bank feed integration — connect real bank accounts via Plaid or TrueLayer
- ⬜ Tax reporting helpers — self-assessment summaries for freelancers
- ⬜ Investment portfolio analysis — compare performance vs market benchmarks
- ⬜ Savings goals — "Save £5,000 for a holiday by December"
- ⬜ Bill splitting — split expenses with friends
- ⬜ Multi-currency support — for users with foreign accounts
- ⬜ API for power users — let developers build on top of Spendara
- ⬜ Advanced CSV import — handle any bank format automatically
- ⬜ Bulk transaction editing — categorise multiple transactions at once

### Business
- ⬜ Annual pricing — £39.99/year (save 33%) to reduce churn
- ⬜ Business tier — £9.99/month for freelancers and small business owners
- ⬜ Affiliate programme — earn commission recommending savings accounts
- ⬜ Press coverage — reach out to TechCrunch, Forbes, UK finance press
- ⬜ App Store — proper iOS and Android apps

### Infrastructure
- ⬜ Move to AWS or GCP — proper cloud infrastructure
- ⬜ CDN for static assets — faster load times globally
- ⬜ Database read replicas — handle more concurrent users
- ⬜ Load testing — make sure the app handles traffic spikes

### Success Metric
- 5,000 active users
- 750 paying users (£3,742.50/month)
- MRR growing 15% month on month

---

## Phase 4 — £10k/month (5,000 → 13,000+ users)

**Goal: Hit and sustain £10k/month recurring revenue**

### App Development
- ⬜ Full mobile apps — native iOS and Android
- ⬜ Open banking integration — automatic transaction imports from any UK bank
- ⬜ Financial health score — single number showing overall financial health
- ⬜ Personalised recommendations — "Based on your spending, you could save £X more"
- ⬜ Community features — anonymised benchmarks ("People like you save X% of income")
- ⬜ White label option — sell Spendara to banks and credit unions
- ⬜ Enterprise tier — for financial advisors managing multiple clients

### Business Model at This Stage
- 2,004 premium users at £4.99/month = £10,000/month
- Plus annual subscribers
- Plus business tier users
- Plus potential affiliate income
- Realistic total: £12,000–£15,000/month

### Team at This Stage
- You — CEO, product direction
- 1-2 developers — features and maintenance
- 1 designer — UI/UX improvements
- 1 customer support — handling user queries
- Possibly your girlfriend in operations

---

## The Critical Path

These 5 things will make or break reaching £10k:

1. **Stripe payments** — without this you can't make money, do this first
2. **CSV import** — biggest onboarding barrier, users want their history
3. **Mobile experience** — most users will be on phones, it must be flawless
4. **Retention** — keeping users is more important than getting new ones
5. **Word of mouth** — at this price point, organic growth is everything

---

## Timeline (Realistic)

| Milestone         | Target     | MRR     |
|-------------------|------------|---------|
| 10 paying users   | Month 1-2  | £50     |
| 50 paying users   | Month 3-4  | £250    |
| 150 paying users  | Month 6    | £750    |
| 500 paying users  | Month 12   | £2,495  |
| 1,000 paying users| Month 18   | £4,990  |
| 2,000 paying users| Month 24   | £9,980  |

£10k/month is realistically 18-24 months away if you ship consistently and market well.

---

## The Most Important Thing

The gap between where you are and £10k/month is not technical. The app already works. The gap is:

- **Stripe** — so you can charge
- **Marketing** — so people find it
- **Retention** — so they stay
- **Consistency** — shipping every week, promoting every week
