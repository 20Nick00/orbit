# Orbit — build plan (v0, 2026-10-31)

Open-source, self-hosted wealth tracker. Everything orbits your net worth: assets left · net worth center · liabilities right. No budgets, no AI widgets, no upsells.

**Repo:** github.com/20Nick00/orbit · **Latest prototype:** https://20nick00.github.io/orbit/ (served from `docs/index.html`) · **Publish:** double-click `publish.bat` after any change.

## Decisions (locked)
| Area | Decision |
|---|---|
| Connectivity | Plaid from day one (pay-as-you-go; sandbox free). CSV/OFX import as the universal fallback so anyone can use it without Plaid. Aggregators sit behind one `Aggregator` interface so SimpleFIN/Teller can be added as plugins. |
| Stack | Python 3.12 · FastAPI · SQLite (WAL) · Jinja2 + HTMX + vanilla JS · hand-drawn SVG charts (ported from prototype). No frontend build step. |
| Brand | Orbit. Flat surfaces, 1px rules, tabular numerals, one accent, red/green only for sign. |
| Deployment | Docker Compose, single user, reached over Tailscale; PWA for phone. Multi-user later. |
| Paystubs | Watched folder (`data/inbox/paystubs/`) parsed automatically + optional Gmail/IMAP fetcher that pulls paystub PDFs by label/sender monthly. Per-employer mapping rule. Portal scraping (ADP/Workday via Playwright) is a stretch goal — fragile. |
| Property | Automated AVM via RentCast (free tier, ~50 calls/mo) with manual override. Zillow/Redfin have no open API; ATTOM is paid. Refresh monthly. |
| Budgeting | None in v1. Trends, categories, savings rate only. |
| History | Daily `balance_snapshots` + immutable `month_end_snapshots` (written on the 1st, never rewritten). Almanac reads month-end only. |
| License | MIT (adoption) — switch to AGPL if you ever host it for others. |

## Repo layout
```
orbit/
  app/            FastAPI app, routers, templates, static
  core/           models, snapshots, type inference, rollups (no web deps)
  aggregators/    plaid.py, csv_import.py, ofx_import.py, (simplefin.py later)
  paystubs/       watcher.py, gmail_fetch.py, parsers/, rules/
  property/       rentcast.py
  jobs/           nightly sync, month-end close, AVM refresh (APScheduler)
  data/           orbit.sqlite, inbox/, exports/  (gitignored)
  docker-compose.yml  Dockerfile  README.md  CONTRIBUTING.md
```

## Data model (core)
- `institutions(id, name, aggregator, external_id)`
- `accounts(id, institution_id, name, type, subtype, currency, is_liability, is_manual, mask, created_at, hidden)` — `type ∈ cash, invest, retire, health, insure, crypto, property, cards, loans, mortgage, other`
- `balance_snapshots(account_id, date, balance, source)` — one row per account per day
- `month_end_snapshots(account_id, month, balance)` — immutable
- `transactions(id, account_id, date, amount, merchant, category, memo, pending, external_id)`
- `holdings(account_id, date, ticker, name, qty, value, cost_basis)`
- `account_facts(account_id, key, value)` — APY, rate, payment, limit, death benefit, etc.
- `paystubs(id, employer, pay_date, period_start, period_end, gross, net, source_file)`
- `paystub_lines(paystub_id, label, group ∈ tax|pretax|posttax|benefit|employer, amount, ytd_amount)`
- `import_rules(id, source_kind, matcher, column_map or line_map)`
- `valuations(account_id, date, value, source)` — property/vehicle estimates

## Sessions to run in Claude Code (in order)
Each one is a fresh `claude` session in the repo. Paste the prompt; let it read `docs/` first.

### 0 · Scaffold
> This folder is already a git repo pushed to github.com/20Nick00/orbit with the prototype in `docs/index.html` (GitHub Pages). Keep that file and `publish.bat` working. Create a Python 3.12 FastAPI project named **orbit** with the layout in BUILD_PLAN.md. SQLite via SQLAlchemy 2 + Alembic. Jinja2 templates, HTMX, no JS bundler. Add Dockerfile + docker-compose.yml (app + a volume for `data/`), `.env.example`, ruff + pytest, GitHub Actions CI, MIT license, README skeleton. Health endpoint and an empty dashboard route that renders the base layout with the Orbit (Keel-direction) tokens from `docs/index.html` (copy the `:root` tokens and base CSS into `app/static/orbit.css`).

### 1 · Core models + snapshots
> Implement the data model in BUILD_PLAN.md as SQLAlchemy models + Alembic migration. Write `core/snapshots.py`: `record_daily(account_id, date, balance)`, `close_month(month)` that writes immutable month-end rows from the last daily snapshot of each month (idempotent, refuses to overwrite), and `net_worth_series(start, end, step)` returning daily or month-end series. Write `core/types.py` with type inference from `(aggregator, type, subtype, name)` → our 10 types, with an override table. Unit tests for all three, including a transfer that must not change net worth.

### 2 · Plaid aggregator
> Implement `aggregators/plaid.py` behind an `Aggregator` protocol (`link()`, `sync_accounts()`, `sync_transactions()`, `sync_holdings()`, `sync_liabilities()`). Use Plaid Link (hosted link flow) with products `transactions, investments, liabilities`; `/transactions/sync` with cursors; store items + access tokens encrypted at rest (Fernet key from `.env`). Map Plaid type/subtype to our types via `core/types.py`. Webhook endpoint for `SYNC_UPDATES_AVAILABLE`. Nightly job in `jobs/` that syncs every item and calls `record_daily`. Sandbox tests with Plaid's test credentials. Settings page: connect, re-auth, remove item.

### 3 · File import + manual assets
> Implement `aggregators/csv_import.py` and `ofx_import.py`. CSV: detect delimiter/header, auto-map date/amount/memo/balance by heuristics, show a one-time mapping UI (HTMX) when ambiguous, save the mapping as an `import_rules` row keyed by header signature. Dedupe on (account, date, amount, memo hash). OFX/QFX via `ofxparse`. Manual accounts: create/edit with a value and optional note; each save writes a daily snapshot. Add “+ Add” page matching `docs/index.html` Connect view.

### 4 · Dashboard
> Port the dashboard from `docs/index.html` to Jinja templates + `app/static/charts.js` (the SVG area/bar/waterfall functions from the prototype, unchanged API). Three columns: assets (grouped by type, subtotal, share %, thin proportional bar, 30d delta, “updated Xd ago” chip), net worth center (hero number, Today/1M/YTD/1Y deltas, range toggle default 1Y with monthly points, assets/liabilities composition stacks, six facts), liabilities right. Server renders everything; HTMX swaps the chart on range change. Target: first paint < 200 ms on a 10k-transaction DB (precompute series in `core/rollups.py`).

### 5 · Account + type pages
> Add `/account/{id}` and `/type/{type}` pages per the prototype: balance hero with ranges, facts grid, holdings table (investments), loan terms (liabilities), recent activity, and an editable historical balance table (edit a daily snapshot → recompute; month-end rows read-only with a “reopen month” admin action). Type page aggregates members. Breadcrumb back to dashboard. Every row on the dashboard links here — nothing more than one click deep.

### 6 · Paystubs
> Build `paystubs/`: a watcher on `data/inbox/paystubs/` (watchdog) and an optional Gmail/IMAP fetcher (`PAYSTUB_IMAP_*` env vars, filter by sender/label, download PDF attachments, run monthly). Parser: `pdfplumber` text → line items; `rules/<employer>.yaml` maps labels to `tax|pretax|posttax|benefit|employer` and captures gross/net/YTD. First unknown employer opens a mapping UI, saves a rule. Paycheck page per the prototype: latest stub segmented bar, line table with YTD, facts (effective tax rate, 401k/HSA vs IRS limits — put limits in a config table by year), and the Gross → Taxes → Pre-tax → Benefits → Spending → Pre-tax back → Match → Market/home residual → Net worth change waterfall for YTD.

### 7 · Property valuation
> Add `property/rentcast.py` (AVM endpoint, API key in `.env`, free-tier rate limiting, cache by address+month). Monthly job writes a `valuations` row and a daily snapshot for property accounts; manual override wins until the next explicit refresh. Show source + date on the account page; compute home equity against linked mortgage account (`account_facts.linked_liability`).

### 8 · Trends + Almanac
> Implement `core/rollups.py` monthly rollups (income, spending excl. transfers, kept, savings rate, NW change, contributions vs market gain = NW change − contributions − kept). Trends page: income vs spending, monthly NW change, category spend (90d), investments vs cash. Almanac page: one row per year (NW Dec 31, change, income, spending, kept, savings rate, contributions, market gain, notes — editable), 2026 month table, records (ATH, best month, milestones crossed with dates). CSV export of every table. Month-end close job on the 1st.

### 9 · Package + publish
> Finish for public release: README with 5-minute Docker Compose quickstart, Tailscale notes, PWA manifest + service worker (cache shell only), nightly SQLite backup to `data/backups/`, CONTRIBUTING with the `Aggregator` plugin contract and a SimpleFIN stub, screenshots, `make demo` that seeds example data from the prototype. Security pass: tokens encrypted, no secrets in logs, CSRF on forms.

## Not in v1
Budgets, multi-user, mobile native, AI insights, bill reminders, Plaid Identity/Income, crypto wallet scanning (manual or Coinbase CSV for now).

## Open items for you
- Plaid: create a dashboard account, request Production access (needs app name, privacy policy URL, use-case description). Start in Sandbox.
- RentCast: sign up for an API key (free tier).
- Paystubs: confirm whether your payroll provider emails PDFs (enables the Gmail fetcher) or portal-only (folder drop).
- Name check: “Orbit” is a common word — the GitHub repo is 20Nick00/orbit; check trademark class 36 and a domain (orbit.money, getorbit.app) before any public launch.
