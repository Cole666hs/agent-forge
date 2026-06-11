"""Tests for real-time dashboard updates via HTMX polling."""
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


# ---------------------------------------------------------------------------
# Partials endpoints — return HTML fragments, not full pages
# ---------------------------------------------------------------------------

def test_usage_partial_returns_html_fragment(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/partials/usage",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    body = r.text
    # Fragment, not a full page — no <html> or <body> tags
    assert "<html" not in body.lower()
    assert "<body" not in body.lower()
    # Has the quota bar
    assert "quota-bar" in body
    # Has the metric info
    assert "tokens" in body.lower()


def test_usage_partial_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/partials/usage")
    assert r.status_code == 401


def test_usage_partial_reflects_current_usage(app_setup):
    app, ctx = app_setup
    # v0.6.0: usage lives in SQLite now
    app.state.usage.record("acme", 50_000)
    client = TestClient(app)
    r = client.get(
        "/dashboard/partials/usage",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    # 50% of 100,000 free limit
    assert "50,000" in r.text


def test_tenants_partial_returns_table_rows(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/partials/tenants",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    body = r.text
    # No layout, just table rows
    assert "<html" not in body.lower()
    assert "<table" not in body.lower()  # just <tr> rows, no <table> wrapper
    # Each tenant is a row
    assert "<tr>" in body
    assert "acme" in body


def test_tenants_partial_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/partials/tenants")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# HTMX polling attrs are present in the rendered full pages
# ---------------------------------------------------------------------------

def test_overview_page_has_polling_attrs_on_quota_card(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/", cookies={"agentforge_api_key": ctx["api_key"]})
    assert r.status_code == 200
    body = r.text
    # The quota card must poll for updates
    assert 'hx-get="/dashboard/partials/usage"' in body
    assert "hx-trigger=" in body
    # The trigger should mention an interval (e.g. every 5s)
    assert "every" in body


def test_tenants_page_has_polling_attrs_on_table_body(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/tenants", cookies={"agentforge_api_key": ctx["api_key"]})
    assert r.status_code == 200
    body = r.text
    assert 'hx-get="/dashboard/partials/tenants"' in body
    assert "hx-trigger=" in body
