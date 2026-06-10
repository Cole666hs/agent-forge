"""Tests for the OTLP/HTTP metrics exporter (v0.5.5)."""
import json
from unittest.mock import patch, MagicMock

import pytest

from agentforge.observability.metrics import MetricsRegistry
from agentforge.observability.otlp import OtlpExporter


# ---------------------------------------------------------------------------
# Configuration: disabled by default
# ---------------------------------------------------------------------------

def test_otlp_exporter_disabled_when_no_endpoint():
    reg = MetricsRegistry()
    exp = OtlpExporter(endpoint=None, registry=reg)
    # No error, no work
    exp.push_once()


def test_otlp_exporter_constructor_stores_config():
    reg = MetricsRegistry()
    exp = OtlpExporter(endpoint="http://collector:4318", registry=reg,
                       service_name="agentforge", service_version="0.5.5",
                       push_interval_seconds=10)
    assert exp.endpoint == "http://collector:4318"
    assert exp.service_name == "agentforge"


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------

def test_payload_contains_resource():
    reg = MetricsRegistry()
    reg.counter("test_total", "test")
    exp = OtlpExporter(endpoint="http://x", registry=reg,
                       service_name="agentforge", service_version="0.5.5")
    payload = exp._build_payload()
    rm = payload["resourceMetrics"][0]
    attrs = {a["key"]: list(a["value"].values())[0] for a in rm["resource"]["attributes"]}
    assert attrs.get("service.name") == "agentforge"
    assert attrs.get("service.version") == "0.5.5"


def test_payload_includes_counters():
    reg = MetricsRegistry()
    c = reg.counter("test_total", "test total", label_names=("kind",))
    c.labels(kind="a").inc(3)
    c.labels(kind="b").inc(7)
    exp = OtlpExporter(endpoint="http://x", registry=reg)
    payload = exp._build_payload()
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    counter_metric = next((m for m in metrics if m["name"] == "test_total"), None)
    assert counter_metric is not None
    assert counter_metric["unit"] == "1"
    assert "sum" in counter_metric
    pts = counter_metric["sum"]["dataPoints"]
    # Two data points (one per label combination). Map kind → asInt
    by_kind: dict[str, str] = {}
    for pt in pts:
        # Find the 'kind' attribute on this point
        kind_attr = next((a for a in pt["attributes"] if a["key"] == "kind"), None)
        if kind_attr is not None:
            by_kind[kind_attr["value"]["stringValue"]] = pt["asInt"]
    assert by_kind == {"a": "3", "b": "7"}


def test_payload_includes_histograms():
    reg = MetricsRegistry()
    h = reg.histogram("test_seconds", "test seconds", label_names=("op",),
                      buckets=(0.1, 1.0))
    h.labels(op="x").observe(0.05)
    h.labels(op="x").observe(0.5)
    h.labels(op="x").observe(5.0)
    exp = OtlpExporter(endpoint="http://x", registry=reg)
    payload = exp._build_payload()
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    hist_metric = next((m for m in metrics if m["name"] == "test_seconds"), None)
    assert hist_metric is not None
    assert hist_metric["unit"] == "s"
    assert "histogram" in hist_metric
    pt = hist_metric["histogram"]["dataPoints"][0]
    assert pt["count"] == "3"
    # Sum: 0.05 + 0.5 + 5.0 = 5.55 (or close)
    assert abs(float(pt["sum"]) - 5.55) < 0.01
    # Buckets: 2 explicit bounds + +Inf included
    assert pt["explicitBounds"] == [0.1, 1.0]
    assert len(pt["bucketCounts"]) == len(pt["explicitBounds"]) + 1  # +Inf included


# ---------------------------------------------------------------------------
# HTTP push
# ---------------------------------------------------------------------------

def test_push_once_posts_to_endpoint():
    reg = MetricsRegistry()
    reg.counter("test_total", "test").inc(5)
    exp = OtlpExporter(endpoint="http://collector:4318", registry=reg)

    captured = {}
    def fake_post(url, json=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    with patch("agentforge.observability.otlp.requests.post", side_effect=fake_post):
        exp.push_once()

    assert captured["url"] == "http://collector:4318/v1/metrics"
    assert "resourceMetrics" in captured["json"]


def test_push_uses_correct_path():
    """OTLP metrics path is /v1/metrics (not /v1/traces)."""
    reg = MetricsRegistry()
    reg.counter("test_total", "test")
    exp = OtlpExporter(endpoint="http://collector:4318", registry=reg)
    captured = {}
    def fake_post(url, **kwargs):
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        return resp
    with patch("agentforge.observability.otlp.requests.post", side_effect=fake_post):
        exp.push_once()
    assert captured["url"].endswith("/v1/metrics")


def test_push_handles_network_errors_gracefully():
    """A failing collector must not crash the agent process."""
    import requests
    reg = MetricsRegistry()
    reg.counter("test_total", "test")
    exp = OtlpExporter(endpoint="http://down:4318", registry=reg)
    with patch("agentforge.observability.otlp.requests.post",
               side_effect=requests.ConnectionError("nope")):
        # Should not raise
        exp.push_once()


def test_push_handles_non_2xx_response():
    reg = MetricsRegistry()
    reg.counter("test_total", "test")
    exp = OtlpExporter(endpoint="http://x", registry=reg)
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status = MagicMock(side_effect=Exception("500"))
    with patch("agentforge.observability.otlp.requests.post", return_value=resp):
        # Should not raise
        exp.push_once()


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def test_start_spawns_thread_stop_joins():
    reg = MetricsRegistry()
    reg.counter("test_total", "test")
    exp = OtlpExporter(endpoint="http://x", registry=reg, push_interval_seconds=3600)
    exp.start()
    assert exp._thread is not None
    assert exp._thread.is_alive()
    exp.stop()
    assert not exp._thread.is_alive()


def test_start_is_idempotent():
    reg = MetricsRegistry()
    exp = OtlpExporter(endpoint="http://x", registry=reg, push_interval_seconds=3600)
    exp.start()
    t1 = exp._thread
    exp.start()  # no-op
    assert exp._thread is t1
    exp.stop()


# ---------------------------------------------------------------------------
# End-to-end: import + use without breaking the no-SDK-dep invariant
# ---------------------------------------------------------------------------

def test_otlp_module_imports_without_new_deps():
    """otlp.py only imports from stdlib + requests (already a dep)."""
    import agentforge.observability.otlp as otlp
    # Should not have any opentelemetry imports
    src = open(otlp.__file__).read()
    assert "import opentelemetry" not in src
    assert "from opentelemetry" not in src
