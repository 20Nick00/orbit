import logging
from datetime import date
from decimal import Decimal as D

import pytest

from paystubs.parsers.pdf import MONEY_RE, UnreadablePdf, parse_lines, parse_pdf

from .make_stubs import stub_a, write_pdf


def test_single_column(parsed_a):
    assert parsed_a.pay_date == date(2026, 10, 30)
    assert parsed_a.period_start == date(2026, 10, 16)
    assert parsed_a.period_end == date(2026, 10, 31)
    assert len(parsed_a.lines) == 12
    gross = parsed_a.lines[0]
    assert (gross.label, gross.amount, gross.ytd) == ("Gross Pay", D("10417"), D("208340"))
    assert parsed_a.header_candidates[0] == "Northwind Test Labs Inc"
    assert all(not MONEY_RE.search(line) for line in parsed_a.header_candidates)
    assert "\n" not in parsed_a.text_for_matching
    assert "header_candidates=" not in repr(parsed_a)
    assert "text_for_matching=" not in repr(parsed_a)


def test_side_by_side_and_repeated_net(parsed_b):
    assert parsed_b.pay_date == date(2026, 6, 12)
    assert parsed_b.period_start == date(2026, 5, 30)
    assert parsed_b.period_end == date(2026, 6, 12)
    rows = {line.label: line for line in parsed_b.lines}
    assert len(rows) == 11
    assert rows["Regular"].amount == D("4000")
    assert rows["Regular"].ytd == D("48000")
    assert rows["Federal Withholding"].amount == D("-500")
    assert rows["Net Pay"].amount == D("2394")
    assert rows["Net Pay"].ytd == D("28728")


@pytest.mark.parametrize(
    "token,expected",
    [
        ("(1,493.00)", "-1493"),
        ("-1,493.00", "-1493"),
        ("1,493.00-", "-1493"),
        ("$1,493.00", "1493"),
        ("($1,493.00)", "-1493"),
        ("-$1,493.00", "-1493"),
        ("1.2345", "1.2345"),
        ("0.00", "0"),
        ("$10.123", "10.123"),
    ],
)
def test_money_formats(token, expected):
    assert parse_lines(f"Tax {token}")[0].amount == D(expected)


@pytest.mark.parametrize(
    "text",
    [
        "401(k)",
        "401K",
        "10/30/2026",
        "***-**-1234",
        "Check 12345",
        "1.2",
        "1.23456",
        "12,34.00",
        "abc1.00",
    ],
)
def test_non_money(text):
    assert not MONEY_RE.search(text)
    assert parse_lines(text) == []


def test_labels_and_runs():
    rows = parse_lines(
        "Regular 80.00 50.00 4,000.00 48,000.00 Federal Withholding 500.00 6,000.00\n"
        "* 401(k) 10/30/2026 Pre-tax: 100.00\n"
        "1.00 2.00\nDescription Current YTD"
    )
    assert [(r.label, r.amount, r.ytd) for r in rows] == [
        ("Regular", D("4000"), D("48000")),
        ("Federal Withholding", D("500"), D("6000")),
        ("401(k) Pre-tax", D("100"), None),
    ]


def test_repeated_labels_identical_once_different_summed():
    rows = parse_lines(
        "Net Pay 10.00 100.00\nNET   PAY 10.00 100.00\nNet Pay 5.00 50.00\nNet Pay 5.00 50.00"
    )
    assert len(rows) == 1
    assert rows[0].amount == D("15")
    assert rows[0].ytd == D("150")
    assert rows[0].repeats == 2
    assert rows[0].merged is True
    missing = parse_lines("Tax 1.00 10.00\nTax 2.00")
    assert missing[0].amount == D("3")
    assert missing[0].ytd is None


@pytest.mark.parametrize("label", ["Pay Date", "Check Date", "Advice Date", "Payment Date"])
@pytest.mark.parametrize(
    "value", ["10/30/2026", "10/30/26", "2026-10-30", "Oct 30, 2026", "October 30, 2026"]
)
def test_date_formats(tmp_path, label, value):
    parsed = parse_pdf(write_pdf(tmp_path / "dates.pdf", [f"{label}: {value}", "Gross Pay 10.00"]))
    assert parsed.pay_date == date(2026, 10, 30)


@pytest.mark.parametrize(
    "begin,end",
    [
        ("Period Begin", "Period End"),
        ("Period Beginning", "Period Ending"),
        ("Period Start", "Period End"),
    ],
)
def test_separate_period_labels(tmp_path, begin, end):
    parsed = parse_pdf(
        write_pdf(tmp_path / "dates.pdf", [f"{begin}: Oct 16, 2026", f"{end}: October 31, 2026"])
    )
    assert parsed.period_start == date(2026, 10, 16)
    assert parsed.period_end == date(2026, 10, 31)


@pytest.mark.parametrize(
    "lines",
    [
        ["Pay Date 02/30/2026"],
        ["Pay Date 10/30/2026", "Check Date 10/31/2026"],
        ["Pay Date 13/10/2026"],
        ["Pay Date 30/10/2026"],
        ["No dates supplied"],
    ],
)
def test_invalid_or_conflicting_date_is_missing(tmp_path, lines):
    assert parse_pdf(write_pdf(tmp_path / "bad-date.pdf", lines)).pay_date is None


def test_blank_encrypted_and_non_pdf(tmp_path):
    with pytest.raises(UnreadablePdf, match="OCR"):
        parse_pdf(write_pdf(tmp_path / "blank.pdf", []))
    with pytest.raises(UnreadablePdf, match="password-protected"):
        parse_pdf(
            write_pdf(
                tmp_path / "encrypted.pdf", ["Secret synthetic text"], password="fake-password"
            )
        )
    bad = tmp_path / "not.pdf"
    bad.write_bytes(b"secret content not a PDF")
    with pytest.raises(UnreadablePdf, match="not a PDF") as error:
        parse_pdf(bad)
    assert "secret" not in str(error.value)


def test_owner_only_encryption_still_opens(tmp_path):
    parsed = parse_pdf(write_pdf(tmp_path / "owner.pdf", ["Medicare 151.00 3,020.00"], password=""))
    assert parsed.lines[0].label == "Medicare"
    assert parsed.lines[0].amount == D("151.00")


def test_detached_dollar_signs_and_one_decimal_hours():
    gross, regular, medicare = parse_lines(
        "Gross Pay $ 4,000.00 $ 48,000.00\n"
        "Regular 80.0 50.00 4,000.00 48,000.00\n"
        "Medicare $ 58.00 $ 696.00 "
    )
    assert (gross.label, gross.amount, gross.ytd) == ("Gross Pay", D("4000.00"), D("48000.00"))
    assert (regular.label, regular.amount, regular.ytd) == ("Regular", D("4000.00"), D("48000.00"))
    assert (medicare.label, medicare.amount, medicare.ytd) == ("Medicare", D("58.00"), D("696.00"))


def test_fixture_overrides(tmp_path):
    parsed = parse_pdf(
        stub_a(tmp_path / "override.pdf", "11/15/2026", overrides={"Net Pay": ("1.00", None)})
    )
    assert parsed.pay_date == date(2026, 11, 15)
    assert parsed.lines[-1].amount == D("1")
    assert parsed.lines[-1].ytd is None


def test_pdf_library_debug_logging_does_not_expose_content(tmp_path, caplog):
    path = stub_a(tmp_path / "private.pdf")
    with caplog.at_level(logging.DEBUG):
        parse_pdf(path)
    assert "Northwind" not in caplog.text
    assert "Gross Pay" not in caplog.text
    assert "10417" not in caplog.text
    assert "10,417" not in caplog.text
    assert not [
        record for record in caplog.records if record.name.startswith(("pdfminer", "pdfplumber"))
    ]
