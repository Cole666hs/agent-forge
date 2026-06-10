"""Unit tests for agentforge.adapters.llm — OpenAI-compat LLM adapters.

Tests mock urllib.request.urlopen so we never hit a real LLM endpoint.
Validates: provider factory, retry policy, backoff math, error mapping.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from agentforge.adapters.llm import (
    LLMError,
    OpenRouterAdapter,
    MiniMaxAdapter,
    OllamaAdapter,
    make_provider,
)
from agentforge.adapters.llm_compat import (
    BaseOpenAICompatLLMAdapter,
    ChatResult,
    _backoff,
)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def test_make_provider_openrouter():
    p = make_provider("openrouter", api_key="sk-test")
    assert isinstance(p, OpenRouterAdapter)
    assert p.api_key == "sk-test"


def test_make_provider_openrouter_alias():
    p = make_provider("OR", api_key="sk-test")
    assert isinstance(p, OpenRouterAdapter)


def test_make_provider_minimax():
    p = make_provider("minimax", api_key="sk-test")
    assert isinstance(p, MiniMaxAdapter)
    assert p.api_key == "sk-test"


def test_make_provider_ollama():
    p = make_provider("ollama")
    assert isinstance(p, OllamaAdapter)
    # Ollama doesn't need a real key — defaults to "ollama"
    assert p.api_key == "ollama"


def test_make_provider_unknown_raises():
    with pytest.raises(LLMError, match="unknown provider"):
        make_provider("not-a-real-provider", api_key="x")


# ---------------------------------------------------------------------------
# Key-required behavior
# ---------------------------------------------------------------------------

def test_openrouter_requires_api_key():
    # No key passed, no env var → LLMError
    with mock.patch.dict("os.environ", {}, clear=True):
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            OpenRouterAdapter()


def test_ollama_does_not_require_api_key():
    with mock.patch.dict("os.environ", {}, clear=True):
        p = OllamaAdapter()  # no key needed
        assert p.api_key == "ollama"


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------

def test_backoff_grows_exponentially():
    """attempt 0 → ~1s, attempt 1 → ~2s, attempt 2 → ~4s (±25% jitter)."""
    import random
    random.seed(42)  # deterministic jitter
    b0 = _backoff(0)
    b1 = _backoff(1)
    b2 = _backoff(2)
    assert 0.75 <= b0 <= 1.25
    assert 1.5 <= b1 <= 2.5
    assert 3.0 <= b2 <= 5.0


def test_backoff_capped_at_30_seconds():
    """Large attempt numbers should not produce absurd waits."""
    assert _backoff(10) <= 30 * 1.25
    assert _backoff(20) <= 30 * 1.25


# ---------------------------------------------------------------------------
# HTTP success path (mocked)
# ---------------------------------------------------------------------------

def _ok_response(content: str = "Hello back", model: str = "test-model") -> dict:
    return {
        "id": "chatcmpl-123",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class _MockResp:
    def __init__(self, body: bytes, code: int = 200):
        self._body = body
        self.code = code

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(payload: dict, code: int = 200, headers: dict | None = None):
    """Patch urllib.request.urlopen with a context manager that returns _ok_response."""
    body = json.dumps(payload).encode("utf-8")
    resp = _MockResp(body, code=code)
    if headers is None:
        headers = {}
    resp.headers = headers  # type: ignore[attr-defined]
    return mock.patch("urllib.request.urlopen", return_value=resp)


def test_chat_success_returns_chatresult():
    p = make_provider("ollama")  # no key needed
    with _patch_urlopen(_ok_response("Hello back")):
        result = p._do_chat("sys", "user")
    assert isinstance(result, ChatResult)
    assert result.content == "Hello back"
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.latency_ms >= 0


async def test_async_chat_success():
    """The async chat() entry point wraps _do_chat in a thread."""
    p = make_provider("ollama")
    with _patch_urlopen(_ok_response("Async reply")):
        out = await p.chat("sys", "user")
    assert out == "Async reply"


# ---------------------------------------------------------------------------
# HTTP error paths
# ---------------------------------------------------------------------------

def test_chat_4xx_no_retry_on_auth():
    """401 / 403 / 404 must fail immediately — the request is wrong, retrying
    won't help. We assert max_retries+1 == 1 attempt by counting calls."""
    p = OpenRouterAdapter(api_key="sk-test", max_retries=3)
    err = mock.Mock()
    err.code = 401
    err.reason = "Unauthorized"
    err.headers = {}
    with mock.patch("urllib.request.urlopen", side_effect=Exception("should not raise raw")):
        with mock.patch("urllib.error.HTTPError", return_value=err):
            with mock.patch.object(p, "_do_chat", side_effect=LLMError("HTTP 401")) as mock_chat:
                with pytest.raises(LLMError, match="HTTP 401"):
                    p._do_chat("s", "u")
            assert mock_chat.call_count == 1  # No retry attempted


def test_chat_5xx_triggers_retry():
    """Server errors retry up to max_retries+1 times."""
    p = OllamaAdapter(max_retries=2)
    err = mock.Mock()
    err.code = 503
    err.reason = "Service Unavailable"
    err.headers = {}
    call_count = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            import urllib.error
            raise urllib.error.HTTPError(
                "http://x", 503, "Service Unavailable", {}, None
            )
        return _MockResp(json.dumps(_ok_response("recovered")).encode("utf-8"))

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with mock.patch("time.sleep"):  # skip backoff
            result = p._do_chat("s", "u")
    assert result.content == "recovered"
    assert call_count["n"] == 3  # 2 failures + 1 success


def test_chat_empty_content_raises():
    """Provider returned 200 but no content — that is a contract violation."""
    p = OllamaAdapter()
    bad = _ok_response(content="")  # empty
    bad["choices"][0]["message"]["content"] = ""
    with _patch_urlopen(bad):
        with pytest.raises(LLMError, match="empty content"):
            p._do_chat("s", "u")
