import pytest

from gateway.config import ConfigError, load_settings, parse_api_keys, parse_upstreams
from tests.support import API_KEY, SESSION_SECRET, make_settings


def test_defaults() -> None:
    settings = make_settings()
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.idle_timeout_seconds == 60
    assert settings.connect_timeout_seconds == 5
    assert settings.session_ttl_seconds == 43200
    assert settings.shutdown_grace_seconds == 25
    assert settings.api_keys == {"tester": API_KEY}


def test_cookie_secure_defaults_to_true() -> None:
    settings = load_settings(
        {
            "GATEWAY_UPSTREAMS": "claude=http://claude:8000",
            "GATEWAY_API_KEYS": f"tester:{API_KEY}",
            "GATEWAY_SESSION_SECRET": SESSION_SECRET,
        }
    )
    assert settings.cookie_secure is True


def test_parse_upstreams() -> None:
    upstreams = parse_upstreams(
        "claude=http://claude:8000, grok=http://grok, dsh=http://10.0.0.5:9000/"
    )
    assert upstreams["claude"].authority == "claude:8000"
    assert upstreams["grok"].authority == "grok:80"
    assert upstreams["dsh"].host == "10.0.0.5"
    assert list(upstreams) == ["claude", "grok", "dsh"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "claude",
        "Claude=http://claude:8000",
        "claude=https://claude:8000",
        "claude=http://claude:8000/prefix",
        "claude=http://claude:notaport",
        "claude=http://a:1,claude=http://b:1",
        "docs=http://docs:8000",
        "health=http://health:8000",
    ],
)
def test_invalid_upstreams(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_upstreams(value)


def test_parse_api_keys() -> None:
    keys = parse_api_keys(f"alice:{API_KEY}, bob:{API_KEY[::-1]}")
    assert keys == {"alice": API_KEY, "bob": API_KEY[::-1]}


@pytest.mark.parametrize(
    "value",
    ["", "no-separator", "alice:short", f"alice:{API_KEY},alice:{API_KEY}", f"bad name:{API_KEY}"],
)
def test_invalid_api_keys(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_api_keys(value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "abc"),
        ("LOG_LEVEL", "TRACE"),
        ("GATEWAY_IDLE_TIMEOUT_SECONDS", "0"),
        ("GATEWAY_COOKIE_SECURE", "maybe"),
        ("GATEWAY_SESSION_SECRET", "too-short"),
    ],
)
def test_invalid_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings(**{name: value})


@pytest.mark.parametrize(
    "name", ["GATEWAY_UPSTREAMS", "GATEWAY_API_KEYS", "GATEWAY_SESSION_SECRET"]
)
def test_required(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings(**{name: ""})


def test_database_defaults_to_sqlite_in_memory() -> None:
    assert make_settings().database_url == "sqlite+aiosqlite:///:memory:"


def test_database_url_from_aws_parts_escapes_the_password() -> None:
    settings = make_settings(
        GATEWAY_DB_HOST="db.cluster.example",
        GATEWAY_DB_USER="gateway",
        GATEWAY_DB_PASSWORD="p@ss:w/rd",
    )
    assert settings.database_url == (
        "postgresql+asyncpg://gateway:p%40ss%3Aw%2Frd@db.cluster.example:5432/gateway?ssl=verify-full"
    )
    plain = make_settings(
        GATEWAY_DB_HOST="db", GATEWAY_DB_USER="u", GATEWAY_DB_PASSWORD="p", GATEWAY_DB_SSL="disable"
    )
    assert plain.database_url.endswith("?ssl=disable")
    with pytest.raises(ConfigError, match="GATEWAY_DB_SSL"):
        make_settings(
            GATEWAY_DB_HOST="db", GATEWAY_DB_USER="u", GATEWAY_DB_PASSWORD="p", GATEWAY_DB_SSL="on"
        )
    with pytest.raises(ConfigError, match="GATEWAY_DB_PASSWORD"):
        make_settings(GATEWAY_DB_HOST="db", GATEWAY_DB_USER="gateway")


def test_database_url_must_use_a_supported_driver() -> None:
    url = "postgresql+asyncpg://u:p@localhost/gateway"
    assert make_settings(GATEWAY_DATABASE_URL=url).database_url == url
    with pytest.raises(ConfigError, match="GATEWAY_DATABASE_URL"):
        make_settings(GATEWAY_DATABASE_URL="mysql://u:p@localhost/gateway")
