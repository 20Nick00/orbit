"""Encrypted Plaid connections and hosted Link sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plaid_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("institution_id", sa.Integer(), sa.ForeignKey("institutions.id"), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("transactions_cursor", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_synced_at", sa.DateTime()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.UniqueConstraint("item_id"),
        sa.CheckConstraint("status IN ('ok', 'login_required', 'error')", name="valid_status"),
    )
    op.create_table(
        "plaid_accounts",
        sa.Column("plaid_account_id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("plaid_items.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("legacy_transaction_max_id", sa.Integer()),
        sa.Column("missing_since", sa.Date()),
        sa.UniqueConstraint("account_id"),
    )
    op.create_table(
        "plaid_link_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("plaid_item_id", sa.Integer(), sa.ForeignKey("plaid_items.id")),
        sa.Column("link_token_encrypted", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.UniqueConstraint("state"),
        sa.CheckConstraint("kind IN ('connect', 'reauth')", name="valid_kind"),
    )


def downgrade():
    op.drop_table("plaid_link_sessions")
    op.drop_table("plaid_accounts")
    op.drop_table("plaid_items")
