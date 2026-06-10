"""agentforge — self-hosted multi-agent orchestration."""

from __future__ import annotations

from agentforge.adapters import (
    BaseChannelAdapter,
    BaseLLMAdapter,
    BaseOpenAICompatLLMAdapter,
    ChatResult,
    LLMError,
    MiniMaxAdapter,
    OllamaAdapter,
    OpenRouterAdapter,
    WebhookChannelAdapter,
    WebhookError,
    make_provider,
)
from agentforge.core import VALID_INTENTS, FileMailbox, Mailbox, Message

__version__ = "0.1.0"
__all__ = [
    # Core
    "FileMailbox",
    "Mailbox",
    "Message",
    "VALID_INTENTS",
    # Adapters
    "BaseChannelAdapter",
    "BaseLLMAdapter",
    "BaseOpenAICompatLLMAdapter",
    "OpenRouterAdapter",
    "MiniMaxAdapter",
    "OllamaAdapter",
    "make_provider",
    "ChatResult",
    "LLMError",
    "WebhookChannelAdapter",
    "WebhookError",
]
