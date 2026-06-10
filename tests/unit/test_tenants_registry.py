"""Tests for tenants.registry — tenant + API-key + plan management."""
import json
import pytest

from agentforge.billing.plans import Plan
from agentforge.tenants.registry import TenantRegistry


def test_new_tenant_defaults_to_free_plan(tmp_path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme")
    entry = reg._data["tenants"]["acme"]
    assert entry["plan"] == "free"


def test_set_plan_persists(tmp_path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme")
    reg.set_plan("acme", Plan.PRO)
    reg2 = TenantRegistry(path=tmp_path / "tenants.json")
    assert reg2.get_plan("acme") == Plan.PRO


def test_set_plan_unknown_tenant_raises(tmp_path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    with pytest.raises(ValueError, match="not found"):
        reg.set_plan("ghost", Plan.PRO)


def test_get_plan_unknown_tenant_raises(tmp_path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    with pytest.raises(ValueError, match="not found"):
        reg.get_plan("ghost")


def test_load_legacy_registry_without_plan_field(tmp_path):
    """Backwards compat: v0.3.0 tenants.json without 'plan' → free."""
    legacy = {"tenants": {"oldco": {"api_key_hash": "abc", "created_at": "2026-01-01"}}}
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps(legacy))
    reg = TenantRegistry(path=p)
    assert reg.get_plan("oldco") == Plan.FREE
