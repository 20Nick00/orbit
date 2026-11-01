from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.demo import ACCTS, DemoRefusedError, generate, main, seed_demo
from core.models import (
    Account,
    AccountFact,
    BalanceSnapshot,
    Institution,
    MonthEndSnapshot,
    Paystub,
    PaystubLine,
    Transaction,
)
from core.snapshots import close_month, net_worth_series

TODAY = date(2026, 10, 31)


def test_prototype_totals_and_history(demo_session):
    points = {p.date: p for p in net_worth_series(date(2024, 11, 1), TODAY, session=demo_session)}
    assert points[TODAY].net_worth == Decimal("504500.00")
    assert points[TODAY].assets == Decimal("1045020.00")
    assert points[TODAY].liabilities == Decimal("540520.00")
    for day, value in {
        TODAY - timedelta(days=1): "504371.26",
        TODAY - timedelta(days=30): "503566.12",
        TODAY - timedelta(days=365): "387075.28",
        date(2025, 12, 31): "403665.37",
        date(2024, 11, 1): "267319.71",
    }.items():
        assert abs(points[day].net_worth - Decimal(value)) <= Decimal("0.10")
    expected = [
        "8405.39",
        "30862.87",
        "147613.73",
        "95173.11",
        "46662.91",
        "8015.44",
        "4149.91",
        "14262.57",
        "7502.42",
        "683473.02",
        "683.07",
        "2028.38",
        "6714.95",
        "19282.22",
        "513846.63",
    ]
    accounts = demo_session.scalars(select(Account).order_by(Account.id)).all()
    for account, item, amount in zip(accounts, ACCTS, expected, strict=True):
        assert account.name == item["name"]
        snapshot = demo_session.scalars(
            select(BalanceSnapshot)
            .where(
                BalanceSnapshot.account_id == account.id,
                BalanceSnapshot.date <= TODAY - timedelta(days=30),
            )
            .order_by(BalanceSnapshot.date.desc())
            .limit(1)
        ).one()
        assert abs(snapshot.balance - Decimal(amount)) <= Decimal("0.10")
        facts = dict(
            demo_session.execute(
                select(AccountFact.key, AccountFact.value).where(
                    AccountFact.account_id == account.id
                )
            ).all()
        )
        assert all(facts[key] == value for key, value in item["facts"])
        assert facts["subtitle"] == item["sub"]
    home = accounts[9]
    assert home.is_manual
    assert demo_session.scalar(
        select(func.max(BalanceSnapshot.date)).where(BalanceSnapshot.account_id == home.id)
    ) == TODAY - timedelta(days=29)
    assert demo_session.scalar(select(func.count()).select_from(MonthEndSnapshot)) == 0


def test_prototype_transactions_and_random_sequence(demo_session):
    rows = demo_session.scalars(select(Transaction)).all()
    assert len(rows) == 259
    assert abs(sum(row.amount for row in rows) - Decimal("-2238.69")) <= Decimal("1.50")
    _, transactions, rnd = generate(TODAY)
    assert len(transactions) == 259
    assert rnd() == 0.8683739686384797


def test_rerun_replaces_demo_rows(demo_session):
    original = demo_session.scalar(select(func.count()).select_from(BalanceSnapshot))
    seed_demo(demo_session, TODAY)
    demo_session.commit()
    assert demo_session.scalar(select(func.count()).select_from(Account)) == 15
    assert demo_session.scalar(select(func.count()).select_from(Transaction)) == 259
    assert demo_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == original


def test_failed_replacement_rolls_back_every_change(demo_session, monkeypatch):
    from sqlalchemy.orm import Session

    before = list(
        demo_session.execute(
            select(
                BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance
            ).order_by(BalanceSnapshot.account_id, BalanceSnapshot.date)
        )
    )

    def fail_rebuild(*args):
        raise RuntimeError("Interrupted before commit")

    monkeypatch.setattr("core.demo.rollups.rebuild", fail_rebuild)
    with pytest.raises(RuntimeError, match="Interrupted"):
        with Session(demo_session.get_bind()) as writer, writer.begin():
            seed_demo(writer, TODAY + timedelta(days=1), transactions=10000)
    after = list(
        demo_session.execute(
            select(
                BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance
            ).order_by(BalanceSnapshot.account_id, BalanceSnapshot.date)
        )
    )
    assert after == before
    assert demo_session.scalar(select(func.count()).select_from(Transaction)) == 259


@pytest.mark.parametrize("aggregator", [None, "plaid"])
def test_real_accounts_refused_without_changes(demo_session, aggregator):
    institution = Institution(name="Real bank", aggregator=aggregator)
    demo_session.add(institution)
    demo_session.flush()
    real = Account(name="Real checking", type="cash", institution_id=institution.id)
    if aggregator is None:
        real.institution_id = None
    demo_session.add(real)
    demo_session.commit()
    before = list(demo_session.execute(select(Account.id, Account.name)))
    transaction_ids = list(demo_session.scalars(select(Transaction.id)))
    with pytest.raises(DemoRefusedError, match="non-demo"):
        seed_demo(demo_session, TODAY)
    assert list(demo_session.execute(select(Account.id, Account.name))) == before
    assert list(demo_session.scalars(select(Transaction.id))) == transaction_ids


def test_closed_demo_refused(demo_session):
    close_month("2026-09", today=TODAY, session=demo_session)
    demo_session.commit()
    before = list(
        demo_session.execute(select(MonthEndSnapshot.account_id, MonthEndSnapshot.balance))
    )
    with pytest.raises(DemoRefusedError, match="month-end"):
        seed_demo(demo_session, TODAY)
    assert (
        list(demo_session.execute(select(MonthEndSnapshot.account_id, MonthEndSnapshot.balance)))
        == before
    )
    assert demo_session.scalar(select(func.count()).select_from(Account)) == 15


def test_filler_does_not_change_prototype_or_balances(demo_session):
    before = list(
        demo_session.execute(select(Transaction.date, Transaction.merchant, Transaction.amount))
    )
    summary = seed_demo(demo_session, TODAY, transactions=10000)
    demo_session.commit()
    assert summary["transactions"] == 10000
    assert summary["next_random"] == 0.8683739686384797
    rows = list(
        demo_session.execute(
            select(Transaction.date, Transaction.merchant, Transaction.amount).order_by(
                Transaction.id
            )
        )
    )
    assert len(rows) == 10000
    assert rows[:259] == before
    assert all(TODAY - timedelta(days=729) <= row.date <= TODAY for row in rows)
    assert net_worth_series(TODAY, TODAY, session=demo_session)[0].net_worth == Decimal("504500.00")


def test_cli_arguments_and_summary(rollup_session, monkeypatch, capsys):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield rollup_session
        rollup_session.commit()

    monkeypatch.setattr("core.demo.session_scope", scope)
    main(["--as-of", "2026-10-31", "--transactions", "10000"])
    assert "15 accounts, 10000 transactions, as of 2026-10-31" in capsys.readouterr().out


def test_cli_refusal_is_nonzero(rollup_session, monkeypatch):
    from contextlib import contextmanager

    rollup_session.add(Account(name="Real", type="cash"))
    rollup_session.commit()

    @contextmanager
    def scope():
        yield rollup_session

    monkeypatch.setattr("core.demo.session_scope", scope)
    with pytest.raises(SystemExit) as exc:
        main(["--as-of", "2026-10-31"])
    assert exc.value.code == 1
    assert rollup_session.scalar(select(func.count()).select_from(Account)) == 1


def test_demo_paystubs_add_up_and_rerun_cleanly(demo_session):
    seed_demo(demo_session, TODAY)
    stubs = demo_session.scalars(select(Paystub).order_by(Paystub.pay_date)).all()
    # 15th and 30th from January to October, except February has no 30th.
    assert len(stubs) == 19
    latest = stubs[-1]
    assert latest.pay_date == date(2026, 10, 30)
    assert latest.net == Decimal("5890.00")
    assert latest.gross_ytd == Decimal("10417.00") * 19
    lines = demo_session.scalars(
        select(PaystubLine).where(PaystubLine.paystub_id == latest.id)
    ).all()
    deducted = sum(line.amount for line in lines if line.group_ != "employer")
    assert latest.gross - deducted == latest.net
    assert {line.limit_bucket for line in lines} == {None, "401k", "hsa"}


def test_imported_paystubs_refused_without_changes(demo_session):
    demo_session.add(
        Paystub(employer="Real Employer", pay_date=TODAY, source_file="archive/stub.pdf")
    )
    demo_session.flush()
    with pytest.raises(DemoRefusedError):
        seed_demo(demo_session, TODAY)
    assert demo_session.scalar(select(func.count()).select_from(Paystub)) == 20
