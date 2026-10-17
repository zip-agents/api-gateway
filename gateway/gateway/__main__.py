"""Entrypoint: `python -m gateway`."""

import sys

import uvicorn

from gateway.app import Gateway
from gateway.config import ConfigError, load_settings
from gateway.logs import configure_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        # stderr, not the JSON log: logging is not configured yet, and stdout is for logs.
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    config = uvicorn.Config(
        Gateway(settings),
        host="0.0.0.0",  # noqa: S104 - in a container, behind the load balancer
        port=settings.port,
        log_config=None,
        access_log=False,
        # Upstream responses carry their own Server and Date headers; don't add second ones.
        server_header=False,
        date_header=False,
        # uvicorn stops accepting on SIGTERM, then waits this long for in-flight streams.
        timeout_graceful_shutdown=settings.shutdown_grace_seconds,
        # Behind an ALB the client address arrives in X-Forwarded-For; the proxy forwards
        # that header as is and appends the peer, so uvicorn need not rewrite it.
        proxy_headers=False,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
