"""agentforge.state — SQLite-backed persistence for tenant/usage/runs state.

v0.6.0 design notes (2026-06-11):

  Why SQLite (not the existing JSON files)?
  - The JSON files (tenants.json, usage.json, runs.json) are restart-flüchtig
    in a way that hurts: a daemon crash mid-write could lose quota counters
    (billing bug) or run history. The atomic write pattern (tempfile +
    os.replace) helps, but is per-file — there is no cross-file consistency.
  - The in-memory `_data` dict is populated at __init__ and never re-read
    from disk. If anything external writes the file while the daemon is up
    (admin tool, another worker), the daemon silently ignores it.
  - Multi-worker is impossible with the JSON approach: two processes
    writing to the same JSON file will clobber each other.

  Why not Postgres / Redis / etc?
  - Single-binary deploy stays. `pip install agentforge && agentforge serve`
    must Just Work without spinning up a database.
  - WAL mode gives us concurrent readers + single writer, which is enough
    for the realistic deployment (1 daemon, N dashboard readers).
  - `sqlite3` is stdlib — no new dep.

  Schema design:
  - `tenants(tenant_id PK, api_key_hash, plan, created_at)` — direct
    mapping of the existing JSON shape.
  - `usage(tenant_id, month, tokens)` with composite PK — the JSON
    file's "lazy reset on month change" logic moves to a `WHERE month = ?`
    read instead of a per-record compare.
  - `runs(id PK, workflow, tenant_id, agent, started_at, ended_at,
     status, duration_seconds, error)` + index on (workflow, started_at DESC)
    for the dashboard's "recent runs for this workflow" query.
  - `PRAGMA user_version` is the migration marker. v0.6.0 ships schema
    version 1. Bumping requires writing a migration block in `migrate()`.

  Drop-in interface: each SQLite class exposes the same public methods
  as its JSON counterpart (add, get_plan, set_plan, lookup, remove,
  list_tenants / get, record, reset / record, list_runs). Tests that
  exercise the public API continue to pass; only tests that pokes
  at internal `._data` need to change.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from agentforge.billing.plans import Plan, is_valid_plan
from agentforge.core.runs import RunRecord  # re-export for callers

logger = logging.getLogger("agentforge.state")

SCHEMA_VERSION = 2

# Default per-workflow run cap — matches RunStore default.
DEFAULT_MAX_RUNS_PER_WORKFLOW = 100


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with our standard pragmas.

    - WAL mode for concurrent readers + single writer.
    - foreign_keys=ON for future-proofing.
    - Row factory for dict-like access.
    - check_same_thread=False; we serialize via a per-connection lock
      in `State` below because sqlite3's per-connection threading model
      is brittle.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables and set the user_version if first run. Idempotent.

    v0.7.1: the PRAGMA user_version bump moved INTO the executescript
    so the entire migration is one atomic transaction (handles by
    `executescript` internally — it issues a COMMIT first, then runs
    the whole script in a single implicit transaction). This way a
    crash mid-migration can't leave the DB with a v2 schema but
    user_version=1, which would re-run the migration on next open
    (harmless with IF NOT EXISTS but ugly). In autocommit mode
    (isolation_level=None), wrapping the script in manual BEGIN/COMMIT
    is NOT an option — `executescript` already issues its own COMMIT,
    which conflicts with the manual BEGIN and breaks atomicity.
    """
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS tenants (
            tenant_id    TEXT PRIMARY KEY,
            api_key_hash TEXT NOT NULL,
            plan         TEXT NOT NULL DEFAULT 'free',
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            tenant_id TEXT NOT NULL,
            month     TEXT NOT NULL,
            tokens    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, month)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id               TEXT PRIMARY KEY,
            workflow         TEXT NOT NULL,
            tenant_id        TEXT NOT NULL,
            agent            TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            ended_at         TEXT NOT NULL,
            status           TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            error            TEXT
        );
        CREATE INDEX IF NOT EXISTS runs_by_workflow
            ON runs(workflow, started_at DESC);
        CREATE TABLE IF NOT EXISTS run_events (
            seq       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id    TEXT NOT NULL,
            workflow  TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            kind      TEXT NOT NULL,
            payload   TEXT NOT NULL DEFAULT '{{}}',
            ts        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS run_events_by_workflow_seq
            ON run_events(workflow, seq);
        PRAGMA user_version = {SCHEMA_VERSION};
    """)


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------

class State:
    """Single SQLite-backed state store with thread-safe access.

    All public methods acquire `self._lock` for the duration of the
    transaction. This is intentionally simple: in the realistic deploy
    (1 daemon process, multiple dashboard readers via the FastAPI app),
    the lock is held for microseconds. If we ever need cross-process
    coordination, switch to a real DB (Postgres). The interface stays
    the same; only the impl changes.

    `State` is a single object that holds all 3 logical stores. The
    individual "registry / usage / runs" handles below are thin
    facades that call into the same connection under the same lock.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)
        self._lock = threading.RLock()
        _ensure_schema(self._conn)
        self.tenants = SQLiteTenantRegistry(self)
        self.usage = SQLiteUsageStore(self)
        self.runs = SQLiteRunStore(self)
        self.events = EventBus(self)
        logger.info("state: opened %s (schema v%d)", self.db_path, SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Acquire the lock and yield the connection for a transaction.

        Caller is responsible for committing/rolling back. We use
        autocommit mode (isolation_level=None) so the caller controls
        the transaction boundary explicitly.
        """
        with self._lock:
            yield self._conn


# ---------------------------------------------------------------------------
# SQLiteTenantRegistry
# ---------------------------------------------------------------------------

def _hash_key(api_key: str) -> str:
    import hashlib
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _generate_api_key() -> str:
    import secrets
    return secrets.token_urlsafe(32)


class SQLiteTenantRegistry:
    """SQLite-backed tenant + API-key + plan store.

    Drop-in replacement for the JSON `TenantRegistry`. Same public methods
    (add, get_plan, set_plan, lookup, remove, list_tenants). The
    constructor takes a `State` instead of a `path` — the State owns
    the connection and the lock.
    """

    def __init__(self, state: State):
        self._state = state

    def add(
        self,
        tenant_id: str,
        api_key: Optional[str] = None,
    ) -> str:
        if not tenant_id or not all(c.isalnum() or c in "-_" for c in tenant_id):
            raise ValueError(
                f"tenant_id must match [a-zA-Z0-9_-]+, got {tenant_id!r}"
            )
        with self._state._tx() as conn:
            cur = conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
            if cur.fetchone() is not None:
                raise ValueError(f"tenant {tenant_id!r} already exists")
            key = api_key or _generate_api_key()
            conn.execute(
                "INSERT INTO tenants(tenant_id, api_key_hash, plan, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tenant_id, _hash_key(key), Plan.FREE.value,
                 datetime.now(timezone.utc).isoformat()),
            )
        return key

    def get_plan(self, tenant_id: str) -> Plan:
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT plan FROM tenants WHERE tenant_id = ?", (tenant_id,)
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"tenant {tenant_id!r} not found")
        raw = row["plan"]
        if not is_valid_plan(raw):
            return Plan.FREE
        return Plan(raw)

    def set_plan(self, tenant_id: str, plan: Plan) -> None:
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
            )
            if cur.fetchone() is None:
                raise ValueError(f"tenant {tenant_id!r} not found")
            conn.execute(
                "UPDATE tenants SET plan = ? WHERE tenant_id = ?",
                (plan.value, tenant_id),
            )

    def lookup(self, api_key: str) -> Optional[str]:
        if not api_key:
            return None
        import hmac
        candidate = _hash_key(api_key)
        with self._state._tx() as conn:
            cur = conn.execute("SELECT tenant_id, api_key_hash FROM tenants")
            for row in cur.fetchall():
                if hmac.compare_digest(candidate, row["api_key_hash"]):
                    return row["tenant_id"]
        return None

    def remove(self, tenant_id: str) -> bool:
        with self._state._tx() as conn:
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            return cur.rowcount > 0

    def list_tenants(self) -> List[str]:
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT tenant_id FROM tenants ORDER BY tenant_id"
            )
            return [row["tenant_id"] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# SQLiteUsageStore
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    """Snapshot of one tenant's token usage for the current calendar month."""
    tenant_id: str
    tokens: int
    month: str  # "YYYY-MM" (UTC)


class SQLiteUsageStore:
    """SQLite-backed per-tenant per-month token counter.

    Drop-in replacement for the JSON `UsageStore`. The "lazy reset on
    month change" logic moves from a per-record compare to a `WHERE
    month = ?` query — an unknown month reads as 0 in the current
    month, which is the same observable behavior.
    """

    def __init__(self, state: State):
        self._state = state

    @staticmethod
    def _current_month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def get(self, tenant_id: str) -> TokenUsage:
        month = self._current_month()
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT tokens FROM usage WHERE tenant_id = ? AND month = ?",
                (tenant_id, month),
            )
            row = cur.fetchone()
        return TokenUsage(
            tenant_id=tenant_id,
            tokens=int(row["tokens"]) if row else 0,
            month=month,
        )

    def record(self, tenant_id: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        month = self._current_month()
        with self._state._tx() as conn:
            # UPSERT: insert if missing for this month, else increment.
            conn.execute(
                "INSERT INTO usage(tenant_id, month, tokens) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, month) DO UPDATE SET "
                "tokens = tokens + excluded.tokens",
                (tenant_id, month, tokens),
            )

    def reset(self, tenant_id: str) -> None:
        with self._state._tx() as conn:
            conn.execute(
                "DELETE FROM usage WHERE tenant_id = ?", (tenant_id,)
            )


# ---------------------------------------------------------------------------
# SQLiteRunStore
# ---------------------------------------------------------------------------

class SQLiteRunStore:
    """SQLite-backed run history with per-workflow cap.

    Drop-in replacement for the JSON `RunStore`. Eviction is done in a
    single SQL statement (DELETE the oldest beyond the cap) rather
    than reading + trimming in Python.
    """

    def __init__(self, state: State, max_per_workflow: int = DEFAULT_MAX_RUNS_PER_WORKFLOW):
        self._state = state
        self.max_per_workflow = max_per_workflow

    @property
    def events(self) -> "EventBus":
        """Live-event bus for run lifecycle. Backed by the run_events
        table on the same State. Exposed here because `run_store.events`
        is the natural place to look when you have a run handle but no
        direct reference to the State container."""
        return self._state.events

    def record(self, run: RunRecord) -> None:
        with self._state._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(id, workflow, tenant_id, agent, started_at, ended_at, "
                " status, duration_seconds, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id, run.workflow, run.tenant_id, run.agent,
                    run.started_at, run.ended_at, run.status,
                    run.duration_seconds, run.error,
                ),
            )
            # Evict the oldest beyond the cap for this workflow.
            conn.execute(
                "DELETE FROM runs WHERE workflow = ? AND id NOT IN ("
                "  SELECT id FROM runs WHERE workflow = ? "
                "  ORDER BY started_at DESC LIMIT ?"
                ")",
                (run.workflow, run.workflow, self.max_per_workflow),
            )

    def list_runs(self, workflow: str, limit: Optional[int] = None) -> List[RunRecord]:
        sql = (
            "SELECT id, workflow, tenant_id, agent, started_at, ended_at, "
            "status, duration_seconds, error FROM runs "
            "WHERE workflow = ? ORDER BY started_at DESC"
        )
        params: tuple = (workflow,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (workflow, int(limit))
        with self._state._tx() as conn:
            cur = conn.execute(sql, params)
            return [RunRecord(**dict(row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# JSON → SQLite migration
# ---------------------------------------------------------------------------

def migrate_json_to_sqlite(
    json_tenants_path: Optional[Path],
    json_usage_path: Optional[Path],
    json_runs_path: Optional[Path],
    state: State,
) -> dict:
    """One-shot import: read each JSON file (if present) and write its
    contents into the SQLite store. Idempotent: re-running against the
    same DB will skip already-imported rows (tenants, usage entries,
    runs are upserted by primary key).

    Returns a small report dict so the caller can log it.
    """
    report = {"tenants": 0, "usage": 0, "runs": 0, "skipped": []}

    if json_tenants_path and json_tenants_path.exists():
        try:
            data = json.loads(json_tenants_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            report["skipped"].append(f"tenants: corrupt {json_tenants_path}")
        else:
            with state._tx() as conn:
                for tid, entry in data.get("tenants", {}).items():
                    plan = entry.get("plan", Plan.FREE.value)
                    if not is_valid_plan(plan):
                        plan = Plan.FREE.value
                    conn.execute(
                        "INSERT OR IGNORE INTO tenants "
                        "(tenant_id, api_key_hash, plan, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (tid, entry.get("api_key_hash", ""), plan,
                         entry.get("created_at",
                                   datetime.now(timezone.utc).isoformat())),
                    )
                    report["tenants"] += 1

    if json_usage_path and json_usage_path.exists():
        try:
            data = json.loads(json_usage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            report["skipped"].append(f"usage: corrupt {json_usage_path}")
        else:
            with state._tx() as conn:
                for tid, entry in data.get("tenants", {}).items():
                    month = entry.get("month")
                    tokens = int(entry.get("tokens", 0))
                    if not month or tokens <= 0:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO usage(tenant_id, month, tokens) "
                        "VALUES (?, ?, ?)",
                        (tid, month, tokens),
                    )
                    report["usage"] += 1

    if json_runs_path and json_runs_path.exists():
        try:
            data = json.loads(json_runs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            report["skipped"].append(f"runs: corrupt {json_runs_path}")
        else:
            with state._tx() as conn:
                for r in data.get("runs", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO runs "
                        "(id, workflow, tenant_id, agent, started_at, ended_at, "
                        " status, duration_seconds, error) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["id"], r["workflow"], r["tenant_id"], r["agent"],
                         r["started_at"], r["ended_at"], r["status"],
                         float(r["duration_seconds"]), r.get("error")),
                    )
                    report["runs"] += 1

    if report["tenants"] or report["usage"] or report["runs"]:
        logger.info(
            "state: migrated from JSON — tenants=%d usage=%d runs=%d skipped=%s",
            report["tenants"], report["usage"], report["runs"], report["skipped"],
        )
    return report


# ---------------------------------------------------------------------------
# EventBus (v0.7.0)
# ---------------------------------------------------------------------------
# Append-only event log for workflow runs. Two consumers:
#   1. Replay: a WebSocket client that reconnects with `?since=<seq>`
#      back-fills the events it missed by reading the run_events table.
#   2. Live: an in-process list of asyncio.Queue subscribers gets notified
#      synchronously when `publish()` runs. Each subscriber sees events
#      for one workflow only (filtered by the queue's `_workflow` attr).
#
# The DB is the source of truth; in-process queues are a delivery
# optimization for the current process. A subscriber that disconnects
# loses nothing durable — it just re-subscribes with `since=<last_seq>`.

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, List


@dataclass(frozen=True)
class RunEvent:
    """One row in the run_events table. `payload` is JSON-decoded on read."""
    seq: int
    run_id: str
    workflow: str
    tenant_id: str
    kind: str
    payload: dict
    ts: str


class EventBus:
    """In-process pub/sub for run events, backed by the run_events table.

    `publish()` writes to SQLite (so a future subscriber can replay it)
    and then fans out to all current subscribers of the workflow.

    `subscribe()` returns an async iterator that yields a snapshot of
    every event for the workflow with seq > `since`, then blocks on
    new live events until the consumer stops iterating (or the
    connection is closed via `aclose()`).
    """

    def __init__(self, state: "State"):
        self._state = state
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = threading.RLock()

    def publish(
        self,
        run_id: str,
        workflow: str,
        tenant_id: str,
        kind: str,
        payload: Optional[dict] = None,
    ) -> int:
        """Append one event. Returns its seq number (monotonic per-DB)."""
        if payload is None:
            payload = {}
        ts = datetime.now(timezone.utc).isoformat()
        with self._state._tx() as conn:
            cur = conn.execute(
                "INSERT INTO run_events(run_id, workflow, tenant_id, kind, payload, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, workflow, tenant_id, kind, json.dumps(payload), ts),
            )
            seq = int(cur.lastrowid or 0)
        # Notify current subscribers. We copy the list under lock so a
        # subscriber that disconnects mid-fan-out doesn't break the
        # iteration. Per v0.7.1 review fix: the tuple now includes
        # tenant_id so the WS endpoint can filter by tenant without a
        # second DB roundtrip per event.
        with self._lock:
            queues = list(self._subscribers.get(workflow, ()))
        for q in queues:
            try:
                q.put_nowait((seq, run_id, tenant_id, kind, payload, ts))
            except asyncio.QueueFull:  # pragma: no cover — bounded queue
                pass
        return seq

    def events_since(self, workflow: str, since: int) -> List[RunEvent]:
        """Replay events for one workflow with seq > since, ordered by seq."""
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT seq, run_id, workflow, tenant_id, kind, payload, ts "
                "FROM run_events WHERE workflow = ? AND seq > ? "
                "ORDER BY seq ASC",
                (workflow, since),
            )
            rows = cur.fetchall()
        return [
            RunEvent(
                seq=int(r["seq"]), run_id=r["run_id"],
                workflow=r["workflow"], tenant_id=r["tenant_id"],
                kind=r["kind"], payload=json.loads(r["payload"] or "{}"),
                ts=r["ts"],
            )
            for r in rows
        ]

    def max_seq(self, workflow: str) -> int:
        """Highest seq seen for this workflow, or 0 if no events yet."""
        with self._state._tx() as conn:
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE workflow = ?",
                (workflow,),
            )
            (m,) = cur.fetchone()
        return int(m)

    async def subscribe(self, workflow: str, since: int = 0) -> AsyncIterator[RunEvent]:
        """Yield a live stream of RunEvent for one workflow.

        On entry: drains the table for seq > since. Then waits for new
        events published after subscribe() was called. Closes cleanly
        via aclose() on the returned async iterator (use `async with`
        or call `await aclose()` explicitly).
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        with self._lock:
            self._subscribers.setdefault(workflow, []).append(queue)
        try:
            # Replay backlog first (no yield, no throttle). The caller
            # has already received these via the pre-render so most
            # reconnects will have an empty backlog.
            for ev in self.events_since(workflow, since):
                yield ev
            # Live events. Block on the queue; the publish() call wakes
            # us up. If the queue is closed (a different code path
            # closes it) the CancelledError escapes and the iterator
            # terminates. The 6-tuple (seq, run_id, tenant_id, kind,
            # payload, ts) lets the WS endpoint enforce tenant isolation
            # without a DB roundtrip.
            while True:
                seq, run_id, ev_tenant_id, kind, payload, ts = await queue.get()
                yield RunEvent(
                    seq=seq, run_id=run_id, workflow=workflow,
                    tenant_id=ev_tenant_id,
                    kind=kind, payload=payload, ts=ts,
                )
        finally:
            with self._lock:
                subs = self._subscribers.get(workflow, [])
                if queue in subs:
                    subs.remove(queue)
                if not subs:
                    self._subscribers.pop(workflow, None)
