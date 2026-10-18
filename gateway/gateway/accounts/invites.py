"""Invite codes: the only way to get an account.

An admin creates a one-time code for a recipient whose email they don't know (it goes to
a recruiter, who passes it on). The recipient opens /account/join, enters the code, and
chooses their own email and password. No email is sent at any point.

    K7QM-4XHT-9CWD     12 characters from 31 unambiguous ones (no 0/O, 1/I/L): ~59 bits

That is typeable, and still far beyond guessing: codes expire (1-30 days), work once,
and the join form allows 10 attempts a minute per client. Only SHA-256 of the normalised
code is stored, so the database cannot reproduce one.

Redeeming claims the code with a single conditional UPDATE (unused and unexpired ->
used), so a code works exactly once even if submitted twice at the same moment. If the
account then cannot be created (the email already has one, the password is refused,
Cognito fails), the claim is undone and the code works again.

The email address is not verified (nothing is sent to it): Cognito keeps it marked
unverified, so its forgotten-password email does not apply. A user who loses their
password gets a new code from an admin.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from gateway.accounts.directory import Directory, DirectoryError, InvalidPassword, UserExists
from gateway.accounts.keys import utc_now
from gateway.accounts.store import InviteRecord, SqlStore

MAX_DAYS = 30
LABEL_PATTERN = re.compile(r"^[\w .,:()/@&'+-]{1,120}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12


class InvalidInvite(ValueError):
    pass


class Redeemed(Enum):
    CREATED = "created"  # account created with the chosen password: sign in now
    INVALID = "invalid"  # unknown, used, revoked or expired
    EXISTS = "exists"  # that email already has an account; the code still works
    BAD_PASSWORD = "bad-password"  # noqa: S105 (an outcome, not a password): code still works
    UNAVAILABLE = "unavailable"  # sign-up is not configured, or Cognito failed


@dataclass(frozen=True)
class NewInvite:
    invite: InviteRecord
    code: str  # formatted for reading out: XXXX-XXXX-XXXX


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, CODE_LENGTH, 4))


def normalise_code(text: str) -> str | None:
    """The code as stored: upper case, without spaces or dashes. None if malformed."""
    code = re.sub(r"[\s-]", "", text).upper()
    if len(code) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in code):
        return None
    return code


def token_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def valid_email(email: str) -> bool:
    return len(email) <= 320 and EMAIL_PATTERN.fullmatch(email) is not None


def password_problem(password: str) -> str | None:
    """The user pool's policy (terraform/modules/cognito-pool), checked before Cognito is."""
    if len(password) < 12:
        return "Use at least 12 characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    if not (any(c.islower() for c in password) and any(c.isupper() for c in password)):
        return "Use both upper and lower case letters."
    if not any(c.isdigit() for c in password):
        return "Include at least one number."
    return None


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
            raise InvalidInvite(f"Codes can be valid for 1 to {MAX_DAYS} days.")
        now = utc_now()
        code = new_code()
        invite = InviteRecord(
            id=secrets.token_hex(6),
            label=label,
            created_by=created_by,
            created=now,
            expires=now + timedelta(days=days),
        )
        normalised = normalise_code(code)
        assert normalised is not None
        await self._store.create_invite(invite, token_hash(normalised))
        return NewInvite(invite, code)

    async def redeem(self, code: str, email: str, password: str) -> Redeemed:
        """Create the account. Email and password are checked by the caller first."""
        if self._directory is None:
            return Redeemed.UNAVAILABLE
        normalised = normalise_code(code)
        if normalised is None:
            return Redeemed.INVALID
        invite = await self._store.claim_invite(token_hash(normalised), email, utc_now())
        if invite is None:
            return Redeemed.INVALID
        try:
            await self._directory.create_user(email, invite.id, password)
        except UserExists:
            await self._store.release_invite(invite.id)
            return Redeemed.EXISTS
        except InvalidPassword:
            await self._store.release_invite(invite.id)
            return Redeemed.BAD_PASSWORD
        except DirectoryError:
            await self._store.release_invite(invite.id)
            return Redeemed.UNAVAILABLE
        return Redeemed.CREATED

    async def revoke(self, invite_id: str) -> bool:
        return await self._store.revoke_invite(invite_id)

    async def list_invites(self) -> list[InviteRecord]:
        return await self._store.list_invites()
