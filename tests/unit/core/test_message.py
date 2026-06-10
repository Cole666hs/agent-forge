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


def test_is_expired_false_when_no_expiry():
    m = Message(from_="a", to="b", content="x")
    assert m.is_expired() is False


def test_is_expired_true_when_in_past():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    m = Message(from_="a", to="b", content="x", expires_at=past)
    assert m.is_expired() is True


def test_is_expired_false_when_in_future():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    m = Message(from_="a", to="b", content="x", expires_at=future)
    assert m.is_expired() is False


def test_is_expired_tolerates_malformed_timestamp():
    m = Message(from_="a", to="b", content="x", expires_at="not-a-date")
    assert m.is_expired() is False  # malformed → treat as not expired
