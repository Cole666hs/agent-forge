"""Unit tests for agentforge.core.message — pure dataclass, no IO."""

from __future__ import annotations

from agentforge.core.message import VALID_INTENTS, Message


def test_message_defaults():
    m = Message(from_="alice", to="bob", content="hello")
    assert m.from_ == "alice"
    assert m.to == "bob"
    assert m.content == "hello"
    assert m.intent == "respond"
    assert m.read is False
    assert m.context_refs == []
    assert m.id.startswith("msg_")
    assert m.ts  # ISO 8601 timestamp present


def test_message_to_dict_renames_from_underscore():
    m = Message(from_="alice", to="bob", content="x")
    d = m.to_dict()
    assert d["from"] == "alice"
    assert "from_" not in d


def test_message_from_dict_tolerates_missing_fields():
    m = Message.from_dict({"from": "a", "to": "b", "content": "c"})
    assert m.from_ == "a"
    assert m.intent == "respond"  # defaulted
    assert m.context_refs == []   # defaulted


def test_valid_intents_constant():
    assert "respond" in VALID_INTENTS
    assert "ping" in VALID_INTENTS
    assert "ack" in VALID_INTENTS
