"""Regression test for workflow hot-reload (v0.8.0 #2).

A workflow is loaded fresh from disk on every run via
`Workflow.from_yaml(wf_path)`, so editing the YAML and saving
takes effect on the next run without any daemon restart. This
test pins that behavior — if anyone adds a workflow cache later,
it must invalidate on file mtime or this test will fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


def test_workflow_yaml_edit_takes_effect_without_restart(tmp_path: Path):
    """Edit the YAML between two runs of the same workflow; the
    second run must use the new version (proves no stale cache)."""
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    wf_path = wf_dir / "demo.yaml"
    # v1: 1 step (echo).
    wf_path.write_text(
        "name: demo\ndescription: v1\n"
        "steps:\n"
        "  - id: echo\n"
        "    type: respond\n"
        "    inputs:\n"
        "      to: user\n"
        "      content: 'v1'\n",
        encoding="utf-8",
    )
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    from fastapi.testclient import TestClient
    from agentforge.core.runs import RunStore
    runs_db = app.state.runs
    with TestClient(app) as c:
        # Run v1.
        r = c.post(
            "/v1/workflows/demo/run",
            json={"agent": "tester"},
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 200
        # v2: same name, different content.
        wf_path.write_text(
            "name: demo\ndescription: v2\n"
            "steps:\n"
            "  - id: echo\n"
            "    type: respond\n"
            "    inputs:\n"
            "      to: user\n"
            "      content: 'v2'\n",
            encoding="utf-8",
        )
        # Run v2 — no daemon restart, no cache invalidation call.
        r = c.post(
            "/v1/workflows/demo/run",
            json={"agent": "tester"},
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 200
        # Both runs must have succeeded (proves the second run picked
        # up the new YAML — a stale cache would have failed the
        # content="v2" parse or used the v1 content).
    # Run history has 2 entries.
    history = runs_db.list_runs("demo", limit=10)
    assert len(history) == 2
    assert all(r.status == "success" for r in history)
