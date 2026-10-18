"""Configuration from environment variables, validated once at startup.

Invalid configuration raises `ConfigError`; `__main__` turns that into a clear message and
a non-zero exit, the same behaviour the zip services have.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy.engine import URL

from gateway.accounts.oidc import CognitoSettings

_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_KEY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Short keys are guessable; 32 characters is what `openssl rand -hex 16` produces.
MIN_API_KEY_LENGTH = 32
MIN_SESSION_SECRET_LENGTH = 32


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Upstream:
    name: str
    host: str
    port: int

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Settings:
    port: int
    log_level: str
    upstreams: Mapping[str, Upstream]
    # key name -> secret. The name identifies the caller in logs; the secret never is logged.
    api_keys: Mapping[str, str]
    session_secret: str
    session_ttl_seconds: int
    cookie_secure: bool
    connect_timeout_seconds: float
    idle_timeout_seconds: float
    shutdown_grace_seconds: int
    browser_jobs_max: int
    # Accounts data (user API keys, ...): Postgres, or SQLite in memory for tests and
    # throwaway runs. Contains the password: never log it.
    database_url: str = "sqlite+aiosqlite:///:memory:"
    # Sign-in with Cognito (None: only API-key sign-in), and the public URL Cognito
    # redirects back to (exactly as registered on the app client).
    cognito: CognitoSettings | None = None
    public_url: str | None = None
    allow_key_login: bool = True
    # Bytes (uploads plus archives) each user may move per UTC day; 0 for no limit.
    user_daily_quota_bytes: int = 10 * 1024 * 1024 * 1024


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    log_level = env.get("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    session_secret = _required(env, "GATEWAY_SESSION_SECRET")
    if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
        raise ConfigError(
            f"GATEWAY_SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters"
        )
    cognito = _cognito(env)
    public_url = _public_url(env, required=cognito is not None)
    return Settings(
        port=_int(env, "PORT", 8000, minimum=1, maximum=65535),
        log_level=log_level,
        upstreams=parse_upstreams(_required(env, "GATEWAY_UPSTREAMS")),
        api_keys=parse_api_keys(_required(env, "GATEWAY_API_KEYS")),
        session_secret=session_secret,
        session_ttl_seconds=_int(env, "GATEWAY_SESSION_TTL_SECONDS", 43200, minimum=60),
        cookie_secure=_bool(env, "GATEWAY_COOKIE_SECURE", True),
        connect_timeout_seconds=_int(env, "GATEWAY_CONNECT_TIMEOUT_SECONDS", 5, minimum=1),
        idle_timeout_seconds=_int(env, "GATEWAY_IDLE_TIMEOUT_SECONDS", 60, minimum=1),
        shutdown_grace_seconds=_int(env, "SHUTDOWN_GRACE_SECONDS", 25, minimum=0),
        browser_jobs_max=_int(env, "GATEWAY_BROWSER_JOBS_MAX", 16, minimum=1, maximum=256),
        database_url=_database_url(env),
        cognito=cognito,
        public_url=public_url,
        allow_key_login=_bool(env, "GATEWAY_ALLOW_KEY_LOGIN", True),
        user_daily_quota_bytes=_int(
            env, "GATEWAY_USER_DAILY_QUOTA_MIB", 10 * 1024, minimum=0, maximum=10 * 1024 * 1024
        )
        * 1024
        * 1024,
    )


_COGNITO_VARIABLES = (
    "GATEWAY_COGNITO_DOMAIN",
    "GATEWAY_COGNITO_CLIENT_ID",
    "GATEWAY_COGNITO_CLIENT_SECRET",
    "GATEWAY_COGNITO_USER_POOL_ID",
)
_POOL_ID = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d_[A-Za-z0-9]+$")
# The local fake-cognito's hosted login, reached by the browser over plain HTTP.
_LOCAL_HTTP = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?$")


def _cognito(env: Mapping[str, str]) -> CognitoSettings | None:
    values = {name: env.get(name, "").strip() for name in _COGNITO_VARIABLES}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"Cognito sign-in also needs {', '.join(missing)}")
    domain = values["GATEWAY_COGNITO_DOMAIN"].rstrip("/")
    if not (domain.startswith("https://") or _LOCAL_HTTP.fullmatch(domain)):
        raise ConfigError(
            "GATEWAY_COGNITO_DOMAIN must be the https:// hosted-login domain "
            "(or http://localhost:<port> for the local fake-cognito)"
        )
    backchannel = env.get("GATEWAY_COGNITO_BACKCHANNEL_URL", "").strip().rstrip("/") or None
    if backchannel is not None:
        parts = urlsplit(backchannel)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path:
            raise ConfigError("GATEWAY_COGNITO_BACKCHANNEL_URL must be http(s)://host[:port]")
        if not _LOCAL_HTTP.fullmatch(domain):
            # Tokens are verified with keys fetched from here: on AWS that must be Cognito.
            raise ConfigError(
                "GATEWAY_COGNITO_BACKCHANNEL_URL is only for the local fake-cognito: "
                "GATEWAY_COGNITO_DOMAIN must then be http://localhost:<port>"
            )
    if not _POOL_ID.fullmatch(values["GATEWAY_COGNITO_USER_POOL_ID"]):
        raise ConfigError("GATEWAY_COGNITO_USER_POOL_ID must look like eu-west-2_AbC123")
    return CognitoSettings(
        domain=domain,
        client_id=values["GATEWAY_COGNITO_CLIENT_ID"],
        client_secret=values["GATEWAY_COGNITO_CLIENT_SECRET"],
        user_pool_id=values["GATEWAY_COGNITO_USER_POOL_ID"],
        backchannel_url=backchannel,
    )


def _public_url(env: Mapping[str, str], *, required: bool) -> str | None:
    value = env.get("GATEWAY_PUBLIC_URL", "").strip().rstrip("/")
    if not value:
        if required:
            raise ConfigError("GATEWAY_PUBLIC_URL is required with Cognito sign-in")
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path:
        raise ConfigError("GATEWAY_PUBLIC_URL must be http(s)://host[:port], with no path")
    return value


def _database_url(env: Mapping[str, str]) -> str:
    """GATEWAY_DATABASE_URL; or, on AWS, Postgres from parts (the password comes from
    Secrets Manager); or SQLite in memory."""
    host = env.get("GATEWAY_DB_HOST", "").strip()
    if host:
        user = _required(env, "GATEWAY_DB_USER")
        password = _required(env, "GATEWAY_DB_PASSWORD")
        url = URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password,
            host=host,
            port=_int(env, "GATEWAY_DB_PORT", 5432, minimum=1, maximum=65535),
            database=env.get("GATEWAY_DB_NAME", "gateway").strip() or "gateway",
            # TLS to Aurora, verified against the RDS CAs (accounts/store.py, certs/).
            query={
                "ssl": _choice(
                    env, "GATEWAY_DB_SSL", "verify-full", ("verify-full", "require", "disable")
                )
            },
        )
        return url.render_as_string(hide_password=False)
    url_text = env.get("GATEWAY_DATABASE_URL", "").strip()
    if not url_text:
        return "sqlite+aiosqlite:///:memory:"
    if not url_text.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
        raise ConfigError(
            "GATEWAY_DATABASE_URL must start with postgresql+asyncpg:// or sqlite+aiosqlite://"
        )
    return url_text


def parse_upstreams(value: str) -> dict[str, Upstream]:
    """Parse `name=http://host:port,name2=http://host2:port`."""
    upstreams: dict[str, Upstream] = {}
    for item in _items(value):
        name, sep, url = item.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not _SERVICE_NAME.fullmatch(name):
            raise ConfigError(f"GATEWAY_UPSTREAMS: invalid entry {item!r}; expected name=url")
        if name in upstreams:
            raise ConfigError(f"GATEWAY_UPSTREAMS: duplicate service {name!r}")
        if name in RESERVED_PREFIXES:
            raise ConfigError(f"GATEWAY_UPSTREAMS: {name!r} is reserved by the gateway")
        parts = urlsplit(url)
        # Plain HTTP only: upstreams are reached over the private network (see README).
        if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/"):
            raise ConfigError(
                f"GATEWAY_UPSTREAMS: {name}: expected http://host[:port], got {url!r}"
            )
        try:
            port = parts.port or 80
        except ValueError as exc:
            raise ConfigError(f"GATEWAY_UPSTREAMS: {name}: invalid port in {url!r}") from exc
        upstreams[name] = Upstream(name=name, host=parts.hostname, port=port)
    if not upstreams:
        raise ConfigError("GATEWAY_UPSTREAMS must list at least one service")
    return upstreams


def parse_api_keys(value: str) -> dict[str, str]:
    """Parse `name:secret,name2:secret2`."""
    keys: dict[str, str] = {}
    for item in _items(value):
        name, sep, secret = item.partition(":")
        name, secret = name.strip(), secret.strip()
        if not sep or not _KEY_NAME.fullmatch(name):
            raise ConfigError("GATEWAY_API_KEYS: each entry must be name:secret")
        if name in keys:
            raise ConfigError(f"GATEWAY_API_KEYS: duplicate key name {name!r}")
        if len(secret) < MIN_API_KEY_LENGTH:
            raise ConfigError(
                f"GATEWAY_API_KEYS: key {name!r} must be at least {MIN_API_KEY_LENGTH} characters"
            )
        if not secret.isascii() or not secret.isprintable() or " " in secret:
            raise ConfigError(f"GATEWAY_API_KEYS: key {name!r} must be printable ASCII")
        keys[name] = secret
    if not keys:
        raise ConfigError("GATEWAY_API_KEYS must contain at least one key")
    return keys


# First path segments the gateway serves itself; a service cannot be named after one.
RESERVED_PREFIXES = frozenset({"docs", "health", "static", "api", "account", "zip"})


def _items(value: str) -> list[str]:
    return [item for item in (part.strip() for part in value.split(",")) if item]


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ConfigError(f"{name} must be {bounds}, got {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes"):
        return True
    if lowered in ("0", "false", "no"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = env.get(name, "").strip().lower() or default
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value
