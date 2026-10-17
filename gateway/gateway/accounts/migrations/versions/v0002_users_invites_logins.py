"""Users, invite links and login history.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("sub", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invite_id", sa.String(16)),
        sa.Column("last_login", sa.DateTime(timezone=True)),
        sa.Column("login_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("disabled", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("used_by", sa.String(64)),
    )
    op.create_table(
        "logins",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("sub", sa.String(64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(256)),
    )
    op.create_index("ix_logins_sub_at", "logins", ["sub", "at"])
    op.create_index("ix_logins_at", "logins", ["at"])


def downgrade() -> None:
    op.drop_table("logins")
    op.drop_table("invites")
    op.drop_table("users")
