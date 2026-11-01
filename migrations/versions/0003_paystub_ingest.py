"""Paystub ingestion, review queue and mailbox job state."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("paystubs", recreate="never") as batch:
        batch.add_column(sa.Column("gross_ytd", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("net_ytd", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("file_sha256", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reconcile_diff", sa.BigInteger(), nullable=True))
        # A unique index avoids rebuilding the parent table with live line foreign keys.
        batch.create_index("ix_paystubs_file_sha256", ["file_sha256"], unique=True)
    # An inline check avoids a table rebuild, which offline (--sql) migrations cannot do on SQLite.
    op.execute(
        "ALTER TABLE paystub_lines ADD COLUMN limit_bucket TEXT "
        "CONSTRAINT ck_paystub_lines_valid_limit_bucket CHECK (limit_bucket IN ('401k', 'hsa'))"
    )
    op.create_table(
        "paystub_pending",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("source_file", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("suggested_employer", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint(
            "status IN ('needs_mapping', 'needs_review', 'failed', 'dismissed')",
            name="valid_status",
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "paystub_job_state",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("last_success_month", sa.Text(), nullable=True),
        sa.Column("last_attempt_date", sa.Date(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_saved_count", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_table("paystub_job_state")
    op.drop_table("paystub_pending")
    op.execute("ALTER TABLE paystub_lines DROP COLUMN limit_bucket")
    with op.batch_alter_table("paystubs", recreate="never") as batch:
        batch.drop_index("ix_paystubs_file_sha256")
        for column in ("gross_ytd", "net_ytd", "file_sha256", "reconcile_diff"):
            batch.drop_column(column)
