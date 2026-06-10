"""agentforge — self-hosted multi-agent orchestration."""

from __future__ import annotations

from agentforge.adapters import (
    BaseChannelAdapter, BaseLLMAdapter,
    BaseOpenAICompatLLMAdapter, ChatResult, DiscordChannelAdapter, EmailChannelAdapter,
    EmailError, LLMError, MiniMaxAdapter, OllamaAdapter, OpenRouterAdapter,
    TelegramChannelAdapter, WebhookChannelAdapter, WebhookError, make_provider,
)
from agentforge.core import VALID_INTENTS, FileMailbox, Mailbox, Message
from agentforge.tenants import TenantRegistry
from agentforge.workflows import State, Step, Workflow, WorkflowError, register_step_type

__version__ = "0.2.0"
__all__ = [
    "FileMailbox", "Mailbox", "Message", "VALID_INTENTS",
    "BaseChannelAdapter", "BaseLLMAdapter",
    "BaseOpenAICompatLLMAdapter", "OpenRouterAdapter", "MiniMaxAdapter",
    "OllamaAdapter", "make_provider", "ChatResult", "LLMError",
    "DiscordChannelAdapter", "EmailChannelAdapter", "EmailError",
    "TelegramChannelAdapter", "WebhookChannelAdapter", "WebhookError",
    "State", "Step", "Workflow", "WorkflowError", "register_step_type",
    "TenantRegistry",
]
