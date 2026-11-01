from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from aggregators.csv_import import analyze, detect, import_csv, parse_date, parse_money
from aggregators.file_store import ImportError
from core.models import Account, BalanceSnapshot, ImportRule, Transaction


def account(session, liability=False):
    row = Account(name="Fake account", type="cards" if liability else "cash")
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize(
    ("filename", "delimiter", "roles"),
    [
        ("chase.csv", ",", {"date": 0, "amount": 5, "memo": 2}),
        ("capital-one.csv", ",", {"date": 0, "debit": 5, "credit": 6}),
        ("amex.csv", ",", {"date": 0, "amount": 2}),
        ("comma-decimals.csv", ";", {"date": 0, "amount": 2}),
        ("preamble.csv", ",", {"date": 0, "amount": 2}),
    ],
)
def test_layouts(fixture_bytes, filename, delimiter, roles):
    analysis = analyze(fixture_bytes(filename))
    assert analysis.delimiter == delimiter
    assert analysis.has_header
    assert not analysis.ambiguities
    assert all(analysis.mapping["roles"][role] == column for role, column in roles.items())


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_delimiter_quotes_and_signature(delimiter):
    header = delimiter.join(["Date", "Description", "Amount"])
    body = delimiter.join(["2026-10-30", '"Fake, shop; with | separators"', "-4.50"])
    first = analyze((header + "\n" + body).encode())
    second = analyze((header + "\n" + body.replace("4.50", "6.50")).encode())
    assert first.delimiter == delimiter
    assert first.signature == second.signature
    assert first.rows[0].memo == "Fake, shop; with | separators"


def test_encoding_and_binary():
    data = "Date,Description,Amount\n2026-10-30,Fake café,-1.00\n"
    assert analyze(data.encode("cp1252")).rows[0].memo == "Fake café"
    assert analyze(data.encode("utf-8-sig")).has_header
    with pytest.raises(ImportError, match="binary"):
        analyze(b"\x00\x01Date,Amount")


@pytest.mark.parametrize(
    ("text", "decimal", "expected"),
    [
        ("$1,234.56", ".", "1234.56"),
        ("(12.34)", ".", "-12.34"),
        ("12.34-", ".", "-12.34"),
        ("12.34 DR", ".", "-12.34"),
        ("12.34 CR", ".", "12.34"),
        ("−12.34", ".", "-12.34"),
        ("1.234,56", ",", "1234.56"),
    ],
)
def test_money(text, decimal, expected):
    assert parse_money(text, decimal) == Decimal(expected)


@pytest.mark.parametrize(
    ("value", "fmt"),
    [
        ("2026-01-05", "auto"),
        ("20260105", "auto"),
        ("1/5/2026", "mdy"),
        ("5/1/2026", "dmy"),
        ("1/5/26", "mdy"),
        ("5.1.2026", "auto"),
        ("Jan 5, 2026", "auto"),
    ],
)
def test_dates(value, fmt):
    assert parse_date(value, fmt) == date(2026, 1, 5)


def test_headerless_and_preamble(fixture_bytes):
    assert analyze(fixture_bytes("no-header.csv")).ambiguities
    assert detect(fixture_bytes("no-header.csv"))[0] == ["Column 1", "Column 2", "Column 3"]
    assert analyze(fixture_bytes("preamble.csv")).raw_rows[0][0] == 5


def test_separate_payee_and_description_are_not_a_tie():
    analysis = analyze(b"Date,Payee,Description,Amount\n2026-10-30,Fake Shop,Fake memo,-1\n")
    assert not analysis.ambiguities
    assert analysis.rows[0].merchant == "Fake Shop"
    assert analysis.rows[0].memo == "Fake memo"


def test_headerless_saved_rule_still_requires_review(session, fixture_bytes):
    data = fixture_bytes("no-header.csv")
    target = account(session)
    mapping = {"roles": {"date": 0, "memo": 1, "amount": 2}}
    import_csv(session, target, data, mapping=mapping, confirmed=True)
    analysis = analyze(data, target, session=session)
    assert analysis.ambiguities == ["header"]
    assert analysis.mapping["roles"] == mapping["roles"]


@pytest.mark.parametrize(
    "header",
    [
        "Date,Description,Debit,Credit",
        "Date,Description,Withdrawals,Deposits",
        "Trans. Date,Description,Charges,Payments",
    ],
)
def test_other_bank_column_aliases(header):
    analysis = analyze((header + "\n10/30/2026,Fake Coffee,4.50,\n").encode())
    assert not analysis.ambiguities
    assert analysis.rows[0].amount == Decimal("-4.50")


@pytest.mark.parametrize("filename", ["chase.csv", "capital-one.csv", "amex.csv"])
def test_card_purchases_negative(session, fixture_bytes, filename):
    target = account(session, liability=True)
    result = import_csv(session, target, fixture_bytes(filename))
    assert result.added > 0
    coffee = session.scalar(select(Transaction).where(Transaction.memo == "Fake Coffee"))
    assert coffee.amount == Decimal("-4.50")


def test_csv_balances_and_order(session, fixture_bytes):
    target = account(session, liability=True)
    result = import_csv(session, target, fixture_bytes("capital-one.csv"))
    assert result.balances == []
    asset = account(session)
    result = import_csv(session, asset, fixture_bytes("comma-decimals.csv"))
    assert result.balances[-1][1] == Decimal("1236.56")
    data = b"Date,Amount,Balance\n2026-10-31,-2,98\n2026-10-30,-3,100\n2026-10-30,-1,103\n"
    assert analyze(data).balances[date(2026, 10, 30)] == Decimal(100)
    lines = data.splitlines()
    reverse = b"\n".join([lines[0], *reversed(lines[1:])])
    assert analyze(reverse).balances[date(2026, 10, 30)] == Decimal(100)


def test_direction_needs_no_card_majority(session):
    target = account(session, liability=True)
    for data in [b"Date,Amount,DR/CR\n2026-10-30,4.50,DR", b"Date,Amount\n2026-10-30,4.50 DR"]:
        analysis = analyze(data, target)
        assert not analysis.ambiguities
        assert analysis.rows[0].amount == Decimal("-4.50")


def test_ten_percent_bad_rows_and_footer(session):
    data = "Date,Amount\n" + "2026-10-30,-1\n" * 10 + "Total,garbage\n"
    result = import_csv(session, account(session), data.encode())
    assert result.added == 10
    assert result.unreadable_rows == [12]
    assert "rows" in analyze((data + "bad,bad\n").encode()).ambiguities


def test_only_confirmed_rules_saved_and_bad_saved_rule_reopens(session, fixture_bytes):
    target = account(session)
    data = fixture_bytes("chase.csv")
    import_csv(session, target, data)
    assert session.scalar(select(ImportRule)) is None
    mapping = analyze(data, target).mapping
    import_csv(session, target, data, mapping=mapping, confirmed=True)
    rule = session.scalar(select(ImportRule))
    import_csv(session, target, data, mapping=mapping, confirmed=True)
    assert session.scalars(select(ImportRule)).all() == [rule]
    invalid = data.replace(b"10/30/2026", b"bad-date").replace(b"10/29/2026", b"bad-date")
    analysis = analyze(invalid, target, session=session)
    assert analysis.ambiguities
    assert analysis.mapping["roles"] == mapping["roles"]


@pytest.mark.parametrize(
    ("filename", "liability", "opens"),
    [
        ("chase.csv", True, False),
        ("chase-checking.csv", False, False),
        ("bank-of-america.csv", False, False),
        ("discover.csv", True, False),
        ("citi.csv", True, False),
        ("capital-one.csv", True, False),
        ("wells-fargo.csv", False, True),
    ],
)
def test_real_bank_layouts(session, fixture_bytes, filename, liability, opens):
    target = account(session, liability)
    data = fixture_bytes(filename)
    analysis = analyze(data, target)
    assert bool(analysis.ambiguities) is opens
    mapping = None
    if opens:
        mapping = {"roles": {"date": 0, "amount": 1, "memo": 4}, "date_format": "mdy"}
        analysis = analyze(data, target, mapping=mapping, confirmed=True)
        assert not analysis.ambiguities
    amounts = {row.memo: row.amount for row in analysis.rows}
    assert amounts["Fake Coffee"] == Decimal("-4.50")
    assert amounts["Fake Payment"] > 0
    result = import_csv(session, target, data, mapping=mapping, confirmed=opens)
    assert not result.unreadable_rows
    if filename in {"chase.csv", "capital-one.csv"}:
        assert result.missing_balance and result.balances_written == 0
    if filename == "bank-of-america.csv":
        assert result.added == 2 and result.balances_written == 3


def test_summary_block_wider_than_a_short_table_is_not_the_header(session):
    data = (
        b"Description,,Summary Amt.\n"
        b'Beginning balance as of 10/01/2026,,"1,000.00"\n'
        b'Total credits,,"2,000.00"\n'
        b'Total debits,,"-50.00"\n'
        b'Ending balance as of 10/31/2026,,"2,950.00"\n'
        b"\n"
        b"Date,Description,Amount,Running Bal.\n"
        b'10/01/2026,Beginning balance as of 10/01/2026,,"1,000.00"\n'
        b'10/02/2026,PAYROLL,"2,000.00","3,000.00"\n'
        b'10/13/2026,GROCER,"-50.00","2,950.00"\n'
    )
    analysis = analyze(data, account(session))
    assert not analysis.ambiguities
    assert analysis.columns[0] == "Date"
    assert [row.amount for row in analysis.rows if not row.balance_only] == [
        Decimal("2000.00"),
        Decimal("-50.00"),
    ]
    assert analysis.balances[date(2026, 10, 1)] == Decimal("1000.00")


@pytest.mark.parametrize("liability", [False, True])
@pytest.mark.parametrize(
    ("marker", "expected"), [("CR", [10] * 5 + [50, -3]), ("DR", [10] * 5 + [-50, -3])]
)
def test_mixed_explicit_markers_never_flip(session, liability, marker, expected):
    data = (
        "Date,Amount\n" + "2026-10-30,10.00\n" * 5 + f"2026-10-30,50.00 {marker}\n2026-10-30,-3\n"
    ).encode()
    analysis = analyze(data, account(session, liability))
    assert not analysis.ambiguities
    assert [row.amount for row in analysis.rows] == list(map(Decimal, expected))
    assert analysis.rows[5].amount_marker == marker
    assert analysis.rows[0].amount_marker is None


def test_direction_overrides_markers_and_saved_sign(session):
    target = account(session, True)
    data = b"Date,Amount,Type,Balance\n2026-10-30,10 CR,DR,25 CR\n2026-10-31,5 DR,CR,30 DR\n"
    mapping = {
        "roles": {"date": 0, "amount": 1, "direction": 2, "balance": 3},
        "liability_amount_sign": "positive",
        "liability_balance_sign": "negative",
    }
    analysis = analyze(data, target, mapping=mapping)
    assert not analysis.ambiguities
    assert [r.amount for r in analysis.rows] == [Decimal(-10), Decimal(5)]
    assert list(analysis.balances.values()) == [Decimal(-25), Decimal(30)]


@pytest.mark.parametrize("setting", ["liability_amount_sign", "liability_balance_sign"])
@pytest.mark.parametrize("sign", ["positive", "negative"])
def test_saved_sign_reopens_on_contrary_evidence(session, setting, sign):
    from aggregators.file_store import save_rule

    target = account(session, True)
    column = "Amount" if setting == "liability_amount_sign" else "Debit,Credit,Balance"
    value = "10" if sign == "negative" else "-10"
    cells = value if setting == "liability_amount_sign" else f"1,,{value}"
    data = (f"Date,{column}\n" + "".join(f"2026-10-{d},{cells}\n" for d in range(25, 30))).encode()
    mapping = analyze(data, target).mapping
    save_rule(session, "csv", detect(data)[3], {**mapping, setting: sign})
    analysis = analyze(data, target, session=session)
    assert setting in analysis.ambiguities
    assert not analyze(data, target, mapping={**mapping, setting: sign}, confirmed=True).ambiguities


@pytest.mark.parametrize("kind", ["loans", "mortgage", "other"])
def test_amount_majority_only_for_cards(session, kind):
    from aggregators.file_store import save_rule

    target = Account(name="Fake debt", type=kind, is_liability=True)
    session.add(target)
    session.flush()
    data = b"Date,Amount\n" + b"2026-10-30,10\n" * 5
    analysis = analyze(data, target)
    assert "liability_amount_sign" in analysis.ambiguities
    save_rule(
        session,
        "csv",
        analysis.signature,
        {**analysis.mapping, "liability_amount_sign": "positive"},
    )
    analysis = analyze(data, target, session=session)
    assert not analysis.ambiguities
    assert all(row.amount == Decimal(-10) for row in analysis.rows)


@pytest.mark.parametrize(
    ("balances", "opens", "expected"),
    [
        ([10, 20], False, [10, 20]),
        ([-10] * 5, False, [10] * 5),
        ([-10, -20], True, None),
        ([10, -20], True, None),
    ],
)
def test_liability_balance_evidence(session, balances, opens, expected):
    data = (
        "Date,Debit,Credit,Balance\n"
        + "".join(f"2026-10-{25 + i},1,,{balance}\n" for i, balance in enumerate(balances))
    ).encode()
    analysis = analyze(data, account(session, True))
    assert ("liability_balance_sign" in analysis.ambiguities) is opens
    if expected:
        assert list(analysis.balances.values()) == list(map(Decimal, expected))


def test_saved_signs_used_without_clear_evidence(session):
    from aggregators.file_store import save_rule

    target = account(session, True)
    data = b"Date,Amount,Balance\n2026-10-30,10,-20\n2026-10-31,-3,17\n"
    mapping = {
        **analyze(data, target).mapping,
        "liability_amount_sign": "positive",
        "liability_balance_sign": "negative",
    }
    save_rule(session, "csv", detect(data)[3], mapping)
    analysis = analyze(data, target, session=session)
    assert not analysis.ambiguities
    assert [r.amount for r in analysis.rows] == [Decimal(-10), Decimal(3)]
    assert list(analysis.balances.values()) == [Decimal(20), Decimal(-17)]


def test_unsigned_assets_require_answer_and_reuse_it(session):
    target = account(session)
    data = b"Date,Amount\n" + b"2026-10-30,10\n" * 3
    analysis = analyze(data, target)
    assert "unsigned_amounts" in analysis.ambiguities
    import_csv(
        session, target, data, mapping={**analysis.mapping, "amounts_signed": True}, confirmed=True
    )
    assert session.scalar(select(ImportRule)).column_map["amounts_signed"] is True
    assert not analyze(data, target, session=session).ambiguities
    assert all(a == Decimal(10) for a in session.scalars(select(Transaction.amount)))


def test_partial_direction_column_requires_review():
    analysis = analyze(b"Date,Amount,Type\n2026-10-30,10,CR\n2026-10-31,10,Sale\n")
    assert "roles" in analysis.ambiguities
    assert analysis.mapping["roles"]["direction"] == 2
    dropped = analyze(b"Date,Amount,Type\n2026-10-30,-10,Sale\n2026-10-31,10,Payment\n")
    assert not dropped.ambiguities and "direction" not in dropped.mapping["roles"]


def test_one_day_conflicting_balances_record_none(session):
    data = b"Date,Amount,Balance\n2026-10-30,-2,98\n2026-10-30,-1,99\n"
    result = import_csv(session, account(session), data)
    assert result.added == 2 and result.balances_written == 0
    assert any("same day" in notice for notice in result.notices)
    assert analyze(data.replace(b",99", b",98")).balances == {date(2026, 10, 30): Decimal(98)}


def test_trailing_empty_cells_only_and_confirmed_row_threshold(session):
    data = b"Date,Amount\n2026-10-30,-1,\n2026-10-31,-2\n2026-10-29,-3,unexpected\n"
    analysis = analyze(data)
    assert "rows" in analysis.ambiguities
    result = import_csv(session, account(session), data, mapping=analysis.mapping, confirmed=True)
    assert result.added == 2 and result.unreadable_rows == [4]
    bad_amount = data.replace(b"-2", b"bad")
    assert not analyze(bad_amount, confirmed=True).ambiguities
    assert "amount" in analyze(bad_amount.replace(b"-3", b"bad"), confirmed=True).ambiguities
    bad_date = data.replace(b"2026-10-31", b"bad")
    assert not analyze(bad_date, confirmed=True).ambiguities
    assert (
        "date_format"
        in analyze(bad_date.replace(b"2026-10-29", b"bad"), confirmed=True).ambiguities
    )


@pytest.mark.parametrize(
    ("value", "fmt"),
    [
        ("10-5-2026", "mdy"),
        ("5-10-2026", "dmy"),
        ("10/05/2026 00:00:00", "mdy"),
        ("2026-10-05 12:34", "auto"),
        ("2026-10-05 12:34 PM", "auto"),
        ("5-10-2026 1:02:03AM", "dmy"),
        ("Oct 5, 2026 12:34 PM", "auto"),
    ],
)
def test_more_date_forms(value, fmt):
    assert parse_date(value, fmt) == date(2026, 10, 5)


def test_hyphen_dates_and_times_still_ask_order():
    assert "date_format" in analyze(b"Date,Amount\n10-05-2026 00:00:00,-1\n").ambiguities


def test_empty_text_stored_as_null(session):
    data = b"Date,Amount,Description,Merchant,Category\n2026-10-30,-1,,,\n"
    import_csv(session, account(session), data)
    row = session.scalar(select(Transaction))
    assert (row.memo, row.merchant, row.category) == (None, None, None)


@pytest.mark.parametrize(
    ("headers", "index"),
    [
        ("Memo,Details,Payee", 0),
        ("Details,Payee", 0),
        ("Memo,Transaction Description,Details", 1),
        ("Memo,Description,Payee", 1),
    ],
)
def test_description_priority(headers, index):
    from aggregators.csv_import import guess_roles

    roles, ties = guess_roles(headers.split(","))
    assert not ties and roles["memo"] == index


def test_two_column_preamble_not_mistaken_for_short_header():
    analysis = analyze(b"Fake export\nDate,Amount\n2026-10-30,-10\n")
    assert not analysis.ambiguities and analysis.columns == ["Date", "Amount"]


@pytest.mark.parametrize("footer", [b"Total,,95.00 CR", b"10/03/2026,Footer,garbage CR"])
@pytest.mark.parametrize("first", ["-5.00", "5.00"])
def test_unreadable_footer_marker_does_not_flip_payroll(session, footer, first):
    data = (
        f"Date,Description,Amount\n10/01/2026,Cafe,{first}\n10/02/2026,Payroll,2000.00\n"
    ).encode() + footer
    result = import_csv(
        session, account(session), data, mapping={"date_format": "mdy"}, confirmed=True
    )
    assert result.added == 2 and result.unreadable_rows == [4]
    payroll = session.scalar(select(Transaction).where(Transaction.memo == "Payroll"))
    assert payroll.amount == Decimal("2000.00")


@pytest.mark.parametrize(("first", "expected"), [("-5.00", [-5, 100, 3]), ("5.00", [-5, -100, 3])])
def test_cr_markers_only_flip_unsigned_positive_amounts(first, expected):
    data = (f"Date,Amount\n2026-10-29,{first}\n2026-10-30,100.00\n2026-10-31,3.00 CR\n").encode()
    analysis = analyze(data)
    assert not analysis.ambiguities
    assert [row.amount for row in analysis.rows] == list(map(Decimal, expected))


@pytest.mark.parametrize(
    ("data", "columns"),
    [
        (
            b"Bank export,October\nPosted,Payee,Value\n2026-10-30,Cafe,-4.50\n",
            ["Posted", "Payee", "Value"],
        ),
        (
            b"Date,Amount\nDate,Description,Amount,Balance\n2026-10-30,Cafe,-4.50,100\n",
            ["Date", "Description", "Amount", "Balance"],
        ),
    ],
)
def test_short_preamble_cannot_be_header(data, columns):
    detected, rows, _, _, header = detect(data)
    assert header and detected == columns
    assert len(rows) == 1 and rows[0][1][0] == "2026-10-30"


@pytest.mark.parametrize("last_cell", [b"", b"extra"])
def test_short_header_requires_all_later_trailing_cells_empty(last_cell):
    data = b"Date,Amount\n2026-10-30,-1,\n2026-10-31,-2," + last_cell
    assert detect(data)[4] is (last_cell == b"")


@pytest.mark.parametrize("footer", [b"Total,,-3.00", b",,Total"])
def test_confirmed_footer_imports_readable_rows(session, footer):
    data = b"Date,Description,Amount\n10/30/2026,A,-1\n10/31/2026,B,-2\n" + footer
    analysis = analyze(data)
    assert analysis.ambiguities
    result = import_csv(session, account(session), data, mapping=analysis.mapping, confirmed=True)
    assert result.added == 2 and result.unreadable_rows == [4]


def test_confirmed_columns_need_half_readable_and_one_complete_row(session):
    data = b"Date,Amount\n2026-10-30,-1\nTotal,bad\n"
    result = import_csv(session, account(session), data, confirmed=True)
    assert result.added == 1 and result.unreadable_rows == [3]
    assert "rows" in analyze(b"Date,Amount\n2026-10-30,bad\nTotal,-1\n", confirmed=True).ambiguities
    assert "date_format" in analyze(b"Date,Amount\n10/05/2026,-1\n", confirmed=True).ambiguities


def test_mapped_direction_wins_with_marker_elsewhere():
    data = b"Date,Amount,Flow\n2026-10-30,10,CR\n2026-10-31,5 CR,CR\n"
    analysis = analyze(data, mapping={"roles": {"date": 0, "amount": 1, "direction": 2}})
    assert not analysis.ambiguities
    assert [row.amount for row in analysis.rows] == [Decimal(10), Decimal(5)]


@pytest.mark.parametrize("fmt", ["auto", "mdy", "dmy"])
def test_short_hyphen_year_is_unreadable(fmt):
    with pytest.raises(ValueError, match="Unreadable date"):
        parse_date("26-10-05", fmt)


@pytest.mark.parametrize("invalid", ["1969-12-31", "2027-11-01", "26-10-05"])
def test_out_of_range_dates_cannot_write_balances(session, monkeypatch, invalid):
    monkeypatch.setattr("aggregators.csv_import.local_today", lambda: date(2026, 10, 31))
    data = ("Date,Amount,Balance\n2026-10-30,-1,100\n" + f"{invalid},-1,999\n").encode()
    result = import_csv(
        session, account(session), data, mapping={"date_format": "dmy"}, confirmed=True
    )
    assert result.added == 1 and result.unreadable_rows == [3]
    assert result.balances == [(date(2026, 10, 30), Decimal(100))]
    assert len(session.scalars(select(BalanceSnapshot)).all()) == 1


def test_date_range_boundaries_use_local_today(monkeypatch):
    monkeypatch.setattr("aggregators.csv_import.local_today", lambda: date(2026, 10, 31))
    assert parse_date("1970-01-01") == date(1970, 1, 1)
    assert parse_date("2027-10-31") == date(2027, 10, 31)
    with pytest.raises(ValueError):
        parse_date("2027-11-01")
    monkeypatch.setattr("aggregators.csv_import.local_today", lambda: date(2024, 2, 29))
    assert parse_date("2025-02-28") == date(2025, 2, 28)
    with pytest.raises(ValueError):
        parse_date("2025-03-01")
