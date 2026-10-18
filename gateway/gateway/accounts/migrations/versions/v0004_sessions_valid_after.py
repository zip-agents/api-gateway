"""Per-user cut-off for sessions: signing out ends every copy of the session.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("sessions_valid_after", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("sessions_valid_after")
