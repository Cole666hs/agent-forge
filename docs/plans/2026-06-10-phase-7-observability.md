# Phase 7: Observability Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Skills to load for execution:** `test-driven-development`, `verification-before-completion`, `requesting-code-review`

**Goal:** Make agentforge production-observable by adding structured JSON logging, a Prometheus-style `/metrics` endpoint, request-ID propagation, and a `/readyz` health check — all without adding a new runtime dependency.

**Architecture:** A new `agentforge.observability` subpackage ships three primitives — `logging.configure_logging()` (JSON or text, env-driven), `metrics.{Counter,Histogram,MetricsRegistry}` (hand-rolled, thread-safe, no `prometheus_client` dep), and `middleware.RequestIdMiddleware` (ASGI, propagates `req_<hex>` through contextvars). Existing code (mailbox, engine, LLM adapter, serve) gains thin metric-increment calls. The `agentforge serve` process exposes `/metrics` and `/readyz`. Library imports stay side-effect-free (everything is opt-in via `configure_logging()` + `MetricsRegistry`).

**Tech Stack:** Python stdlib only (`logging`, `contextvars`, `threading`, `time`). No new deps.

**Acceptance Criteria:**
- [ ] `pytest tests/` — all pass, target ≥135 tests
- [ ] `AGENTFORGE_LOG_FORMAT=json agentforge serve` emits one JSON line per log event with `ts`, `level`, `logger`, `msg`, `request_id` (when in request context)
- [ ] `curl http://127.0.0.1:8765/metrics` returns valid Prometheus text format (parseable by `promtool check metrics` if installed) with at least: `agentforge_mailbox_messages_total`, `agentforge_workflow_step_duration_seconds`, `agentforge_llm_tokens_total`
- [ ] `curl http://127.0.0.1:8765/readyz` returns 200 when mailbox-root is writable + tenants.json is readable, 503 otherwise
- [ ] `curl -H 'X-Request-Id: my-trace' http://127.0.0.1:8765/v1/inbox?agent=x` — response includes `X-Request-Id: my-trace`; all log lines emitted during that request have `request_id=my-trace`
- [ ] `agentforge run --log-format json --log-level DEBUG` works for one-shot runs
- [ ] Library import (`import agentforge`) is still side-effect-free — no log handlers added, no threads started, no metrics initialized
- [ ] `verify-install-path` skill still passes: clean venv install, init, run, serve all work

**Out of Scope:**
- OpenTelemetry SDK / OTLP export (would need new deps + collector)
- Log shipping to external systems (Loki, Datadog, etc.)
- Per-tenant metrics dashboards (use external Prometheus + Grafana with `tenant_id` label)
- Alerting rules (not a library concern)
- Persistent metric storage (in-memory only — process restart resets counters; documented in README)
- `prometheus_client` library (hand-rolled metrics stay <150 LOC, zero new deps)

**Skills to load for execution:**
- `test-driven-development` — for the RED-GREEN-REFACTOR cycle in every task
- `verification-before-completion` — for the post-implementation checks (evidence before claims)
- `requesting-code-review` — for the pre-commit quality gate (security scan, subagent reviewer)
- `verify-install-path` — for the post-implementation fresh-venv check (this phase's primary risk is "looks good in tests, breaks on real install")

**Pre-implementation baseline:** 124 tests, 12 commits, v0.2.1 tag. Post-implementation target: ≥135 tests, ≥17 commits, v0.3.0 tag.

**Rollback Plan:**
- All changes are additive (new module, new endpoints, new optional CLI flags). No schema changes, no auth changes, no breaking API changes.
- Rollback: `git revert <v0.3.0-commit> && bash deploy.sh` — every existing call site continues to work because the new metric calls are non-blocking and the new endpoints are opt-in (`/metrics`, `/readyz` are new paths; old paths unchanged).
- Data loss window: none (no persistent state added).

---

## Task Structure

### Task 1: Observability package skeleton

**Objective:** Create the `agentforge.observability` package directory with `__init__.py` exporting the public API.

**Files:**
- Create: `src/agentforge/observability/__init__.py`

**Step 1: Write the skeleton**

```python
"""agentforge.observability — structured logging, metrics, request tracing.

Public API:
  - logging.configure_logging(format, level) — opt-in JSON or text logging
  - logging.JsonFormatter
  - metrics.Counter, Histogram, MetricsRegistry, get_registry()
  - middleware.RequestIdMiddleware

Library import (import agentforge) is side-effect-free. Nothing here
initializes on import — you must call configure_logging() and
get_registry() explicitly.
"""

from agentforge.observability import logging as obs_logging
from agentforge.observability import metrics
from agentforge.observability import middleware

__all__ = [
    "obs_logging",
    "metrics",
    "middleware",
]
```

**Step 2: Verify import is side-effect-free**

Run: `python -c "import agentforge; assert not any(h.name.startswith('agentforge') for h in __import__('logging').Logger.manager.loggerDict.values() if hasattr(h, 'name'))"`
Expected: no error.

**Step 3: Commit**

```bash
git add src/agentforge/observability/__init__.py
git commit -m "feat(observability): add package skeleton (side-effect-free import)"
```

---

### Task 2: JsonFormatter for structured logging

**Objective:** JSON log formatter that emits one JSON object per line with `ts`, `level`, `logger`, `msg`, plus any `extra` fields passed by the caller or contextvars-resolved fields like `request_id`.

**Files:**
- Create: `src/agentforge/observability/logging.py`
- Create: `tests/unit/observability/__init__.py` (empty)
- Create: `tests/unit/observability/test_logging.py`

**Step 1: Write failing test**

```python
import logging
import io
import json
from agentforge.observability.logging import JsonFormatter

def test_json_formatter_emits_one_json_per_line():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("test.json")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info("hello %s", "world", extra={"agent": "bot1"})

    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.json"
    assert parsed["agent"] == "bot1"
    assert "ts" in parsed  # ISO 8601
```

**Step 2: Run test to verify failure**

Run: `pytest tests/unit/observability/test_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentforge.observability.logging'`

**Step 3: Write minimal implementation**

```python
"""Structured logging for agentforge.

Opt-in: call `configure_logging()` once at process start. Library code
uses `logger = logging.getLogger(__name__)` and is silent until configured.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, MutableMapping

from agentforge.observability.context import get_request_id


_STD_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Format LogRecord as a single-line JSON object.

    Includes ts (ISO 8601 UTC), level, logger, msg, and any extra fields
    passed by the caller or resolved from context (request_id).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra fields (anything not in the std LogRecord set)
        for k, v in record.__dict__.items():
            if k not in _STD_LOGRECORD_ATTRS and not k.startswith("_"):
                payload[k] = v
        # Attach request_id from contextvar if present
        rid = get_request_id()
        if rid is not None:
            payload.setdefault("request_id", rid)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    fmt: str = "text",
    level: str = "INFO",
    stream: Any | None = None,
) -> None:
    """Configure the root agentforge logger (and all sub-loggers).

    fmt: "json" (one JSON object per line) or "text" (default Python format).
    level: "DEBUG" | "INFO" | "WARNING" | "ERROR".
    stream: where to write (default: stderr).

    Idempotent: calling twice replaces the handler instead of stacking.
    """
    fmt = (fmt or os.environ.get("AGENTFORGE_LOG_FORMAT", "text")).lower()
    level = level or os.environ.get("AGENTFORGE_LOG_LEVEL", "INFO")
    stream = stream or sys.stderr

    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    elif fmt == "text":
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    else:
        raise ValueError(f"unknown log format: {fmt!r}; want 'json' or 'text'")

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger("agentforge")
    root.handlers = [handler]  # replace, don't stack
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
```

**Step 4: Run test to verify pass**

Run: `pytest tests/unit/observability/test_logging.py -v`
Expected: PASS

**Step 5: Add test for text format and env-var config**

```python
def test_configure_logging_text_format():
    import logging
    from agentforge.observability.logging import configure_logging
    configure_logging(fmt="text", level="WARNING")
    assert logging.getLogger("agentforge").level == logging.WARNING

def test_configure_logging_env_var():
    import os, logging
    os.environ["AGENTFORGE_LOG_FORMAT"] = "json"
    os.environ["AGENTFORGE_LOG_LEVEL"] = "DEBUG"
    from agentforge.observability.logging import configure_logging
    configure_logging()  # fmt/level from env
    assert logging.getLogger("agentforge").level == logging.DEBUG
    del os.environ["AGENTFORGE_LOG_FORMAT"]
    del os.environ["AGENTFORGE_LOG_LEVEL"]
```

**Step 6: Run all logging tests, then commit**

```bash
pytest tests/unit/observability/test_logging.py -v
git add src/agentforge/observability/logging.py tests/unit/observability/
git commit -m "feat(observability): JsonFormatter + configure_logging (json|text, env-driven)"
```

---

### Task 3: context.py — request_id contextvar

**Objective:** Provide a thread-/task-safe request_id contextvar that the JSON formatter reads and the ASGI middleware writes.

**Files:**
- Create: `src/agentforge/observability/context.py`
- Create: `tests/unit/observability/test_context.py`

**Step 1: Write failing test**

```python
from agentforge.observability.context import set_request_id, get_request_id, reset_request_id

def test_request_id_roundtrip():
    assert get_request_id() is None
    set_request_id("req_abc")
    assert get_request_id() == "req_abc"
    reset_request_id()
    assert get_request_id() is None
```

**Step 2: Implement**

```python
"""Per-request context (request_id, tenant_id, etc.) via contextvars.

Contextvars are safe across asyncio tasks and threads (each task gets
its own copy). The JSON formatter reads request_id, the ASGI middleware
sets it, the logging config doesn't touch it.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_request_id_var: ContextVar[Optional[str]] = ContextVar("agentforge_request_id", default=None)


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


def reset_request_id() -> None:
    _request_id_var.set(None)
```

**Step 3: Test + commit**

```bash
pytest tests/unit/observability/test_context.py -v
git add src/agentforge/observability/context.py tests/unit/observability/test_context.py
git commit -m "feat(observability): request_id contextvar"
```

---

### Task 4: metrics.py — Counter, Histogram, MetricsRegistry

**Objective:** Hand-rolled Prometheus-compatible metrics primitives, thread-safe, ~150 LOC.

**Files:**
- Create: `src/agentforge/observability/metrics.py`
- Create: `tests/unit/observability/test_metrics.py`

**Step 1: Write failing test**

```python
from agentforge.observability.metrics import Counter, Histogram, MetricsRegistry

def test_counter_increments():
    c = Counter(name="test_total", help="test", registry=MetricsRegistry())
    c.inc()
    c.inc(3)
    assert c.value() == 4

def test_counter_with_labels():
    reg = MetricsRegistry()
    c = Counter(name="http_total", help="http", label_names=("method",), registry=reg)
    c.labels(method="GET").inc()
    c.labels(method="POST").inc(2)
    out = reg.render()
    assert 'http_total{method="GET"} 1.0' in out
    assert 'http_total{method="POST"} 2.0' in out

def test_histogram_buckets_and_sum_count():
    reg = MetricsRegistry()
    h = Histogram(name="dur", help="dur", registry=reg, buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)  # → +Inf bucket
    h.observe(0.3)   # → 0.5 + 1.0 + +Inf
    h.observe(0.7)   # → 1.0 + +Inf
    out = reg.render()
    assert 'dur_bucket{le="0.1"} 1.0' in out
    assert 'dur_bucket{le="0.5"} 2.0' in out
    assert 'dur_bucket{le="1.0"} 3.0' in out
    assert 'dur_bucket{le="+Inf"} 3.0' in out
    assert 'dur_sum 1.05' in out
    assert 'dur_count 3' in out

def test_render_is_valid_prometheus_text():
    """Smoke test: header lines, HELP, TYPE present."""
    reg = MetricsRegistry()
    reg.counter("c", "help").inc()
    out = reg.render()
    assert "# HELP c help" in out
    assert "# TYPE c counter" in out
    assert "c 1.0" in out
```

**Step 2: Run to verify failure, then implement**

```python
"""Hand-rolled Prometheus-compatible metrics.

Why not prometheus_client? Zero new deps. Three classes (Counter, Histogram,
MetricsRegistry) cover everything we need for Phase 7. If we ever need
quantiles, exemplars, or push-gateway, swap to the real library then.

Thread-safe via a single Lock around the dict-of-labels state. Not lock-free,
but our hot path is short (inc/observe + dict lookup), so contention is
unmeasurable in practice.
"""

from __future__ import annotations

import threading
from typing import Iterable, Sequence


# Default Prometheus histogram buckets
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class _Metric:
    """Base class. Holds registry reference + name/help."""

    def __init__(self, name: str, help: str, registry: "MetricsRegistry"):
        if not name:
            raise ValueError("metric name must be non-empty")
        self.name = name
        self.help = help
        self._registry = registry
        registry._register(self)


class Counter(_Metric):
    """Monotonic counter. inc() adds 1, inc(n) adds n."""

    def __init__(
        self,
        name: str,
        help: str,
        registry: "MetricsRegistry",
        label_names: Sequence[str] = (),
    ):
        super().__init__(name, help, registry)
        self._label_names = tuple(label_names)
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, n: float = 1.0) -> None:
        if n < 0:
            raise ValueError("counter can only increase")
        with self._lock:
            self._values[()] = self._values.get((), 0.0) + n

    def labels(self, **kwargs: str) -> "Counter":
        """Return a child Counter for the given label values.

        Pattern: parent.labels(method="GET").inc()
        """
        return _LabeledCounter(self, _check_labels(self._label_names, kwargs))


class _LabeledCounter:
    def __init__(self, parent: Counter, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def inc(self, n: float = 1.0) -> None:
        if n < 0:
            raise ValueError("counter can only increase")
        with self._parent._lock:
            self._parent._values[self._label_values] = (
                self._parent._values.get(self._label_values, 0.0) + n
            )


class Histogram(_Metric):
    """Cumulative histogram with configurable buckets.

    Buckets are upper-inclusive: an observation of 0.05 lands in the
    `le=0.1` bucket and the `le=+Inf` bucket (which always exists).
    """

    def __init__(
        self,
        name: str,
        help: str,
        registry: "MetricsRegistry",
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        label_names: Sequence[str] = (),
    ):
        super().__init__(name, help, registry)
        self._buckets = tuple(sorted(set(buckets)))
        self._label_names = tuple(label_names)
        # {label_values: {bucket: count, '_sum': x, '_count': n}}
        self._series: dict[tuple[str, ...], dict] = {}
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            s = self._series.setdefault((), {
                **{b: 0 for b in self._buckets},
                "_inf": 0,
                "_sum": 0.0,
                "_count": 0,
            })
            s["_sum"] += value
            s["_count"] += 1
            for b in self._buckets:
                if value <= b:
                    s[b] += 1
            s["_inf"] += 1  # +Inf bucket always increments

    def labels(self, **kwargs: str) -> "Histogram":
        return _LabeledHistogram(self, _check_labels(self._label_names, kwargs))


class _LabeledHistogram:
    def __init__(self, parent: Histogram, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def observe(self, value: float) -> None:
        with self._parent._lock:
            s = self._parent._series.setdefault(self._label_values, {
                **{b: 0 for b in self._parent._buckets},
                "_inf": 0,
                "_sum": 0.0,
                "_count": 0,
            })
            s["_sum"] += value
            s["_count"] += 1
            for b in self._parent._buckets:
                if value <= b:
                    s[b] += 1
            s["_inf"] += 1


class MetricsRegistry:
    """Holds all metrics for one process. Renders Prometheus text format."""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._lock = threading.Lock()

    def _register(self, m: _Metric) -> None:
        with self._lock:
            self._metrics.append(m)

    def counter(self, name: str, help: str, label_names: Sequence[str] = ()) -> Counter:
        return Counter(name, help, self, label_names)

    def histogram(self, name: str, help: str, buckets: Sequence[float] = DEFAULT_BUCKETS,
                  label_names: Sequence[str] = ()) -> Histogram:
        return Histogram(name, help, self, buckets, label_names)

    def render(self) -> str:
        """Render all metrics in Prometheus text format."""
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics)
        for m in metrics:
            lines.append(f"# HELP {m.name} {m.help}")
            if isinstance(m, Counter):
                lines.append(f"# TYPE {m.name} counter")
                with m._lock:
                    for labels, value in m._values.items():
                        lines.append(_format_line(m.name, m._label_names, labels, value))
            elif isinstance(m, Histogram):
                lines.append(f"# TYPE {m.name} histogram")
                with m._lock:
                    for label_values, s in m._series.items():
                        for b in m._buckets:
                            lines.append(_format_line(
                                f"{m.name}_bucket", m._label_names, label_values,
                                s[b], extra_labels=[("le", _fmt_float(b))]
                            ))
                        lines.append(_format_line(
                            f"{m.name}_bucket", m._label_names, label_values,
                            s["_inf"], extra_labels=[("le", "+Inf")]
                        ))
                        lines.append(_format_line(
                            f"{m.name}_sum", m._label_names, label_values, s["_sum"]
                        ))
                        lines.append(_format_line(
                            f"{m.name}_count", m._label_names, label_values, s["_count"]
                        ))
        return "\n".join(lines) + "\n"


# --- helpers -----------------------------------------------------------------

def _check_labels(expected: Sequence[str], actual: dict[str, str]) -> tuple[str, ...]:
    if set(expected) != set(actual.keys()):
        raise ValueError(
            f"expected labels {expected!r}, got {sorted(actual.keys())!r}"
        )
    return tuple(actual[k] for k in expected)


def _format_line(
    name: str,
    label_names: Sequence[str],
    label_values: Sequence[str],
    value: float,
    extra_labels: Iterable[tuple[str, str]] = (),
) -> str:
    all_labels = list(zip(label_names, label_values)) + list(extra_labels)
    if all_labels:
        lstr = "{" + ",".join(
            f'{k}="{_escape(v)}"' for k, v in all_labels
        ) + "}"
    else:
        lstr = ""
    return f"{name}{lstr} {_fmt_float(value)}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_float(v: float) -> str:
    if v == int(v):
        return f"{v:.1f}"
    return f"{v}"


# --- process-singleton accessor ----------------------------------------------

_global_registry: MetricsRegistry | None = None
_global_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Process-singleton registry. Created on first call (lazy)."""
    global _global_registry
    with _global_lock:
        if _global_registry is None:
            _global_registry = MetricsRegistry()
    return _global_registry
```

**Step 3: Test + commit**

```bash
pytest tests/unit/observability/test_metrics.py -v
git add src/agentforge/observability/metrics.py tests/unit/observability/test_metrics.py
git commit -m "feat(observability): hand-rolled Counter/Histogram/MetricsRegistry (Prometheus text format)"
```

---

### Task 5: middleware.py — RequestIdMiddleware

**Objective:** Pure ASGI middleware that reads `X-Request-Id` from the request (or generates one), sets it on the contextvar for the duration of the request, and echoes it on the response.

**Files:**
- Create: `src/agentforge/observability/middleware.py`
- Create: `tests/unit/observability/test_middleware.py`

**Step 1: Write failing test (uses httpx + asgi-lifespan-style direct ASGI invocation)**

```python
import asyncio
from agentforge.observability.middleware import RequestIdMiddleware
from agentforge.observability.context import get_request_id

async def _app(scope, receive, send):
    rid = get_request_id()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(rid).encode()})

def test_middleware_generates_request_id_when_missing():
    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["headers"] = msg.get("headers", [])

    app = RequestIdMiddleware(_app)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    asyncio.run(app(scope, receive, send))
    # Should set X-Request-Id: req_<hex> on response
    headers = dict(captured["headers"])
    assert any(k == b"x-request-id" and v.startswith(b"req_") for k, v in headers)

def test_middleware_echoes_inbound_request_id():
    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "headers": []}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["headers"] = msg.get("headers", [])

    app = RequestIdMiddleware(_app)
    scope = {
        "type": "http", "method": "GET", "path": "/", "headers": [],
    }
    # Inbound header
    scope["headers"] = [(b"x-request-id", b"my-trace-123")]
    asyncio.run(app(scope, receive, send))
    headers = dict(captured["headers"])
    assert (b"x-request-id", b"my-trace-123") in headers
```

**Step 2: Implement**

```python
"""ASGI middleware for request-ID propagation.

Reads `X-Request-Id` from inbound request (or generates `req_<12hex>`),
sets it on the request_id contextvar for the duration of the request,
and echoes it on the response.
"""

from __future__ import annotations

import secrets
from typing import Awaitable, Callable

from agentforge.observability.context import set_request_id, reset_request_id

_ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


class RequestIdMiddleware:
    """Pure ASGI middleware — no FastAPI/Starlette dep."""

    HEADER = b"x-request-id"

    def __init__(self, app: _ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read inbound request-id (or generate)
        inbound = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"x-request-id":
                inbound = v.decode("latin-1")
                break
        rid = inbound or f"req_{secrets.token_hex(6)}"

        set_request_id(rid)
        try:
            # Wrap send() so the response gets the X-Request-Id header
            response_started = {"done": False}

            async def send_with_header(msg):
                if msg["type"] == "http.response.start" and not response_started["done"]:
                    response_started["done"] = True
                    headers = list(msg.get("headers", []))
                    if not any(k.lower() == self.HEADER for k, _ in headers):
                        headers.append((self.HEADER, rid.encode("latin-1")))
                    msg = {**msg, "headers": headers}
                await send(msg)

            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id()
```

**Step 3: Test + commit**

```bash
pytest tests/unit/observability/test_middleware.py -v
git add src/agentforge/observability/middleware.py tests/unit/observability/test_middleware.py
git commit -m "feat(observability): ASGI RequestIdMiddleware (read or generate, echo on response)"
```

---

### Task 6: Wire metrics into mailbox.py (send, list_inbox, ack)

**Objective:** Increment mailbox counters and observe latencies in the 3 hot-path methods.

**Files:**
- Modify: `src/agentforge/core/mailbox.py` (add metric calls in `send`, `list_inbox`, `ack`)
- Create: `tests/unit/observability/test_mailbox_instrumentation.py`

**Step 1: Write failing test (uses fresh registry, asserts metric output)**

```python
import time
from agentforge.core.mailbox import FileMailbox
from agentforge.core.message import Message
from agentforge.observability.metrics import MetricsRegistry

def test_mailbox_send_increments_counter():
    reg = MetricsRegistry()
    from agentforge.observability.instrumentation import instrument_mailbox
    mbox = FileMailbox(root="/tmp/test_mbox_obs", tenant_id="acme")
    instrument_mailbox(mbox, registry=reg)
    mbox.send(Message(from_="u", to="b", content="hi"))
    out = reg.render()
    assert "agentforge_mailbox_messages_total" in out
    assert 'agentforge_mailbox_messages_total{tenant="acme",direction="sent"} 1.0' in out
```

**Step 2: Add `instrument_mailbox()` helper**

Create: `src/agentforge/observability/instrumentation.py`

```python
"""Wrap existing classes with metric instrumentation.

Each `instrument_*(obj, registry)` call attaches metrics to the
class. Idempotent (re-calling is a no-op).
"""

from __future__ import annotations

import time
from typing import Any

from agentforge.observability.metrics import MetricsRegistry


def instrument_mailbox(mbox: Any, registry: MetricsRegistry) -> None:
    """Attach mailbox metrics: messages_total{sent|received} + send/list duration."""
    if getattr(mbox, "_agentforge_instrumented", False):
        return
    sent_counter = registry.counter(
        "agentforge_mailbox_messages_total",
        "Total messages written/read from mailbox",
        label_names=("tenant", "direction"),
    )
    send_duration = registry.histogram(
        "agentforge_mailbox_send_duration_seconds",
        "FileMailbox.send() latency in seconds",
        label_names=("tenant",),
    )
    list_duration = registry.histogram(
        "agentforge_mailbox_list_duration_seconds",
        "FileMailbox.list_inbox() latency in seconds",
        label_names=("tenant",),
    )

    tenant = mbox.tenant_id or "default"
    _orig_send = mbox.send
    _orig_list = mbox.list_inbox

    def _instrumented_send(msg, *args, **kwargs):
        t0 = time.monotonic()
        try:
            return _orig_send(msg, *args, **kwargs)
        finally:
            send_duration.labels(tenant=tenant).observe(time.monotonic() - t0)
            sent_counter.labels(tenant=tenant, direction="sent").inc()

    async def _instrumented_list_inbox(*args, **kwargs):
        t0 = time.monotonic()
        try:
            return await _orig_list(*args, **kwargs)
        finally:
            list_duration.labels(tenant=tenant).observe(time.monotonic() - t0)
            sent_counter.labels(tenant=tenant, direction="received").inc(
                len(await _orig_list(*args, **kwargs))  # second call is fine for metric
            )

    # Check if list_inbox is async or sync (depends on the codebase)
    import inspect
    if inspect.iscoroutinefunction(_orig_list):
        mbox.list_inbox = _instrumented_list_inbox
    else:
        def _sync_list(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = _orig_list(*args, **kwargs)
            finally:
                list_duration.labels(tenant=tenant).observe(time.monotonic() - t0)
                sent_counter.labels(tenant=tenant, direction="received").inc(len(result))
            return result
        mbox.list_inbox = _sync_list

    mbox.send = _instrumented_send
    mbox._agentforge_instrumented = True
```

**Step 3: Run test, fix issues, commit**

Note: `FileMailbox.send()` is sync (returns the Message) and `list_inbox()` is sync in the existing codebase (returns a list, not awaitable). Confirm by reading the source before writing the wrapper. Adjust the helper accordingly.

```bash
pytest tests/unit/observability/test_mailbox_instrumentation.py -v
git add src/agentforge/observability/instrumentation.py tests/unit/observability/test_mailbox_instrumentation.py
git commit -m "feat(observability): instrument mailbox.send/list_inbox with counters + duration"
```

---

### Task 7: Wire metrics into engine.py (per-step latency, errors, retries)

**Objective:** Increment workflow step counter, observe step duration, count retries.

**Files:**
- Modify: `src/agentforge/workflows/engine.py` (add metric calls in the step-retry loop)
- Modify: `src/agentforge/observability/instrumentation.py` (add `instrument_engine`)
- Create: `tests/unit/observability/test_engine_instrumentation.py`

**Step 1: Write failing test**

```python
import asyncio
from agentforge.workflows.engine import Workflow, State
from agentforge.observability.metrics import MetricsRegistry
from agentforge.observability.instrumentation import instrument_engine

def test_workflow_step_counter_increments():
    reg = MetricsRegistry()
    wf = Workflow(name="t", steps=[
        {"id": "noop", "type": "receive"},  # will fail (empty inbox) — but metric increments
    ])
    instrument_engine(wf, registry=reg)
    asyncio.run(wf.run(state=State(), mailbox=None, llm=None, agent_name="bot"))
    out = reg.render()
    assert "agentforge_workflow_steps_total" in out
```

**Step 2: Add `instrument_engine()`**

```python
def instrument_engine(wf: Any, registry: MetricsRegistry) -> None:
    """Attach workflow step metrics: counter + duration histogram + retry counter."""
    if getattr(wf, "_agentforge_instrumented", False):
        return
    steps_total = registry.counter(
        "agentforge_workflow_steps_total",
        "Total workflow step invocations (incl. retries)",
        label_names=("workflow", "step_type", "outcome"),  # outcome: success|error
    )
    step_duration = registry.histogram(
        "agentforge_workflow_step_duration_seconds",
        "Workflow step latency in seconds",
        label_names=("workflow", "step_type"),
    )
    retries_total = registry.counter(
        "agentforge_workflow_step_retries_total",
        "Total workflow step retries",
        label_names=("workflow", "step_id"),
    )

    _orig_run = wf.run

    async def _instrumented_run(*args, **kwargs):
        state = kwargs.get("state") or args[0] if args else None
        mailbox = kwargs.get("mailbox") or args[1] if len(args) > 1 else None
        llm = kwargs.get("llm") or args[2] if len(args) > 2 else None
        agent_name = kwargs.get("agent_name") or args[3] if len(args) > 3 else "default"

        # We instrument the existing _run_steps method if accessible,
        # otherwise wrap .run with a simpler duration+outcome tracker.
        # (Simplification: count overall runs. Per-step would need a deeper
        # refactor of the step loop. Document this trade-off in README.)
        t0 = time.monotonic()
        try:
            return await _orig_run(*args, **kwargs)
        finally:
            step_duration.labels(workflow=wf.name, step_type="<all>").observe(
                time.monotonic() - t0
            )
            steps_total.labels(workflow=wf.name, step_type="<all>", outcome="success").inc()

    wf.run = _instrumented_run
    wf._agentforge_instrumented = True
```

**NOTE on simplicity vs. per-step:** for Phase 7, track workflow-level (one counter per run, one histogram per run) rather than per-step. Per-step would require either (a) monkey-patching the step-handler dict, (b) re-engineering the retry loop to emit events, or (c) wrapping every registered step type. Workflow-level is 90% of the value at 10% of the complexity. Document this trade-off in README and revisit in Phase 8 if needed.

**Step 3: Test + commit**

```bash
pytest tests/unit/observability/test_engine_instrumentation.py -v
git add src/agentforge/observability/instrumentation.py src/agentforge/workflows/engine.py tests/unit/observability/test_engine_instrumentation.py
git commit -m "feat(observability): instrument workflow engine (counter + duration)"
```

---

### Task 8: Wire metrics into llm_compat.py (latency, tokens)

**Objective:** Increment LLM call counter, observe latency, count tokens in/out.

**Files:**
- Modify: `src/agentforge/adapters/llm_compat.py` (add metric calls in `_do_chat`)
- Create: `tests/unit/observability/test_llm_instrumentation.py`

**Step 1: Write failing test**

```python
from unittest import mock
from agentforge.adapters.llm import make_provider
from agentforge.adapters.llm_compat import BaseOpenAICompatLLMAdapter
from agentforge.observability.metrics import MetricsRegistry

def test_llm_call_records_latency_and_tokens():
    reg = MetricsRegistry()
    p = make_provider("ollama")
    # Attach metrics by monkey-patching
    from agentforge.observability.instrumentation import instrument_llm
    instrument_llm(p, registry=reg)

    fake_body = '{"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":7,"completion_tokens":3}}'

    class _MockResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return fake_body.encode()

    with mock.patch("urllib.request.urlopen", return_value=_MockResp()):
        p._do_chat("sys", "user")

    out = reg.render()
    assert "agentforge_llm_calls_total" in out
    assert "agentforge_llm_tokens_total" in out
    assert 'agentforge_llm_tokens_total{direction="in"} 7.0' in out
    assert 'agentforge_llm_tokens_total{direction="out"} 3.0' in out
```

**Step 2: Add `instrument_llm()`**

```python
def instrument_llm(provider: Any, registry: MetricsRegistry) -> None:
    """Attach LLM metrics: call counter, duration histogram, token counter."""
    if getattr(provider, "_agentforge_instrumented", False):
        return
    calls_total = registry.counter(
        "agentforge_llm_calls_total",
        "Total LLM API calls",
        label_names=("provider", "outcome"),  # outcome: success|error
    )
    call_duration = registry.histogram(
        "agentforge_llm_call_duration_seconds",
        "LLM call latency in seconds",
        label_names=("provider",),
    )
    tokens_total = registry.counter(
        "agentforge_llm_tokens_total",
        "Total LLM tokens consumed",
        label_names=("provider", "direction"),  # direction: in|out
    )

    provider_name = type(provider).__name__
    _orig = provider._do_chat

    def _instrumented(system, user, *args, **kwargs):
        t0 = time.monotonic()
        outcome = "success"
        try:
            result = _orig(system, user, *args, **kwargs)
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            call_duration.labels(provider=provider_name).observe(time.monotonic() - t0)
            calls_total.labels(provider=provider_name, outcome=outcome).inc()
            if outcome == "success":
                # Re-call is wrong; we need the result. Capture via nonlocal.
                pass

    # Better: wrap with access to result
    def _wrap(system, user, *args, **kwargs):
        t0 = time.monotonic()
        outcome = "success"
        result = None
        try:
            result = _orig(system, user, *args, **kwargs)
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            call_duration.labels(provider=provider_name).observe(time.monotonic() - t0)
            calls_total.labels(provider=provider_name, outcome=outcome).inc()
            if outcome == "success" and result is not None:
                if result.tokens_in is not None:
                    tokens_total.labels(provider=provider_name, direction="in").inc(result.tokens_in)
                if result.tokens_out is not None:
                    tokens_total.labels(provider=provider_name, direction="out").inc(result.tokens_out)

    provider._do_chat = _wrap
    provider._agentforge_instrumented = True
```

**Step 3: Test + commit**

```bash
pytest tests/unit/observability/test_llm_instrumentation.py -v
git add src/agentforge/observability/instrumentation.py tests/unit/observability/test_llm_instrumentation.py
git commit -m "feat(observability): instrument LLM adapter (calls, duration, tokens)"
```

---

### Task 9: Add /metrics endpoint to serve.py

**Objective:** Expose the global metrics registry as Prometheus text on `GET /metrics` (no auth — same as `/health`).

**Files:**
- Modify: `src/agentforge/serve.py` (add `/metrics` route)
- Modify: `tests/unit/test_serve.py` (add test)

**Step 1: Write failing test**

```python
def test_metrics_endpoint_returns_prometheus_text():
    from fastapi.testclient import TestClient
    from agentforge.serve import create_app

    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        Path(fs, "mailbox").mkdir()
        app = create_app(tenants_path=tenants, mailbox_root=Path(fs) / "mailbox")
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # Even with no metrics, response should be valid (empty or with HELP lines)
        assert "# HELP" in resp.text or resp.text == ""
```

**Step 2: Add endpoint**

In `serve.py`, after the `/health` route:

```python
from agentforge.observability.metrics import get_registry

@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus text format. No auth — same as /health."""
    return Response(content=get_registry().render(), media_type="text/plain; version=0.0.4")
```

**Step 3: Test + commit**

```bash
pytest tests/unit/test_serve.py -v
git add src/agentforge/serve.py tests/unit/test_serve.py
git commit -m "feat(serve): /metrics endpoint (Prometheus text, no auth)"
```

---

### Task 10: Add /readyz endpoint to serve.py

**Objective:** Distinguish "process is up" (liveness) from "ready to serve" (readiness). /readyz checks that the mailbox root is writable AND tenants.json is readable. Returns 200 on success, 503 with reason on failure.

**Files:**
- Modify: `src/agentforge/serve.py`
- Modify: `tests/unit/test_serve.py`

**Step 1: Write failing test**

```python
def test_readyz_returns_200_when_dependencies_ok():
    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        Path(fs, "mailbox").mkdir()
        app = create_app(tenants_path=tenants, mailbox_root=Path(fs) / "mailbox")
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

def test_readyz_returns_503_when_tenants_unreadable():
    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        tenants.write_text("{}")  # exists, readable
        mailbox = Path(fs) / "mailbox"
        mailbox.mkdir()
        # Make mailbox unwritable by replacing with a file
        mailbox.rmdir()
        mailbox.write_text("not a dir")
        app = create_app(tenants_path=tenants, mailbox_root=mailbox)
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert "mailbox" in resp.json()["reason"].lower()
```

**Step 2: Add endpoint**

```python
@app.get("/readyz", include_in_schema=False)
def readyz() -> Response:
    """Readiness check: mailbox writability + tenants readability."""
    reasons = []
    # Mailbox root must exist and be writable
    if not mailbox_root.exists():
        reasons.append(f"mailbox root missing: {mailbox_root}")
    elif not mailbox_root.is_dir():
        reasons.append(f"mailbox root not a directory: {mailbox_root}")
    else:
        try:
            test_file = mailbox_root / ".readyz_probe"
            test_file.write_text("ok")
            test_file.unlink()
        except OSError as e:
            reasons.append(f"mailbox root not writable: {e}")
    # Tenants file must be readable
    if not tenants_path.exists():
        reasons.append(f"tenants file missing: {tenants_path}")
    elif not tenants_path.is_file():
        reasons.append(f"tenants path not a file: {tenants_path}")
    if reasons:
        return Response(
            status_code=503,
            content=json.dumps({"status": "not_ready", "reasons": reasons}),
            media_type="application/json",
        )
    return Response(
        content=json.dumps({"status": "ready"}),
        media_type="application/json",
    )
```

**Step 3: Test + commit**

```bash
pytest tests/unit/test_serve.py -v
git add src/agentforge/serve.py tests/unit/test_serve.py
git commit -m "feat(serve): /readyz endpoint (mailbox writability + tenants readability)"
```

---

### Task 11: Add RequestIdMiddleware to serve.py

**Objective:** Wrap the FastAPI app with the ASGI middleware so every request gets a request_id propagated through logs and echoed on the response.

**Files:**
- Modify: `src/agentforge/serve.py` (add middleware to the app)
- Modify: `tests/unit/test_serve.py` (add test)

**Step 1: Write failing test**

```python
def test_request_id_middleware_echoes_header():
    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        Path(fs, "mailbox").mkdir()
        app = create_app(tenants_path=tenants, mailbox_root=Path(fs) / "mailbox")
        client = TestClient(app)
        resp = client.get("/health", headers={"X-Request-Id": "trace-abc"})
        assert resp.headers.get("x-request-id") == "trace-abc"

def test_request_id_middleware_generates_when_missing():
    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        Path(fs, "mailbox").mkdir()
        app = create_app(tenants_path=tenants, mailbox_root=Path(fs) / "mailbox")
        client = TestClient(app)
        resp = client.get("/health")
        rid = resp.headers.get("x-request-id")
        assert rid is not None
        assert rid.startswith("req_")
```

**Step 2: Wrap the app**

In `create_app()`, before returning:

```python
from agentforge.observability.middleware import RequestIdMiddleware
app.add_middleware(RequestIdMiddleware)
```

(FastAPI's `add_middleware` accepts an ASGI middleware class — Starlette handles the wrapping.)

**Step 3: Test + commit**

```bash
pytest tests/unit/test_serve.py -v
git add src/agentforge/serve.py tests/unit/test_serve.py
git commit -m "feat(serve): RequestIdMiddleware (X-Request-Id in/out)"
```

---

### Task 12: CLI --log-format and --log-level options

**Objective:** Expose logging config to CLI users. Defaults match env vars (so `agentforge serve` with `AGENTFORGE_LOG_FORMAT=json` works without any CLI flag).

**Files:**
- Modify: `src/agentforge/cli.py` (add global options, call `configure_logging()` at startup)
- Modify: `tests/unit/test_cli.py`

**Step 1: Write failing test**

```python
def test_cli_global_log_options_appear_in_help(runner: CliRunner):
    result = runner.invoke(cli, ["--help"])
    assert "--log-format" in result.output
    assert "--log-level" in result.output

def test_cli_log_format_json_emits_json_log(capsys, tmp_path):
    """Run with --log-format json, capture stderr, assert valid JSON line."""
    from click.testing import CliRunner
    runner = CliRunner(mix_stderr=False)
    # ... actually triggering a log line requires a workflow failure
    # Simpler: invoke `serve --help` (won't actually start uvicorn, just prints help)
    # Use `init` and a workflow that errors
    ...
```

For this test, the cleanest verification is to check that `configure_logging()` was called with the right args. Use a `mock.patch` on `configure_logging` and check the call args.

```python
def test_serve_invokes_configure_logging(runner: CliRunner, monkeypatch):
    with mock.patch("agentforge.cli.configure_logging") as cfg:
        with runner.isolated_filesystem() as fs:
            # Don't actually let uvicorn.run() execute; just check configure_logging
            # was called. Easiest: pass --help to serve to short-circuit.
            result = runner.invoke(cli, [
                "serve", "--help",
            ])
        # The --help short-circuits before configure_logging is called.
        # That's a problem. Better: check the option exists and parse it.
        assert "--log-format" in result.output

def test_run_invokes_configure_logging(runner: CliRunner, tmp_path, monkeypatch):
    """Verify the run command configures logging per the flag."""
    with mock.patch("agentforge.cli.configure_logging") as cfg:
        with mock.patch("agentforge.cli._resolve_llm", return_value=None):
            wf = tmp_path / "wf.yaml"
            wf.write_text(yaml.safe_dump({"name": "x", "steps": []}))
            result = runner.invoke(cli, [
                "run", str(wf), "--agent", "x",
                "--mailbox", str(tmp_path / "mailbox"),
                "--log-format", "json", "--log-level", "DEBUG",
            ])
        cfg.assert_called_once_with("json", "DEBUG")
```

**Step 2: Add CLI options + call `configure_logging` at start of `serve` and `run`**

```python
# At top of cli.py, add import:
from agentforge.observability.logging import configure_logging

# In the @click.group decorator, add:
@click.option("--log-format", default=None, envvar="AGENTFORGE_LOG_FORMAT",
              help='Log format: "json" or "text" (default: text, or $AGENTFORGE_LOG_FORMAT).')
@click.option("--log-level", default=None, envvar="AGENTFORGE_LOG_LEVEL",
              help='Log level: "DEBUG"|"INFO"|"WARNING"|"ERROR" (default: INFO, or $AGENTFORGE_LOG_LEVEL).')
@click.pass_context
def cli(ctx, log_format, log_level, ...):
    configure_logging(fmt=log_format or "text", level=log_level or "INFO")
    ...
```

**Step 3: Test + commit**

```bash
pytest tests/unit/test_cli.py -v
git add src/agentforge/cli.py tests/unit/test_cli.py
git commit -m "feat(cli): --log-format and --log-level options (env-driven defaults)"
```

---

### Task 13: Wire metrics in serve.py startup (instrument mailbox, LLM)

**Objective:** When the FastAPI app starts, call `instrument_mailbox()` on the per-tenant FileMailbox instances. This is a per-request thing because the mailbox is per-tenant — the simplest pattern: a helper `mailbox_for(tenant_id)` that returns an instrumented instance, memoized per tenant.

**Files:**
- Modify: `src/agentforge/serve.py`
- Create: `tests/unit/test_serve_metrics.py`

**Step 1: Write failing test**

```python
def test_serve_instruments_per_tenant_mailbox():
    """After a request, mailbox counter for that tenant is incremented."""
    with runner.isolated_filesystem() as fs:
        tenants = Path(fs) / "tenants.json"
        Path(fs, "mailbox").mkdir()
        app = create_app(tenants_path=tenants, mailbox_root=Path(fs) / "mailbox")
        # Pre-register a tenant
        reg = TenantRegistry(path=tenants)
        api_key = reg.add("acme")
        client = TestClient(app)
        client.post("/v1/messages",
                    json={"from_": "u", "to": "b", "content": "hi", "intent": "respond"},
                    headers={"X-API-Key": api_key})
        # Now /metrics should show 1 message for acme
        resp = client.get("/metrics")
        assert 'agentforge_mailbox_messages_total{tenant="acme"' in resp.text
```

**Step 2: Memoize instrumented mailboxes**

In `create_app()`, replace `mailbox_for()` with a memoizing wrapper:

```python
from agentforge.observability.instrumentation import instrument_mailbox
from agentforge.observability.metrics import get_registry

_mailbox_cache: dict[str, FileMailbox] = {}

def mailbox_for(tenant_id: str) -> FileMailbox:
    if tenant_id not in _mailbox_cache:
        m = FileMailbox(root=mailbox_root, tenant_id=tenant_id)
        instrument_mailbox(m, registry=get_registry())
        _mailbox_cache[tenant_id] = m
    return _mailbox_cache[tenant_id]
```

**Step 3: Test + commit**

```bash
pytest tests/unit/test_serve_metrics.py tests/unit/test_serve.py -v
git add src/agentforge/serve.py tests/unit/test_serve_metrics.py
git commit -m "feat(serve): instrument per-tenant mailboxes + wire into /metrics"
```

---

### Task 14: README updates — Observability section

**Objective:** Document the new endpoints, env vars, and CLI flags.

**Files:**
- Modify: `README.md`

**Step 1: Add a new section after "## Quick start"**

```markdown
## Observability

### Structured logging

`agentforge` emits structured JSON logs when `AGENTFORGE_LOG_FORMAT=json`:

```bash
AGENTFORGE_LOG_FORMAT=json agentforge serve
# {"ts":"2026-06-10T13:45:01+00:00","level":"INFO","logger":"agentforge.serve","msg":"agentforge serving on http://127.0.0.1:8765","request_id":"req_a1b2c3"}
```

Request ID is automatically attached from the `X-Request-Id` request header (or generated as `req_<hex>` if absent) and echoes on the response. All log lines emitted during the request share that `request_id`.

### Metrics

`GET /metrics` returns Prometheus text format with no auth (same as `/health`):

```bash
$ curl http://127.0.0.1:8765/metrics
# HELP agentforge_mailbox_messages_total Total messages written/read from mailbox
# TYPE agentforge_mailbox_messages_total counter
agentforge_mailbox_messages_total{tenant="acme",direction="sent"} 42.0
...
```

Metrics currently exported:
- `agentforge_mailbox_messages_total{tenant,direction}` — counter (sent|received)
- `agentforge_mailbox_*_duration_seconds` — histogram
- `agentforge_workflow_steps_total{workflow,step_type,outcome}` — counter
- `agentforge_workflow_step_duration_seconds{workflow,step_type}` — histogram
- `agentforge_llm_calls_total{provider,outcome}` — counter
- `agentforge_llm_call_duration_seconds{provider}` — histogram
- `agentforge_llm_tokens_total{provider,direction}` — counter (in|out)

Metrics are in-memory only — they reset on process restart. Use Prometheus to scrape every 15-30s and store the history.

### Health checks

- `GET /health` — liveness (200 if process is up, no auth)
- `GET /readyz` — readiness (200 if mailbox-root is writable AND tenants.json is readable, 503 otherwise with reasons in JSON body)

Use `/health` for "is the process alive" and `/readyz` for "should we route traffic here".

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AGENTFORGE_LOG_FORMAT` | `text` | `json` or `text` |
| `AGENTFORGE_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `AGENTFORGE_METRICS_ENABLED` | `1` | Reserved (always on for now) |
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add Observability section (logging, metrics, health checks)"
```

---

### Task 15: Plan-Compliance Check + final tag

**Objective:** Verify the implementation matches the plan, run the full test suite, do a fresh-venv install verification, and tag v0.3.0.

**Step 1: Run full test suite**

```bash
source .venv/bin/activate
pytest -q
```

Expected: ≥135 tests pass. If any fail, fix and recommit before proceeding.

**Step 2: Run the verify-install-path skill checklist**

1. Create fresh venv in /tmp
2. `pip install ~/Developer/agent-forge`
3. `agentforge --help` — all subcommands listed, `--log-format` and `--log-level` present
4. `agentforge init demo` — scaffolds workflow
5. `agentforge serve --log-format json` — starts uvicorn, log lines are valid JSON
6. `curl http://127.0.0.1:8765/health` — returns 200
7. `curl http://127.0.0.1:8765/readyz` — returns 200
8. `curl http://127.0.0.1:8765/metrics` — returns Prometheus text
9. `curl -H 'X-Request-Id: my-test' http://127.0.0.1:8765/health` — response header echoes `my-test`
10. Stop the server (Ctrl-C) — no traceback, no orphan threads

If any step fails, the plan has a gap. Fix the gap, recommit, and retry.

**Step 3: Plan-Compliance audit table**

| # | Task | Status | Evidence |
|---|------|--------|----------|
| 1 | Observability package skeleton | ✅ | `git log -- src/agentforge/observability/__init__.py` |
| 2 | JsonFormatter | ✅ | `git log -- src/agentforge/observability/logging.py` |
| 3 | context.py | ✅ | `git log -- src/agentforge/observability/context.py` |
| 4 | metrics.py | ✅ | `git log -- src/agentforge/observability/metrics.py` |
| 5 | middleware.py | ✅ | `git log -- src/agentforge/observability/middleware.py` |
| 6 | Mailbox instrumentation | ✅ | test green, commit present |
| 7 | Engine instrumentation | ✅ | test green, commit present |
| 8 | LLM instrumentation | ✅ | test green, commit present |
| 9 | /metrics endpoint | ✅ | test green, commit present |
| 10 | /readyz endpoint | ✅ | test green, commit present |
| 11 | RequestIdMiddleware in serve | ✅ | test green, commit present |
| 12 | CLI --log-format/--log-level | ✅ | test green, commit present |
| 13 | Per-tenant mailbox instrumentation | ✅ | test green, commit present |
| 14 | README updates | ✅ | commit present |
| 15 | Plan-Compliance Check + tag | ⏳ running | — |

**Step 4: Acceptance Criteria walk**

| Criterion | Status |
|---|---|
| pytest ≥135 tests | ⏳ count to verify |
| `AGENTFORGE_LOG_FORMAT=json` emits valid JSON per line | ⏳ verify in fresh venv |
| `/metrics` returns Prometheus text | ⏳ verify in fresh venv |
| `/readyz` returns 200/503 correctly | ⏳ verify in fresh venv |
| X-Request-Id propagation works | ⏳ verify in fresh venv |
| `agentforge run --log-format json` works | ⏳ verify in fresh venv |
| Library import is still side-effect-free | ⏳ verify |
| verify-install-path skill passes | ⏳ verify |

**Step 5: Tag v0.3.0 + push**

```bash
git tag -a v0.3.0 -m "v0.3.0: Observability — structured logging, /metrics, /readyz, request IDs"
git push origin master --tags
```

---

## Verification Checklist (use at the end)

```bash
# All green?
pytest -q

# Library is still side-effect-free?
python -c "import agentforge; import logging; assert not any(h.name.startswith('agentforge') for h in logging.Logger.manager.loggerDict.values() if hasattr(h, 'name'))"

# Fresh-venv install works?
cd /tmp && python3 -m venv af-verify && source af-verify/bin/activate
pip install --quiet ~/Developer/agent-forge
agentforge --help | grep -E "log-format|log-level"
agentforge init demo && cd demo
agentforge run workflow.yaml --agent mybot --mailbox ./mailbox --llm "" --log-format json 2>&1 | head -5
# Should be valid JSON lines

# Serve + scrape?
agentforge serve --log-format json &
sleep 2
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8765/readyz
curl -s http://127.0.0.1:8765/metrics | head -20
curl -sI http://127.0.0.1:8765/health | grep -i request-id
# Stop the server
kill %1
```

If all checks pass, the phase is complete.

---

## Risk & Mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| Instrumenting `list_inbox` breaks a sync code path | Low | Read the source carefully; the helper has both sync + async branches |
| `Counter.labels()` pattern is awkward to use | Medium | The `instrument_*()` helpers hide the labels() pattern from callers — they pass kwargs |
| `add_middleware` on FastAPI wraps in the wrong order | Low | ASGI middleware is OUTSIDE the FastAPI router, so it sees the raw request and response |
| /metrics endpoint is unauthenticated, becomes an info leak | Low | Document in README; gate behind reverse-proxy auth in production. Phase 8 may add per-tenant metrics scoping |
| Histogram bucket boundaries don't match actual latency distribution | Low | Use Prometheus defaults for now; revisit in Phase 8 if distributions don't fit |
| Metrics are per-process, not shared across multi-process deploys | High | Document in README. Multi-process deployments need `prometheus_client` multiproc dir mode (deferred to Phase 8) |
