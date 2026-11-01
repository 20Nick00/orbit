from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aggregators.file_store import ImportError, ImportRow, dedupe_key, write_import
from core.detail import _series, read_account, read_type
from core.manual import create_manual_account, update_manual_account
from core.models import (
    Account,
    AccountFact,
    BalanceEdit,
    BalanceSnapshot,
    Institution,
    MonthEndSnapshot,
    RollupCache,
    Transaction,
)
from core.rollups import monthly_rollups, read_chart
from core.snapshots import (
    close_month,
    closed_month_balances,
    net_worth_series,
    record_daily,
    reopen_month,
)
from jobs.month_end import close_due_months

DAY = date(2026, 9, 30)
TODAY = date(2026, 10, 31)


def target(session):
    account = Account(name="Fake checking", type="cash")
    session.add(account)
    session.flush()
    return account


def closed_other_account(session, months=(8, 9)):
    other = target(session)
    for month in months:
        day = date(2026, month, 30 if month == 9 else 31)
        record_daily(other.id, day, Decimal(1000), source="csv", session=session)
        close_month(day, today=TODAY, session=session)
    return other


def closed_balances(session, account):
    return dict(
        session.execute(
            select(MonthEndSnapshot.month, MonthEndSnapshot.balance).where(
                MonthEndSnapshot.account_id == account.id
            )
        ).all()
    )


def month_totals(session, start=date(2026, 7, 1), end=TODAY):
    return {
        point.date.isoformat()[:7]: point.net_worth
        for point in net_worth_series(start, end, "month", session=session)
    }


def test_late_import_computes_closed_months_without_changing_rows(session):
    other = closed_other_account(session)
    original = closed_balances(session, other)
    late = target(session)
    result = write_import(
        session,
        late,
        [],
        {
            date(2026, 8, 31): Decimal(100),
            DAY: Decimal(200),
            date(2026, 10, 5): Decimal(300),
        },
        "csv",
    )
    assert result.balances_written == 3 and result.balance_days_skipped == 0
    assert closed_balances(session, late) == {}
    assert closed_balances(session, other) == original
    assert month_totals(session) == {
        "2026-08": Decimal(1100),
        "2026-09": Decimal(1200),
        "2026-10": Decimal(1300),
    }
    assert result.notices == [
        "Net worth for closed months from Aug 2026 to Sep 2026 now reflects this account."
    ]


def test_out_of_order_files_remain_correctable(session):
    closed_other_account(session, months=(7, 8, 9))
    late = target(session)
    for day, value in [(DAY, 300), (date(2026, 7, 31), 100), (date(2026, 8, 31), 200)]:
        result = write_import(session, late, [], {day: Decimal(value)}, "csv")
        assert result.balances_written == 1 and result.balance_days_skipped == 0
    assert closed_balances(session, late) == {}
    assert month_totals(session) == {
        "2026-07": Decimal(1100),
        "2026-08": Decimal(1200),
        "2026-09": Decimal(1300),
    }


def test_straddling_files_update_computed_month_end(session):
    closed_other_account(session)
    late = target(session)
    first = write_import(
        session,
        late,
        [],
        {
            date(2026, 8, 10): Decimal(50),
            date(2026, 8, 31): Decimal(100),
            date(2026, 9, 9): Decimal(150),
        },
        "csv",
    )
    assert first.balances_written == 3 and first.balance_days_skipped == 0
    assert month_totals(session)["2026-09"] == Decimal(1150)
    second = write_import(
        session,
        late,
        [],
        {
            date(2026, 9, 10): Decimal(160),
            DAY: Decimal(200),
            date(2026, 10, 9): Decimal(300),
        },
        "csv",
    )
    assert second.balances_written == 3 and second.balance_days_skipped == 0
    assert closed_balances(session, late) == {}
    assert month_totals(session) == {
        "2026-08": Decimal(1100),
        "2026-09": Decimal(1200),
        "2026-10": Decimal(1300),
    }
    assert second.notices == ["Net worth for Sep 2026 now reflects this account."]


def test_corrected_reimport_updates_computed_month_end(session):
    closed_other_account(session)
    late = target(session)
    balances = {date(2026, 8, 31): Decimal(100), DAY: Decimal(100000), TODAY: Decimal(300)}
    write_import(session, late, [], balances, "csv")
    assert month_totals(session)["2026-09"] == Decimal(101000)
    identical = write_import(session, late, [], balances, "csv")
    assert identical.balances_written == 3 and identical.balance_days_skipped == 0
    assert identical.notices == []
    balances[DAY] = Decimal(200)
    result = write_import(session, late, [], balances, "csv")
    assert result.balances_written == 3 and result.balance_days_skipped == 0
    assert closed_balances(session, late) == {}
    assert month_totals(session)["2026-09"] == Decimal(1200)
    assert result.notices == ["Net worth for Sep 2026 now reflects this account."]


@pytest.mark.parametrize("provided_session", [False, True])
def test_month_end_job_does_not_freeze_incomplete_file_history(session, provided_session):
    other = closed_other_account(session, months=(9,))
    record_daily(other.id, TODAY, Decimal(1000), source="csv", session=session)
    late = target(session)
    write_import(session, late, [], {date(2026, 9, 15): Decimal(100)}, "csv")
    session.commit()
    kwargs = {"session": session} if provided_session else {}
    assert close_due_months(date(2026, 11, 1), **kwargs) == ["2026-10"]
    assert closed_balances(session, late) == {}
    assert month_totals(session) == {"2026-09": Decimal(1100), "2026-10": Decimal(1100)}
    result = write_import(
        session,
        late,
        [],
        {DAY: Decimal(200), TODAY: Decimal(300), date(2026, 11, 2): Decimal(400)},
        "csv",
    )
    assert result.balances_written == 3 and result.balance_days_skipped == 0
    assert month_totals(session) == {"2026-09": Decimal(1200), "2026-10": Decimal(1300)}
    assert closed_balances(session, late) == {}


def test_old_partial_march_import_survives_restart_with_later_closed_months(session):
    account = target(session)
    november = date(2026, 11, 1)
    for day in (DAY, TODAY):
        record_daily(account.id, day, Decimal(1000), source="csv", session=session)
        assert close_month(day, today=november, session=session) == 1
    original = closed_balances(session, account)
    first = write_import(
        session,
        account,
        [],
        {date(2026, 3, 5): Decimal(300), date(2026, 3, 15): Decimal(400)},
        "csv",
    )
    assert first.balances_written == 2 and first.balance_days_skipped == 0
    assert close_due_months(november, session) == []
    assert closed_balances(session, account) == original
    second = write_import(
        session,
        account,
        [],
        {
            date(2026, 3, 20): Decimal(500),
            date(2026, 3, 31): Decimal(600),
            date(2026, 4, 15): Decimal(650),
        },
        "csv",
    )
    assert second.balances_written == 3 and second.balance_days_skipped == 0
    expected = {"2026-03": Decimal(600), "2026-04": Decimal(650), **original}
    assert month_totals(session, start=date(2026, 3, 1)) == expected
    assert close_due_months(november, session) == ["2026-03"]
    assert closed_balances(session, account) == {"2026-03": Decimal(600), **original}
    assert month_totals(session, start=date(2026, 3, 1)) == expected
    assert close_due_months(november, session) == []


def test_one_file_account_newest_first_survives_restart(session):
    account = target(session)
    for balances in (
        {date(2026, 10, 1): Decimal(300), date(2026, 10, 20): Decimal(400)},
        {date(2026, 9, 1): Decimal(100), date(2026, 9, 15): Decimal(150)},
    ):
        result = write_import(session, account, [], balances, "csv")
        assert result.balances_written == 2 and result.balance_days_skipped == 0
    november = date(2026, 11, 1)
    assert close_due_months(november, session) == []
    assert closed_balances(session, account) == {}
    result = write_import(
        session,
        account,
        [],
        {date(2026, 9, 16): Decimal(160), DAY: Decimal(200)},
        "csv",
    )
    assert result.balances_written == 2 and result.balance_days_skipped == 0
    expected = {"2026-09": Decimal(200), "2026-10": Decimal(400)}
    assert month_totals(session) == expected
    assert close_due_months(november, session) == ["2026-09"]
    assert closed_balances(session, account) == {"2026-09": Decimal(200)}
    assert month_totals(session) == expected


@pytest.mark.parametrize("source", ["csv", "ofx"])
@pytest.mark.parametrize("provided_session", [False, True])
def test_file_only_month_has_totals_without_frozen_rows(session, source, provided_session):
    account = target(session)
    result = write_import(session, account, [], {date(2026, 9, 15): Decimal(100)}, source)
    assert result.balances_written == 1 and result.balance_days_skipped == 0
    session.commit()
    kwargs = {"session": session} if provided_session else {}
    assert close_due_months(TODAY, **kwargs) == []
    assert close_due_months(TODAY, **kwargs) == []
    assert closed_balances(session, account) == {}
    assert month_totals(session) == {"2026-09": Decimal(100)}
    rows = monthly_rollups("2026-09", "2026-09", today=TODAY, session=session)
    assert len(rows) == 1 and rows[0].closed and rows[0].net_worth_end == Decimal(100)


@pytest.mark.parametrize("source", ["manual", "plaid"])
def test_month_without_files_or_rows_waits_for_close(session, source):
    account = target(session)
    record_daily(account.id, date(2026, 9, 15), Decimal(100), source=source, session=session)
    assert closed_month_balances(session=session) == {}
    assert month_totals(session) == {}
    assert close_due_months(TODAY, session) == ["2026-09"]
    assert month_totals(session) == {"2026-09": Decimal(100)}


def test_hidden_file_balance_establishes_month_for_visible_accounts(session):
    visible, hidden = target(session), target(session)
    hidden.hidden = True
    record_daily(visible.id, date(2026, 8, 15), Decimal(200), session=session)
    record_daily(hidden.id, date(2026, 9, 15), Decimal(100), source="csv", session=session)
    assert closed_month_balances(session=session) == {"2026-09": {visible.id: Decimal(200)}}
    assert month_totals(session) == {"2026-09": Decimal(200)}


def test_file_month_readers_respect_current_date(session):
    account = target(session)
    today = date(2026, 10, 15)
    for day, value in [(date(2026, 9, 15), 100), (today, 200), (TODAY, 300)]:
        record_daily(account.id, day, Decimal(value), source="csv", session=session)
    assert month_totals(session, end=today) == {"2026-09": Decimal(100)}
    _, closed, _, _ = _series(session, [account], today)
    assert set(closed) == {DAY}
    rows = monthly_rollups("2026-09", "2026-10", today=today, session=session)
    assert rows[0].closed and rows[0].net_worth_end == Decimal(100)
    assert not rows[1].closed and rows[1].net_worth_end == Decimal(200)


@pytest.mark.parametrize("skip_incomplete_files", [False, True])
@pytest.mark.parametrize(
    "source,day,later,expected",
    [
        ("csv", DAY, False, 1),
        ("csv", date(2026, 9, 15), True, 0),
        ("csv", date(2026, 9, 15), False, 0),
        ("ofx", DAY, False, 1),
        ("ofx", date(2026, 9, 15), True, 0),
        ("ofx", date(2026, 9, 15), False, 0),
        ("manual", date(2026, 9, 15), False, 1),
        ("plaid", date(2026, 9, 15), False, 1),
        ("rentcast", date(2026, 9, 15), False, 1),
    ],
)
def test_close_month_file_guard_is_only_for_job(
    session, source, day, later, expected, skip_incomplete_files
):
    account = target(session)
    record_daily(account.id, day, Decimal(100), source=source, session=session)
    if later:
        record_daily(account.id, TODAY, Decimal(200), source=source, session=session)
    if not skip_incomplete_files:
        expected = 1
        kwargs = {}
    else:
        kwargs = {"skip_incomplete_files": True}
    assert close_month("2026-09", today=TODAY, session=session, **kwargs) == expected
    assert closed_balances(session, account) == ({"2026-09": Decimal(100)} if expected else {})


def test_existing_rows_win_over_daily_changes_and_protect_reimport(session):
    account = target(session)
    write_import(session, account, [], {DAY: Decimal(100)}, "csv")
    close_month("2026-09", today=TODAY, session=session)
    record_daily(account.id, DAY, Decimal(200), source="csv", session=session)
    assert month_totals(session) == {"2026-09": Decimal(100)}
    result = write_import(session, account, [], {DAY: Decimal(300)}, "csv")
    assert result.balance_days_skipped == 1 and result.balances_written == 0
    assert result.notices == []
    assert session.get(BalanceSnapshot, (account.id, DAY)).balance == Decimal(200)
    assert closed_balances(session, account) == {"2026-09": Decimal(100)}


@pytest.mark.parametrize("october_balance", [False, True])
def test_later_close_protects_only_days_that_change_its_basis(session, october_balance):
    account = target(session)
    record_daily(account.id, date(2026, 7, 31), Decimal(100), source="csv", session=session)
    close_month("2026-07", today=TODAY, session=session)
    end = date(2026, 10, 31)
    if october_balance:
        record_daily(account.id, end, Decimal(200), source="csv", session=session)
    # A person's explicit close freezes carry-forward even without later history.
    assert close_month("2026-10", today=date(2026, 11, 1), session=session) == 1
    original = closed_balances(session, account)
    gap = date(2026, 9, 15)
    result = write_import(session, account, [], {gap: Decimal(150), end: Decimal(999)}, "csv")
    assert result.protected_balance_days == ({end} if october_balance else {gap, end})
    assert result.balances_written == (1 if october_balance else 0)
    assert closed_balances(session, account) == original
    if october_balance:
        assert session.get(BalanceSnapshot, (account.id, gap)).balance == Decimal(150)
        assert session.get(BalanceSnapshot, (account.id, end)).balance == Decimal(200)
    else:
        assert session.get(BalanceSnapshot, (account.id, gap)) is None


def test_reopened_month_waits_for_explicit_close(session):
    other = closed_other_account(session, months=(9,))
    reopen_month("2026-09", session=session)
    late = target(session)
    result = write_import(
        session, late, [], {DAY: Decimal(100), date(2026, 10, 5): Decimal(200)}, "csv"
    )
    assert closed_balances(session, late) == {}
    assert result.notices == []
    assert month_totals(session) == {"2026-10": Decimal(1200)}
    assert close_due_months(TODAY, session=session) == []
    assert closed_balances(session, late) == {}
    assert close_month("2026-09", today=TODAY, session=session) == 2
    assert closed_balances(session, late) == {"2026-09": Decimal(100)}
    assert closed_balances(session, other) == {"2026-09": Decimal(1000)}
    assert month_totals(session) == {"2026-09": Decimal(1100), "2026-10": Decimal(1200)}


def test_late_account_can_change_side_without_reopening(session):
    closed_other_account(session, months=(9,))
    late = target(session)
    write_import(session, late, [], {DAY: Decimal(200)}, "csv")
    assert month_totals(session)["2026-09"] == Decimal(1200)
    for liability, expected in [(True, 800), (False, 1200)]:
        update_manual_account(
            late.id, late.name, "other", Decimal(200), "", TODAY, liability, session=session
        )
        assert month_totals(session)["2026-09"] == Decimal(expected)
        assert closed_balances(session, late) == {}


def test_late_import_invalidates_chart_and_updates_almanac_excluding_hidden(session):
    closed_other_account(session)
    late = target(session)
    before = read_chart(session, TODAY, "1Y")
    assert before["series"][before["dates"].index("2026-09-30")] == 1000
    assert session.scalar(select(func.count()).select_from(RollupCache)) > 0
    write_import(session, late, [], {DAY: Decimal(200)}, "csv")
    assert session.scalar(select(func.count()).select_from(RollupCache)) == 0
    for hidden, expected in [(False, 1200), (True, 1000)]:
        late.hidden = hidden
        session.flush()
        chart = read_chart(session, TODAY, "1Y")
        assert chart["series"][chart["dates"].index("2026-09-30")] == expected
        rows = monthly_rollups("2026-09", "2026-09", today=TODAY, session=session)
        assert rows[0].closed and rows[0].net_worth_end == Decimal(expected)
        assert month_totals(session)["2026-09"] == Decimal(expected)


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("liability", [False, True])
def test_late_account_page_closed_figure_matches_computed_balance(session, hidden, liability):
    closed_other_account(session, months=(9,))
    late = Account(name="Late account", type="other", hidden=hidden, is_liability=liability)
    session.add(late)
    session.flush()
    write_import(session, late, [], {date(2026, 9, 15): Decimal(200)}, "csv")
    values = closed_month_balances(account_ids=[late.id], session=session)
    assert values == {"2026-09": {late.id: Decimal(200)}}
    # Inspect the actual closed series too: the chart could otherwise fall back to daily.
    _, closed, _, owed = _series(session, [late], TODAY)
    assert closed[DAY].net_worth == values["2026-09"][late.id]
    assert owed == liability
    chart = read_account(session, late.id, TODAY, range_key="1Y")["chart"]
    assert chart["series"][chart["dates"].index("2026-09-30")] == 200
    if not hidden:
        chart = read_type(session, "other", TODAY, range_key="1Y")["chart"]
        assert chart["series"][chart["dates"].index("2026-09-30")] == 200
    assert closed_balances(session, late) == {}


def test_multiset_dedupe_overlap_and_normalization(session):
    account = target(session)
    coffee = ImportRow(1, DAY, Decimal("-4.50"), "Fake Coffee")
    result = write_import(session, account, [coffee, coffee], {}, "csv")
    assert result.added == 2
    assert write_import(session, account, [coffee, coffee], {}, "csv").duplicates_skipped == 2
    other = ImportRow(3, DAY + timedelta(days=1), Decimal("-10"), "Fake Books")
    result = write_import(session, account, [coffee, coffee, other], {}, "csv")
    assert (result.added, result.duplicates_skipped) == (1, 2)
    assert dedupe_key(account.id, DAY, Decimal("-4.5"), "  FAKE  coffee ") == dedupe_key(
        account.id, DAY, Decimal("-4.50"), "Fake Coffee"
    )


def test_manual_edited_and_locked_days_skipped(session):
    account = target(session)
    record_daily(account.id, DAY, Decimal(10), source="csv", session=session)
    close_month("2026-09", today=date(2026, 10, 31), session=session)
    manual_day, edited_day = date(2026, 10, 1), date(2026, 10, 2)
    record_daily(account.id, manual_day, Decimal(20), session=session)
    record_daily(account.id, edited_day, Decimal(30), source="ofx", session=session)
    session.add(
        BalanceEdit(
            account_id=account.id,
            date=edited_day,
            old_balance=Decimal(20),
            new_balance=Decimal(30),
            old_source="csv",
        )
    )
    session.flush()
    result = write_import(
        session,
        account,
        [],
        {DAY: Decimal(99), manual_day: Decimal(99), edited_day: Decimal(99)},
        "csv",
    )
    assert result.balance_days_skipped == 3
    assert result.balances_written == 0
    assert session.get(BalanceSnapshot, (account.id, DAY)).balance == Decimal(10)


def test_midway_failure_rolls_back_every_write(session, monkeypatch):
    from aggregators import file_store

    account = target(session)
    session.commit()
    original = file_store.record_daily

    def fail_second(account_id, day, balance, **kwargs):
        if day != DAY:
            raise RuntimeError("Synthetic failure")
        return original(account_id, day, balance, **kwargs)

    monkeypatch.setattr(file_store, "record_daily", fail_second)
    with pytest.raises(RuntimeError, match="Synthetic"):
        write_import(
            session,
            account,
            [ImportRow(1, DAY, Decimal(-5))],
            {DAY: Decimal(10), DAY + timedelta(days=1): Decimal(20)},
            "csv",
        )
    session.commit()
    assert session.scalar(select(func.count(Transaction.id))) == 0
    assert session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 0


def test_plaid_targets_refused(session):
    institution = Institution(name="Fake Plaid", aggregator="plaid", external_id="fake")
    session.add(institution)
    session.flush()
    account = Account(name="Fake linked", type="cash", institution_id=institution.id)
    session.add(account)
    session.flush()
    with pytest.raises(ImportError, match="Plaid"):
        write_import(session, account, [], {}, "csv")


@pytest.mark.parametrize("liability", [False, True])
def test_manual_create_edit_daily_snapshots_and_other(session, liability):
    account = create_manual_account(
        "Fake other", "other", Decimal(50), "Fake note", DAY, liability, session=session
    )
    assert account.is_manual and account.is_liability == liability
    assert session.get(AccountFact, (account.id, "note")).value == "Fake note"
    update_manual_account(
        account.id, "Fake renamed", "other", Decimal(50), "", DAY, liability, session=session
    )
    assert session.get(AccountFact, (account.id, "note")) is None
    assert len(session.scalars(select(BalanceSnapshot)).all()) == 1
    update_manual_account(
        account.id,
        "Fake renamed",
        "other",
        Decimal(50),
        "",
        DAY + timedelta(days=1),
        liability,
        session=session,
    )
    assert len(session.scalars(select(BalanceSnapshot)).all()) == 2
    assert net_worth_series(DAY, DAY, session=session)[0].net_worth == Decimal(
        -50 if liability else 50
    )


def test_manual_inference_and_file_account_edit(session):
    account = create_manual_account("Fake home", "", Decimal(100), "", DAY, True, session=session)
    assert account.type == "property" and not account.is_liability
    cash = target(session)
    update_manual_account(cash.id, "Fake", "cash", Decimal(10), "", DAY, session=session)
    assert not cash.is_manual
    assert session.get(BalanceSnapshot, (cash.id, DAY)).source == "manual"


@pytest.mark.parametrize("kind", ["cards", "other"])
@pytest.mark.parametrize("closed", [False, True])
def test_side_change_requires_reopening(session, kind, closed):
    account = target(session)
    record_daily(account.id, DAY, Decimal(10), source="csv", session=session)
    if closed:
        close_month("2026-09", today=date(2026, 10, 31), session=session)
        with pytest.raises(
            ValueError,
            match=(
                "This account has closed months. Reopen them before moving it "
                "between assets and debts."
            ),
        ):
            update_manual_account(
                account.id,
                "Changed",
                kind,
                Decimal(20),
                "",
                date(2026, 10, 31),
                True,
                session=session,
            )
        assert account.type == "cash" and account.name == "Fake checking"
        # Staying on the same side is allowed even with a closed month.
        update_manual_account(
            account.id, "Renamed", "invest", Decimal(20), "", date(2026, 10, 31), session=session
        )
        assert not account.is_liability
    else:
        update_manual_account(
            account.id, "Changed", kind, Decimal(20), "", date(2026, 10, 31), True, session=session
        )
        assert account.is_liability and not account.is_manual


def test_new_day_inside_closed_month_skipped_and_late_account_notice(session):
    account = target(session)
    record_daily(account.id, date(2026, 9, 20), Decimal(10), source="csv", session=session)
    close_month("2026-09", today=date(2026, 10, 31), session=session)
    result = write_import(session, account, [], {DAY: Decimal(99)}, "csv")
    assert result.balance_days_skipped == 1 and result.balances_written == 0
    assert session.get(BalanceSnapshot, (account.id, DAY)) is None
    late = Account(name="Late account", type="cash")
    session.add(late)
    session.flush()
    result = write_import(session, late, [], {date(2026, 9, 15): Decimal(99)}, "csv")
    assert result.balances_written == 1
    assert session.get(MonthEndSnapshot, (late.id, "2026-09")) is None
    assert result.notices == ["Net worth for Sep 2026 now reflects this account."]


def test_plaid_manual_update_refused(session):
    institution = Institution(name="Fake Plaid", aggregator="plaid", external_id="fake")
    session.add(institution)
    session.flush()
    account = Account(name="Fake linked", type="cash", institution_id=institution.id)
    session.add(account)
    session.flush()
    with pytest.raises(ValueError, match="Plaid-linked"):
        update_manual_account(account.id, "Fake", "cash", Decimal(10), "", DAY, session=session)


def test_closed_other_debt_cannot_become_asset_by_checkbox(session):
    account = create_manual_account(
        "Fake debt", "other", Decimal(10), "", DAY, True, session=session
    )
    close_month("2026-09", today=date(2026, 10, 31), session=session)
    with pytest.raises(ValueError, match="closed months"):
        update_manual_account(
            account.id,
            "Fake asset",
            "other",
            Decimal(20),
            "",
            date(2026, 10, 31),
            False,
            session=session,
        )
    assert account.is_liability and account.is_manual


def test_older_days_with_balances_before_next_close_are_written(session):
    account = target(session)
    record_daily(account.id, date(2026, 7, 31), Decimal(10), source="csv", session=session)
    close_month("2026-07", today=date(2026, 11, 1), session=session)
    record_daily(account.id, date(2026, 10, 31), Decimal(20), source="csv", session=session)
    close_month("2026-10", today=date(2026, 11, 1), session=session)
    before = date(2026, 6, 15)
    gap = date(2026, 9, 15)
    end = date(2026, 10, 31)
    after = date(2026, 11, 1)
    result = write_import(
        session,
        account,
        [],
        {before: Decimal(99), gap: Decimal(99), end: Decimal(99), after: Decimal(30)},
        "csv",
    )
    assert result.balance_days_skipped == 1
    assert result.protected_balance_days == {end}
    assert result.balances == [(before, Decimal(99)), (gap, Decimal(99)), (after, Decimal(30))]
    assert session.get(BalanceSnapshot, (account.id, gap)).balance == Decimal(99)
    assert session.get(BalanceSnapshot, (account.id, end)).balance == Decimal(20)


@pytest.mark.parametrize("existing", [None, "today", "yesterday"])
def test_future_balance_does_not_hide_missing_balance_warning(session, monkeypatch, existing):
    monkeypatch.setattr("aggregators.file_store.local_today", lambda: DAY)
    account = target(session)
    if existing:
        day = DAY if existing == "today" else DAY - timedelta(days=1)
        record_daily(account.id, day, Decimal(10), source="csv", session=session)
    result = write_import(session, account, [], {DAY + timedelta(days=1): Decimal(20)}, "csv")
    assert result.missing_balance is (existing is None)
    assert result.balances_written == 1


@pytest.mark.parametrize("same_day", [False, True])
def test_file_account_rename_preserves_importable_day(session, same_day):
    account = target(session)
    previous = DAY if same_day else DAY - timedelta(days=1)
    record_daily(account.id, previous, Decimal(10), source="csv", session=session)
    update_manual_account(account.id, "Renamed", "cash", Decimal(10), "", DAY, session=session)
    assert account.name == "Renamed" and not account.is_manual
    snapshot = session.get(BalanceSnapshot, (account.id, DAY))
    assert snapshot is None or snapshot.source == "csv"
    result = write_import(session, account, [], {DAY: Decimal(20)}, "csv")
    assert result.balances_written == 1 and result.balance_days_skipped == 0
    update_manual_account(account.id, "Renamed", "cash", Decimal(30), "", DAY, session=session)
    assert session.get(BalanceSnapshot, (account.id, DAY)).source == "manual"
    assert write_import(session, account, [], {DAY: Decimal(40)}, "csv").balance_days_skipped == 1
    assert session.get(BalanceSnapshot, (account.id, DAY)).balance == Decimal(30)
