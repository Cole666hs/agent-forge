"""agentforge.adapters — channel + LLM adapter contracts and providers."""

from agentforge.adapters.base import BaseChannelAdapter, BaseLLMAdapter
from agentforge.adapters.llm import (
    LLMError,
    BaseOpenAICompatLLMAdapter,
    MiniMaxAdapter,
    OllamaAdapter,
    OpenRouterAdapter,
    make_provider,
)
from agentforge.adapters.llm_compat import ChatResult

__all__ = [
    # ABCs
    "BaseChannelAdapter",
    "BaseLLMAdapter",
    # LLM concrete providers
    "BaseOpenAICompatLLMAdapter",
    "OpenRouterAdapter",
    "MiniMaxAdapter",
    "OllamaAdapter",
    "make_provider",
    "ChatResult",
    "LLMError",
]
