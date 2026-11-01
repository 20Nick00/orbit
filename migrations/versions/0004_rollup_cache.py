"""Precomputed dashboard and automatic cache invalidation."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SOURCES = (
    "balance_snapshots",
    "month_end_snapshots",
    "accounts",
    "institutions",
    "account_facts",
)


def upgrade():
    op.create_table(
        "rollup_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_rollup_cache"),
    )
    if op.get_context().dialect.name == "sqlite":
        for source in SOURCES:
            for action in ("INSERT", "UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER invalidate_rollups_{source}_{action.lower()} "
                        f"AFTER {action} ON {source} BEGIN DELETE FROM rollup_cache; END"
                    )
                )


def downgrade():
    if op.get_context().dialect.name == "sqlite":
        for source in SOURCES:
            for action in ("insert", "update", "delete"):
                op.execute(sa.text(f"DROP TRIGGER invalidate_rollups_{source}_{action}"))
    op.drop_table("rollup_cache")
