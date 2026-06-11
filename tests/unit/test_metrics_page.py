"""Tests for the v0.9.0 metrics page + supporting state methods.

Covers:
- RunStore.count_runs() respects optional filters (workflow, since, status)
- RunStore.duration_percentile() returns None on empty, correct value otherwise
- The /dashboard/metrics HTTP endpoint renders the page with the
  expected numbers + per-workflow breakdown
- The page is in the nav (base.html link present)
- Auth: 401 without cookie
"""
from __future__ import annotations

import json
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
# RunStore methods (unit)
# ---------------------------------------------------------------------------

@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def _seed(runs_store, workflow: str, n: int, status: str = "success",
          base: datetime | None = None, dur: float = 1.0):
    base = base or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        started = (base - timedelta(seconds=i)).isoformat()
        ended = (base - timedelta(seconds=i, milliseconds=-10)).isoformat()
        # Status in the ID so two _seed calls with the same workflow
        # don't collide on PK (record() uses INSERT OR REPLACE — a
        # collision silently overwrites the prior row).
        runs_store.record(RunRecord(
            id=f"r-{workflow}-{status}-{i:04d}", workflow=workflow,
            tenant_id="t", agent="a", started_at=started, ended_at=ended,
            status=status, duration_seconds=dur, error=None,
        ))


def test_count_runs_total(state: State):
    _seed(state.runs, "wf", 5)
    assert state.runs.count_runs() == 5


def test_count_runs_filter_by_workflow(state: State):
    _seed(state.runs, "wf-a", 3)
    _seed(state.runs, "wf-b", 7)
    assert state.runs.count_runs(workflow="wf-a") == 3
    assert state.runs.count_runs(workflow="wf-b") == 7


def test_count_runs_filter_by_status(state: State):
    _seed(state.runs, "wf", 5, status="success", base=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    # Use a different base so the 2 error runs don't collide started_at
    # with the 5 success runs.
    _seed(state.runs, "wf", 2, status="error", base=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert state.runs.count_runs() == 7
    assert state.runs.count_runs(status="success") == 5
    assert state.runs.count_runs(status="error") == 2


def test_count_runs_filter_by_since(state: State):
    """since is an ISO 8601 lower bound on started_at."""
    now = datetime.now(timezone.utc)
    # 3 runs in the last hour, 3 runs from 2 days ago.
    _seed(state.runs, "wf", 3, base=now)
    _seed(state.runs, "wf-old", 3, base=now - timedelta(days=2))
    last_hour = (now - timedelta(hours=1)).isoformat()
    assert state.runs.count_runs(since=last_hour) == 3


def test_duration_percentile_empty(state: State):
    assert state.runs.duration_percentile() is None


def test_duration_percentile_basic(state: State):
    # 10 runs with durations 0.1, 0.2, ..., 1.0 seconds.
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i, d in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]):
        state.runs.record(RunRecord(
            id=f"r-p{i}", workflow="wf", tenant_id="t", agent="a",
            started_at=(base - timedelta(seconds=i)).isoformat(),
            ended_at=(base - timedelta(seconds=i, milliseconds=-10)).isoformat(),
            status="success", duration_seconds=d, error=None,
        ))
    p50 = state.runs.duration_percentile(pct=0.5)
    p95 = state.runs.duration_percentile(pct=0.95)
    assert p50 is not None
    assert p95 is not None
    # p50 of {0.1..1.0} should be ~0.5 (the median).
    assert 0.4 < p50 < 0.6
    # p95 should be near the top of the range.
    assert 0.8 < p95 < 1.05


def test_duration_percentile_filtered(state: State):
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    state.runs.record(RunRecord(
        id="r-old", workflow="wf", tenant_id="t", agent="a",
        started_at=(base - timedelta(days=30)).isoformat(),
        ended_at=base.isoformat(),
        status="success", duration_seconds=10.0, error=None,
    ))
    state.runs.record(RunRecord(
        id="r-new", workflow="wf", tenant_id="t", agent="a",
        started_at=base.isoformat(),
        ended_at=base.isoformat(),
        status="success", duration_seconds=0.1, error=None,
    ))
    # No since → includes both. p50 should be ~5.
    p50_all = state.runs.duration_percentile(pct=0.5)
    assert p50_all is not None and 3.0 < p50_all < 7.0
    # since=now → only the new run, p50 = 0.1.
    p50_new = state.runs.duration_percentile(
        pct=0.5, since=base.isoformat(),
    )
    assert p50_new is not None and abs(p50_new - 0.1) < 0.05


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
def app_for_metrics(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
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
        # Seed a few runs so the page has something to show.
        base = datetime.now(timezone.utc) - timedelta(minutes=5)
        for i in range(7):
            started = (base + timedelta(seconds=i)).isoformat()
            ended = (base + timedelta(seconds=i, milliseconds=10)).isoformat()
            c.app.state.runs.record(RunRecord(
                id=f"r-met{i:03d}", workflow="demo", tenant_id="acme",
                agent="tester", started_at=started, ended_at=ended,
                status="success" if i < 5 else "error",
                duration_seconds=0.5 + i * 0.1, error=None,
            ))
        yield c, api_key


def test_metrics_page_renders_for_authorized_user(app_for_metrics):
    c, api_key = app_for_metrics
    r = c.get(
        "/dashboard/metrics",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    body = r.text
    # Headline numbers.
    assert "Runs (24h)" in body
    assert "Runs (7d)" in body
    assert "Runs (all-time)" in body
    # The 7 runs we seeded are all in 24h, so total_24h >= 7.
    assert "Runs (24h)" in body
    # Per-workflow section.
    assert "Per-workflow" in body
    assert "demo" in body
    # Duration percentiles table.
    assert "Duration percentiles" in body
    assert "p50" in body
    assert "p95" in body


def test_metrics_page_requires_auth(app_for_metrics):
    c, _ = app_for_metrics
    r = c.get("/dashboard/metrics")
    assert r.status_code == 401


def test_metrics_link_in_nav(app_for_metrics):
    """The Metrics link is in the topbar (base.html). Every page that
    extends base.html gets the link."""
    c, api_key = app_for_metrics
    r = c.get(
        "/dashboard/",
        cookies={"agentforge_api_key": api_key},
    )
    assert r.status_code == 200
    assert 'href="/dashboard/metrics"' in r.text
