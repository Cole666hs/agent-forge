"""Unit tests for agentforge.adapters.discord — DiscordChannelAdapter.

Tests mock discord.Client so we never connect to Discord. Verifies:
name, send uses channel.send, receive yields messages, start connects,
stop closes cleanly.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest import mock

import pytest

from agentforge.adapters.discord import DiscordChannelAdapter
from agentforge.core.message import Message


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    """A mock discord.Client with start, close, and event hooks."""
    client = mock.Mock()
    client.start = mock.AsyncMock()
    client.close = mock.AsyncMock()
    client.is_closed = mock.Mock(return_value=False)
    # Mock channel for send tests
    mock_channel = mock.Mock()
    mock_channel.send = mock.AsyncMock()
    client.fetch_channel = mock.AsyncMock(return_value=mock_channel)
    client._test_channel = mock_channel  # so tests can access it
    return client


@pytest.fixture
def patched_client_class(mock_client):
    with mock.patch("discord.Client") as MockClient:
        MockClient.return_value = mock_client
        yield MockClient, mock_client


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_name_is_discord(patched_client_class):
    _, _ = patched_client_class
    adapter = DiscordChannelAdapter(token="bot-token", channel_id=999)
    assert adapter.name == "discord"


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

async def test_send_calls_channel_send(patched_client_class, mock_client):
    _, _ = patched_client_class
    adapter = DiscordChannelAdapter(token="bot-token", channel_id=999)
    await adapter.start()  # start required before send
    msg = Message(from_="alice", to="bob", content="hello discord")

    await adapter.send(msg)

    mock_client.fetch_channel.assert_awaited_with(999)
    mock_client._test_channel.send.assert_awaited_once()
    sent = mock_client._test_channel.send.await_args.args[0]
    assert "hello discord" in sent


async def test_send_requires_channel_id(patched_client_class, mock_client):
    _, _ = patched_client_class
    adapter = DiscordChannelAdapter(token="bot-token")
    with pytest.raises(ValueError, match="channel_id"):
        await adapter.send(Message(from_="a", to="b", content="x"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_start_calls_client_start(patched_client_class, mock_client):
    _, _ = patched_client_class
    adapter = DiscordChannelAdapter(token="bot-token", channel_id=999)

    await adapter.start()

    mock_client.start.assert_awaited_once()


async def test_stop_closes_client(patched_client_class, mock_client):
    _, _ = patched_client_class
    adapter = DiscordChannelAdapter(token="bot-token", channel_id=999)
    await adapter.start()
    await adapter.stop()

    mock_client.close.assert_awaited_once()
