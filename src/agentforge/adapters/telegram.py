"""agentforge.adapters.telegram — Telegram channel adapter.

Wraps python-telegram-bot v20+. Async lifecycle via Application.
Long-polling for receive; bot.send_message for send.

Note: requires `python-telegram-bot>=20.0` (a runtime dep).
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, ClassVar, Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from agentforge.adapters.base import BaseChannelAdapter
from agentforge.core.message import Message

logger = logging.getLogger(__name__)


class TelegramChannelAdapter(BaseChannelAdapter):
    """Telegram bot adapter.

    Constructor args:
      token: BotFather token (string like "123456:ABC-DEF...")
      chat_id: numeric chat ID to send to. Optional for receive-only bots
        (set via the bot's commands instead of from this constructor).

    Lifecycle:
      await adapter.start()    # begins long-polling
      await adapter.send(msg)  # sends msg.content to chat_id
      async for m in adapter.receive(): ...  # yields incoming texts
      await adapter.stop()     # clean shutdown
    """

    name: ClassVar[str] = "telegram"

    def __init__(self, token: str, chat_id: Optional[int] = None):
        if not token:
            raise ValueError("Telegram token is required")
        self.token = token
        self.chat_id = chat_id
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._app: Optional[Application] = None
        self._started = False

    async def start(self) -> None:
        """Build the Application, register handler, start polling."""
        if self._started:
            return
        self._app = Application.builder().token(self.token).build()
        self._app.add_handler(MessageHandler(filters.TEXT, self._on_telegram_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        self._started = True
        logger.info("telegram adapter started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        """Stop polling and shut down the Application."""
        if not self._started or self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._started = False
            self._app = None

    async def send(self, message: Message) -> None:
        """Send message.content as a Telegram text message to chat_id."""
        if self.chat_id is None:
            raise ValueError(
                "Telegram chat_id not configured — required for send()"
            )
        if self._app is None:
            raise RuntimeError("Telegram adapter not started — call start() first")
        await self._app.bot.send_message(
            chat_id=self.chat_id, text=message.content
        )
        logger.info(
            "telegram: → chat_id=%s (%d chars)", self.chat_id, len(message.content)
        )

    async def receive(self) -> AsyncIterator[Message]:
        """Yield incoming text messages as agentforge Message objects."""
        while True:
            yield await self._inbox.get()

    # -- internal: telegram Update handler --------------------------------

    async def _on_telegram_message(self, update: Update, context) -> None:
        """Called by python-telegram-bot for every incoming text message."""
        if update.message is None or update.message.text is None:
            return
        tg_msg = update.message
        msg = Message(
            from_=str(tg_msg.from_user.id) if tg_msg.from_user else "unknown",
            to=str(tg_msg.chat_id),
            content=tg_msg.text,
            intent="respond",
        )
        await self._inbox.put(msg)
