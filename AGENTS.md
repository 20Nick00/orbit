# Orbit: project rules

Open-source, self-hosted wealth tracker. Assets left, net worth center, liabilities right. No budgets, no AI widgets, no upsells. The plan and data model are in `BUILD_PLAN.md`. The UI reference is `docs/index.html` (the prototype); do not break it or `publish.bat`.

## Stack
- Python 3.12, FastAPI, SQLAlchemy 2, SQLite (WAL), Jinja2 + HTMX + vanilla JS, hand-drawn SVG charts. No frontend build step.
- Dependencies live in `.venv`, pinned in `requirements.txt` (runtime) and `requirements-dev.txt` (tests and lint). Ask Nick before adding one.
- Checks, from the project root: `.venv\Scripts\python.exe -m pytest`, `-m ruff check .`, `-m ruff format --check .`.
- Commands: `.\make demo` loads example accounts, `.\make run` starts the app at http://127.0.0.1:8000, `.\make test` runs the tests (`make <target>` outside Windows; set `PYTHON` to the project's `.venv` Python inside a worktree).
- Docker isn't installed on Nick's PC, so the Docker files can only be tested by CI once the repo is on GitHub.

## Rules
- Money is `Decimal` in code and integer cents in the database. Never use floats for stored amounts.
- Liabilities are stored as positive balances (amount owed), flagged by `accounts.is_liability`.
- Never invent numbers. If data is missing, show that it is missing.
- The dashboard reads only the `core/rollups.py` cache. Triggers empty it when balances or accounts change, and the next read rebuilds it; a batch writer may call `rollups.rebuild` once to warm it.
- This repo is meant to go public through `publish.bat`. Real pay data, employer rules and secrets must stay out of Git: `data/` (which holds the live employer rules, in `data/paystubs/rules/`), `.env` and `paystubs/rules/*` (except `example.yaml`) are ignored.
- Never log paystub text, account numbers or credentials.
- IMAP access is read-only: never delete, move or mark mail.

## Working alongside other sessions
- Several sessions may be building Orbit at once. Work in your own Git worktree under `.claude/worktrees/`, not the main checkout.
- In the main checkout, commit by explicit path. Never `git add -A`: it picks up other sessions' files.
- The GitHub repo is public and `main` starts from one clean commit. Never merge, rebase onto or push a branch older than that commit, and start new worktrees from current `main`. Only upload when Nick asks, through `publish.bat`.

## Docs
- `BUILD_PLAN.md`: decisions, data model, session prompts. Read before adding a feature.
- `FEATURES.md`: curated feature spec. Read when deciding what a screen should do.
- `docs/ui-research.md`: research behind the design. Read only for UI design questions.
