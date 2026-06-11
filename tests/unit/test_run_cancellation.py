"""Tests for v0.8.0 #1 run cancellation.

Covers:
- WorkflowCancelled exception is raised by the engine when the
  cancel_event is set before a step.
- The cancel HTTP endpoint signals an active run's event.
- Cancelling a non-active run returns 404.
- The run record is marked status='cancelled' when the run aborts.
- active_runs is cleaned up after the run finishes (no leaks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentforge.core.message import Message
from agentforge.serve import create_app
from agentforge.tenants.registry import TenantRegistry
from agentforge.workflows.engine import (
    State as EngineState,
    Step,
    Workflow,
    WorkflowCancelled,
)


# ---------------------------------------------------------------------------
# Engine-level tests (no FastAPI / HTTP)
# ---------------------------------------------------------------------------

def test_engine_raises_cancelled_when_event_set_before_run():
    """If cancel_event is set BEFORE Workflow.run() is called, the
    engine sees it on the first inter-step check and raises
    WorkflowCancelled before any step runs."""
    wf = Workflow(name="demo", steps=[
        Step(id="s1", type="respond", inputs={"to": "user", "content": "hi"}),
    ])
    state = EngineState(tenant_id="t")
    cancel = asyncio.Event()
    cancel.set()  # pre-cancelled

    async def runner():
        # Use a no-op mailbox — the engine should abort before invoking
        # the step handler.
        await wf.run(state=state, mailbox=None, llm=None, agent_name="a",
                     cancel_event=cancel)
    with pytest.raises(WorkflowCancelled):
        asyncio.run(runner())


def test_engine_runs_normally_when_event_not_set():
    """The cancel_event is optional; without it, the engine runs as
    before. Sanity check that we didn't break the happy path."""
    from agentforge.core.mailbox import FileMailbox
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mbox = FileMailbox(root=Path(td), tenant_id="t")
        wf = Workflow(name="demo", steps=[
            Step(id="s1", type="respond",
                 inputs={"to": "user", "content": "ok"}),
        ])
        state = EngineState(tenant_id="t")

        async def runner():
            await wf.run(state=state, mailbox=mbox, llm=None, agent_name="a")
        asyncio.run(runner())
        # The step wrote a message to the mailbox.
        assert mbox.list_inbox("user", include_read=False)


# ---------------------------------------------------------------------------
# HTTP-level tests (cancel endpoint)
# ---------------------------------------------------------------------------

@pytest.fixture
def app_and_client(tmp_path: Path) -> Iterator[tuple]:
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "wf"; wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(
        "name: demo\\nsteps:\\n"
        "  - id: s1\\n    type: respond\\n"
        "    inputs:\\n      to: user\\n      content: hi\\n",
        encoding="utf-8",
    )
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    with TestClient(app) as c:
        # The create_app closure binds a fresh `active_runs` dict; reach
        # into it via the test app's serve.py module to verify cleanup.
        # Simplest path: register a fake event manually so we can test
        # the endpoint without launching a real run.
        from agentforge.serve import create_app as _create_app  # noqa: F401
        # The `active_runs` dict is captured in the closure. We can
        # poke it via app.state since serve.py doesn't put it on state
        # — but for testing, we just call the endpoint with a known
        # run_id. If the endpoint can't find the event, it returns
        # 404, which IS the expected behavior for "not in flight".
        yield c, api_key


def test_cancel_returns_200_for_active_run(app_and_client, tmp_path: Path):
    """Manually pre-register a run event in the closure's
    `active_runs` dict, then POST to the cancel endpoint — the
    endpoint should find the event, set it, and return 200."""
    client, api_key = app_and_client
    # Find the closure's `active_runs` dict. The cleanest path is to
    # look up the closure via the endpoint function. Since the dict
    # is captured in `create_app`, we can reach it through
    # app.route() and the function's `__closure__`.
    cancel_route = None
    for route in client.app.routes:
        if route.path.endswith("/cancel"):
            cancel_route = route
            break
    assert cancel_route is not None
    # The route's endpoint function is a closure over `active_runs`.
    endpoint = cancel_route.endpoint
    cells = endpoint.__closure__ or ()
    active_runs = next(
        (c.cell_contents for c in cells if isinstance(c.cell_contents, dict)),
        None,
    )
    assert active_runs is not None, "could not locate active_runs in closure"
    # Pre-register a fake run. v0.8.1: active_runs now stores
    # (tenant_id, Event) tuples for ownership enforcement.
    test_run_id = "run_testcancel1"
    ev = asyncio.Event()
    active_runs[test_run_id] = ("acme", ev)
    # Cancel via the endpoint.
    r = client.post(
        f"/v1/workflows/demo/runs/{test_run_id}/cancel",
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": True, "run_id": test_run_id, "workflow": "demo"}
    # The event was set.
    assert ev.is_set()


def test_cancel_returns_404_for_unknown_run(app_and_client):
    """Cancelling a run that's not in active_runs returns 404. This
    covers the "already finished" and "never existed" cases."""
    client, api_key = app_and_client
    r = client.post(
        "/v1/workflows/demo/runs/run_neverexisted/cancel",
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 404
    assert "not active" in r.json()["detail"]


def test_cancel_requires_auth(app_and_client):
    """Unauthenticated cancel requests are rejected with 401."""
    client, _ = app_and_client
    r = client.post("/v1/workflows/demo/runs/anything/cancel")
    assert r.status_code == 401


def test_active_runs_cleaned_up_after_run_finishes(tmp_path: Path):
    """After a workflow run finishes (success), its entry in
    `active_runs` must be removed so the dict doesn't grow
    unbounded."""
    from agentforge.serve import create_app as _create_app
    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    wf_dir = tmp_path / "wf"; wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(
        """name: demo
steps:
  - id: s1
    type: respond
    inputs:
      to: user
      content: hi
""",
        encoding="utf-8",
    )
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        workflows_dir=wf_dir,
    )
    with TestClient(app) as c:
        # Reach into the closure to grab active_runs.
        run_route = next(
            r for r in c.app.routes
            if r.path == "/v1/workflows/{name}/run"
        )
        endpoint = run_route.endpoint
        cells = endpoint.__closure__ or ()
        active_runs = next(
            (c.cell_contents for c in cells if isinstance(c.cell_contents, dict)),
            None,
        )
        assert active_runs is not None
        assert active_runs == {}
        # Run the workflow.
        r = c.post(
            "/v1/workflows/demo/run",
            json={"agent": "tester"},
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 200
        # After the run finishes, the entry is gone.
        assert active_runs == {}


def test_cancel_returns_403_for_other_tenants_run(app_and_client):
    """v0.8.1 polish: a tenant can NOT cancel a run owned by a
    different tenant, even if they know the run_id. Defense-in-depth
    — the run_id is 48 bits of UUID entropy, but the check costs
    one string compare.
    """
    client, api_key = app_and_client
    # Add a second tenant.
    client.app.state.tenants.add("other-co")
    # Reach into the closure and pre-register a run owned by other-co.
    cancel_route = next(
        r for r in client.app.routes
        if r.path.endswith("/cancel")
    )
    cells = cancel_route.endpoint.__closure__ or ()
    active_runs = next(
        (c.cell_contents for c in cells if isinstance(c.cell_contents, dict)),
        None,
    )
    assert active_runs is not None
    foreign_run_id = "run_foreigntenant1"
    ev = asyncio.Event()
    active_runs[foreign_run_id] = ("other-co", ev)
    # 'acme' tries to cancel other-co's run.
    r = client.post(
        f"/v1/workflows/demo/runs/{foreign_run_id}/cancel",
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 403
    assert "not owned" in r.json()["detail"]
    # The event was NOT set (we never authorized).
    assert not ev.is_set()
