# Orbit — curated feature spec (v0, 2026-10-31)

Rule for every screen: one job, nothing more than one click from the dashboard, every number traceable to a source and a date.

## 1. What the prototype has today
| Screen | Features |
|---|---|
| Dashboard | Assets (left) and liabilities (right) grouped by 10 types, subtotal + share %, proportional bar per account, 30-day delta. Center: net worth hero, Today/1M/YTD/1Y deltas, 1M–All range toggle, assets/liabilities composition stacks, six facts (assets, liabilities, liquid NW, debt-to-assets, home equity, 1-yr change). "Example data" + "Synced Xm ago" chips. |
| Account page | Balance hero with ranges, share of assets and of net worth, type-specific facts (APY, rate, payment, limit, death benefit…), holdings table, recent activity, open/refresh/edit actions. |
| Type page | Aggregated chart, member list with bars and deltas, share of NW, 1-yr change. |
| Activity | All transactions, search + category + account filters, day grouping, income highlighted. |
| Trends | Income vs spending, monthly NW change, category spend (90d), investments vs cash. |
| Paycheck | Latest stub segmented bar, line table with YTD, effective tax rate, 401(k)/HSA vs IRS limits, Gross → net worth waterfall. |
| Almanac | Year table (NW, change, income, spending, kept, savings rate, notes), month table, records (ATH, best month, milestones). |
| Connect | Six source types: bank/brokerage link, file import, manual asset, paystubs, crypto, property estimate. |
| Shell | Light/dark, phone layout with tab bar, three visual directions (Keel chosen). |

## 2. Phase 1 — build first (your stated start)

### 2a. Detailed net worth tracking
1. **Two-date compare.** Pick any two dates; table of every account's balance then vs now, delta, and % — sortable. The thing spreadsheet users build by hand.
2. **Growth decomposition on the chart.** Toggle: net worth line vs stacked "contributions (cash in) + market/valuation gain". Answers "did I get richer or did markets?"
3. **By type / by account stacked view.** Switch the center chart from one line to stacked areas by type (assets teal shades, liabilities rust shades) or by account.
4. **Net worth definitions.** One switch cycles Total → Liquid (cash + taxable + crypto − cards) → Investable (excludes home, vehicles, insurance). The hero number re-labels itself.
5. **Transfer detection.** Match opposite-sign transactions across accounts within 3 days; mark as transfers so they never count as income or spending and never distort net worth.
6. **Staleness as a first-class signal.** Per-account "updated 3d ago" chip; amber at 3 days, red at 7; sync-health panel listing failing links with a fix button; manual accounts get a quarterly "still $X?" prompt.
7. **Editable history + closed accounts.** Edit any daily snapshot inline; month-end rows locked with an explicit "reopen month". Closing an account keeps its history so the chart never drops a cliff.
8. **Annotations.** Add a note to any date ("bought condo", "bonus", "refi"). Shown as small markers on the chart and as the almanac Notes column — the chart becomes the diary.
9. **Milestone lines.** Optional horizontal lines ($250k, $500k, $1M) on the chart; crossed milestones get a date in the almanac.

### 2b. Spending and cash flow from transactions, by account
10. **Account cash-flow view.** On every cash and card account page: inflows, outflows, net per month (bars), average monthly outflow, and "months of runway" for checking/savings.
11. **Card statement view.** Statement balance, due date, minimum, paid-in-full streak, utilization; "cash needed in the next 30 days vs checking balance" on the dashboard facts.
12. **Category rules engine.** Merchant → category rules that learn from your corrections; bulk re-categorize; split a transaction; mark reimbursable.
13. **Recurring detection.** Subscriptions and bills found automatically (cadence + amount tolerance); list with next charge date and annual cost; flag price increases.
14. **Spending guides, not budgets.** Optional per-category monthly guide; actual vs guide as a thin bar on Trends. No envelopes, no rollover, no rings on the home screen.
15. **Merchant page.** Click any merchant: total, count, trend, which accounts you pay it from.
16. **Fixed vs variable split.** Housing/loans/insurance/subscriptions vs everything else; shows what a bad month can and can't move.

### 2c. Visual interface
17. **Chart defaults that respect the data.** 1Y default with monthly points; daily points only ≤ 3M; y-axis never forced to zero on net worth; closed/hidden accounts excluded from the series.
18. **Hover breakdown.** Hovering the net worth line shows the date, total, and the by-type breakdown at that date.
19. **Command palette (Ctrl/Cmd-K).** Jump to any account, merchant, month, or screen by typing. Keyboard shortcuts: `/` search, `g d` dashboard, `g a` activity.
20. **Privacy blur.** One key hides every number (for screen sharing or public places); persists per device.
21. **Density toggle.** Comfortable vs compact rows.
22. **Teaching empty states.** No accounts yet → the dashboard shows the example data with a banner "This is sample data — add your first account", not a blank page.
23. **Mobile.** Columns stack as Net worth → Assets → Liabilities; swipe between them; tab bar stays.

## 3. Phase 2 — account inclusivity
24. **New types with their own facts schema:** vehicles (value, loan link), 529/education, pension (monthly benefit or present value), RSUs/options (vested vs unvested, strike, vest schedule → future-value row), private/angel equity (last round price, date), business equity, receivables (money owed to you), BNPL (Affirm/Klarna as liabilities), I-bonds/Treasuries (maturity, rate), cash on hand, escrow, estimated tax owed (accrued liability), security deposits. Points/gift cards: optional, excluded from net worth by default.
25. **Per-account switches:** include in net worth, include in liquid, hidden, closed, currency.
26. **Multi-currency.** Store native currency, convert at snapshot time with stored FX rate; show both on the account page.
27. **Cost basis everywhere.** Unrealized gain and return since purchase for brokerage, crypto, property, vehicles.
28. **Migration imports.** Mint, Monarch, Empower, Copilot, YNAB, Tiller CSV formats recognized on drop; history preserved.
29. **Household view (later).** Second person's accounts tagged, dashboard filter Mine / Theirs / Household.

## 4. Phase 3 — quality of life
30. **Bill calendar.** Card due dates, loan and mortgage payments, insurance premiums, estimated taxes; month view and "next 30 days" strip.
31. **Daily or weekly digest** (email/Telegram/Discord, opt-in): net worth change, upcoming bills, stale accounts, new recurring charges.
32. **Projection (clearly labeled assumption).** Dashed line from trailing-12-month savings plus a user-set return; FI number and date on the almanac.
33. **Export everything.** CSV per table, full SQLite backup, almanac year page as PDF.
34. **Demo mode.** One toggle swaps your data for the sample set for screenshots and sharing.

## 5. Keep out (on purpose)
AI insight cards, advice panels, upsells, envelope budgeting, gamification, credit-score widgets, bill negotiation, news feeds, more than one accent color.

## 6. How this maps to the build sessions
- Session 4 (Dashboard): items 17, 18, 22, 23, 6 (chips).
- Session 5 (Account/type pages): items 7, 10, 11, 15, 27.
- New **Session 5b — Net worth depth:** items 1, 2, 3, 4, 8, 9.
- New **Session 5c — Transactions engine:** items 5, 12, 13, 14, 16.
- Session 8 (Trends/Almanac): items 8 (notes column), 9, 32.
- Session 9: items 19, 20, 21, 33, 34.
- Phase 2 and 3 become Sessions 10+ after v1 ships.
