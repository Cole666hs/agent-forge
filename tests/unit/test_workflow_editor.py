"""Tests for the workflow editor (v0.5.2 — Phase 10)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
        "mailbox_root": mailbox_root, "workflows_dir": workflows_dir,
    }


# ---------------------------------------------------------------------------
# New workflow
# ---------------------------------------------------------------------------

def test_new_workflow_get_returns_form(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/new",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    body = r.text.lower()
    assert "name" in body
    assert "<textarea" in body
    assert "<form" in body


def test_new_workflow_get_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/workflows/new")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Create / save (POST)
# ---------------------------------------------------------------------------

def test_post_new_workflow_creates_file(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    yaml_content = "name: greet\nsteps: []\n"
    r = client.post(
        "/dashboard/workflows/greet",
        data={"yaml_content": yaml_content},
        cookies={"agentforge_api_key": ctx["api_key"]},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    # File exists on disk
    assert (ctx["workflows_dir"] / "greet.yaml").exists()
    assert (ctx["workflows_dir"] / "greet.yaml").read_text() == yaml_content


def test_post_workflow_with_invalid_yaml_returns_400(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    bad_yaml = "name: [broken\n  not valid: yaml : :\n"
    r = client.post(
        "/dashboard/workflows/broken",
        data={"yaml_content": bad_yaml},
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 400
    # File not created
    assert not (ctx["workflows_dir"] / "broken.yaml").exists()
    # Error in response body
    assert "yaml" in r.text.lower() or "error" in r.text.lower()


def test_post_workflow_with_empty_yaml_returns_400(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/dashboard/workflows/empty",
        data={"yaml_content": ""},
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 400


def test_post_workflow_overwrites_existing(app_setup):
    app, ctx = app_setup
    (ctx["workflows_dir"] / "greet.yaml").write_text("name: greet\nsteps: []\n")
    client = TestClient(app)
    new_yaml = "name: greet\ndescription: updated\nsteps: []\n"
    r = client.post(
        "/dashboard/workflows/greet",
        data={"yaml_content": new_yaml},
        cookies={"agentforge_api_key": ctx["api_key"]},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    assert (ctx["workflows_dir"] / "greet.yaml").read_text() == new_yaml


def test_post_workflow_requires_auth(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/dashboard/workflows/anon",
        data={"yaml_content": "name: anon\nsteps: []\n"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def test_edit_workflow_get_returns_form_with_current_yaml(app_setup):
    app, ctx = app_setup
    (ctx["workflows_dir"] / "greet.yaml").write_text("name: greet\ndescription: says hi\nsteps: []\n")
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/greet/edit",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    # The current YAML should be in the textarea
    assert "name: greet" in r.text
    assert "description: says hi" in r.text
    assert "<textarea" in r.text


def test_edit_workflow_get_nonexistent_returns_404(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/ghost/edit",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 404


def test_edit_workflow_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/workflows/anything/edit")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_workflow_removes_file(app_setup):
    app, ctx = app_setup
    (ctx["workflows_dir"] / "victim.yaml").write_text("name: victim\nsteps: []\n")
    client = TestClient(app)
    r = client.post(
        "/dashboard/workflows/victim/delete",
        cookies={"agentforge_api_key": ctx["api_key"]},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 200)
    assert not (ctx["workflows_dir"] / "victim.yaml").exists()


def test_delete_workflow_nonexistent_is_safe(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/dashboard/workflows/ghost/delete",
        cookies={"agentforge_api_key": ctx["api_key"]},
        follow_redirects=False,
    )
    # Either 404 or 302 (redirects to /workflows) — both acceptable
    assert r.status_code in (302, 303, 404)


def test_delete_workflow_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.post("/dashboard/workflows/x/delete")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Workflows list page links to new + edit
# ---------------------------------------------------------------------------

def test_workflows_list_has_create_button(app_setup):
    app, ctx = app_setup
    (ctx["workflows_dir"] / "greet.yaml").write_text("name: greet\nsteps: []\n")
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert "/dashboard/workflows/new" in r.text
    assert "/dashboard/workflows/greet/edit" in r.text
    assert "/dashboard/workflows/greet/delete" in r.text
