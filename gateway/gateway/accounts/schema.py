"""The accounts database schema (SQLAlchemy Core). Alembic migrations create it; keep the
two in step (gateway/accounts/migrations/versions/)."""

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)

metadata = MetaData()

api_keys = Table(
    "api_keys",
    metadata,
    Column("prefix", String(8), primary_key=True),  # the key's ID; not secret
    Column("owner", String(200), nullable=False),  # "op:<name>" or "user:<cognito sub>"
    Column("owner_label", String(320), nullable=False),  # an email or a key name, for pages
    Column("name", String(64), nullable=False),
    Column("secret_hash", String(64), nullable=False),  # hex SHA-256; never the secret
    Column("created", DateTime(timezone=True), nullable=False),
    Column("revoked", DateTime(timezone=True)),
    Column("last_used", DateTime(timezone=True)),
    Column("use_count", BigInteger, nullable=False, server_default="0"),
    Index("ix_api_keys_owner_created", "owner", "created"),
)

users = Table(
    "users",
    metadata,
    Column("sub", String(64), primary_key=True),  # Cognito's immutable user id
    Column("email", String(320), nullable=False, unique=True),
    Column("created", DateTime(timezone=True), nullable=False),
    Column("invite_id", String(16)),  # the invite that created the account
    Column("last_login", DateTime(timezone=True)),
    Column("login_count", BigInteger, nullable=False, server_default="0"),
    Column("disabled", DateTime(timezone=True)),  # set by an admin; signs the user out
)

invites = Table(
    "invites",
    metadata,
    Column("id", String(16), primary_key=True),  # shown to admins; not the secret token
    Column("token_hash", String(64), nullable=False, unique=True),  # SHA-256 of the token
    Column("label", String(120), nullable=False),  # e.g. "Recruiter, Acme"
    Column("created_by", String(320), nullable=False),
    Column("created", DateTime(timezone=True), nullable=False),
    Column("expires", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),  # unused | used | revoked
    Column("email", String(320)),  # entered by whoever redeemed it
    Column("used_at", DateTime(timezone=True)),
    Column("used_by", String(64)),  # the Cognito sub, once that user first signs in
)

logins = Table(
    "logins",
    metadata,
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("sub", String(64), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("ip", String(64)),
    Column("user_agent", String(256)),
    Index("ix_logins_sub_at", "sub", "at"),
    Index("ix_logins_at", "at"),
)

# Bytes moved per user per UTC day (uploads plus archives), for the daily quota
# (quota.py). Written about once a minute per active user, not per request.
usage_daily = Table(
    "usage_daily",
    metadata,
    Column("owner", String(200), primary_key=True),  # "user:<cognito sub>"
    Column("day", Date, primary_key=True),
    Column("bytes", BigInteger, nullable=False, server_default="0"),
    Column("requests", BigInteger, nullable=False, server_default="0"),
    Index("ix_usage_daily_day", "day"),
)
