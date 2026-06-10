"""T5 RED — RequestIdMiddleware test."""

import asyncio

from agentforge.observability.middleware import RequestIdMiddleware
from agentforge.observability.context import get_request_id, reset_request_id


async def _capture_app(scope, receive, send):
    """Minimal ASGI app that captures the current request_id and echoes it."""
    rid = get_request_id()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(rid).encode()})


def test_middleware_generates_request_id_when_missing():
    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["headers"] = msg.get("headers", [])

    app = RequestIdMiddleware(_capture_app)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    asyncio.run(app(scope, receive, send))
    headers = dict(captured["headers"])
    rid_header = headers.get(b"x-request-id")
    assert rid_header is not None
    assert rid_header.startswith(b"req_")


def test_middleware_echoes_inbound_request_id():
    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["headers"] = msg.get("headers", [])

    app = RequestIdMiddleware(_capture_app)
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"x-request-id", b"my-trace-123")],
    }
    asyncio.run(app(scope, receive, send))
    headers = dict(captured["headers"])
    assert headers.get(b"x-request-id") == b"my-trace-123"


def test_middleware_sets_contextvar_during_request():
    """The downstream app sees the request_id via get_request_id()."""
    seen = {}

    async def app(scope, receive, send):
        seen["rid"] = get_request_id()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        pass

    wrapped = RequestIdMiddleware(app)
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"x-request-id", b"trace-xyz")],
    }
    asyncio.run(wrapped(scope, receive, send))
    assert seen["rid"] == "trace-xyz"


def test_middleware_clears_contextvar_after_request():
    """After the request completes, the contextvar is back to None."""
    reset_request_id()  # baseline
    seen = {}

    async def app(scope, receive, send):
        seen["during"] = get_request_id()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        pass

    wrapped = RequestIdMiddleware(app)
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"x-request-id", b"trace-clear")],
    }
    asyncio.run(wrapped(scope, receive, send))
    assert seen["during"] == "trace-clear"
    assert get_request_id() is None  # cleaned up


def test_middleware_passes_through_non_http_scope():
    """WebSocket and lifespan scopes are forwarded unchanged."""
    seen = {"called": False}

    async def app(scope, receive, send):
        seen["called"] = True
        seen["scope_type"] = scope["type"]

    async def receive():
        return {}

    async def send(msg):
        pass

    wrapped = RequestIdMiddleware(app)
    scope = {"type": "websocket", "path": "/ws"}
    asyncio.run(wrapped(scope, receive, send))
    assert seen["called"] is True
    assert seen["scope_type"] == "websocket"
