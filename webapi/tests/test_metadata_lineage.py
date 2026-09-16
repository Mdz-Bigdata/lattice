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

"""Lineage edges, SQL-derived table and column lineage, graphs and query usage."""

import pytest

from webapi.metadata_lineage import LineageService
from webapi.metadata_service import MetadataError, MetadataService
from webapi.metadata_store import MetadataStore, today

SCHEMA = "mysql-local.default.lattice_demo"
ORDERS = f"{SCHEMA}.orders"
CUSTOMERS = f"{SCHEMA}.customers"
SUMMARY = f"{SCHEMA}.order_summary"
COPY = f"{SCHEMA}.orders_copy"


@pytest.fixture
def catalog():
    store = MetadataStore("sqlite:///:memory:")
    store.bootstrap()
    service = MetadataService(store)
    service.seed()
    service.create("databaseService", {"name": "mysql-local", "service_type": "Mysql", "datasource_id": "mysql-local"})
    service.create("database", {"name": "default", "parent_fqn": "mysql-local"})
    service.create("databaseSchema", {"name": "lattice_demo", "parent_fqn": "mysql-local.default"})
    tables = {
        "orders": [("order_id", "int"), ("customer_id", "int"), ("amount", "decimal(10,2)")],
        "customers": [("customer_id", "int"), ("name", "varchar(64)")],
        "order_summary": [("customer_id", "int"), ("name", "varchar(64)"), ("total", "decimal(12,2)")],
        "orders_copy": [("order_id", "int"), ("amount", "decimal(10,2)")],
    }
    for name, columns in tables.items():
        service.create(
            "table",
            {"name": name, "parent_fqn": SCHEMA, "source_schema": "lattice_demo", "columns": [{"name": c, "type": t} for c, t in columns]},
        )
    yield service
    store.close()


@pytest.fixture
def lineage(catalog):
    return LineageService(catalog.store, catalog)


def expect_error(status, operation, *args, **kwargs):
    with pytest.raises(MetadataError) as caught:
        operation(*args, **kwargs)
    assert caught.value.status_code == status
    return str(caught.value)


def test_insert_select_derives_table_and_column_lineage(lineage, catalog):
    result = lineage.from_sql(
        "INSERT INTO lattice_demo.order_summary SELECT c.customer_id, c.name, SUM(o.amount) AS total "
        "FROM lattice_demo.orders o JOIN lattice_demo.customers c ON o.customer_id = c.customer_id GROUP BY 1, 2",
        datasource_id="mysql-local",
        apply=True,
    )
    assert result["kind"] == "query" and result["dialect"] == "mysql"
    assert result["target"]["fqn"] == SUMMARY
    assert sorted(source["fqn"] for source in result["sources"]) == [CUSTOMERS, ORDERS]
    columns = {item["to_column"].rsplit(".", 1)[1]: item for item in result["columns"]}
    assert columns["total"]["from_columns"] == [f"{ORDERS}.amount"]
    assert "SUM(" in columns["total"]["function"]
    assert columns["name"]["from_columns"] == [f"{CUSTOMERS}.name"]
    edges = {edge["from_fqn"]: edge for edge in catalog.store.edges(to_fqn=SUMMARY)}
    assert set(edges) == {ORDERS, CUSTOMERS} and result["applied"] == 2
    assert edges[ORDERS]["source"] == "query"
    assert [item["to_column"] for item in edges[ORDERS]["columns"]] == [f"{SUMMARY}.total"]
    assert "从 SQL 解析出 2 条血缘" in catalog.feed(entity_fqn=SUMMARY)["items"][0]["summary"]


def test_star_projection_maps_matching_columns(lineage):
    result = lineage.from_sql("INSERT INTO lattice_demo.orders_copy SELECT * FROM lattice_demo.orders", datasource_id="mysql-local")
    assert sorted(item["to_column"] for item in result["columns"]) == [f"{COPY}.amount", f"{COPY}.order_id"]
    assert result["applied"] == 0


def test_ctas_view_and_plain_select(lineage, catalog):
    ctas = lineage.from_sql("CREATE TABLE lattice_demo.order_summary AS SELECT customer_id FROM lattice_demo.orders", datasource_id="mysql-local")
    assert ctas["target"]["fqn"] == SUMMARY and [s["fqn"] for s in ctas["sources"]] == [ORDERS]
    view = lineage.from_sql("CREATE VIEW lattice_demo.v_orders AS SELECT * FROM orders", datasource_id="mysql-local")
    assert view["kind"] == "view" and view["target"] is None and view["target_reference"] == "lattice_demo.v_orders"
    plain = lineage.from_sql("SELECT order_id FROM orders", datasource_id="mysql-local", target=COPY)
    assert plain["kind"] == "select" and plain["target"]["fqn"] == COPY
    assert plain["columns"] == [{"from_columns": [f"{ORDERS}.order_id"], "to_column": f"{COPY}.order_id", "function": ""}]
    assert catalog.store.edges() == []


def test_unresolved_and_ambiguous_references(lineage, catalog):
    catalog.create("databaseSchema", {"name": "archive", "parent_fqn": "mysql-local.default"})
    catalog.create("table", {"name": "orders", "parent_fqn": "mysql-local.default.archive", "source_schema": "archive", "columns": [{"name": "order_id", "type": "int"}]})
    lineage.invalidate()
    result = lineage.from_sql("SELECT * FROM lattice_demo.missing JOIN orders ON 1 = 1", datasource_id="mysql-local", target=SUMMARY)
    assert result["unresolved"] == ["lattice_demo.missing"]
    assert [source["fqn"] for source in result["sources"]] == [ORDERS]  # the default schema wins
    archived = lineage.from_sql("SELECT * FROM archive.orders", datasource_id="mysql-local", target=SUMMARY)
    assert [source["fqn"] for source in archived["sources"]] == ["mysql-local.default.archive.orders"]
    expect_error(400, lineage.from_sql, "SELECT FROM WHERE", datasource_id="mysql-local")
    expect_error(400, lineage.from_sql, "SELECT 1; SELECT 2")
    expect_error(400, lineage.from_sql, "   ")


def test_manual_edges_are_validated(lineage, catalog):
    expect_error(400, lineage.add_edge, {"from_fqn": ORDERS, "to_fqn": ORDERS})
    catalog.create("glossary", {"name": "Sales"})
    assert "数据资产" in expect_error(400, lineage.add_edge, {"from_fqn": "Sales", "to_fqn": ORDERS, "from_type": "glossary"})
    expect_error(404, lineage.add_edge, {"from_fqn": ORDERS, "to_fqn": SUMMARY, "columns": [{"from_columns": ["nope"], "to_column": "total"}]})
    edge = lineage.add_edge({"from_fqn": ORDERS, "to_fqn": SUMMARY, "description": "汇总", "columns": [{"from_columns": ["AMOUNT"], "to_column": "total", "function": "SUM"}]})
    assert edge["columns"] == [{"from_columns": [f"{ORDERS}.amount"], "to_column": f"{SUMMARY}.total", "function": "SUM"}]
    catalog.delete("table", CUSTOMERS)
    assert "已删除" in expect_error(400, lineage.add_edge, {"from_fqn": CUSTOMERS, "to_fqn": SUMMARY})


def test_graph_walks_depth_and_impact_counts_downstream(lineage, catalog):
    catalog.create("semanticModel", {"name": "sales_model", "description": "销售语义模型"})
    lineage.add_edge({"from_fqn": ORDERS, "to_fqn": SUMMARY})
    lineage.add_edge({"from_fqn": SUMMARY, "to_fqn": COPY})
    lineage.add_edge({"from_fqn": COPY, "to_fqn": "sales_model", "to_type": "semanticModel"})
    graph = lineage.graph("table", SUMMARY, upstream_depth=1, downstream_depth=1)
    assert {(node["fqn"], node["depth"]) for node in graph["nodes"]} == {(ORDERS, -1), (SUMMARY, 0), (COPY, 1)}
    assert len(graph["edges"]) == 2 and graph["upstream_count"] == 1 and graph["downstream_count"] == 1
    impact = lineage.impact("table", ORDERS)
    assert impact["total"] == 3
    assert {item["entity_type"]: item["count"] for item in impact["by_type"]} == {"table": 2, "semanticModel": 1}
    expect_error(400, lineage.graph, "table", ORDERS, upstream_depth=9)


def test_removing_an_edge_is_recorded(lineage, catalog):
    lineage.add_edge({"from_fqn": ORDERS, "to_fqn": SUMMARY})
    assert lineage.remove_edge(ORDERS, SUMMARY)["deleted"]
    assert catalog.store.edges() == []
    expect_error(404, lineage.remove_edge, ORDERS, SUMMARY)
    assert catalog.feed(entity_fqn=SUMMARY)["items"][0]["summary"].startswith("删除血缘")


def test_executed_queries_are_attributed_once(lineage, catalog):
    payload = {"id": "q1", "sql": "SELECT * FROM lattice_demo.orders o JOIN customers c ON o.customer_id = c.customer_id", "datasource_id": "mysql-local", "dialect": "mysql"}
    assert lineage.record_query(payload) == 2
    assert lineage.record_query(payload) == 2
    assert catalog.store.usage(ORDERS, today())[0]["queries"] == 1
    assert lineage.record_query({**payload, "id": "q2"}) == 2
    assert lineage.usage("table", ORDERS)["queries"] == 2
    assert lineage.queries_for("table", ORDERS)["total"] == 2
    assert lineage.record_query({**payload, "datasource_id": "unknown"}) == 0
    assert lineage.record_query({**payload, "id": "q3", "sql": "SELEC nonsense"}) == 0


def test_view_definitions_become_view_lineage(lineage, catalog):
    catalog.update("table", COPY, {"table_type": "View", "view_definition": "SELECT order_id, amount FROM lattice_demo.orders WHERE amount > 0"})
    summary = lineage.sync_views("mysql-local")
    assert summary["views"] == 1 and summary["edges"] == 1
    edge = catalog.store.edges(to_fqn=COPY)[0]
    assert edge["source"] == "view" and edge["from_fqn"] == ORDERS and len(edge["columns"]) == 2


def test_qualified_names_found_in_several_services_stay_unresolved(lineage, catalog):
    catalog.create("databaseService", {"name": "pg", "service_type": "Postgres", "datasource_id": "pg"})
    catalog.create("database", {"name": "default", "parent_fqn": "pg"})
    catalog.create("databaseSchema", {"name": "lattice_demo", "parent_fqn": "pg.default"})
    catalog.create("table", {"name": "orders", "parent_fqn": "pg.default.lattice_demo", "source_schema": "lattice_demo", "columns": [{"name": "order_id", "type": "int"}]})
    lineage.invalidate()
    assert lineage.find_table_anywhere("lattice_demo", "orders") is None
    assert lineage.find_table("mysql-local", "lattice_demo", "orders")["fqn"] == ORDERS
