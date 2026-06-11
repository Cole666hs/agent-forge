"""Tests for agentforge.state — SQLite-backed tenant/usage/runs state.

Companion to the JSON-backed tests in test_tenants_registry.py,
test_billing_usage.py, test_runs.py. These exercise the SQLite
implementation directly with the same public-API surface, so any
refactor that breaks the interface will fail both backends.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentforge.billing.plans import Plan
from agentforge.state import (
    State,
    RunRecord,
    TokenUsage,
    migrate_json_to_sqlite,
)


@pytest.fixture
def state(tmp_path: Path) -> State:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------

def test_state_tenants_new_defaults_to_free(state: State):
    api_key = state.tenants.add("acme")
    assert state.tenants.get_plan("acme") == Plan.FREE
    # API key returned is non-empty and not stored plaintext
    assert api_key
    assert state.tenants.lookup(api_key) == "acme"
    assert state.tenants.lookup("not-a-real-key") is None


def test_state_tenants_set_plan_persists(state: State):
    state.tenants.add("acme")
    state.tenants.set_plan("acme", Plan.PRO)
    assert state.tenants.get_plan("acme") == Plan.PRO


def test_state_tenants_set_plan_unknown_raises(state: State):
    with pytest.raises(ValueError, match="not found"):
        state.tenants.set_plan("ghost", Plan.PRO)


def test_state_tenants_get_plan_unknown_raises(state: State):
    with pytest.raises(ValueError, match="not found"):
        state.tenants.get_plan("ghost")


def test_state_tenants_add_duplicate_raises(state: State):
    state.tenants.add("acme")
    with pytest.raises(ValueError, match="already exists"):
        state.tenants.add("acme")


def test_state_tenants_add_rejects_invalid_id(state: State):
    with pytest.raises(ValueError, match=r"\[a-zA-Z0-9_-\]\+"):
        state.tenants.add("acme/../escape")
    with pytest.raises(ValueError, match=r"\[a-zA-Z0-9_-\]\+"):
        state.tenants.add("")
    with pytest.raises(ValueError, match=r"\[a-zA-Z0-9_-\]\+"):
        state.tenants.add("has space")


def test_state_tenants_remove(state: State):
    state.tenants.add("acme")
    assert state.tenants.remove("acme") is True
    assert state.tenants.remove("acme") is False
    with pytest.raises(ValueError):
        state.tenants.get_plan("acme")


def test_state_tenants_list_sorted(state: State):
    state.tenants.add("zebra")
    state.tenants.add("alpha")
    state.tenants.add("mango")
    assert state.tenants.list_tenants() == ["alpha", "mango", "zebra"]


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def test_state_usage_initial_is_zero(state: State):
    u = state.usage.get("acme")
    assert u.tokens == 0
    assert u.month == _current_month()


def test_state_usage_record_accumulates(state: State):
    state.usage.record("acme", 100)
    state.usage.record("acme", 50)
    assert state.usage.get("acme").tokens == 150


def test_state_usage_get_unknown_tenant_is_zero(state: State):
    assert state.usage.get("ghost").tokens == 0


def test_state_usage_reset(state: State):
    state.usage.record("acme", 500)
    state.usage.reset("acme")
    assert state.usage.get("acme").tokens == 0


def test_state_usage_rejects_negative(state: State):
    with pytest.raises(ValueError, match="non-negative"):
        state.usage.record("acme", -1)


def test_state_usage_separate_tenants(state: State):
    state.usage.record("acme", 100)
    state.usage.record("globex", 250)
    assert state.usage.get("acme").tokens == 100
    assert state.usage.get("globex").tokens == 250


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def _make_run(idx: int, workflow: str = "greet", status: str = "success") -> RunRecord:
    return RunRecord(
        id=f"r{idx}",
        workflow=workflow,
        tenant_id="acme",
        agent="bot",
        started_at=f"2026-06-11T10:0{idx}:00+00:00",
        ended_at=f"2026-06-11T10:0{idx + 1}:00+00:00",
        status=status,
        duration_seconds=60.0,
        error=None,
    )


def test_state_runs_record_and_list(state: State):
    state.runs.record(_make_run(1))
    runs = state.runs.list_runs("greet")
    assert len(runs) == 1
    assert runs[0].id == "r1"
    assert runs[0].status == "success"


def test_state_runs_list_newest_first(state: State):
    state.runs.record(_make_run(1))
    state.runs.record(_make_run(2))
    state.runs.record(_make_run(3))
    runs = state.runs.list_runs("greet")
    assert [r.id for r in runs] == ["r3", "r2", "r1"]


def test_state_runs_filter_by_workflow(state: State):
    state.runs.record(_make_run(1, workflow="greet"))
    state.runs.record(_make_run(2, workflow="farewell"))
    assert [r.id for r in state.runs.list_runs("greet")] == ["r1"]
    assert [r.id for r in state.runs.list_runs("farewell")] == ["r2"]


def test_state_runs_limit(state: State):
    for i in range(5):
        state.runs.record(_make_run(i))
    runs = state.runs.list_runs("greet", limit=2)
    assert [r.id for r in runs] == ["r4", "r3"]


def test_state_runs_eviction_per_workflow(state: State):
    """Per-workflow cap enforced via SQL DELETE — oldest runs evicted first."""
    small = State(state.db_path.parent / "small.db",
                  max_per_workflow=3) if False else None  # type: ignore
    # Build a fresh state with a small cap.
    s = State(state.db_path.parent / "evict.db")
    try:
        s.runs.max_per_workflow = 3
        for i in range(5):
            s.runs.record(_make_run(i))
        runs = s.runs.list_runs("greet")
        assert len(runs) == 3
        # r0 and r1 evicted; r2, r3, r4 remain, newest first
        assert [r.id for r in runs] == ["r4", "r3", "r2"]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Restart-resilience
# ---------------------------------------------------------------------------

def test_state_restart_preserves_data(tmp_path: Path):
    """The whole point: close the state, reopen, data still there."""
    s1 = State(tmp_path / "state.db")
    s1.tenants.add("acme")
    s1.usage.record("acme", 500)
    s1.runs.record(_make_run(1))
    s1.close()

    s2 = State(tmp_path / "state.db")
    try:
        assert s2.tenants.list_tenants() == ["acme"]
        assert s2.usage.get("acme").tokens == 500
        assert len(s2.runs.list_runs("greet")) == 1
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# JSON → SQLite migration
# ---------------------------------------------------------------------------

def test_migrate_json_to_sqlite_imports_all(tmp_path: Path):
    """All three JSON files import in one call. Idempotent on re-run."""
    db = tmp_path / "state.db"
    state = State(db)
    try:
        # Synthesize legacy JSON files
        from agentforge.state import _hash_key
        tenants_json = tmp_path / "tenants.json"
        usage_json = tmp_path / "usage.json"
        runs_json = tmp_path / "runs.json"
        tenants_json.write_text(json.dumps({
            "tenants": {
                "oldco": {"api_key_hash": _hash_key("secret"),
                          "plan": "enterprise", "created_at": "2026-01-01"},
            },
        }))
        usage_json.write_text(json.dumps({
            "tenants": {"oldco": {"tokens": 999, "month": "2026-05"}},
        }))
        runs_json.write_text(json.dumps({
            "runs": [{
                "id": "r0", "workflow": "legacy", "tenant_id": "oldco",
                "agent": "oldbot",
                "started_at": "2026-05-01T00:00:00+00:00",
                "ended_at": "2026-05-01T00:01:00+00:00",
                "status": "success", "duration_seconds": 60.0,
                "error": None,
            }],
        }))

        report = migrate_json_to_sqlite(tenants_json, usage_json, runs_json, state)
        assert report == {"tenants": 1, "usage": 1, "runs": 1, "skipped": []}
        assert state.tenants.get_plan("oldco") == Plan.ENTERPRISE
        assert len(state.runs.list_runs("legacy")) == 1

        # Re-running against same DB is idempotent (INSERT OR IGNORE)
        report2 = migrate_json_to_sqlite(tenants_json, usage_json, runs_json, state)
        assert report2["tenants"] == 1
        assert state.tenants.list_tenants() == ["oldco"]
    finally:
        state.close()


def test_migrate_handles_missing_files(tmp_path: Path):
    """All-None paths → empty report, no error."""
    state = State(tmp_path / "state.db")
    try:
        report = migrate_json_to_sqlite(None, None, None, state)
        assert report == {"tenants": 0, "usage": 0, "runs": 0, "skipped": []}
    finally:
        state.close()


def test_migrate_handles_corrupt_json(tmp_path: Path):
    """A corrupt file is logged-and-skipped, not raised."""
    state = State(tmp_path / "state.db")
    try:
        corrupt = tmp_path / "tenants.json"
        corrupt.write_text("{not valid json")
        report = migrate_json_to_sqlite(corrupt, None, None, state)
        assert "tenants" in str(report["skipped"])
    finally:
        state.close()
