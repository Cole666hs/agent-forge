"""Abstract base classes for channel and LLM adapters.

All adapters are async — the multi-agent concurrency model assumes
non-blocking I/O. Synchronous I/O in adapters would block the event
loop and serialize every agent in the system.
"""

from __future__ import annotations

import abc
from typing import AsyncIterator, ClassVar

from agentforge.core.message import Message


class BaseChannelAdapter(abc.ABC):
    """Adapter for sending/receiving messages over an external channel.

    Channels are I/O endpoints: Telegram, Discord, Email, Webhook, etc.
    Each adapter knows how to:
      - send(msg): push an outgoing Message via this channel
      - receive(): async-iterate incoming Messages
      - start(): initialize any resources (HTTP server, websocket, polling loop)
      - stop(): clean shutdown

    The adapter is responsible for translating between the channel's
    native format and the agentforge Message dataclass. Validation,
    retries, and idempotency are library-level concerns; the adapter
    just transports.
    """

    name: ClassVar[str] = ""  # e.g. "telegram", "discord", "webhook"

    @abc.abstractmethod
    async def send(self, message: Message) -> None: ...

    @abc.abstractmethod
    def receive(self) -> AsyncIterator[Message]:
        if False:
            yield  # pragma: no cover — marks this as an async generator
        raise NotImplementedError

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...


class BaseLLMAdapter(abc.ABC):
    """Adapter for LLM providers (Ollama, OpenRouter, Anthropic, etc).

    All concrete adapters must implement `chat()`. Higher-level methods
    (streaming, tool-use, embeddings) can be added per-adapter as
    optional — keep the base contract small.
    """

    @abc.abstractmethod
    async def chat(self, system: str, user: str, **kwargs) -> str: ...
