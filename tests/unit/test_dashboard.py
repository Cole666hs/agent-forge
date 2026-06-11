"""Tests for dashboard cookie auth + login form."""
from fastapi.testclient import TestClient
from pathlib import Path

from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


def test_dashboard_login_get_returns_form(tmp_path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/login")
    assert r.status_code == 200
    body = r.text.lower()
    assert "api_key" in body
    assert "<form" in body


def test_dashboard_login_post_with_valid_key_sets_cookie(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post(
        "/dashboard/login",
        data={"api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert "agentforge_api_key" in r.headers.get("set-cookie", "")


def test_dashboard_login_post_with_invalid_key_rejects(tmp_path):
    TenantRegistry(path=tmp_path / "tenants.json")  # empty
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post("/dashboard/login", data={"api_key": "fake-key"})
    assert r.status_code == 401


def test_dashboard_static_css_served(tmp_path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/static/dashboard.css")
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")
    assert len(r.text) > 100


def test_dashboard_overview_requires_auth(tmp_path):
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/")
    assert r.status_code == 401


def test_dashboard_overview_shows_tenant_and_quota(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    from agentforge.billing.usage import UsageStore
    UsageStore(path=tmp_path / "usage.json").record("acme", 42_000)
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/", cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "acme" in r.text
    assert "42,000" in r.text
    assert "free" in r.text
    assert "100,000" in r.text


def test_dashboard_tenants_lists_all_tenants(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    tenants.add("beta")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get("/dashboard/tenants", cookies={"agentforge_api_key": api_key})
    assert r.status_code == 200
    assert "acme" in r.text
    assert "beta" in r.text


def test_dashboard_tenants_create_form(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post(
        "/dashboard/tenants",
        data={"tenant_id": "newco"},
        cookies={"agentforge_api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    # v0.6.0: tenants live in SQLite (state.db) now, not in tenants.json.
    # The dashboard router reads from app.state.tenants which is the
    # SQLite handle. Use the app's own handle to assert.
    assert "newco" in app.state.tenants.list_tenants()


def test_dashboard_tenants_delete(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    tenants.add("victim")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post(
        "/dashboard/tenants/victim/delete",
        cookies={"agentforge_api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    # v0.6.0: read from the app's SQLite handle, not the JSON file
    assert "victim" not in app.state.tenants.list_tenants()


def test_dashboard_tenant_detail_shows_plan_switcher(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.get(
        "/dashboard/tenants/acme",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    assert "acme" in r.text
    assert "free" in r.text
    body = r.text.lower()
    assert "pro" in body
    assert "enterprise" in body


def test_dashboard_tenant_plan_switch(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
    )
    client = TestClient(app)
    r = client.post(
        "/dashboard/tenants/acme/plan",
        data={"plan": "pro"},
        cookies={"agentforge_api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    # v0.6.0: plan lives in SQLite now
    assert app.state.tenants.get_plan("acme").value == "pro"


def test_dashboard_workflows_lists_yaml_files(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "greet.yaml").write_text("name: greet\nsteps: []\n")
    (wf_dir / "summarize.yaml").write_text("name: summarize\nsteps: []\n")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    assert "greet" in r.text
    assert "summarize" in r.text


def test_dashboard_workflow_detail_shows_yaml(tmp_path):
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "greet.yaml").write_text("name: greet\ndescription: Says hello\nsteps: []\n")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/greet",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    assert "Says hello" in r.text
    assert "<form" in r.text.lower()
