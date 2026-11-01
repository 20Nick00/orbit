# Contributing

This page covers setup, the checks to run, the project rules, and how to write a new aggregator (a service that connects Orbit to your financial institutions).

## Set up

Set up as in the README's "Run without Docker" section. Then run these checks before you open a pull request. CI (the automatic checks that run on GitHub) runs the same checks.

```bash
ruff check .
ruff format --check .
pytest
node --test tests/service_worker.test.cjs
```

Notes:

- Run the first three with the virtual environment active. `ruff` and `pytest` come from `requirements-dev.txt`.
- The last command needs Node.js installed. The test uses only Node's built-in test runner, so you don't install any packages for it.
- `make test` runs `pytest` with the project's `.venv` Python. On Windows, use `.\make test`.
- `make demo-10k` loads the example data with 10,000 transactions, for speed checks.
- The Plaid sandbox tests are skipped unless `ORBIT_RUN_SANDBOX=1` is set, `PLAID_CLIENT_ID` and `PLAID_SECRET` are set, and `PLAID_ENV` is blank or `sandbox`. When they run, they call Plaid's sandbox over the internet.

## Project rules

- Runtime dependencies go in `requirements.txt`. Test and lint tools go in `requirements-dev.txt`. Pin exact versions with `==`.
- Change database tables only through an Alembic migration (Alembic is the database migration tool):

  ```bash
  alembic revision --autogenerate -m "what changed"
  ```

  After a merge, run `alembic heads`. It should list one head. If it lists more than one, two migrations conflict. Fix that before you merge.
- `core/` must not import from `app/` or from FastAPI.
- `docs/index.html` is the public prototype and the UI reference. `app/static/orbit.css` started as a copy of its Keel styles.

Money and data:

- Money is `Decimal` in code. `Decimal` is Python's exact decimal number type. Never use floats for money.
- The database stores money as whole cents (integers). Share quantities are stored as exact decimal text.
- Liabilities (cards, loans and mortgages) are stored as positive balances, the amount you owe. The `is_liability` flag marks them.
- Never invent numbers. If a value is missing, store nothing and show it as missing.
- Never log secrets, account numbers or paystub text. Keep provider error text out of logs and out of the database.

## Writing an Aggregator plugin

An aggregator connects to your financial institutions and sends account data to Orbit. Plaid is the only live aggregator today. This section describes the contract a new one must follow.

### What is wired up today

Read this first. The contract is small, but it does not cover the whole job.

- The `Aggregator` protocol has five methods. Plaid's class also has `finish` (handles the browser callback after sign-in) and `remove` (removes a connection). The protocol does not list either one. Plaid's `sync_accounts` also takes a `record_balances` keyword that the protocol does not list. The nightly job uses it.
- There is no plugin registry. Nothing loads a plugin by name.
- The Settings page (`app/routers/settings.py`) reads Plaid's tables and calls `PlaidAggregator` directly. It can show and connect only Plaid connections.
- The nightly job (`jobs/nightly.py`) syncs only `plaid_items` rows, using `PlaidAggregator`. The scheduler (`jobs/scheduler.py`) adds that job only when `PLAID_CLIENT_ID` and `PLAID_SECRET` are set.
- Plaid's connection tables (`plaid_items`, `plaid_accounts` and `plaid_link_sessions`) are Plaid-specific. Plan for a new plugin to add its own tables and an Alembic migration.
- CSV and OFX file imports deliberately do not implement this protocol. The protocol describes a live connection that Orbit can sync. A file upload has no live connection. See `NOTES.md`.

So a new provider needs its class, its own link and remove routes (like the ones in `app/routers/settings.py`), its own tables and migration, a section on the Settings page, and a step in the nightly sync.

### The contract

`aggregators/base.py` defines the contract:

```python
@dataclass(frozen=True)
class LinkStart:
    url: str
    state: str


@dataclass(frozen=True)
class AccountBalance:
    account_id: int
    balance: Decimal


@runtime_checkable
class Aggregator(Protocol):
    def link(self, kind, product_set, redirect_base, item=None) -> LinkStart: ...

    def sync_accounts(self, item, *, session=None) -> list[AccountBalance]: ...

    def sync_transactions(self, item, *, session=None) -> None: ...

    def sync_holdings(self, item, *, today=None, session=None) -> None: ...

    def sync_liabilities(self, item, *, session=None) -> None: ...
```

A protocol lists the methods a class must have. Your class does not need to inherit from it. `isinstance(your_object, Aggregator)` checks only that the method names exist. The SimpleFIN test also compares method signatures, and you can copy that check for your own class.

- `LinkStart(url, state)`: `url` is where the browser goes next. `state` is a random value that Orbit saves and checks when the provider sends the browser back.
- `AccountBalance(account_id, balance)`: an Orbit account ID and its balance as a `Decimal`.
- `item` is a connection, either its database row or its ID.

#### `link(kind, product_set, redirect_base, item=None) -> LinkStart`

- **Receives:** `kind` is `"connect"` (a new connection) or `"reauth"` (repair an existing one). `product_set` is `"banking"` or `"investments"`. `redirect_base` is the web address the provider sends the browser back to. Orbit passes its own callback address. `item` is the existing connection, used only for `reauth`.
- **Returns:** a `LinkStart`. Its `url` is the provider's sign-in page.
- **Writes:** a short-lived row that remembers the request. Plaid uses `plaid_link_sessions`, which holds the state, the kind, the connection ID, the encrypted link token and an expiry time.
- **Rules:** reject a `kind` or `product_set` you don't know. Encrypt every token or credential you store with `core.crypto.encrypt`.

#### `sync_accounts(item, *, session=None) -> list[AccountBalance]`

- **Receives:** `item`, the connection. `session` is an open database session. If you pass `None`, the method opens and commits its own session.
- **Returns:** one `AccountBalance` for each account that has a balance from the provider. Accounts without a balance are left out.
- **Writes:** `accounts` rows (name, type, subtype, currency and mask), and a link row that ties each provider account to one Orbit account. Plaid also saves the credit limit as an account fact.
- **Rules:**
  - Set the account type with `infer_type(...)` from `core/types.py`, and set `is_liability` with `is_liability_type(...)`. The built-in type rules know only the names `"plaid"` and `"ofx"`. For any other name, Orbit uses name keywords, then falls back to `other`. To correct a type, add an override with `add_override` in `core/types.py`.
  - Keep the account's currency. Plaid hides non-USD accounts, because totals cannot mix currencies.
  - Never move a provider account to another connection. Plaid raises an error instead.
  - Save balances with `record_daily(account_id, date, balance, source=..., session=...)` from `core/snapshots.py`. Plaid's `sync_accounts` does this itself, unless it is called with `record_balances=False`. The nightly job passes `False` and records the balances itself.
  - `record_daily` replaces the balance already saved for that day, including a balance you edited by hand. The file importers skip days that were set by hand (source `manual`). Plaid does not check.
  - Accounts the provider stops sending are marked with a missing date. They are not deleted.

#### `sync_transactions(item, *, session=None) -> None`

- **Receives:** `item` and `session`, as above.
- **Reads:** the sync cursor saved on the connection. A cursor is the provider's bookmark for where the last sync stopped.
- **Writes:** `transactions` rows. Each row is saved under the provider's transaction ID, in the `external_id` field. Transactions the provider reports as removed are deleted by that ID.
- **Rules:**
  - Dedupe by the provider's stable ID. The database allows only one row per account and ID. Use an upsert (insert the row, or update it if it already exists), so running the same sync again adds no duplicates.
  - Money going out is negative. Plaid sends money out as a positive number, so Orbit negates it.
  - Use `Decimal` amounts.
  - Save the new cursor only after every page has been saved.

#### `sync_holdings(item, *, today=None, session=None) -> None`

- **Receives:** `item`, `today` and `session`. `today` is the date to file the holdings under. The nightly job passes the owner's local date. If you leave `today` as `None`, Plaid uses the computer's own date, not `ORBIT_TIMEZONE`. Always pass `today`.
- **Writes:** `holdings` rows for that date (ticker, name, quantity, value and cost basis). Plaid first deletes that date's rows for the affected accounts, then inserts the new rows. A second run on the same day replaces the day's rows instead of adding to them.
- **Rules:**
  - Use `Decimal` for quantities, values and cost basis. A missing value is stored as empty, never as zero.
  - If the provider says the product is not available for this connection, return without writing. Plaid treats its `OPTIONAL_UNAVAILABLE` error codes this way.

#### `sync_liabilities(item, *, session=None) -> None`

- **Receives:** `item` and `session`.
- **Writes:** `account_facts` rows, each a key and a text value, for loan and card accounts. Examples are a minimum payment, a due date or an interest rate.
- **Rules:**
  - Save only the keys in `FIELDS` in `aggregators/plaid_liabilities.py`. That list is an allowlist. Anything not on it stays out of the database.
  - Save only values the provider sent. Don't fill gaps.

### Rules for every method

- **Credentials:** encrypt before you save, with `core.crypto.encrypt`. Decrypt only when you call the provider, with `core.crypto.decrypt`.
- **Locks:** wrap each sync of a connection in `item_lock(item_id)` from `aggregators/locks.py`. This stops two syncs of the same connection from running at once. The lock works only inside one Orbit process.
- **Errors:** never put provider error text, tokens or account numbers into logs, exception messages or the database. Plaid keeps only the codes listed in `SAFE_ERROR_CODES` (in `aggregators/plaid.py`), and it logs the code and the exception name.
- **Idempotent:** running a sync twice must give the same result as running it once.

### Starting point: the SimpleFIN stub

`aggregators/simplefin.py` is a stub. A stub is a placeholder with the right shape. It has all five methods, and each one raises `NotImplementedError`. It is not connected to the app, and it makes no network calls. Start from it. Here are its first lines, copied from the file:

```python
"""An unwired starting point for a future SimpleFIN integration. No network calls."""

from aggregators.base import AccountBalance, LinkStart


class SimpleFINAggregator:
    def link(self, kind, product_set, redirect_base, item=None) -> LinkStart:
        # Claim the setup token; encrypt credentials with core.crypto.encrypt before saving.
        raise NotImplementedError("SimpleFIN linking is not implemented; use Plaid or file import.")

    def sync_accounts(self, item, *, session=None) -> list[AccountBalance]:
        # Map with core.types.infer_type/is_liability_type; use Decimal balances and
        # core.snapshots.record_daily(source='simplefin'), preserving liability signs.
        raise NotImplementedError("SimpleFIN account and balance sync is not implemented.")
```

A minimal test, based on `tests/aggregators/test_simplefin.py`, goes in `tests/aggregators/`:

```python
import pytest

from aggregators.base import Aggregator
from aggregators.simplefin import SimpleFINAggregator


def test_stub_conforms_to_protocol():
    assert isinstance(SimpleFINAggregator(), Aggregator)


def test_link_is_not_implemented_yet():
    with pytest.raises(NotImplementedError, match="SimpleFIN.*not implemented"):
        SimpleFINAggregator().link("connect", "banking", "http://localhost/callback")
```

Replace each `NotImplementedError` with real code, following the rules above. When a method works, update the stub test that expects it to raise. Then add the connection pieces listed under "What is wired up today."

### Service worker rule

`app/static/sw.js` is the service worker, a small background script the browser runs for the site. It caches only the files listed in `SHELL_PATHS`: styles, scripts, fonts, icons and the offline page.

- Online, browsers always fetch the current files, so your edits show up straight away. The cache is only used when the server can't be reached.
- If you change any file in `SHELL_PATHS`, bump `CACHE_NAME` (for example, `orbit-shell-v1` to `orbit-shell-v2`). A new name makes the browser install a fresh offline copy and delete the old one.
- If you add a file to `SHELL_PATHS`, update the expected list in `tests/test_pwa.py` and the file count in `tests/service_worker.test.cjs`.
- Never add a financial page, an API response or any other private route to `SHELL_PATHS`.

The tests check the exact list of cached files, and they check that private routes are never cached. They do not check that `CACHE_NAME` changed. The Node test also hard-codes the name `orbit-shell-v1`, so a bump means updating that test too.

Run the checks for this rule with:

```bash
node --test tests/service_worker.test.cjs
pytest tests/test_pwa.py
```

### CSRF rule for routes and forms

CSRF protection stops other websites from submitting forms to Orbit on your behalf. Two rules apply to every form and route that changes data.

- Every new POST route must depend on `require_csrf` from `app/security.py`. Copy the pattern used in `app/routers/settings.py`:

  ```python
  @router.post("/example", dependencies=[Depends(require_csrf)])
  def example(): ...
  ```

- Every form that uses POST must include a hidden `csrf_token` field:

  ```html
  <form method="post" action="/example">
    <input type="hidden" name="csrf_token" value="{{ request.state.csrf_token }}">
    <button class="btn">Save</button>
  </form>
  ```

`tests/web/test_csrf.py` checks both rules. It fails if a POST route in the main app lacks `require_csrf`, or if a POST form in `app/templates` lacks the hidden field. Run it with:

```bash
pytest tests/web/test_csrf.py
```
