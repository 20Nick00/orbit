"""Audited balance edits and archive-first month reopening."""

import sqlalchemy as sa
from alembic import op

from core.models import Money

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "balance_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("old_balance", Money(), nullable=False),
        sa.Column("new_balance", Money(), nullable=False),
        sa.Column("old_source", sa.Text(), nullable=False),
        sa.Column(
            "edited_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )
    op.create_table(
        "month_end_reopens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "reopened_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleting", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="month_format",
        ),
    )
    op.create_table(
        "month_end_archive",
        sa.Column(
            "reopen_id", sa.Integer(), sa.ForeignKey("month_end_reopens.id"), primary_key=True
        ),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("balance", Money(), nullable=False),
    )
    if op.get_context().dialect.name == "sqlite":
        for table in ("balance_edits", "month_end_reopens"):
            op.execute(
                f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id) BEGIN "
                f"SELECT RAISE(ABORT, '{table} are immutable'); END"
            )
        op.execute(
            "CREATE TRIGGER month_end_archive_requires_open_reopen "
            "BEFORE INSERT ON month_end_archive "
            "WHEN NOT EXISTS (SELECT 1 FROM month_end_reopens "
            "WHERE id = NEW.reopen_id AND deleting = 1) "
            "OR EXISTS (SELECT 1 FROM month_end_archive "
            "WHERE reopen_id = NEW.reopen_id AND account_id = NEW.account_id) BEGIN "
            "SELECT RAISE(ABORT, 'month_end_archive are immutable'); END"
        )
        op.execute("DROP TRIGGER month_end_snapshots_no_delete")
        op.execute(
            "CREATE TRIGGER month_end_snapshots_no_delete BEFORE DELETE ON month_end_snapshots "
            "WHEN NOT EXISTS (SELECT 1 FROM month_end_archive a "
            "JOIN month_end_reopens r ON r.id = a.reopen_id "
            "WHERE r.deleting = 1 AND r.month = OLD.month "
            "AND a.account_id = OLD.account_id AND a.balance = OLD.balance) BEGIN "
            "SELECT RAISE(ABORT, 'month_end_snapshots are immutable'); END"
        )
        for table in ("balance_edits", "month_end_archive", "month_end_reopens"):
            for action in ("UPDATE", "DELETE"):
                condition = (
                    "WHEN OLD.deleting = 0 "
                    if table == "month_end_reopens" and action == "UPDATE"
                    else ""
                )
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    f"{condition}BEGIN SELECT RAISE(ABORT, '{table} are immutable'); END"
                )


def downgrade():
    if op.get_context().dialect.name == "sqlite":
        op.execute("DROP TRIGGER balance_edits_no_replace")
        op.execute("DROP TRIGGER month_end_reopens_no_replace")
        op.execute("DROP TRIGGER month_end_archive_requires_open_reopen")
        op.execute("DROP TRIGGER month_end_snapshots_no_delete")
        op.execute(
            "CREATE TRIGGER month_end_snapshots_no_delete "
            "BEFORE DELETE ON month_end_snapshots BEGIN "
            "SELECT RAISE(ABORT, 'month_end_snapshots are immutable'); END"
        )
        for table in ("balance_edits", "month_end_archive", "month_end_reopens"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER {table}_no_{action}")
    for table in ("month_end_archive", "month_end_reopens", "balance_edits"):
        op.drop_table(table)
