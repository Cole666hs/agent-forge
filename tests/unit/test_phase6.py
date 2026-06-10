"""Unit tests for the State tenant_id column + TenantRegistry."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentforge.tenants.registry import TenantRegistry
from agentforge.workflows.engine import State


# ---------------------------------------------------------------------------
# State with tenant_id
# ---------------------------------------------------------------------------

def test_state_persists_with_tenant_id(tmp_path: Path):
    """State persists + hydrates with a tenant_id column."""
    db = tmp_path / "state.db"
    s = State(run_id="r1", tenant_id="acme")
    s.set("k", "v")
    s.persist(db)

    # Re-hydrate with same tenant_id
    s2 = State(run_id="r1", tenant_id="acme")
    s2.hydrate(db)
    assert s2.get("k") == "v"


def test_state_tenants_are_isolated(tmp_path: Path):
    """Two tenants with the same run_id don't see each other's state."""
    db = tmp_path / "state.db"
    a = State(run_id="r1", tenant_id="acme")
    a.set("k", "from-acme")
    a.persist(db)
    b = State(run_id="r1", tenant_id="corp")
    b.set("k", "from-corp")
    b.persist(db)

    a2 = State(run_id="r1", tenant_id="acme")
    a2.hydrate(db)
    assert a2.get("k") == "from-acme"

    b2 = State(run_id="r1", tenant_id="corp")
    b2.hydrate(db)
    assert b2.get("k") == "from-corp"


def test_state_no_tenant_id_backward_compat(tmp_path: Path):
    """Empty tenant_id still works (single-tenant / self-hosted)."""
    db = tmp_path / "state.db"
    s = State(run_id="r1")  # no tenant
    s.set("k", "v")
    s.persist(db)
    s2 = State(run_id="r1")
    s2.hydrate(db)
    assert s2.get("k") == "v"


# ---------------------------------------------------------------------------
# TenantRegistry
# ---------------------------------------------------------------------------

def test_registry_add_and_lookup(tmp_path: Path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme", api_key="key-acme-123")
    assert reg.lookup("key-acme-123") == "acme"


def test_registry_unknown_key_returns_none(tmp_path: Path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme", api_key="key-acme-123")
    assert reg.lookup("key-nope") is None


def test_registry_persists_across_instances(tmp_path: Path):
    """Two TenantRegistry instances pointing at the same file see the same data."""
    path = tmp_path / "tenants.json"
    TenantRegistry(path=path).add("acme", api_key="k1")
    reg2 = TenantRegistry(path=path)
    assert reg2.lookup("k1") == "acme"


def test_registry_duplicate_tenant_rejected(tmp_path: Path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme", api_key="k1")
    with pytest.raises(ValueError, match="already exists"):
        reg.add("acme", api_key="k2")


def test_registry_remove(tmp_path: Path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme", api_key="k1")
    reg.remove("acme")
    assert reg.lookup("k1") is None
    assert "acme" not in reg.list_tenants()


def test_registry_list_tenants(tmp_path: Path):
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    reg.add("acme", api_key="k1")
    reg.add("corp", api_key="k2")
    assert sorted(reg.list_tenants()) == ["acme", "corp"]


def test_registry_does_not_store_key_in_plaintext(tmp_path: Path):
    """API keys must be stored hashed, never in plaintext (defense in
    depth — if the file leaks, keys aren't immediately usable)."""
    path = tmp_path / "tenants.json"
    TenantRegistry(path=path).add("acme", api_key="super-secret-key")
    raw = path.read_text()
    assert "super-secret-key" not in raw
    # The hash is in the file
    assert "api_key_hash" in raw
