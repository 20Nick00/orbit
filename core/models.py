"""Core schema. Money is stored as integer cents, never floating point."""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from core.types import ACCOUNT_TYPES, LIABILITY_TYPES


def _decimal(value):
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
        raise TypeError("Amounts must be Decimal, int, or numeric str; never float or bool")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid decimal amount") from None
    if not result.is_finite():
        raise ValueError("Amounts must be finite")
    return result


def _money(value):
    result = _decimal(value)
    if result and result.adjusted() > 16:
        raise ValueError("Amount exceeds the integer cents storage range")
    with localcontext() as context:
        context.prec = max(28, len(result.as_tuple().digits) + 2)
        result = result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(result * 100)
    if not -(2**63) <= cents < 2**63:
        raise ValueError("Amount exceeds the integer cents storage range")
    return result


class Money(TypeDecorator):
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = _money(value)
        with localcontext() as context:
            context.prec = 28
            return int(value * 100)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        with localcontext() as context:
            context.prec = 28
            return Decimal(value).scaleb(-2)


class DecimalText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(_decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


_TYPE_VALUES = ", ".join(repr(value) for value in ACCOUNT_TYPES)


class Institution(Base):
    __tablename__ = "institutions"
    __table_args__ = (UniqueConstraint("aggregator", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    aggregator: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)


class PlaidItem(Base):
    __tablename__ = "plaid_items"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'login_required', 'error')", name="valid_status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, unique=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"))
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    transactions_cursor: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="ok", server_default="ok")
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class PlaidAccount(Base):
    __tablename__ = "plaid_accounts"
    plaid_account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    legacy_transaction_max_id: Mapped[int | None] = mapped_column(Integer)
    missing_since: Mapped[dt.date | None] = mapped_column(Date)


class PlaidLinkSession(Base):
    __tablename__ = "plaid_link_sessions"
    __table_args__ = (CheckConstraint("kind IN ('connect', 'reauth')", name="valid_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    plaid_item_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_items.id"))
    link_token_encrypted: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(f"type IN ({_TYPE_VALUES})", name="valid_type"),
        CheckConstraint(
            "type = 'other' OR "
            "(type IN ('cards', 'loans', 'mortgage') AND is_liability = 1) OR "
            "(type NOT IN ('cards', 'loans', 'mortgage') AND is_liability = 0)",
            name="liability_matches_type",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institutions.id"))
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    subtype: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    is_liability: Mapped[bool] = mapped_column(
        Boolean,
        default=lambda context: context.get_current_parameters()["type"] in LIABILITY_TYPES,
        server_default=text("0"),
    )
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    mask: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(Text)


class MonthEndSnapshot(Base):
    __tablename__ = "month_end_snapshots"
    __table_args__ = (
        CheckConstraint(
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="month_format",
        ),
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Money)


class BalanceEdit(Base):
    __tablename__ = "balance_edits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    old_balance: Mapped[Decimal] = mapped_column(Money)
    new_balance: Mapped[Decimal] = mapped_column(Money)
    old_source: Mapped[str] = mapped_column(Text)
    edited_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class MonthEndReopen(Base):
    __tablename__ = "month_end_reopens"
    __table_args__ = (
        CheckConstraint(
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="month_format",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[str] = mapped_column(String(7))
    reason: Mapped[str | None] = mapped_column(Text)
    reopened_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    deleting: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class MonthEndArchive(Base):
    __tablename__ = "month_end_archive"
    reopen_id: Mapped[int] = mapped_column(ForeignKey("month_end_reopens.id"), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Money)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id"),
        Index("ix_transactions_account_id_date", "account_id", "date"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    merchant: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    memo: Mapped[str | None] = mapped_column(Text)
    pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    external_id: Mapped[str | None] = mapped_column(Text)


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (Index("ix_holdings_account_id_date", "account_id", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    ticker: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(DecimalText)
    value: Mapped[Decimal | None] = mapped_column(Money)
    cost_basis: Mapped[Decimal | None] = mapped_column(Money)


class AccountFact(Base):
    __tablename__ = "account_facts"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class Paystub(Base):
    __tablename__ = "paystubs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employer: Mapped[str] = mapped_column(Text)
    pay_date: Mapped[dt.date] = mapped_column(Date)
    period_start: Mapped[dt.date | None] = mapped_column(Date)
    period_end: Mapped[dt.date | None] = mapped_column(Date)
    gross: Mapped[Decimal | None] = mapped_column(Money)
    net: Mapped[Decimal | None] = mapped_column(Money)
    gross_ytd: Mapped[Decimal | None] = mapped_column(Money)
    net_ytd: Mapped[Decimal | None] = mapped_column(Money)
    file_sha256: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    reconcile_diff: Mapped[Decimal | None] = mapped_column(Money)
    source_file: Mapped[str | None] = mapped_column(Text)


class PaystubLine(Base):
    __tablename__ = "paystub_lines"
    __table_args__ = (
        CheckConstraint(
            "\"group\" IN ('tax', 'pretax', 'posttax', 'benefit', 'employer')", name="valid_group"
        ),
        CheckConstraint("limit_bucket IN ('401k', 'hsa')", name="valid_limit_bucket"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paystub_id: Mapped[int] = mapped_column(ForeignKey("paystubs.id"))
    label: Mapped[str] = mapped_column(Text)
    group_: Mapped[str] = mapped_column("group", Text)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    ytd_amount: Mapped[Decimal | None] = mapped_column(Money)
    limit_bucket: Mapped[str | None] = mapped_column(Text)


class PaystubPending(Base):
    __tablename__ = "paystub_pending"
    __table_args__ = (
        CheckConstraint(
            "status IN ('needs_mapping', 'needs_review', 'failed', 'dismissed')",
            name="valid_status",
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_sha256: Mapped[str] = mapped_column(Text, unique=True)
    source_file: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    suggested_employer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class PaystubJobState(Base):
    __tablename__ = "paystub_job_state"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_success_month: Mapped[str | None] = mapped_column(Text)
    last_attempt_date: Mapped[dt.date | None] = mapped_column(Date)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_saved_count: Mapped[int | None] = mapped_column(Integer)


class ImportRule(Base):
    __tablename__ = "import_rules"
    __table_args__ = (
        CheckConstraint(
            "(column_map IS NOT NULL AND line_map IS NULL) OR "
            "(column_map IS NULL AND line_map IS NOT NULL)",
            name="exactly_one_map",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text)
    matcher: Mapped[str] = mapped_column(Text)
    column_map: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    line_map: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class Valuation(Base):
    __tablename__ = "valuations"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(Text, primary_key=True)


class TypeOverride(Base):
    __tablename__ = "type_overrides"
    __table_args__ = (
        CheckConstraint(
            "aggregator IS NOT NULL OR account_type IS NOT NULL OR "
            "subtype IS NOT NULL OR name_contains IS NOT NULL",
            name="has_matcher",
        ),
        CheckConstraint(f"mapped_type IN ({_TYPE_VALUES})", name="valid_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    aggregator: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str | None] = mapped_column(Text)
    subtype: Mapped[str | None] = mapped_column(Text)
    name_contains: Mapped[str | None] = mapped_column(Text)
    mapped_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class RentcastCache(Base):
    __tablename__ = "rentcast_cache"
    address_key: Mapped[str] = mapped_column(Text, primary_key=True)
    month: Mapped[str] = mapped_column(Text, primary_key=True)
    price: Mapped[Decimal] = mapped_column(Money)
    price_low: Mapped[Decimal | None] = mapped_column(Money)
    price_high: Mapped[Decimal | None] = mapped_column(Money)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class RentcastUsage(Base):
    __tablename__ = "rentcast_usage"
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer)


class RentcastAttempts(Base):
    __tablename__ = "rentcast_attempts"
    address_key: Mapped[str] = mapped_column(Text, primary_key=True)
    month: Mapped[str] = mapped_column(Text, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    last_attempt: Mapped[dt.date] = mapped_column(Date)


class AlmanacNote(Base):
    __tablename__ = "almanac_notes"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    note: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class RollupCache(Base):
    __tablename__ = "rollup_cache"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[dt.date] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSON)


# Create this table last: its SQLite triggers reference all five source tables.
for _source in (BalanceSnapshot, MonthEndSnapshot, Account, Institution, AccountFact):
    RollupCache.__table__.add_is_dependent_on(_source.__table__)
    for _action in ("INSERT", "UPDATE", "DELETE"):
        event.listen(
            RollupCache.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER invalidate_rollups_{_source.__tablename__}_{_action.lower()} "
                f"AFTER {_action} ON {_source.__tablename__} "
                "BEGIN DELETE FROM rollup_cache; END"
            ).execute_if(dialect="sqlite"),
        )
