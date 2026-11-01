import csv
import datetime as dt
import io
import logging
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import quote, quote_plus

import httpx
import pytest

from app.csv_export import csv_response
from app.routers import almanac, dashboard, property, trends
from core.db import session_scope
from core.models import Account, AccountFact, AlmanacNote, MonthEndSnapshot, Transaction
from core.snapshots import record_daily
from property import valuation as valuation_service
from property.rentcast import RentCastClient
from property.valuation import refresh, valuation_status

TODAY = dt.date(2026, 3, 15)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    for module in (almanac, property, trends, dashboard, valuation_service):
        monkeypatch.setattr(module, "local_today", lambda: TODAY)


@pytest.fixture
def home(client):
    with session_scope() as active:
        account = Account(name="Test home", type="property")
        debt = Account(name="Mortgage", type="mortgage")
        active.add_all([account, debt])
        active.flush()
        active.add(AccountFact(account_id=account.id, key="address", value="Private address"))
        return account.id


@pytest.fixture
def data(client, home):
    with session_scope() as active:
        bank = Account(name="Bank", type="cash")
        active.add(bank)
        active.flush()
        active.add_all(
            [
                MonthEndSnapshot(account_id=bank.id, month="2025-12", balance=1000),
                MonthEndSnapshot(account_id=bank.id, month="2026-01", balance=1100),
                Transaction(account_id=bank.id, date=TODAY, amount=200, category="salary"),
                Transaction(account_id=bank.id, date=TODAY, amount=-50, category='Food, "home"'),
            ]
        )
        record_daily(bank.id, TODAY, 1250, session=active)
        record_daily(home, TODAY, 100000, session=active)
    return home


@pytest.mark.parametrize("path", ["/trends", "/almanac"])
def test_empty_pages(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "No data yet." in response.text
    assert f'href="{path}" aria-current="page"' in response.text


def test_populated_pages_and_ytd(client, data):
    response = client.get("/almanac")
    assert response.status_code == 200
    assert 'class="ytd"' in response.text and "<small>YTD</small>" in response.text
    assert 'class="tot"' in response.text
    assert "Contributions" in response.text and "Market gain" in response.text
    assert "2026, month by month" in response.text
    response = client.get("/trends")
    assert response.status_code == 200
    assert 'type="application/json"' in response.text
    assert "200.00" in response.text and "Food" in response.text
    response = client.get(f"/account/{data}/valuation")
    assert response.status_code == 200 and "Manual value" in response.text
    assert "RentCast is not configured" in response.text


def test_notes_save_escape_and_htmx(client, data):
    note = '<script>alert("x")</script> =SUM(A1:A2)'
    response = client.post("/almanac/notes/2026", data={"note": note}, follow_redirects=False)
    assert response.status_code == 303
    with session_scope() as active:
        assert active.get(AlmanacNote, 2026).note == note
    page = client.get("/almanac").text
    assert '<script>alert("x")</script>' not in page
    assert "&lt;script&gt;" in page
    assert 'hx-post="/almanac/notes/2026"' in page and 'hx-trigger="change"' in page
    response = client.post(
        "/almanac/notes/2026", data={"note": "<b>Updated</b>"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200 and "Saved" in response.text
    assert "&lt;b&gt;Updated&lt;/b&gt;" in response.text


@pytest.mark.parametrize("year,note", [(1899, "x"), (2201, "x"), (2026, "x" * 2001)])
def test_notes_limits(client, year, note):
    response = client.post(f"/almanac/notes/{year}", data={"note": note})
    assert response.status_code == 200
    assert 'role="alert"' in response.text and "at most 2000 characters" in response.text
    assert note in response.text
    with session_scope() as active:
        assert active.get(AlmanacNote, year) is None


@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("valuation/override", {"value": "100"}),
        ("valuation/refresh", {}),
        ("property-details", {"address": "Private address"}),
    ],
)
def test_foreign_origin_on_every_property_post(client, home, suffix, payload):
    response = client.post(
        f"/account/{home}/{suffix}", data=payload, headers={"Origin": "https://foreign.example"}
    )
    assert response.status_code == 403


def test_note_origin_and_referer_checks(client):
    for headers in (
        {"Origin": "null"},
        {"Origin": "http://foreign.example"},
        {"Referer": "http://foreign.example/path"},
    ):
        assert (
            client.post("/almanac/notes/2026", data={"note": "x"}, headers=headers).status_code
            == 403
        )
    assert (
        client.post(
            "/almanac/notes/2026", data={"note": "x"}, headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_override_parsing_and_invalid_amounts(client, home):
    response = client.post(f"/account/{home}/valuation/override", data={"value": "$1,234.50"})
    assert response.status_code == 200 and "wins until you refresh" in response.text
    assert valuation_status(home)["value"] == Decimal("1234.50")
    for value in ("junk", "0", "-1", "NaN", "12,34", "Infinity", "1e20"):
        response = client.post(f"/account/{home}/valuation/override", data={"value": value})
        assert response.status_code == 200 and "Enter a positive amount" in response.text
        assert valuation_status(home)["value"] == Decimal("1234.50")


def test_property_details_link_clear_and_reject_asset(client, home):
    with session_scope() as active:
        other = Account(name="Cash", type="cash")
        active.add(other)
        active.flush()
        other_id = other.id
    url = f"/account/{home}/property-details"
    response = client.post(url, data={"address": "New private address", "linked_liability": "2"})
    assert response.status_code == 200
    assert valuation_status(home)["linked_liability"] == "2"
    client.post(url, data={"address": "New private address", "linked_liability": ""})
    assert valuation_status(home)["linked_liability"] is None
    response = client.post(url, data={"address": "x", "linked_liability": str(other_id)})
    assert "Choose a visible liability" in response.text
    assert valuation_status(home)["address"] == "New private address"
    assert client.get(f"/account/{other_id}/valuation").status_code == 404
    assert client.get("/account/9999/valuation").status_code == 404


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "authenticate"),
        (404, "no estimate"),
        (429, "could not be refreshed"),
        (500, "could not be refreshed"),
    ],
)
def test_refresh_errors_are_plain_and_private(client, home, monkeypatch, caplog, status, message):
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="httpx")
    rentcast = RentCastClient(
        "private-key", transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )
    monkeypatch.setattr(
        property,
        "refresh",
        lambda account_id, **kwargs: refresh(account_id, client=rentcast, **kwargs),
    )
    response = client.post(f"/account/{home}/valuation/refresh")
    assert response.status_code == 200 and message in response.text
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    for private in (
        "Private address",
        quote("Private address"),
        quote_plus("Private address"),
        "private-key",
    ):
        assert private not in caplog.text


def test_no_key_and_quota_messages(client, home, monkeypatch):
    response = client.post(f"/account/{home}/valuation/refresh")
    assert response.status_code == 200 and "not configured" in response.text
    from property.rentcast import QuotaExceededError

    def quota(*args, **kwargs):
        raise QuotaExceededError("Limit reached")

    monkeypatch.setattr(property, "refresh", quota)
    response = client.post(f"/account/{home}/valuation/refresh")
    assert response.status_code == 200 and "RentCast request limit reached" in response.text


def test_csv_formula_guard_quoting_bom_and_numbers():
    response = csv_response(
        "test.csv",
        ("Text", "Amount", "Missing"),
        [
            ('Food, "home"', Decimal("-12.30"), None),
            ("=1+1", 10, ""),
            ("+formula", -4, ""),
            ("-formula", 0, ""),
            ("@formula", 0, ""),
            ("\tformula", 0, ""),
            ("\rformula", 0, ""),
        ],
    )
    assert response.body.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert rows[1] == ['Food, "home"', "-12.30", ""]
    assert all(row[0].startswith("'") for row in rows[2:])
    assert rows[3][1] == "-4"


def test_every_csv_link_works_and_every_table_has_export(client, data):
    links = set()

    class ExportParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
            self.tables = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "table":
                self.tables += 1
            if tag == "a" and "csv" in attrs.get("class", "").split():
                self.links.append(attrs["href"])

    for path in ("/almanac", "/trends", f"/account/{data}/valuation"):
        parser = ExportParser()
        parser.feed(client.get(path).text)
        assert len(parser.links) >= parser.tables
        links.update(parser.links)
    assert links == {
        "/export/almanac-years.csv",
        "/export/almanac-months.csv",
        "/export/almanac-records.csv",
        "/export/trends-monthly.csv",
        "/export/trends-categories.csv",
        "/export/trends-mix.csv",
        f"/export/account-{data}-valuations.csv",
    }
    for link in links:
        response = client.get(link)
        assert response.status_code == 200, link
        assert response.headers["content-type"].startswith("text/csv"), link
        assert response.content.startswith(b"\xef\xbb\xbf"), link


def test_exports_have_real_data_and_safe_notes(client, data):
    client.post("/almanac/notes/2026", data={"note": "=SUM(A1:A2)"})
    years = client.get("/export/almanac-years.csv").content.decode("utf-8-sig")
    assert "'=SUM(A1:A2)" in years
    client.post(f"/account/{data}/valuation/override", data={"value": "1234.50"})
    history = list(
        csv.reader(
            io.StringIO(
                client.get(f"/export/account-{data}-valuations.csv").content.decode("utf-8-sig")
            )
        )
    )
    assert history[1] == ["2026-03-15", "1234.50", "manual"]
    categories = list(
        csv.reader(
            io.StringIO(client.get("/export/trends-categories.csv").content.decode("utf-8-sig"))
        )
    )
    assert categories[1] == ['Food, "home"', "50.00"]


def test_mixed_currency_is_plain_message(client):
    with session_scope() as active:
        for currency in ("USD", "EUR"):
            account = Account(name=currency, type="cash", currency=currency)
            active.add(account)
            active.flush()
            record_daily(account.id, TODAY, 100, session=active)
    for path in ("/trends", "/almanac", "/export/trends-monthly.csv"):
        response = client.get(path)
        assert response.status_code == 400 and "different currencies" in response.text


def test_scheduler_lifespan_starts_and_stops_only_when_enabled(monkeypatch):
    from app import main
    from core.config import get_settings
    from tests.csrf_client import TestClient

    calls = []

    class FakeScheduler:
        def start(self):
            calls.append("start")

        def shutdown(self, *, wait):
            assert wait is True
            calls.append("stop")

    monkeypatch.setattr(main, "build_scheduler", lambda settings: FakeScheduler())
    with TestClient(main.create_app()):
        assert calls == []
    monkeypatch.setenv("ORBIT_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(main.create_app()):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


@pytest.mark.parametrize("htmx", [False, True])
def test_note_newlines_and_visible_length_error_preserve_input(client, htmx):
    headers = {"HX-Request": "true"} if htmx else {}
    response = client.post("/almanac/notes/2026", data={"note": "x\r\n" * 1000}, headers=headers)
    assert response.status_code == 200
    with session_scope() as active:
        assert active.get(AlmanacNote, 2026).note == "x\n" * 1000
    typed = "<b>Keep this typed note</b>" + "x" * 2001
    response = client.post("/almanac/notes/2026", data={"note": typed}, headers=headers)
    assert response.status_code == 200 and 'role="alert"' in response.text
    assert "&lt;b&gt;Keep this typed note&lt;/b&gt;" + "x" * 2001 in response.text
    assert "Saved" not in response.text
    with session_scope() as active:
        assert active.get(AlmanacNote, 2026).note == "x\n" * 1000


@pytest.mark.parametrize("target", ["property", "note", "note_htmx"])
def test_database_write_errors_are_visible_private_and_rolled_back(
    client, home, caplog, monkeypatch, target
):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from core.db import get_engine

    engine = get_engine()
    assert engine.hide_parameters
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    monkeypatch.setattr(logging.getLogger("sqlalchemy.engine.Engine"), "disabled", False)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="httpx")
    # Exercise SQL logging as well as the exception path; neither may log values.
    monkeypatch.setattr(engine, "echo", True)
    rollbacks = []
    real_rollback = Session.rollback

    def rollback(active):
        rollbacks.append(True)
        return real_rollback(active)

    monkeypatch.setattr(Session, "rollback", rollback)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if target == "property" and "INSERT INTO account_facts" in statement:
            # Fail the second write, proving the address write also rolls back.
            if "linked_liability" in parameters:
                raise OperationalError(statement, parameters, RuntimeError("forced write error"))
        if target != "property" and "INSERT INTO almanac_notes" in statement:
            raise OperationalError(statement, parameters, RuntimeError("forced write error"))

    address = "123 Secret Address / Unit 9"
    note = "Secret note about my family"
    event.listen(engine, "before_cursor_execute", fail)
    try:
        if target == "property":
            response = client.post(
                f"/account/{home}/property-details",
                data={"address": address, "linked_liability": "none"},
            )
            assert "Property details could not be saved" in response.text
        else:
            response = client.post(
                "/almanac/notes/2026",
                data={"note": note},
                headers={"HX-Request": "true"} if target == "note_htmx" else {},
            )
            assert 'role="alert"' in response.text and "could not be saved" in response.text
            assert note in response.text
        assert response.status_code == 200 and rollbacks
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    for private in (address, quote(address, safe=""), quote_plus(address), note):
        assert private not in caplog.text
    with session_scope() as active:
        assert active.get(AccountFact, (home, "address")).value == "Private address"
        assert active.get(AccountFact, (home, "linked_liability")) is None
        assert active.get(AlmanacNote, 2026) is None


def test_property_multiselect_and_paid_off_choice(client, home):
    with session_scope() as active:
        heloc = Account(name="HELOC", type="loans")
        hidden = Account(name="Hidden loan", type="loans", hidden=True)
        active.add_all([heloc, hidden])
        active.flush()
        heloc_id, hidden_id = heloc.id, hidden.id
    url = f"/account/{home}/property-details"
    response = client.post(url, data={"linked_liability": ["2", str(heloc_id)]})
    assert response.status_code == 200
    assert valuation_status(home)["linked_liability"] == f"2,{heloc_id}"
    assert 'name="linked_liability" multiple' in response.text
    assert f'value="{heloc_id}" selected' in response.text
    assert 'value="2" selected' in response.text
    assert "Hidden loan" not in response.text
    response = client.post(url, data={"linked_liability": ["none", str(heloc_id)]})
    assert "on its own" in response.text
    assert valuation_status(home)["linked_liability"] == f"2,{heloc_id}"
    response = client.post(url, data={"linked_liability": ["2", str(hidden_id)]})
    assert "Choose a visible liability" in response.text
    assert valuation_status(home)["linked_liability"] == f"2,{heloc_id}"
    response = client.post(url, data={"linked_liability": "none"})
    assert valuation_status(home)["linked_liability"] == "none"
    assert 'value="none" selected' in response.text


def test_almanac_omits_empty_months_and_csv_rates_have_four_decimals(client):
    with session_scope() as active:
        bank = Account(name="Bank", type="cash")
        active.add(bank)
        active.flush()
        # The first data is February, and there is no March data.
        day = dt.date(2026, 2, 10)
        active.add_all(
            [
                Transaction(account_id=bank.id, date=day, amount=3, category="Salary"),
                Transaction(account_id=bank.id, date=day, amount=-1, category="Food"),
            ]
        )
    page = client.get("/almanac").text
    assert "2026-01" not in page and "2026-03" not in page
    assert "2026-02" in page
    for path in (
        "/export/almanac-months.csv",
        "/export/almanac-years.csv",
        "/export/trends-monthly.csv",
    ):
        rows = list(csv.DictReader(io.StringIO(client.get(path).content.decode("utf-8-sig"))))
        populated = [r for r in rows if r["Income"] == "3.00"]
        assert len(populated) == 1
        assert populated[0]["Savings rate"] == "0.6667"
        assert populated[0]["Spending"] == "1.00"
        if path == "/export/almanac-months.csv":
            assert len(rows) == 1 and rows[0]["Month"] == "2026-02"
    records = list(
        csv.reader(
            io.StringIO(client.get("/export/almanac-records.csv").content.decode("utf-8-sig"))
        )
    )
    assert ["Average savings rate", "0.6667", ""] in records


@pytest.mark.parametrize("path", ["/dashboard", "/trends", "/almanac"])
def test_navigation_once_and_current_page(client, path):
    page = client.get(path).text
    for href, label in (
        ("/dashboard", "Dashboard"),
        ("/trends", "Trends"),
        ("/almanac", "Almanac"),
    ):
        # Once in the top bar and once in the phone tabs.
        assert page.count(f'href="{href}"') == 2
        suffix = ' aria-current="page"' if href == path else ""
        assert f'<a href="{href}"{suffix}>{label}</a>' in page
    assert page.count('aria-current="page"') == 2


def test_dashboard_home_equity_note_keeps_existing_fact_markup(client, home):
    with session_scope() as active:
        paid = Account(name="Paid-off home", type="property")
        active.add(paid)
        active.flush()
        active.add(AccountFact(account_id=paid.id, key="linked_liability", value="none"))
        record_daily(paid.id, TODAY, 500, session=active)
        record_daily(home, TODAY, 900, session=active)
    page = client.get("/dashboard").text
    assert (
        '<div class="k">Home equity</div><div class="v num">$500</div>'
        "<small>1 home not linked</small>" in page
    )
    assert page.count('<div class="k">Home equity</div>') == 1


@pytest.mark.parametrize("path", ["/almanac", "/trends"])
def test_report_sql_count_does_not_grow_with_history(client, path):
    from sqlalchemy import event

    from core.db import get_engine

    with session_scope() as active:
        bank = Account(name="Bank", type="cash")
        active.add(bank)
        active.flush()
        account_id = bank.id
        record_daily(account_id, TODAY, 100, session=active)
    statements = []

    def count(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", count)
    try:
        assert client.get(path).status_code == 200
        short_count = len(statements)
        with session_scope() as active:
            for year in range(2020, 2026):
                for month in range(1, 13):
                    record_daily(account_id, dt.date(year, month, 15), 100, session=active)
        statements.clear()
        assert client.get(path).status_code == 200
        assert len(statements) == short_count <= 10
        history_reads = [
            sql
            for sql in statements
            if "FROM balance_snapshots" in sql and "min(" not in sql and "strftime(" not in sql
        ]
        assert len(history_reads) == 1
    finally:
        event.remove(engine, "before_cursor_execute", count)


def test_reporting_scale(client, record_property):
    """Record end-to-end timings on the review's data size without a flaky CI cutoff."""
    from statistics import median
    from time import perf_counter

    from sqlalchemy import insert

    from core.models import BalanceSnapshot

    first = TODAY - dt.timedelta(days=999)
    with session_scope() as active:
        accounts = [Account(name=f"Scale bank {i}", type="cash") for i in range(10)]
        active.add_all(accounts)
        active.flush()
        active.execute(
            insert(BalanceSnapshot),
            [
                dict(
                    account_id=account.id,
                    date=first + dt.timedelta(days=i),
                    balance=Decimal(1000 + i),
                    source="manual",
                )
                for account in accounts
                for i in range(1000)
            ],
        )
        active.execute(
            insert(Transaction),
            [
                dict(
                    account_id=accounts[i % 10].id,
                    date=first + dt.timedelta(days=i % 1000),
                    amount=Decimal(10) if i % 2 else Decimal(-5),
                    category="Salary" if i % 2 else "Food",
                )
                for i in range(14000)
            ],
        )
    for path in ("/almanac", "/trends"):
        timings = []
        for _ in range(3):
            start = perf_counter()
            response = client.get(path)
            timings.append((perf_counter() - start) * 1000)
            assert response.status_code == 200
        elapsed = median(timings)
        record_property(path[1:] + "_median_ms", round(elapsed, 1))
        print(f"{path}: {elapsed:.1f} ms median (target <300 ms; 14k transactions, 10k snapshots)")
