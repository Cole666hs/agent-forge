"""Tests for v0.10.0 CLI `runs cancel` subcommand.

The CLI does two things:
  1. Looks up the run_id in the local state.db to find its workflow.
  2. POSTs to the daemon's cancel endpoint with X-API-Key.

We use click's CliRunner (in-process) so we can mock the `requests.post`
call directly. Subprocess tests would need to inject mocks via
PYTHONPATH/env vars — more machinery than the feature deserves.

Covers:
- Happy path: 200 → "cancellation requested" + exit 0
- 404 (run already finished): "not active" + exit 0
- 403 (cross-tenant): "not owned" + exit 1
- 401 (bad key): "API key rejected" + exit 1
- 500 (daemon error): exit 2 + body surfaced
- Daemon unreachable (ConnectionError): exit 2
- Unknown run_id: exit 1 before any HTTP call
- Missing API key: exit 1 before any HTTP call
- The CLI looks up the workflow from the run record (not from args)
- $AGENTFORGE_API_KEY env var works
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agentforge.cli import cli
from agentforge.core.runs import RunRecord
from agentforge.state import State


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """A tmp state.db with one run for tenant 'acme' / workflow 'demo'."""
    db = tmp_path / "state.db"
    state = State(db)
    state.tenants.add("acme")
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    state.runs.record(RunRecord(
        id="r-cancel00001", workflow="demo", tenant_id="acme", agent="tester",
        started_at=base.isoformat(), ended_at=base.isoformat(),
        status="success", duration_seconds=0.5, error=None,
    ))
    state.close()
    return db


def _invoke(args, state_db: Path, env: dict | None = None):
    """Run the click CLI in-process. `env` overrides process env for the call."""
    runner = CliRunner()
    return runner.invoke(cli, ["--state-db", str(state_db), *args], env=env or {})


class _FakeResponse:
    """Minimal stand-in for requests.Response."""
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.text = body


# Patch where the CLI uses requests, not where it's defined. The CLI
# imports `requests as _req` inside the function, so the local
# reference resolves to the same module that the test patches.
# (Python imports are cached; a second `import requests` returns the
# same module object.)
REQUESTS_POST = "requests.post"


def test_runs_cancel_happy_path(state_db: Path):
    """200 → 'cancellation requested' on stdout, exit 0."""
    with patch(REQUESTS_POST,
               return_value=_FakeResponse(200, '{"cancelled": true}')):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 0, r.output
    assert "cancellation requested" in r.output
    assert "r-cancel00001" in r.output


def test_runs_cancel_404_already_finished(state_db: Path):
    """404 → 'not active' on stdout, exit 0 (desired state reached)."""
    with patch(REQUESTS_POST,
               return_value=_FakeResponse(404, "not in flight")):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 0, r.output
    assert "not active" in r.output


def test_runs_cancel_403_cross_tenant(state_db: Path):
    """403 → 'not owned' on output, exit 1."""
    with patch(REQUESTS_POST,
               return_value=_FakeResponse(403, "forbidden")):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 1
    # click 8: error output goes to output, not a separate stream
    assert "not owned" in r.output


def test_runs_cancel_401_bad_key(state_db: Path):
    """401 → 'API key rejected', exit 1."""
    with patch(REQUESTS_POST,
               return_value=_FakeResponse(401, "unauthorized")):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 1
    assert "API key rejected" in r.output


def test_runs_cancel_500_unexpected(state_db: Path):
    """500 → exit 2 + body surfaced."""
    with patch(REQUESTS_POST,
               return_value=_FakeResponse(500, "kaboom")):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 2
    assert "500" in r.output
    assert "kaboom" in r.output


def test_runs_cancel_daemon_unreachable(state_db: Path):
    """ConnectionError → exit 2 + 'cannot reach daemon' on output."""
    import requests
    with patch(REQUESTS_POST,
               side_effect=requests.ConnectionError("refused")):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 2
    assert "cannot reach daemon" in r.output
    assert "http://d:8765" in r.output


def test_runs_cancel_unknown_run_id(state_db: Path):
    """Unknown run_id → exit 1, NO HTTP call (mock would fail otherwise)."""
    with patch(REQUESTS_POST) as mock_post:
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "does-not-exist"],
            state_db,
        )
    assert r.exit_code == 1
    assert "not found" in r.output
    mock_post.assert_not_called()


def test_runs_cancel_missing_api_key(state_db: Path):
    """No API key → exit 1, NO HTTP call."""
    with patch(REQUESTS_POST) as mock_post:
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 1
    assert "no API key" in r.output
    mock_post.assert_not_called()


def test_runs_cancel_uses_workflow_from_run_record(state_db: Path):
    """CLI looks up the workflow via get_run(), not from a CLI arg.
    Verifies the URL path uses the workflow from the runs record.
    """
    captured: dict = {}
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(200, '{"cancelled": true}')
    with patch(REQUESTS_POST, side_effect=fake_post):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "--api-key", "fake-test-key-001",
             "runs", "cancel", "r-cancel00001"],
            state_db,
        )
    assert r.exit_code == 0, r.output
    # The workflow was "demo" in the fixture.
    assert captured["url"] == "http://d:8765/v1/workflows/demo/runs/r-cancel00001/cancel"
    assert captured["headers"].get("X-API-Key") == "fake-test-key-001"


def test_runs_cancel_env_var_api_key(state_db: Path):
    """$AGENTFORGE_API_KEY works (no need to pass --api-key)."""
    with patch(REQUESTS_POST, return_value=_FakeResponse(200)):
        r = _invoke(
            ["--daemon-url", "http://d:8765",
             "runs", "cancel", "r-cancel00001"],
            state_db,
            env={"AGENTFORGE_API_KEY": "fake-test-key-env-001"},
        )
    assert r.exit_code == 0, r.output
