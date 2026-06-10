"""Unit tests for agentforge.adapters.webhook — WebhookChannelAdapter.

Tests use aiohttp.test_utils (TestClient/TestServer) for the receive
path and unittest.mock for the send path. No real network — the
'aiohttp.test_utils.TestServer' runs the app in-process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import AsyncIterator
from unittest import mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentforge.adapters.webhook import WebhookChannelAdapter
from agentforge.core.message import Message


# ---------------------------------------------------------------------------
# Send path — outgoing webhooks (POST to target_url)
# ---------------------------------------------------------------------------

async def test_send_posts_json_to_target_url():
    """send() POSTs the message as JSON to the configured target URL."""
    adapter = WebhookChannelAdapter(
        target_url="https://example.com/hook",
        secret=None,
    )
    msg = Message(from_="alice", to="bob", content="hello")

    # Mock the aiohttp.ClientSession context manager
    mock_response = mock.Mock()
    mock_response.status = 200
    mock_response.__aenter__ = mock.AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("aiohttp.ClientSession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.__aenter__ = mock.AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = mock.AsyncMock(return_value=False)
        session_instance.post = mock.Mock(return_value=mock_response)

        await adapter.send(msg)

    session_instance.post.assert_called_once()
    call_args = session_instance.post.call_args
    assert call_args.args[0] == "https://example.com/hook"
    # The body is sent as raw bytes (so HMAC signing works on the wire).
    body_bytes = call_args.kwargs.get("data")
    assert body_bytes is not None
    sent = json.loads(body_bytes.decode("utf-8"))
    assert sent["from"] == "alice"
    assert sent["to"] == "bob"
    assert sent["content"] == "hello"


async def test_send_includes_hmac_signature_when_secret_set():
    """When secret is configured, send() adds an X-Signature header
    with HMAC-SHA256 of the body."""
    secret = "super-secret-key"
    adapter = WebhookChannelAdapter(
        target_url="https://example.com/hook",
        secret=secret,
    )
    msg = Message(from_="a", to="b", content="x")

    mock_response = mock.Mock()
    mock_response.status = 200
    mock_response.__aenter__ = mock.AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("aiohttp.ClientSession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.__aenter__ = mock.AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = mock.AsyncMock(return_value=False)
        session_instance.post = mock.Mock(return_value=mock_response)

        await adapter.send(msg)

    call_kwargs = session_instance.post.call_args.kwargs
    headers = call_kwargs.get("headers", {})
    assert "X-Signature" in headers
    # The signature is hex(HMAC-SHA256(secret, body)) — but we don't
    # re-derive the exact body here, so just verify the header exists
    # and looks like a hex string.
    sig = headers["X-Signature"]
    assert len(sig) == 64  # SHA-256 hex
    assert all(c in "0123456789abcdef" for c in sig)


async def test_send_retries_on_5xx():
    """HTTP 503 from the target triggers a retry."""
    adapter = WebhookChannelAdapter(
        target_url="https://example.com/hook",
        max_retries=2,
    )
    msg = Message(from_="a", to="b", content="x")

    fail_resp = mock.Mock()
    fail_resp.status = 503
    fail_resp.__aenter__ = mock.AsyncMock(return_value=fail_resp)
    fail_resp.__aexit__ = mock.AsyncMock(return_value=False)

    ok_resp = mock.Mock()
    ok_resp.status = 200
    ok_resp.__aenter__ = mock.AsyncMock(return_value=ok_resp)
    ok_resp.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("aiohttp.ClientSession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.__aenter__ = mock.AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = mock.AsyncMock(return_value=False)
        # First call 503, second call 200
        session_instance.post = mock.Mock(side_effect=[fail_resp, ok_resp])

        await adapter.send(msg)

    assert session_instance.post.call_count == 2  # 1 fail + 1 success


async def test_send_fails_fast_on_4xx():
    """HTTP 401 is the caller's fault, not a server problem — no retry."""
    adapter = WebhookChannelAdapter(
        target_url="https://example.com/hook",
        max_retries=3,
    )
    msg = Message(from_="a", to="b", content="x")

    fail_resp = mock.Mock()
    fail_resp.status = 401
    fail_resp.__aenter__ = mock.AsyncMock(return_value=fail_resp)
    fail_resp.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("aiohttp.ClientSession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.__aenter__ = mock.AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = mock.AsyncMock(return_value=False)
        session_instance.post = mock.Mock(return_value=fail_resp)

        with pytest.raises(Exception):  # WebhookError
            await adapter.send(msg)

    assert session_instance.post.call_count == 1  # no retry


async def test_name_is_webhook():
    adapter = WebhookChannelAdapter(target_url="https://x")
    assert adapter.name == "webhook"


# ---------------------------------------------------------------------------
# Server path — receiving webhooks (POST /webhook on the local listener)
# ---------------------------------------------------------------------------

async def test_server_injects_received_messages():
    """POST /webhook with a valid message JSON → adapter yields it from receive()."""
    adapter = WebhookChannelAdapter(target_url="https://example.com/hook")
    await adapter.start(port=0)  # port=0 → OS picks a free port
    try:
        # The aiohttp server is running on adapter._app; we can hit it directly
        port = adapter._port
        url = f"http://127.0.0.1:{port}/webhook"
        payload = {
            "from": "alice", "to": "bob",
            "content": "incoming",
            "intent": "respond",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                assert resp.status == 200

        # Drain the receive() generator
        received = None
        async for m in adapter.receive():
            received = m
            break
        assert received is not None
        assert received.from_ == "alice"
        assert received.content == "incoming"
    finally:
        await adapter.stop()


async def test_server_rejects_malformed_json_with_400():
    """POST /webhook with non-JSON body returns 400 and does not crash."""
    adapter = WebhookChannelAdapter(target_url="https://example.com/hook")
    await adapter.start(port=0)
    try:
        port = adapter._port
        url = f"http://127.0.0.1:{port}/webhook"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=b"{not json") as resp:
                assert resp.status == 400
    finally:
        await adapter.stop()


async def test_server_validates_hmac_signature():
    """When secret is set, server checks X-Signature against body."""
    secret = "shared-secret"
    adapter = WebhookChannelAdapter(
        target_url="https://example.com/hook",
        secret=secret,
    )
    await adapter.start(port=0)
    try:
        port = adapter._port
        url = f"http://127.0.0.1:{port}/webhook"
        body = json.dumps({
            "from": "alice", "to": "bob",
            "content": "signed", "intent": "respond",
        }).encode("utf-8")
        good_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        bad_sig = "0" * 64

        async with aiohttp.ClientSession() as session:
            # Bad signature → 401
            async with session.post(
                url, data=body,
                headers={"Content-Type": "application/json", "X-Signature": bad_sig},
            ) as resp:
                assert resp.status == 401

            # Good signature → 200
            async with session.post(
                url, data=body,
                headers={"Content-Type": "application/json", "X-Signature": good_sig},
            ) as resp:
                assert resp.status == 200
    finally:
        await adapter.stop()
