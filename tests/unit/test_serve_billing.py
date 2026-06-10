"""Tests for billing/quota HTTP surface in agentforge.serve."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge.billing.usage import UsageStore
from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def app_setup(tmp_path: Path):
    tenants_path = tmp_path / "tenants.json"
    reg = TenantRegistry(path=tenants_path)
    api_key = reg.add("acme")
    mailbox_root = tmp_path / "mailbox"
    state_db = tmp_path / "state.db"
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    app = create_app(
        tenants_path=tenants_path, mailbox_root=mailbox_root,
        state_db=state_db, workflows_dir=workflows_dir,
    )
    return app, {
        "tenant_id": "acme", "api_key": api_key,
        "mailbox_root": mailbox_root, "tenants_path": tenants_path,
    }


def test_get_tenant_usage_endpoint(app_setup):
    app, ctx = app_setup
    UsageStore(path=ctx["mailbox_root"].parent / "usage.json").record("acme", 42_000)
    # Restart app so it picks up the seeded usage (registry is in-memory)
    app2 = create_app(
        tenants_path=ctx["tenants_path"],
        mailbox_root=ctx["mailbox_root"],
    )
    client = TestClient(app2)
    r = client.get(
        "/v1/tenants/acme/usage",
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["plan"] == "free"
    assert body["used"] == 42_000
    assert body["limit"] == 100_000
    assert body["remaining"] == 58_000
    assert body["warning"] is False
    assert body["exceeded"] is False


def test_get_tenant_usage_warning_above_80pct(app_setup):
    app, ctx = app_setup
    UsageStore(path=ctx["mailbox_root"].parent / "usage.json").record("acme", 85_000)
    app2 = create_app(
        tenants_path=ctx["tenants_path"], mailbox_root=ctx["mailbox_root"],
    )
    client = TestClient(app2)
    r = client.get("/v1/tenants/acme/usage", headers={"X-API-Key": ctx["api_key"]})
    assert r.status_code == 200
    body = r.json()
    assert body["warning"] is True
    assert body["exceeded"] is False


def test_get_tenant_usage_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/v1/tenants/acme/usage")
    assert r.status_code == 401


def test_post_message_includes_quota_headers(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        headers={"X-API-Key": ctx["api_key"]},
        json={"to": "bot", "content": "hi", "intent": "respond"},
    )
    assert r.status_code == 201
    # Quota headers are informational — messages don't consume tokens,
    # so used stays at whatever the current usage is (0 here).
    assert r.headers.get("X-Quota-Used") == "0"
    assert r.headers.get("X-Quota-Limit") == "100000"
    assert r.headers.get("X-Quota-Warning") == "false"
    assert r.headers.get("X-Quota-Exceeded") == "false"
