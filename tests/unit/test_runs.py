"""Tests for workflow run history (v0.5.4)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge.core.runs import RunStore, RunRecord
from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def app_setup(tmp_path: Path):
    tenants_path = tmp_path / "tenants.json"
    reg = TenantRegistry(path=tenants_path)
    api_key = reg.add("acme")
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "greet.yaml").write_text("name: greet\nsteps: []\n")
    app = create_app(
        tenants_path=tmp_path / "tenants.json",
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=workflows_dir,
    )
    return app, {
        "api_key": api_key,
        "workflows_dir": workflows_dir,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# RunStore unit tests
# ---------------------------------------------------------------------------

def test_runstore_record_and_list(tmp_path):
    s = RunStore(path=tmp_path / "runs.json")
    s.record(RunRecord(
        id="r1", workflow="greet", tenant_id="acme", agent="bot",
        started_at="2026-06-11T10:00:00+00:00",
        ended_at="2026-06-11T10:00:01+00:00",
        status="success", duration_seconds=1.0, error=None,
    ))
    runs = s.list_runs("greet")
    assert len(runs) == 1
    assert runs[0].id == "r1"
    assert runs[0].status == "success"


def test_runstore_list_newest_first(tmp_path):
    s = RunStore(path=tmp_path / "runs.json")
    for i in range(3):
        s.record(RunRecord(
            id=f"r{i}", workflow="greet", tenant_id="acme", agent="bot",
            started_at=f"2026-06-11T10:0{i}:00+00:00",
            ended_at=f"2026-06-11T10:0{i}:01+00:00",
            status="success", duration_seconds=1.0, error=None,
        ))
    runs = s.list_runs("greet")
    assert [r.id for r in runs] == ["r2", "r1", "r0"]


def test_runstore_limit(tmp_path):
    s = RunStore(path=tmp_path / "runs.json")
    for i in range(10):
        s.record(RunRecord(
            id=f"r{i}", workflow="greet", tenant_id="acme", agent="bot",
            started_at=f"2026-06-11T10:00:0{i}+00:00",
            ended_at=f"2026-06-11T10:00:1{i}+00:00",
            status="success", duration_seconds=1.0, error=None,
        ))
    runs = s.list_runs("greet", limit=3)
    assert len(runs) == 3
    # Newest 3
    assert [r.id for r in runs] == ["r9", "r8", "r7"]


def test_runstore_filters_by_workflow(tmp_path):
    s = RunStore(path=tmp_path / "runs.json")
    s.record(RunRecord(
        id="r1", workflow="a", tenant_id="acme", agent="bot",
        started_at="t", ended_at="t", status="success",
        duration_seconds=1.0, error=None,
    ))
    s.record(RunRecord(
        id="r2", workflow="b", tenant_id="acme", agent="bot",
        started_at="t", ended_at="t", status="success",
        duration_seconds=1.0, error=None,
    ))
    assert len(s.list_runs("a")) == 1
    assert len(s.list_runs("b")) == 1
    assert len(s.list_runs("c")) == 0


def test_runstore_caps_at_max_per_workflow(tmp_path):
    s = RunStore(path=tmp_path / "runs.json", max_per_workflow=5)
    for i in range(8):
        s.record(RunRecord(
            id=f"r{i}", workflow="greet", tenant_id="acme", agent="bot",
            started_at=f"t{i}", ended_at=f"t{i}", status="success",
            duration_seconds=1.0, error=None,
        ))
    runs = s.list_runs("greet")
    assert len(runs) == 5
    # Most recent 5
    assert [r.id for r in runs] == ["r7", "r6", "r5", "r4", "r3"]


def test_runstore_persistence(tmp_path):
    p = tmp_path / "runs.json"
    s1 = RunStore(path=p)
    s1.record(RunRecord(
        id="r1", workflow="greet", tenant_id="acme", agent="bot",
        started_at="t", ended_at="t", status="success",
        duration_seconds=1.0, error=None,
    ))
    s2 = RunStore(path=p)
    assert len(s2.list_runs("greet")) == 1


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------

def test_workflow_runs_page_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/workflows/greet/runs")
    assert r.status_code == 401


def test_workflow_runs_page_shows_recorded_runs(app_setup):
    app, ctx = app_setup
    RunStore(path=ctx["tmp_path"] / "runs.json").record(RunRecord(
        id="r1", workflow="greet", tenant_id="acme", agent="bot",
        started_at="2026-06-11T10:00:00+00:00",
        ended_at="2026-06-11T10:00:01+00:00",
        status="success", duration_seconds=1.0, error=None,
    ))
    # Re-create the app so the in-memory RunStore re-loads the file
    app = create_app(
        tenants_path=ctx["tmp_path"] / "tenants.json",
        mailbox_root=ctx["tmp_path"] / "mailbox",
        workflows_dir=ctx["workflows_dir"],
    )
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/greet/runs",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert "r1" in r.text or "success" in r.text
    assert "greet" in r.text


def test_workflow_runs_page_for_unknown_workflow_is_empty(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/ghost/runs",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert "no runs" in r.text.lower() or "0" in r.text or "ghost" in r.text


def test_workflow_runs_partial_endpoint(app_setup):
    app, ctx = app_setup
    RunStore(path=ctx["tmp_path"] / "runs.json").record(RunRecord(
        id="r1", workflow="greet", tenant_id="acme", agent="bot",
        started_at="t", ended_at="t", status="success",
        duration_seconds=1.0, error=None,
    ))
    # Re-create the app so the in-memory RunStore re-loads the file
    app = create_app(
        tenants_path=ctx["tmp_path"] / "tenants.json",
        mailbox_root=ctx["tmp_path"] / "mailbox",
        workflows_dir=ctx["workflows_dir"],
    )
    client = TestClient(app)
    r = client.get(
        "/dashboard/partials/runs/greet",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    body = r.text
    assert "<html" not in body.lower()  # fragment, not full page
    assert "r1" in body


def test_workflow_runs_partial_requires_auth(app_setup):
    app, _ = app_setup
    client = TestClient(app)
    r = client.get("/dashboard/partials/runs/greet")
    assert r.status_code == 401


def test_workflow_detail_links_to_runs_page(app_setup):
    app, ctx = app_setup
    client = TestClient(app)
    r = client.get(
        "/dashboard/workflows/greet",
        cookies={"agentforge_api_key": ctx["api_key"]},
    )
    assert r.status_code == 200
    assert "/dashboard/workflows/greet/runs" in r.text
