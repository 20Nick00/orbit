import re
from datetime import date
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.routers.add import MAX_UPLOAD
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    ImportRule,
    Institution,
    Paystub,
    PaystubLine,
    Transaction,
)
from core.snapshots import close_month, net_worth_series, record_daily
from paystubs.config import Settings


def upload(client, data, *, filename="fake.csv", **fields):
    return client.post(
        "/add/import", files={"file": (filename, data)}, data={"name": "Fake checking", **fields}
    )


def token(response):
    return re.search(r'name="token" value="([0-9a-f]{32})"', response.text)[1]


def test_add_cards_and_global_button(client):
    response = client.get("/add")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(".sources .src")
    assert len(cards) == 6
    assert [card["href"] for card in cards] == [
        "/settings",
        "/add/import",
        "/add/manual",
        "/paycheck",
        "/add/manual?type=crypto",
        "/add/manual?type=property",
    ]
    assert client.get(cards[0]["href"]).status_code == 200
    assert 'href="/add"' in client.get("/dashboard").text


def test_mapping_rule_end_to_end(client, session, fixture_bytes):
    data = b"When,What,Value\n" + fixture_bytes("no-header.csv")
    response = upload(client, data)
    held = token(response)
    assert "When" in response.text
    assert session.scalar(select(Account)) is None
    response = client.post(
        "/add/import/confirm",
        data={
            "token": held,
            "date": "0",
            "memo": "1",
            "amount": "2",
            "date_format": "auto",
            "decimal": ".",
        },
    )
    assert response.status_code == 200 and "2 added" in response.text
    rule = session.scalar(select(ImportRule))
    assert rule.source_kind == "csv" and rule.column_map["roles"]["memo"] == 1
    account = session.scalar(select(Account))
    folder = Settings.from_env().data_dir / "inbox" / "imports"
    assert not list(folder.glob(f"{held}.*"))
    response = upload(client, data, account_id=str(account.id))
    assert "0 added" in response.text and "2 duplicates skipped" in response.text
    assert 'name="token"' not in response.text


@pytest.mark.parametrize(
    ("data", "type", "expected"),
    [
        ("Description,Value\nFake item,-1\n", "cash", "Choose a date column"),
        ("Date,Amount,Transaction Amount\n2026-10-30,-1,-1\n", "cash", "Choose a date column"),
        ("Date,Amount\n2026-10-30,nope\n2026-10-31,-1\n", "cash", "90%"),
        ("Date,Amount\nwrong,-1\n2026-10-31,-1\n", "cash", "dates"),
        ("Date,Amount\n01/02/2026,-1\n02/03/2026,-1\n", "cash", "How are dates"),
        ("Date,Amount\n2026-10-30,1\n2026-10-31,-1\n", "cards", "a card purchase looks like"),
        (
            "Date,Debit,Credit,Balance\n2026-10-30,1,,10\n2026-10-31,1,,-10\n",
            "cards",
            "money you owe is shown as",
        ),
    ],
)
def test_each_ambiguity_reaches_form(client, session, data, type, expected):
    response = upload(client, data.encode(), type=type)
    assert response.status_code == 200 and expected in response.text
    assert token(response)
    assert session.scalar(select(Transaction)) is None
    assert session.scalar(select(Account)) is None


def test_unusable_mapping_rerenders(client, fixture_bytes):
    response = upload(client, fixture_bytes("no-header.csv"))
    response = client.post(
        "/add/import/confirm",
        data={"token": token(response), "date": "0", "amount": "1", "decimal": "."},
    )
    assert "mapping still" in response.text
    assert 'name="token"' in response.text


@pytest.mark.parametrize(
    ("sign", "phrase"), [("positive", "You owe $100.00"), ("negative", "credit of $100.00")]
)
def test_ofx_card_question_web(client, fixture_bytes, sign, phrase):
    response = upload(client, fixture_bytes("card.qfx"), filename="fake.qfx")
    assert "What you owe" in response.text and "A credit in your favour" in response.text
    response = client.post(
        "/add/import/confirm",
        data={"token": token(response), "account_id_0": "new", "liability_balance_sign_0": sign},
    )
    assert phrase in response.text
    assert "2026-10-31" in response.text


def test_security_and_upload_limits(client):
    for route in ("/add/import", "/add/import/confirm", "/add/manual", "/account/1/manual"):
        assert client.post(route, headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.post(route, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    response = upload(client, b"x" * (MAX_UPLOAD + 1))
    assert response.status_code == 200 and "10 MB" in response.text
    assert client.post("/add/import/confirm", data={"token": "../../private"}).status_code == 400
    response = client.post("/add/import/confirm", data={"token": "a" * 32})
    assert response.status_code == 200 and "expired" in response.text


def test_held_file_expiry(client, fixture_bytes):
    import os
    import time

    response = upload(client, fixture_bytes("no-header.csv"))
    held = token(response)
    folder = Settings.from_env().data_dir / "inbox" / "imports"
    for path in folder.glob(f"{held}.*"):
        os.utime(path, (time.time() - 90000, time.time() - 90000))
    upload(client, fixture_bytes("preamble.csv"))
    assert not list(folder.glob(f"{held}.*"))


def test_manual_web_create_edit_note_and_snapshot(client, session):
    response = client.post(
        "/add/manual",
        data={"name": "Fake home", "type": "property", "value": "123,456.78", "note": "Fake note"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    account = session.scalar(select(Account))
    assert response.headers["location"] == f"/account/{account.id}"
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and "Fake note" in page.text
    assert f'href="/account/{account.id}/manual"' in page.text
    response = client.post(
        f"/account/{account.id}/manual",
        data={
            "name": "Fake loan",
            "type": "other",
            "value": "99.99",
            "note": "",
            "is_liability": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert account.is_liability
    assert session.get(AccountFact, (account.id, "note")) is None
    rows = session.scalars(select(BalanceSnapshot)).all()
    assert len(rows) == 1 and rows[0].date == date(2026, 10, 31)
    assert rows[0].balance == Decimal("99.99")
    assert 'value="crypto" selected' in client.get("/add/manual?type=crypto").text


def test_plaid_edit_404_and_targets_exclude_plaid_manual(client, session):
    institution = Institution(name="Fake Plaid", aggregator="plaid", external_id="fake")
    session.add(institution)
    session.flush()
    accounts = [
        Account(name="Fake manual excluded", type="cash", is_manual=True),
        Account(name="Fake Plaid excluded", type="cash", institution_id=institution.id),
        Account(name="Fake file included", type="cash"),
    ]
    session.add_all(accounts)
    session.commit()
    response = client.get("/add/import")
    assert "Fake file included" in response.text
    assert (
        "Fake manual excluded" not in response.text and "Fake Plaid excluded" not in response.text
    )
    for method in (client.get, client.post):
        assert method(f"/account/{accounts[1].id}/manual").status_code == 404
    assert client.get(f"/account/{accounts[-1].id}/manual").status_code == 200


def test_import_updates_trends_and_paycheck(client, session, monkeypatch, caplog):
    from app.routers import trends

    monkeypatch.setattr(trends, "local_today", lambda: date(2026, 10, 31))
    stub = Paystub(
        employer="Fake Employer", pay_date=date(2026, 10, 31), gross=Decimal(1000), net=Decimal(800)
    )
    session.add(stub)
    session.flush()
    session.add_all(
        [
            PaystubLine(
                paystub_id=stub.id, label="Fake gross", group_="benefit", amount=Decimal(0)
            ),
            PaystubLine(paystub_id=stub.id, label="Fake tax", group_="tax", amount=Decimal(200)),
        ]
    )
    session.commit()
    assert client.get("/trends").status_code == 200
    assert client.get("/paycheck").status_code == 200
    response = upload(
        client,
        b"Date,Description,Amount,Category\n"
        b"2026-01-01,Secret Fake Memo,-10,Food\n2026-10-31,Secret Fake Memo,-20,Food\n",
    )
    assert "2 added" in response.text
    categories = client.get("/export/trends-categories.csv")
    assert "Food,20" in categories.text
    paycheck = client.get("/paycheck")
    assert paycheck.status_code == 200 and "$30" in paycheck.text
    assert "Secret Fake Memo" not in caplog.text


def test_htmx_returns_fragment_and_normal_post_full_page(client, fixture_bytes):
    response = client.post(
        "/add/import",
        files={"file": ("fake.csv", fixture_bytes("preamble.csv"))},
        data={"name": "Fake cash"},
        headers={"HX-Request": "true"},
    )
    assert "1 added" in response.text and "<!doctype" not in response.text
    response = upload(client, fixture_bytes("preamble.csv"))
    assert "<!doctype html>" in response.text


def text_of(response):
    return BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)


@pytest.mark.parametrize("htmx", [False, True])
def test_unknown_new_type_asks_and_explicit_other_allowed(client, session, htmx):
    data = b"Date,Amount\n2026-10-30,-1\n"
    response = client.post(
        "/add/import",
        files={"file": ("fake.csv", data)},
        data={"name": "Mystery", "type": ""},
        headers={"HX-Request": "true"} if htmx else {},
    )
    assert response.status_code == 200
    assert (
        'Orbit can\'t tell what kind of account "Mystery" is. '
        "Choose a type so debts land on the right side."
    ) in text_of(response)
    assert session.scalar(select(Account)) is None
    if not htmx:
        assert 'name="file"' in response.text and 'name="type"' in response.text
    response = upload(client, data, name="Mystery", type="other")
    assert "Added to Mystery as Other" in text_of(response)
    assert "1 added" in response.text


def test_duplicate_new_account_refused_and_existing_dedupes(client, session):
    data = b"Date,Amount\n2026-10-30,-1\n"
    assert "1 added" in upload(client, data, name="Fake Checking").text
    response = upload(client, data, name="  fAKE checking  ")
    assert (
        'An account named "fAKE checking" already exists. '
        "Choose it from the list so duplicates are caught."
    ) in text_of(response)
    account = session.scalar(select(Account))
    assert len(session.scalars(select(Account)).all()) == 1
    assert "1 duplicates skipped" in upload(client, data, account_id=str(account.id)).text


def test_no_balance_dashboard_missing_then_edit_repairs(client, session, fixture_bytes):
    from core.rollups import read_dashboard

    response = upload(client, fixture_bytes("chase.csv"), name="Fake card", type="cards")
    assert "Added to Fake card as Credit cards (counts as a debt)" in text_of(response)
    warning = "Net worth will show as missing until you enter this account's current balance."
    assert warning in text_of(response)
    account = session.scalar(select(Account))
    path = f"/account/{account.id}/manual"
    assert BeautifulSoup(response.text, "html.parser").select_one(f'a.btn[href="{path}"]')
    assert read_dashboard(session, date(2026, 10, 31))["net_worth"] is None
    session.commit()
    dashboard = BeautifulSoup(client.get("/dashboard").text, "html.parser")
    assert dashboard.select_one("#nwBig").text.strip() == "—"
    assert f'href="{path}"' in client.get(f"/account/{account.id}").text
    assert client.get(path).status_code == 200
    response = client.post(
        path,
        data={
            "name": "Repaired card",
            "type": "cards",
            "value": "125.50",
            "note": "Entered from statement",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert not account.is_manual and account.name == "Repaired card"
    snapshot = session.get(BalanceSnapshot, (account.id, date(2026, 10, 31)))
    assert snapshot.balance == Decimal("125.50") and snapshot.source == "manual"
    assert session.get(AccountFact, (account.id, "note")).value == "Entered from statement"
    assert read_dashboard(session, date(2026, 10, 31))["net_worth"] == -12550
    session.commit()
    assert "missing until" not in text_of(
        upload(client, fixture_bytes("chase.csv"), account_id=str(account.id))
    )


def test_card_balance_net_worth_exact(client, session):
    from core.rollups import read_dashboard

    data = b"Date,Debit,Credit,Balance\n2026-10-30,10,,100.25 DR\n2026-10-31,,5,95.25 DR\n"
    response = upload(client, data, name="Fake card", type="cards")
    assert "You owe $95.25" in response.text
    assert read_dashboard(session, date(2026, 10, 31))["net_worth"] == -9525
    session.commit()


def test_mapping_explains_issues_preselects_guesses_and_saves_only_answers(client, session):
    # Dates force the form open; amount and balance signs can be inferred.
    data = (
        "Date,Description,Amount,Balance\n"
        + "".join(f"10/0{i}/2026,Fake purchase,-10,100\n" for i in range(1, 6))
    ).encode()
    response = upload(client, data, type="cards")
    soup = BeautifulSoup(response.text, "html.parser")
    assert "Orbit needs your help because" in soup.text
    assert "date order is unclear" in soup.text
    for role, index in {"date": "0", "memo": "1", "amount": "2", "balance": "3"}.items():
        assert soup.select_one(f'select[name="{role}"] option[selected]')["value"] == index
    assert not soup.select('input[name="liability_amount_sign"]')
    assert not soup.select('input[name="liability_balance_sign"]')
    response = client.post(
        "/add/import/confirm",
        data={
            "token": token(response),
            "date": "0",
            "memo": "1",
            "amount": "2",
            "balance": "3",
            "date_format": "mdy",
            "decimal": ".",
        },
    )
    assert "5 added" in response.text
    mapping = session.scalar(select(ImportRule)).column_map
    assert "liability_amount_sign" not in mapping and "liability_balance_sign" not in mapping


@pytest.mark.parametrize("answer", ["checkbox", "direction"])
def test_unsigned_form_can_confirm_or_choose_direction(client, session, answer):
    data = b"Date,Amount,Flow\n2026-10-29,10,DR\n2026-10-30,10,CR\n2026-10-31,10,DR\n"
    response = upload(client, data)
    assert (
        "Every amount in this file is positive, so Orbit can't tell money in from money out."
        in text_of(response)
    )
    assert "These amounts are correct as they are" in response.text
    fields = {"token": token(response), "date": "0", "amount": "1", "date_format": "auto"}
    fields.update({"amounts_signed": "on"} if answer == "checkbox" else {"direction": "2"})
    response = client.post("/add/import/confirm", data=fields)
    assert "3 added" in response.text
    account = session.scalar(select(Account))
    mapping = session.scalar(select(ImportRule)).column_map
    assert mapping["amounts_signed"] is (answer == "checkbox")
    amounts = list(session.scalars(select(Transaction.amount).order_by(Transaction.id)))
    assert amounts == list(map(Decimal, [10, 10, 10] if answer == "checkbox" else [-10, 10, -10]))
    assert "3 duplicates skipped" in upload(client, data, account_id=str(account.id)).text


@pytest.mark.parametrize("kind", ["missing", "extension", "expired", "oversize"])
def test_htmx_errors_swap_visible_panel(client, kind):
    headers = {"HX-Request": "true"}
    if kind == "expired":
        response = client.post("/add/import/confirm", data={"token": "a" * 32}, headers=headers)
    elif kind == "extension":
        response = client.post(
            "/add/import", files={"file": ("fake.txt", b"fake")}, headers=headers
        )
    elif kind == "oversize":
        response = client.post(
            "/add/import",
            content=b"",
            headers={
                **headers,
                "Content-Length": str(MAX_UPLOAD + 1),
            },
        )
    else:
        response = client.post("/add/import", headers=headers)
    assert response.status_code == 200
    assert 'role="alert"' in response.text and "<!doctype" not in response.text


def test_content_length_rejected_before_form_read(client, monkeypatch):
    from starlette.requests import Request

    def must_not_read(*args, **kwargs):
        raise AssertionError("Oversize requests must be rejected before parsing the form")

    monkeypatch.setattr(Request, "form", must_not_read)
    response = client.post(
        "/add/import",
        content=b"",
        headers={
            "HX-Request": "true",
            "Content-Length": str(MAX_UPLOAD + 1),
        },
    )
    assert response.status_code == 200 and "10 MB" in response.text


@pytest.mark.parametrize("route", ["/add", "/add/import"])
def test_page_load_purges_held_files(client, fixture_bytes, route):
    import os
    import time

    held = token(upload(client, fixture_bytes("no-header.csv")))
    folder = Settings.from_env().data_dir / "inbox" / "imports"
    for path in folder.glob(f"{held}.*"):
        os.utime(path, (time.time() - 90000, time.time() - 90000))
    assert client.get(route).status_code == 200
    assert not list(folder.glob(f"{held}.*"))


@pytest.mark.parametrize(
    "filename",
    [
        "chase.csv",
        "chase-checking.csv",
        "bank-of-america.csv",
        "discover.csv",
        "citi.csv",
        "capital-one.csv",
        "wells-fargo.csv",
    ],
)
def test_real_layout_form_behavior(client, fixture_bytes, filename):
    kind = (
        "cash"
        if filename in {"chase-checking.csv", "bank-of-america.csv", "wells-fargo.csv"}
        else "cards"
    )
    response = upload(client, fixture_bytes(filename), type=kind)
    assert response.status_code == 200
    assert ('name="token"' in response.text) is (filename == "wells-fargo.csv")
    if filename != "wells-fargo.csv":
        assert "Import complete" in response.text


def test_multipart_size_error_reaches_panel(client, monkeypatch):
    from starlette.exceptions import HTTPException
    from starlette.requests import Request

    def too_large(*args, **kwargs):
        raise HTTPException(400, "Part exceeded maximum size")

    monkeypatch.setattr(Request, "form", too_large)
    response = client.post("/add/import", headers={"HX-Request": "true"})
    assert response.status_code == 200 and "10 MB" in response.text
    assert 'role="alert"' in response.text


def test_answered_signs_saved_and_contrary_file_reopens(client, session):
    data = b"Date,Amount,Balance\n2026-10-30,10,-100\n2026-10-31,-5,-95\n"
    response = upload(client, data, type="cards")
    response = client.post(
        "/add/import/confirm",
        data={
            "token": token(response),
            "date": "0",
            "amount": "1",
            "balance": "2",
            "date_format": "auto",
            "decimal": ".",
            "liability_amount_sign": "positive",
            "liability_balance_sign": "negative",
        },
    )
    assert "2 added" in response.text
    rule = session.scalar(select(ImportRule))
    assert rule.column_map["liability_amount_sign"] == "positive"
    assert rule.column_map["liability_balance_sign"] == "negative"
    account = session.scalar(select(Account))
    contrary = (
        "Date,Amount,Balance\n" + "".join(f"2026-10-{day},-10,100\n" for day in range(20, 25))
    ).encode()
    response = upload(client, contrary, account_id=str(account.id))
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('select[name="liability_amount_sign"]')
    assert soup.select_one('select[name="liability_balance_sign"]')
    assert len(session.scalars(select(Transaction)).all()) == 2


@pytest.mark.parametrize("footer", [b"Total,,-3.00", b",,Total"])
def test_confirmed_skips_unreadable_rows_in_result(client, footer):
    data = b"Date,Description,Amount\n10/30/2026,A,-1\n10/31/2026,B,-2\n" + footer
    response = upload(client, data)
    response = client.post(
        "/add/import/confirm",
        data={
            "token": token(response),
            "date": "0",
            "memo": "1",
            "amount": "2",
            "date_format": "mdy",
        },
    )
    assert "2 added" in response.text and "Unreadable rows skipped: 4." in response.text


@pytest.mark.parametrize("kind", ["manual", "plaid"])
def test_new_csv_account_name_taken_by_excluded_account(client, session, kind):
    account = Account(name="Fake checking", type="cash", is_manual=kind == "manual")
    if kind == "plaid":
        institution = Institution(name="Fake bank", aggregator="plaid", external_id="fake")
        session.add(institution)
        session.flush()
        account.institution_id = institution.id
    session.add(account)
    session.commit()
    response = upload(client, b"Date,Amount\n2026-10-30,-1\n")
    assert (
        'An account named "Fake checking" already exists and can\'t receive file imports. '
        "Use a different name."
    ) in text_of(response)
    assert session.scalar(select(Transaction)) is None


def test_future_only_balance_shows_missing_balance_warning(client, monkeypatch):
    monkeypatch.setattr("aggregators.file_store.local_today", lambda: date(2026, 10, 31))
    monkeypatch.setattr("aggregators.csv_import.local_today", lambda: date(2026, 10, 31))
    response = upload(client, b"Date,Amount,Balance\n2026-11-01,-1,100\n")
    assert "1 added" in response.text
    assert (
        "Net worth will show as missing until you enter this account's current balance."
        in text_of(response)
    )


def test_late_csv_upload_reaches_closed_month_and_can_be_corrected(client, session):
    other = Account(name="Fake savings", type="cash", is_manual=True)
    session.add(other)
    session.flush()
    record_daily(other.id, date(2026, 9, 30), Decimal(1000), session=session)
    close_month("2026-09", today=date(2026, 10, 31), session=session)
    session.commit()
    response = upload(
        client,
        b"Date,Description,Amount,Balance\n"
        b"2026-09-10,Fake pay,500.00,500.00\n"
        b"2026-09-20,Fake cafe,-50.00,450.00\n"
        b"2026-10-05,Fake cafe,-50.00,400.00\n",
    )
    assert "Net worth for Sep 2026 now reflects this account." in response.text
    september = date(2026, 9, 30)
    session.rollback()
    points = net_worth_series(september, september, "month", session=session)
    assert points[0].net_worth == Decimal(1450)
    account = session.scalar(select(Account).where(Account.name == "Fake checking"))
    response = upload(
        client,
        b"Date,Description,Amount,Balance\n2026-09-30,Fake cafe,-30.00,420.00\n",
        account_id=str(account.id),
    )
    assert "balance days skipped" not in response.text
    session.rollback()
    points = net_worth_series(september, september, "month", session=session)
    assert points[0].net_worth == Decimal(1420)
