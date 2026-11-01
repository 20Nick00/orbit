"""Property request cache, quota reservations, and yearly notes."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # Cached dashboards predate the shared home-equity figure and its note.
    op.execute("DELETE FROM rollup_cache")
    op.create_table(
        "rentcast_cache",
        sa.Column("address_key", sa.Text(), primary_key=True),
        sa.Column("month", sa.Text(), primary_key=True),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("price_low", sa.BigInteger(), nullable=True),
        sa.Column("price_high", sa.BigInteger(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "rentcast_usage",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False),
    )
    op.create_table(
        "rentcast_attempts",
        sa.Column("address_key", sa.Text(), primary_key=True),
        sa.Column("month", sa.Text(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt", sa.Date(), nullable=False),
    )
    op.create_table(
        "almanac_notes",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade():
    op.drop_table("almanac_notes")
    op.drop_table("rentcast_attempts")
    op.drop_table("rentcast_usage")
    op.drop_table("rentcast_cache")
