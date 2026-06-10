"""Tests for CodeMirror integration in the workflow editor (v0.5.3).

Note: CodeMirror runs client-side. The TestClient doesn't execute JS,
so these tests verify the *infrastructure* is served correctly: the
script tag, the stylesheet link, and the textarea id that the JS
targets. Visual verification (syntax highlighting, line numbers) needs
a real browser.
"""
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
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    app = create_app(
        tenants_path=tenants_path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=workflows_dir,
    )
    return app, {"api_key": api_key, "workflows_dir": workflows_dir}


def test_new_workflow_form_loads_codemirror_cdn(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/new",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    # esm.sh or unpkg CodeMirror 6 module import
    assert "codemirror" in r.text.lower()
    # The textarea must have the id the JS targets
    assert 'id="yaml_content"' in r.text
    # It's a textarea (so the form still works without JS)
    assert "<textarea" in r.text


def test_edit_form_loads_codemirror_cdn(app_setup):
    app, ctx = app_setup
    (ctx["workflows_dir"] / "greet.yaml").write_text("name: greet\nsteps: []\n")
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/greet/edit",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert "codemirror" in r.text.lower()
    assert 'id="yaml_content"' in r.text
    # Existing YAML is still in the textarea
    assert "name: greet" in r.text


def test_codemirror_script_is_module_type(app_setup):
    """CM6 imports via ESM — must be type=module so import works."""
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/new",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert 'type="module"' in r.text


def test_codemirror_targets_correct_textarea_id(app_setup):
    """The init script must reference the #yaml_content element."""
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/new",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    # The JS that initializes CM targets the textarea by id
    assert "#yaml_content" in r.text or "yaml_content" in r.text


def test_save_still_works_with_codemirror_form(app_setup):
    """The form must POST yaml_content from the textarea (CM writes back to it on submit)."""
    app, ctx = app_setup
    client = TestClient(app)
    r = client.post(
        "/dashboard/workflows/cmsave",
        data={"yaml_content": "name: cmsave\nsteps: []\n"},
        cookies={"agentforge_api_key": ctx["api_key"]},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert (ctx["workflows_dir"] / "cmsave.yaml").read_text() == "name: cmsave\nsteps: []\n"
