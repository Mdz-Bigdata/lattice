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

"""Metadata ingestion from real (DuckDB) data sources, Polaris, the scheduler and semantic models."""

import json

import duckdb
import pytest

from webapi.datasources import DataSourceRegistry
from webapi.metadata_ingest import BOT_USER, IngestionError, MetadataIngestor, MetadataScheduler
from webapi.metadata_insights import InsightsService
from webapi.metadata_lineage import LineageService
from webapi.metadata_service import MetadataService
from webapi.connectors import ConnectorError, DuckDBConnector
from webapi.metadata_store import MetadataStore, MetadataStoreError, today
from webapi.query import QueryStore


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "analytics.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE sales(region VARCHAR, amount DECIMAL(10,2), sold DATE)")
        con.execute("INSERT INTO sales VALUES ('华东', 10.5, DATE '2024-01-01'), ('华南', 20.25, DATE '2024-02-01')")
        con.execute("COMMENT ON TABLE sales IS '销售明细'")
        con.execute("CREATE VIEW big_sales AS SELECT region, amount FROM sales WHERE amount > 15")
    return path


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "web").mkdir()
    QueryStore(tmp_path / "web")
    return DataSourceRegistry(tmp_path / "web", tmp_path / "polaris", tmp_path / "engines")


@pytest.fixture
def env(registry, warehouse):
    store = MetadataStore("sqlite:///:memory:")
    store.bootstrap()
    service = MetadataService(store)
    service.seed()
    lineage = LineageService(store, service)
    source = registry.create("分析库", "duckdb", {"path": str(warehouse)})
    ingestor = MetadataIngestor(service, lineage, registry)
    yield service, ingestor, source
    store.close()


def table_fqn(source, name):
    return f"{source['id']}.analytics.main.{name}"


def test_each_data_source_is_bound_to_one_service(env):
    service, ingestor, source = env
    first = ingestor.sync_datasources()
    assert first["created"] == 2  # the builtin sample and the DuckDB file
    assert ingestor.sync_datasources()["created"] == 0
    row = service.row("databaseService", source["id"])
    assert row["service_type"] == "DuckDB" and row["display_name"] == "分析库"
    assert row["json"]["ingestion"]["enabled"] and row["json"]["ingestion"]["interval_minutes"] == 1440
    assert service.feed(entity_fqn=source["id"])["items"][0]["user_name"] == BOT_USER
    service.delete("databaseService", source["id"], hard=True)
    assert ingestor.sync_datasources()["created"] == 0  # a removed service stays removed
    assert ingestor.sync_datasources(force=True)["created"] == 1


def test_crawl_writes_tables_columns_comments_and_view_lineage(env):
    service, ingestor, source = env
    ingestor.sync_datasources()
    run = ingestor.run(source["id"])
    assert run["status"] == "success", run
    assert run["summary"]["tables"] == 2 and run["summary"]["view_edges"] == 1
    sales = service.get("table", table_fqn(source, "sales"))
    assert sales["description"] == "销售明细" and sales["row_count"] == 2
    assert [column["name"] for column in sales["columns"]] == ["region", "amount", "sold"]
    assert sales["columns"][1]["data_type"] == "DECIMAL"
    view = service.get("table", table_fqn(source, "big_sales"))
    assert view["table_type"] == "View" and "amount > 15" in view["view_definition"]
    edge = service.store.edges(to_fqn=view["fqn"])[0]
    assert edge["from_fqn"] == sales["fqn"] and edge["source"] == "view" and len(edge["columns"]) == 2
    listing = ingestor.runs(service_fqn=source["id"])
    assert listing["total"] == 1 and listing["items"][0]["service"]["fqn"] == source["id"]


def test_recrawl_keeps_human_text_and_marks_dropped_tables(env, warehouse):
    service, ingestor, source = env
    ingestor.sync_datasources()
    ingestor.run(source["id"])
    service.update("table", table_fqn(source, "sales"), {"description": "人工维护的描述", "columns": [{"name": "region", "description": "销售大区"}]})
    with duckdb.connect(str(warehouse)) as con:
        con.execute("DROP VIEW big_sales")
        con.execute("ALTER TABLE sales ADD COLUMN channel VARCHAR")
        con.execute("CREATE TABLE refunds(id INTEGER)")
    run = ingestor.run(source["id"])
    assert run["summary"]["deleted"] == 1 and run["summary"]["created"] == 1
    sales = service.get("table", table_fqn(source, "sales"))
    assert sales["description"] == "人工维护的描述"
    assert next(c for c in sales["columns"] if c["name"] == "region")["description"] == "销售大区"
    assert sales["columns"][-1]["name"] == "channel"
    assert service.get("table", table_fqn(source, "big_sales"))["deleted"]
    assert not service.get("table", table_fqn(source, "refunds"))["deleted"]


def test_runs_refuse_unsupported_and_orphaned_services(env, registry):
    service, ingestor, source = env
    ingestor.sync_datasources()
    service.create("messagingService", {"name": "kafka", "service_type": "Kafka"})
    with pytest.raises(IngestionError, match="暂不支持自动拾取"):
        ingestor.run("kafka")
    registry.delete(source["id"])
    with pytest.raises(IngestionError, match="已不存在"):
        ingestor.run(source["id"])


def test_service_wizard_rejects_secrets_and_mismatched_sources(env):
    service, ingestor, source = env
    with pytest.raises(IngestionError, match="机密"):
        ingestor.create_service({"category": "messaging", "service_type": "Kafka", "name": "kafka", "connection": {"bootstrap_servers": "k:9092", "sasl_password": "x"}})
    with pytest.raises(IngestionError, match="不一致"):
        ingestor.create_service({"category": "database", "service_type": "Mysql", "name": "wrong", "datasource_id": source["id"]})
    with pytest.raises(IngestionError, match="不支持连接器"):
        ingestor.create_service({"category": "messaging", "service_type": "Tableau", "name": "t"})
    bound = ingestor.create_service({"category": "database", "service_type": "DuckDB", "name": "warehouse", "datasource_id": source["id"]})
    assert bound["service_type"] == "DuckDB" and bound["datasource_id"] == source["id"]
    assert ingestor.sync_datasources()["created"] == 1  # only the sample; the file is bound already
    kafka = ingestor.create_service({"category": "messaging", "service_type": "Kafka", "name": "kafka", "connection": {"bootstrap_servers": "k:9092"}})
    assert kafka["entity_type"] == "messagingService" and kafka["connection"]["bootstrap_servers"] == "k:9092"
    items = {item["fqn"]: item for item in ingestor.services()}
    assert items["warehouse"]["automated"] and not items["kafka"]["automated"]


def test_scheduler_runs_due_services_once_and_snapshots_the_day(env):
    service, ingestor, source = env
    insights = InsightsService(service.store, service)
    scheduler = MetadataScheduler(ingestor, insights, interval=0.1)
    assert sorted(scheduler.tick()) == sorted(["local-sample", source["id"]])
    assert scheduler.tick() == []
    assert service.store.get_snapshot(today()) is not None
    assert service.list("table")["total"] == 9  # seven sample tables, one table and one view


class FakeModels:
    def list(self):
        return {"items": [{"id": "m1", "name": "销售模型"}], "catalog": "lattice", "namespace": "demo"}

    def get(self, model_id):
        return {
            "id": model_id,
            "yaml": (
                "version: 0.2.0.dev0\n"
                "semantic_model:\n"
                "  - name: sales_model\n"
                "    description: 销售语义模型\n"
                "    datasets:\n"
                "      - name: sales\n"
                "        source: analytics.main.sales\n"
                "        fields:\n"
                "          - name: amount\n"
                "            datatype: Decimal\n"
                "    metrics:\n"
                "      - name: total_sales\n"
                "        expression:\n"
                "          dialects:\n"
                "            - dialect: ANSI_SQL\n"
                "              expression: SUM(sales.amount)\n"
            ),
        }


def test_semantic_models_become_assets_with_lineage(env):
    service, ingestor, source = env
    ingestor.sync_datasources()
    ingestor.run(source["id"])
    ingestor.models = FakeModels()
    summary = ingestor.sync_semantic_models()
    assert summary["semantic_models"] == 1 and summary["lineage_edges"] == 1
    model = service.get("semanticModel", "sales_model")
    assert model["description"] == "销售语义模型"
    assert model["metrics"][0]["expression"] == "SUM(sales.amount)"
    assert model["datasets"][0]["fields"][0]["name"] == "amount"
    assert service.store.edges(to_fqn="sales_model")[0]["from_fqn"] == table_fqn(source, "sales")


class FakePolaris:
    """Answers the Polaris operations the crawler uses, like PolarisClient.request does."""

    def __init__(self, source_id):
        self.source_id = source_id
        self.fail: set[str] = set()

    def status(self):
        return {"status": "online", "version": "1.7.0"}

    def request(self, spec, operation, path_params=None, query=None, body=None, headers=None, token=None):
        if operation in self.fail:
            return {"status": 500, "body": {}}
        if operation == "listCatalogs":
            return {"status": 200, "body": {"catalogs": [{"name": "lattice"}]}}
        if operation == "listNamespaces":
            return {"status": 200, "body": {"namespaces": [["demo"]]}}
        if operation == "listTables":
            return {"status": 200, "body": {"identifiers": [{"namespace": ["demo"], "name": "orders"}]}}
        if operation == "loadTable":
            schema = {"schema-id": 0, "fields": [{"id": 1, "name": "order_id", "type": "long", "required": True}, {"id": 2, "name": "status", "type": "string", "required": False, "doc": "订单状态"}]}
            return {"status": 200, "body": {"metadata": {"current-schema-id": 0, "schemas": [schema], "location": "s3://lattice-warehouse/lattice/demo/orders", "properties": {}}}}
        if operation == "listGenericTables":
            return {"status": 200, "body": {"identifiers": [{"namespace": ["demo"], "name": "sales_registered"}]}}
        if operation == "loadGenericTable":
            properties = {
                "lattice.source-datasource": self.source_id,
                "lattice.source-schema": "main",
                "lattice.source-table": "sales",
                "lattice.columns": json.dumps([{"name": "region", "type": "VARCHAR"}, {"name": "amount", "type": "DECIMAL(10,2)"}]),
                "lattice.row-count": "2",
            }
            return {"status": 200, "body": {"table": {"name": "sales_registered", "format": "duckdb", "doc": "登记的销售表", "properties": properties}}}
        return {"status": 404, "body": {}}


def test_polaris_crawl_links_generic_tables_to_their_origin(env):
    service, ingestor, source = env
    ingestor.sync_datasources()
    ingestor.run(source["id"])
    ingestor.polaris = FakePolaris(source["id"])
    ingestor.sync_datasources()
    run = ingestor.run("polaris")
    assert run["status"] == "success", run
    iceberg = service.get("table", "polaris.lattice.demo.orders")
    assert iceberg["table_type"] == "Iceberg" and [column["name"] for column in iceberg["columns"]] == ["order_id", "status"]
    assert iceberg["columns"][0]["constraint"] == "NOT_NULL" and iceberg["columns"][1]["description"] == "订单状态"
    assert iceberg["polaris"]["kind"] == "iceberg"
    registered = service.get("table", "polaris.lattice.demo.sales_registered")
    assert registered["table_type"] == "External" and registered["row_count"] == 2
    edge = service.store.edges(to_fqn=registered["fqn"])[0]
    assert edge["from_fqn"] == table_fqn(source, "sales") and edge["source"] == "polaris" and len(edge["columns"]) == 2


def test_transient_read_errors_never_delete_catalog_tables(env, monkeypatch):
    service, ingestor, source = env
    ingestor.sync_datasources()
    ingestor.run(source["id"])
    original = DuckDBConnector.table

    def flaky(self, schema, name):
        if name == "big_sales":
            raise ConnectorError("timeout")
        return original(self, schema, name)

    monkeypatch.setattr(DuckDBConnector, "table", flaky)
    run = ingestor.run(source["id"])
    assert run["status"] == "partial" and run["summary"]["deleted"] == 0
    assert not service.get("table", table_fqn(source, "big_sales"))["deleted"]


def test_failed_polaris_listings_keep_their_tables(env):
    service, ingestor, source = env
    ingestor.polaris = FakePolaris(source["id"])
    ingestor.sync_datasources()
    assert ingestor.run("polaris")["status"] == "success"
    ingestor.polaris.fail = {"listTables", "loadGenericTable"}
    run = ingestor.run("polaris")
    assert run["status"] == "partial" and run["summary"]["deleted"] == 0
    assert not service.get("table", "polaris.lattice.demo.orders")["deleted"]
    assert not service.get("table", "polaris.lattice.demo.sales_registered")["deleted"]


def test_a_run_that_cannot_start_is_not_left_running(env, monkeypatch):
    service, ingestor, source = env
    ingestor.sync_datasources()

    def broken(run):
        raise MetadataStoreError(503, "元数据库连接失败：测试")

    monkeypatch.setattr(service.store, "insert_run", broken)
    with pytest.raises(MetadataStoreError):
        ingestor.run(source["id"])
    assert not ingestor.is_running(source["id"])
