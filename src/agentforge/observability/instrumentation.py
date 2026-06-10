"""Wrap existing classes with metric instrumentation.

Each `instrument_*(obj, registry)` call attaches metrics to the
class. Idempotent (re-calling is a no-op — checked via a sentinel attr).

The wrappers preserve the original function's signature and return value.
The metric increments/observations happen in a `try/finally` so a raised
exception still records the duration and increments the error counter
(when applicable).
"""

from __future__ import annotations

import time
from typing import Any

from agentforge.observability.metrics import MetricsRegistry


_SENTINEL = "_agentforge_instrumented"


def instrument_mailbox(mbox: Any, registry: MetricsRegistry) -> None:
    """Attach mailbox metrics: messages_total{sent|received} + send/list duration.

    Wraps the existing FileMailbox.send and .list_inbox. The original
    functions are sync (return values, not awaitables), so the wrappers
    stay sync too.
    """
    if getattr(mbox, _SENTINEL, False):
        return

    tenant = mbox.tenant_id or "default"
    msgs_counter = registry.counter(
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

    _orig_send = mbox.send
    _orig_list = mbox.list_inbox

    def _instrumented_send(msg, *args, **kwargs):
        t0 = time.monotonic()
        try:
            return _orig_send(msg, *args, **kwargs)
        finally:
            send_duration.labels(tenant=tenant).observe(time.monotonic() - t0)
            msgs_counter.labels(tenant=tenant, direction="sent").inc()

    def _instrumented_list(*args, **kwargs):
        t0 = time.monotonic()
        result = _orig_list(*args, **kwargs)
        list_duration.labels(tenant=tenant).observe(time.monotonic() - t0)
        msgs_counter.labels(tenant=tenant, direction="received").inc(len(result))
        return result

    mbox.send = _instrumented_send
    mbox.list_inbox = _instrumented_list
    setattr(mbox, _SENTINEL, True)


def instrument_workflow(wf: Any, registry: MetricsRegistry) -> None:
    """Attach workflow metrics: runs_total{outcome} + run_duration_seconds.

    Tracks at the workflow level (one counter/histogram observation per
    run) rather than per-step. Per-step instrumentation would require
    monkey-patching the step-handler dict or refactoring the retry loop
    to emit events — out of scope for Phase 7. Workflow-level is 90% of
    the value at 10% of the complexity.
    """
    if getattr(wf, _SENTINEL, False):
        return

    runs_counter = registry.counter(
        "agentforge_workflow_runs_total",
        "Total workflow.run() invocations",
        label_names=("workflow", "outcome"),  # outcome: success|error
    )
    run_duration = registry.histogram(
        "agentforge_workflow_run_duration_seconds",
        "Workflow.run() wall time in seconds",
        label_names=("workflow",),
    )

    wf_name = wf.name
    _orig_run = wf.run

    async def _instrumented_run(*args, **kwargs):
        t0 = time.monotonic()
        outcome = "success"
        try:
            return await _orig_run(*args, **kwargs)
        except Exception:
            outcome = "error"
            raise
        finally:
            run_duration.labels(workflow=wf_name).observe(time.monotonic() - t0)
            runs_counter.labels(workflow=wf_name, outcome=outcome).inc()

    wf.run = _instrumented_run
    setattr(wf, _SENTINEL, True)
