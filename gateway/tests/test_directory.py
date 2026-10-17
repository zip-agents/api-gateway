"""CognitoDirectory against botocore's Stubber: the exact Cognito calls, no AWS."""

from typing import Any

import pytest
from botocore.stub import Stubber

from gateway.accounts.directory import CognitoDirectory, DirectoryError, UserExists

pytestmark = pytest.mark.anyio

POOL = "eu-west-2_Test123"


@pytest.fixture
def directory() -> CognitoDirectory:
    return CognitoDirectory(POOL, "eu-west-2")


def stub(directory: CognitoDirectory) -> Any:
    return Stubber(directory._client)


async def test_create_user_asks_cognito_to_email_a_password(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_response(
            "admin_create_user",
            {"User": {"Username": "grace@example.com"}},
            {
                "UserPoolId": POOL,
                "Username": "grace@example.com",
                "UserAttributes": [
                    {"Name": "email", "Value": "grace@example.com"},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "custom:invite_id", "Value": "abc123"},
                ],
                "DesiredDeliveryMediums": ["EMAIL"],
            },
        )
        await directory.create_user("grace@example.com", "abc123")
        stubber.assert_no_pending_responses()


async def test_existing_user(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_client_error("admin_create_user", "UsernameExistsException")
        with pytest.raises(UserExists):
            await directory.create_user("grace@example.com", "abc123")


async def test_other_errors(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_client_error("admin_create_user", "TooManyRequestsException")
        with pytest.raises(DirectoryError, match="TooManyRequestsException"):
            await directory.create_user("grace@example.com", "abc123")


async def test_disable_user_also_signs_them_out(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        expected = {"UserPoolId": POOL, "Username": "sub-grace"}
        stubber.add_response("admin_disable_user", {}, expected)
        stubber.add_response("admin_user_global_sign_out", {}, expected)
        await directory.disable_user("sub-grace")
        stubber.assert_no_pending_responses()
