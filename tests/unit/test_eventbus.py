"""Tests for the v0.7.0 EventBus + run_events schema.

Covers:
- Schema migration v1 → v2 (existing data preserved, new table empty).
- publish() writes to run_events and returns a monotonic seq.
- events_since() returns events in seq order, filtered by workflow.
- max_seq() returns 0 for an empty workflow.
- subscribe() replays the backlog then yields live events.
- subscribe() is per-workflow (events for other workflows are not seen).
- Multi-subscriber fan-out (one event → N queues).
- Cleanup on subscriber disconnect (queue removed from list).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from agentforge.state import SCHEMA_VERSION, EventBus, State


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_schema_v2_includes_run_events(state: State):
    """After opening a fresh State, the run_events table must exist and
    be empty."""
    cur = state._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='run_events'"
    )
    assert cur.fetchone() is not None
    cur = state._conn.execute("SELECT COUNT(*) FROM run_events")
    assert cur.fetchone()[0] == 0
    # user_version reflects SCHEMA_VERSION.
    cur = state._conn.execute("PRAGMA user_version")
    (v,) = cur.fetchone()
    assert v == SCHEMA_VERSION == 2


def test_schema_migrates_v1_to_v2(tmp_path: Path):
    """Open a DB that has user_version=1 (without run_events), then a
    State on the same path; the new table must be created, user_version
    bumped to 2, and any pre-existing data preserved."""
    db_path = tmp_path / "legacy.db"
    # Build a v1 schema by hand (tenants/usage/runs, no run_events).
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tenants (
            tenant_id    TEXT PRIMARY KEY,
            api_key_hash TEXT NOT NULL,
            plan         TEXT NOT NULL DEFAULT 'free',
            created_at   TEXT NOT NULL
        );
        INSERT INTO tenants VALUES ('legacy', 'hash', 'free', '2025-01-01T00:00:00Z');
        CREATE TABLE usage (
            tenant_id TEXT NOT NULL,
            month     TEXT NOT NULL,
            tokens    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, month)
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow TEXT NOT NULL, tenant_id TEXT NOT NULL,
            agent TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            status TEXT NOT NULL, duration_seconds REAL NOT NULL, error TEXT
        );
        INSERT INTO runs VALUES (
            'r1', 'wf', 'legacy', 'a', '2025-01-01T00:00:00Z',
            '2025-01-01T00:00:01Z', 'success', 1.0, NULL
        );
        PRAGMA user_version = 1;
    """)
    conn.close()
    # Now open via State — schema bump must add run_events and preserve rows.
    s = State(db_path)
    try:
        cur = s._conn.execute("PRAGMA user_version")
        (v,) = cur.fetchone()
        assert v == 2
        # Legacy row preserved.
        cur = s._conn.execute("SELECT tenant_id FROM tenants")
        assert cur.fetchone()["tenant_id"] == "legacy"
        cur = s._conn.execute("SELECT id FROM runs")
        assert cur.fetchone()["id"] == "r1"
        # New table empty.
        cur = s._conn.execute("SELECT COUNT(*) FROM run_events")
        assert cur.fetchone()[0] == 0
    finally:
        s.close()


# ---------------------------------------------------------------------------
# publish() + events_since() + max_seq()
# ---------------------------------------------------------------------------

def test_publish_returns_monotonic_seq(state: State):
    """Consecutive publish() calls on the same workflow return strictly
    increasing seq numbers."""
    bus = state.events
    s1 = bus.publish("r1", "wf", "t", "started")
    s2 = bus.publish("r1", "wf", "t", "finished", {"ok": True})
    s3 = bus.publish("r1", "wf", "t", "started")
    assert s1 < s2 < s3
    assert s2 - s1 == 1
    assert s3 - s2 == 1


def test_events_since_filters_by_workflow(state: State):
    """events_since('wf_a', 0) must NOT include events for 'wf_b'."""
    bus = state.events
    bus.publish("r1", "wf_a", "t", "started")
    bus.publish("r2", "wf_b", "t", "started")
    bus.publish("r3", "wf_a", "t", "finished")
    a_events = bus.events_since("wf_a", 0)
    b_events = bus.events_since("wf_b", 0)
    assert [e.run_id for e in a_events] == ["r1", "r3"]
    assert [e.run_id for e in b_events] == ["r2"]


def test_events_since_respects_cursor(state: State):
    """Only events with seq > since are returned, in order."""
    bus = state.events
    s1 = bus.publish("r1", "wf", "t", "started")
    s2 = bus.publish("r2", "wf", "t", "started")
    s3 = bus.publish("r3", "wf", "t", "finished")
    backfill = bus.events_since("wf", s1)
    assert [e.seq for e in backfill] == [s2, s3]


def test_max_seq_zero_when_empty(state: State):
    """An untouched workflow has max_seq == 0, not None."""
    bus = state.events
    assert bus.max_seq("wf") == 0
    bus.publish("r1", "wf", "t", "started")
    bus.publish("r2", "wf", "t", "finished")
    assert bus.max_seq("wf") > 0


def test_payload_round_trip(state: State):
    """The payload dict is stored as JSON and re-parsed on read."""
    bus = state.events
    payload = {"status": "success", "tokens": 42, "nested": {"k": "v"}}
    bus.publish("r1", "wf", "t", "finished", payload)
    [ev] = bus.events_since("wf", 0)
    assert ev.payload == payload


# v0.7.1: the publish() → queue fan-out must now carry tenant_id so the
# WS endpoint can filter without a DB roundtrip. Subscribers receive
# the tenant_id on both replay and live events.

def test_live_event_carries_tenant_id(state: State):
    """A live event delivered via the in-process queue exposes the
    publisher's tenant_id. The DB row also has it (events_since)."""
    bus = state.events
    seen: list = []

    async def consume():
        async for ev in bus.subscribe("wf", since=0):
            seen.append(ev)
            if len(seen) >= 1:
                return

    async def runner():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        bus.publish("r1", "wf", "tenant-X", "started")
        await asyncio.wait_for(task, timeout=2.0)
    asyncio.run(runner())
    assert seen[0].tenant_id == "tenant-X"
    # DB roundtrip also has it.
    [db_ev] = bus.events_since("wf", 0)
    assert db_ev.tenant_id == "tenant-X"


def test_replay_event_carries_tenant_id(state: State):
    """events_since() (used by the replay path) returns events with
    tenant_id populated."""
    bus = state.events
    bus.publish("r1", "wf", "tenant-A", "started")
    bus.publish("r2", "wf", "tenant-B", "finished")
    events = bus.events_since("wf", 0)
    assert [e.tenant_id for e in events] == ["tenant-A", "tenant-B"]


# ---------------------------------------------------------------------------
# subscribe() — async iterator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_replays_then_yields_live(state: State):
    """subscribe() must drain any events with seq > since FIRST, then
    block on new live events."""
    bus = state.events
    bus.publish("r1", "wf", "t", "started")
    bus.publish("r1", "wf", "t", "finished")
    seen: list = []
    async def consume():
        async for ev in bus.subscribe("wf", since=0):
            seen.append(ev)
            if len(seen) >= 3:
                break
    task = asyncio.create_task(consume())
    # Give the replay phase a chance to finish, then publish one more.
    await asyncio.sleep(0.05)
    bus.publish("r2", "wf", "t", "started")
    await asyncio.wait_for(task, timeout=2.0)
    assert [e.run_id for e in seen] == ["r1", "r1", "r2"]


@pytest.mark.asyncio
async def test_subscribe_does_not_see_other_workflows(state: State):
    """A subscriber for 'wf_a' must not be notified about 'wf_b' events."""
    bus = state.events
    seen: list = []
    async def consume():
        async for ev in bus.subscribe("wf_a", since=0):
            seen.append(ev)
            if len(seen) >= 1:
                break
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    bus.publish("r1", "wf_b", "t", "started")  # NOT for wf_a
    bus.publish("r1", "wf_a", "t", "started")  # this one
    await asyncio.wait_for(task, timeout=2.0)
    assert len(seen) == 1
    assert seen[0].workflow == "wf_a"


@pytest.mark.asyncio
async def test_subscribe_fan_out_to_multiple_consumers(state: State):
    """One publish() must reach all current subscribers of the workflow."""
    bus = state.events
    a, b, c = [], [], []
    async def consume(buf, stop_at=1):
        async for ev in bus.subscribe("wf", since=0):
            buf.append(ev)
            if len(buf) >= stop_at:
                return
    t1 = asyncio.create_task(consume(a))
    t2 = asyncio.create_task(consume(b))
    t3 = asyncio.create_task(consume(c, stop_at=2))
    await asyncio.sleep(0.05)
    bus.publish("r1", "wf", "t", "started")
    bus.publish("r2", "wf", "t", "finished")
    await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=2.0)
    # a, b stop at 1 event; c stops at 2.
    assert [e.run_id for e in a] == ["r1"]
    assert [e.run_id for e in b] == ["r1"]
    assert [e.run_id for e in c] == ["r1", "r2"]


@pytest.mark.asyncio
async def test_subscribe_cleanup_on_disconnect(state: State):
    """When the consumer's iterator is closed, the queue must be removed
    from the bus's subscriber list."""
    bus = state.events
    assert bus._subscribers == {}
    # Start a consumer that takes one event then exits.
    async def consume():
        async for ev in bus.subscribe("wf", since=0):
            return
    t = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    # One subscriber is now registered.
    assert "wf" in bus._subscribers and len(bus._subscribers["wf"]) == 1
    bus.publish("r1", "wf", "t", "started")
    await asyncio.wait_for(t, timeout=2.0)
    # Give the finally block a tick to run.
    await asyncio.sleep(0.05)
    assert bus._subscribers == {}
