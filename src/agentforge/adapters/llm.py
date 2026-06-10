"""agentforge.adapters.llm — concrete LLM provider adapters.

Provider list mirrors the production set in mailbox-llm-bridge:
  - OpenRouter (free + paid models via openrouter.ai)
  - MiniMax direct API (M2.7 / M3 via /v1 endpoint)
  - Ollama (local, OpenAI-compatible /v1 endpoint)

All providers share BaseOpenAICompatLLMAdapter (in llm_compat) — they
only differ in endpoint, default model, and auth-var.
"""

from __future__ import annotations

from agentforge.adapters.llm_compat import (
    LLMError,
    BaseOpenAICompatLLMAdapter,
)


__all__ = [
    "LLMError",
    "BaseOpenAICompatLLMAdapter",
    "OpenRouterAdapter",
    "MiniMaxAdapter",
    "OllamaAdapter",
    "make_provider",
]


class OpenRouterAdapter(BaseOpenAICompatLLMAdapter):
    """OpenRouter — free + paid models via openrouter.ai.

    Default: google/gemma-4-31b-it:free (1M context, free).
    """
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemma-4-31b-it:free"
    ENV_API_KEY = "OPENROUTER_API_KEY"
    EXTRA_HEADERS = {
        "HTTP-Referer": "https://github.com/Cole666hs/agent-forge",
        "X-Title": "agentforge",
    }


class MiniMaxAdapter(BaseOpenAICompatLLMAdapter):
    """MiniMax direct API (M2.7 / M3) via /v1 endpoint."""
    DEFAULT_BASE_URL = "https://api.minimax.io/v1/chat/completions"
    DEFAULT_MODEL = "MiniMax-M3"
    ENV_API_KEY = "MINIMAX_API_KEY"


class OllamaAdapter(BaseOpenAICompatLLMAdapter):
    """Ollama local instance (OpenAI-compatible /v1 endpoint).

    No real auth — Ollama ignores the key. Default api_key="ollama"
    just keeps the base class happy. Higher timeout because local
    model loading can be slow on first call.
    """
    DEFAULT_BASE_URL = "http://localhost:11434/v1/chat/completions"
    DEFAULT_MODEL = "qwen2.5-coder"
    ENV_API_KEY = ""  # explicit: no env read
    DEFAULT_TIMEOUT = 120.0
    DEFAULT_TEMPERATURE = 0.3

    def __init__(self, api_key: str = "ollama", **kwargs):
        super().__init__(api_key=api_key, **kwargs)


def make_provider(name: str, **kwargs) -> BaseOpenAICompatLLMAdapter:
    """Dispatch a provider by short name. Accepts aliases."""
    name = (name or "").lower().strip()
    if name in ("openrouter", "or"):
        return OpenRouterAdapter(**kwargs)
    if name in ("minimax", "m"):
        return MiniMaxAdapter(**kwargs)
    if name in ("ollama", "local"):
        return OllamaAdapter(**kwargs)
    raise LLMError(
        f"unknown provider: {name!r}; want openrouter|minimax|ollama"
    )
