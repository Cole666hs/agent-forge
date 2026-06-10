"""ASGI middleware for request-ID propagation.

Reads `X-Request-Id` from inbound request (or generates `req_<12hex>`),
sets it on the request_id contextvar for the duration of the request,
and echoes it on the response.
"""

from __future__ import annotations

import secrets
from typing import Awaitable, Callable

from agentforge.observability.context import reset_request_id, set_request_id


_ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]],
    Awaitable[None],
]


class RequestIdMiddleware:
    """Pure ASGI middleware — no FastAPI/Starlette dep.

    The middleware is transparent to non-HTTP scopes (websocket, lifespan)
    so it can wrap any ASGI stack including FastAPI.
    """

    HEADER_NAME = b"x-request-id"
    HEADER_TITLE = "X-Request-Id"  # for documentation only

    def __init__(self, app: _ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Read inbound request-id (or generate)
        rid: str | None = None
        for k, v in scope.get("headers", []):
            if k.lower() == self.HEADER_NAME:
                rid = v.decode("latin-1") if isinstance(v, bytes) else str(v)
                break
        if not rid:
            rid = f"req_{secrets.token_hex(6)}"

        # 2. Set contextvar so log lines from the request see it
        set_request_id(rid)
        try:
            # 3. Wrap send() to inject the header on http.response.start
            response_started = {"done": False}

            async def send_with_header(msg):
                if (
                    msg["type"] == "http.response.start"
                    and not response_started["done"]
                ):
                    response_started["done"] = True
                    headers = list(msg.get("headers", []))
                    if not any(k.lower() == self.HEADER_NAME for k, _ in headers):
                        headers.append((self.HEADER_NAME, rid.encode("latin-1")))
                    msg = {**msg, "headers": headers}
                await send(msg)

            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id()
