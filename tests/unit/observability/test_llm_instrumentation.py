"""T8 RED — LLM instrumentation test."""

import json
from unittest import mock

from agentforge.adapters.llm import make_provider
from agentforge.observability.metrics import MetricsRegistry
from agentforge.observability.instrumentation import instrument_llm


def _mock_resp(body_bytes):
    class _MockResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body_bytes
    return _MockResp()


def test_instrument_llm_records_call_counter_and_duration():
    reg = MetricsRegistry()
    p = make_provider("ollama")
    instrument_llm(p, registry=reg)

    body = json.dumps({
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }).encode("utf-8")

    with mock.patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        p._do_chat("sys", "user")

    out = reg.render()
    assert "agentforge_llm_calls_total" in out
    assert 'agentforge_llm_calls_total{provider="OllamaAdapter",outcome="success"} 1.0' in out
    assert "agentforge_llm_call_duration_seconds_count" in out


def test_instrument_llm_records_token_usage():
    reg = MetricsRegistry()
    p = make_provider("ollama")
    instrument_llm(p, registry=reg)

    body = json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }).encode("utf-8")

    with mock.patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        p._do_chat("sys", "user")

    out = reg.render()
    assert 'agentforge_llm_tokens_total{provider="OllamaAdapter",direction="in"} 11.0' in out
    assert 'agentforge_llm_tokens_total{provider="OllamaAdapter",direction="out"} 5.0' in out


def test_instrument_llm_records_error_outcome():
    reg = MetricsRegistry()
    p = make_provider("ollama")
    instrument_llm(p, registry=reg)

    with mock.patch("urllib.request.urlopen", side_effect=Exception("boom")):
        try:
            p._do_chat("sys", "user")
        except Exception:
            pass

    out = reg.render()
    assert 'agentforge_llm_calls_total{provider="OllamaAdapter",outcome="error"} 1.0' in out


def test_instrument_llm_idempotent():
    reg = MetricsRegistry()
    p = make_provider("ollama")
    instrument_llm(p, registry=reg)
    instrument_llm(p, registry=reg)  # no-op

    body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode("utf-8")
    with mock.patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        p._do_chat("sys", "user")

    out = reg.render()
    assert 'agentforge_llm_calls_total{provider="OllamaAdapter",outcome="success"} 1.0' in out
