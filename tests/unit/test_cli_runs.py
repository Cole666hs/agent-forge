"""Tests for v0.9.0 CLI runs subcommands (ls, show).

Covers:
- `agentforge runs ls <workflow>` prints an ASCII table of runs.
- Filtering by --status works.
- Empty result prints a friendly "(no runs...)" message.
- `agentforge runs show <run_id>` prints the run record + events.
- Unknown run_id → exit code 1, error message to stderr.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentforge import cli as cli_mod
from agentforge.core.runs import RunRecord
from agentforge.state import State


@pytest.fixture
def cli_env(tmp_path: Path):
    """A tmp dir with a state.db pre-seeded with 3 runs for one
    workflow. Returns (Path to state.db, expected workflow name,
    expected run_ids)."""
    db = tmp_path / "state.db"
    state = State(db)
    state.tenants.add("acme")
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    rids = []
    for i, (status, dur) in enumerate([
        ("success", 0.5), ("error", 1.2), ("cancelled", 0.1),
    ]):
        started = (base - timedelta(seconds=i)).isoformat()
        ended = (base - timedelta(seconds=i, milliseconds=-100)).isoformat()
        rid = f"r-test{i:04d}"
        rids.append(rid)
        state.runs.record(RunRecord(
            id=rid, workflow="demo", tenant_id="acme", agent="tester",
            started_at=started, ended_at=ended, status=status,
            duration_seconds=dur, error=None if status == "success" else "boom",
        ))
        # Add an event so `show` has something to render.
        state.runs.events.publish(
            run_id=rid, workflow="demo", tenant_id="acme",
            kind="started" if i == 0 else "finished",
            payload={"status": status, "i": i},
        )
    state.close()
    return db, "demo", rids


def _run(args, state_db: Path) -> subprocess.CompletedProcess:
    """Invoke the agentforge CLI as a subprocess (uses the click
    test runner machinery)."""
    return subprocess.run(
        [sys.executable, "-m", "agentforge.cli",
         "--state-db", str(state_db), *args],
        capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
    )


# Project root, derived from this file's location. The CLI is run as
# a subprocess via `python -m agentforge.cli` and needs to be invoked
# from a directory where the `agentforge` package is importable. The
# hardcoded `/home/cole/Developer/agent-forge` that used to live
# here broke in CI on day 1 — the runner's checkout path is
# `/home/runner/work/agent-forge/agent-forge`, not the developer's
# local path. Resolving from __file__ works on every host.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_runs_ls_prints_ascii_table(cli_env):
    db, wf, rids = cli_env
    r = _run(["runs", "ls", wf], db)
    assert r.returncode == 0, r.stderr
    body = r.stdout
    # Header row.
    assert "run_id" in body
    assert "agent" in body
    assert "status" in body
    # All three run_ids are present.
    for rid in rids:
        assert rid in body


def test_runs_ls_status_filter(cli_env):
    db, wf, _ = cli_env
    r = _run(["runs", "ls", wf, "--status", "success"], db)
    assert r.returncode == 0, r.stderr
    # Only the success row (r-test0000) should be present.
    assert "r-test0000" in r.stdout
    # The other two are filtered out.
    assert "r-test0001" not in r.stdout
    assert "r-test0002" not in r.stdout


def test_runs_ls_empty(cli_env):
    db, _, _ = cli_env
    r = _run(["runs", "ls", "no-such-workflow"], db)
    assert r.returncode == 0, r.stderr
    assert "(no runs" in r.stdout


def test_runs_show_prints_run_and_events(cli_env):
    db, wf, rids = cli_env
    r = _run(["runs", "show", rids[0]], db)
    assert r.returncode == 0, r.stderr
    body = r.stdout
    # Run header fields.
    assert rids[0] in body
    assert "workflow: demo" in body
    assert "agent:    tester" in body
    assert "status:   success" in body
    # Event timeline header.
    assert "event timeline" in body
    # The started event we published.
    assert "started" in body


def test_runs_show_unknown_run_returns_error(cli_env):
    db, _, _ = cli_env
    r = _run(["runs", "show", "does-not-exist"], db)
    assert r.returncode == 1
    assert "not found" in r.stderr
