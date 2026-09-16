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

"""Connector guards, registry masking, LLM settings, ingestion and API routes."""

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import sqlglot
import httpx
import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi import llm as llm_module
from webapi.connectors import (
    CONNECTORS,
    SECRET_MASK,
    ConnectorError,
    DuckDBConnector,
    MySQLConnector,
    guard_sql,
    mask_config,
    merge_secrets,
    referenced_tables,
    wrap_limit,
)
from webapi.datasources import DataSourceError, DataSourceRegistry
from webapi.ingest import IngestError, IngestService
from webapi.llm import LlmError, LlmSettings, SqlGenerator, extract_json, normalize_plan
from webapi.polaris import PolarisClient
from webapi.query import QueryStore
from webapi.specs import SpecRegistry

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def duck_file(tmp_path):
    path = tmp_path / "analytics.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE sales(region VARCHAR, amount DECIMAL(10,2), sold DATE)")
        con.execute(
            "INSERT INTO sales VALUES ('华东', 10.5, DATE '2024-01-01'), ('华南', 20.25, DATE '2024-02-01'), ('华北', NULL, NULL)"
        )
        con.execute("CREATE VIEW big_sales AS SELECT * FROM sales WHERE amount > 15")
    return path


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "web").mkdir()
    QueryStore(tmp_path / "web")
    return DataSourceRegistry(tmp_path / "web", tmp_path / "polaris", tmp_path / "engines")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(
            app_module.app.state.polaris,
            "status",
            lambda: {"status": "online", "version": "1.7.0"},
        )
        yield value


# ----- SQL guard ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "dialect, sql",
    [
        ("mysql", "DROP TABLE orders"),
        ("mysql", "SELECT 1; SELECT 2"),
        ("mysql", "INSERT INTO t VALUES (1)"),
        ("mysql", "UPDATE t SET a = 1"),
        ("mysql", "SELECT * FROM t INTO OUTFILE '/tmp/x'"),
        ("postgres", "COPY t TO '/tmp/x'"),
        ("postgres", "SELECT * INTO new_table FROM t"),
        ("clickhouse", "ALTER TABLE t DELETE WHERE 1"),
        ("clickhouse", "TRUNCATE TABLE t"),
        ("hive", "LOAD DATA INPATH '/x' INTO TABLE t"),
        ("starrocks", "CREATE TABLE t (a INT)"),
        ("doris", "DELETE FROM t WHERE a = 1"),
        ("duckdb", "ATTACH '/tmp/out.db'"),
        ("duckdb", ""),
        ("mysql", "SELECT * FROM t FOR UPDATE"),
    ],
)
def test_guard_rejects_non_readonly_statements(dialect, sql):
    with pytest.raises(ConnectorError):
        guard_sql(sql, dialect)


@pytest.mark.parametrize(
    "dialect, sql",
    [
        ("mysql", "SELECT a, count(*) FROM db.t GROUP BY a ORDER BY 2 DESC LIMIT 10"),
        ("clickhouse", "SELECT toStartOfMonth(d) AS m, sum(x) FROM t GROUP BY m"),
        ("hive", "WITH x AS (SELECT * FROM t) SELECT * FROM x"),
        ("postgres", "SELECT date_trunc('month', d) AS m, sum(v) FROM s.t GROUP BY 1"),
        ("starrocks", "SELECT * FROM t WHERE a IN (SELECT a FROM u)"),
    ],
)
def test_guard_accepts_readonly_queries_and_reports_tables(dialect, sql):
    tree = guard_sql(sql, dialect)
    assert referenced_tables(tree)


def test_wrap_limit_keeps_original_text_and_bounds_rows():
    wrapped = wrap_limit("SELECT a FROM t -- trailing comment\n;", 5)
    assert wrapped.startswith("SELECT * FROM (\nSELECT a FROM t -- trailing comment")
    assert wrapped.endswith(") AS lattice_q LIMIT 5")


def test_referenced_tables_ignores_ctes_and_rejects_three_part_names():
    tree = guard_sql("WITH c AS (SELECT * FROM demo.t) SELECT * FROM c JOIN u ON 1=1", "duckdb")
    assert set(referenced_tables(tree)) == {(None, "u"), ("demo", "t")}
    with pytest.raises(ConnectorError):
        referenced_tables(guard_sql("SELECT * FROM a.b.c", "duckdb"))


# ----- DuckDB connector --------------------------------------------------------------
def test_duckdb_connector_reads_metadata_and_bounded_results(duck_file):
    connector = DuckDBConnector({"path": str(duck_file)})
    result = connector.test()
    assert result["ok"] and result["status"] == "online" and result["table_count"] == 1
    assert [item["name"] for item in connector.schemas()] == ["main"]
    tables = connector.tables("main")
    assert {(item["name"], item["kind"]) for item in tables} == {("sales", "table"), ("big_sales", "view")}
    detail = connector.table("main", "sales")
    assert [column["name"] for column in detail["columns"]] == ["region", "amount", "sold"]
    assert detail["rows"] == 3
    preview = connector.preview("main", "sales", 2)
    assert len(preview["rows"]) == 2 and preview["truncated"] is True
    assert preview["rows"][0] == ["华东", 10.5, "2024-01-01"]
    query = connector.query("SELECT region, sum(amount) AS total FROM sales GROUP BY region ORDER BY region")
    assert query["rows"] == [["华东", 10.5], ["华北", None], ["华南", 20.25]]
    assert query["truncated"] is False


def test_duckdb_connector_rejects_files_and_mutations(duck_file):
    connector = DuckDBConnector({"path": str(duck_file)})
    with pytest.raises(ConnectorError):
        connector.query("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(ConnectorError):
        connector.query("DELETE FROM sales")
    with pytest.raises(ConnectorError):
        DuckDBConnector({"path": "relative.duckdb"})
    with pytest.raises(ConnectorError):
        DuckDBConnector({"path": str(duck_file), "unexpected": 1})


def test_connector_normalization_validates_ports_and_required_fields():
    with pytest.raises(ConnectorError, match="端口"):
        MySQLConnector({"host": "h", "port": 70000, "user": "u"})
    with pytest.raises(ConnectorError, match="主机"):
        MySQLConnector({"port": 3306, "user": "u"})
    connector = MySQLConnector({"host": "h", "port": "3307", "user": "u", "ssl": "true"})
    assert connector.config["port"] == 3307 and connector.config["ssl"] is True
    assert connector.summary() == "h:3307"
    assert connector.qualified("d", "t") == "`d`.`t`"


def test_all_connector_types_expose_descriptors():
    assert set(CONNECTORS) == {
        "duckdb", "mysql", "postgresql", "clickhouse", "starrocks", "doris", "hive", "iceberg", "paimon",
        "oracle", "mssql", "mongodb", "elasticsearch", "kafka",
    }
    for connector in CONNECTORS.values():
        descriptor = connector.descriptor()
        assert descriptor["label"] and descriptor["driver"] and descriptor["fields"]
        assert all(field["type"] in {"text", "number", "password", "select", "checkbox", "textarea"} for field in descriptor["fields"])


def test_mask_and_merge_secrets():
    masked = mask_config("mysql", {"host": "h", "password": "secret"})
    assert masked == {"host": "h", "password": SECRET_MASK}
    assert merge_secrets("mysql", masked, {"password": "secret"})["password"] == "secret"
    assert merge_secrets("mysql", {"host": "h"}, {"password": "secret"})["password"] == "secret"
    assert merge_secrets("mysql", {"password": SECRET_MASK}, None)["password"] == ""
    assert merge_secrets("mysql", {"password": "new"}, {"password": "secret"})["password"] == "new"


# ----- registry -----------------------------------------------------------------------
def test_registry_lists_builtin_sample_and_masks_user_secrets(registry, duck_file):
    listing = registry.list()
    assert [item["id"] for item in listing["items"]] == ["local-sample"]
    assert len(listing["types"]) == 14
    created = registry.create("分析库", "duckdb", {"path": str(duck_file)}, "测试")
    assert created["id"].startswith("ds_") and created["summary"] == str(duck_file)
    mysql = registry.create("仓库", "mysql", {"host": "127.0.0.1", "port": 1, "user": "u", "password": "p"})
    assert mysql["config"]["password"] == SECRET_MASK
    stored = json.loads(registry.path.read_text())
    assert stored["sources"][mysql["id"]]["config"]["password"] == "p"
    assert registry.path.stat().st_mode & 0o777 == 0o600
    updated = registry.update(mysql["id"], {"name": "仓库2", "config": {**mysql["config"], "database": "d"}})
    assert updated["name"] == "仓库2" and updated["config"]["database"] == "d"
    assert json.loads(registry.path.read_text())["sources"][mysql["id"]]["config"]["password"] == "p"
    # A builtin source can be removed from the list; it is hidden rather than
    # forgotten, because it is rebuilt from the runtime files on every read.
    registry.delete("local-sample")
    assert "local-sample" not in {item["id"] for item in registry.list()["items"]}
    registry.restore()
    assert "local-sample" in {item["id"] for item in registry.list()["items"]}
    with pytest.raises(DataSourceError):
        registry.update("local-sample", {"name": "x"})
    registry.delete(mysql["id"])
    assert mysql["id"] not in {item["id"] for item in registry.list()["items"]}


def test_registry_test_reports_unreachable_engines_without_raising(registry):
    result = registry.test_config("mysql", {"host": "127.0.0.1", "port": 1, "user": "u"})
    assert result["ok"] is False and result["status"] == "offline" and result["detail"]
    result = registry.test_config("postgresql", {"host": "127.0.0.1", "port": 1, "user": "u", "database": "d"})
    assert result["ok"] is False
    with pytest.raises(DataSourceError):
        registry.test_config("sqlite", {})


def test_registry_seeds_builtin_sources_from_private_runtime_files(tmp_path, registry):
    engines = tmp_path / "engines"
    engines.mkdir()
    (engines / "mysql.json").write_text(
        json.dumps({"type": "mysql", "host": "127.0.0.1", "port": 33306, "user": "lattice", "password": "s", "database": "lattice_demo"})
    )
    polaris = tmp_path / "polaris"
    polaris.mkdir()
    (polaris / "credentials.json").write_text(
        json.dumps({"client_id": "id", "client_secret": "zzhiddenzz", "realm": "LATTICE", "base_url": "http://127.0.0.1:8181"})
    )
    (polaris / "local-s3.json").write_text(json.dumps({"endpoint": "http://127.0.0.1:19000", "access_key": "a", "secret_key": "b", "region": "us-east-1"}))
    items = {item["id"]: item for item in registry.list()["items"]}
    assert items["mysql-local"]["builtin"] and items["mysql-local"]["config"]["password"] == SECRET_MASK
    assert items["mysql-local"]["summary"] == "127.0.0.1:33306 / lattice_demo"
    iceberg = items["iceberg-local"]
    assert iceberg["config"]["credential"] == SECRET_MASK
    assert "zzhiddenzz" not in json.dumps(registry.list())
    record = registry.record("iceberg-local")
    assert record["config"]["credential"] == "id:zzhiddenzz"
    assert "Polaris-Realm: LATTICE" in record["config"]["extra_headers"]


def test_registry_test_persists_last_result(registry, duck_file):
    created = registry.create("分析库", "duckdb", {"path": str(duck_file)})
    result = registry.test(created["id"])
    assert result["ok"] is True
    assert registry.get(created["id"])["last_test"]["status"] == "online"
    context = registry.schema_context(created["id"])
    assert "main.sales" in context and "amount" in context


# ----- LLM settings and SQL generation -----------------------------------------------------
def test_llm_settings_mask_key_and_validate(tmp_path):
    settings = LlmSettings(tmp_path / "llm.json")
    assert settings.status()["configured"] is False
    with pytest.raises(LlmError):
        settings.save({"provider": "openai", "model": "m"})
    status = settings.save({"provider": "openai", "model": "deepseek-chat", "base_url": "https://api.example.com/v1", "api_key": "sk-1"})
    assert status["configured"] is True and status["api_key_masked"] == SECRET_MASK
    assert (tmp_path / "llm.json").stat().st_mode & 0o777 == 0o600
    settings.save({"provider": "openai", "api_key": SECRET_MASK, "model": "deepseek-chat", "base_url": "https://api.example.com/v1"})
    assert settings.load()["api_key"] == "sk-1"
    assert settings.save({"provider": "anthropic", "api_key": "k"})["model"] == "claude-opus-5"
    assert settings.save({"provider": "none"})["configured"] is False


def test_extract_json_and_normalize_plan():
    plan = normalize_plan(extract_json('```json\n{"title": "T", "sql": "SELECT 1;", "steps": ["a"], "dimension": "", "metric": "", "chart_type": "line"}\n```'))
    assert plan["sql"] == "SELECT 1" and plan["chart_type"] == "line"
    with pytest.raises(LlmError):
        extract_json("no json here")
    with pytest.raises(LlmError):
        normalize_plan({"title": "x"})


def test_openai_compatible_generation_uses_configured_endpoint(tmp_path, monkeypatch):
    settings = LlmSettings(tmp_path / "llm.json")
    settings.save({"provider": "openai", "model": "qwen", "base_url": "http://127.0.0.1:11434/v1", "api_key": "key"})
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert body["model"] == "qwen" and request.headers["Authorization"] == "Bearer key"
        content = json.dumps({"title": "月度", "sql": "SELECT 1 AS x", "steps": ["s1"], "dimension": "", "metric": "x", "chart_type": "bar"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    class FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("trust_env", None)
            super().__init__(transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k == "timeout"})

    monkeypatch.setattr(llm_module.httpx, "Client", FakeClient)
    plan = SqlGenerator(settings).generate("月度销售", "MySQL SQL", "仓库", "- t: a INT")
    assert plan["sql"] == "SELECT 1 AS x" and plan["steps"] == ["s1"]
    assert seen[0].url.path == "/v1/chat/completions"
    assert "key" not in json.dumps(plan)


def test_anthropic_generation_uses_official_sdk(tmp_path, monkeypatch):
    settings = LlmSettings(tmp_path / "llm.json")
    settings.save({"provider": "anthropic", "api_key": "sk-ant", "model": "claude-opus-5"})
    calls = []

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            text = json.dumps({"title": "T", "sql": "SELECT 2 AS y", "steps": [], "dimension": "", "metric": "", "chart_type": "table"})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    class FakeAnthropic:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "sk-ant"
            self.messages = FakeMessages()
            self.beta = SimpleNamespace(messages=FakeMessages())

    monkeypatch.setattr(llm_module, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic, AuthenticationError=Exception, PermissionDeniedError=Exception, NotFoundError=Exception, RateLimitError=Exception, APIStatusError=Exception, APIConnectionError=Exception), raising=False)
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", llm_module.anthropic)
    plan = SqlGenerator(settings).generate("问题", "DuckDB SQL", "示例", "- t: a INT")
    assert plan["sql"] == "SELECT 2 AS y"
    assert calls[0]["model"] == "claude-opus-5"
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert calls[0]["fallbacks"] == "default"


# ----- ingestion ---------------------------------------------------------------------------
@pytest.fixture
def ingest(tmp_path, registry):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"client_id": "root", "client_secret": "secret", "realm": "LATTICE", "base_url": "http://127.0.0.1:8181"}))
    polaris = PolarisClient(credentials, SpecRegistry(ROOT / "integrations" / "polaris" / "spec"))
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/oauth/tokens"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 300})
        if path.endswith("/namespaces/demo") and request.method == "GET":
            return httpx.Response(200, json={"namespace": ["demo"], "properties": {}})
        if path.endswith("/generic-tables") and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={"table": body})
        if path.endswith("/generic-tables/model_1"):
            return httpx.Response(200, json={"table": {"name": "model_1", "format": "lattice", "properties": {}}})
        if path.endswith("/generic-tables/ext_1") and request.method == "GET":
            return httpx.Response(200, json={"table": {"name": "ext_1", "format": "duckdb", "properties": {"lattice.source-datasource": "local-sample"}}})
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/namespaces"):
            return httpx.Response(200, json={"namespaces": [["demo"]]})
        if path.endswith("/tables"):
            return httpx.Response(200, json={"identifiers": [{"namespace": ["demo"], "name": "ice_1"}]})
        if path.endswith("/generic-tables"):
            return httpx.Response(200, json={"identifiers": [{"namespace": ["demo"], "name": "ext_1"}]})
        return httpx.Response(404, json={"error": {"type": "NotFound"}})

    polaris._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    return IngestService(polaris, registry), requests


def test_ingest_registers_generic_table_without_secrets(ingest):
    service, requests = ingest
    result = service.register("local-sample", "main", "t_lattice_orders")
    assert result["kind"] == "generic" and result["format"] == "duckdb" and result["name"] == "t_lattice_orders"
    create = next(r for r in requests if r.method == "POST" and r.url.path.endswith("/generic-tables"))
    body = json.loads(create.content)
    assert body["format"] == "duckdb" and body["properties"]["lattice.source-table"] == "t_lattice_orders"
    assert "order_id" in body["properties"]["lattice.columns"]
    listing = service.catalog("lattice")
    kinds = {(item["name"], item["kind"]) for item in listing["tables"]}
    assert kinds == {("ice_1", "iceberg"), ("ext_1", "generic")}
    with pytest.raises(IngestError, match="语义模型"):
        service.unregister("lattice", "demo", "model_1")
    service.unregister("lattice", "demo", "ext_1")
    assert requests[-1].method == "DELETE"
    with pytest.raises(IngestError):
        service.register("local-sample", "main", "missing_table")
    with pytest.raises(IngestError):
        service.register("local-sample", "main", "t_lattice_orders", table_name="bad name")



def test_listing_keeps_what_the_query_link_needs(tmp_path, registry):
    """A registered Generic Table gets a "在 SQL 工作台查询" link, and the page
    builds that query from the source schema and table recorded on the entry.
    The listing drops only the bulky column and document payloads, so those two
    must survive; losing them would leave the row without a usable link."""
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"client_id": "root", "client_secret": "secret",
                                       "realm": "LATTICE", "base_url": "http://127.0.0.1:8181"}))
    polaris = PolarisClient(credentials, SpecRegistry(ROOT / "integrations" / "polaris" / "spec"))
    stored = {
        "lattice.source-datasource": "quality-postgres",
        "lattice.source-schema": "public",
        "lattice.source-table": "admin_logs",
        "lattice.columns": json.dumps([{"name": "id"}]),
        "lattice.document": "x" * 500,
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/oauth/tokens"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 300})
        if path.endswith("/namespaces"):
            return httpx.Response(200, json={"namespaces": [["demo"]]})
        if path.endswith("/tables"):
            return httpx.Response(200, json={"identifiers": []})
        if path.endswith("/generic-tables"):
            return httpx.Response(200, json={"identifiers": [{"namespace": ["demo"], "name": "admin_logs"}]})
        if path.endswith("/generic-tables/admin_logs"):
            return httpx.Response(200, json={"table": {"name": "admin_logs", "format": "postgresql",
                                                       "properties": stored}})
        return httpx.Response(404, json={"error": {"type": "NotFound"}})

    polaris._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    entry = IngestService(polaris, registry).catalog("lattice")["tables"][0]
    assert entry["kind"] == "generic"
    assert entry["source_datasource"] == "quality-postgres"
    properties = entry["properties"]
    assert properties["lattice.source-schema"] == "public"
    assert properties["lattice.source-table"] == "admin_logs"
    # The two payloads that would bloat the listing are the only ones dropped.
    assert "lattice.columns" not in properties and "lattice.document" not in properties


def test_ingest_page_lets_a_registered_table_be_queried():
    """A registered row used to offer only 取消注册, so the table the page had
    just registered was a dead end.  A Generic Table's rows stay in the origin
    engine, so its query must run against that data source, not the Iceberg
    catalog the neighbouring rows use."""
    text = (ROOT / "web" / "src" / "IngestionPage.tsx").read_text()
    body = text.split("function origin(", 1)[1].split("\n  }", 1)[0]
    # The link is offered only for a source that is still registered, since a
    # removed or hidden one cannot answer the query.
    assert "sources.find(" in body
    assert '"lattice.source-schema"' in body and '"lattice.source-table"' in body

    query = text.split("function queryOrigin(", 1)[1].split("\n  }", 1)[0]
    assert "tableReference(" in query, "the origin dialect quotes the identifiers"
    assert "target.source.id" in query and "ICEBERG_SOURCE" not in query

    actions = text.split("<th>操作</th>", 1)[1]
    generic = actions.split('item.kind === "iceberg"', 1)[1].split(") : (", 1)[1]
    assert "queryOrigin(item)" in generic, "a registered table needs its query link"
    assert "unregister(item)" in generic, "and must still be removable"


def test_health_reports_a_build_that_changes_with_the_bundle(monkeypatch, tmp_path):
    """A single-page app never re-requests its entry document while the tab stays
    open, so a rebuild alone does not reach it.  Health carries a build id the
    page compares against the one it started on; it must follow the entry
    document, whose name for the hashed bundles changes on every build."""
    static = tmp_path / "dist"
    static.mkdir()
    monkeypatch.setattr(app_module, "STATIC", static)

    # Nothing built yet: no id, and so nothing for a page to compare against.
    assert app_module.build_id() == ""

    static.joinpath("index.html").write_text('<script src="/assets/index-AAA.js">')
    first = app_module.build_id()
    assert first and app_module.build_id() == first, "a stable build keeps its id"

    static.joinpath("index.html").write_text('<script src="/assets/index-BBB.js">')
    assert app_module.build_id() != first, "a rebuilt bundle must be noticed"

# ----- API routes ---------------------------------------------------------------------------
def test_api_datasource_lifecycle_and_queries(client, duck_file):
    listing = client.get("/api/datasources").json()
    assert [item["id"] for item in listing["items"]] == ["local-sample"] and len(listing["types"]) == 14
    created = client.post("/api/datasources", json={"name": "分析库", "type": "duckdb", "config": {"path": str(duck_file)}}).json()
    source_id = created["id"]
    assert client.post(f"/api/datasources/{source_id}/test", json={}).json()["ok"] is True
    assert client.get(f"/api/datasources/{source_id}/schemas").json()["items"][0]["name"] == "main"
    tables = client.get(f"/api/datasources/{source_id}/tables", params={"schema": "main"}).json()
    assert {item["name"] for item in tables["items"]} == {"sales", "big_sales"}
    detail = client.get(f"/api/datasources/{source_id}/table", params={"schema": "main", "name": "sales"}).json()
    assert detail["rows"] == 3
    preview = client.post(f"/api/datasources/{source_id}/preview", json={"schema": "main", "name": "sales", "limit": 1}).json()
    assert len(preview["rows"]) == 1 and preview["datasource_id"] == source_id
    query = client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT count(*) AS n FROM sales"}).json()
    assert query["rows"] == [[3]] and query["provider"] == "sql" and query["dialect"] == "duckdb"
    assert client.post(f"/api/datasources/{source_id}/query", json={"sql": "DROP TABLE sales"}).status_code == 400
    assert client.get("/api/history").json()["items"][0]["id"] == query["id"]
    via_query = client.post("/api/query", json={"sql": "SELECT 1 AS one", "datasource_id": source_id}).json()
    assert via_query["rows"] == [[1]]
    assert client.post("/api/datasources/local-sample/delete", json={}).status_code == 200
    client.post("/api/datasources/restore", json={})
    assert client.post(f"/api/datasources/{source_id}/delete", json={}).json() == {"deleted": True}
    assert client.get(f"/api/datasources/{source_id}").status_code == 404
    assert client.post("/api/query", json={"question": "月度趋势", "datasource_id": "missing"}).status_code == 404


def test_api_query_stream_emits_steps_sql_and_result(client):
    with client.stream("POST", "/api/query/stream", json={"question": "月度销售额趋势"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())
    events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
    assert events[:2] == ["step", "step"] and "sql" in events and events[-2:] == ["result", "done"]
    result = json.loads(next(line for line in text.splitlines() if line.startswith("data: {\"id\"")).split(": ", 1)[1])
    assert result["provider"] == "rules" and len(result["rows"]) == 11 and result["datasource_id"] == "local-sample"
    with client.stream("POST", "/api/query/stream", json={"question": "无法识别的问题"}) as response:
        text = "".join(response.iter_text())
    assert "event: error" in text and "event: done" in text


def test_api_question_outside_the_builtin_rules_asks_for_a_model(client, duck_file):
    """A question no built-in rule recognises needs a model, whatever the source."""
    created = client.post("/api/datasources", json={"name": "分析库", "type": "duckdb", "config": {"path": str(duck_file)}}).json()
    response = client.post("/api/query", json={"question": "各区域销售额", "datasource_id": created["id"]})
    assert response.status_code == 400 and "模型" in response.json()["detail"]


def test_builtin_rules_are_translated_for_the_selected_engine():
    """Every local engine holds the same six sample tables, so a recognised
    question answers on any of them without a model; only the dialect changes."""
    from webapi.questions import QueryService

    _, sql = QueryStore.translate("月度销售额趋势")
    assert QueryService.localize(sql, "duckdb", "DuckDB SQL") == sql
    mysql = QueryService.localize(sql, "mysql", "MySQL SQL")
    assert "DATE_FORMAT" in mysql
    # MySQL rejects an ORDER BY expression that is not in the GROUP BY under
    # ONLY_FULL_GROUP_BY, so the translated statement must order by the alias.
    assert "CASE WHEN" not in mysql
    assert "ORDER BY sales_month" in mysql
    for dialect in ("postgres", "clickhouse", "hive", "starrocks", "doris"):
        translated = QueryService.localize(sql, dialect, dialect)
        assert translated.lower().startswith("select")
        assert len(sqlglot.parse(translated, read=dialect)) == 1


def test_api_llm_settings_and_bootstrap(client):
    assert client.get("/api/llm").json()["configured"] is False
    saved = client.post("/api/llm", json={"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}).json()
    assert saved["api_key_masked"] == SECRET_MASK and saved["configured"] is True
    assert client.post("/api/llm/test", json={}).status_code == 400
    boot = client.get("/api/bootstrap").json()
    assert boot["llm"]["configured"] is True and boot["datasources"]["count"] == 1
    assert boot["links"]["webapi"] == "127.0.0.1:8787"
    assert any("StarRocks" in item for item in boot["capabilities"])
    assert client.post("/api/llm", json={"provider": "none"}).json()["configured"] is False


def test_iceberg_table_reads_the_row_count_without_materialising_the_summary():
    """pyiceberg's Summary is a Mapping whose __iter__ yields (key, value) pairs,
    so dict(summary) feeds a tuple back into __getitem__ and raises
    AttributeError: 'tuple' object has no attribute 'lower'. The connector must
    read the key directly."""
    from webapi.connectors import IcebergConnector

    class FakeSummary:
        """Reproduces pyiceberg's Mapping-over-pydantic behaviour."""

        def __init__(self, values):
            self._values = values

        def __getitem__(self, key):
            if not isinstance(key, str):
                raise AttributeError(f"'{type(key).__name__}' object has no attribute 'lower'")
            return self._values.get(key)

        def get(self, key, default=None):
            value = self[key]
            return default if value is None else value

        def __iter__(self):
            return iter(self._values.items())  # pairs, not keys

        def __len__(self):
            return len(self._values)

    class FakeField:
        def __init__(self, name):
            self.name, self.field_type, self.required, self.doc = name, "string", False, ""

    class FakeSchema:
        fields = [FakeField("customer_id")]

    class FakeTable:
        format_version = 2

        def current_snapshot(self):
            return SimpleNamespace(summary=FakeSummary({"total-records": "60"}))

        def schema(self):
            return FakeSchema()

        def location(self):
            return "s3://bucket/demo"

    connector = IcebergConnector.__new__(IcebergConnector)
    connector._run = lambda action: action(SimpleNamespace(load_table=lambda _: FakeTable()))
    connector.split_namespace = staticmethod(lambda value: (value,))
    detail = connector.table("demo", "t_lattice_customers")
    assert detail["rows"] == 60
    assert [column["name"] for column in detail["columns"]] == ["customer_id"]


def test_model_sql_in_the_wrong_dialect_is_repaired_once(monkeypatch):
    """A model is told the target dialect but is not bound by it; one wrong
    function name otherwise makes the whole question fail on that engine."""
    from webapi.questions import QueryError, QueryService

    calls = []

    class Service(QueryService):
        def __init__(self):  # bypass the real stores; only run_generated is under test
            pass

        def execute(self, record, sql, limit=None, refresh=False):
            calls.append(sql)
            if "strftime" in sql:
                raise QueryError(400, "function strftime(date, unknown) does not exist")
            return {"rows": [["2026-01", 1.0]], "columns": ["m", "v"], "truncated": False}

    duck = "SELECT strftime(order_date, '%Y-%m') AS m, count(*) AS v FROM t_lattice_orders GROUP BY 1"
    result, repaired = Service().run_generated({}, duck, "postgres", "PostgreSQL SQL")
    assert result["rows"] == [["2026-01", 1.0]]
    assert "TO_CHAR" in repaired, "the retry must carry the translated statement"
    assert len(calls) == 2, "the repair runs only after a real failure"

    # A DuckDB target has nothing to translate to, so the first error stands.
    with pytest.raises(QueryError):
        Service().run_generated({}, duck, "duckdb", "DuckDB SQL")

    # When the translation also fails, the original error is what the user sees.
    class AlwaysFails(Service):
        def execute(self, record, sql, limit=None, refresh=False):
            raise QueryError(400, "原始错误")

    with pytest.raises(QueryError, match="原始错误"):
        AlwaysFails().run_generated({}, duck, "mysql", "MySQL SQL")


def test_builtin_source_is_hidden_not_forgotten(client):
    """A builtin source is rebuilt from the runtime files on every read, so a
    deletion has to be remembered or the card reappears on the next start."""
    listed = lambda: {item["id"] for item in client.get("/api/datasources").json()["items"]}
    assert "local-sample" in listed()

    removed = client.post("/api/datasources/local-sample/delete", json={})
    assert removed.status_code == 200
    assert "local-sample" not in listed()

    # A fresh registry over the same directory must still hide it.
    reopened = DataSourceRegistry(
        app_module.RUNTIME, app_module.POLARIS_RUNTIME, app_module.ENGINES_RUNTIME
    )
    assert "local-sample" not in {item["id"] for item in reopened.list()["items"]}

    restored = client.post("/api/datasources/restore", json={})
    assert restored.status_code == 200
    assert restored.json()["restored"] == ["local-sample"]
    assert "local-sample" in listed()
    assert client.post("/api/datasources/restore", json={}).json()["restored"] == []


def test_dropdown_label_is_the_engine_unless_two_sources_share_it():
    """The option text drops the source name, which only repeats the engine, and
    stays unique so two sources on one engine never collapse into one entry."""
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "web" / "src" / "SourceBrowser.tsx"
    text = source.read_text()
    assert "export function sourceLabel" in text
    body = text.split("export function sourceLabel", 1)[1].split("\n}", 1)[0]
    # The label starts from the engine, never from the source's own name.
    assert "source.type_label" in body
    assert re.search(r"siblings\.length\s*<\s*2", body), "a unique engine needs no suffix"
    assert "source.name" in body, "identical engines still need telling apart"


def test_only_one_postgresql_source_is_registered(tmp_path, registry):
    """Two PostgreSQL entries only forced the reader to tell them apart; the real
    business database is the one that is listed."""
    engines = tmp_path / "engines"
    engines.mkdir(exist_ok=True)
    (engines / "postgres.json").write_text(
        json.dumps({"type": "postgresql", "host": "127.0.0.1", "port": 55432,
                    "user": "u", "password": "p", "database": "lattice_demo"})
    )
    (engines / "quality-datasource.json").write_text(
        json.dumps({"type": "postgresql", "host": "localhost", "port": 5432,
                    "user": "postgres", "password": "postgres", "database": "blog_converter"})
    )
    fresh = DataSourceRegistry(tmp_path / "web", tmp_path / "polaris", engines)
    postgres = [
        item for item in fresh.builtin_records().values() if item["type"] == "postgresql"
    ]
    assert len(postgres) == 1, "a second PostgreSQL entry is what the reader complained about"
    assert postgres[0]["name"] == "PostgreSQL"
    assert postgres[0]["config"]["database"] == "blog_converter"


def test_builtin_rule_explains_a_source_without_the_sample_tables():
    """The engine's own 'relation does not exist' text read as a platform fault."""
    from webapi.questions import QueryError, QueryService

    class Registry:
        def schema_context(self, source_id):
            return "- public.articles，约 56 行：id uuid, title character varying"

    service = QueryService.__new__(QueryService)
    service.registry = Registry()
    record = {"id": "quality-postgres"}
    sql = "SELECT count(*) FROM t_lattice_order_items"
    with pytest.raises(QueryError) as info:
        service.require_sample_tables(record, sql, "PostgreSQL")
    assert info.value.status_code == 400
    assert "t_lattice_order_items" in str(info.value)
    assert "SQL 工作台" in str(info.value)

    # A source that does hold the tables passes straight through.
    class Seeded(Registry):
        def schema_context(self, source_id):
            return "- lattice_demo.t_lattice_order_items，约 1100 行：order_id int"

    service.registry = Seeded()
    service.require_sample_tables(record, sql, "MySQL")


def test_api_repeated_queries_are_served_from_the_cache_until_invalidated(client, duck_file):
    created = client.post("/api/datasources", json={"name": "缓存库", "type": "duckdb", "config": {"path": str(duck_file)}}).json()
    source_id = created["id"]
    first = client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT count(*) AS n FROM sales"}).json()
    assert first["cached"] is False and first["cache_age_seconds"] is None
    second = client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT count(*) AS n  FROM sales;"}).json()
    assert second["cached"] is True and second["rows"] == [[3]] and second["id"] != first["id"]
    assert "命中查询缓存" in second["steps"][-1]
    fresh = client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT count(*) AS n FROM sales", "refresh": True}).json()
    assert fresh["cached"] is False
    via_query = client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM sales", "datasource_id": source_id}).json()
    assert via_query["cached"] is True
    listing = client.get("/api/query/cache", params={"datasource_id": source_id}).json()
    assert listing["stats"]["entries"] == 1 and listing["items"][0]["tables"] == ["sales"] and listing["stats"]["hits"] == 2
    assert client.post("/api/query/cache/invalidate", json={"table": "sales"}).json()["removed"] == 1
    assert client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT count(*) AS n FROM sales"}).json()["cached"] is False
    client.post(f"/api/datasources/{source_id}/update", json={"description": "改了描述"})
    assert client.get("/api/query/cache").json()["stats"]["entries"] == 0
    settings = client.post("/api/query/cache/settings", json={"ttl_seconds": 120, "max_entries": 50}).json()
    assert (settings["ttl_seconds"], settings["max_entries"]) == (120, 50)
    assert client.post("/api/query/cache/settings", json={"ttl_seconds": 1}).status_code == 422
    assert client.post("/api/query/cache/settings", json={"enabled": False}).json()["enabled"] is False
    assert client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT 1 AS one"}).json()["cached"] is False
    assert client.post(f"/api/datasources/{source_id}/query", json={"sql": "SELECT 1 AS one"}).json()["cached"] is False
    assert "查询结果缓存" in " ".join(client.get("/api/bootstrap").json()["capabilities"])
