from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Counter:
    """Monotonically increasing counter metric."""

    name: str
    help: str
    labels: list[str] = field(default_factory=list)
    _values: dict[tuple[str, ...], float] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def inc(self, amount: float = 1, labels: Optional[dict[str, str]] = None) -> None:
        """Increment counter by amount."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            self._values[label_tuple] = self._values.get(label_tuple, 0) + amount

    def get(self, labels: Optional[dict[str, str]] = None) -> float:
        """Get current counter value."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            return self._values.get(label_tuple, 0)

    def _validate_labels(self, labels: Optional[dict[str, str]]) -> tuple[str, ...]:
        """Convert labels dict to tuple, validating against schema."""
        if not self.labels:
            return ()
        if labels is None:
            labels = {}
        return tuple(labels.get(label, "") for label in self.labels)

    def export(self) -> dict[str, Any]:
        """Export metric data."""
        with self._lock:
            return {
                "type": "counter",
                "name": self.name,
                "help": self.help,
                "labels": self.labels,
                "values": {
                    self._format_labels(label_tuple): value
                    for label_tuple, value in self._values.items()
                }
            }

    def _format_labels(self, label_tuple: tuple[str, ...]) -> str:
        """Format label tuple as string."""
        if not label_tuple:
            return ""
        return ",".join(f"{k}={v}" for k, v in zip(self.labels, label_tuple))


@dataclass
class Gauge:
    """Gauge metric that can go up or down."""

    name: str
    help: str
    labels: list[str] = field(default_factory=list)
    _values: dict[tuple[str, ...], float] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def set(self, value: float, labels: Optional[dict[str, str]] = None) -> None:
        """Set gauge to specific value."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            self._values[label_tuple] = value

    def inc(self, amount: float = 1, labels: Optional[dict[str, str]] = None) -> None:
        """Increment gauge by amount."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            self._values[label_tuple] = self._values.get(label_tuple, 0) + amount

    def dec(self, amount: float = 1, labels: Optional[dict[str, str]] = None) -> None:
        """Decrement gauge by amount."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            self._values[label_tuple] = self._values.get(label_tuple, 0) - amount

    def get(self, labels: Optional[dict[str, str]] = None) -> float:
        """Get current gauge value."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            return self._values.get(label_tuple, 0)

    def _validate_labels(self, labels: Optional[dict[str, str]]) -> tuple[str, ...]:
        """Convert labels dict to tuple, validating against schema."""
        if not self.labels:
            return ()
        if labels is None:
            labels = {}
        return tuple(labels.get(label, "") for label in self.labels)

    def export(self) -> dict[str, Any]:
        """Export metric data."""
        with self._lock:
            return {
                "type": "gauge",
                "name": self.name,
                "help": self.help,
                "labels": self.labels,
                "values": {
                    self._format_labels(label_tuple): value
                    for label_tuple, value in self._values.items()
                }
            }

    def _format_labels(self, label_tuple: tuple[str, ...]) -> str:
        """Format label tuple as string."""
        if not label_tuple:
            return ""
        return ",".join(f"{k}={v}" for k, v in zip(self.labels, label_tuple))


@dataclass
class _HistogramData:
    """Internal histogram data structure."""

    count: int = 0
    sum: float = 0.0
    buckets: dict[float, int] = field(default_factory=dict)


@dataclass
class Histogram:
    """Histogram metric for tracking distributions."""

    name: str
    help: str
    buckets: list[float]
    labels: list[str] = field(default_factory=list)
    _data: dict[tuple[str, ...], _HistogramData] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def observe(self, value: float, labels: Optional[dict[str, str]] = None) -> None:
        """Record an observation."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            if label_tuple not in self._data:
                self._data[label_tuple] = _HistogramData(
                    buckets={bucket: 0 for bucket in self.buckets}
                )

            data = self._data[label_tuple]
            data.count += 1
            data.sum += value

            for bucket in self.buckets:
                if value <= bucket:
                    data.buckets[bucket] += 1

    def get(self, labels: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Get histogram data."""
        label_tuple = self._validate_labels(labels)
        with self._lock:
            data = self._data.get(label_tuple, _HistogramData(
                buckets={bucket: 0 for bucket in self.buckets}
            ))
            return {
                "count": data.count,
                "sum": data.sum,
                "buckets": dict(data.buckets)
            }

    def _validate_labels(self, labels: Optional[dict[str, str]]) -> tuple[str, ...]:
        """Convert labels dict to tuple, validating against schema."""
        if not self.labels:
            return ()
        if labels is None:
            labels = {}
        return tuple(labels.get(label, "") for label in self.labels)

    def export(self) -> dict[str, Any]:
        """Export metric data."""
        with self._lock:
            return {
                "type": "histogram",
                "name": self.name,
                "help": self.help,
                "labels": self.labels,
                "values": {
                    self._format_labels(label_tuple): {
                        "count": data.count,
                        "sum": data.sum,
                        "buckets": dict(data.buckets)
                    }
                    for label_tuple, data in self._data.items()
                }
            }

    def _format_labels(self, label_tuple: tuple[str, ...]) -> str:
        """Format label tuple as string."""
        if not label_tuple:
            return ""
        return ",".join(f"{k}={v}" for k, v in zip(self.labels, label_tuple))


class MetricsRegistry:
    """Thread-safe registry for all metrics."""

    def __init__(self) -> None:
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._lock = threading.Lock()

    def register(self, metric: Counter | Gauge | Histogram) -> None:
        """Register a metric."""
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"Metric {metric.name} already registered")
            self._metrics[metric.name] = metric

    def get_metric(self, name: str) -> Optional[Counter | Gauge | Histogram]:
        """Get a metric by name."""
        with self._lock:
            return self._metrics.get(name)

    def export_dict(self) -> dict[str, Any]:
        """Export all metrics as a dictionary."""
        with self._lock:
            return {
                name: metric.export()
                for name, metric in self._metrics.items()
            }

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []

        with self._lock:
            for name, metric in self._metrics.items():
                exported = metric.export()

                # HELP and TYPE lines
                lines.append(f"# HELP {name} {exported['help']}")
                lines.append(f"# TYPE {name} {exported['type']}")

                # Metric values
                if exported['type'] == 'histogram':
                    for label_str, data in exported['values'].items():
                        label_suffix = f"{{{label_str}}}" if label_str else ""

                        # Bucket lines
                        for bucket, count in sorted(data['buckets'].items()):
                            bucket_labels = f"{label_str},le=\"{bucket}\"" if label_str else f"le=\"{bucket}\""
                            lines.append(f"{name}_bucket{{{bucket_labels}}} {count}")

                        # +Inf bucket
                        inf_labels = f"{label_str},le=\"+Inf\"" if label_str else "le=\"+Inf\""
                        lines.append(f"{name}_bucket{{{inf_labels}}} {data['count']}")

                        # Sum and count
                        lines.append(f"{name}_sum{label_suffix} {data['sum']}")
                        lines.append(f"{name}_count{label_suffix} {data['count']}")
                else:
                    for label_str, value in exported['values'].items():
                        label_suffix = f"{{{label_str}}}" if label_str else ""
                        lines.append(f"{name}{label_suffix} {value}")

        return "\n".join(lines) + "\n"


# Global registry
_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    """Get the global metrics registry."""
    return _registry


# Pre-defined metrics - Counters
PRINTS_STARTED = Counter(
    "kiln_prints_started_total",
    "Total prints started",
    labels=["printer", "material"]
)
_registry.register(PRINTS_STARTED)

PRINTS_COMPLETED = Counter(
    "kiln_prints_completed_total",
    "Total prints completed",
    labels=["printer", "status"]
)
_registry.register(PRINTS_COMPLETED)

SAFETY_VIOLATIONS = Counter(
    "kiln_safety_violations_total",
    "Safety check failures",
    labels=["type"]
)
_registry.register(SAFETY_VIOLATIONS)

API_REQUESTS = Counter(
    "kiln_api_requests_total",
    "API requests",
    labels=["tool", "status"]
)
_registry.register(API_REQUESTS)

# Pre-defined metrics - Gauges
ACTIVE_PRINTERS = Gauge(
    "kiln_active_printers",
    "Currently connected printers"
)
_registry.register(ACTIVE_PRINTERS)

QUEUE_DEPTH = Gauge(
    "kiln_queue_depth",
    "Jobs in queue",
    labels=["status"]
)
_registry.register(QUEUE_DEPTH)

PRINTER_TEMP = Gauge(
    "kiln_printer_temperature_celsius",
    "Printer temperature",
    labels=["printer", "heater"]
)
_registry.register(PRINTER_TEMP)

# Pre-defined metrics - Histograms
PRINT_DURATION = Histogram(
    "kiln_print_duration_seconds",
    "Print job duration",
    buckets=[300, 900, 1800, 3600, 7200, 14400, 28800]
)
_registry.register(PRINT_DURATION)

API_LATENCY = Histogram(
    "kiln_api_latency_seconds",
    "API call latency",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)
_registry.register(API_LATENCY)
