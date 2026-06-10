"""agentforge.adapters.llm_compat — OpenAI-compat base class.

Extracted from mailbox-llm-bridge/src/mailbox_bridge/llm_providers.py.
The retry/backoff/error logic is the well-tested core; the sync
`_do_chat()` method here is wrapped in async `chat()` by the ABC.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import ClassVar

from agentforge.adapters.base import BaseLLMAdapter

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when a provider call fails after retries."""


@dataclass
class ChatResult:
    content: str
    model: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None


class BaseOpenAICompatLLMAdapter(BaseLLMAdapter):
    """Base for OpenAI-compatible /v1/chat/completions endpoints.

    Subclasses set class-level constants for endpoint, default model,
    and env-var name. The base class handles HTTP, JSON, retries, and
    error normalization. ~70 lines of duplicated logic live here once
    instead of three times.

    The async `chat()` entry point wraps the sync `_do_chat()` in
    asyncio.to_thread — urllib is sync, and we don't want to block
    the event loop on a 60s LLM call.
    """

    DEFAULT_BASE_URL: ClassVar[str] = ""
    DEFAULT_MODEL: ClassVar[str] = ""
    ENV_API_KEY: ClassVar[str] = ""
    DEFAULT_TIMEOUT: ClassVar[float] = 60.0
    DEFAULT_MAX_TOKENS: ClassVar[int] = 2048
    DEFAULT_TEMPERATURE: ClassVar[float] = 0.4
    EXTRA_HEADERS: ClassVar[dict] = {}

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ):
        # Resolve api_key: explicit arg > env var > ""
        if api_key is None and self.ENV_API_KEY:
            api_key = os.environ.get(self.ENV_API_KEY, "")
        if not api_key and self.ENV_API_KEY:
            # Only raise if THIS provider requires a real key (Ollama
            # leaves ENV_API_KEY=*** and uses a dummy "ollama" key).
            raise LLMError(f"{self.ENV_API_KEY} not set")
        self.api_key = api_key or ""
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.max_retries = max_retries
        self.max_tokens = max_tokens if max_tokens is not None else self.DEFAULT_MAX_TOKENS
        self.temperature = temperature if temperature is not None else self.DEFAULT_TEMPERATURE
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

    # -- async entry point (ABC contract) --------------------------------

    async def chat(self, system: str, user: str, **kwargs) -> str:
        """Async wrapper around _do_chat. Runs the HTTP call in a thread
        so the event loop stays responsive."""
        result = await asyncio.to_thread(self._do_chat, system, user, **kwargs)
        return result.content

    # -- sync HTTP core (the tested logic) --------------------------------

    def _do_chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        target_model = model or self.DEFAULT_MODEL
        payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        request_timeout = timeout if timeout is not None else self.timeout

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.base_url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {self.api_key}")
            for k, v in self.EXTRA_HEADERS.items():
                req.add_header(k, v)

            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                    body = resp.read()
                latency_ms = int((time.monotonic() - t0) * 1000)
                parsed = json.loads(body)
                choices = parsed.get("choices") or []
                if not choices:
                    raise LLMError(f"no choices in response: {parsed!r}")
                msg = choices[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                if not content:
                    raise LLMError(f"empty content in response: {parsed!r}")
                usage = parsed.get("usage") or {}
                return ChatResult(
                    content=content,
                    model=parsed.get("model", target_model),
                    latency_ms=latency_ms,
                    tokens_in=usage.get("prompt_tokens"),
                    tokens_out=usage.get("completion_tokens"),
                )
            except urllib.error.HTTPError as e:
                # Status-code-aware retry policy. We don't retry 4xx
                # except 408 (request timeout) and 429 (rate limit).
                # 5xx IS retried. Connection-level errors retried unconditionally.
                last_err = e
                retry_after = self._parse_retry_after(e.headers) if e.headers else None
                if e.code in (408, 429) and attempt < self.max_retries:
                    backoff = retry_after if retry_after is not None else _backoff(attempt)
                    logger.warning(
                        "%s: HTTP %d, retrying in %.2fs (attempt %d/%d)",
                        self.__class__.__name__, e.code, backoff,
                        attempt + 1, self.max_retries + 1,
                    )
                    time.sleep(backoff)
                    continue
                if 500 <= e.code < 600 and attempt < self.max_retries:
                    backoff = retry_after if retry_after is not None else _backoff(attempt)
                    logger.warning(
                        "%s: HTTP %d (server error), retrying in %.2fs (attempt %d/%d)",
                        self.__class__.__name__, e.code, backoff,
                        attempt + 1, self.max_retries + 1,
                    )
                    time.sleep(backoff)
                    continue
                raise LLMError(
                    f"{self.__class__.__name__} HTTP {e.code} "
                    f"(no retry): {e.reason}"
                ) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(
                    f"{self.__class__.__name__} call failed after "
                    f"{self.max_retries + 1} attempts: {e}"
                ) from e
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                raise LLMError(
                    f"{self.__class__.__name__} response parse failed: {e}"
                ) from e
        raise LLMError(f"{self.__class__.__name__} call failed: {last_err}")

    @staticmethod
    def _parse_retry_after(headers) -> float | None:
        """Parse the Retry-After header. Returns seconds (float) or None.

        RFC 7231: Retry-After can be either an HTTP-date or a
        delta-seconds (integer). We accept both, return float seconds.
        """
        try:
            val = headers.get("Retry-After") or headers.get("retry-after")
            if val is None:
                return None
            seconds = int(val)
            if seconds >= 0:
                return float(seconds)
        except (TypeError, ValueError):
            pass
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(val)
            if dt is not None:
                delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                return max(0.0, float(delta))
        except Exception:
            pass
        return None


def _backoff(attempt: int) -> float:
    """Exponential backoff with ±25% jitter to break thundering herd.

    Returns seconds. attempt=0 → 1s, attempt=1 → 2s, attempt=2 → 4s,
    each with random ±25% jitter. Capped at 30s to avoid absurd waits.
    """
    base = min(2 ** attempt, 30)
    jitter = base * 0.25 * (2 * random.random() - 1)
    return max(0.0, base + jitter)
