"""agentforge.adapters.discord — Discord channel adapter.

Wraps discord.py v2+ (the `discord` package). Async Client lifecycle.
channel.send for outbound, on_message event for inbound.

Note: requires `discord.py>=2.0` (a runtime dep).
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, ClassVar, Optional

import discord

from agentforge.adapters.base import BaseChannelAdapter
from agentforge.core.message import Message

logger = logging.getLogger(__name__)


class DiscordChannelAdapter(BaseChannelAdapter):
    """Discord bot adapter.

    Constructor args:
      token: bot token from Discord Developer Portal
      channel_id: numeric channel ID to send to. Optional for receive-only.

    Lifecycle:
      await adapter.start()    # bot.login + connect (does NOT block)
      await adapter.send(msg)  # sends msg.content to channel_id
      async for m in adapter.receive(): ...  # yields incoming texts
      await adapter.stop()     # bot.close()
    """

    name: ClassVar[str] = "discord"

    def __init__(self, token: str, channel_id: Optional[int] = None):
        if not token:
            raise ValueError("Discord token is required")
        self.token = token
        self.channel_id = channel_id
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._client: Optional[discord.Client] = None
        self._started = False

    async def start(self) -> None:
        """Build the discord.Client, register event handler, connect."""
        if self._started:
            return
        # intents=discord.Intents.default() is the safe minimal set.
        # message_content requires the privileged intent — we register it
        # because the adapter needs to read message.text.
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._client.event(self._on_discord_message)
        self._started = True
        # Client.start() blocks until the bot disconnects. We need to
        # run it in the background and keep a reference for stop().
        self._start_task = asyncio.create_task(self._client.start(self.token))
        # Give the client a moment to log in (don't block forever)
        await asyncio.sleep(0.1)
        logger.info("discord adapter starting (channel_id=%s)", self.channel_id)

    async def stop(self) -> None:
        """Close the Discord client."""
        if not self._started or self._client is None:
            return
        try:
            await self._client.close()
        finally:
            self._started = False
            self._client = None

    async def send(self, message: Message) -> None:
        """Send message.content to the configured channel."""
        if self.channel_id is None:
            raise ValueError(
                "Discord channel_id not configured — required for send()"
            )
        if self._client is None:
            raise RuntimeError("Discord adapter not started — call start() first")
        channel = await self._client.fetch_channel(self.channel_id)
        await channel.send(message.content)
        logger.info(
            "discord: → channel_id=%s (%d chars)", self.channel_id, len(message.content)
        )

    async def receive(self) -> AsyncIterator[Message]:
        """Yield incoming text messages as agentforge Message objects."""
        while True:
            yield await self._inbox.get()

    # -- internal: discord event handler ----------------------------------

    async def _on_discord_message(self, message: discord.Message) -> None:
        """Called by discord.py for every incoming message event."""
        # Ignore bot's own messages
        if message.author.bot:
            return
        if not message.content:
            return
        msg = Message(
            from_=str(message.author.id),
            to=str(message.channel.id),
            content=message.content,
            intent="respond",
        )
        await self._inbox.put(msg)
