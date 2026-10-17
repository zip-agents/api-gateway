"""User accounts in Cognito: the only writes the gateway makes there.

Creating a user with AdminCreateUser makes Cognito email them a temporary password
(self sign-up is off in the user pool, so this is the only way in). Disabling a user
also signs them out of Cognito everywhere.
"""

import asyncio
from typing import Any, Protocol


class UserExists(Exception):
    pass


class DirectoryError(Exception):
    pass


class Directory(Protocol):
    async def create_user(self, email: str, invite_id: str) -> None: ...

    async def disable_user(self, sub: str) -> None: ...


class CognitoDirectory:
    def __init__(self, user_pool_id: str, region: str) -> None:
        import boto3  # only needed with Cognito configured

        self._pool = user_pool_id
        self._client: Any = boto3.client("cognito-idp", region_name=region)

    async def create_user(self, email: str, invite_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.admin_create_user,
                UserPoolId=self._pool,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    # They prove the address by receiving the temporary password.
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "custom:invite_id", "Value": invite_id},
                ],
                DesiredDeliveryMediums=["EMAIL"],
            )
        except self._client.exceptions.UsernameExistsException as exc:
            raise UserExists(email) from exc
        except Exception as exc:
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
