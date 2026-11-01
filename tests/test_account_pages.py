import json
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.formatting import d_txt2, day_label, money2, month_label, parse_balance
from app.routers.accounts import ERRORS
from app.routers.dashboard import get_today
from core.db import get_engine
from core.demo import ACCTS, DemoRefusedError, seed_demo
from core.detail import DETAIL_RANGES, read_account, read_type
from core.models import (
    Account,
    AccountFact,
    BalanceEdit,
    BalanceSnapshot,
    Holding,
    MonthEndArchive,
    MonthEndReopen,
    MonthEndSnapshot,
    Transaction,
)
from core.rollups import SchemaOutOfDateError
from core.snapshots import close_month, record_daily, reopen_month

TODAY = date(2026, 10, 31)


@pytest.fixture
def history_client(client):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + Path(get_engine().url.database).as_posix())
    command.upgrade(cfg, "head")
    client.app.dependency_overrides[get_today] = lambda: TODAY
    return client


@pytest.fixture
def demo_client(history_client):
    with Session(get_engine()) as session:
        seed_demo(session, TODAY)
        session.commit()
    return history_client


def account_id(name):
    with Session(get_engine()) as session:
        return session.scalar(select(Account.id).where(Account.name == name))


def add_account(*, type_key="cash", balance="500"):
    with Session(get_engine()) as session:
        account = Account(name="Test account", type=type_key)
        session.add(account)
        session.flush()
        key = account.id
        record_daily(key, date(2026, 9, 1), "600", source="import", session=session)
        record_daily(key, TODAY, balance, source="import", session=session)
        session.commit()
        return key


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.charts = []
        self.tables_in_scroll = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "href" in attrs:
            self.links.append(attrs["href"])
        if "data-chart" in attrs:
            self.charts.append(json.loads(attrs["data-chart"]))
        if tag == "table":
            self.tables_in_scroll.append(any("tscroll" in cls.split() for _, cls in self.stack))
        if tag not in {"input", "meta", "link", "br", "img"}:
            self.stack.append((tag, attrs.get("class", "")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def parse(page):
    parser = PageParser()
    parser.feed(page)
    return parser


def test_demo_pages_and_dashboard_one_click_crawl(demo_client):
    with Session(get_engine()) as session:
        ids = session.scalars(select(Account.id)).all()
        types = set(session.scalars(select(Account.type)))
    assert len(ids) == 15 and len(types) == 10
    for key in ids:
        response = demo_client.get(f"/account/{key}")
        assert response.status_code == 200
        assert "Example data" in response.text
        assert 'aria-label="Breadcrumb"' in response.text
        assert 'href="/dashboard"' in response.text
        assert 'href="/type/' in response.text
        assert response.text.count('aria-pressed="true"') == 1
        assert all(parse(response.text).tables_in_scroll)
    for type_key in types:
        response = demo_client.get(f"/type/{type_key}")
        assert response.status_code == 200
        assert 'aria-label="Breadcrumb"' in response.text
    links = parse(demo_client.get("/dashboard").text).links
    destinations = {link for link in links if link.startswith(("/account/", "/type/"))}
    assert len(destinations) == 25
    for link in destinations:
        assert demo_client.get(link).status_code == 200


def test_not_found_and_empty_type(history_client):
    assert history_client.get("/account/999").status_code == 404
    assert history_client.get("/type/unknown").status_code == 404
    assert history_client.get("/account/999/chart").status_code == 404
    assert history_client.get("/type/unknown/chart").status_code == 404
    page = history_client.get("/type/other")
    assert page.status_code == 200
    assert "No accounts of this type yet." in page.text


def test_holdings_terms_and_private_facts(demo_client):
    brokerage = next(a for a in ACCTS if a["id"] == "brk")
    key = account_id(brokerage["name"])
    with Session(get_engine()) as session:
        data = read_account(session, key, TODAY)
        assert len(data["holdings"]) == len(brokerage["holdings"])
        for holding, source in zip(data["holdings"], brokerage["holdings"], strict=True):
            assert holding["ticker"] == source[0]
            assert holding["weight"] == Decimal(str(source[2])) / 100
        session.add(Holding(account_id=key, date=date(2020, 1, 1), ticker="OLD", value="1"))
        session.add_all(
            [
                AccountFact(account_id=key, key="address", value="PRIVATE ADDRESS"),
                AccountFact(account_id=key, key="linked_liability", value="PRIVATE LINK"),
                AccountFact(account_id=key, key="custom_fact", value="Visible fact"),
            ]
        )
        session.commit()
    page = demo_client.get(f"/account/{key}").text
    assert "Holdings" in page and "OLD" not in page
    assert "PRIVATE" not in page and ">subtitle<" not in page
    assert "Custom fact" in page
    checking = demo_client.get(f"/account/{account_id('Total Checking')}").text
    assert "<h3>Holdings</h3>" not in checking
    mortgage = next(a for a in ACCTS if a["type"] == "mortgage")
    with Session(get_engine()) as session:
        data = read_account(session, account_id(mortgage["name"]), TODAY)
        assert data["terms"] and "Rate" in data["terms"]
        assert not data["terms"].keys() & data["facts"].keys()
        assert not {"subtitle", "address", "linked_liability"} & data["facts"].keys()
    page = demo_client.get(f"/account/{account_id(mortgage['name'])}").text
    assert "Loan terms" in page and page.count(">Rate<") == 1


def test_hidden_account_and_type_totals(demo_client):
    key = account_id("Total Checking")
    with Session(get_engine()) as session:
        session.get(Account, key).hidden = True
        session.commit()
        data = read_account(session, key, TODAY)
        assert data["share_side"] is None and data["share_net_worth"] is None
        for type_key in ("cash", "invest", "loans"):
            data = read_type(session, type_key, TODAY)
            signed_total = sum(row["balance"] for row in data["rows"])
            assert data["total"] == (-signed_total if data["is_liability"] else signed_total)
            assert sum(row["bar_pct"] for row in data["rows"]) == pytest.approx(100)
    page = demo_client.get(f"/account/{key}").text
    assert 'Share of assets</div><div class="v num">—' in page
    assert "Total Checking" not in demo_client.get("/type/cash").text


@pytest.mark.parametrize("range_key", DETAIL_RANGES)
def test_chart_routes(demo_client, range_key):
    key = account_id("Total Checking")
    for path in (f"/account/{key}/chart", "/type/cash/chart"):
        response = demo_client.get(path, params={"range": range_key})
        assert response.status_code == 200
        chart = parse(response.text).charts[0]
        assert chart["range"] == range_key and chart["series"]
        assert chart["h"] == 190 and chart["label"] == "Balance over time"
        assert demo_client.get(path, params={"range": "6M"}).status_code == 400


def test_edit_post_updates_dashboard_and_only_affected_days(history_client):
    key = add_account()
    assert 'id="nwBig">$500</div>' in history_client.get("/dashboard").text
    response = history_client.post(
        f"/account/{key}/history/2026-10-31",
        data={"balance": "700.25", "month": "2026-10"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "notice=saved" in response.headers["location"]
    assert 'id="nwBig">$700</div>' in history_client.get("/dashboard").text
    chart = parse(history_client.get(f"/account/{key}/chart?range=3M").text).charts[0]
    assert chart["series"][:-1] == [600] * 60
    assert chart["series"][-1] == 700
    with Session(get_engine()) as session:
        assert session.get(BalanceSnapshot, (key, TODAY)).balance == Decimal("700.25")
        assert session.scalar(select(func.count()).select_from(BalanceEdit)) == 1


@pytest.mark.parametrize("balance", ["abc", "NaN", "1e5", "1.234", "", "Infinity"])
def test_invalid_balance_is_fixed_error_and_no_change(history_client, balance):
    key = add_account()
    response = history_client.post(
        f"/account/{key}/history/2026-10-31",
        data={"balance": balance, "month": "2026-10"},
    )
    assert response.status_code == 200
    assert ERRORS["balance"] in response.text
    with Session(get_engine()) as session:
        assert session.get(BalanceSnapshot, (key, TODAY)).balance == Decimal("500")
        assert not session.scalars(select(BalanceEdit)).all()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://testserver.evil.example"},
    ],
)
@pytest.mark.parametrize("action", ["2026-10-31", "close", "reopen"])
def test_cross_site_posts_rejected(history_client, headers, action):
    key = add_account()
    with Session(get_engine()) as session:
        close_month("2026-09", today=TODAY, session=session)
        session.commit()
    response = history_client.post(
        f"/account/{key}/history/{action}",
        headers=headers,
        data={"balance": "999", "month": "2026-09", "confirm": "2026-09"},
    )
    assert response.status_code == 403
    with Session(get_engine()) as session:
        assert session.get(BalanceSnapshot, (key, TODAY)).balance == Decimal("500")
        assert session.get(MonthEndSnapshot, (key, "2026-09")) is not None
        assert not session.scalars(select(MonthEndReopen)).all()
        assert not session.scalars(select(BalanceEdit)).all()


def test_close_confirm_reopen_and_validation(history_client):
    key = add_account()
    base = f"/account/{key}/history"
    assert (
        ERRORS["not_over"] in history_client.post(base + "/close", data={"month": "2026-10"}).text
    )
    assert (
        "Month closed for all accounts."
        in history_client.post(base + "/close", data={"month": "2026-09"}).text
    )
    page = history_client.get(f"/account/{key}?month=2026-09").text
    assert "Month-end · locked" in page and "ALL accounts" in page
    assert 'form="f-2026-09-01"' not in page
    assert (
        ERRORS["locked"]
        in history_client.post(
            base + "/2026-09-01", data={"month": "2026-09", "balance": "999"}
        ).text
    )
    assert (
        ERRORS["confirm"]
        in history_client.post(
            base + "/reopen", data={"month": "2026-09", "confirm": "2026-09 "}
        ).text
    )
    with Session(get_engine()) as session:
        assert session.get(MonthEndSnapshot, (key, "2026-09")) is not None
        assert not session.scalars(select(MonthEndArchive)).all()
    assert (
        "Month reopened for all accounts."
        in history_client.post(
            base + "/reopen",
            data={"month": "2026-09", "confirm": "2026-09", "reason": "Correction"},
        ).text
    )
    assert (
        ERRORS["not_closed"]
        in history_client.post(
            base + "/reopen", data={"month": "2026-09", "confirm": "2026-09"}
        ).text
    )
    assert (
        ERRORS["date"]
        in history_client.post(
            base + "/2026-02-30", data={"month": "2026-09", "balance": "10"}
        ).text
    )
    assert (
        ERRORS["missing"]
        in history_client.post(
            base + "/2026-09-02", data={"month": "2026-09", "balance": "10"}
        ).text
    )


def test_query_text_never_reflected(history_client):
    key = add_account()
    attack = "<script>alert('INJECTED')</script>"
    page = history_client.get(f"/account/{key}", params={"error": attack, "month": attack}).text
    assert "INJECTED" not in page and "October 2026" in page
    response = history_client.post(
        f"/account/{key}/history/close", data={"month": attack}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "INJECTED" not in response.headers["location"]
    assert ERRORS["month"] in history_client.get(response.headers["location"]).text


def test_liability_owed_and_positive_paydown(history_client):
    key = add_account(type_key="cards", balance="500")
    page = history_client.get(f"/account/{key}").text
    assert 'class="big num">$500</div>' in page
    assert "Changes show the effect on net worth." in page
    assert "Enter the amount owed. A negative amount means overpaid." in page
    assert "Card terms" in page and "No terms recorded yet." in page
    with Session(get_engine()) as session:
        data = read_account(session, key, TODAY)
        assert data["balance"] == 50000
        assert data["deltas"]["1M"]["d"] == 10000
        assert data["chart"]["series"][-1] == 500
        assert data["history"]["rows"][0]["balance_cents"] == 50000
        assert data["history"]["rows"][0]["change"] == 10000
    history_client.post(
        f"/account/{key}/history/2026-10-31", data={"month": "2026-10", "balance": "-10.25"}
    )
    with Session(get_engine()) as session:
        assert session.get(BalanceSnapshot, (key, TODAY)).balance == Decimal("-10.25")


def test_sparse_future_missing_and_mixed_other(history_client):
    with Session(get_engine()) as session:
        asset = Account(name="Asset", type="other")
        debt = Account(name="Debt", type="other", is_liability=True)
        session.add_all([asset, debt])
        session.flush()
        record_daily(asset.id, date(2026, 9, 1), "100", session=session)
        record_daily(asset.id, date(2027, 1, 1), "999", session=session)
        record_daily(debt.id, date(2026, 10, 1), "25", session=session)
        session.commit()
        data = read_type(session, "other", TODAY)
        assert data["total"] == 7500 and not data["is_liability"]
        assert data["share_side"] is None
        assert data["chart"]["series"][:30] == [100] * 30
        assert data["chart"]["series"][30:] == [75] * 31
        missing = Account(name="Missing", type="other")
        session.add(missing)
        session.commit()
        for range_key in DETAIL_RANGES:
            missing_type = read_type(session, "other", TODAY, range_key=range_key)
            assert missing_type["total"] is None
            assert missing_type["chart"]["series"] == []
            assert missing_type["chart"]["dates"] == []
        data = read_account(session, missing.id, TODAY)
        assert data["balance"] is None and data["chart"]["series"] == []


def test_single_currency_account_survives_mixed_dashboard(history_client):
    key = add_account()
    with Session(get_engine()) as session:
        foreign = Account(name="Euro", type="cash", currency="EUR")
        session.add(foreign)
        session.flush()
        record_daily(foreign.id, TODAY, "10", session=session)
        session.commit()
        data = read_account(session, key, TODAY)
        assert data["balance"] == 50000 and data["share_side"] is None
    assert history_client.get(f"/account/{key}").status_code == 200
    assert "different currencies" in history_client.get("/type/cash").text


def test_account_survives_missing_dashboard_cache(history_client, monkeypatch):
    key = add_account()

    def outdated(*args):
        raise SchemaOutOfDateError("Missing cache")

    monkeypatch.setattr("core.detail.read_dashboard", outdated)
    page = history_client.get(f"/account/{key}")
    assert page.status_code == 200
    assert 'class="big num">$500</div>' in page.text
    assert 'Share of assets</div><div class="v num">—' in page.text
    assert "alembic upgrade head" in history_client.get("/type/cash").text


def test_missing_holdings_and_zero_shares(history_client):
    key = add_account(type_key="invest", balance="0")
    with Session(get_engine()) as session:
        session.add_all(
            [
                Holding(account_id=key, date=TODAY, ticker="Known", value="100"),
                Holding(account_id=key, date=TODAY, ticker="Missing", value=None),
            ]
        )
        session.commit()
        data = read_account(session, key, TODAY)
        assert data["share_side"] is None and data["share_net_worth"] is None
        assert all(row["weight"] is None for row in data["holdings"])
        assert data["holdings"][1]["value"] is None


def test_history_navigation_and_global_closed_status(history_client):
    key = add_account()
    with Session(get_engine()) as session:
        close_month("2026-09", today=TODAY, session=session)
        late = Account(name="Late account", type="cash")
        session.add(late)
        session.flush()
        record_daily(late.id, date(2026, 9, 15), "25", session=session)
        session.commit()
        data = read_account(session, late.id, TODAY, month="2026-09")
        assert data["history"]["status"] == "closed"
        assert not data["history"]["own_row"]
        assert data["history"]["rows"][0]["editable"]
        late_key = late.id
        data = read_account(session, key, TODAY, month="invalid")
        assert data["history"]["month"] == "2026-10"
        assert data["history"]["prev_month"] == "2026-09"
        assert data["history"]["next_month"] is None
        data = read_account(session, key, TODAY, month="2026-09")
        assert data["history"]["next_month"] == "2026-10"
        assert data["history"]["own_row"]
    page = history_client.get(f"/account/{late_key}?month=2026-09").text
    assert (
        "Closed for other accounts · this account's figure comes from its daily balances "
        "and can still change" in page
    )
    assert 'aria-label="Balance on 2026-09-15"' in page
    assert "Closed · month-end balances are locked" not in page
    page = history_client.get(f"/account/{key}?month=2026-09").text
    assert "Closed · month-end balances are locked" in page
    assert "Closed for other accounts" not in page


@pytest.mark.parametrize("reopened_with_manual", [False, True])
def test_close_button_freezes_incomplete_file_balances(history_client, reopened_with_manual):
    with Session(get_engine()) as session:
        account = Account(name="File account", type="cash")
        session.add(account)
        session.flush()
        key = account.id
        record_daily(key, date(2026, 9, 15), "100", source="csv", session=session)
        expected = {key: Decimal(100)}
        if reopened_with_manual:
            manual = Account(name="Manual account", type="cash", is_manual=True)
            session.add(manual)
            session.flush()
            record_daily(manual.id, date(2026, 9, 30), "200", session=session)
            expected[manual.id] = Decimal(200)
            assert close_month("2026-09", today=TODAY, session=session) == 2
            assert reopen_month("2026-09", session=session) == 2
        session.commit()
    page = history_client.get(f"/account/{key}?month=2026-09").text
    assert "Close month</button>" in page
    response = history_client.post(f"/account/{key}/history/close", data={"month": "2026-09"})
    assert response.status_code == 200
    assert "Month closed for all accounts." in response.text
    assert "Nothing to close" not in response.text
    assert ERRORS["empty"] not in response.text
    assert "Closed · month-end balances are locked" in response.text
    with Session(get_engine()) as session:
        rows = session.execute(
            select(MonthEndSnapshot.account_id, MonthEndSnapshot.balance).where(
                MonthEndSnapshot.month == "2026-09"
            )
        ).all()
        assert dict(rows) == expected


def test_noop_post_and_recent_ten_edits(history_client):
    key = add_account()
    path = f"/account/{key}/history/2026-10-31"
    response = history_client.post(path, data={"balance": "500", "month": "2026-10"})
    assert "Balance unchanged." in response.text
    with Session(get_engine()) as session:
        assert not session.scalars(select(BalanceEdit)).all()
        assert session.get(BalanceSnapshot, (key, TODAY)).source == "import"
    for value in range(501, 512):
        response = history_client.post(path, data={"balance": str(value), "month": "2026-10"})
        assert "Balance saved." in response.text
    with Session(get_engine()) as session:
        edits = read_account(session, key, TODAY)["edits"]
        assert len(edits) == 10 and edits[0]["new_balance"] == 51100


def test_activity_order_and_fallback_labels(history_client):
    key = add_account()
    with Session(get_engine()) as session:
        session.add_all(
            Transaction(account_id=key, date=TODAY, amount="1.25", merchant=f"Merchant {i}")
            for i in range(16)
        )
        session.add(Transaction(account_id=key, date=TODAY, amount="2", memo="Memo", pending=True))
        session.add(Transaction(account_id=key, date=TODAY, amount="-1.25"))
        session.commit()
        activity = read_account(session, key, TODAY)["activity"]
        assert len(activity) == 15
        assert [r["label"] for r in activity[:3]] == ["—", "Memo", "Merchant 15"]
    page = history_client.get(f"/account/{key}").text
    assert "Pending" in page and "+$2.00" in page and "−$1.25" in page


def test_demo_refuses_archived_accounts(demo_client):
    with Session(get_engine()) as session:
        close_month("2026-09", today=TODAY, session=session)
        reopen_month("2026-09", session=session)
        session.commit()
        with pytest.raises(DemoRefusedError, match="archives"):
            seed_demo(session, TODAY)
        assert session.scalar(select(func.count()).select_from(Account)) == 15


@pytest.mark.parametrize(
    "value, expected",
    [
        (" $1,234.56 ", "1234.56"),
        ("1,234", "1234"),
        ("1,234,567.89", "1234567.89"),
        ("1234.5", "1234.50"),
        ("$1,234.50", "1234.50"),
        ("−$12.34", "-12.34"),
        ("-0.25", "-0.25"),
        (".50", "0.50"),
        ("0", "0.00"),
        ("92233720368547758.07", "92233720368547758.07"),
    ],
)
def test_parse_balance(value, expected):
    assert parse_balance(value) == Decimal(expected)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "abc",
        "NaN",
        "Infinity",
        "1e5",
        "1.234",
        "--2",
        "12,34",
        "1,5",
        "1,2345",
        ",123",
        "92233720368547758.08",
    ],
)
def test_parse_balance_rejects_invalid(value):
    with pytest.raises(ValueError):
        parse_balance(value)


def test_display_filters():
    assert money2(123456) == "$1,234.56"
    assert money2(-123456) == "−$1,234.56"
    assert money2(0) == "$0.00" and money2(None) == "—"
    assert day_label(date(2026, 10, 3)) == "Oct 3, 2026"
    assert day_label("2026-10-03") == "Oct 3, 2026"
    assert month_label("2026-09") == "September 2026"


@pytest.mark.parametrize(
    "value, expected",
    [(30, "+$0.30"), (-30, "−$0.30"), (0, "$0.00"), (None, "—"), (123456, "+$1,234.56")],
)
def test_signed_two_decimal_filter(value, expected):
    assert d_txt2(value) == expected


def test_carried_forward_lock_page_and_reopen_link(history_client):
    with Session(get_engine()) as session:
        account = Account(name="Home", type="property")
        session.add(account)
        session.flush()
        key = account.id
        record_daily(key, date(2026, 8, 31), "500", session=session)
        close_month("2026-09", today=TODAY, session=session)
        close_month("2026-10", today=date(2026, 11, 1), session=session)
        session.commit()
    history_client.app.dependency_overrides[get_today] = lambda: date(2026, 11, 1)
    page = history_client.get(f"/account/{key}?month=2026-08").text
    assert "Locked · used by October 2026 month-end" in page
    assert 'aria-label="Balance on 2026-08-31"' not in page
    assert 'href="?month=2026-10#admin"' in page
    assert 'id="admin"' in history_client.get(f"/account/{key}?month=2026-10").text
    response = history_client.post(
        f"/account/{key}/history/2026-08-31", data={"month": "2026-08", "balance": "600"}
    )
    assert ERRORS["locked"] in response.text
    for month in ("2026-10", "2026-09"):
        response = history_client.post(
            f"/account/{key}/history/reopen", data={"month": month, "confirm": month}
        )
        assert "Month reopened for all accounts." in response.text
    page = history_client.get(f"/account/{key}?month=2026-08").text
    assert 'aria-label="Balance on 2026-08-31"' in page


def test_daily_record_note_cents_and_source(history_client):
    key = add_account()
    with Session(get_engine()) as session:
        close_month("2026-09", today=TODAY, session=session)
        record_daily(key, date(2026, 9, 1), "600.30", source="demo", session=session)
        record_daily(key, date(2026, 10, 1), "600.60", source="demo", session=session)
        session.commit()
    page = history_client.get(f"/account/{key}?month=2026-09").text
    assert "$600.00" in page and "Month-end · locked" in page
    assert "Daily record: $600.30" in page and ">Demo<br>" in page
    page = history_client.get(f"/account/{key}?month=2026-10").text
    assert "+$0.30" in page


@pytest.mark.parametrize("already_closed", [False, True])
def test_close_nothing_reports_error(history_client, already_closed):
    key = add_account()
    month = "2026-09" if already_closed else "2026-08"
    if already_closed:
        with Session(get_engine()) as session:
            close_month(month, today=TODAY, session=session)
            session.commit()
    response = history_client.post(
        f"/account/{key}/history/close", data={"month": month}, follow_redirects=False
    )
    assert response.status_code == 303 and "error=empty" in response.headers["location"]
    page = history_client.get(response.headers["location"]).text
    assert ERRORS["empty"] in page
    assert "Month closed for all accounts." not in page


def test_web_reopen_and_edit_refresh_dashboard_monthly_chart(history_client):
    key = add_account()
    with Session(get_engine()) as session:
        close_month("2026-09", today=TODAY, session=session)
        record_daily(key, date(2026, 9, 1), "650", session=session)
        session.commit()

    def september_value():
        chart = parse(history_client.get("/dashboard").text).charts[0]
        assert chart["range"] == "1Y"
        return chart["series"][chart["dates"].index("2026-09-30")]

    assert september_value() == 600
    response = history_client.post(
        f"/account/{key}/history/reopen", data={"month": "2026-09", "confirm": "2026-09"}
    )
    assert "Month reopened for all accounts." in response.text
    response = history_client.post(
        f"/account/{key}/history/2026-09-01", data={"month": "2026-09", "balance": "700"}
    )
    assert "Balance saved." in response.text
    assert september_value() == 700


def test_history_timestamps_are_local_dates(history_client):
    key = add_account()
    # 01:00 UTC is the previous evening on a New York server.
    stamp = datetime(2026, 10, 31, 1)
    expected = stamp.replace(tzinfo=UTC).astimezone().date()
    with Session(get_engine()) as session:
        session.add(
            BalanceEdit(
                account_id=key,
                date=TODAY,
                old_balance="501",
                new_balance="500",
                old_source="manual",
                edited_at=stamp,
            )
        )
        reopen = MonthEndReopen(month="2026-09", deleting=True, reopened_at=stamp)
        session.add(reopen)
        session.flush()
        session.add(MonthEndArchive(reopen_id=reopen.id, account_id=key, balance="600"))
        session.flush()
        reopen.deleting = False
        session.commit()
        detail = read_account(session, key, TODAY, month="2026-09")
        assert detail["edits"][0]["edited_at"] == expected
        assert detail["history"]["reopens"][0]["reopened_at"] == expected
        assert type(detail["edits"][0]["edited_at"]) is date
        assert type(detail["history"]["reopens"][0]["reopened_at"]) is date


@pytest.mark.parametrize("revision", ["0001", "0004"])
def test_pages_on_real_older_database(client, revision):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + Path(get_engine().url.database).as_posix())
    command.upgrade(cfg, revision)
    client.app.dependency_overrides[get_today] = lambda: TODAY
    key = add_account() if revision == "0004" else 1
    notice = "The database needs updating. Run: alembic upgrade head"
    for path in (f"/account/{key}", f"/account/{key}/chart", "/type/cash", "/type/cash/chart"):
        response = client.get(path)
        assert response.status_code == 200
        # Type views need only the tables already present in 0004. Account
        # views also read audit tables introduced by 0005.
        if revision == "0001" or path.startswith("/account/"):
            assert notice in response.text
        else:
            assert parse(response.text).charts[0]["series"]
