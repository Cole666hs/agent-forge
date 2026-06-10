"""Hand-rolled Prometheus-compatible metrics.

Why not prometheus_client? Zero new deps. Three classes (Counter, Histogram,
MetricsRegistry) cover everything we need for Phase 7. If we ever need
quantiles, exemplars, or push-gateway, swap to the real library then.

Thread-safe via a single Lock around the dict-of-labels state. Not lock-free,
but our hot path is short (inc/observe + dict lookup), so contention is
unmeasurable in practice.
"""

from __future__ import annotations

import threading
from typing import Iterable, Sequence


# Default Prometheus histogram buckets (in seconds)
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class _Metric:
    """Base class. Holds registry reference + name/help."""

    def __init__(self, name: str, help: str, registry: "MetricsRegistry"):
        if not name:
            raise ValueError("metric name must be non-empty")
        self.name = name
        self.help = help
        self._registry = registry
        registry._register(self)


class Counter(_Metric):
    """Monotonic counter. inc() adds 1, inc(n) adds n.

    With no labels: call .inc() directly.
    With labels: use .labels(**values).inc(). The pattern keeps the
    hot-path fast (no kwargs dict construction) and matches the
    prometheus_client API.
    """

    def __init__(
        self,
        name: str,
        help: str,
        registry: "MetricsRegistry",
        label_names: Sequence[str] = (),
    ):
        super().__init__(name, help, registry)
        self._label_names: tuple[str, ...] = tuple(label_names)
        self._values: dict[tuple[str, ...], float] = {(): 0.0}
        self._lock = threading.Lock()

    def inc(self, n: float = 1.0) -> None:
        if n < 0:
            raise ValueError("counter can only increase")
        with self._lock:
            self._values[()] = self._values.get((), 0.0) + n

    def labels(self, **kwargs: str) -> "_LabeledCounter":
        return _LabeledCounter(self, _check_labels(self._label_names, kwargs))

    def value(self) -> float:
        with self._lock:
            return self._values.get((), 0.0)


class _LabeledCounter:
    def __init__(self, parent: Counter, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def inc(self, n: float = 1.0) -> None:
        if n < 0:
            raise ValueError("counter can only increase")
        with self._parent._lock:
            self._parent._values[self._label_values] = (
                self._parent._values.get(self._label_values, 0.0) + n
            )


class Histogram(_Metric):
    """Cumulative histogram with configurable buckets.

    Buckets are upper-inclusive: an observation of 0.05 lands in the
    `le=0.1` bucket and the `le=+Inf` bucket (which always exists).
    """

    def __init__(
        self,
        name: str,
        help: str,
        registry: "MetricsRegistry",
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        label_names: Sequence[str] = (),
    ):
        super().__init__(name, help, registry)
        self._buckets: tuple[float, ...] = tuple(sorted(set(buckets)))
        self._label_names: tuple[str, ...] = tuple(label_names)
        # {label_values: {bucket_or_key: count_or_sum}}
        self._series: dict[tuple[str, ...], dict] = {}
        self._lock = threading.Lock()

    def _empty_series(self) -> dict:
        d: dict = {b: 0.0 for b in self._buckets}
        d["_inf"] = 0.0
        d["_sum"] = 0.0
        d["_count"] = 0.0
        return d

    def observe(self, value: float) -> None:
        with self._lock:
            s = self._series.setdefault((), self._empty_series())
            s["_sum"] += value
            s["_count"] += 1
            for b in self._buckets:
                if value <= b:
                    s[b] += 1
            s["_inf"] += 1  # +Inf bucket always increments

    def labels(self, **kwargs: str) -> "_LabeledHistogram":
        return _LabeledHistogram(self, _check_labels(self._label_names, kwargs))


class _LabeledHistogram:
    def __init__(self, parent: Histogram, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def observe(self, value: float) -> None:
        with self._parent._lock:
            s = self._parent._series.setdefault(
                self._label_values, self._parent._empty_series()
            )
            s["_sum"] += value
            s["_count"] += 1
            for b in self._parent._buckets:
                if value <= b:
                    s[b] += 1
            s["_inf"] += 1


class MetricsRegistry:
    """Holds all metrics for one process. Renders Prometheus text format."""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._lock = threading.Lock()

    def _register(self, m: _Metric) -> None:
        with self._lock:
            self._metrics.append(m)

    def counter(
        self, name: str, help: str, label_names: Sequence[str] = ()
    ) -> Counter:
        return Counter(name, help, self, label_names)

    def histogram(
        self,
        name: str,
        help: str,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        label_names: Sequence[str] = (),
    ) -> Histogram:
        return Histogram(name, help, self, buckets, label_names)

    def render(self) -> str:
        """Render all metrics in Prometheus text format."""
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics)
        for m in metrics:
            lines.append(f"# HELP {m.name} {m.help}")
            if isinstance(m, Counter):
                lines.append(f"# TYPE {m.name} counter")
                with m._lock:
                    items = list(m._values.items())
                for label_values, value in items:
                    lines.append(
                        _format_line(m.name, m._label_names, label_values, value)
                    )
            elif isinstance(m, Histogram):
                lines.append(f"# TYPE {m.name} histogram")
                with m._lock:
                    series_items = list(m._series.items())
                for label_values, s in series_items:
                    for b in m._buckets:
                        lines.append(_format_line(
                            f"{m.name}_bucket", m._label_names, label_values,
                            s[b], extra_labels=[("le", _fmt_float(b))]
                        ))
                    lines.append(_format_line(
                        f"{m.name}_bucket", m._label_names, label_values,
                        s["_inf"], extra_labels=[("le", "+Inf")]
                    ))
                    lines.append(_format_line(
                        f"{m.name}_sum", m._label_names, label_values, s["_sum"]
                    ))
                    lines.append(_format_line(
                        f"{m.name}_count", m._label_names, label_values, s["_count"]
                    ))
        return "\n".join(lines) + ("\n" if lines else "")


# --- helpers -----------------------------------------------------------------

def _check_labels(expected: Sequence[str], actual: dict[str, str]) -> tuple[str, ...]:
    if set(expected) != set(actual.keys()):
        raise ValueError(
            f"expected labels {list(expected)!r}, got {sorted(actual.keys())!r}"
        )
    return tuple(actual[k] for k in expected)


def _format_line(
    name: str,
    label_names: Sequence[str],
    label_values: Sequence[str],
    value: float,
    extra_labels: Iterable[tuple[str, str]] = (),
) -> str:
    all_labels = list(zip(label_names, label_values)) + list(extra_labels)
    if all_labels:
        lstr = "{" + ",".join(
            f'{k}="{_escape(v)}"' for k, v in all_labels
        ) + "}"
    else:
        lstr = ""
    return f"{name}{lstr} {_fmt_float(value)}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_float(v: float) -> str:
    """Format a float for Prometheus text.

    - Whole numbers get ".0" suffix (Prometheus expects `1.0`, not `1`)
    - Non-whole numbers get up to 6 significant digits to suppress
      Python's noisy 0.1+0.2=0.30000000000000004 artifact
    - Special values: nan, inf, -inf → "NaN", "+Inf", "-Inf"
    """
    if v != v:  # NaN
        return "NaN"
    if v == float("inf"):
        return "+Inf"
    if v == float("-inf"):
        return "-Inf"
    if v == int(v) and abs(v) < 1e16:
        return f"{v:.1f}"
    # General format: 6 significant digits
    return f"{v:.6g}"


# --- process-singleton accessor ----------------------------------------------

_global_registry: MetricsRegistry | None = None
_global_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Process-singleton registry. Created on first call (lazy).

    This is the integration point: library code calls get_registry() at
    call time (not import time) so the registry only exists when a
    process actually wants to expose metrics.
    """
    global _global_registry
    with _global_lock:
        if _global_registry is None:
            _global_registry = MetricsRegistry()
    return _global_registry
