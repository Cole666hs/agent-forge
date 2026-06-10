"""Unit tests for agentforge.adapters.email — EmailChannelAdapter.

Send path: aiosmtplib (mocked). Receive path: imaplib polling in
asyncio.to_thread (mocked). No real network — all I/O is patched.
"""

from __future__ import annotations

import email
import imaplib
import asyncio
from email.message import EmailMessage
from unittest import mock

import pytest

from agentforge.adapters.email import EmailChannelAdapter, EmailError
from agentforge.core.message import Message


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_name_is_email():
    adapter = EmailChannelAdapter(
        smtp_host="smtp.example.com", imap_host="imap.example.com",
        username="bot", password="secret", from_address="bot@example.com",
    )
    assert adapter.name == "email"


def test_missing_smtp_host_raises():
    with pytest.raises(ValueError, match="smtp_host"):
        EmailChannelAdapter(
            smtp_host="", imap_host="imap.example.com",
            username="bot", password="x", from_address="b@x",
        )


def test_missing_imap_host_raises():
    with pytest.raises(ValueError, match="imap_host"):
        EmailChannelAdapter(
            smtp_host="smtp.example.com", imap_host="",
            username="bot", password="x", from_address="b@x",
        )


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

async def test_send_uses_aiosmtplib():
    """send() calls aiosmtplib.send with the rendered email."""
    adapter = EmailChannelAdapter(
        smtp_host="smtp.example.com", imap_host="imap.example.com",
        username="bot", password="secret", from_address="bot@example.com",
    )
    msg = Message(from_="bot", to="alice@example.com", content="hello via email")

    with mock.patch("agentforge.adapters.email.aiosmtplib") as mock_smtp:
        mock_smtp.send = mock.AsyncMock()
        await adapter.send(msg)

    mock_smtp.send.assert_awaited_once()
    call_args = mock_smtp.send.await_args
    # aiosmtplib.send takes the message as a positional arg, not keyword
    sent_msg = call_args.args[0]
    assert "hello via email" in sent_msg.as_string()
    assert call_args.kwargs["hostname"] == "smtp.example.com"
    assert call_args.kwargs["username"] == "bot"
    assert call_args.kwargs["password"] == "secret"


async def test_send_uses_tls_when_configured():
    """When use_tls=True, aiosmtplib.send is called with use_tls=True."""
    adapter = EmailChannelAdapter(
        smtp_host="smtp.example.com", imap_host="imap.example.com",
        username="bot", password="secret", from_address="bot@example.com",
        use_tls=True,
    )
    msg = Message(from_="bot", to="alice@example.com", content="x")

    with mock.patch("agentforge.adapters.email.aiosmtplib") as mock_smtp:
        mock_smtp.send = mock.AsyncMock()
        await adapter.send(msg)

    assert mock_smtp.send.await_args.kwargs["use_tls"] is True


# ---------------------------------------------------------------------------
# Receive (IMAP polling)
# ---------------------------------------------------------------------------

def _make_mock_imap(messages: list):
    """Build a mock imaplib.IMAP4_SSL that yields `messages` on select+search."""
    imap = mock.Mock(spec=imaplib.IMAP4_SSL)
    imap.login = mock.Mock(return_value=("OK", [b"logged in"]))
    imap.logout = mock.Mock(return_value=("OK", [b"bye"]))
    imap.select = mock.Mock(return_value=("OK", [b"1"]))
    # search returns the IDs of our test messages
    msg_ids = [str(i + 1).encode() for i in range(len(messages))]
    imap.search = mock.Mock(return_value=("OK", [b" ".join(msg_ids) if msg_ids else b""]))
    # fetch returns (status, [header_bytes, body_bytes]) for each id
    def fake_fetch(_id, _what):
        idx = int(_id) - 1
        m = messages[idx]
        raw = m.as_bytes()
        # imaplib fetch returns [(metadata_bytes, raw_bytes), ...]
        return ("OK", [(b"1 (FLAGS (\\Seen) UID " + _id + b")", raw)])
    imap.fetch = mock.Mock(side_effect=fake_fetch)
    return imap


def _make_test_email(from_addr: str, to_addr: str, subject: str, body: str) -> EmailMessage:
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = to_addr
    m["Subject"] = subject
    m.set_content(body)
    return m


async def test_receive_yields_unread_messages():
    """receive() polls IMAP and yields unread messages as Message objects."""
    test_emails = [
        _make_test_email("alice@example.com", "bot@example.com", "Hi", "first email body"),
        _make_test_email("bob@example.com", "bot@example.com", "Hey", "second email body"),
    ]
    mock_imap = _make_mock_imap(test_emails)

    adapter = EmailChannelAdapter(
        smtp_host="smtp.example.com", imap_host="imap.example.com",
        username="bot", password="secret", from_address="bot@example.com",
        poll_interval=0,  # don't sleep in tests
    )
    # Patch IMAP4_SSL constructor so the adapter uses our mock
    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        await adapter.start()  # start() opens IMAP + spawns poll loop
        # Collect one message then break
        async def collect_one():
            async for m in adapter.receive():
                return m
        received = await asyncio.wait_for(collect_one(), timeout=2.0)
        await adapter.stop()

    assert received.from_ == "alice@example.com"
    assert "first email body" in received.content


async def test_stop_closes_imap_connection():
    """stop() calls imap.logout to close the connection cleanly."""
    adapter = EmailChannelAdapter(
        smtp_host="smtp.example.com", imap_host="imap.example.com",
        username="bot", password="secret", from_address="bot@example.com",
    )
    mock_imap = _make_mock_imap([])
    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        await adapter.start()
        await adapter.stop()

    mock_imap.logout.assert_called()
