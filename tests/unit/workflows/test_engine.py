"""Unit tests for agentforge.workflows.engine — Workflow + State.

Tests the load → run → persist flow with a mock LLM and a real
FileMailbox (tmp_path). No real network calls.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

import pytest
import yaml

from agentforge.core.mailbox import FileMailbox
from agentforge.core.message import Message
from agentforge.workflows.engine import (
    State,
    Step,
    Workflow,
    WorkflowError,
)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def test_workflow_loads_minimal_yaml():
    yaml_text = textwrap.dedent("""
        name: echo
        description: "Echo bot"
        steps:
          - id: receive
            type: receive
          - id: respond
            type: respond
            inputs:
              content: "hello"
    """)
    wf = Workflow.from_yaml_text(yaml_text)
    assert wf.name == "echo"
    assert wf.description == "Echo bot"
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "receive"
    assert wf.steps[0].type == "receive"
    assert wf.steps[1].inputs["content"] == "hello"


def test_workflow_unknown_step_type_raises():
    yaml_text = textwrap.dedent("""
        name: bad
        steps:
          - id: x
            type: not_a_real_step_type
    """)
    with pytest.raises(WorkflowError, match="unknown step type"):
        Workflow.from_yaml_text(yaml_text)


def test_workflow_step_ids_must_be_unique():
    yaml_text = textwrap.dedent("""
        name: dup
        steps:
          - id: x
            type: receive
          - id: x
            type: respond
    """)
    with pytest.raises(WorkflowError, match="duplicate step id"):
        Workflow.from_yaml_text(yaml_text)


# ---------------------------------------------------------------------------
# State: pure in-memory + SQLite persistence
# ---------------------------------------------------------------------------

def test_state_set_get():
    s = State()
    s.set("a.b.c", 42)
    assert s.get("a.b.c") == 42
    assert s.get("a.b") == {"c": 42}


def test_state_render_template_substitutes_state():
    s = State()
    s.set("user.name", "alice")
    assert s.render("Hello {{ user.name }}!") == "Hello alice!"


def test_state_render_missing_key_returns_empty():
    s = State()
    # Missing keys render as empty string (don't crash)
    assert s.render("{{ missing }}") == ""


def test_state_persists_to_sqlite(tmp_path: Path):
    db = tmp_path / "state.db"
    s1 = State(run_id="run-123")
    s1.set("step1", "value1")
    s1.set("step2.nested", "value2")
    s1.persist(db)

    # Simulate "crash" — fresh State, same run_id
    s2 = State(run_id="run-123")
    s2.hydrate(db)
    assert s2.get("step1") == "value1"
    assert s2.get("step2.nested") == "value2"


# ---------------------------------------------------------------------------
# Workflow.run() — end-to-end with mocked LLM and real FileMailbox
# ---------------------------------------------------------------------------

@pytest.fixture
def mbox(tmp_path: Path) -> FileMailbox:
    return FileMailbox(root=tmp_path / "mailbox")


async def test_workflow_run_receive_respond(mbox: FileMailbox):
    """Minimal receive → respond flow."""
    # Seed the inbox with a message
    mbox.send(Message(from_="user", to="bot", content="hi there"))

    yaml_text = textwrap.dedent("""
        name: echo
        steps:
          - id: receive
            type: receive
          - id: respond
            type: respond
            inputs:
              to: "{{ receive.from }}"
              content: "echo: {{ receive.content }}"
    """)
    wf = Workflow.from_yaml_text(yaml_text)
    final_state = await wf.run(state=State(), mailbox=mbox, llm=None, agent_name="bot")

    # The state should have a "receive" entry with the message
    assert final_state.get("receive.content") == "hi there"
    # The response should be in the user's inbox
    user_inbox = mbox.list_inbox("user", include_read=True)
    assert len(user_inbox) == 1
    assert user_inbox[0].content == "echo: hi there"
    assert user_inbox[0].from_ == "bot"


async def test_workflow_run_llm_call_uses_provider(mbox: FileMailbox):
    """llm_call step invokes the provider with rendered templates."""
    mbox.send(Message(from_="user", to="bot", content="what is 2+2?"))

    yaml_text = textwrap.dedent("""
        name: think-respond
        steps:
          - id: receive
            type: receive
          - id: think
            type: llm_call
            inputs:
              system: "you are a math helper"
              user: "{{ receive.content }}"
              output_key: "think"
          - id: respond
            type: respond
            inputs:
              to: "{{ receive.from }}"
              content: "{{ think }}"
    """)
    wf = Workflow.from_yaml_text(yaml_text)

    # Mock LLM provider
    mock_llm = mock.Mock()
    mock_llm.chat = mock.AsyncMock(return_value="The answer is 4")

    await wf.run(state=State(), mailbox=mbox, llm=mock_llm, agent_name="bot")

    # LLM was called with the rendered prompts
    mock_llm.chat.assert_awaited_once()
    sys_arg, user_arg = mock_llm.chat.await_args.args
    assert sys_arg == "you are a math helper"
    assert user_arg == "what is 2+2?"

    # Response in inbox
    user_inbox = mbox.list_inbox("user", include_read=True)
    assert user_inbox[0].content == "The answer is 4"


async def test_workflow_run_state_persists(mbox: FileMailbox, tmp_path: Path):
    """A workflow with a state.db persists intermediate state."""
    mbox.send(Message(from_="user", to="bot", content="persist me"))
    db = tmp_path / "state.db"

    yaml_text = textwrap.dedent("""
        name: persist
        steps:
          - id: receive
            type: receive
          - id: respond
            type: respond
            inputs:
              to: "{{ receive.from }}"
              content: "ok: {{ receive.content }}"
    """)
    wf = Workflow.from_yaml_text(yaml_text)
    s = State(run_id="run-abc")
    await wf.run(state=s, mailbox=mbox, llm=None, agent_name="bot", state_db=db)

    # Reload state from DB and check it survived
    s2 = State(run_id="run-abc")
    s2.hydrate(db)
    assert s2.get("receive.content") == "persist me"
