"""One JSON object per log line, for production.

Log aggregators (CloudWatch, Loki, Datadog) index structured fields and treat a
plain line as an opaque blob. Writing JSON here means you can query
`level:ERROR AND logger:api.views.orders` instead of grepping.

Deliberately dependency-free: `python-json-logger` would do this too, but this
is thirty lines and one less package to keep patched.
"""

from __future__ import annotations

import json
import logging
import traceback

# Attributes LogRecord always carries. Anything on a record that is *not* in
# here was attached by the caller via `logger.info(msg, extra={...})`, so it is
# application context worth emitting.
_STANDARD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value

        # `default=str` so a stray Decimal, datetime or model instance in
        # `extra` degrades to its string form instead of raising inside the
        # logger — an exception thrown while logging an exception is the worst
        # possible failure mode.
        return json.dumps(payload, default=str)
