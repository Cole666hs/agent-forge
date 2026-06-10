"""T7 RED — workflow engine instrumentation test."""

import asyncio

from agentforge.workflows.engine import State, Workflow
from agentforge.observability.metrics import MetricsRegistry
from agentforge.observability.instrumentation import instrument_workflow


def _make_wf(name, steps_dicts):
    """Build a Workflow from a list of step dicts (uses from_yaml_text)."""
    import yaml
    yaml_text = yaml.safe_dump({"name": name, "steps": steps_dicts})
    return Workflow.from_yaml_text(yaml_text)


def test_instrument_workflow_increments_run_counter(tmp_path):
    """Each successful workflow.run() increments the steps_total counter."""
    reg = MetricsRegistry()
    wf = _make_wf("test_wf", [
        {"id": "noop", "type": "respond",
         "inputs": {"to": "u", "content": "ok"}},
    ])
    instrument_workflow(wf, registry=reg)

    # Provide a real mailbox for the respond step
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mailbox = FileMailbox(root=tmp_path / "mb", tenant_id="t1")
    mailbox.send(Message(from_="u", to="b", content="hi"))  # seed inbox

    asyncio.run(wf.run(state=State(), mailbox=mailbox, llm=None, agent_name="b"))

    out = reg.render()
    assert "agentforge_workflow_runs_total" in out
    assert 'agentforge_workflow_runs_total{workflow="test_wf",outcome="success"} 1.0' in out


def test_instrument_workflow_records_run_duration(tmp_path):
    reg = MetricsRegistry()
    wf = _make_wf("d", [
        {"id": "r", "type": "respond", "inputs": {"to": "u", "content": "ok"}},
    ])
    instrument_workflow(wf, registry=reg)
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mailbox = FileMailbox(root=tmp_path / "mb")
    mailbox.send(Message(from_="u", to="b", content="hi"))
    asyncio.run(wf.run(state=State(), mailbox=mailbox, llm=None, agent_name="b"))
    out = reg.render()
    assert "agentforge_workflow_run_duration_seconds" in out
    assert "agentforge_workflow_run_duration_seconds_count" in out


def test_instrument_workflow_idempotent(tmp_path):
    reg = MetricsRegistry()
    wf = _make_wf("i", [
        {"id": "r", "type": "respond", "inputs": {"to": "u", "content": "ok"}},
    ])
    instrument_workflow(wf, registry=reg)
    instrument_workflow(wf, registry=reg)  # no-op
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mailbox = FileMailbox(root=tmp_path / "mb")
    mailbox.send(Message(from_="u", to="b", content="hi"))
    asyncio.run(wf.run(state=State(), mailbox=mailbox, llm=None, agent_name="b"))
    out = reg.render()
    assert 'agentforge_workflow_runs_total{workflow="i",outcome="success"} 1.0' in out
