import pytest

from gateway.auth import KeyRing, Session, SessionSigner, bearer_token
from tests.support import API_KEY, SESSION_SECRET

OTHER_KEY = "z" * 40


@pytest.fixture
def keyring() -> KeyRing:
    return KeyRing({"alice": API_KEY, "bob": OTHER_KEY})


def test_identify(keyring: KeyRing) -> None:
    assert keyring.identify(API_KEY) == "alice"
    assert keyring.identify(OTHER_KEY) == "bob"
    assert keyring.identify(API_KEY + "x") is None
    assert keyring.identify("") is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"Bearer {API_KEY}", API_KEY),
        (f"bearer   {API_KEY}  ", API_KEY),
        (f"Basic {API_KEY}", None),
        ("Bearer", None),
        ("Bearer ", None),
        (API_KEY, None),
        (None, None),
    ],
)
def test_bearer_token(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected


def test_session_round_trip(keyring: KeyRing) -> None:
    signer = SessionSigner(SESSION_SECRET, 60, keyring)
    token = signer.issue("alice", now=1000)
    assert signer.verify(token, now=1059) == Session("op:alice", "alice")
    assert signer.verify(token, now=1060) is None  # expired


def test_session_rejects_tampering(keyring: KeyRing) -> None:
    signer = SessionSigner(SESSION_SECRET, 60, keyring)
    token = signer.issue("alice", now=1000)
    payload, signature = token.split(".")
    forged = signer.issue("bob", now=1000).split(".")[0]
    assert signer.verify(f"{forged}.{signature}", now=1001) is None
    assert signer.verify(f"{payload}.{signature[:-2]}AA", now=1001) is None
    assert signer.verify("garbage", now=1001) is None
    assert signer.verify("a.b.c", now=1001) is None
    assert signer.verify(None) is None


def test_session_from_another_secret_is_rejected(keyring: KeyRing) -> None:
    token = SessionSigner("x" * 32, 60, keyring).issue("alice", now=1000)
    assert SessionSigner(SESSION_SECRET, 60, keyring).verify(token, now=1001) is None


def test_removing_a_key_ends_its_sessions(keyring: KeyRing) -> None:
    token = SessionSigner(SESSION_SECRET, 60, keyring).issue("bob", now=1000)
    without_bob = KeyRing({"alice": API_KEY})
    assert SessionSigner(SESSION_SECRET, 60, without_bob).verify(token, now=1001) is None


def test_user_session_round_trip(keyring: KeyRing) -> None:
    signer = SessionSigner(SESSION_SECRET, 60, keyring)
    session = Session("user:0b1c-sub", "ada@example.com", admin=True)
    verified = signer.verify(signer.issue(session, now=1000), now=1001)
    assert verified == session
    assert verified is not None
    assert verified.user_sub == "0b1c-sub"
    assert verified.operator_key is None


def test_session_with_an_unknown_kind_is_rejected(keyring: KeyRing) -> None:
    signer = SessionSigner(SESSION_SECRET, 60, keyring)
    token = signer.issue(Session("root:me", "me"), now=1000)
    assert signer.verify(token, now=1001) is None


def test_csrf_token_is_bound_to_the_session(keyring: KeyRing) -> None:
    signer = SessionSigner(SESSION_SECRET, 60, keyring)
    first, second = signer.issue("alice", now=1000), signer.issue("alice", now=1001)
    assert signer.check_csrf(first, signer.csrf_token(first))
    assert not signer.check_csrf(second, signer.csrf_token(first))
    assert not signer.check_csrf(first, None)
    assert not signer.check_csrf(first, "é")  # non-ASCII input is refused, not an error
