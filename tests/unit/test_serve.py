"""Unit tests for agentforge.serve — FastAPI server with API-key auth.

Uses fastapi.testclient.TestClient to hit the app in-process. No
network, no real uvicorn — the lifespan is bypassed via fixture.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def app_setup(tmp_path: Path):
    """A server with one tenant registered, plus a mailbox + state dir."""
    tenants_path = tmp_path / "tenants.json"
    reg = TenantRegistry(path=tenants_path)
    api_key = reg.add("acme")  # tenant_id='acme', random key

    mailbox_root = tmp_path / "mailbox"
    state_db = tmp_path / "state.db"
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()

    app = create_app(
        tenants_path=tenants_path,
        mailbox_root=mailbox_root,
        state_db=state_db,
        workflows_dir=workflows_dir,
    )
    return app, {
        "tenant_id": "acme",
        "api_key": api_key,
        "mailbox_root": mailbox_root,
        "state_db": state_db,
        "workflows_dir": workflows_dir,
    }


# ---------------------------------------------------------------------------
# /health — no auth
# ---------------------------------------------------------------------------

def test_health_no_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /v1/* — requires X-API-Key
# ---------------------------------------------------------------------------

def test_v1_inbox_requires_api_key(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/v1/inbox", params={"agent": "bot"})
    assert r.status_code == 401


def test_v1_inbox_rejects_unknown_key(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get(
        "/v1/inbox", params={"agent": "bot"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert r.status_code == 401


def test_v1_inbox_returns_messages_for_tenant(app_setup):
    app, ctx = app_setup
    # Pre-seed a message in acme's mailbox
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mbox = FileMailbox(root=ctx["mailbox_root"], tenant_id="acme")
    mbox.send(Message(from_="alice", to="bot", content="hi"))

    client = TestClient(app)
    r = client.get(
        "/v1/inbox", params={"agent": "bot"},
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content"] == "hi"


def test_v1_inbox_isolates_tenants(app_setup):
    """Tenant A's API key can't see tenant B's messages — even via inbox."""
    app, ctx = app_setup
    # Add a second tenant + message in B's scope
    reg = TenantRegistry(path=ctx["mailbox_root"].parent / "tenants.json")
    api_key_b = reg.add("corp")
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    FileMailbox(root=ctx["mailbox_root"], tenant_id="corp").send(
        Message(from_="x", to="bot", content="for-corp")
    )

    # Use tenant A's key — should see no messages
    client = TestClient(app)
    r = client.get(
        "/v1/inbox", params={"agent": "bot"},
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_v1_post_message_writes_to_inbox(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={"to": "bot", "content": "via api"},
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 201
    assert r.json()["from"] == "acme"  # sent as the tenant
    # Verify it's in the mailbox
    from agentforge.core.mailbox import FileMailbox
    mbox = FileMailbox(root=ctx["mailbox_root"], tenant_id="acme")
    inbox = mbox.list_inbox("bot", include_read=True)
    assert len(inbox) == 1
    assert inbox[0].content == "via api"


def test_v1_post_message_requires_content(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={"to": "bot"},  # no content
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 422  # pydantic validation


def test_v1_run_workflow_executes_and_returns_state(app_setup):
    app, ctx = app_setup
    # Write a tiny workflow
    wf = ctx["workflows_dir"] / "echo.yaml"
    wf.write_text(textwrap.dedent("""
        name: echo
        steps:
          - id: receive
            type: receive
          - id: respond
            type: respond
            inputs:
              to: "{{ receive.from }}"
              content: "echo: {{ receive.content }}"
    """).strip())
    # Seed an inbox message
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mbox = FileMailbox(root=ctx["mailbox_root"], tenant_id="acme")
    mbox.send(Message(from_="user", to="bot", content="hello"))

    client = TestClient(app)
    r = client.post(
        "/v1/workflows/echo/run",
        json={"agent": "bot"},
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["state_keys"] == ["receive", "respond"]
    # The response should be in user's inbox
    user_inbox = mbox.list_inbox("user", include_read=True)
    assert any("echo: hello" in m.content for m in user_inbox)


def test_v1_run_workflow_unknown_workflow_404(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/v1/workflows/nonexistent/run",
        json={"agent": "bot"},
        headers={"X-API-Key": ctx["api_key"]},
    )
    assert r.status_code == 404
