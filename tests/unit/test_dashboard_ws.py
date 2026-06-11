"""Tests for the v0.7.0 WebSocket run-event stream.

Covers:
- Unauthenticated clients are rejected (close code 1008).
- The hello frame is sent on connect with the current max_seq.
- A published event appears as a JSON frame in the WS stream.
- Reconnect with `?since=N` replays missed events from the table.
- The endpoint is workflow-scoped (events for other workflows don't leak).
- Disconnect cleans up the bus subscriber (no leaked queues).
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
    """Build a wired app + tenant + api_key. Yields (client, api_key,
    tenant_id). The tenant has a workflow on disk so the runs route
    resolves. The mailbox is wired but the workflows dir uses the
    mailbox parent's default location (created by create_app)."""
    tenants_path = tmp_path / "tenants.json"
    mailbox_root = tmp_path / "mailbox"
    tenants = TenantRegistry(path=tenants_path)
    api_key = tenants.add("acme")
    workflows_dir = mailbox_root.parent / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "demo.yaml").write_text(
        "name: demo\ndescription: demo\nsteps:\n  - id: echo\n    type: respond\n    inputs:\n      to: user\n      content: 'hi'\n",
        encoding="utf-8",
    )
    app = create_app(
        tenants_path=tenants_path,
        mailbox_root=mailbox_root,
        workflows_dir=workflows_dir,
    )
    with TestClient(app) as c:
        yield c, api_key, "acme"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_ws_rejects_without_cookie(tmp_path: Path):
    """No cookie → close code 1008, no events."""
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    with TestClient(app) as c:
        with pytest.raises(Exception):
            # TestClient raises on non-101 close codes by default.
            with c.websocket_connect("/dashboard/ws/runs/demo") as ws:
                ws.receive_text()


def test_ws_rejects_invalid_cookie(tmp_path: Path):
    """Bad cookie → close code 1008."""
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect(
                "/dashboard/ws/runs/demo",
                cookies={"agentforge_api_key": "not-a-real-key"},
            ) as ws:
                ws.receive_text()


# ---------------------------------------------------------------------------
# Hello + live events
# ---------------------------------------------------------------------------

def test_ws_sends_hello_with_max_seq(client_with_tenant):
    """First frame on connect is a hello with max_seq for the workflow."""
    client, api_key, _ = client_with_tenant
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        first = json.loads(ws.receive_text())
        assert first["kind"] == "hello"
        assert first["workflow"] == "demo"
        assert isinstance(first["seq"], int)


def test_ws_streams_published_events(client_with_tenant):
    """Publishing an event on the bus makes it appear on the WS stream."""
    client, api_key, _ = client_with_tenant
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        hello = json.loads(ws.receive_text())
        # Drain hello; publish; expect a 'started' frame next.
        bus = client.app.state.runs.events
        bus.publish(
            run_id="r-test", workflow="demo", tenant_id="acme",
            kind="started", payload={"agent": "a"},
        )
        msg = json.loads(ws.receive_text())
        assert msg["kind"] == "started"
        assert msg["run_id"] == "r-test"
        assert msg["seq"] > hello["seq"]


def test_ws_is_workflow_scoped(client_with_tenant):
    """Events for 'other' workflow are not visible on the 'demo' socket."""
    client, api_key, _ = client_with_tenant
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        ws.receive_text()  # hello
        bus = client.app.state.runs.events
        bus.publish("r1", "other", "acme", "started")
        # Now publish a demo event — that one should appear.
        bus.publish("r2", "demo", "acme", "started")
        msg = json.loads(ws.receive_text())
        # The 'other' workflow's event must NOT have been delivered to
        # this socket. The first event we see is for 'demo'.
        assert msg["run_id"] == "r2"
        assert msg["kind"] == "started"


# ---------------------------------------------------------------------------
# Replay on reconnect
# ---------------------------------------------------------------------------

def test_ws_replays_events_after_since(client_with_tenant):
    """Connecting with since=<N> replays events with seq > N first,
    then goes live. Order: hello (with current max_seq), then
    replayed events from the table."""
    client, api_key, _ = client_with_tenant
    bus = client.app.state.runs.events
    # Pre-populate: two events for demo.
    bus.publish("r1", "demo", "acme", "started")
    s2 = bus.publish("r2", "demo", "acme", "finished", {"status": "success"})
    # First event's seq, used as `since`.
    s1_seq = 1  # first publish in this fresh DB
    with client.websocket_connect(
        f"/dashboard/ws/runs/demo?since={s1_seq}",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        # Order: hello first (current max_seq), then replay.
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "hello"
        assert hello["seq"] >= s2
        # Replay should deliver only seq > s1_seq, i.e. s2.
        replayed = json.loads(ws.receive_text())
        assert replayed["kind"] == "finished"
        assert replayed["seq"] == s2


def test_ws_reconnect_cleans_up_subscriber(client_with_tenant):
    """Closing the WS must remove the queue from the bus subscriber list."""
    client, api_key, _ = client_with_tenant
    bus = client.app.state.runs.events
    assert bus._subscribers == {}
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        ws.receive_text()  # hello
        # Subscriber registered.
        assert "demo" in bus._subscribers
        assert len(bus._subscribers["demo"]) == 1
    # Socket closed; the iterator's finally block removed the queue.
    # Allow a tick for the cleanup to run.
    import time; time.sleep(0.05)
    assert bus._subscribers == {}


# ---------------------------------------------------------------------------
# v0.7.1 hardening tests
# ---------------------------------------------------------------------------

def test_ws_rejects_cross_origin(client_with_tenant):
    """CSRF protection: when the Origin header is present and does not
    match the request's Host, the WS is rejected with 1008.

    (Same-origin requests, where the browser does not send Origin at
    all, must still work — covered by the existing auth tests.)
    """
    client, api_key, _ = client_with_tenant
    # TestClient lets us inject headers; we simulate the cross-origin
    # case by setting Origin to a different host.
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/dashboard/ws/runs/demo",
            cookies={"agentforge_api_key": api_key},
            headers={"origin": "https://evil.example"},
        ) as ws:
            ws.receive_text()


def test_ws_allows_same_origin(client_with_tenant):
    """When Origin matches the Host, the WS is allowed through."""
    client, api_key, _ = client_with_tenant
    # TestClient sets Host to "testserver" by default. Same-origin means
    # Origin: http://testserver (or https://testserver).
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
        headers={"origin": "http://testserver"},
    ) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "hello"


def test_ws_tenant_isolation_blocks_other_tenants_events(
    client_with_tenant, tmp_path: Path
):
    """A subscribing tenant only sees events whose tenant_id matches
    their own. Events from a different tenant are silently dropped.

    This is the security fix from the v0.7.1 review: a malicious or
    curious authenticated tenant must not be able to observe another
    tenant's run events.
    """
    client, api_key, _ = client_with_tenant
    # Add a second tenant to the same registry so we can publish
    # "foreign" events for them.
    registry = client.app.state.tenants
    other_key = registry.add("other-co")
    bus = client.app.state.runs.events
    with client.websocket_connect(
        "/dashboard/ws/runs/demo",
        cookies={"agentforge_api_key": api_key},
    ) as ws:
        ws.receive_text()  # hello
        # Foreign event first (should NOT reach this socket).
        bus.publish("r1", "demo", "other-co", "started", {"agent": "x"})
        # Own event next (SHOULD reach this socket).
        bus.publish("r2", "demo", "acme", "started", {"agent": "y"})
        msg = json.loads(ws.receive_text())
        assert msg["run_id"] == "r2"
        assert msg["kind"] == "started"
