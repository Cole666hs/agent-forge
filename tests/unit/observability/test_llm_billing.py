"""Tests for instrument_llm with billing/quota wiring."""
import pytest

from agentforge.billing.plans import Plan
from agentforge.billing.quota import QuotaExceededError
from agentforge.billing.usage import UsageStore
from agentforge.observability.instrumentation import instrument_llm
from agentforge.observability.metrics import MetricsRegistry
from agentforge.tenants.registry import TenantRegistry


class _FakeLLMResponse:
    def __init__(self, content="ok", tokens_in=10, tokens_out=20):
        self.content = content
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class _FakeProvider:
    """Minimal LLM provider. Has a `_do_chat` method that returns a response."""
    def __init__(self, tokens_in=10, tokens_out=20, fail=False):
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self._fail = fail
        self.calls = 0

    def _do_chat(self, system, user, *args, **kwargs):
        self.calls += 1
        if self._fail:
            raise RuntimeError("llm call failed")
        return _FakeLLMResponse(
            content="ok", tokens_in=self._tokens_in, tokens_out=self._tokens_out
        )


def test_instrument_llm_records_tokens_to_usage_store(tmp_path):
    reg = MetricsRegistry()
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    tenants.add("acme")
    usage = UsageStore(path=tmp_path / "usage.json")
    provider = _FakeProvider(tokens_in=100, tokens_out=50)
    instrument_llm(
        provider, registry=reg,
        tenants=tenants, usage=usage, tenant_id="acme",
    )
    provider._do_chat("sys", "user")
    # 100 in + 50 out = 150 tokens
    assert usage.get("acme").tokens == 150


def test_instrument_llm_blocks_when_quota_exceeded(tmp_path):
    reg = MetricsRegistry()
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    tenants.add("acme")
    usage = UsageStore(path=tmp_path / "usage.json")
    usage.record("acme", 100_000)  # free limit reached
    provider = _FakeProvider(tokens_in=10, tokens_out=20)
    instrument_llm(
        provider, registry=reg,
        tenants=tenants, usage=usage, tenant_id="acme",
    )
    # Pre-flight (0 tokens) passes, real call runs, post-call recording
    # would push us over → raises QuotaExceededError.
    with pytest.raises(QuotaExceededError):
        provider._do_chat("sys", "user")
    # Provider WAS called (pre-flight didn't block) but tokens were
    # not recorded (the post-call record raises before storage).
    assert provider.calls == 1
    # Tokens were not recorded (the post-call enforce_quota blocked)
    assert usage.get("acme").tokens == 100_000


def test_instrument_llm_enterprise_unlimited_never_blocks(tmp_path):
    reg = MetricsRegistry()
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    tenants.add("acme")
    tenants.set_plan("acme", Plan.ENTERPRISE)
    usage = UsageStore(path=tmp_path / "usage.json")
    usage.record("acme", 10**12)  # way over free/pro limit
    provider = _FakeProvider(tokens_in=100, tokens_out=100)
    instrument_llm(
        provider, registry=reg,
        tenants=tenants, usage=usage, tenant_id="acme",
    )
    # Should not raise
    provider._do_chat("sys", "user")
    assert usage.get("acme").tokens == 10**12 + 200


def test_instrument_llm_records_quota_exceeded_outcome_metric(tmp_path):
    reg = MetricsRegistry()
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    tenants.add("acme")
    usage = UsageStore(path=tmp_path / "usage.json")
    usage.record("acme", 100_000)
    provider = _FakeProvider()
    instrument_llm(
        provider, registry=reg,
        tenants=tenants, usage=usage, tenant_id="acme",
    )
    with pytest.raises(QuotaExceededError):
        provider._do_chat("sys", "user")
    out = reg.render()
    # The blocked call still gets counted (with the quota_exceeded label)
    assert "quota_exceeded" in out


def test_instrument_llm_without_billing_kwargs_works(tmp_path):
    """Backward compat: old callers instrument_llm(p, registry=reg) still work."""
    reg = MetricsRegistry()
    provider = _FakeProvider(tokens_in=5, tokens_out=5)
    instrument_llm(provider, registry=reg)  # no billing kwargs
    provider._do_chat("sys", "user")
    # Metrics still work
    out = reg.render()
    assert "agentforge_llm_calls_total" in out
