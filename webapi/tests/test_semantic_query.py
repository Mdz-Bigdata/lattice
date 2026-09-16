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

"""The semantic-layer query engine: parsing, SQL compilation, the registry and its routes."""

import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.semantic_query import Compiler, SemanticQueryEngine, SemanticQueryError, extract_document, parse_model

MODEL = {
    "name": "sales",
    "description": "销售模型",
    "datasets": [
        {
            "name": "orders",
            "source": "shop.public.orders",
            "primary_key": ["order_id"],
            "fields": [
                {"name": "order_id", "datatype": "Integer", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]}},
                {"name": "customer_id", "datatype": "Integer", "expression": "customer_id"},
                {"name": "amount", "datatype": "Decimal", "expression": "amount"},
                {"name": "order_date", "datatype": "Date", "expression": "order_date", "dimension": {"is_time": True}},
                {"name": "status", "datatype": "String", "expression": "status", "ai_context": {"synonyms": ["订单状态"]}},
                {"name": "net_amount", "datatype": "Decimal", "expression": "amount - discount"},
            ],
        },
        {
            "name": "customers",
            "source": "shop.public.customers",
            "primary_key": ["customer_id"],
            "fields": [
                {"name": "customer_id", "datatype": "Integer", "expression": "customer_id"},
                {"name": "region", "datatype": "String", "expression": "region"},
            ],
        },
        {"name": "islands", "source": "shop.public.islands", "fields": [{"name": "island", "datatype": "String", "expression": "island"}]},
    ],
    "relationships": [{"name": "orders_to_customers", "from": "orders", "to": "customers", "from_columns": ["customer_id"], "to_columns": ["customer_id"]}],
    "metrics": [
        {"name": "total_amount", "datatype": "Decimal", "description": "销售额", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.amount)"}]}, "ai_context": {"synonyms": ["GMV"]}},
        {"name": "order_count", "datatype": "Integer", "expression": "COUNT(DISTINCT orders.order_id)"},
        {"name": "aov", "datatype": "Decimal", "expression": "SUM(orders.net_amount) / COUNT(DISTINCT customers.customer_id)"},
        {"name": "bare", "datatype": "Decimal", "expression": "SUM(amount)"},
    ],
}


@pytest.fixture
def model():
    return parse_model(MODEL, model_id="test", storage="document")


def test_parse_reads_datasets_relationships_metrics_and_dimension_roles(model):
    assert list(model.datasets) == ["orders", "customers", "islands"]
    orders = model.datasets["orders"]
    assert orders.fields["order_date"].is_time and orders.fields["order_date"].is_dimension
    assert orders.fields["amount"].is_dimension is False and orders.fields["status"].is_dimension
    assert orders.fields["status"].synonyms == ["订单状态"]
    assert model.relationships[0].to_dataset == "customers"
    assert model.metrics["total_amount"].synonyms == ["GMV"] and model.metrics["bare"].expressions == {"ANSI_SQL": "SUM(amount)"}
    assert [item["field"] for item in model.dimensions()] == ["orders.order_date", "orders.status", "customers.region", "islands.island"]
    with pytest.raises(SemanticQueryError, match="name"):
        parse_model({"datasets": []})
    assert extract_document({"document": {"semantic_model": '{"name": "m", "datasets": []}'}})["name"] == "m"
    assert extract_document("semantic_model:\n  - name: y\n    datasets: []\n")["name"] == "y"
    assert extract_document({"yaml": "semantic_model:\n  - name: z\n    datasets: []\n"})["name"] == "z"
    assert extract_document("not: a model") is None


def test_compile_joins_groups_and_aggregates_in_the_target_dialect(model):
    compiled = Compiler(model, "duckdb").compile({"metrics": ["total_amount", "order_count"], "dimensions": [{"field": "customers.region"}], "filters": [{"field": "orders.status", "op": "=", "value": "paid"}]})
    sql = compiled.sql
    assert '"customers"."region" AS "region"' in sql and 'SUM("orders"."amount") AS "total_amount"' in sql
    assert 'FROM "public"."orders" AS "orders"' in sql
    assert 'LEFT JOIN "public"."customers" AS "customers"' in sql and '"orders"."customer_id" = "customers"."customer_id"' in sql
    assert "WHERE\n  \"orders\".\"status\" = 'paid'" in sql and 'GROUP BY\n  "customers"."region"' in sql
    assert 'ORDER BY\n  "total_amount" DESC' in sql
    assert compiled.datasets == ["orders", "customers"] and compiled.joins == ["orders_to_customers: orders → customers"]
    assert [column["kind"] for column in compiled.columns] == ["dimension", "metric", "metric"]
    assert compiled.chart == {"dimension": "region", "metric": "total_amount", "type": "bar"}
    mysql = Compiler(model, "mysql").compile({"metrics": ["total_amount"], "dimensions": ["orders.status"]})
    assert "`orders`.`status` AS `status`" in mysql.sql and "`public`.`orders` AS `orders`" in mysql.sql


def test_time_grains_expressions_having_and_bare_fields(model):
    compiled = Compiler(model, "duckdb").compile({
        "metrics": ["aov", "bare"],
        "dimensions": [{"field": "order_date", "grain": "month"}],
        "filters": [{"field": "aov", "op": ">", "value": 10}, {"field": "orders.amount", "op": "between", "value": [1, 100]}, {"field": "customers.region", "op": "in", "value": "华东,华南"}, {"field": "orders.status", "op": "not_null"}],
        "order_by": [{"field": "order_date", "desc": True}],
        "limit": 12,
    })
    sql = compiled.sql
    assert 'DATE_TRUNC(\'MONTH\', "orders"."order_date") AS "order_date"' in sql
    assert '("orders"."amount" - "orders"."discount") AS' not in sql  # a field expression is inlined into the metric
    assert 'SUM("orders"."amount" - "orders"."discount") / COUNT(DISTINCT "customers"."customer_id") AS "aov"' in sql
    assert 'SUM("orders"."amount") AS "bare"' in sql  # a bare column resolves through the only dataset that has it
    assert '"orders"."amount" BETWEEN 1 AND 100' in sql and "\"customers\".\"region\" IN ('华东', '华南')" in sql
    assert 'NOT "orders"."status" IS NULL' in sql and "HAVING" in sql and 'ORDER BY\n  "order_date" DESC' in sql and "LIMIT 12" in sql
    assert compiled.chart["type"] == "line"
    clickhouse = Compiler(model, "clickhouse").compile({"metrics": ["total_amount"], "dimensions": [{"field": "orders.order_date", "grain": "year"}]})
    assert "order_date" in clickhouse.sql and any(marker in clickhouse.sql for marker in ("dateTrunc", "toStartOfYear", "DATE_TRUNC"))


@pytest.mark.parametrize(
    "request_body, message",
    [
        ({"metrics": []}, "至少选择一个指标"),
        ({"metrics": ["nope"]}, "没有指标 nope"),
        ({"metrics": ["total_amount"], "dimensions": ["islands.island"]}, "没有定义关系"),
        ({"metrics": ["total_amount"], "dimensions": [{"field": "orders.status", "grain": "month"}]}, "不是时间维度"),
        ({"metrics": ["total_amount"], "dimensions": [{"field": "orders.order_date", "grain": "hour"}]}, "时间粒度"),
        ({"metrics": ["total_amount"], "dimensions": ["customer_id"]}, "多个数据集"),
        ({"metrics": ["total_amount"], "dimensions": ["orders.nope"]}, "没有字段 nope"),
        ({"metrics": ["total_amount"], "filters": [{"field": "orders.status", "op": "~"}]}, "比较方式"),
        ({"metrics": ["total_amount"], "order_by": [{"field": "region"}]}, "排序字段"),
    ],
)
def test_compile_refuses_requests_the_model_cannot_answer(model, request_body, message):
    with pytest.raises(SemanticQueryError, match=message):
        Compiler(model, "duckdb").compile(request_body)


def test_sources_may_be_queries_and_bare_schemas_are_dropped_for_the_sample(model):
    model.datasets["orders"].source = "SELECT * FROM raw.orders WHERE deleted = 0"
    compiled = Compiler(model, "duckdb", bare_schemas=("main",)).compile({"metrics": ["total_amount"]})
    assert 'FROM (\n  SELECT\n    *\n  FROM "raw"."orders"\n  WHERE\n    "deleted" = 0\n) AS "orders"' in compiled.sql or "FROM (" in compiled.sql
    model.datasets["orders"].source = "local.main.orders"
    assert 'FROM "orders" AS "orders"' in Compiler(model, "duckdb", bare_schemas=("main",)).compile({"metrics": ["total_amount"]}).sql
    assert "FROM `lattice_demo`.`orders` AS `orders`" in Compiler(model, "mysql", schema_map={"main": "lattice_demo"}).compile({"metrics": ["total_amount"]}).sql
    model.datasets["orders"].source = ""
    with pytest.raises(SemanticQueryError, match="source"):
        Compiler(model, "duckdb").compile({"metrics": ["total_amount"]})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_engine_reads_every_store_and_caches_models():
    class Store:
        calls = 0

        def list(self):
            return {"items": [{"id": "m1", "name": "sales-store"}]}

        def get(self, model_id):
            Store.calls += 1
            return {"yaml": "semantic_model:\n  - name: sales-store\n    datasets:\n      - name: t\n        source: s.t\n        fields: [{name: a, datatype: Integer, expression: a}]\n    metrics:\n      - name: m\n        expression: SUM(t.a)\n"}

    class Native:
        def catalog_listing(self, catalog, namespace):
            return {"items": [{"name": "native-one"}]}

        def load(self, catalog, namespace, name):
            return {"status": 200, "body": {"document": {"semantic_model": {"name": name, "datasets": [{"name": "d", "source": "x.d", "fields": []}], "metrics": [{"name": "n", "expression": "COUNT(1)", "description": "计数"}]}}}}

    clock = Clock()
    engine = SemanticQueryEngine(models=Store(), semantic=Native(), builtin=lambda: MODEL, clock=clock)
    listing = engine.list_models()
    assert [(item["id"], item["storage"]) for item in listing] == [("builtin", "builtin"), ("store:m1", "lattice-model-store"), ("native:native-one", "polaris-native")]
    catalog = engine.catalog()
    assert [metric["name"] for metric in catalog["metrics"]] == ["total_amount", "order_count", "aov", "bare", "m", "n"]
    assert catalog["metrics"][0]["datasets"] == ["orders"] and catalog["metrics"][2]["datasets"] == ["customers", "orders"]
    assert engine.catalog("gmv")["metrics"][0]["name"] == "total_amount"
    assert engine.catalog("计数")["metrics"][0]["model"] == "native-one"
    assert engine.load_model("sales-store").storage == "lattice-model-store" and Store.calls == 1
    engine.load_model("store:m1")
    assert Store.calls == 1  # cached
    clock.now += 120
    engine.load_model("store:m1")
    assert Store.calls == 2
    described = engine.describe("builtin")
    assert described["metrics"][0]["synonyms"] == ["GMV"] and described["grains"][0]["value"] == "day"
    with pytest.raises(SemanticQueryError, match="不存在"):
        engine.load_model("missing")


# ----- routes ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        yield value


def test_metrics_api_runs_the_demo_model_on_the_sample_data(client):
    catalog = client.get("/api/metrics/catalog").json()
    names = {metric["name"] for metric in catalog["metrics"]}
    assert {"total_sales", "order_count", "avg_order_value", "total_payment", "customer_count"} <= names
    assert catalog["models"][0]["id"] == "builtin" and catalog["models"][0]["metrics"] >= 6
    described = client.get("/api/metrics/model", params={"ref": "lattice_demo_sales"}).json()
    assert [item["name"] for item in described["relationships"]][:2] == ["items_to_orders", "orders_to_customers"]
    assert any(item["field"] == "t_lattice_orders.order_date" and item["is_time"] for item in described["dimensions"])
    compiled = client.post("/api/metrics/compile", json={"model": "lattice_demo_sales", "metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_customers.region"}]}).json()
    assert compiled["dialect"] == "duckdb" and 'FROM "t_lattice_order_items"' in compiled["sql"] and len(compiled["joins"]) == 2
    answer = client.post("/api/metrics/query", json={
        "model": "lattice_demo_sales",
        "metrics": ["total_sales", "order_count"],
        "dimensions": [{"field": "t_lattice_customers.region"}],
        "filters": [{"field": "t_lattice_orders.status", "op": "not_null"}],
        "limit": 10,
    })
    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["columns"] == ["region", "total_sales", "order_count"] and payload["rows"] and payload["provider"] == "semantic"
    assert payload["chart"]["dimension"] == "region" and payload["semantic"]["datasets"] == ["t_lattice_order_items", "t_lattice_orders", "t_lattice_customers"]
    assert all(row[2] > 0 for row in payload["rows"])
    monthly = client.post("/api/metrics/query", json={"model": "builtin", "metrics": ["total_payment"], "dimensions": [{"field": "t_lattice_orders.order_date", "grain": "month"}]}).json()
    assert monthly["chart"]["type"] == "line" and len(monthly["rows"]) >= 1
    cached = client.post("/api/metrics/query", json={"model": "builtin", "metrics": ["total_payment"], "dimensions": [{"field": "t_lattice_orders.order_date", "grain": "month"}]}).json()
    assert cached["cached"] is True
    assert client.get("/api/history").json()["items"][0]["provider"] == "semantic"
    refused = client.post("/api/metrics/query", json={"model": "builtin", "metrics": ["nope"]})
    assert refused.status_code == 400 and "没有指标" in refused.json()["detail"]
    assert client.post("/api/metrics/refresh", json={}).json()["refreshed"] is True
    tools = {tool["name"] for tool in client.get("/api/metadata/context/tools").json()["mcp"]}
    assert {"list_metrics", "query_metric"} <= tools
    via_tool = client.post("/api/metadata/context/tools/call", json={"name": "query_metric", "arguments": {"model": "builtin", "metrics": ["order_count"], "dimensions": [{"field": "t_lattice_products.category"}]}}).json()
    assert via_tool["result"]["columns"] == ["category", "order_count"] if "result" in via_tool else via_tool["columns"] == ["category", "order_count"]


def test_metric_over_a_fieldless_dataset_reads_raw_columns():
    """A hand-written native model: one dataset without fields, a metric over a physical column."""
    document = {
        "name": "tpcds_retail_model",
        "datasets": [{"name": "store_sales", "source": "public.store_sales", "primary_key": ["ss_item_sk", "ss_ticket_number"]}],
        "metrics": [{"name": "total_sales", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(ss_ext_sales_price)"}]}}],
    }
    compiled = Compiler(parse_model(document), "mysql").compile({"metrics": ["total_sales"]})
    assert "SUM(`store_sales`.`ss_ext_sales_price`) AS `total_sales`" in compiled.sql
    assert "FROM `public`.`store_sales` AS `store_sales`" in compiled.sql
    assert compiled.datasets == ["store_sales"]
    # With several datasets the owner comes from the qualified columns in the same expression.
    document["datasets"].append({"name": "date_dim", "source": "public.date_dim", "fields": [{"name": "d_year", "datatype": "Integer"}]})
    document["metrics"].append({"name": "avg_price", "expression": "SUM(store_sales.ss_ext_sales_price) / COUNT(ss_ticket_number)"})
    document["metrics"].append({"name": "lost", "expression": "SUM(ss_ext_sales_price)"})
    model = parse_model(document)
    assert "COUNT(`store_sales`.`ss_ticket_number`)" in Compiler(model, "mysql").compile({"metrics": ["avg_price"]}).sql
    with pytest.raises(SemanticQueryError, match="没有这个字段"):
        Compiler(model, "mysql").compile({"metrics": ["lost"]})


def test_default_schema_aliases_follow_the_data_source():
    """``public.store_sales`` runs as lattice_demo.store_sales on StarRocks / Doris, as a bare
    table on the DuckDB sample, and stays ``public`` on PostgreSQL where that schema is real."""
    from webapi.semantic_query import DEFAULT_SCHEMA_ALIASES, NATIVE_DEFAULT_SCHEMAS

    document = {
        "name": "tpcds_retail_model",
        "datasets": [{"name": "store_sales", "source": "public.store_sales"}],
        "metrics": [{"name": "total_sales", "expression": "SUM(ss_ext_sales_price)"}],
    }
    model = parse_model(document)
    request = {"metrics": ["total_sales"]}

    class Connector:
        def __init__(self, default):
            self.default = default

        def default_schema(self):
            return self.default

    class Registry:
        def __init__(self, default):
            self.default = default

        def connector(self, source_id):
            return Connector(self.default)

    class Queries:
        def __init__(self, record, default):
            self.record, self.registry = record, Registry(default)

        def _record(self, source_id):
            return self.record

    def compile_on(record, default):
        engine = SemanticQueryEngine(queries=Queries(record, default), builtin=lambda: document)
        return engine.compile("builtin", request, record["id"])["sql"]

    assert "FROM `lattice_demo`.`store_sales`" in compile_on({"id": "starrocks-local", "type": "starrocks"}, "lattice_demo")
    assert "FROM `lattice_demo`.`store_sales`" in compile_on({"id": "doris-local", "type": "doris"}, "lattice_demo")
    assert 'FROM "store_sales" AS "store_sales"' in compile_on({"id": "local-sample", "type": "duckdb"}, "main")
    assert 'FROM "public"."store_sales"' in compile_on({"id": "pg", "type": "postgresql"}, "sales")
    assert 'FROM `store_sales` AS `store_sales`' in compile_on({"id": "mysql-x", "type": "mysql"}, None)
    assert set(NATIVE_DEFAULT_SCHEMAS.values()) <= set(DEFAULT_SCHEMA_ALIASES)
    # Iceberg and Paimon are read through DuckDB, yet ``main`` is not a namespace there:
    # both the Ossie ``public`` and the demo model's ``main`` go to the configured namespace.
    lake = {"name": "m", "datasets": [{"name": "a", "source": "main.t_lattice_orders"}, {"name": "b", "source": "public.store_sales"}], "metrics": [{"name": "n", "expression": "COUNT(a.order_id) + COUNT(b.ss_ticket_number)"}],
            "relationships": [{"name": "r", "from": "a", "to": "b", "from_columns": ["order_id"], "to_columns": ["ss_ticket_number"]}]}
    engine = SemanticQueryEngine(queries=Queries({"id": "iceberg-local", "type": "iceberg"}, "demo"), builtin=lambda: lake)
    sql = engine.compile("builtin", {"metrics": ["n"]}, "iceberg-local")["sql"]
    assert '"demo"."t_lattice_orders"' in sql and '"demo"."store_sales"' in sql
