"""T4 RED — metrics primitives test."""

import pytest

from agentforge.observability.metrics import (
    Counter,
    Histogram,
    MetricsRegistry,
)


def test_counter_increments_default_label():
    c = Counter(name="test_total", help="test counter", registry=MetricsRegistry())
    c.inc()
    c.inc(3)
    assert c.value() == 4.0


def test_counter_rejects_negative():
    c = Counter(name="t", help="t", registry=MetricsRegistry())
    with pytest.raises(ValueError, match="counter can only increase"):
        c.inc(-1)


def test_counter_with_labels():
    reg = MetricsRegistry()
    c = Counter(name="http_total", help="http", registry=reg, label_names=("method",))
    c.labels(method="GET").inc()
    c.labels(method="POST").inc(2)
    out = reg.render()
    assert 'http_total{method="GET"} 1.0' in out
    assert 'http_total{method="POST"} 2.0' in out


def test_counter_label_mismatch_raises():
    reg = MetricsRegistry()
    c = Counter(name="c", help="c", registry=reg, label_names=("a",))
    with pytest.raises(ValueError, match="expected labels"):
        c.labels(b="x")  # wrong key


def test_histogram_buckets_and_sum_count():
    reg = MetricsRegistry()
    h = Histogram(name="dur", help="dur", registry=reg, buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)
    h.observe(0.3)
    h.observe(0.7)
    out = reg.render()
    assert 'dur_bucket{le="0.1"} 1.0' in out
    assert 'dur_bucket{le="0.5"} 2.0' in out
    assert 'dur_bucket{le="1.0"} 3.0' in out
    assert 'dur_bucket{le="+Inf"} 3.0' in out
    assert "dur_sum 1.05" in out
    assert "dur_count 3" in out


def test_histogram_labeled():
    reg = MetricsRegistry()
    h = Histogram(name="rt", help="rt", registry=reg, label_names=("path",), buckets=(1.0,))
    h.labels(path="/a").observe(0.5)
    h.labels(path="/b").observe(2.0)
    out = reg.render()
    # Label order: user labels first, then synthetic (le)
    assert 'rt_bucket{path="/a",le="1.0"} 1.0' in out
    assert 'rt_bucket{path="/a",le="+Inf"} 1.0' in out
    assert 'rt_bucket{path="/b",le="+Inf"} 1.0' in out
    assert 'rt_bucket{path="/b",le="1.0"} 0.0' in out


def test_render_includes_help_and_type():
    reg = MetricsRegistry()
    reg.counter("c", "help text").inc()
    h = reg.histogram("h", "hist help", buckets=(1.0,))
    h.observe(0.5)
    out = reg.render()
    assert "# HELP c help text" in out
    assert "# TYPE c counter" in out
    assert "# HELP h hist help" in out
    assert "# TYPE h histogram" in out


def test_registry_factory_methods():
    reg = MetricsRegistry()
    c = reg.counter("c1", "h1")
    h = reg.histogram("h1", "h2")
    assert isinstance(c, Counter)
    assert isinstance(h, Histogram)
    # Second call returns a new metric with same name (no dedup — the
    # contract is "one metric per name, owner is responsible")
    c2 = reg.counter("c1", "h1-different-help")
    assert c is not c2
    out = reg.render()
    # Both counters present
    assert out.count("# TYPE c1 counter") == 2


def test_get_registry_returns_singleton():
    from agentforge.observability.metrics import get_registry
    a = get_registry()
    b = get_registry()
    assert a is b


def test_metric_requires_name():
    reg = MetricsRegistry()
    with pytest.raises(ValueError, match="name"):
        Counter(name="", help="x", registry=reg)
