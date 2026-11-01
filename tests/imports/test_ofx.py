import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aggregators.csv_import import import_csv
from aggregators.file_store import ImportError
from aggregators.ofx_import import analyze, import_ofx
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Holding,
    ImportRule,
    Institution,
    Transaction,
)
from core.snapshots import net_worth_series


def test_sgml_local_date_mask_hash_and_cross_format_dedupe(session, fixture_bytes, caplog):
    data = fixture_bytes("checking.ofx")
    statements = analyze(data, session=session)
    assert statements[0].needs_target
    assert statements[0].rows[0].date == date(2026, 10, 30)
    results = import_ofx(session, data, choices={"0": {"account_id": "new"}})
    account = session.get(Account, results[0].account_id)
    assert account.type == "cash"
    assert account.mask == "1111"
    fact = session.get(AccountFact, (account.id, "import_key"))
    assert len(fact.value) == 64 and "FAKE00001111" not in fact.value
    assert not analyze(data, session=session)[0].needs_target
    assert import_ofx(session, data)[0].duplicates_skipped == 1
    csv = b"Date,Description,Amount\n2026-10-30,Fake Coffee,-4.50\n"
    assert import_csv(session, account, csv).duplicates_skipped == 1
    assert "FAKE00001111" not in caplog.text and "Fake Coffee" not in caplog.text


@pytest.mark.parametrize(("meaning", "owed"), [("positive", "100"), ("negative", "-100")])
def test_positive_card_balance_asks_and_reuses_answer(session, fixture_bytes, meaning, owed):
    data = fixture_bytes("card.qfx")
    assert analyze(data, session=session)[0].needs_sign
    with pytest.raises(ImportError, match="confirm"):
        import_ofx(session, data, choices={"0": {"account_id": "new"}})
    results = import_ofx(
        session, data, choices={"0": {"account_id": "new", "liability_balance_sign": meaning}}
    )
    account_id = results[0].account_id
    assert session.get(BalanceSnapshot, (account_id, date(2026, 10, 31))).balance == Decimal(owed)
    assert session.scalar(select(Transaction.amount)) == Decimal("-4.50")
    assert net_worth_series(date(2026, 10, 31), date(2026, 10, 31), session=session)[
        0
    ].net_worth == -Decimal(owed)
    rule = session.scalar(select(ImportRule))
    assert rule.source_kind == "ofx" and len(rule.matcher) == 64
    assert not analyze(data, session=session)[0].needs_sign


def test_negative_debt_balance_and_missing_date(session, fixture_bytes):
    data = fixture_bytes("card.qfx").replace(b"<BALAMT>100.00", b"<BALAMT>-100.00")
    assert not analyze(data, session=session)[0].needs_sign
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.balances[0][1] == Decimal(100)
    no_asof = data.replace(b"<DTASOF>20261031</DTASOF>", b"")
    assert analyze(no_asof, session=session)[0].balance_date == date(2026, 10, 31)
    no_dates = no_asof.replace(b"<DTEND>20261031</DTEND>", b"")
    statement = analyze(no_dates, session=session)[0]
    assert statement.balance_date is None and statement.notices


def test_multi_account_and_institution_reused(session, fixture_bytes):
    results = import_ofx(
        session,
        fixture_bytes("multi-account.ofx"),
        choices={"0": {"account_id": "new"}, "1": {"account_id": "new"}},
    )
    assert len(results) == 2 and all(r.added == 1 for r in results)
    assert session.scalar(select(func.count(Institution.id))) == 1
    assert set(session.scalars(select(Transaction.category))) == {None, "Transfer"}


def test_investment_positions_cash_and_replacement(session, fixture_bytes):
    data = fixture_bytes("investment.ofx")
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.balances == [(date(2026, 10, 31), Decimal(135))]
    holding = session.scalar(select(Holding))
    assert (holding.ticker, holding.name, holding.qty, holding.value) == (
        "FAKE",
        "Fake Example Stock",
        Decimal(2),
        Decimal(110),
    )
    tx = session.scalar(select(Transaction))
    assert tx.amount == Decimal(-100) and tx.date == date(2026, 10, 30)
    import_ofx(session, data)
    assert session.scalar(select(func.count(Holding.id))) == 1
    assert session.scalar(select(func.count(Transaction.id))) == 1


def test_late_investment_import_keeps_first_holdings_then_protects_them(session, fixture_bytes):
    from core.models import MonthEndSnapshot
    from core.snapshots import close_month, record_daily, reopen_month

    other = Account(name="Other account", type="cash")
    session.add(other)
    session.flush()
    day = date(2026, 9, 30)
    record_daily(other.id, day, Decimal(1000), source="csv", session=session)
    close_month("2026-09", today=date(2026, 10, 31), session=session)
    data = (
        fixture_bytes("investment.ofx")
        .replace(b"20261031", b"20260930")
        .replace(b"20261030", b"20260929")
        .replace(b"20261001", b"20260901")
    )
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.balances_written == 1
    assert session.get(MonthEndSnapshot, (result.account_id, "2026-09")) is None
    holding = session.scalar(select(Holding))
    assert holding is not None and holding.date == day and holding.value == Decimal(110)
    original_id = holding.id
    reopen_month("2026-09", session=session)
    assert close_month("2026-09", today=date(2026, 10, 31), session=session) == 2
    second = import_ofx(session, data.replace(b"<MKTVAL>110", b"<MKTVAL>999"))[0]
    assert second.balance_days_skipped == 1 and second.balances_written == 0
    session.expire_all()
    holding = session.scalar(select(Holding))
    assert holding.id == original_id and holding.value == Decimal(110)
    assert session.get(BalanceSnapshot, (result.account_id, day)).balance == Decimal(135)
    assert session.get(MonthEndSnapshot, (result.account_id, "2026-09")).balance == Decimal(135)


@pytest.mark.parametrize("missing", [b"<MKTVAL>110", b"<AVAILCASH>25"])
def test_missing_investment_values_never_become_zero(session, fixture_bytes, missing):
    data = fixture_bytes("investment.ofx").replace(missing, b"")
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.balances_written == 0 and result.notices
    if b"MKTVAL" in missing:
        assert session.scalar(select(Holding)).value is None


def test_missing_investment_total_not_invented(session, fixture_bytes):
    data = fixture_bytes("investment.ofx").replace(b"<TOTAL>-100", b"")
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.added == 0


def test_multistatement_rollback_includes_new_accounts(session, fixture_bytes, monkeypatch):
    from aggregators import ofx_import

    original = ofx_import.write_import
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Synthetic failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(ofx_import, "write_import", fail_second)
    with pytest.raises(RuntimeError):
        import_ofx(
            session,
            fixture_bytes("multi-account.ofx"),
            choices={"0": {"account_id": "new"}, "1": {"account_id": "new"}},
        )
    session.commit()
    for model in (Account, Institution, Transaction, Holding):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_parser_failure_safe(session, caplog):
    with pytest.raises(ImportError) as error:
        analyze(b"<OFX>SECRET-ACCOUNT SECRET-MEMO</OFX>", session=session)
    assert "SECRET" not in str(error.value)
    assert "SECRET" not in caplog.text


def test_xml_unicode_and_missing_amount(session, fixture_bytes):
    data = fixture_bytes("card.qfx").replace(b"Fake Coffee", "Fake café".encode())
    assert analyze(data, session=session)[0].rows[0].memo == "Fake café"
    missing = data.replace(b"<TRNAMT>-4.50", b"<TRNAMT>null")
    statement = analyze(missing, session=session)[0]
    assert statement.rows[0].amount is None


def test_one_balance_question_per_institution(session, fixture_bytes):
    data = fixture_bytes("card.qfx")
    start, end = data.index(b"<CCSTMTTRNRS>"), data.index(b"</CCSTMTTRNRS>") + 14
    original = data[start:end]
    second = original.replace(b"FAKE00002222", b"FAKE00005555")
    data = data[:start] + original + second + data[end:]
    assert sum(s.needs_sign for s in analyze(data, session=session)) == 1
    results = import_ofx(
        session,
        data,
        choices={
            "0": {"account_id": "new", "liability_balance_sign": "positive"},
            "1": {"account_id": "new"},
        },
    )
    assert len(results) == 2
    assert all(result.balances[0][1] == Decimal(100) for result in results)


@pytest.mark.parametrize(
    ("sign", "raw", "stored"),
    [
        ("positive", "-25", "-25"),
        ("negative", "-25", "25"),
        ("negative", "25", "-25"),
        (None, "-25", "25"),
    ],
)
def test_ofx_balance_sign_preserves_overpayment(session, fixture_bytes, sign, raw, stored):
    from aggregators.file_store import save_rule
    from aggregators.ofx_import import _hash

    if sign:
        save_rule(session, "ofx", _hash("FAKECARD"), {"liability_balance_sign": sign})
    data = fixture_bytes("card.qfx").replace(b"<BALAMT>100.00", f"<BALAMT>{raw}".encode())
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    assert result.balances[0][1] == Decimal(stored)


def test_no_fid_banks_have_separate_rules(session, fixture_bytes):
    data = fixture_bytes("card.qfx").replace(b"<FID>FAKECARD</FID>", b"")
    import_ofx(
        session, data, choices={"0": {"account_id": "new", "liability_balance_sign": "positive"}}
    )
    assert not analyze(data, session=session)[0].needs_sign
    other = data.replace(b"Fake Card Bank", b"Second Fake Bank")
    statement = analyze(other, session=session)[0]
    assert statement.needs_sign and statement.needs_target
    anonymous = re.sub(rb"<FI>.*?</FI>", b"", data, flags=re.S)
    rule_count = session.scalar(select(func.count(ImportRule.id)))
    import_ofx(
        session,
        anonymous,
        choices={"0": {"account_id": "new", "liability_balance_sign": "positive"}},
    )
    second_anonymous = anonymous.replace(b"FAKE00002222", b"FAKE00003333")
    assert analyze(anonymous, session=session)[0].needs_sign
    assert analyze(second_anonymous, session=session)[0].needs_sign
    assert session.scalar(select(func.count(ImportRule.id))) == rule_count

    # Old fallback rules must not supply an answer for an unidentified bank either.
    from aggregators.file_store import save_rule
    from aggregators.ofx_import import _hash

    save_rule(session, "ofx", _hash("OFX import"), {"liability_balance_sign": "negative"})
    assert analyze(second_anonymous, session=session)[0].needs_sign


def test_no_fid_questions_group_by_bank(session, monkeypatch):
    from aggregators import ofx_import

    def statements(*args, **kwargs):
        return [
            ofx_import.Statement(
                str(i), name, "", None, name, "cards", "USD", [], Decimal(25), date(2026, 10, 31)
            )
            for i, name in enumerate(["Bank A", "Bank B", "Bank A"])
        ]

    monkeypatch.setattr(ofx_import, "parse", statements)
    result = analyze(b"", session=session)
    assert [s.needs_sign for s in result] == [True, True, False]
    result = analyze(
        b"",
        session=session,
        choices={
            "0": {"liability_balance_sign": "positive"},
            "1": {"liability_balance_sign": "negative"},
        },
    )
    assert [s.balance_sign for s in result] == ["positive", "negative", "positive"]


@pytest.mark.parametrize(
    ("filename", "original", "replacement"),
    [
        ("card.qfx", b"<TRNAMT>-4.50", b"<TRNAMT>-1,500"),
        ("card.qfx", b"<BALAMT>100.00", b"<BALAMT>1,500.00"),
        ("investment.ofx", b"<MKTVAL>110", b"<MKTVAL>1,500"),
        ("investment.ofx", b"<TOTAL>-100", b"<TOTAL>-1,500"),
        ("investment.ofx", b"<AVAILCASH>25", b"<AVAILCASH>1,500"),
        ("investment.ofx", b"<UNITS>2", b"<UNITS>1,500"),
    ],
)
def test_thousands_comma_rejected_before_parser(
    session, fixture_bytes, monkeypatch, filename, original, replacement
):
    from aggregators.ofx_import import _Parser

    called = []
    monkeypatch.setattr(_Parser, "parse", lambda *a, **kw: called.append(True))
    with pytest.raises(ImportError, match="fresh export"):
        analyze(fixture_bytes(filename).replace(original, replacement), session=session)
    assert not called


@pytest.mark.parametrize("account_id", [b"1", b"1234"])
def test_short_id_not_stored_in_mask_or_name(session, fixture_bytes, account_id):
    data = fixture_bytes("checking.ofx").replace(b"FAKE00001111", account_id)
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    account = session.get(Account, result.account_id)
    assert account.mask is None and account_id.decode() not in account.name


@pytest.mark.parametrize("protection", ["manual", "closed", "edited"])
@pytest.mark.parametrize("missing_balance", [False, True])
def test_holdings_unchanged_on_protected_day(session, fixture_bytes, protection, missing_balance):
    from core.models import BalanceEdit
    from core.snapshots import close_month, record_daily

    data = fixture_bytes("investment.ofx")
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    if protection == "manual":
        record_daily(
            result.account_id, date(2026, 10, 31), Decimal(135), source="manual", session=session
        )
    elif protection == "closed":
        close_month("2026-10", today=date(2026, 11, 1), session=session)
    else:
        session.add(
            BalanceEdit(
                account_id=result.account_id,
                date=date(2026, 10, 31),
                old_balance=Decimal(130),
                new_balance=Decimal(135),
                old_source="ofx",
            )
        )
        session.flush()
    holding = session.scalar(select(Holding))
    original_id = holding.id
    changed = data.replace(b"<MKTVAL>110", b"<MKTVAL>999")
    if missing_balance:
        changed = changed.replace(b"<AVAILCASH>25", b"")
    result = import_ofx(session, changed)[0]
    session.expire_all()
    holding = session.scalar(select(Holding))
    assert result.balance_days_skipped == (0 if missing_balance else 1)
    assert result.balances_written == 0
    assert holding.id == original_id and holding.value == Decimal(110)


@pytest.mark.parametrize("taken_suffix", [False, True])
def test_ofx_new_account_name_collision_gets_suffix(session, fixture_bytes, taken_suffix):
    data = fixture_bytes("checking.ofx")
    name = analyze(data, session=session)[0].name
    session.add(Account(name=f"  {name.upper()}  ", type="cash"))
    if taken_suffix:
        session.add(Account(name=f"{name} (2)", type="cash"))
    session.flush()
    result = import_ofx(session, data, choices={"0": {"account_id": "new"}})[0]
    account = session.get(Account, result.account_id)
    assert account.name == f"{name} ({3 if taken_suffix else 2})"
    assert result.added == 1
    assert import_ofx(session, data)[0].account_id == account.id
