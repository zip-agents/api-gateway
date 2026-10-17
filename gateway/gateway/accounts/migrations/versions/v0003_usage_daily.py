"""Daily byte usage per user, for the quota.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_daily",
        sa.Column("owner", sa.String(200), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("requests", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_index("ix_usage_daily_day", "usage_daily", ["day"])


def downgrade() -> None:
    op.drop_index("ix_usage_daily_day", table_name="usage_daily")
    op.drop_table("usage_daily")
