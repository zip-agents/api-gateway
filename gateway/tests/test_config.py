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


COGNITO = {
    "GATEWAY_COGNITO_DOMAIN": "https://zip.auth.eu-west-2.amazoncognito.com",
    "GATEWAY_COGNITO_CLIENT_ID": "client",
    "GATEWAY_COGNITO_CLIENT_SECRET": "secret",
    "GATEWAY_COGNITO_USER_POOL_ID": "eu-west-2_AbC123",
    "GATEWAY_PUBLIC_URL": "https://zip.example.com",
}


def test_cognito_settings_on_aws() -> None:
    cognito = make_settings(**COGNITO).cognito
    assert cognito is not None
    assert cognito.region == "eu-west-2"
    assert cognito.token_url == "https://zip.auth.eu-west-2.amazoncognito.com/oauth2/token"
    assert cognito.jwks_url == (
        "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_AbC123/.well-known/jwks.json"
    )
    with pytest.raises(ConfigError, match="GATEWAY_COGNITO_CLIENT_SECRET"):
        make_settings(**{**COGNITO, "GATEWAY_COGNITO_CLIENT_SECRET": ""})
    with pytest.raises(ConfigError, match="GATEWAY_PUBLIC_URL"):
        make_settings(**{**COGNITO, "GATEWAY_PUBLIC_URL": ""})


def test_cognito_settings_for_the_local_fake() -> None:
    cognito = make_settings(
        **{
            **COGNITO,
            "GATEWAY_COGNITO_DOMAIN": "http://localhost:9229",
            "GATEWAY_COGNITO_BACKCHANNEL_URL": "http://fake-cognito:9229",
        }
    ).cognito
    assert cognito is not None
    assert cognito.token_url == "http://fake-cognito:9229/oauth2/token"
    assert cognito.jwks_url == "http://fake-cognito:9229/eu-west-2_AbC123/.well-known/jwks.json"
    # The issuer stays Cognito's: the gateway verifies the same claim in both places.
    assert cognito.issuer == "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_AbC123"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GATEWAY_COGNITO_DOMAIN", "http://zip.example.com"),  # plain HTTP only on localhost
        ("GATEWAY_COGNITO_BACKCHANNEL_URL", "ftp://fake-cognito"),
        ("GATEWAY_COGNITO_BACKCHANNEL_URL", "http://fake-cognito:9229/path"),
    ],
)
def test_cognito_urls_are_checked(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings(**{**COGNITO, name: value})
