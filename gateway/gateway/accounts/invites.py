"""Invite links: the only way to get an account.

An admin creates a link for a recipient whose email they do not know (it goes to a
recruiter, who passes it on). Whoever opens it enters their own email; the link is then
used up, and Cognito emails that address a temporary password.

    https://<host>/docs/invite/<token>     token: 24 random bytes, base64url

Only SHA-256(token) is stored, so the database cannot reproduce a link. Claiming is a
single conditional UPDATE (unused and unexpired -> used), so a link works exactly once
even if it is submitted twice at the same time. If Cognito then refuses to create the
account (the email already has one), the claim is undone and the link works again.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from gateway.accounts.directory import Directory, DirectoryError, UserExists
from gateway.accounts.keys import utc_now
from gateway.accounts.store import InviteRecord, SqlStore

MAX_DAYS = 30
LABEL_PATTERN = re.compile(r"^[\w .,:()/@&'+-]{1,120}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class InvalidInvite(ValueError):
    pass


class Redeemed(Enum):
    CREATED = "created"  # account created; Cognito has emailed the temporary password
    INVALID = "invalid"  # unknown, used, revoked or expired
    EXISTS = "exists"  # that email already has an account; the link still works
    UNAVAILABLE = "unavailable"  # sign-up is not configured, or Cognito failed


@dataclass(frozen=True)
class NewInvite:
    invite: InviteRecord
    token: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def valid_email(email: str) -> bool:
    return len(email) <= 320 and EMAIL_PATTERN.fullmatch(email) is not None


class InviteService:
    def __init__(self, store: SqlStore, directory: Directory | None) -> None:
        self._store = store
        self._directory = directory

    @property
    def enabled(self) -> bool:
        return self._directory is not None

    async def create(self, label: str, days: int, created_by: str) -> NewInvite:
        label = label.strip()
        if not LABEL_PATTERN.fullmatch(label):
            raise InvalidInvite("Use a label of 1-120 plain characters, e.g. 'Recruiter, Acme'.")
        if not 1 <= days <= MAX_DAYS:
            raise InvalidInvite(f"Links can be valid for 1 to {MAX_DAYS} days.")
        now = utc_now()
        token = secrets.token_urlsafe(24)
        invite = InviteRecord(
            id=secrets.token_hex(6),
            label=label,
            created_by=created_by,
            created=now,
            expires=now + timedelta(days=days),
        )
        await self._store.create_invite(invite, token_hash(token))
        return NewInvite(invite, token)

    async def is_open(self, token: str) -> bool:
        """Whether the link can still be used (for the invite page; never reveals why not)."""
        return await self._store.invite_open(token_hash(token), utc_now())

    async def redeem(self, token: str, email: str) -> Redeemed:
        if self._directory is None:
            return Redeemed.UNAVAILABLE
        invite = await self._store.claim_invite(token_hash(token), email, utc_now())
        if invite is None:
            return Redeemed.INVALID
        try:
            await self._directory.create_user(email, invite.id)
        except UserExists:
            await self._store.release_invite(invite.id)
            return Redeemed.EXISTS
        except DirectoryError:
            await self._store.release_invite(invite.id)
            return Redeemed.UNAVAILABLE
        return Redeemed.CREATED

    async def revoke(self, invite_id: str) -> bool:
        return await self._store.revoke_invite(invite_id)

    async def list_invites(self) -> list[InviteRecord]:
        return await self._store.list_invites()
