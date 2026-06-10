"""Per-request context (request_id, tenant_id, etc.) via contextvars.

Contextvars are safe across asyncio tasks and threads (each task gets
its own copy). The JSON formatter reads request_id, the ASGI middleware
sets it, the logging config doesn't touch it.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_request_id_var: ContextVar[Optional[str]] = ContextVar(
    "agentforge_request_id", default=None
)


def set_request_id(request_id: str) -> None:
    """Set the current request_id. Called by RequestIdMiddleware on entry."""
    _request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    """Return the current request_id, or None if not in a request context."""
    return _request_id_var.get()


def reset_request_id() -> None:
    """Clear the current request_id. Called by RequestIdMiddleware on exit."""
    _request_id_var.set(None)
