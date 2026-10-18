"""Persistence for accounts data: SQL through SQLAlchemy Core (async).

Postgres in deployment (Aurora Serverless v2 on AWS, a container locally) and SQLite in
memory for tests and throwaway runs: the same code and the same migrations run on both.
The secret part of an API key is never stored, only its SHA-256 (see keys.py).

The schema is created and upgraded by Alembic (migrate.py), once per process, before the
first query. Health checks and operator keys never touch the database, so an idle Aurora
cluster can pause; and pooled connections are closed after IDLE_CLOSE_SECONDS without a
query, as Aurora does not pause while any connection is open (`IdleCloser`).
"""

import asyncio
import contextlib
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, delete, event, insert, make_url, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from gateway.accounts.connect import begin
from gateway.accounts.migrate import migrate
from gateway.accounts.schema import api_keys, invites, logins, usage_daily, users

# A paused Aurora Serverless cluster resumes on the first connection: ~15 s, 30 s or
# more after a day paused. Opening a connection is also retried once (connect.py).
CONNECT_TIMEOUT_SECONDS = 60
# Close idle pooled connections well before Aurora's auto-pause (10 minutes, database.tf).
IDLE_CLOSE_SECONDS = 300.0
# Amazon RDS's CA bundle, for GATEWAY_DB_SSL=verify-full (see certs/README.md).
RDS_CA_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "rds-global-bundle.pem"
_LIST_LIMIT = 1000


class KeyExists(Exception):
    """A key with this prefix already exists (a collision: generate another prefix)."""


@dataclass(frozen=True)
class ApiKeyRecord:
    prefix: str
    owner: str  # "op:<operator key name>" or "user:<cognito sub>"
    owner_label: str  # what pages show for the owner: an email or a key name
    name: str
    secret_hash: str  # hex SHA-256 of the secret part; the secret itself is never stored
    created: datetime
    revoked: datetime | None = None
    last_used: datetime | None = None
    use_count: int = 0

    @property
    def active(self) -> bool:
        return self.revoked is None


def create_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        # One shared connection, so an in-memory database lives as long as the engine.
        return create_async_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    url, tls = connect_tls(url)
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,  # survives Aurora pausing and resuming between requests
        pool_recycle=1800,
        connect_args={"timeout": CONNECT_TIMEOUT_SECONDS, "command_timeout": 30, **tls},
    )


def connect_tls(url: str) -> tuple[str, dict[str, Any]]:
    """Turn `?ssl=verify-full` into an SSL context that trusts only the RDS CAs and checks
    the hostname. asyncpg has no URL option for a CA file, so the context is passed as a
    connect argument instead. Other `ssl` values (`require`, `disable`) pass through."""
    parsed = make_url(url)
    if parsed.query.get("ssl") != "verify-full":
        return url, {}
    context = ssl.create_default_context(cafile=str(RDS_CA_BUNDLE))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return parsed.difference_update_query(["ssl"]).render_as_string(hide_password=False), {
        "ssl": context
    }


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything in the store is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _key(row: RowMapping) -> ApiKeyRecord:
    created = _utc(row["created"])
    assert created is not None
    return ApiKeyRecord(
        prefix=row["prefix"],
        owner=row["owner"],
        owner_label=row["owner_label"],
        name=row["name"],
        secret_hash=row["secret_hash"],
        created=created,
        revoked=_utc(row["revoked"]),
        last_used=_utc(row["last_used"]),
        use_count=int(row["use_count"]),
    )


LOGIN_RETENTION = timedelta(days=90)
USAGE_RETENTION = timedelta(days=90)


@dataclass(frozen=True)
class UserRecord:
    sub: str
    email: str
    created: datetime
    invite_id: str | None
    last_login: datetime | None
    login_count: int
    disabled: datetime | None
    sessions_valid_after: datetime | None = None

    @property
    def active(self) -> bool:
        return self.disabled is None


@dataclass(frozen=True)
class InviteRecord:
    id: str
    label: str
    created_by: str
    created: datetime
    expires: datetime
    status: str = "unused"  # unused | used | revoked
    email: str | None = None
    used_at: datetime | None = None
    used_by: str | None = None

    def state(self, now: datetime) -> str:
        """unused | used | revoked | expired, for display."""
        if self.status == "unused" and self.expires <= now:
            return "expired"
        return self.status


@dataclass(frozen=True)
class LoginRecord:
    sub: str
    email: str
    at: datetime
    ip: str
    user_agent: str


def _must(value: datetime | None) -> datetime:
    assert value is not None
    return value


def _user(row: RowMapping) -> UserRecord:
    return UserRecord(
        sub=row["sub"],
        email=row["email"],
        created=_must(_utc(row["created"])),
        invite_id=row["invite_id"],
        last_login=_utc(row["last_login"]),
        login_count=int(row["login_count"]),
        disabled=_utc(row["disabled"]),
        sessions_valid_after=_utc(row["sessions_valid_after"]),
    )


def _invite(row: RowMapping) -> InviteRecord:
    return InviteRecord(
        id=row["id"],
        label=row["label"],
        created_by=row["created_by"],
        created=_must(_utc(row["created"])),
        expires=_must(_utc(row["expires"])),
        status=row["status"],
        email=row["email"],
        used_at=_utc(row["used_at"]),
        used_by=row["used_by"],
    )


class IdleCloser:
    """Close the pool's connections once none has been used for `idle_seconds`.

    The pool would otherwise keep them open for good, and an open connection, even an idle
    one, keeps Aurora Serverless awake. The next query opens a new connection (waiting for
    Aurora to resume if it paused). Watches the pool's checkout and checkin events, so it
    covers every query; a timer runs only while connections are open.
    """

    def __init__(self, engine: AsyncEngine, idle_seconds: float) -> None:
        self._engine = engine
        self._idle = idle_seconds
        self._in_use = 0
        self._last_used = time.monotonic()
        self._timer: asyncio.Task[None] | None = None
        # Listeners on the engine carry over to the new pool that dispose() creates.
        event.listen(engine.sync_engine, "checkout", self._checkout)
        event.listen(engine.sync_engine, "checkin", self._checkin)

    def _checkout(self, *_: object) -> None:
        self._in_use += 1

    def _checkin(self, *_: object) -> None:
        self._in_use = max(0, self._in_use - 1)
        self._last_used = time.monotonic()
        if self._timer is None:
            # Pool events run inside the event loop's thread (SQLAlchemy's greenlets).
            self._timer = asyncio.get_running_loop().create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        try:
            while True:
                left = self._last_used + self._idle - time.monotonic()
                if self._in_use == 0 and left <= 0:
                    await self._engine.dispose()
                    return
                await asyncio.sleep(left if left > 0 else self._idle)  # in use: look again
        finally:
            self._timer = None

    async def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer


class SqlStore:
    def __init__(self, engine: AsyncEngine, idle_close_seconds: float = IDLE_CLOSE_SECONDS) -> None:
        self._engine = engine
        self._ready = False
        self._lock = asyncio.Lock()
        # Not SQLite: its single shared connection is the in-memory database.
        self._idle_closer = (
            None if engine.dialect.name == "sqlite" else IdleCloser(engine, idle_close_seconds)
        )

    @classmethod
    def from_url(cls, url: str) -> "SqlStore":
        return cls(create_engine(url))

    async def ensure_schema(self) -> None:
        """Run migrations once per process (also called at startup, to fail fast)."""
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await migrate(self._engine)
                self._ready = True

    async def close(self) -> None:
        if self._idle_closer is not None:
            await self._idle_closer.stop()
        await self._engine.dispose()

    async def _execute(self, statement: Any) -> Any:
        await self.ensure_schema()
        async with begin(self._engine) as connection:
            return await connection.execute(statement)

    async def create_key(self, key: ApiKeyRecord) -> None:
        try:
            await self._execute(
                insert(api_keys).values(
                    prefix=key.prefix,
                    owner=key.owner,
                    owner_label=key.owner_label,
                    name=key.name,
                    secret_hash=key.secret_hash,
                    created=key.created,
                    revoked=key.revoked,
                    last_used=key.last_used,
                    use_count=key.use_count,
                )
            )
        except IntegrityError as exc:
            raise KeyExists(key.prefix) from exc

    async def get_key(self, prefix: str) -> ApiKeyRecord | None:
        result = await self._execute(select(api_keys).where(api_keys.c.prefix == prefix))
        row = result.mappings().first()
        return _key(row) if row is not None else None

    async def list_keys(self, owner: str | None = None) -> list[ApiKeyRecord]:
        query = select(api_keys).order_by(api_keys.c.created.desc()).limit(_LIST_LIMIT)
        if owner is not None:
            query = query.where(api_keys.c.owner == owner)
        result = await self._execute(query)
        return [_key(row) for row in result.mappings()]

    async def revoke_key(self, prefix: str, when: datetime, owner: str | None = None) -> bool:
        statement = (
            update(api_keys)
            .where(api_keys.c.prefix == prefix, api_keys.c.revoked.is_(None))
            .values(revoked=when)
        )
        if owner is not None:
            statement = statement.where(api_keys.c.owner == owner)
        result = await self._execute(statement)
        return bool(result.rowcount == 1)

    async def add_key_usage(self, prefix: str, count: int, last_used: datetime) -> None:
        latest = case(
            (api_keys.c.last_used.is_(None), last_used),
            (api_keys.c.last_used < last_used, last_used),
            else_=api_keys.c.last_used,
        )
        await self._execute(
            update(api_keys)
            .where(api_keys.c.prefix == prefix)
            .values(use_count=api_keys.c.use_count + count, last_used=latest)
        )

    # ------------------------------------------------------------------- users and logins

    async def record_login(
        self,
        sub: str,
        email: str,
        invite_id: str | None,
        at: datetime,
        ip: str | None,
        user_agent: str | None,
    ) -> UserRecord:
        """Create the user on first sign-in, count the login and keep it in the history.

        Also links a first sign-in to the invite that created the account, and prunes
        login events older than LOGIN_RETENTION.
        """
        await self.ensure_schema()
        async with begin(self._engine) as connection:
            existing = (
                (await connection.execute(select(users).where(users.c.sub == sub)))
                .mappings()
                .first()
            )
            if existing is None:
                await connection.execute(
                    insert(users).values(
                        sub=sub,
                        email=email,
                        created=at,
                        invite_id=invite_id,
                        last_login=at,
                        login_count=1,
                    )
                )
                if invite_id:
                    await connection.execute(
                        update(invites)
                        .where(invites.c.id == invite_id, invites.c.used_by.is_(None))
                        .values(used_by=sub)
                    )
            else:
                await connection.execute(
                    update(users)
                    .where(users.c.sub == sub)
                    .values(email=email, last_login=at, login_count=users.c.login_count + 1)
                )
            await connection.execute(
                insert(logins).values(
                    sub=sub, at=at, ip=(ip or "")[:64], user_agent=(user_agent or "")[:256]
                )
            )
            await connection.execute(delete(logins).where(logins.c.at < at - LOGIN_RETENTION))
            row = (
                (await connection.execute(select(users).where(users.c.sub == sub))).mappings().one()
            )
        return _user(row)

    async def get_user(self, sub: str) -> UserRecord | None:
        row = (await self._execute(select(users).where(users.c.sub == sub))).mappings().first()
        return _user(row) if row is not None else None

    async def list_users(self) -> list[UserRecord]:
        query = select(users).order_by(users.c.created.desc()).limit(_LIST_LIMIT)
        return [_user(row) for row in (await self._execute(query)).mappings()]

    async def recent_logins(self, sub: str | None = None, limit: int = 50) -> list[LoginRecord]:
        query = (
            select(logins.c.sub, logins.c.at, logins.c.ip, logins.c.user_agent, users.c.email)
            .join(users, users.c.sub == logins.c.sub, isouter=True)
            .order_by(logins.c.at.desc())
            .limit(limit)
        )
        if sub is not None:
            query = query.where(logins.c.sub == sub)
        rows = (await self._execute(query)).mappings()
        return [
            LoginRecord(
                sub=row["sub"],
                email=row["email"] or "",
                at=_must(_utc(row["at"])),
                ip=row["ip"] or "",
                user_agent=row["user_agent"] or "",
            )
            for row in rows
        ]

    async def end_sessions(self, sub: str, when: datetime) -> None:
        """Refuse every session of this user issued before `when` (they signed out)."""
        await self._execute(
            update(users).where(users.c.sub == sub).values(sessions_valid_after=when)
        )

    async def disable_user(self, sub: str, when: datetime) -> bool:
        """Disable a user and revoke all their API keys, in one transaction."""
        await self.ensure_schema()
        async with begin(self._engine) as connection:
            result = await connection.execute(
                update(users)
                .where(users.c.sub == sub, users.c.disabled.is_(None))
                .values(disabled=when)
            )
            await connection.execute(
                update(api_keys)
                .where(api_keys.c.owner == f"user:{sub}", api_keys.c.revoked.is_(None))
                .values(revoked=when)
            )
        return bool(result.rowcount == 1)

    # ------------------------------------------------------------------- invites

    async def create_invite(self, invite: InviteRecord, token_hash: str) -> None:
        await self._execute(
            insert(invites).values(
                id=invite.id,
                token_hash=token_hash,
                label=invite.label,
                created_by=invite.created_by,
                created=invite.created,
                expires=invite.expires,
                status="unused",
            )
        )

    async def claim_invite(self, token_hash: str, email: str, now: datetime) -> InviteRecord | None:
        """Mark an unused, unexpired invite used by `email`; None if it cannot be.

        A single conditional UPDATE, so two simultaneous redemptions cannot both succeed.
        """
        result = await self._execute(
            update(invites)
            .where(
                invites.c.token_hash == token_hash,
                invites.c.status == "unused",
                invites.c.expires > now,
            )
            .values(status="used", email=email, used_at=now)
        )
        if result.rowcount != 1:
            return None
        row = (
            (await self._execute(select(invites).where(invites.c.token_hash == token_hash)))
            .mappings()
            .one()
        )
        return _invite(row)

    async def release_invite(self, invite_id: str) -> None:
        """Undo a claim whose account could not be created, so the link works again."""
        await self._execute(
            update(invites)
            .where(invites.c.id == invite_id, invites.c.status == "used")
            .values(status="unused", email=None, used_at=None)
        )

    async def revoke_invite(self, invite_id: str) -> bool:
        result = await self._execute(
            update(invites)
            .where(invites.c.id == invite_id, invites.c.status == "unused")
            .values(status="revoked")
        )
        return bool(result.rowcount == 1)

    async def list_invites(self) -> list[InviteRecord]:
        query = select(invites).order_by(invites.c.created.desc()).limit(_LIST_LIMIT)
        return [_invite(row) for row in (await self._execute(query)).mappings()]

    async def invite_labels(self) -> dict[str, str]:
        """invite id -> label, for showing how each user was invited."""
        rows = await self._execute(select(invites.c.id, invites.c.label))
        return {row.id: row.label for row in rows}

    # ------------------------------------------------------------------- daily usage

    async def add_daily_usage(self, owner: str, day: date, nbytes: int, requests: int) -> None:
        """Add to a user's total for `day`: one upsert, safe with several gateway tasks."""
        dialect = postgresql if self._engine.dialect.name == "postgresql" else sqlite
        statement = dialect.insert(usage_daily).values(
            owner=owner, day=day, bytes=nbytes, requests=requests
        )
        statement = statement.on_conflict_do_update(
            index_elements=[usage_daily.c.owner, usage_daily.c.day],
            set_={
                "bytes": usage_daily.c.bytes + statement.excluded.bytes,
                "requests": usage_daily.c.requests + statement.excluded.requests,
            },
        )
        await self._execute(statement)

    async def daily_usage(self, owner: str, day: date) -> int:
        query = select(usage_daily.c.bytes).where(
            usage_daily.c.owner == owner, usage_daily.c.day == day
        )
        return int((await self._execute(query)).scalar() or 0)

    async def daily_usage_all(self, day: date) -> dict[str, int]:
        """owner -> bytes on `day`, for the admin pages."""
        rows = await self._execute(
            select(usage_daily.c.owner, usage_daily.c.bytes).where(usage_daily.c.day == day)
        )
        return {row.owner: int(row.bytes) for row in rows}

    async def prune_daily_usage(self, today: date) -> None:
        await self._execute(delete(usage_daily).where(usage_daily.c.day < today - USAGE_RETENTION))
