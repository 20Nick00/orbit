from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from core.detail import read_account
from core.models import (
    BalanceEdit,
    BalanceSnapshot,
    MonthEndArchive,
    MonthEndReopen,
    MonthEndSnapshot,
)
from core.snapshots import (
    MonthEndLockedError,
    MonthNotClosedError,
    SnapshotNotFoundError,
    close_month,
    edit_snapshot,
    locked_days,
    net_worth_series,
    record_daily,
    reopen_month,
)

TODAY = date(2026, 10, 31)
FIRST = date(2026, 9, 3)
LAST = date(2026, 9, 28)


def seed(session, account_factory):
    account = account_factory()
    record_daily(account.id, FIRST, "100", source="import", session=session)
    record_daily(account.id, LAST, "200", source="import", session=session)
    session.commit()
    return account.id


def test_edit_audit_noop_and_missing(session, account_factory):
    key = seed(session, account_factory)
    assert edit_snapshot(key, FIRST, "150.25", session=session)
    row = session.get(BalanceSnapshot, (key, FIRST))
    assert (row.balance, row.source) == (Decimal("150.25"), "manual")
    edit = session.scalars(select(BalanceEdit)).one()
    assert (edit.old_balance, edit.new_balance, edit.old_source) == (
        Decimal("100"),
        Decimal("150.25"),
        "import",
    )
    assert edit.edited_at is not None
    assert not edit_snapshot(key, LAST, "200.00", session=session)
    assert session.get(BalanceSnapshot, (key, LAST)).source == "import"
    assert session.scalar(select(func.count()).select_from(BalanceEdit)) == 1
    with pytest.raises(SnapshotNotFoundError):
        edit_snapshot(key, date(2026, 9, 4), "100", session=session)


def test_locked_days_and_frozen_series(session, account_factory):
    key = seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    session.commit()
    assert locked_days(key, session=session) == {LAST: "2026-09"}
    with pytest.raises(MonthEndLockedError):
        edit_snapshot(key, LAST, "300", session=session)
    assert edit_snapshot(key, FIRST, "125", session=session)
    daily = net_worth_series(FIRST, LAST, session=session)
    assert [point.net_worth for point in daily[:-1]] == [Decimal("125")] * 25
    assert daily[-1].net_worth == Decimal("200")
    monthly = net_worth_series(FIRST, TODAY, "month", session=session)
    assert monthly[0].net_worth == Decimal("200")
    data = read_account(session, key, TODAY, month="2026-09", range_key="All")
    assert data["history"]["rows"][0]["is_month_end"]
    assert data["history"]["rows"][1]["editable"]
    assert data["chart"]["series"][data["chart"]["dates"].index("2026-09-30")] == 200


def test_reopen_all_accounts_archive_and_reclose(session, account_factory):
    key = seed(session, account_factory)
    debt = account_factory(type="loans")
    record_daily(debt.id, LAST, "75.99", session=session)
    close_month("2026-09", today=TODAY, session=session)
    session.commit()
    assert reopen_month("2026-09", reason="Correction", session=session) == 2
    assert not session.scalars(select(MonthEndSnapshot)).all()
    archived = {r.account_id: r.balance for r in session.scalars(select(MonthEndArchive))}
    assert archived == {key: Decimal("200"), debt.id: Decimal("75.99")}
    reopen = session.scalars(select(MonthEndReopen)).one()
    assert reopen.reason == "Correction" and not reopen.deleting
    edit_snapshot(key, LAST, "250", session=session)
    assert close_month("2026-09", today=TODAY, session=session) == 2
    assert session.get(MonthEndSnapshot, (key, "2026-09")).balance == Decimal("250")
    session.commit()
    history = read_account(session, key, TODAY, month="2026-09")["history"]
    assert history["reopens"][0]["balance"] == 20000
    assert history["reopens"][0]["reason"] == "Correction"


@pytest.mark.parametrize("mismatch", ["balance", "month", "account"])
def test_raw_delete_requires_exact_archive(session, account_factory, mismatch):
    key = seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    session.commit()
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(delete(MonthEndSnapshot))
    session.rollback()
    other = account_factory()
    reopen = MonthEndReopen(month="2026-08" if mismatch == "month" else "2026-09", deleting=True)
    session.add(reopen)
    session.flush()
    session.add(
        MonthEndArchive(
            reopen_id=reopen.id,
            account_id=other.id if mismatch == "account" else key,
            balance=Decimal("199" if mismatch == "balance" else "200"),
        )
    )
    session.flush()
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(delete(MonthEndSnapshot))
    session.rollback()
    assert session.get(MonthEndSnapshot, (key, "2026-09")).balance == Decimal("200")


@pytest.mark.parametrize("table", ["balance_edits", "month_end_archive", "month_end_reopens"])
@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_audits_are_immutable(session, account_factory, table, action):
    key = seed(session, account_factory)
    edit_snapshot(key, FIRST, "101", session=session)
    close_month("2026-09", today=TODAY, session=session)
    reopen_month("2026-09", session=session)
    session.commit()
    column = "balance" if table == "month_end_archive" else "id"
    sql = (
        f"UPDATE {table} SET {column} = {column}" if action == "UPDATE" else f"DELETE FROM {table}"
    )
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(text(sql))
    session.rollback()


def test_finished_reopen_cannot_authorize_later_delete(session, account_factory):
    seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    reopen_month("2026-09", session=session)
    close_month("2026-09", today=TODAY, session=session)
    session.commit()
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(update(MonthEndReopen).values(deleting=True))
    session.rollback()
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(delete(MonthEndSnapshot))
    session.rollback()


def test_failed_reopen_rolls_back_everything(database, session, account_factory):
    key = seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    session.commit()

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE month_end_reopens"):
            raise RuntimeError("Simulated failure after deletion")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="Simulated"):
            reopen_month("2026-09")
    finally:
        event.remove(database, "before_cursor_execute", fail)
    session.expire_all()
    assert session.get(MonthEndSnapshot, (key, "2026-09")).balance == Decimal("200")
    assert not session.scalars(select(MonthEndArchive)).all()
    assert not session.scalars(select(MonthEndReopen)).all()


def test_unclosed_month_raises(session):
    with pytest.raises(MonthNotClosedError):
        reopen_month("2026-09", session=session)


def test_edit_carries_only_until_next_snapshot(session, account_factory):
    key = seed(session, account_factory)
    middle = date(2026, 9, 15)
    record_daily(key, middle, "150", session=session)
    session.commit()
    edit_snapshot(key, middle, "175", session=session)
    series = {
        point.date: point.net_worth for point in net_worth_series(FIRST, TODAY, session=session)
    }
    assert series[date(2026, 9, 14)] == Decimal("100")
    assert series[middle] == series[date(2026, 9, 27)] == Decimal("175")
    assert series[LAST] == series[TODAY] == Decimal("200")


def test_carried_forward_closed_row_and_previous_month_change(session, account_factory):
    account = account_factory(type="loans")
    record_daily(account.id, date(2026, 8, 31), "500", session=session)
    close_month("2026-09", today=TODAY, session=session)
    record_daily(account.id, date(2026, 10, 2), "400", session=session)
    session.commit()
    august = read_account(session, account.id, TODAY, month="2026-08")["history"]["rows"][0]
    assert not august["editable"] and not august["is_month_end"]
    assert august["locked_by"] == "2026-09"
    assert august["balance_cents"] == 50000 and august["recorded_cents"] is None
    with pytest.raises(MonthEndLockedError):
        edit_snapshot(account.id, date(2026, 8, 31), "600", session=session)
    data = read_account(session, account.id, TODAY, month="2026-09")
    row = data["history"]["rows"][0]
    assert (row["date"], row["balance_cents"], row["source"], row["editable"]) == (
        date(2026, 9, 30),
        50000,
        "month-end",
        False,
    )
    data = read_account(session, account.id, TODAY, month="2026-10")
    assert data["balance"] == 40000
    assert data["chart"]["series"][-1] == 400
    assert data["history"]["rows"][0]["change"] == 10000
    assert data["deltas"]["1M"]["d"] == 10000


def test_carried_forward_reopen_and_reclose(session, account_factory):
    account = account_factory()
    day = date(2026, 8, 31)
    record_daily(account.id, day, "500", session=session)
    close_month("2026-09", today=TODAY, session=session)
    assert locked_days(account.id, session=session) == {day: "2026-09"}
    reopen_month("2026-09", session=session)
    assert locked_days(account.id, session=session) == {}
    history = read_account(session, account.id, TODAY, month="2026-08")["history"]
    assert history["rows"][0]["editable"] and history["rows"][0]["locked_by"] is None
    assert edit_snapshot(account.id, day, "600", session=session)
    close_month("2026-09", today=TODAY, session=session)
    with pytest.raises(MonthEndLockedError):
        edit_snapshot(account.id, day, "700", session=session)
    assert session.get(MonthEndSnapshot, (account.id, "2026-09")).balance == Decimal("600")


def test_latest_dependent_month_and_two_queries(database, session, account_factory):
    account = account_factory()
    day = date(2026, 8, 31)
    record_daily(account.id, day, "500", session=session)
    for month in ("2026-08", "2026-09", "2026-10"):
        close_month(month, today=date(2026, 11, 1), session=session)
    key = account.id
    statements = []

    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database, "before_cursor_execute", track)
    try:
        assert locked_days(key, session=session) == {day: "2026-10"}
    finally:
        event.remove(database, "before_cursor_execute", track)
    assert len(statements) == 2
    # A close in the row's own month still displays that month's frozen value,
    # even when a later close also depends on it.
    row = read_account(session, key, TODAY, month="2026-08")["history"]["rows"][0]
    assert row["is_month_end"] and row["locked_by"] is None
    reopen_month("2026-08", session=session)
    row = read_account(session, key, TODAY, month="2026-08")["history"]["rows"][0]
    assert not row["is_month_end"] and row["locked_by"] == "2026-10"
    reopen_month("2026-10", session=session)
    assert locked_days(key, session=session) == {day: "2026-09"}
    with pytest.raises(MonthEndLockedError):
        edit_snapshot(key, day, "600", session=session)


def test_frozen_display_and_recorded_changes(session, account_factory):
    key = seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    record_daily(key, LAST, "250.30", source="import", session=session)
    record_daily(key, date(2026, 10, 2), "260.60", session=session)
    session.commit()
    row = read_account(session, key, TODAY, month="2026-09")["history"]["rows"][0]
    assert row["balance_cents"] == 20000 and row["recorded_cents"] == 25030
    assert row["change"] == 15030 and not row["editable"]
    following = read_account(session, key, TODAY, month="2026-10")["history"]["rows"][0]
    assert following["change"] == 1030 and following["recorded_cents"] is None
    assert row["change"] + following["change"] == 26060 - 10000


@pytest.mark.parametrize("table", ["balance_edits", "month_end_reopens", "month_end_archive"])
def test_audits_reject_insert_or_replace(session, account_factory, table):
    key = seed(session, account_factory)
    edit_snapshot(key, FIRST, "101", session=session)
    close_month("2026-09", today=TODAY, session=session)
    reopen_month("2026-09", session=session)
    # Exercise archive replacement while its parent is still open too, so the
    # duplicate-key guard is tested independently of the finished-parent guard.
    if table == "month_end_archive":
        parent = MonthEndReopen(month="2026-09", deleting=True)
        session.add(parent)
        session.flush()
        session.add(MonthEndArchive(reopen_id=parent.id, account_id=key, balance="200"))
        session.flush()
        statement = (
            "INSERT OR REPLACE INTO month_end_archive (reopen_id, account_id, balance) "
            "SELECT reopen_id, account_id, balance + 1 FROM month_end_archive "
            "WHERE reopen_id = :id"
        )
        params = {"id": parent.id}
    elif table == "balance_edits":
        statement = (
            "INSERT OR REPLACE INTO balance_edits "
            "(id, account_id, date, old_balance, new_balance, old_source, edited_at) "
            "SELECT id, account_id, date, old_balance, new_balance + 1, old_source, edited_at "
            "FROM balance_edits"
        )
        params = {}
    else:
        statement = (
            "INSERT OR REPLACE INTO month_end_reopens (id, month, reason, reopened_at, deleting) "
            "SELECT id, month, reason, reopened_at, 1 FROM month_end_reopens"
        )
        params = {}
    session.commit()
    assert session.scalar(text("PRAGMA recursive_triggers")) == 0
    with pytest.raises(IntegrityError, match=f"{table} are immutable"):
        session.execute(text(statement), params)
    session.rollback()


def test_finished_reopen_rejects_new_archive_rows(session, account_factory):
    seed(session, account_factory)
    close_month("2026-09", today=TODAY, session=session)
    reopen_month("2026-09", session=session)
    parent = session.scalars(select(MonthEndReopen)).one()
    other = account_factory()
    params = {"reopen": parent.id, "account": other.id}
    session.commit()
    with pytest.raises(IntegrityError, match="month_end_archive are immutable"):
        session.execute(
            text(
                "INSERT INTO month_end_archive (reopen_id, account_id, balance) "
                "VALUES (:reopen, :account, 20000)"
            ),
            params,
        )
    session.rollback()


@pytest.mark.parametrize("action", ["edit", "close", "reopen"])
def test_write_lock_precedes_history_reads(database, session, account_factory, action):
    key = seed(session, account_factory)
    if action == "reopen":
        close_month("2026-09", today=TODAY, session=session)
        session.commit()
    # A SQLAlchemy read transaction need not be a real driver transaction yet.
    session.scalar(select(BalanceSnapshot.balance).limit(1))
    statements = []

    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database, "before_cursor_execute", track)
    try:
        if action == "edit":
            edit_snapshot(key, FIRST, "101", session=session)
        elif action == "close":
            close_month("2026-09", today=TODAY, session=session)
        else:
            reopen_month("2026-09", session=session)
    finally:
        event.remove(database, "before_cursor_execute", track)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert any(statement.startswith("SELECT") for statement in statements[1:])
