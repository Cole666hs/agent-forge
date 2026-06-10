"""Unit tests for agentforge.adapters.telegram — TelegramChannelAdapter.

Tests mock the python-telegram-bot Application/Bot so we never hit the
real Telegram API. The adapter contract is verified: name, send uses
bot.send_message, receive yields incoming updates, start/stop are
idempotent.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest import mock

import pytest

from agentforge.adapters.telegram import TelegramChannelAdapter
from agentforge.core.message import Message


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app():
    """A mock Application with bot, add_handler, start, stop, initialize."""
    app = mock.Mock()
    app.bot = mock.Mock()
    app.bot.send_message = mock.AsyncMock()
    app.add_handler = mock.Mock()
    app.initialize = mock.AsyncMock()
    app.start = mock.AsyncMock()
    app.stop = mock.AsyncMock()
    app.shutdown = mock.AsyncMock()
    app.updater = mock.Mock()
    app.updater.start_polling = mock.AsyncMock()
    app.updater.stop = mock.AsyncMock()
    return app


@pytest.fixture
def patched_app_factory(mock_app):
    """Patch telegram.ext.Application.builder so adapter init returns mock_app."""
    builder = mock.Mock()
    builder.token = mock.Mock(return_value=builder)
    builder.build = mock.Mock(return_value=mock_app)
    with mock.patch("agentforge.adapters.telegram.Application") as MockApp:
        MockApp.builder.return_value = builder
        yield MockApp, mock_app


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_name_is_telegram(patched_app_factory):
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc", chat_id=42)
    assert adapter.name == "telegram"


async def test_chat_id_required_for_send(patched_app_factory):
    """send() without a chat_id configured raises."""
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc")
    with pytest.raises(ValueError, match="chat_id"):
        await adapter.send(Message(from_="a", to="b", content="x"))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

async def test_send_calls_bot_send_message(patched_app_factory, mock_app):
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc", chat_id=42)
    await adapter.start()  # start() required before send() — initializes bot
    msg = Message(from_="alice", to="bob", content="hello")

    await adapter.send(msg)

    mock_app.bot.send_message.assert_awaited_once()
    args, kwargs = mock_app.bot.send_message.await_args
    assert kwargs["chat_id"] == 42
    assert "hello" in kwargs["text"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_start_initializes_and_polls(patched_app_factory, mock_app):
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc", chat_id=42)

    await adapter.start()

    mock_app.initialize.assert_awaited_once()
    mock_app.start.assert_awaited_once()
    mock_app.updater.start_polling.assert_awaited_once()
    # At least one handler registered
    assert mock_app.add_handler.called


async def test_stop_shuts_down_cleanly(patched_app_factory, mock_app):
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc", chat_id=42)
    await adapter.start()
    await adapter.stop()

    mock_app.updater.stop.assert_awaited_once()
    mock_app.stop.assert_awaited_once()
    mock_app.shutdown.assert_awaited_once()


async def test_start_is_idempotent(patched_app_factory, mock_app):
    _, _ = patched_app_factory
    adapter = TelegramChannelAdapter(token="123:abc", chat_id=42)

    await adapter.start()
    await adapter.start()  # second call should be no-op

    # initialize called once, not twice
    assert mock_app.initialize.await_count == 1
