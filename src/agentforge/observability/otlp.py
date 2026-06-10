"""Hand-rolled OpenTelemetry OTLP/HTTP metrics exporter.

Pushes the in-process MetricsRegistry to a configured OTLP/HTTP endpoint
(default protocol, JSON encoding) on a background thread. No opentelemetry-*
dependency — keeps the project self-contained.

Schema reference: https://opentelemetry.io/docs/specs/otlp/#metrics-request
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import requests

from agentforge.observability.metrics import Counter, Histogram, MetricsRegistry

logger = logging.getLogger(__name__)


def _str_attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _fmt_unix_nanos(t: float | None = None) -> str:
    """Format a Unix timestamp in nanoseconds as a string."""
    if t is None:
        t = time.time()
    return str(int(t * 1_000_000_000))


def _escape_label_value(v: str) -> str:
    """OTLP allows any UTF-8 string; no escaping needed beyond JSON."""
    return v


class OtlpExporter:
    """Periodically push a MetricsRegistry to an OTLP/HTTP collector.

    Configure with `endpoint="http://collector:4318"` and the standard
    OTLP path `/v1/metrics` is appended automatically. `push_interval_seconds`
    controls how often the background thread fires. If `endpoint` is None
    the exporter is inert — push_once() is a no-op, start() does nothing.
    """

    def __init__(
        self,
        endpoint: Optional[str],
        registry: MetricsRegistry,
        service_name: str = "agentforge",
        service_version: str = "0.5.5",
        push_interval_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
    ):
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.registry = registry
        self.service_name = service_name
        self.service_version = service_version
        self.push_interval = push_interval_seconds
        self.timeout = timeout_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _build_payload(self) -> dict:
        """Convert the in-process registry to an OTLP metrics request body."""
        now_nanos = _fmt_unix_nanos()
        # The agent process start is the metric start. We don't track this
        # exactly; use a far-past timestamp so cumulative metrics look right.
        start_nanos = _fmt_unix_nanos(time.time() - 3600)

        scope_metrics: list[dict] = []
        with self.registry._lock:
            metrics_snapshot = list(self.registry._metrics)

        for m in metrics_snapshot:
            if isinstance(m, Counter):
                scope_metrics.append(self._counter_to_otlp(m, start_nanos, now_nanos))
            elif isinstance(m, Histogram):
                scope_metrics.append(self._histogram_to_otlp(m, start_nanos, now_nanos))

        return {
            "resourceMetrics": [{
                "resource": {
                    "attributes": [
                        _str_attr("service.name", self.service_name),
                        _str_attr("service.version", self.service_version),
                    ],
                },
                "scopeMetrics": [{
                    "scope": {"name": "agentforge", "version": self.service_version},
                    "metrics": scope_metrics,
                }],
            }],
        }

    def _counter_to_otlp(self, m: Counter, start_nanos: str, now_nanos: str) -> dict:
        data_points = []
        with m._lock:
            items = list(m._values.items())
        for label_values, value in items:
            attrs = [
                _str_attr(name, _escape_label_value(str(lv)))
                for name, lv in zip(m._label_names, label_values)
            ]
            data_points.append({
                "attributes": attrs,
                "startTimeUnixNano": start_nanos,
                "timeUnixNano": now_nanos,
                "asInt": str(int(value)),
            })
        return {
            "name": m.name,
            "unit": "1",
            "sum": {
                "dataPoints": data_points,
                "aggregationTemporality": "AGGREGATION_TEMPORALITY_CUMULATIVE",
                "isMonotonic": True,
            },
        }

    def _histogram_to_otlp(self, m: Histogram, start_nanos: str, now_nanos: str) -> dict:
        data_points = []
        with m._lock:
            series_items = list(m._series.items())
        for label_values, s in series_items:
            attrs = [
                _str_attr(name, _escape_label_value(str(lv)))
                for name, lv in zip(m._label_names, label_values)
            ]
            # Bucket counts in order; +Inf is appended at the end (no upper bound)
            explicit_bounds = list(m._buckets)
            bucket_counts = [str(int(s[b])) for b in explicit_bounds]
            bucket_counts.append(str(int(s["_inf"])))
            data_points.append({
                "attributes": attrs,
                "startTimeUnixNano": start_nanos,
                "timeUnixNano": now_nanos,
                "count": str(int(s["_count"])),
                "sum": s["_sum"],
                "bucketCounts": bucket_counts,
                "explicitBounds": explicit_bounds,
            })
        return {
            "name": m.name,
            "unit": "s",
            "histogram": {
                "dataPoints": data_points,
                "aggregationTemporality": "AGGREGATION_TEMPORALITY_CUMULATIVE",
            },
        }

    def push_once(self) -> None:
        """Build payload and POST to the configured endpoint. No-op if disabled."""
        if not self.endpoint:
            return
        try:
            payload = self._build_payload()
            url = f"{self.endpoint}/v1/metrics"
            resp = requests.post(
                url, json=payload, timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "OTLP push to %s returned status %d: %s",
                    url, resp.status_code, resp.text[:200],
                )
        except requests.RequestException as e:
            logger.warning("OTLP push failed: %s", e)
        except Exception as e:
            # Last-resort guard: never let metrics export crash the agent
            logger.exception("OTLP push unexpected error: %s", e)

    def _loop(self) -> None:
        """Background thread loop. Sleeps in 1s slices so stop() is responsive."""
        while not self._stop.is_set():
            self.push_once()
            # Sleep in small slices to react to stop() quickly
            for _ in range(int(self.push_interval)):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def start(self) -> None:
        """Spawn a daemon thread that pushes every push_interval_seconds.

        Idempotent — calling start() twice is a no-op (same thread).
        """
        if not self.endpoint:
            logger.info("OTLP exporter disabled (no endpoint configured)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="otlp-exporter", daemon=True,
        )
        self._thread.start()
        logger.info("OTLP exporter started, pushing to %s every %ss",
                    self.endpoint, self.push_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background thread to stop and wait for it to join."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
