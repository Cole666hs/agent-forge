"""agentforge.workflows.engine — Workflow + State + run().

A Workflow is a sequence of Steps. Each step reads from and writes to
the shared State. State is persisted to SQLite so a workflow can be
resumed after a crash.

Built-in step types:
  - receive: pull oldest unread message from mailbox.inbox(agent_name)
  - llm_call: invoke the LLM provider with rendered prompts
  - respond: write a Message to a recipient's inbox

Custom step types can be registered via @register_step_type("name", fn).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

import yaml

from agentforge.core.message import Message

if TYPE_CHECKING:
    from agentforge.adapters.base import BaseLLMAdapter
    from agentforge.core.mailbox import Mailbox

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WorkflowError(RuntimeError):
    """Raised on YAML parse errors, unknown step types, or step failures
    that are not retried."""


class WorkflowCancelled(Exception):
    """Raised by `Workflow.run()` when the caller flips the cancellation
    event between steps (v0.8.0 #1). The run record is marked
    status='cancelled'; the partial state up to the last-completed
    step is preserved.
    """


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:
    """Workflow execution state — a nested dict with dotted-path access.

    Persistence: serialize the dict to a SQLite table; hydrate by reading
    all rows for the same run_id. Dotted-path keys keep the schema flat
    (no JSON nested objects in the DB).
    """

    def __init__(self, run_id: Optional[str] = None, tenant_id: str = ""):
        self.run_id = run_id or f"run-{id(self)}"
        self.tenant_id = tenant_id
        self._data: Dict[str, Any] = {}

    def set(self, path: str, value: Any) -> None:
        keys = path.split(".")
        d = self._data
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    def get(self, path: str, default: Any = None) -> Any:
        keys = path.split(".")
        d: Any = self._data
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d

    def render(self, template: str) -> str:
        """Substitute {{ path.to.value }} placeholders from state.

        Missing keys render as empty string — never raise. This is a
        pragmatic choice: a missing path is usually "the step that
        produced this value didn't run", and the workflow will fail
        downstream with a clearer error than a KeyError here.
        """
        def sub(m: "re.Match[str]") -> str:
            path = m.group(1).strip()
            val = self.get(path, "")
            if val is None:
                return ""
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)
        return re.sub(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}", sub, template)

    def persist(self, db_path: Path) -> None:
        """Save state to SQLite. Overwrites any prior state for this
        (tenant_id, run_id) pair."""
        with _open_state_db(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_state (
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, run_id, key)
                )
            """)
            conn.execute(
                "DELETE FROM workflow_state WHERE tenant_id = ? AND run_id = ?",
                (self.tenant_id, self.run_id),
            )
            for path, value in _flatten(self._data):
                conn.execute(
                    "INSERT INTO workflow_state (tenant_id, run_id, key, value) "
                    "VALUES (?, ?, ?, ?)",
                    (self.tenant_id, self.run_id, path,
                     json.dumps(value, ensure_ascii=False)),
                )
            conn.commit()

    def hydrate(self, db_path: Path) -> None:
        """Load state from SQLite for (tenant_id, run_id). Discards any
        in-memory data first."""
        self._data = {}
        if not Path(db_path).exists():
            return
        with _open_state_db(db_path) as conn:
            for key, value in conn.execute(
                "SELECT key, value FROM workflow_state "
                "WHERE tenant_id = ? AND run_id = ?",
                (self.tenant_id, self.run_id),
            ):
                self._data = _set_path(self._data, key, json.loads(value))


def _flatten(d: Dict[str, Any], prefix: str = "") -> List[tuple]:
    """Flatten nested dict to list of (dotted_path, value) pairs.
    Leaf values must be JSON-serializable (str, int, float, bool, list, None)."""
    out: List[tuple] = []
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.extend(_flatten(v, prefix=path))
        else:
            out.append((path, v))
    return out


def _set_path(d: Dict[str, Any], path: str, value: Any) -> Dict[str, Any]:
    """Inverse of _flatten: set a value at a dotted path, creating dicts as needed."""
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    return d


# SQLite default locking is "database is locked" on concurrent writers.
# WAL mode allows concurrent readers + one writer without blocking,
# and the timeout makes writers wait briefly instead of erroring.
# This is the same fix recommended by HAMILLER + NEMESIS reviews.
_DB_TIMEOUT_SECONDS = 5.0


def _open_state_db(db_path: Path) -> sqlite3.Connection:
    """Open the state DB with WAL mode + busy timeout.

    WAL: concurrent readers don't block a writer. Use `PRAGMA journal_mode=WAL`
    on every connection (it's a per-connection setting, not persisted).
    Timeout: `sqlite3.connect(timeout=...)` makes concurrent writers wait
    up to that many seconds before raising "database is locked".
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=_DB_TIMEOUT_SECONDS)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL is the standard combo
    return conn


# ---------------------------------------------------------------------------
# Step + Workflow
# ---------------------------------------------------------------------------

@dataclass
class Step:
    id: str
    type: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    on_error_retry: int = 0
    on_error_backoff: str = "exponential"  # or "none"


# Registry: step_type -> async fn(inputs, state, context) -> state-mutation
StepHandler = Callable[[Dict[str, Any], State, "StepContext"], Awaitable[None]]


@dataclass
class StepContext:
    """Bundle of dependencies passed to step handlers."""
    mailbox: "Mailbox"
    llm: Optional["BaseLLMAdapter"]
    agent_name: str
    state_db: Optional[Path] = None


_STEP_REGISTRY: Dict[str, StepHandler] = {}


def register_step_type(name: str, handler: StepHandler) -> None:
    """Register a custom step type. Raises if name already taken."""
    if name in _STEP_REGISTRY:
        raise WorkflowError(f"step type {name!r} already registered")
    _STEP_REGISTRY[name] = handler


def _list_step_types() -> List[str]:
    return sorted(_STEP_REGISTRY.keys())


@dataclass
class Workflow:
    name: str
    description: str = ""
    steps: List[Step] = field(default_factory=list)

    @classmethod
    def from_yaml_text(cls, text: str) -> "Workflow":
        data = yaml.safe_load(text) or {}
        return cls._from_dict(data)

    @classmethod
    def from_yaml(cls, path: Path) -> "Workflow":
        return cls.from_yaml_text(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        name = data.get("name")
        if not name:
            raise WorkflowError("workflow missing required field: name")
        steps_raw = data.get("steps") or []
        if not isinstance(steps_raw, list):
            raise WorkflowError("steps must be a list")
        seen_ids = set()
        steps: List[Step] = []
        for i, s in enumerate(steps_raw):
            if not isinstance(s, dict):
                raise WorkflowError(f"step {i} is not a mapping")
            sid = s.get("id")
            stype = s.get("type")
            if not sid or not stype:
                raise WorkflowError(f"step {i} missing 'id' or 'type'")
            if sid in seen_ids:
                raise WorkflowError(f"duplicate step id: {sid!r}")
            seen_ids.add(sid)
            if stype not in _STEP_REGISTRY:
                raise WorkflowError(
                    f"unknown step type: {stype!r}. "
                    f"Known types: {_list_step_types()}"
                )
            err = s.get("on_error", {}) or {}
            steps.append(Step(
                id=sid,
                type=stype,
                inputs=s.get("inputs", {}) or {},
                on_error_retry=int(err.get("retry", 0)),
                on_error_backoff=err.get("backoff", "exponential"),
            ))
        return cls(
            name=name,
            description=data.get("description", ""),
            steps=steps,
        )

    async def run(
        self,
        state: State,
        mailbox: "Mailbox",
        llm: Optional["BaseLLMAdapter"],
        agent_name: str,
        state_db: Optional[Path] = None,
        cancel_event: Optional["asyncio.Event"] = None,
    ) -> State:
        """Execute all steps in order. State is mutated and (optionally) persisted.

        v0.8.0 #1: `cancel_event` is an optional asyncio.Event. If
        provided, the engine checks it BETWEEN steps (not mid-step —
        we don't want to interrupt a half-written LLM call or mailbox
        send). When the event is set, the loop raises WorkflowCancelled
        after the current step returns. The caller is responsible for
        updating the run record + publishing a 'cancelled' event.
        """
        ctx = StepContext(
            mailbox=mailbox, llm=llm, agent_name=agent_name, state_db=state_db,
        )
        for step in self.steps:
            # Cancellation check: between steps only. Step handlers
            # themselves are not interruptible — they'd need their own
            # internal checks (e.g. an LLM call that respects the
            # event between token chunks). For our built-in step types,
            # this is sufficient: receive/llm_call/respond are short.
            if cancel_event is not None and cancel_event.is_set():
                logger.info("workflow %r cancelled before step %s",
                            self.name, step.id)
                raise WorkflowCancelled(
                    f"workflow {self.name!r} cancelled before step {step.id!r}"
                )
            handler = _STEP_REGISTRY[step.type]
            rendered_inputs = {
                k: (state.render(v) if isinstance(v, str) else v)
                for k, v in step.inputs.items()
            }
            attempts = step.on_error_retry + 1
            last_err: Optional[Exception] = None
            for attempt in range(attempts):
                try:
                    await handler(rendered_inputs, state, ctx)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt + 1 < attempts:
                        # Exponential backoff: 1s, 2s, 4s, ...
                        wait = min(2 ** attempt, 30)
                        logger.warning(
                            "step %s attempt %d failed: %s. retrying in %ds",
                            step.id, attempt + 1, e, wait,
                        )
                        import asyncio
                        await asyncio.sleep(wait)
                    # Also check cancellation between retry attempts.
                    if cancel_event is not None and cancel_event.is_set():
                        raise WorkflowCancelled(
                            f"workflow {self.name!r} cancelled mid-retry of {step.id!r}"
                        ) from e
            if last_err is not None:
                raise WorkflowError(
                    f"step {step.id!r} ({step.type}) failed after {attempts} attempts: {last_err}"
                ) from last_err
            # Persist after each step for resume-after-crash
            if state_db is not None:
                state.persist(state_db)
        return state


# ---------------------------------------------------------------------------
# Built-in step handlers
# ---------------------------------------------------------------------------

async def _receive_step(inputs: Dict[str, Any], state: State, ctx: StepContext) -> None:
    """Pull the oldest unread message from this agent's inbox."""
    messages = ctx.mailbox.list_inbox(ctx.agent_name, include_read=False, limit=1)
    if not messages:
        raise WorkflowError(
            f"receive step: no unread messages in {ctx.agent_name!r}'s inbox"
        )
    msg = messages[0]
    # Store the whole message as a dict so it survives SQLite persistence
    state.set("receive", msg.to_dict())


async def _llm_call_step(inputs: Dict[str, Any], state: State, ctx: StepContext) -> None:
    """Call the LLM provider with rendered system+user prompts."""
    if ctx.llm is None:
        raise WorkflowError("llm_call step: no LLM provider configured")
    system_prompt = inputs.get("system", "")
    user_prompt = inputs.get("user", "")
    response = await ctx.llm.chat(system_prompt, user_prompt)
    state.set(inputs.get("output_key", "llm"), response)


async def _respond_step(inputs: Dict[str, Any], state: State, ctx: StepContext) -> None:
    """Send a message to the recipient's inbox."""
    to = inputs.get("to")
    content = inputs.get("content", "")
    if not to:
        raise WorkflowError("respond step: 'to' is required")
    msg = Message(
        from_=ctx.agent_name,
        to=to,
        content=content,
        intent=inputs.get("intent", "respond"),
    )
    ctx.mailbox.send(msg)
    # Also store the sent message in state for downstream steps / debugging
    state.set(inputs.get("output_key", "respond"), msg.to_dict())


# Register built-in step types
register_step_type("receive", _receive_step)
register_step_type("llm_call", _llm_call_step)
register_step_type("respond", _respond_step)
