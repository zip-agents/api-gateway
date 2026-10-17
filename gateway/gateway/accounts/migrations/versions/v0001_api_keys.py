"""User API keys.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("prefix", sa.String(8), primary_key=True),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("owner_label", sa.String(320), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.DateTime(timezone=True)),
        sa.Column("last_used", sa.DateTime(timezone=True)),
        sa.Column("use_count", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_index("ix_api_keys_owner_created", "api_keys", ["owner", "created"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_owner_created", table_name="api_keys")
    op.drop_table("api_keys")
