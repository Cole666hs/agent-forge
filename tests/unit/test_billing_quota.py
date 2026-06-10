"""Tests for billing.quota — QuotaStatus + enforce_quota + QuotaExceededError."""
import pytest
from agentforge.billing.plans import Plan
from agentforge.billing.quota import QuotaStatus, quota_status, enforce_quota, QuotaExceededError
from agentforge.billing.usage import UsageStore
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def setup(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    tenants.add("acme")
    usage = UsageStore(path=tmp_path / "usage.json")
    return tenants, usage


def test_quota_status_free_below_warning(setup):
    tenants, usage = setup
    s = quota_status(tenants, usage, "acme")
    assert s.used == 0
    assert s.limit == 100_000
    assert s.remaining == 100_000
    assert s.pct == 0.0
    assert s.warning is False
    assert s.exceeded is False
    assert s.plan == Plan.FREE


def test_quota_status_free_above_warning(setup):
    tenants, usage = setup
    usage.record("acme", 85_000)
    s = quota_status(tenants, usage, "acme")
    assert s.warning is True
    assert s.exceeded is False
    assert s.remaining == 15_000


def test_quota_status_free_at_limit(setup):
    tenants, usage = setup
    usage.record("acme", 100_000)
    s = quota_status(tenants, usage, "acme")
    assert s.warning is True
    assert s.exceeded is True
    assert s.remaining == 0


def test_quota_status_enterprise_never_exceeded(setup):
    tenants, usage = setup
    tenants.set_plan("acme", Plan.ENTERPRISE)
    usage.record("acme", 10_000_000_000)
    s = quota_status(tenants, usage, "acme")
    assert s.exceeded is False
    assert s.limit is None
    assert s.remaining is None


def test_enforce_quota_passes_under_limit(setup):
    tenants, usage = setup
    usage.record("acme", 50_000)
    s = enforce_quota(tenants, usage, "acme", tokens_to_add=10_000)
    assert s.used == 60_000
    assert s.exceeded is False


def test_enforce_quota_blocks_at_limit(setup):
    tenants, usage = setup
    usage.record("acme", 100_000)
    with pytest.raises(QuotaExceededError) as exc_info:
        enforce_quota(tenants, usage, "acme", tokens_to_add=1)
    assert exc_info.value.used == 100_000
    assert exc_info.value.limit == 100_000
    assert "quota exceeded" in str(exc_info.value).lower()


def test_enforce_quota_blocks_when_adding_would_overflow(setup):
    tenants, usage = setup
    usage.record("acme", 99_990)
    with pytest.raises(QuotaExceededError):
        enforce_quota(tenants, usage, "acme", tokens_to_add=20)


def test_enforce_quota_does_not_record_on_block(setup):
    tenants, usage = setup
    usage.record("acme", 100_000)
    with pytest.raises(QuotaExceededError):
        enforce_quota(tenants, usage, "acme", tokens_to_add=1)
    assert usage.get("acme").tokens == 100_000  # unchanged


def test_enforce_quota_enterprise_unlimited(setup):
    tenants, usage = setup
    tenants.set_plan("acme", Plan.ENTERPRISE)
    s = enforce_quota(tenants, usage, "acme", tokens_to_add=10**12)
    assert usage.get("acme").tokens == 10**12
    assert s.exceeded is False


def test_enforce_quota_rejects_negative(setup):
    tenants, usage = setup
    with pytest.raises(ValueError, match="non-negative"):
        enforce_quota(tenants, usage, "acme", tokens_to_add=-1)
