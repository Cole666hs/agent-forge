"""T6 RED — mailbox instrumentation test."""

from agentforge.core.mailbox import FileMailbox
from agentforge.core.message import Message
from agentforge.observability.metrics import MetricsRegistry
from agentforge.observability.instrumentation import instrument_mailbox


def test_instrument_mailbox_increments_send_counter(tmp_path):
    reg = MetricsRegistry()
    mbox = FileMailbox(root=tmp_path / "mb", tenant_id="acme")
    instrument_mailbox(mbox, registry=reg)
    mbox.send(Message(from_="u", to="b", content="hi"))
    mbox.send(Message(from_="u2", to="b2", content="hi2"))
    out = reg.render()
    assert "agentforge_mailbox_messages_total" in out
    assert 'agentforge_mailbox_messages_total{tenant="acme",direction="sent"} 2.0' in out


def test_instrument_mailbox_increments_list_counter(tmp_path):
    reg = MetricsRegistry()
    mbox = FileMailbox(root=tmp_path / "mb", tenant_id="acme")
    instrument_mailbox(mbox, registry=reg)
    mbox.send(Message(from_="u", to="b", content="hi"))
    mbox.list_inbox("b")
    mbox.list_inbox("b")
    out = reg.render()
    # 2 list_inbox calls, each returning 1 message
    assert 'agentforge_mailbox_messages_total{tenant="acme",direction="received"} 2.0' in out


def test_instrument_mailbox_records_send_duration(tmp_path):
    reg = MetricsRegistry()
    mbox = FileMailbox(root=tmp_path / "mb", tenant_id="acme")
    instrument_mailbox(mbox, registry=reg)
    mbox.send(Message(from_="u", to="b", content="hi"))
    out = reg.render()
    assert "agentforge_mailbox_send_duration_seconds" in out
    assert "agentforge_mailbox_send_duration_seconds_count" in out


def test_instrument_mailbox_idempotent(tmp_path):
    reg = MetricsRegistry()
    mbox = FileMailbox(root=tmp_path / "mb", tenant_id="acme")
    instrument_mailbox(mbox, registry=reg)
    instrument_mailbox(mbox, registry=reg)  # second call should be a no-op
    mbox.send(Message(from_="u", to="b", content="hi"))
    # Counter should be 1, not 2
    out = reg.render()
    assert 'agentforge_mailbox_messages_total{tenant="acme",direction="sent"} 1.0' in out


def test_instrument_mailbox_single_tenant_no_id(tmp_path):
    """Tenant_id='' (default) gets a placeholder label, not a metric per tenant."""
    reg = MetricsRegistry()
    mbox = FileMailbox(root=tmp_path / "mb")  # no tenant_id
    instrument_mailbox(mbox, registry=reg)
    mbox.send(Message(from_="u", to="b", content="hi"))
    out = reg.render()
    assert 'tenant="default"' in out  # not tenant="" (empty)
