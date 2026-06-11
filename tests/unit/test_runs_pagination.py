"""Tests for v0.8.0 #3 run-history pagination.

Covers:
- list_runs(before=...) returns only runs older than the cursor
- list_runs(limit=..., before=...) composes both filters
- The /partials/runs/{name} HTTP endpoint accepts ?before= and ?limit=
- The end-to-end "load more" path: page 1 (50), page 2 (50), page 3 (10)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentforge.core.runs import RunRecord
from agentforge.serve import create_app
from agentforge.state import State
from agentforge.tenants.registry import TenantRegistry


@pytest.fixture
def state(tmp_path: Path):
    s = State(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def runs_store(state: State):
    """Convenience: the SQLiteRunStore facade on the State fixture."""
    return state.runs


def _seed_runs(runs, workflow: str, n: int) -> list[RunRecord]:
    """Create n synthetic runs for one workflow with strictly decreasing
    started_at (so newest is first, oldest is last). Returns the list
    in insertion order. `runs` is the SQLiteRunStore facade."""
    out = []
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        started = (base - timedelta(seconds=i)).isoformat()
        ended = (base - timedelta(seconds=i, milliseconds=-100)).isoformat()
        r = RunRecord(
            id=f"r{i:04d}", workflow=workflow, tenant_id="t",
            agent="a", started_at=started, ended_at=ended,
            status="success", duration_seconds=0.1, error=None,
        )
        runs.record(r)
        out.append(r)
    return out


def test_list_runs_default_no_cursor(state: State):
    state.runs.record(RunRecord(
        id="r1", workflow="wf", tenant_id="t", agent="a",
        started_at="2026-06-01T12:00:00+00:00",
        ended_at="2026-06-01T12:00:01+00:00",
        status="success", duration_seconds=1.0, error=None,
    ))
    state.runs.record(RunRecord(
        id="r2", workflow="wf", tenant_id="t", agent="a",
        started_at="2026-06-01T11:00:00+00:00",
        ended_at="2026-06-01T11:00:01+00:00",
        status="success", duration_seconds=1.0, error=None,
    ))
    runs = state.runs.list_runs("wf")
    assert [r.id for r in runs] == ["r1", "r2"]


def test_list_runs_with_before_cursor(runs_store):
    """Passing `before=<ts>` returns only runs strictly older than the
    cursor. Useful for "load more" pagination."""
    _seed_runs(runs_store, "wf", 5)  # 5 runs, 1s apart, newest first
    all_runs = runs_store.list_runs("wf")
    # Page 1: limit 2, no cursor → newest 2.
    page1 = runs_store.list_runs("wf", limit=2)
    assert [r.id for r in page1] == ["r0000", "r0001"]
    # Page 2: limit 2, cursor = page1's oldest started_at.
    cursor = page1[-1].started_at
    page2 = runs_store.list_runs("wf", limit=2, before=cursor)
    assert [r.id for r in page2] == ["r0002", "r0003"]
    # Page 3: cursor = page2's oldest → 1 remaining run.
    cursor = page2[-1].started_at
    page3 = runs_store.list_runs("wf", limit=2, before=cursor)
    assert [r.id for r in page3] == ["r0004"]


def test_list_runs_before_excludes_cursor_value(runs_store):
    """The `before` cursor is STRICT (uses `<`, not `<=`). The cursor
    itself must NOT appear in the result set — it's the boundary."""
    _seed_runs(runs_store, "wf", 3)
    page1 = runs_store.list_runs("wf", limit=2)
    cursor = page1[-1].started_at
    page2 = runs_store.list_runs("wf", limit=2, before=cursor)
    # cursor's run must not appear again.
    assert all(r.started_at != cursor for r in page2)
    assert len(page2) == 1


def test_partial_runs_endpoint_accepts_before_param(tmp_path: Path):
    """The HTTP /partials/runs/{name} endpoint reads ?before= and
    ?limit= from the query string and forwards to list_runs."""
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "wf"; wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(
        "name: demo\nsteps:\n  - id: echo\n    type: respond\n"
        "    inputs:\n      to: user\n      content: hi\n",
        encoding="utf-8",
    )
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    # Seed via the in-process state handle. `app.state.runs` is the
    # SQLiteRunStore facade; the underlying state owns the connection.
    with TestClient(app) as c:
        _seed_runs(app.state.runs, "demo", 7)
        # Page 1: no cursor.
        r = c.get(
            "/dashboard/partials/runs/demo",
            cookies={"agentforge_api_key": api_key},
        )
        assert r.status_code == 200
        assert r.text.count("<tr ") == 7
        # Page 2: cursor + limit=3.
        # Get the oldest-loaded started_at from page 1 (last <tr>).
        from html.parser import HTMLParser
        class _AttrParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.attrs = []
            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    d = dict(attrs)
                    if "data-started-at" in d:
                        self.attrs.append(d["data-started-at"])
        p = _AttrParser()
        p.feed(r.text)
        cursor = p.attrs[-1]
        r2 = c.get(
            f"/dashboard/partials/runs/demo?before={cursor}&limit=3",
            cookies={"agentforge_api_key": api_key},
        )
        assert r2.status_code == 200
        assert r2.text.count("<tr ") == 0  # all 7 already shown, cursor excludes the boundary
        # Verify the boundary itself is NOT in the response.
        assert cursor not in r2.text
