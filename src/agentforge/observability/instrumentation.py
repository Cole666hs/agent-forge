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


def instrument_llm(provider: Any, registry: Any,
                  tenants: Any = None,
                  usage: Any = None,
                  tenant_id: str = "") -> None:
    """Attach LLM metrics + optional quota enforcement.

    If `tenants` and `usage` are provided (and `tenant_id` is set), every
    call goes through enforce_quota(): over-limit calls raise
    QuotaExceededError BEFORE the LLM is invoked. Tokens are recorded
    on success only — failed calls don't consume quota (matches industry
    practice: don't penalize for transient errors).

    For multi-tenant serve (shared LLM provider), pass tenants=None and
    rely on per-step enforcement inside the workflow engine instead.
    """
    if getattr(provider, _SENTINEL, False):
        return

    calls_counter = registry.counter(
        "agentforge_llm_calls_total",
        "Total LLM API calls",
        label_names=("provider", "outcome"),
    )
    call_duration = registry.histogram(
        "agentforge_llm_call_duration_seconds",
        "LLM call latency in seconds",
        label_names=("provider",),
    )
    tokens_counter = registry.counter(
        "agentforge_llm_tokens_total",
        "Total LLM tokens consumed",
        label_names=("provider", "direction"),
    )

    # Lazy imports to avoid circular dependency with billing module
    from agentforge.billing.quota import enforce_quota, QuotaExceededError

    billing_enabled = tenants is not None and usage is not None and bool(tenant_id)
    provider_name = type(provider).__name__
    _orig = provider._do_chat

    def _wrap(system, user, *args, **kwargs):
        t0 = time.monotonic()
        outcome = "success"
        result = None
        try:
            # Quota pre-flight (only if wired). If at/over limit, block
            # before paying for the LLM call.
            if billing_enabled:
                enforce_quota(tenants, usage, tenant_id, tokens_to_add=0)
            result = _orig(system, user, *args, **kwargs)
            # Post-call: record actual tokens consumed
            if result is not None and billing_enabled:
                t_in = getattr(result, "tokens_in", None) or 0
                t_out = getattr(result, "tokens_out", None) or 0
                total = t_in + t_out
                if total > 0:
                    enforce_quota(tenants, usage, tenant_id, tokens_to_add=total)
            return result
        except QuotaExceededError:
            outcome = "quota_exceeded"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            call_duration.labels(provider=provider_name).observe(time.monotonic() - t0)
            calls_counter.labels(provider=provider_name, outcome=outcome).inc()
            if outcome == "success" and result is not None:
                if getattr(result, "tokens_in", None) is not None:
                    tokens_counter.labels(
                        provider=provider_name, direction="in"
                    ).inc(result.tokens_in)
                if getattr(result, "tokens_out", None) is not None:
                    tokens_counter.labels(
                        provider=provider_name, direction="out"
                    ).inc(result.tokens_out)

    provider._do_chat = _wrap
    setattr(provider, _SENTINEL, True)
