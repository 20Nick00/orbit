"""Create the core financial schema and protect closed months."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "institutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aggregator", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institutions")),
        sa.UniqueConstraint("aggregator", "external_id", name=op.f("uq_institutions_aggregator")),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("subtype", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
        sa.Column("is_liability", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("mask", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("hidden", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "type IN ('cash', 'invest', 'retire', 'health', 'insure', 'crypto', 'property', "
            "'cards', 'loans', 'mortgage', 'other')",
            name=op.f("ck_accounts_valid_type"),
        ),
        sa.CheckConstraint(
            "type = 'other' OR "
            "(type IN ('cards', 'loans', 'mortgage') AND is_liability = 1) OR "
            "(type NOT IN ('cards', 'loans', 'mortgage') AND is_liability = 0)",
            name=op.f("ck_accounts_liability_matches_type"),
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name=op.f("fk_accounts_institution_id_institutions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_table(
        "balance_snapshots",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_balance_snapshots_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("account_id", "date", name=op.f("pk_balance_snapshots")),
    )
    op.create_table(
        "month_end_snapshots",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name=op.f("ck_month_end_snapshots_month_format"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_month_end_snapshots_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("account_id", "month", name=op.f("pk_month_end_snapshots")),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("merchant", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("pending", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_transactions_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("account_id", "external_id", name=op.f("uq_transactions_account_id")),
    )
    op.create_index("ix_transactions_account_id_date", "transactions", ["account_id", "date"])
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("qty", sa.Text(), nullable=True),
        sa.Column("value", sa.BigInteger(), nullable=True),
        sa.Column("cost_basis", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_holdings_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holdings")),
    )
    op.create_index("ix_holdings_account_id_date", "holdings", ["account_id", "date"])
    op.create_table(
        "account_facts",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_account_facts_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("account_id", "key", name=op.f("pk_account_facts")),
    )
    op.create_table(
        "paystubs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employer", sa.Text(), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("gross", sa.BigInteger(), nullable=True),
        sa.Column("net", sa.BigInteger(), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paystubs")),
    )
    op.create_table(
        "paystub_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paystub_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("group", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("ytd_amount", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "\"group\" IN ('tax', 'pretax', 'posttax', 'benefit', 'employer')",
            name=op.f("ck_paystub_lines_valid_group"),
        ),
        sa.ForeignKeyConstraint(
            ["paystub_id"], ["paystubs.id"], name=op.f("fk_paystub_lines_paystub_id_paystubs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paystub_lines")),
    )
    op.create_table(
        "import_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("matcher", sa.Text(), nullable=False),
        sa.Column("column_map", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("line_map", sa.JSON(none_as_null=True), nullable=True),
        sa.CheckConstraint(
            "(column_map IS NOT NULL AND line_map IS NULL) OR "
            "(column_map IS NULL AND line_map IS NOT NULL)",
            name=op.f("ck_import_rules_exactly_one_map"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rules")),
    )
    op.create_table(
        "valuations",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_valuations_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("account_id", "date", "source", name=op.f("pk_valuations")),
    )
    op.create_table(
        "type_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("aggregator", sa.Text(), nullable=True),
        sa.Column("account_type", sa.Text(), nullable=True),
        sa.Column("subtype", sa.Text(), nullable=True),
        sa.Column("name_contains", sa.Text(), nullable=True),
        sa.Column("mapped_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "aggregator IS NOT NULL OR account_type IS NOT NULL OR "
            "subtype IS NOT NULL OR name_contains IS NOT NULL",
            name=op.f("ck_type_overrides_has_matcher"),
        ),
        sa.CheckConstraint(
            "mapped_type IN ('cash', 'invest', 'retire', 'health', 'insure', 'crypto', "
            "'property', 'cards', 'loans', 'mortgage', 'other')",
            name=op.f("ck_type_overrides_valid_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_type_overrides")),
    )
    if op.get_context().dialect.name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER month_end_snapshots_no_replace "
                "BEFORE INSERT ON month_end_snapshots "
                "WHEN EXISTS (SELECT 1 FROM month_end_snapshots "
                "WHERE account_id = NEW.account_id AND month = NEW.month) BEGIN "
                "SELECT RAISE(ABORT, 'month_end_snapshots are immutable'); END"
            )
        )
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER month_end_snapshots_no_{action.lower()} "
                    f"BEFORE {action} ON month_end_snapshots BEGIN "
                    "SELECT RAISE(ABORT, 'month_end_snapshots are immutable'); END"
                )
            )


def downgrade():
    if op.get_context().dialect.name == "sqlite":
        op.execute(sa.text("DROP TRIGGER month_end_snapshots_no_replace"))
        op.execute(sa.text("DROP TRIGGER month_end_snapshots_no_update"))
        op.execute(sa.text("DROP TRIGGER month_end_snapshots_no_delete"))
    for table in (
        "type_overrides",
        "valuations",
        "import_rules",
        "paystub_lines",
        "paystubs",
        "account_facts",
        "holdings",
        "transactions",
        "month_end_snapshots",
        "balance_snapshots",
        "accounts",
        "institutions",
    ):
        op.drop_table(table)
