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

"""The /api/metadata routes, their wiring into the app and the MCP transport."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module

ORDERS = "local-sample.sample.main.t_lattice_orders"
ITEMS = "local-sample.sample.main.t_lattice_order_items"
MCP = "/api/metadata/mcp"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        yield value


def ingest_sample(client):
    assert client.post("/api/metadata/ingestion/sync-datasources", json={}).json()["created"] == 1
    run = client.post("/api/metadata/ingestion/run", json={"service_fqn": "local-sample", "background": False}).json()
    assert run["status"] == "success", run
    return run


def test_health_types_and_errors(client):
    health = client.get("/api/metadata/health").json()
    assert health["ok"] and health["backend"] == "sqlite" and health["scheduler"]["running"] is False
    types = client.get("/api/metadata/types").json()
    assert {item["id"] for item in types["service_categories"]} >= {"database", "messaging", "dashboard", "pipeline", "mlmodel", "storage", "search", "metadata", "api"}
    assert len(types["kpi_charts"]) == 4 and types["max_lineage_depth"] == 6
    response = client.post("/api/metadata/entities", json={"entity_type": "nope", "name": "x"})
    assert response.status_code == 400 and "未知的实体类型" in response.json()["detail"]
    assert client.post("/api/metadata/entities", json={"entity_type": "glossary", "name": "x", "unknown": 1}).status_code == 422
    assert client.get("/api/metadata/entity", params={"entity_type": "table", "ref": "a.b.c.d"}).status_code == 404
    capabilities = client.get("/api/bootstrap").json()["capabilities"]
    assert "元数据上下文层与 MCP 服务" in capabilities


def test_ingest_browse_sample_and_usage(client):
    run = ingest_sample(client)
    assert run["summary"]["tables"] == 7
    entity = client.get("/api/metadata/entity", params={"entity_type": "table", "ref": ORDERS, "view": True}).json()
    assert entity["column_count"] == 4 and entity["service"]["fqn"] == "local-sample"
    sample = client.get("/api/metadata/entity/sample", params={"ref": ORDERS, "limit": 2}).json()
    assert sample["columns"] == ["order_id", "customer_id", "order_date", "status"] and len(sample["rows"]) == 2
    assert client.post("/api/datasources/local-sample/query", json={"sql": "SELECT count(*) AS n FROM t_lattice_orders"}).status_code == 200
    usage = client.get("/api/metadata/entity/usage", params={"entity_type": "table", "ref": ORDERS}).json()
    assert usage["queries"] == 1 and usage["views"] == 1
    assert client.get("/api/metadata/entity/queries", params={"entity_type": "table", "ref": ORDERS}).json()["total"] == 1
    children = client.get("/api/metadata/entity/children", params={"entity_type": "databaseSchema", "ref": "local-sample.sample.main"}).json()
    assert children["groups"][0]["total"] == 7
    database = next(item for item in client.get("/api/metadata/tree").json()["categories"] if item["id"] == "database")
    assert {service["fqn"] for service in database["services"]} == {"local-sample", "polaris"}
    services = {item["fqn"]: item for item in client.get("/api/metadata/ingestion/services").json()["items"]}
    assert services["local-sample"]["last_run"]["status"] == "success" and services["local-sample"]["asset_count"] == 7
    assert client.get("/api/metadata/search", params={"q": "t_lattice_orders"}).json()["total"] == 1


def test_question_schema_carries_catalog_semantics(client):
    ingest_sample(client)
    update = {"entity_type": "table", "ref": ORDERS, "description": "订单主表", "columns": [{"name": "status", "description": "订单状态：已完成/已取消"}]}
    assert client.post("/api/metadata/entities/update", json=update).json()["version"] == 0.2
    state = app_module.app.state
    text = state.queries._schema_context(state.sources.record("local-sample"))
    assert "t_lattice_orders（" in text
    assert "main.t_lattice_orders：订单主表" in text and "status：订单状态：已完成/已取消" in text


def test_lineage_glossary_and_task_routes(client):
    ingest_sample(client)
    parsed = client.post(
        "/api/metadata/lineage/sql",
        json={"sql": "INSERT INTO t_lattice_orders SELECT order_id, 0 AS customer_id, NULL AS order_date, 'x' AS status FROM t_lattice_order_items", "datasource_id": "local-sample", "apply": True},
    ).json()
    assert parsed["applied"] == 1 and parsed["columns"][0]["to_column"] == f"{ORDERS}.order_id"
    assert client.get("/api/metadata/lineage", params={"entity_type": "table", "ref": ORDERS}).json()["upstream_count"] == 1
    assert client.get("/api/metadata/lineage/impact", params={"entity_type": "table", "ref": ITEMS}).json()["total"] == 1
    assert client.post("/api/metadata/lineage/edges/delete", json={"from_fqn": ITEMS, "to_fqn": ORDERS}).json()["deleted"]
    assert client.get("/api/metadata/lineage/summary").json()["total"] == 0
    client.post("/api/metadata/entities", json={"entity_type": "glossary", "name": "Sales", "description": "销售"})
    client.post("/api/metadata/entities", json={"entity_type": "glossaryTerm", "name": "GMV", "parent_fqn": "Sales", "description": "成交总额"})
    assert [item["fqn"] for item in client.get("/api/metadata/tags/options", params={"q": "gmv"}).json()["items"]] == ["Sales.GMV"]
    tagged = client.post("/api/metadata/entities/tags", json={"target_fqn": ORDERS, "tags": [{"tag_fqn": "Sales.GMV", "source": "glossary"}]}).json()
    assert tagged["glossary_terms"][0]["tag_fqn"] == "Sales.GMV"
    assert client.get("/api/metadata/glossaries/terms", params={"glossary": "Sales"}).json()["terms"][0]["usage_count"] == 1
    task = client.post(
        "/api/metadata/threads",
        json={"thread_type": "Task", "about_fqn": ORDERS, "about_type": "table", "task_type": "RequestDescription", "message": "请补充描述", "suggestion": "订单主表", "assignees": ["lattice"]},
    ).json()
    assert task["task"]["status"] == "Open"
    assert client.post(f"/api/metadata/threads/{task['id']}/resolve", json={"accept": True}).json()["task"]["status"] == "Closed"
    assert client.get("/api/metadata/entity", params={"entity_type": "table", "ref": ORDERS}).json()["description"] == "订单主表"


def test_mcp_over_streamable_http(client):
    ingest_sample(client)
    accept = {"Accept": "application/json, text/event-stream"}
    init = client.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}, headers=accept)
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "lattice-metadata"
    session = init.headers["mcp-session-id"]
    headers = {**accept, "Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-06-18"}
    assert client.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers).status_code == 202
    tools = client.post(MCP, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers).json()["result"]["tools"]
    assert "query_datasource" in {tool["name"] for tool in tools}
    counted = client.post(MCP, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "query_datasource", "arguments": {"datasource_id": "local-sample", "sql": "SELECT count(*) AS n FROM t_lattice_customers"}}}, headers=headers).json()["result"]
    assert counted["isError"] is False and counted["structuredContent"]["rows"][0][0] > 0
    refused = client.post(MCP, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "query_datasource", "arguments": {"datasource_id": "local-sample", "sql": "DROP TABLE t_lattice_customers"}}}, headers=headers).json()["result"]
    assert refused["isError"] is True
    assert client.post(MCP, content="{", headers={**headers, "Content-Type": "application/json"}).json()["error"]["code"] == -32700
    ping = {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    assert client.post(MCP, json=ping, headers={**headers, "MCP-Protocol-Version": "1999-01-01"}).status_code == 400
    assert client.post(MCP, json=ping, headers={"Mcp-Session-Id": "gone"}).status_code == 404
    assert client.post(MCP, json=ping, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(MCP, content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.get(MCP).status_code == 405
    assert client.delete(MCP, headers={"Mcp-Session-Id": session}).status_code == 204
    assert client.delete(MCP, headers={"Mcp-Session-Id": session}).status_code == 404
    status = client.get("/api/metadata/context/mcp").json()
    assert status["clients"]["claude_code"] == f"claude mcp add --transport http lattice-metadata http://127.0.0.1:8787{MCP}"
    definitions = client.get("/api/metadata/context/tools").json()
    assert definitions["openai"][0]["type"] == "function" and "input_schema" in definitions["anthropic"][0]


def test_alert_secrets_are_masked_and_kept(client):
    created = client.post("/api/metadata/alerts", json={"name": "hooks", "enabled": False, "destinations": [{"type": "webhook", "url": "http://127.0.0.1:9/h", "secret": "s"}]}).json()
    assert created["destinations"][0]["secret"] == "••••••"
    body = {"ref": "hooks", "description": "改描述", "destinations": [{"type": "webhook", "url": "http://127.0.0.1:9/h", "secret": "••••••"}]}
    assert client.post("/api/metadata/alerts/update", json=body).json()["description"] == "改描述"
    stored = app_module.app.state.metadata_store.get_entity("eventSubscription", fqn="hooks")["json"]
    assert stored["destinations"][0]["secret"] == "s"
    assert client.post("/api/metadata/alerts", json={"name": "bad", "destinations": [{"type": "email"}]}).status_code == 400


def test_import_export_properties_and_insights(client):
    bundle = {"entities": [{"entityType": "databaseService", "name": "om", "fullyQualifiedName": "om", "serviceType": "Snowflake"}]}
    assert client.post("/api/metadata/import", json={"format": "openmetadata", "bundle": bundle}).json()["created"] == 1
    exported = client.get("/api/metadata/export", params={"entity_types": "databaseService"}).json()
    assert [entity["fqn"] for entity in exported["entities"]] == ["om"]
    defined = client.post("/api/metadata/properties", json={"entity_type": "table", "name": "owner_email", "property_type": "string"})
    assert defined.status_code == 200
    assert client.get("/api/metadata/properties", params={"entity_type": "table"}).json()["items"][0]["name"] == "owner_email"
    today = dt.date.today()
    kpi = {
        "entity_type": "kpi", "name": "desc", "display_name": "描述覆盖率",
        "fields": {"chart": "percentage_of_entities_with_description_by_type", "target_value": 60, "start_date": (today - dt.timedelta(days=1)).isoformat(), "end_date": (today + dt.timedelta(days=30)).isoformat()},
    }
    assert client.post("/api/metadata/entities", json=kpi).status_code == 200
    insights = client.get("/api/metadata/insights", params={"days": 30}).json()
    assert insights["kpis"][0]["name"] == "desc" and insights["period"]["days"] == 30
    assert client.get("/api/metadata/insights", params={"days": 365}).status_code == 422


def test_content_security_policy_allows_https_icons(client):
    policy = client.get("/api/metadata/health").headers["content-security-policy"]
    assert "img-src 'self' data: https:;" in policy and "script-src 'self'" in policy
