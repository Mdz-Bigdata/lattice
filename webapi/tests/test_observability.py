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

"""Structured logs, request metrics, traces and the operations view."""

import json

import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.observability import (
    Histogram,
    Observatory,
    current_trace,
    process_health,
    route_template,
)


@pytest.fixture
def observatory():
    return Observatory(json_logs=True)


def test_route_templates_collapse_identifiers_so_series_stay_bounded():
    assert route_template("/api/datasources/ds_9fa1/query") == "/api/datasources/{id}/query"
    assert route_template("/api/quality/rules/42/run") == "/api/quality/rules/{id}/run"
    assert route_template("/api/metadata/entity/versions") == "/api/metadata/entity/versions"
    assert route_template("/api/observability/traces/0123456789abcdef") == "/api/observability/traces/{hash}"
    assert route_template("/api/metadata/entity/mysql-local.default.lattice_demo.orders") == "/api/metadata/entity/{ref}"
    assert route_template("/") == "/"


def test_histogram_counts_buckets_and_reports_a_quantile():
    histogram = Histogram()
    for value in (0.001, 0.01, 0.2, 0.2, 60):
        histogram.observe(value)
    assert histogram.count == 5 and round(histogram.total, 3) == 60.411 and histogram.max == 60
    assert histogram.quantile(0.5) == 0.5 and histogram.quantile(0.95) == 60


def test_requests_are_counted_logged_and_traced(observatory, capsys):
    trace = observatory.start_request("POST", "/api/datasources/ds_1/query", "vera")
    token = current_trace.set(trace)
    try:
        with observatory.span("query", "engine", datasource="ds_1"):
            pass
    finally:
        current_trace.reset(token)
    observatory.finish_request(trace, 200, 0.42)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "request" and line["route"] == "/api/datasources/{id}/query"
    assert line["status"] == 200 and line["user"] == "vera" and line["trace"] == trace.id
    summary = observatory.summary()
    assert summary["requests"] == 1 and summary["errors"] == 0
    assert summary["routes"][0]["avg_ms"] == 420.0 and summary["routes"][0]["p95_ms"] == 500.0
    assert summary["spans"][0]["name"] == "engine:query"
    stored = observatory.trace(trace.id)
    assert stored["spans"][0]["attributes"] == {"datasource": "ds_1"} and stored["duration_ms"] == 420.0
    assert observatory.trace_list(route="datasources")[0]["id"] == trace.id
    assert observatory.trace_list(min_ms=1000) == [] and observatory.trace("nope") is None


def test_failures_are_separated_from_slow_requests(observatory):
    for index in range(3):
        failed = observatory.start_request("GET", f"/api/quality/rules/{index}", "root")
        observatory.finish_request(failed, 500, 0.01, "QualityStoreError: 连接失败")
    slow = observatory.start_request("GET", "/api/metadata/search", "root")
    observatory.finish_request(slow, 200, 2.5)
    summary = observatory.summary()
    assert summary["errors"] == 3 and summary["error_rate"] == 0.75
    assert [item["route"] for item in summary["recent_errors"]] == ["/api/quality/rules/{id}"] * 3
    assert observatory.finish_request(observatory.start_request("GET", "/api/x/1"), 200, 0.01, "", "/api/x/{item_id}").route == "/api/x/{item_id}"
    assert summary["recent_slow"][0]["route"] == "/api/metadata/search"
    assert summary["slowest_routes"][0]["route"] == "/api/metadata/search"
    rules = next(item for item in summary["routes"] if item["route"] == "/api/quality/rules/{id}")
    assert rules["by_status"] == {"500": 3} and rules["errors"] == 3


def test_prometheus_export_is_well_formed_and_escapes_labels(observatory):
    trace = observatory.start_request("GET", "/api/metadata/entity/a.b.c", "root")
    observatory.finish_request(trace, 200, 0.05)
    with observatory.span("crawl", "ingestion"):
        pass
    text = observatory.prometheus()
    assert "# TYPE lattice_requests_total counter" in text
    assert 'lattice_requests_total{method="GET",route="/api/metadata/entity/{ref}",status="200"} 1' in text
    assert 'lattice_request_duration_seconds_bucket{method="GET",route="/api/metadata/entity/{ref}",le="0.1"} 1' in text
    assert 'lattice_request_duration_seconds_count{method="GET",route="/api/metadata/entity/{ref}"} 1' in text
    assert 'lattice_span_duration_seconds_count{span="ingestion:crawl"} 1' in text
    assert 'lattice_events_total{event="request"} 1' in text
    assert all(line.startswith("#") or line.count(" ") >= 1 for line in text.splitlines())


def test_plain_logs_clip_fields_and_the_level_can_change(capsys):
    observatory = Observatory(json_logs=False)
    observatory.log("ingestion", detail="x" * 400, table="orders")
    printed = capsys.readouterr().out.strip()
    assert printed.endswith("table=orders") and "…" in printed and "INFO  ingestion" in printed
    assert observatory.set_level("warning") == {"log_level": "WARNING"}
    observatory.log("quiet", level="info")
    assert capsys.readouterr().out == ""
    with pytest.raises(ValueError, match="日志级别"):
        observatory.set_level("chatty")
    assert observatory.summary()["events"]["quiet"] == 1  # counted even when not printed


def test_reset_clears_counters_and_process_health_describes_the_service(observatory):
    observatory.finish_request(observatory.start_request("GET", "/api/health"), 200, 0.01)
    assert observatory.reset()["traces"] == 1
    assert observatory.summary()["requests"] == 0 and observatory.trace_list() == []
    health = process_health()
    assert health["pid"] > 0 and health["threads"] >= 1 and health["python"].startswith("3.")


# ----- routes ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        yield value


def test_every_request_is_traced_and_reported(client):
    client.post("/api/observability/reset", json={})
    answer = client.post("/api/query", json={"sql": "SELECT 1 AS one", "datasource_id": "local-sample"})
    assert answer.status_code == 200
    trace_id = answer.headers["X-Lattice-Trace-Id"]
    assert client.get("/api/datasources/nope").status_code == 404
    summary = client.get("/api/observability/summary").json()
    routes = {(item["method"], item["route"]) for item in summary["metrics"]["routes"]}
    assert ("POST", "/api/query") in routes and ("GET", "/api/datasources/{source_id}") in routes
    assert summary["metrics"]["recent_errors"][0]["status"] == 404  # 4xx is listed, only 5xx counts as an error
    assert summary["process"]["pid"] > 0
    components = {item["name"]: item for item in summary["components"]}
    assert components["cache"]["ok"] is True and components["polaris"]["ok"] is True
    assert "元数据目录" in components["metadata"]["label"] and "label" in components["scheduler"]
    trace = client.get(f"/api/observability/traces/{trace_id}").json()
    assert trace["route"] == "/api/query" and trace["status"] == 200
    assert [span["kind"] for span in trace["spans"]] == ["engine"]
    assert trace["spans"][0]["attributes"]["datasource"] == "local-sample"
    listing = client.get("/api/observability/traces", params={"route": "/api/query", "limit": 5}).json()
    assert listing["items"][0]["id"] == trace_id
    assert client.get("/api/observability/traces/deadbeef").status_code == 404
    metrics = client.get("/api/observability/metrics")
    assert metrics.headers["content-type"].startswith("text/plain")
    assert 'lattice_requests_total{method="POST",route="/api/query",status="200"}' in metrics.text
    assert "lattice_up 1" in metrics.text
    assert client.post("/api/observability/log-level", json={"level": "INFO"}).json() == {"log_level": "INFO"}
    assert client.post("/api/observability/log-level", json={"level": "loud"}).status_code == 400
    assert client.post("/api/observability/reset", json={}).json()["reset"] is True
    assert client.get("/api/observability/summary").json()["metrics"]["requests"] <= 1
