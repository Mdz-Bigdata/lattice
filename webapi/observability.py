# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# See the License for the specific language governing permissions
# and limitations under the License.

"""Structured logs, request metrics and traces behind 运行观测.

Everything here is in-process and bounded: the platform runs as one local
service, so a full metrics backend would cost more to operate than it explains.
Instead each request leaves

* a **structured log line** (JSON on stdout when ``LATTICE_LOG_JSON=1``, one
  readable line otherwise), with the request id, route template, status,
  duration, user and whether the answer came from a cache;
* **counters and latency histograms** per route template, exported in
  Prometheus text format at ``/api/observability/metrics``;
* a **trace**: the request plus the spans recorded inside it (a query on an
  engine, an LLM call, a catalog write), kept for the most recent requests.

Route *templates* are what is counted and logged (``/api/datasources/{id}/query``
rather than one series per data source), so cardinality stays bounded. Query
strings are never logged, and log fields are truncated.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import env

#: Latency buckets in seconds, as a Prometheus histogram would use.
BUCKETS = (0.005, 0.025, 0.1, 0.5, 1.0, 5.0, 30.0)
MAX_TRACES = 200
MAX_SLOW = 50
MAX_ERRORS = 50
MAX_FIELD = 300
SLOW_REQUEST_MS = 1000
#: Identifier-shaped path segments collapse into their placeholder.
PLACEHOLDERS = (
    (re.compile(r"^[0-9]+$"), "{id}"),
    (re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"), "{uuid}"),
    (re.compile(r"^[0-9a-fA-F]{16,64}$"), "{hash}"),
    (re.compile(r"^(ds_|run_)"), "{id}"),
)

current_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("lattice_trace", default=None)


def route_template(path: str) -> str:
    """Collapse identifier-like segments so one route is one metric series."""
    parts = []
    for segment in path.split("/"):
        if not segment:
            parts.append(segment)
            continue
        replaced = segment
        for pattern, placeholder in PLACEHOLDERS:
            if pattern.search(segment):
                replaced = placeholder
                break
        else:
            if len(segment) > 40 or ("." in segment and segment.count(".") >= 2):
                replaced = "{ref}"
        parts.append(replaced)
    return "/".join(parts)[:200] or "/"


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD:
        return value[:MAX_FIELD] + "…"
    return value


@dataclass
class Span:
    """One timed step inside a request: a query, a model call, a catalog write."""

    name: str
    kind: str
    started: float
    attributes: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    error: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "duration_ms": round(self.duration_ms, 2) if self.duration_ms is not None else None,
            "error": self.error,
            "attributes": self.attributes,
        }


@dataclass
class Trace:
    """A request and the spans it produced."""

    id: str
    method: str
    path: str
    route: str
    started: float
    user: str = ""
    status: int = 0
    duration_ms: float = 0.0
    error: str = ""
    spans: list[Span] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "user": self.user,
            "status": self.status,
            "started_at": self.started,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "attributes": self.attributes,
            "spans": [span.view() for span in self.spans],
        }


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stdout`` is at emit time, so a redirected or captured
    stdout (a service wrapper, a test) receives the lines."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


class Histogram:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.max = 0.0
        self.buckets = [0] * (len(BUCKETS) + 1)

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.max = max(self.max, seconds)
        for index, edge in enumerate(BUCKETS):
            if seconds <= edge:
                self.buckets[index] += 1
                break
        else:
            self.buckets[-1] += 1

    def quantile(self, ratio: float) -> float:
        """Upper bound of the bucket holding this quantile; exact enough to spot a regression."""
        if not self.count:
            return 0.0
        target = max(1, math.ceil(self.count * ratio))
        seen = 0
        for index, value in enumerate(self.buckets):
            seen += value
            if seen >= target:
                return BUCKETS[index] if index < len(BUCKETS) else self.max
        return self.max


class Observatory:
    """Counters, latencies, recent traces and the structured logger."""

    def __init__(self, *, json_logs: bool | None = None, level: str = "INFO", clock=time.time):
        self._lock = threading.RLock()
        self._clock = clock
        self.started_at = clock()
        self.requests: dict[tuple[str, str], dict[str, Any]] = {}
        self.latency: dict[tuple[str, str], Histogram] = {}
        self.events: dict[str, int] = {}
        self.spans: dict[str, Histogram] = {}
        self.traces: deque[Trace] = deque(maxlen=MAX_TRACES)
        self.slow: deque[dict[str, Any]] = deque(maxlen=MAX_SLOW)
        self.errors: deque[dict[str, Any]] = deque(maxlen=MAX_ERRORS)
        self.json_logs = (env("LOG_JSON", "0").strip().lower() in {"1", "true", "on", "yes"}) if json_logs is None else bool(json_logs)
        self.logger = logging.getLogger("lattice")
        if not any(isinstance(handler, _StdoutHandler) for handler in self.logger.handlers):
            handler = _StdoutHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
            self.logger.propagate = False
        self.logger.setLevel(getattr(logging, env("LOG_LEVEL", level).strip().upper(), logging.INFO))

    # ----- logging --------------------------------------------------------------------
    def log(self, event: str, level: str = "info", **fields: Any) -> None:
        """One structured line; fields are clipped and never include a query string."""
        payload = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock())),
            "level": level,
            "event": event,
            **{key: _clip(value) for key, value in fields.items() if value not in (None, "")},
        }
        trace = current_trace.get()
        if trace is not None and "trace" not in payload:
            payload["trace"] = trace.id
        with self._lock:
            self.events[event] = self.events.get(event, 0) + 1
        if self.json_logs:
            message = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            extra = " ".join(f"{key}={value}" for key, value in payload.items() if key not in {"time", "level", "event"})
            message = f"{payload['time']} {level.upper():<5} {event} {extra}".rstrip()
        self.logger.log(getattr(logging, level.upper(), logging.INFO), message)

    # ----- traces ----------------------------------------------------------------------
    def start_request(self, method: str, path: str, user: str = "") -> Trace:
        trace = Trace(id=uuid.uuid4().hex[:16], method=method, path=path[:300], route=route_template(path), started=self._clock(), user=user)
        return trace

    def finish_request(self, trace: Trace, status: int, seconds: float, error: str = "", route: str | None = None) -> Trace:
        if route:
            # The matched route's own template beats the heuristic one guessed from the path.
            trace.route = route[:200]
        trace.status = status
        trace.duration_ms = seconds * 1000
        trace.error = error[:MAX_FIELD]
        key = (trace.method, trace.route)
        with self._lock:
            bucket = self.requests.setdefault(key, {"total": 0, "errors": 0, "by_status": {}})
            bucket["total"] += 1
            bucket["by_status"][str(status)] = bucket["by_status"].get(str(status), 0) + 1
            if status >= 500 or error:
                bucket["errors"] += 1
            self.latency.setdefault(key, Histogram()).observe(seconds)
            self.traces.append(trace)
            if trace.duration_ms >= SLOW_REQUEST_MS:
                self.slow.append({"id": trace.id, "route": trace.route, "method": trace.method, "duration_ms": round(trace.duration_ms, 2), "at": trace.started, "user": trace.user})
            if status >= 400:
                self.errors.append({"id": trace.id, "route": trace.route, "method": trace.method, "status": status, "error": trace.error, "at": trace.started, "user": trace.user})
        self.log(
            "request",
            level="error" if status >= 500 else ("warning" if status >= 400 else "info"),
            method=trace.method,
            route=trace.route,
            status=status,
            duration_ms=round(trace.duration_ms, 1),
            user=trace.user,
            spans=len(trace.spans) or None,
            detail=trace.error or None,
            trace=trace.id,
        )
        return trace

    def span(self, name: str, kind: str = "internal", **attributes: Any):
        """Context manager timing one step of the current request."""
        return _SpanContext(self, name, kind, attributes)

    def record_span(self, span: Span) -> None:
        with self._lock:
            self.spans.setdefault(f"{span.kind}:{span.name}", Histogram()).observe((span.duration_ms or 0) / 1000)
        trace = current_trace.get()
        if trace is not None and len(trace.spans) < 50:
            trace.spans.append(span)

    # ----- reporting --------------------------------------------------------------------
    def summary(self, top: int = 10) -> dict[str, Any]:
        with self._lock:
            routes = []
            for (method, route), bucket in self.requests.items():
                histogram = self.latency.get((method, route)) or Histogram()
                routes.append(
                    {
                        "method": method,
                        "route": route,
                        "total": bucket["total"],
                        "errors": bucket["errors"],
                        "by_status": dict(bucket["by_status"]),
                        "avg_ms": round(histogram.total / histogram.count * 1000, 1) if histogram.count else 0.0,
                        "p95_ms": round(histogram.quantile(0.95) * 1000, 1),
                        "max_ms": round(histogram.max * 1000, 1),
                    }
                )
            total = sum(item["total"] for item in routes)
            errors = sum(item["errors"] for item in routes)
            spans = [
                {
                    "name": name,
                    "count": histogram.count,
                    "avg_ms": round(histogram.total / histogram.count * 1000, 1) if histogram.count else 0.0,
                    "p95_ms": round(histogram.quantile(0.95) * 1000, 1),
                }
                for name, histogram in self.spans.items()
            ]
            return {
                "uptime_seconds": round(self._clock() - self.started_at, 1),
                "requests": total,
                "errors": errors,
                "error_rate": round(errors / total, 4) if total else 0.0,
                "routes": sorted(routes, key=lambda item: -item["total"])[:top],
                "slowest_routes": sorted(routes, key=lambda item: -item["p95_ms"])[:top],
                "spans": sorted(spans, key=lambda item: -item["count"])[:top],
                "events": dict(sorted(self.events.items(), key=lambda item: -item[1])[:top]),
                "recent_slow": list(self.slow)[-top:][::-1],
                "recent_errors": list(self.errors)[-top:][::-1],
                "json_logs": self.json_logs,
                "log_level": logging.getLevelName(self.logger.level),
            }

    def trace_list(self, limit: int = 50, route: str | None = None, min_ms: float = 0, status: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                trace.view()
                for trace in reversed(self.traces)
                if (not route or route in trace.route)
                and trace.duration_ms >= min_ms
                and (status is None or trace.status == status)
            ]
        return items[: max(1, min(int(limit or 50), MAX_TRACES))]

    def trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            for trace in reversed(self.traces):
                if trace.id == trace_id:
                    return trace.view()
        return None

    def prometheus(self) -> str:
        """The counters and histograms in Prometheus text exposition format."""
        lines = [
            "# HELP lattice_up 1 when the service is serving.",
            "# TYPE lattice_up gauge",
            "lattice_up 1",
            "# HELP lattice_uptime_seconds Seconds since the service started.",
            "# TYPE lattice_uptime_seconds gauge",
            f"lattice_uptime_seconds {round(self._clock() - self.started_at, 1)}",
            "# HELP lattice_requests_total Requests by method, route and status.",
            "# TYPE lattice_requests_total counter",
        ]
        with self._lock:
            for (method, route), bucket in sorted(self.requests.items()):
                for status, count in sorted(bucket["by_status"].items()):
                    lines.append(f'lattice_requests_total{{method="{method}",route="{_escape(route)}",status="{status}"}} {count}')
            lines += [
                "# HELP lattice_request_duration_seconds Request latency by route.",
                "# TYPE lattice_request_duration_seconds histogram",
            ]
            for (method, route), histogram in sorted(self.latency.items()):
                labels = f'method="{method}",route="{_escape(route)}"'
                cumulative = 0
                for index, edge in enumerate(BUCKETS):
                    cumulative += histogram.buckets[index]
                    lines.append(f'lattice_request_duration_seconds_bucket{{{labels},le="{edge}"}} {cumulative}')
                lines.append(f'lattice_request_duration_seconds_bucket{{{labels},le="+Inf"}} {histogram.count}')
                lines.append(f"lattice_request_duration_seconds_sum{{{labels}}} {round(histogram.total, 6)}")
                lines.append(f"lattice_request_duration_seconds_count{{{labels}}} {histogram.count}")
            lines += [
                "# HELP lattice_span_duration_seconds Duration of internal steps (queries, model calls).",
                "# TYPE lattice_span_duration_seconds summary",
            ]
            for name, histogram in sorted(self.spans.items()):
                lines.append(f'lattice_span_duration_seconds_count{{span="{_escape(name)}"}} {histogram.count}')
                lines.append(f'lattice_span_duration_seconds_sum{{span="{_escape(name)}"}} {round(histogram.total, 6)}')
            lines += ["# HELP lattice_events_total Structured log events by name.", "# TYPE lattice_events_total counter"]
            for event, count in sorted(self.events.items()):
                lines.append(f'lattice_events_total{{event="{_escape(event)}"}} {count}')
        return "\n".join(lines) + "\n"

    def reset(self) -> dict[str, Any]:
        with self._lock:
            counts = {"routes": len(self.requests), "traces": len(self.traces)}
            self.requests.clear()
            self.latency.clear()
            self.spans.clear()
            self.events.clear()
            self.traces.clear()
            self.slow.clear()
            self.errors.clear()
            self.started_at = self._clock()
        return {"reset": True, **counts}

    def set_level(self, level: str) -> dict[str, Any]:
        name = str(level or "").strip().upper()
        if name not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("日志级别只能是 DEBUG、INFO、WARNING 或 ERROR。")
        self.logger.setLevel(getattr(logging, name))
        return {"log_level": name}


#: The observatory the running application installed; spans are dropped without one.
_ACTIVE: Observatory | None = None


def install_observatory(observatory: Observatory) -> Observatory:
    """Make this observatory the one ``observe()`` and ``log_event()`` write to."""
    global _ACTIVE
    _ACTIVE = observatory
    return observatory


def active() -> Observatory | None:
    return _ACTIVE


def observe(name: str, kind: str = "internal", **attributes: Any):
    """Time one step of the current request; a no-op when nothing is installed."""
    observatory = _ACTIVE
    if observatory is None:
        return _NullSpan()
    return observatory.span(name, kind, **attributes)


def log_event(event: str, level: str = "info", **fields: Any) -> None:
    if _ACTIVE is not None:
        _ACTIVE.log(event, level, **fields)


class _NullSpan:
    def __enter__(self) -> Span:
        return Span(name="", kind="", started=time.monotonic())

    def __exit__(self, *exc_info) -> bool:
        return False


class _SpanContext:
    def __init__(self, observatory: Observatory, name: str, kind: str, attributes: dict[str, Any]):
        self.observatory = observatory
        self.span = Span(name=name[:120], kind=kind, started=time.monotonic(), attributes={key: _clip(value) for key, value in attributes.items()})

    def __enter__(self) -> Span:
        return self.span

    def __exit__(self, kind, error, traceback) -> bool:
        self.span.duration_ms = (time.monotonic() - self.span.started) * 1000
        if error is not None:
            self.span.error = str(error)[:MAX_FIELD]
        self.observatory.record_span(self.span)
        return False


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def process_health() -> dict[str, Any]:
    """What the operations view shows about this process, without extra dependencies."""
    info: dict[str, Any] = {"pid": os.getpid(), "threads": threading.active_count(), "python": sys.version.split()[0]}
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        info["memory_mb"] = round(usage.ru_maxrss / divisor, 1)
        info["cpu_seconds"] = round(usage.ru_utime + usage.ru_stime, 1)
    except (ImportError, OSError):  # pragma: no cover - platform specific
        pass
    info["thread_names"] = sorted({thread.name for thread in threading.enumerate() if thread.name.startswith("lattice")})
    return info


def iter_components(state: Any) -> Iterator[dict[str, Any]]:
    """Health of each subsystem, each guarded so one failure does not hide the rest."""
    def probe(name: str, label: str, call) -> dict[str, Any]:
        try:
            return {"name": name, "label": label, **call()}
        except Exception as error:  # noqa: BLE001 - the health view reports failures, never raises
            return {"name": name, "label": label, "ok": False, "detail": f"{type(error).__name__}: {str(error)[:200]}"}

    quality = getattr(state, "quality_store", None)
    if quality is not None:
        yield probe("quality", "质量元数据库", quality.healthy)
    metadata = getattr(state, "metadata_store", None)
    if metadata is not None:
        yield probe("metadata", "元数据目录", metadata.healthy)
    scheduler = getattr(state, "quality_scheduler", None)
    if scheduler is not None:
        yield probe("scheduler", "任务调度器", lambda: {"ok": scheduler.running(), **scheduler.status()})
    ingest = getattr(state, "metadata_scheduler", None)
    if ingest is not None:
        yield probe("ingestion", "元数据拾取调度", lambda: {"ok": ingest.running(), **ingest.status()})
    cache = getattr(state, "query_cache", None)
    if cache is not None:
        yield probe("cache", "查询结果缓存", lambda: {"ok": True, **cache.stats()})
    polaris = getattr(state, "polaris", None)
    if polaris is not None:
        yield probe("polaris", "Apache Polaris", lambda: {"ok": str(polaris.status().get("status")) in {"online", "healthy"}, **polaris.status()})
    llm = getattr(state, "llm", None)
    if llm is not None:
        yield probe("llm", "大模型", lambda: {"ok": bool(llm.configured()), **llm.status()})
