# Wealth-Tracker UI Patterns: Research for a Minimalist DIY "Financial Almanac"

Scope: 18 products + DIY spreadsheets shared on r/personalfinance, r/financialindependence, r/fiaustralia, Bogleheads-adjacent forums and Tiller's community. Primary sources (help docs, Reddit, Product Hunt, reviews.io) preferred; affiliate reviews flagged where used. Sequence was dropped (it is a money-routing/automation tool, not a net-worth tracker).

---

## Product notes

**Copilot Money (iOS/macOS/web)**
- Home = budget progress rings + spending status; dashboard cards are rearrangeable via a cog; Net Worth is its own view "combining assets and debts over different time frames"; manual assets (e.g. jewelry) are supported.
- Accounts tab "compares assets to debts"; Investments tab shows totals, benchmark overlays (S&P 500, BTC), holdings split stocks/crypto/ETF.
- Praise: native Apple feel, Swift Charts, "clear, intuitive" (Gruber, paying user); 4.9/5 iOS. Hate: no Android, web app thinner than iOS, reporting limited to 12 months, no free tier.

**Monarch Money**
- Dashboard = grid of removable, drag-to-reorder widgets (Net worth, Budget, Investments, Transactions, Spending trend, Credit score, Advice...). Web and mobile configured separately.
- Every widget click-throughs to a detail page; left nav; custom saved report filters.
- Praise: "much cleaner UX compared to ... Empower." Hate (r/MonarchMoney): net-worth chart defaults to a zoomed-in daily view ("I don't need to see day-to-day fluctuations... let me set YTD or 1Y"), transfers skewing net worth, inverted/broken balance history, beta redesign breaking goals, chronic sync issues, Sankey chart bugs, black dialog boxes blocking rows on mobile.

**Kubera**
- Single ledger page: Assets and Debts as spreadsheet-like sheets with sections; each row = name, value, cost basis, unrealized gain; net worth totals update live; daily snapshot history since day one; Recap report by tax status.
- Real estate from address (Zillow), vehicles by VIN, crypto wallets/DeFi, manual rows for anything else; click a row for a details page.
- Praise: "supercharged spreadsheet," "almost no learning curve," "minimalistic design," "elegant, powerful dashboard to see my complete financial life in one place" (reviews.io). Hate: price ($150-249/yr), no mobile app, overseas property/banks manual only, occasional brokerage data inaccuracies.

**Empower Personal Dashboard (ex-Personal Capital)**
- Net worth number + historical chart is the hero; asset pie chart; accounts in a left sidebar; Investing tab for allocation, fee analyzer, retirement planner.
- Praise: "every account in one chart, tracked over time." Hate: advisor calls after linking ("declining once doesn't always end the outreach"), "upsell prompts everywhere," alerts framed as sales tactics ("Your allocation is 3% off optimal!"), fragile aggregation, clunky cross-domain login, dashboard "showed me what THEY thought was important."

**YNAB**
- Budget-first; net worth lives in "Reflect" reports (bar chart of assets/liabilities + line).
- Hate (r/ynab): net worth chart "has taken a step back," month-to-month changes "almost imperceptible," y-axis "needlessly out of scale" from closed accounts; can't tap a category to see transactions; single-month only, no custom ranges; users resent the cutesy "Reflect" name ("just call it Reports").

**Tiller (Google Sheets/Excel)**
- Net Worth Tracker sheet: chart of net worth over a customizable period on top; below it a Trends table with Assets, Liabilities and each account under its class, subtotaled by user-defined Account Groups.
- Net Worth Snapshot sheet: pick a Reference Date (end of last year / 12 months ago / custom) and a Comparison Date; shows $ and % change per account/group with a chart.
- Users asked for: liabilities shown as negatives so the chart reads intuitively, per-account mini trendlines, up/down color cues vs prior period.

**ProjectionLab**
- Planning-first: Plan overview, Compare Mode, milestones + "chance of success," Sankey cash flow by year, Monte Carlo fan chart. Net worth is a milestone target, not a live ledger. Manual entry by default, linking optional.
- Praise: "modern interface that makes financial planning something you actually want to do." Hate: learning curve; not a day-to-day tracker.

**Maybe Finance (open source, archived)**
- Vision doc: home = historical net-worth graph + "basic insights," intentionally minimal; `/accounts` list; `/account/:id` = historical balance graph + type-specific details (loan rate, principal). Top priority "100% calculation accuracy." Praised on r/selfhosted as one of "the first modern ui/ux apps I've seen" among self-hosted options.

**Ghostfolio (open source)**
- Overview = portfolio value, gains/losses, net-worth progression, filters by account/asset class/date/tag; Holdings table with value, P&L, weight, performance; Accounts page with per-account performance/allocation. Investment-centric: cash/real estate are second-class.

**Actual Budget (open source)**
- Net Worth report with Trend and Stacked (per-account) modes; includes off-budget accounts; multiple custom dashboards with cards, live/static date ranges, filters. Reddit: "stable and simple," "cleaner UI than Firefly III."

**Firefly III (open source)**
- Budget-focused dashboard; cash-centric net worth. r/selfhosted: "UI/UX is very confusing and the importer is a separate program."

**Lunch Money**
- Net Worth page: period picker top-left, "View/Edit Data" opens a historical balance table; "By account / By type" toggle; blue asset bars, red liability bars, green net-worth line; hover tooltips with % change and allocation; CSV export; month-end snapshots that past edits don't retroactively change.
- Praise: "lightweight, fast, intuitive," multi-currency. Hate: "interface is cluttered," historically no mobile app.

**Piere (piere.com; "Pierre" is a separate Brazilian CloudWalk chat-finance app)**
- Free tier: current net worth only; history requires Premium; Net Worth/Budget/Transactions widgets. Hate: miscategorization (transfers labeled car payments), AI budget that doesn't balance. Thin evidence; low priority as a reference.

**Rocket Money**
- Bright, color-heavy spending/subscription dashboard; net worth is a secondary tool. Praise: "lovely to look at," fast. Hate: useful features paywalled behind Premium, weak investment tracking.

**Quicken Simplifi**
- Net worth at top of dashboard; Spending Plan (income minus bills); Watchlists; pie chart; every dashboard box clicks into detail; address-based home value. Praise: "clean, intuitive," "no pop-up ads," clickable boxes. Hate: recurring-bill misdetection, round-ups categorized as income.

**Origin**
- Net worth central on a "clean overview"; manual home/car with Zillow link; forecast tool. Praise: "intuitive, clean." Con noted: "simple interface with lots of white space" (some prefer it to Monarch's "colorful graphs").

**Wealthfolio (open source desktop)**
- Local-first portfolio dashboard: holdings by stock/ETF/crypto, performance vs benchmarks, income (dividends), goals, multi-currency. Praise: "clean, minimalist," "boring but reliable," "few gimmicks." Hate: CSV-only import ("very pretty" front end but manual entry makes it "hard to recommend").

---

## DIY spreadsheets: what people actually record

- **Cadence**: monthly end-of-month balances is the modal choice (r/FI threads back to 2017-2022); quarterly for the less obsessive ("first weekend after quarter end, ~10 min"). Annual rows are derived from the monthly series.
- **Balance columns per account**, ordered the same every time: checking, savings, taxable brokerage, 401k/IRA/Roth, HSA, pension/insurance cash value, home (lower of two estimates minus 6-8% selling costs), vehicles (trade-in), crypto/business equity (memo line if volatile), mortgage/HELOC/auto, cards/student/personal loans.
- **Flow columns**: gross income, taxes, net income, contributions by account (incl. employer match), withdrawals/debt payments, total spend (by category quarterly/annually), **investment gains separated from contributions**.
- **Derived**: net worth, monthly and YoY $ and % delta, savings rate (income-weighted, not averaged monthly rates), liquidity ratio (liquid / monthly spend), debt-to-asset, FI % (NW / 25x spend), withdrawal rate, months-since-last-$100k milestone, inflation-adjusted toggle.
- **Annual-review posts** (r/FI "Year in Review", r/fiaustralia) consistently report: NW start -> end, invested assets, contributions, market gain, income, spend, savings rate, FI number, plus a **Notes** line (job change, refi, baby) because "a mortgage refinance moved money around and made my columns unusable."
- Sparklines per row and a date-achieved milestone table are the two most-praised spreadsheet flourishes.

---

## Synthesis

### 1. Patterns that recur in the best-liked products
1. **Net-worth-first hero**: one big number + area/line chart at top (Empower, Simplifi, Kubera, Maybe, Origin, Monarch widget).
2. **Explicit assets / liabilities split** with subtotals (Lunch Money's blue/red bars, Tiller trends table, Kubera's Assets/Debts sheets, Copilot's "assets vs debts").
3. **Accounts grouped by type**, user-definable groups, each group subtotaled (Tiller Account Groups, Lunch Money "By type" with subtypes, Empower sidebar).
4. **Time-range toggle that persists** (1M/3M/YTD/1Y/All/custom); the #1 Monarch complaint is a chart that resets to a zoomed-in daily view.
5. **Click-anything drill-down**: every card/row opens a detail page with its own balance history and type-specific fields (Simplifi "every box clicks," Maybe `/account/:id`, Kubera row details).
6. **Per-row change indicators and sparklines** ($ and % vs prior period, up/down color) are requested by Tiller users and built into the r/FI spreadsheet.
7. **Holdings table** (ticker, value, cost, P&L, weight) inside investment accounts (Ghostfolio, Wealthfolio, Kubera, Copilot).
8. **First-class manual assets** with a valuation rule attached (address -> Zillow, VIN, ticker lookup, or plain number + cost basis); Kubera is the model.
9. **Editable historical balance table** behind the chart (Lunch Money "View/Edit Data," Tiller Balance History) so the DIY user can backfill and correct.
10. **Point-in-time snapshot semantics**: month-end snapshots that later edits don't rewrite (Lunch Money) and a "compare two dates" view (Tiller Snapshot).
11. **Customizable, removable widgets** rather than a fixed feed (Monarch, Copilot cog, Actual multi-dashboards).
12. **Calm visual language**: spreadsheet-familiar, mostly monochrome with one accent, generous white space (Kubera, Origin, Wealthfolio get "clean/minimal/boring but reliable").

### 2. Patterns users consistently complain about
- Upsells and advisor outreach (Empower), paywalled history (Piere), Premium nags (Rocket Money).
- Charts with bad defaults: daily noise, y-axis distorted by closed accounts, imperceptible month-to-month change (Monarch, YNAB).
- Sync fragility and silent staleness; no visible "last synced" (Monarch, Empower, Sequence, Kubera brokerage data).
- Transfers and refis corrupting the series; inverted liability signs.
- Clutter: "lots of colors," dense dashboards (Lunch Money "cluttered," Rocket Money's "bright colours" cut both ways); cutesy naming ("Reflect").
- AI/insight widgets that pad the screen without adding decisions (Monarch Advice, Copilot Monthly Snapshot AI); users ask the AI only "how has my net worth changed."
- Budget-first apps burying net worth two taps deep; investment-first apps treating cash/home as afterthoughts; nobody does both cleanly.
- Mobile/web feature asymmetry (Copilot, Monarch Android).

### 3. What the "financial almanac" view should contain
One row per year (with monthly rows underneath, collapsible), columns fixed in order:
- **Balance sheet**: NW start, NW end, $ change, % change; assets by group (cash, taxable, retirement, HSA, insurance CV, home, crypto, other); liabilities by group (mortgage, loans, cards).
- **Flows**: gross income, taxes, net income, total spend, contributions (by account, incl. match), debt paid down, market gain (= NW change - contributions + withdrawals).
- **Ratios**: savings rate (income-weighted), liquidity months, debt/asset, FI % (NW ex-home / 25x spend), withdrawal rate if applicable, inflation-adjusted NW toggle.
- **Milestones**: date each $100k/$250k crossed, months since last milestone.
- **Notes**: one free-text line per year (job, move, refi, big purchase) - the thing every long-term tracker says they wish they'd kept.
- **Comparison selector**: pick any two dates (Tiller Snapshot pattern) to get a per-account delta table.

### 4. Five recommendations for the assets-left / net-worth-center / liabilities-right layout
1. **Make the center column a chart with a sticky range toggle (3M/YTD/1Y/5Y/All, default 1Y) plus a two-date delta readout**; store the selection. Show monthly points, not daily, by default to kill the noise complaint.
2. **Render assets (left) and liabilities (right) as grouped, subtotaled cards with a per-row sparkline and signed delta vs the previous month**; keep liabilities visually positive in the list but subtract in the chart (Tiller users asked for exactly this clarity). Clicking a card opens a detail page: balance history, editable snapshot table, holdings table if investment, rate/term if loan, valuation rule if manual.
3. **Treat manual assets as equals**: each row carries a valuation rule and an "as of" date; show a muted "updated 14d ago" chip on every row (manual or synced). This is the single most-missed trust signal across every product reviewed.
4. **Snapshot, don't recompute**: write an immutable month-end snapshot per account; derive the almanac from snapshots; flag transfers so internal moves never move NW. Give the chart a "hide closed accounts" default to avoid YNAB's squashed y-axis.
5. **Leave out what people hate**: no AI insight cards, no advice panel, no budget rings on the home screen, no more than one accent color plus red/green for sign. Put the almanac table one click away as the only secondary view, and keep budget/cash-flow (if ever added) off the home screen.

---

## Sources
- [Monarch: Set default date range for net worth chart (r/MonarchMoney snapshot)](https://reddit.sentinel-team.org/subreddits/MonarchMoney/snapshots/2026-03-11T14%3A52%3A40.875869Z)
- [r/MonarchMoney snapshot Jan 2026 (UI praise/complaints)](https://reddit.sentinel-team.org/subreddits/MonarchMoney/snapshots/2026-01-24T07%3A40%3A22.40417Z)
- [Monarch help: Customize Your Dashboard](https://help.monarch.com/hc/en-us/articles/360058127551-Customize-Your-Dashboard)
- [Rob Berger: Monarch Money Review](https://robberger.com/monarch-money-review/)
- [r/ynab: What's your opinion on Reflect, the new reports](https://lr.in.psf.lt/r/ynab/comments/1e5nq9w/whats_your_opinion_on_reflect_the_new_reports)
- [Kubera reviews on reviews.io](https://www.reviews.io/company-reviews/store/kubera/lP)
- [Jean Galea: Kubera Review](https://jeangalea.com/kubera-review)
- [Wallet Hacks: Kubera Review](https://wallethacks.com/kubera-review/)
- [Kubera: How Kubera Works](https://www.kubera.com/how-kubera-works)
- [Lunch Money support: Net Worth Tracker](https://support.lunchmoney.app/home/net-worth)
- [Product Hunt: Lunch Money reviews](https://www.producthunt.com/products/lunch-money/reviews)
- [Daring Fireball on Copilot Money](https://daringfireball.net/linked/2025/12/28/)
- [Copilot Money full walkthrough (video summary)](https://videohighlight.com/v/c2PteUnbSMY)
- [Penny Hoarder: Copilot Money Review](https://www.thepennyhoarder.com/budgeting/budgeting-copilot-money-review/)
- [Freenance: Copilot Money Review 2026 (competitor-written)](https://freenance.io/products/copilot-money-review-2026-budgeting-app-iphone-best-design/)
- [BankSync: My Empower Story, Free Tools, Endless Upsells (competitor-written)](https://banksync.io/blog/empower-personal-dashboard-alternative)
- [Finny: Empower Personal Dashboard Review 2026](https://getfinny.app/blog/empower-personal-dashboard-review-2026)
- [Financial Samurai: Empower Personal Dashboard Review (affiliate)](https://www.financialsamurai.com/empower-personal-dashboard-review/)
- [Financial Samurai: ProjectionLab Review](https://www.financialsamurai.com/projectionlab-review/)
- [Maybe Finance wiki: Vision](https://github.com/maybe-finance/maybe/wiki/Vision)
- [Elestio: Ghostfolio overview](https://blog.elest.io/ghostfolio-free-open-source-privacy-first-portfolio-wealth-tracker/)
- [selfhosting.sh: Ghostfolio vs Firefly III](https://selfhosting-sh.pages.dev/compare/ghostfolio-vs-firefly-iii/)
- [Actual Budget docs: Reports](https://actualbudget.org/docs/reports)
- [r/selfhosted: Personal finances app discussion](https://redlib.hyperreal.coffee/r/selfhosted/comments/1adnffd/personal_finances_app_discussion/kk2shka/?context=3)
- [The College Investor: Piere Review](https://thecollegeinvestor.com/46288/piere-review/)
- [TechRadar: Rocket Money review](https://www.techradar.com/pro/software-services/rocket-money-review)
- [Rob Berger: Simplifi Review](https://robberger.com/simplifi-review/)
- [Rob Berger: Origin Review](https://robberger.com/origin-review/)
- [MacHow2: Wealthfolio Review](https://machow2.com/wealthfolio-review/)
- [Slashdot: Sequence Finance](https://slashdot.org/software/p/Sequence-Finance/)
- [Tiller Community: Docs, Net Worth Tracker](https://community.tiller.com/t/docs-net-worth-tracker/76)
- [Tiller Community: Docs, Net Worth Snapshot](https://community.tiller.com/t/docs-net-worth-snapshot/1219)
- [Tiller: Long-term Financial Tracking in Spreadsheets](https://tiller.com/long-term-financial-tracking-in-spreadsheets/)
- [r/financialindependence: 3 years of spreadsheet net worth tracking](https://lr.eu.psf.lt/r/financialindependence/comments/1ikglmh/3_years_of_spreadsheet_net_worth_tracking)
- [r/financialindependence: New and improved advanced FI spreadsheet](https://lr.eu.psf.lt/r/financialindependence/comments/rwq9qw/i_made_a_new_and_improved_advanced)
- [r/financialindependence: 2025 Year in Review thread](https://reddit.sentinel-team.org/posts/1pqr7f2/snapshots/2026-01-02T19%3A20%3A49.752943Z)
- [r/fiaustralia: 2025 annual review](https://reddit.sentinel-team.org/posts/1q0wqlw/snapshots/2026-01-03T01%3A00%3A02.156788Z)
- [Minafi: 5 metrics to track for FI](https://minafi.com/spreadsheet)
- [dev.to: How to build a quarterly net worth spreadsheet](https://dev.to/evvytools/how-to-build-a-quarterly-net-worth-spreadsheet-that-holds-up-for-a-year-2f5h)
