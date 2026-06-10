"""Structured logging for agentforge.

Opt-in: call `configure_logging()` once at process start. Library code
uses `logger = logging.getLogger(__name__)` and is silent until configured.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional, TextIO

from agentforge.observability.context import get_request_id


_STD_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Format LogRecord as a single-line JSON object.

    Includes ts (ISO 8601 UTC), level, logger, msg, and any extra fields
    passed by the caller or resolved from context (request_id).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra fields (anything not in the std LogRecord set)
        for k, v in record.__dict__.items():
            if k not in _STD_LOGRECORD_ATTRS and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
        # Attach request_id from contextvar if present
        rid = get_request_id()
        if rid is not None:
            payload.setdefault("request_id", rid)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    fmt: Optional[str] = None,
    level: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """Configure the root agentforge logger (and all sub-loggers).

    fmt: "json" (one JSON object per line) or "text" (default Python format).
         Falls back to $AGENTFORGE_LOG_FORMAT, then "text".
    level: "DEBUG" | "INFO" | "WARNING" | "ERROR".
           Falls back to $AGENTFORGE_LOG_LEVEL, then "INFO".
    stream: where to write (default: stderr).

    Idempotent: calling twice replaces the handler instead of stacking.
    """
    fmt = (fmt if fmt is not None else os.environ.get("AGENTFORGE_LOG_FORMAT", "text")).lower()
    level = level if level is not None else os.environ.get("AGENTFORGE_LOG_LEVEL", "INFO")
    stream = stream if stream is not None else sys.stderr

    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    elif fmt == "text":
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    else:
        raise ValueError(f"unknown log format: {fmt!r}; want 'json' or 'text'")

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger("agentforge")
    root.handlers = [handler]  # replace, don't stack
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
