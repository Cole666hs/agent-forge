"""Tests for the v0.9.0 run-detail page + supporting EventBus method.

Covers:
- EventBus.events_for_run(run_id) returns events in seq order
- RunStore.get_run(run_id) returns the matching RunRecord, or None
- The /workflows/{name}/runs/{run_id} HTTP endpoint renders the
  detail page (200) for a valid run, 404 for unknown
- The detail page lists all events chronologically
- The ID cell in the runs list is now a clickable link
- The endpoint requires auth (401 without cookie)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentforge.core.runs import RunRecord
from agentforge.serve import create_app
from agentforge.state import State
from agentforge.tenants.registry import TenantRegistry


# ---------------------------------------------------------------------------
# EventBus.events_for_run
# ---------------------------------------------------------------------------

@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def test_events_for_run_returns_in_seq_order(state: State):
    bus = state.events
    bus.publish("r1", "wf", "t", "started")
    bus.publish("r1", "wf", "t", "step1_done", {"step": 1})
    bus.publish("r1", "wf", "t", "step2_done", {"step": 2})
    bus.publish("r1", "wf", "t", "finished", {"ok": True})
    events = bus.events_for_run("r1")
    assert [e.kind for e in events] == [
        "started", "step1_done", "step2_done", "finished",
    ]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)  # monotonic


def test_events_for_run_excludes_other_runs(state: State):
    bus = state.events
    bus.publish("r1", "wf", "t", "started")
    bus.publish("r2", "wf", "t", "started")
    bus.publish("r1", "wf", "t", "finished")
    events = bus.events_for_run("r1")
    assert {e.run_id for e in events} == {"r1"}
    assert [e.kind for e in events] == ["started", "finished"]


def test_events_for_run_empty(state: State):
    bus = state.events
    assert bus.events_for_run("nope") == []


# ---------------------------------------------------------------------------
# RunStore.get_run
# ---------------------------------------------------------------------------

def test_get_run_returns_record(state: State):
    state.runs.record(RunRecord(
        id="r-test", workflow="wf", tenant_id="t", agent="a",
        started_at="2026-06-01T12:00:00+00:00",
        ended_at="2026-06-01T12:00:01+00:00",
        status="success", duration_seconds=1.0, error=None,
    ))
    r = state.runs.get_run("r-test")
    assert r is not None
    assert r.id == "r-test"
    assert r.workflow == "wf"
    assert r.status == "success"


def test_get_run_returns_none_for_missing(state: State):
    assert state.runs.get_run("does-not-exist") is None


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

_YAML_DEMO = """name: demo
steps:
  - id: echo
    type: respond
    inputs:
      to: user
      content: hi
"""


@pytest.fixture
def app_with_run(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "wf"; wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(_YAML_DEMO, encoding="utf-8")
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    with TestClient(app) as c:
        # Record a run + events directly (bypassing the workflow
        # engine to keep the test focused on the detail page).
        c.app.state.runs.record(RunRecord(
            id="r-detail", workflow="demo", tenant_id="acme", agent="tester",
            started_at="2026-06-01T12:00:00+00:00",
            ended_at="2026-06-01T12:00:01+00:00",
            status="success", duration_seconds=1.0, error=None,
        ))
        bus = c.app.state.runs.events
        bus.publish("r-detail", "demo", "acme", "started",
                    payload={"agent": "tester"})
        bus.publish("r-detail", "demo", "acme", "finished",
                    payload={"status": "success", "duration_seconds": 1.0})
        yield c, api_key


def test_run_detail_renders_for_existing_run(app_with_run):
    c, api_key = app_with_run
    r = c.get(
        "/dashboard/workflows/demo/runs/r-detail",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    body = r.text
    # Run metadata is present.
    assert "r-detail" in body
    assert "tester" in body
    assert "success" in body
    # The event timeline lists both events.
    assert "started" in body
    assert "finished" in body
    # And the run-info card.
    assert "Run info" in body
    assert "Event timeline" in body


def test_run_detail_returns_404_for_unknown_run(app_with_run):
    c, api_key = app_with_run
    r = c.get(
        "/dashboard/workflows/demo/runs/does-not-exist",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_run_detail_requires_auth(app_with_run):
    c, _ = app_with_run
    r = c.get("/dashboard/workflows/demo/runs/r-detail")
    assert r.status_code == 401


def test_runs_list_id_cell_is_clickable_link(app_with_run, tmp_path: Path):
    """v0.9.0: the run id in the runs list is a link to the detail
    page, not plain text. Click → land on the detail page."""
    c, api_key = app_with_run
    # The runs list partial should render the id as an <a> tag.
    r = c.get(
        "/dashboard/partials/runs/demo",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    assert '/dashboard/workflows/demo/runs/r-detail' in r.text
    assert '<a href="/dashboard/workflows/demo/runs/r-detail">r-detail</a>' in r.text
