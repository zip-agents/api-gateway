"""CognitoDirectory against botocore's Stubber: the exact Cognito calls, no AWS."""

from typing import Any

import pytest
from botocore.stub import Stubber

from gateway.accounts.directory import (
    CognitoDirectory,
    DirectoryError,
    DirectoryUser,
    InvalidPassword,
    UserExists,
)

pytestmark = pytest.mark.anyio

POOL = "eu-west-2_Test123"
PASSWORD = "Grace-Password-1"


@pytest.fixture
def directory() -> CognitoDirectory:
    return CognitoDirectory(POOL, "eu-west-2")


def stub(directory: CognitoDirectory) -> Any:
    return Stubber(directory._client)


async def test_create_user_sends_no_email_and_sets_the_chosen_password(
    directory: CognitoDirectory,
) -> None:
    with stub(directory) as stubber:
        stubber.add_response(
            "admin_create_user",
            {"User": {"Username": "grace@example.com"}},
            {
                "UserPoolId": POOL,
                "Username": "grace@example.com",
                "UserAttributes": [
                    {"Name": "email", "Value": "grace@example.com"},
                    {"Name": "email_verified", "Value": "false"},
                    {"Name": "custom:invite_id", "Value": "abc123"},
                ],
                "MessageAction": "SUPPRESS",
            },
        )
        stubber.add_response(
            "admin_set_user_password",
            {},
            {
                "UserPoolId": POOL,
                "Username": "grace@example.com",
                "Password": PASSWORD,
                "Permanent": True,
            },
        )
        await directory.create_user("grace@example.com", "abc123", PASSWORD)
        stubber.assert_no_pending_responses()


async def test_existing_user(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_client_error("admin_create_user", "UsernameExistsException")
        with pytest.raises(UserExists):
            await directory.create_user("grace@example.com", "abc123", PASSWORD)


async def test_other_errors(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_client_error("admin_create_user", "TooManyRequestsException")
        with pytest.raises(DirectoryError, match="TooManyRequestsException"):
            await directory.create_user("grace@example.com", "abc123", PASSWORD)


@pytest.mark.parametrize(
    ("error", "raised"),
    [("InvalidPasswordException", InvalidPassword), ("TooManyRequestsException", DirectoryError)],
)
async def test_a_refused_password_deletes_the_new_user_again(
    directory: CognitoDirectory, error: str, raised: type[Exception]
) -> None:
    with stub(directory) as stubber:
        stubber.add_response("admin_create_user", {"User": {"Username": "grace@example.com"}})
        stubber.add_client_error("admin_set_user_password", error)
        stubber.add_response(
            "admin_delete_user", {}, {"UserPoolId": POOL, "Username": "grace@example.com"}
        )
        with pytest.raises(raised):
            await directory.create_user("grace@example.com", "abc123", "weak")
        stubber.assert_no_pending_responses()


async def test_disable_user_also_signs_them_out(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        expected = {"UserPoolId": POOL, "Username": "sub-grace"}
        stubber.add_response("admin_disable_user", {}, expected)
        stubber.add_response("admin_user_global_sign_out", {}, expected)
        await directory.disable_user("sub-grace")
        stubber.assert_no_pending_responses()


async def test_user_state_reads_enabled_and_admin_group(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_response(
            "admin_get_user",
            {"Username": "sub-grace", "Enabled": True},
            {"UserPoolId": POOL, "Username": "sub-grace"},
        )
        stubber.add_response(
            "admin_list_groups_for_user",
            {"Groups": [{"GroupName": "admin"}]},
            {"UserPoolId": POOL, "Username": "sub-grace"},
        )
        found = await directory.user_state("sub-grace", groups=True)
        assert found == DirectoryUser(enabled=True, admin=True)
        stubber.add_response("admin_get_user", {"Username": "sub-x", "Enabled": False})
        stubber.add_response("admin_list_groups_for_user", {"Groups": []})
        found = await directory.user_state("sub-x", groups=True)
        assert found == DirectoryUser(enabled=False, admin=False)


async def test_user_state_without_groups_makes_one_call(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:  # no admin_list_groups_for_user response: not called
        stubber.add_response("admin_get_user", {"Username": "sub-grace", "Enabled": True})
        found = await directory.user_state("sub-grace", groups=False)
        assert found == DirectoryUser(enabled=True, admin=None)
        stubber.assert_no_pending_responses()


async def test_user_state_for_a_deleted_user_and_on_errors(directory: CognitoDirectory) -> None:
    with stub(directory) as stubber:
        stubber.add_client_error("admin_get_user", "UserNotFoundException")
        assert await directory.user_state("gone", groups=True) is None
        stubber.add_client_error("admin_get_user", "TooManyRequestsException")
        with pytest.raises(DirectoryError):
            await directory.user_state("sub-grace", groups=True)
