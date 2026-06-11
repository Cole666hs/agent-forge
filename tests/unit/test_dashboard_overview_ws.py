"""Tests for the v0.8.0 #4 /dashboard/ws/overview WebSocket.

Covers:
- Unauthenticated clients rejected (close 1008).
- Cross-origin requests rejected (close 1008).
- Hello frame on connect.
- A published tenant-quota event triggers a `quota` frame with
  fresh quota_status values.
- Tenant isolation: events for a different tenant are NOT delivered.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def client_with_tenant(tmp_path: Path) -> Iterator[tuple[TestClient, str, str]]:
    tenants_path = tmp_path / "tenants.json"
    mailbox_root = tmp_path / "mailbox"
    tenants = TenantRegistry(path=tenants_path)
    api_key = tenants.add("acme")
    workflows_dir = mailbox_root.parent / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        tenants_path=tenants_path,
        mailbox_root=mailbox_root,
        workflows_dir=workflows_dir,
    )
    with TestClient(app) as c:
        yield c, api_key, "acme"


def test_ws_overview_rejects_without_cookie(tmp_path: Path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/dashboard/ws/overview") as ws:
                ws.receive_text()


def test_ws_overview_rejects_cross_origin(client_with_tenant):
    client, api_key, _ = client_with_tenant
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/dashboard/ws/overview",
            cookies={"agentforge_api_key": api_key},
            headers={"origin": "https://evil.example"},
        ) as ws:
            ws.receive_text()


def test_ws_overview_sends_hello(client_with_tenant):
    client, api_key, _ = client_with_tenant
    with client.websocket_connect(
        "/dashboard/ws/overview",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "hello"
        assert hello["tenant_id"] == "acme"
        assert isinstance(hello["seq"], int)


def test_ws_overview_pushes_quota_on_event(client_with_tenant):
    """A published 'quota_changed' event for our tenant triggers a
    `quota` frame with the re-computed quota_status."""
    client, api_key, tenant_id = client_with_tenant
    bus = client.app.state.runs.events
    with client.websocket_connect(
        "/dashboard/ws/overview",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        ws.receive_text()  # hello
        # Publish an event for our tenant's quota stream.
        bus.publish(
            run_id="r1",
            workflow=f"__tenant_quota__:{tenant_id}",
            tenant_id=tenant_id,
            kind="quota_changed",
            payload={"trigger": "test"},
        )
        msg = json.loads(ws.receive_text())
        assert msg["kind"] == "quota"
        assert msg["tenant_id"] == tenant_id
        # Quota fields are present and of the right shape.
        assert msg["plan"] in ("free", "pro", "enterprise")
        assert isinstance(msg["used"], int)
        assert msg["used"] >= 0
        # `pct` is 0.0 since no usage has been recorded yet.
        assert msg["pct"] == 0.0
        assert msg["warning"] is False
        assert msg["exceeded"] is False


def test_ws_overview_tenant_isolation(client_with_tenant):
    """Events for a different tenant must NOT be delivered to our
    socket. The synthetic workflow key is per-tenant, so the
    publish() call goes to a different bus key — but we also
    defense-in-depth check the tenant_id on the event."""
    client, api_key, tenant_id = client_with_tenant
    # Add a second tenant.
    registry = client.app.state.tenants
    registry.add("other-co")
    bus = client.app.state.runs.events
    with client.websocket_connect(
        "/dashboard/ws/overview",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        ws.receive_text()  # hello
        # Foreign event first (must NOT reach this socket).
        bus.publish(
            run_id="r1",
            workflow="__tenant_quota__:other-co",
            tenant_id="other-co",
            kind="quota_changed",
            payload={},
        )
        # Own event next (must reach).
        bus.publish(
            run_id="r2",
            workflow=f"__tenant_quota__:{tenant_id}",
            tenant_id=tenant_id,
            kind="quota_changed",
            payload={},
        )
        msg = json.loads(ws.receive_text())
        assert msg["kind"] == "quota"
        assert msg["tenant_id"] == tenant_id
