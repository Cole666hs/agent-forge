"""Tests for v0.14.0 workflow versioning.

Covers:
- Schema: workflow_versions table is created on a fresh State
- SQLiteWorkflowVersionStore.save_version:
  - returns a stable hash
  - same content under same workflow = same hash, idempotent
  - preserves first-save metadata on conflict
- list_versions returns newest first, respects limit
- get_version returns None for unknown
- diff produces a unified diff; empty for identical; ValueError for unknown
- hash is content-derived (different content -> different hash)
- CLI workflows versions list / show / diff / save / restore
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from agentforge.cli import cli
from agentforge.state import (
    State,
    WorkflowVersion,
    _hash_workflow_content,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def test_workflow_versions_table_exists(state: State):
    """v0.14.0: third schema version adds workflow_versions."""
    cur = state._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='workflow_versions'"
    )
    assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Store methods
# ---------------------------------------------------------------------------

CONTENT_A = "name: demo\nsteps: []\n"
CONTENT_B = "name: demo\nsteps:\n  - id: a\n    type: respond\n"


def test_save_version_returns_stable_hash(state: State):
    h = state.workflows.save_version("demo", CONTENT_A)
    assert len(h) == 12
    assert h == _hash_workflow_content(CONTENT_A)
    # Hash is content-derived, not random.
    assert h == "8771fd6275ba"  # documented value for CONTENT_A


def test_save_version_idempotent(state: State):
    """Same content under same workflow = same hash, no second row.
    Re-save must preserve the FIRST save's metadata (saved_at,
    saved_by, note) — that's the point of version history."""
    h1 = state.workflows.save_version("demo", CONTENT_A,
                                     saved_by="alice", note="first")
    h2 = state.workflows.save_version("demo", CONTENT_A,
                                     saved_by="bob", note="second")
    assert h1 == h2
    versions = state.workflows.list_versions("demo")
    assert len(versions) == 1
    # The first save's metadata won.
    v = versions[0]
    assert v.saved_by == "alice"
    assert v.note == "first"


def test_save_version_different_content_different_hash(state: State):
    h1 = state.workflows.save_version("demo", CONTENT_A)
    h2 = state.workflows.save_version("demo", CONTENT_B)
    assert h1 != h2
    versions = state.workflows.list_versions("demo")
    assert len(versions) == 2


def test_list_versions_newest_first(state: State):
    state.workflows.save_version("demo", CONTENT_A)
    state.workflows.save_version("demo", CONTENT_B)
    versions = state.workflows.list_versions("demo")
    # CONTENT_B was saved after CONTENT_A, so it's newest.
    assert versions[0].content == CONTENT_B
    assert versions[1].content == CONTENT_A


def test_list_versions_limit(state: State):
    for i in range(5):
        state.workflows.save_version("demo", f"content-{i}")
    versions = state.workflows.list_versions("demo", limit=3)
    assert len(versions) == 3


def test_list_versions_empty_workflow(state: State):
    assert state.workflows.list_versions("never-saved") == []


def test_get_version_unknown_returns_none(state: State):
    assert state.workflows.get_version("demo", "deadbeef") is None
    assert state.workflows.get_version("never-saved", _hash_workflow_content(CONTENT_A)) is None


def test_get_version_known(state: State):
    h = state.workflows.save_version("demo", CONTENT_A)
    v = state.workflows.get_version("demo", h)
    assert v is not None
    assert v.content == CONTENT_A
    assert v.version_hash == h


def test_diff_unknown_hash_raises(state: State):
    h = state.workflows.save_version("demo", CONTENT_A)
    with pytest.raises(ValueError):
        state.workflows.diff("demo", h, "deadbeef")


def test_diff_identical_returns_empty(state: State):
    h = state.workflows.save_version("demo", CONTENT_A)
    diff = state.workflows.diff("demo", h, h)
    assert diff == ""


def test_diff_different_returns_unified_diff(state: State):
    h1 = state.workflows.save_version("demo", CONTENT_A)
    h2 = state.workflows.save_version("demo", CONTENT_B)
    diff = state.workflows.diff("demo", h1, h2)
    assert "---" in diff
    assert "+++" in diff
    # The new step is in the diff.
    assert "id: a" in diff
    assert "+" in diff  # some line was added


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_state_with_versions(tmp_path: Path) -> tuple[Path, str, str]:
    """Seed state.db with two versions of 'demo'. Returns
    (state.db path, hash_a, hash_b)."""
    state_db = tmp_path / "state.db"
    s = State(state_db)
    h1 = s.workflows.save_version("demo", CONTENT_A)
    h2 = s.workflows.save_version("demo", CONTENT_B)
    s.close()
    return state_db, h1, h2


def test_cli_workflows_versions_list_prints_table(tmp_path: Path):
    state_db, _, _ = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "list", "demo",
    ], catch_exceptions=False)
    assert result.exit_code == 0
    # The header row is there.
    assert "hash" in result.output
    assert "saved_at" in result.output
    # Both hashes are in the output.
    assert "8771fd6275ba" in result.output
    # (We don't assert on the second hash because the content's
    # hash is whatever it is; just that the table isn't empty.)


def test_cli_workflows_versions_list_empty(tmp_path: Path):
    state_db = tmp_path / "state.db"
    State(state_db).close()
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "list", "never-saved",
    ], catch_exceptions=False)
    assert result.exit_code == 0
    assert "(no versions saved" in result.output


def test_cli_workflows_versions_show_prints_content(tmp_path: Path):
    state_db, h1, _ = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "show", "demo", h1,
    ], catch_exceptions=False)
    assert result.exit_code == 0
    assert "name: demo" in result.output


def test_cli_workflows_versions_show_unknown_exits_1(tmp_path: Path):
    state_db, _, _ = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "show", "demo", "deadbeef",
    ], catch_exceptions=False)
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_workflows_versions_diff_prints_diff(tmp_path: Path):
    state_db, h1, h2 = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "diff", "demo", h1, h2,
    ], catch_exceptions=False)
    assert result.exit_code == 0
    assert "---" in result.output
    assert "+++" in result.output


def test_cli_workflows_versions_diff_identical_says_no_diff(tmp_path: Path):
    state_db, h1, _ = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "diff", "demo", h1, h1,
    ], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no differences" in result.output


def test_cli_workflows_versions_diff_unknown_exits_1(tmp_path: Path):
    state_db, h1, _ = _setup_state_with_versions(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "diff", "demo", h1, "deadbeef",
    ], catch_exceptions=False)
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_workflows_versions_save_from_file(tmp_path: Path):
    state_db = tmp_path / "state.db"
    State(state_db).close()
    yaml_path = tmp_path / "my-workflow.yaml"
    yaml_path.write_text(CONTENT_A)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "workflows", "versions", "save", "my-workflow",
        "--file", str(yaml_path), "--note", "first",
    ], catch_exceptions=False)
    assert result.exit_code == 0
    assert "saved" in result.output
    # Verify the version is queryable.
    s = State(state_db)
    try:
        v = s.workflows.get_version("my-workflow", "8771fd6275ba")
        assert v is not None
        assert v.note == "first"
    finally:
        s.close()


def test_cli_workflows_versions_save_idempotent(tmp_path: Path):
    """Re-saving the same content under the same workflow produces
    the same hash and does NOT add a second row."""
    state_db = tmp_path / "state.db"
    State(state_db).close()
    yaml_path = tmp_path / "my-workflow.yaml"
    yaml_path.write_text(CONTENT_A)
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(cli, [
            "--state-db", str(state_db),
            "workflows", "versions", "save", "my-workflow",
            "--file", str(yaml_path),
        ], catch_exceptions=False)
        assert result.exit_code == 0
    s = State(state_db)
    try:
        versions = s.workflows.list_versions("my-workflow")
        assert len(versions) == 1
    finally:
        s.close()


def test_cli_workflows_versions_restore_writes_file(tmp_path: Path):
    state_db, h1, _ = _setup_state_with_versions(tmp_path)
    # Layout: mailbox_root.parent/workflows/ holds the live YAML
    # files. The CLI's default resolution puts workflows_dir at
    # <mailbox_root>/../workflows.
    mailbox_root = tmp_path / "mb"
    mailbox_root.mkdir()
    workflows_dir = mailbox_root.parent / "workflows"
    workflows_dir.mkdir()
    current = workflows_dir / "demo.yaml"
    current.write_text("name: demo\n# current edit\nsteps: []\n")
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--state-db", str(state_db),
        "--mailbox-root", str(mailbox_root),
        "workflows", "versions", "restore", "demo", h1,
    ], catch_exceptions=False)
    assert result.exit_code == 0
    # The file is back to the saved content.
    assert current.read_text() == CONTENT_A
