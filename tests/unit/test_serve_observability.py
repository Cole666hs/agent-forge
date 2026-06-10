"""T9+T10+T11 RED — /metrics, /readyz, RequestIdMiddleware test."""

from pathlib import Path

from fastapi.testclient import TestClient

from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


def _build_app(tmp_path, mailbox_writable=True):
    tenants = tmp_path / "tenants.json"
    if mailbox_writable:
        (tmp_path / "mailbox").mkdir()
    return create_app(
        tenants_path=tenants,
        mailbox_root=tmp_path / "mailbox",
    )


def _register_tenant(tmp_path, name="acme") -> str:
    """Pre-register a tenant in tenants.json and return the API key."""
    reg = TenantRegistry(path=tmp_path / "tenants.json")
    return reg.add(name)


# T9: /metrics

def test_metrics_endpoint_returns_prometheus_text(tmp_path):
    app = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # Empty registry → just empty body, but if any metrics exist they have HELP
    assert resp.text == "" or "# HELP" in resp.text


def test_metrics_endpoint_records_send(tmp_path):
    api_key = _register_tenant(tmp_path, "acme")
    app = _build_app(tmp_path)
    client = TestClient(app)
    # Send a message
    client.post(
        "/v1/messages",
        json={"to": "b", "content": "hi", "intent": "respond"},
        headers={"X-API-Key": api_key},
    )
    # /metrics should show the mailbox counter
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "agentforge_mailbox_messages_total" in resp.text
    assert 'tenant="acme"' in resp.text


def test_metrics_endpoint_no_auth(tmp_path):
    """Same as /health — no API key required."""
    app = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200


# T10: /readyz

def test_readyz_returns_200_when_dependencies_ok(tmp_path):
    _register_tenant(tmp_path, "acme")  # creates tenants.json
    app = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"


def test_readyz_returns_503_when_mailbox_unwritable(tmp_path):
    app = _build_app(tmp_path, mailbox_writable=False)
    # The mailbox_root doesn't exist (we didn't mkdir it)
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert any("mailbox" in r.lower() for r in body["reasons"])


# T11: RequestIdMiddleware

def test_request_id_middleware_generates_when_missing(tmp_path):
    app = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/health")
    rid = resp.headers.get("x-request-id")
    assert rid is not None
    assert rid.startswith("req_")


def test_request_id_middleware_echoes_inbound_header(tmp_path):
    app = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/health", headers={"X-Request-Id": "trace-abc"})
    assert resp.headers.get("x-request-id") == "trace-abc"
