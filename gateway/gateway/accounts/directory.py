"""User accounts in Cognito: the only writes the gateway makes there.

Self sign-up is off in the user pool, so accounts are created only here, when someone
redeems an invite code, and by the runbook's admin commands (docs/aws-runbook.md) for
admins. The account gets the password its owner chose on /account/join: AdminCreateUser
with no email (MessageAction=SUPPRESS),
then AdminSetUserPassword as permanent. If the password is refused, the half-made account
is deleted again, so the email address is not left stuck. Disabling a user also signs
them out of Cognito everywhere.

Cognito is also the source of truth for whether a user still exists, is enabled, and is
an admin (`user_state`): users.py asks when the user makes a request, at most once a
minute per user, and asks for groups only for admin sessions.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Protocol


class UserExists(Exception):
    pass


class InvalidPassword(Exception):
    """Cognito refused the password (its policy). The message is Cognito's."""


class DirectoryError(Exception):
    pass


@dataclass(frozen=True)
class DirectoryUser:
    enabled: bool
    admin: bool | None  # a member of the "admin" group; None if groups weren't asked for


class Directory(Protocol):
    async def create_user(self, email: str, invite_id: str, password: str) -> None: ...

    async def disable_user(self, sub: str) -> None: ...

    async def user_state(self, sub: str, *, groups: bool) -> DirectoryUser | None: ...


class CognitoDirectory:
    def __init__(self, user_pool_id: str, region: str, endpoint_url: str | None = None) -> None:
        import boto3  # only needed with Cognito configured

        self._pool = user_pool_id
        # endpoint_url: the local fake-cognito; None for the real Cognito endpoint.
        self._client: Any = boto3.client(
            "cognito-idp", region_name=region, endpoint_url=endpoint_url
        )

    async def create_user(self, email: str, invite_id: str, password: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.admin_create_user,
                UserPoolId=self._pool,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    # Nothing was sent to the address, so it is not verified.
                    {"Name": "email_verified", "Value": "false"},
                    {"Name": "custom:invite_id", "Value": invite_id},
                ],
                MessageAction="SUPPRESS",  # no email: they chose their own password
            )
        except self._client.exceptions.UsernameExistsException as exc:
            raise UserExists(email) from exc
        except Exception as exc:
            raise DirectoryError(type(exc).__name__) from exc
        try:
            await asyncio.to_thread(
                self._client.admin_set_user_password,
                UserPoolId=self._pool,
                Username=email,
                Password=password,
                Permanent=True,
            )
        except Exception as exc:
            # Don't leave an account nobody can sign in to, holding the email address.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._client.admin_delete_user, UserPoolId=self._pool, Username=email
                )
            if isinstance(exc, self._client.exceptions.InvalidPasswordException):
                raise InvalidPassword(str(exc)) from exc
            raise DirectoryError(type(exc).__name__) from exc

    async def disable_user(self, sub: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.admin_disable_user, UserPoolId=self._pool, Username=sub
            )
            await asyncio.to_thread(
                self._client.admin_user_global_sign_out, UserPoolId=self._pool, Username=sub
            )
        except Exception as exc:
            raise DirectoryError(type(exc).__name__) from exc

    async def user_state(self, sub: str, *, groups: bool) -> DirectoryUser | None:
        """Whether the user exists (None if not), is enabled, and (with `groups`, a second
        call) is in the admin group."""
        try:
            user = await asyncio.to_thread(
                self._client.admin_get_user, UserPoolId=self._pool, Username=sub
            )
        except self._client.exceptions.UserNotFoundException:
            return None
        except Exception as exc:
            raise DirectoryError(type(exc).__name__) from exc
        enabled = bool(user.get("Enabled", True))
        if not groups:
            return DirectoryUser(enabled=enabled, admin=None)
        try:
            listed = await asyncio.to_thread(
                self._client.admin_list_groups_for_user, UserPoolId=self._pool, Username=sub
            )
        except Exception as exc:
            raise DirectoryError(type(exc).__name__) from exc
        return DirectoryUser(
            enabled=enabled,
            admin=any(g.get("GroupName") == "admin" for g in listed.get("Groups", [])),
        )
