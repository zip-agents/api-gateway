"""JSON-lines logging to stdout, matching the zip services' log shape.

Every line has `timestamp` (RFC 3339, UTC), `level`, `message`, and `request_id` while a
request is being handled. Extra fields are passed with `logger.info(..., extra={"fields":
{...}})`. uvicorn's own loggers are routed through the same handler, so nothing but JSON
reaches stdout.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        request_id = request_id_var.get()
        if request_id is not None:
            line["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            line.update(fields)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers unless told otherwise; send everything to root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # The gateway writes its own access line per request (see proxy.py).
    logging.getLogger("uvicorn.access").disabled = True
