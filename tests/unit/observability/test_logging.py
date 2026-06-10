"""T2 RED — JsonFormatter test."""

import io
import json
import logging

from agentforge.observability.logging import JsonFormatter, configure_logging


def _capture(logger_name, level=logging.INFO, **kw):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger(logger_name)
    log.handlers = [handler]
    log.setLevel(level)
    log.propagate = False
    return log, buf


def test_json_formatter_emits_one_json_per_line():
    log, buf = _capture("test.json.basic")
    log.info("hello %s", "world", extra={"agent": "bot1"})

    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.json.basic"
    assert parsed["agent"] == "bot1"
    assert "ts" in parsed


def test_json_formatter_includes_request_id_from_context():
    from agentforge.observability.context import set_request_id, reset_request_id
    set_request_id("req_xyz")
    try:
        log, buf = _capture("test.json.rid")
        log.warning("in request")
        parsed = json.loads(buf.getvalue().strip())
        assert parsed["request_id"] == "req_xyz"
        assert parsed["msg"] == "in request"
    finally:
        reset_request_id()


def test_json_formatter_omits_request_id_when_not_in_context():
    log, buf = _capture("test.json.norid")
    log.error("no rid here")
    parsed = json.loads(buf.getvalue().strip())
    assert "request_id" not in parsed


def test_configure_logging_text_format():
    configure_logging(fmt="text", level="WARNING")
    assert logging.getLogger("agentforge").level == logging.WARNING
    # Reset for other tests
    logging.getLogger("agentforge").handlers = []


def test_configure_logging_env_var(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_LOG_FORMAT", "json")
    monkeypatch.setenv("AGENTFORGE_LOG_LEVEL", "DEBUG")
    configure_logging()  # fmt/level from env
    assert logging.getLogger("agentforge").level == logging.DEBUG
    # Cleanup
    logging.getLogger("agentforge").handlers = []


def test_configure_logging_unknown_format_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown log format"):
        configure_logging(fmt="xml")


def test_configure_logging_is_idempotent():
    """Calling configure_logging twice replaces the handler, doesn't stack them."""
    configure_logging(fmt="text", level="INFO")
    n1 = len(logging.getLogger("agentforge").handlers)
    configure_logging(fmt="text", level="INFO")
    n2 = len(logging.getLogger("agentforge").handlers)
    assert n1 == 1
    assert n2 == 1
    # cleanup
    logging.getLogger("agentforge").handlers = []
