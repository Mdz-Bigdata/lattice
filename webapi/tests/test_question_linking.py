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
"""What a model sees for free SQL, what it is held to, and what people confirm."""

import duckdb
import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.connectors import make_connector
from webapi.llm import SqlGenerator
from webapi.schema_linking import INFERRED, JoinEdge, QuestionScope, TableInfo, join_candidates, link_schema, singular_stem
from webapi.semantic_query import SemanticQueryEngine, source_tables
from webapi.sql_audit import audit_sql, value_probe_sql


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    for name in ("LATTICE_LLM_PROVIDER", "LATTICE_LLM_API_KEY", "LATTICE_LLM_MODEL", "LATTICE_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as test_client:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        assert test_client.post("/api/llm", json={"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}).status_code == 200
        yield test_client


def warehouse(path, *, keys=True):
    """Orders fanning out to items, a dozen unrelated tables, and columns to keep away from models."""
    reference = "REFERENCES customers(customer_id)" if keys else ""
    order_reference = "REFERENCES orders(order_id)" if keys else ""
    primary = "PRIMARY KEY" if keys else ""
    with duckdb.connect(str(path)) as con:
        con.execute(f"CREATE TABLE customers(customer_id INTEGER {primary}, region VARCHAR, phone VARCHAR)")
        con.execute("INSERT INTO customers VALUES (1, '华东', '13800000000'), (2, '华南', '13900000000')")
        con.execute(f"CREATE TABLE orders(order_id INTEGER {primary}, customer_id INTEGER {reference}, status VARCHAR, amount DECIMAL(10,2))")
        con.execute("INSERT INTO orders VALUES (1, 1, '已完成', 100), (2, 1, '已取消', 50), (3, 2, '已完成', 70)")
        con.execute(f"CREATE TABLE items(item_id INTEGER, order_id INTEGER {order_reference}, price DECIMAL(10,2))")
        con.execute("INSERT INTO items VALUES (1, 1, 60), (2, 1, 40), (3, 2, 50), (4, 3, 70)")
        for index in range(12):
            con.execute(f"CREATE TABLE audit_log_{index}(event_time TIMESTAMP, payload VARCHAR)")
    return path


def source(client, path):
    created = client.post("/api/datasources", json={"name": "仓库", "type": "duckdb", "config": {"path": str(path)}})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def generator_replies(monkeypatch, statements):
    calls = []

    def fake(self, question, dialect_label, source_label, schema_text):
        calls.append({"question": question, "schema": schema_text})
        sql = statements[min(len(calls) - 1, len(statements) - 1)]
        return {"title": "结果", "sql": sql, "steps": ["思路"], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", fake)
    return calls


def ask(client, question, source_id):
    return client.post("/api/query", json={"question": question, "datasource_id": source_id})


GOOD = "SELECT c.region, SUM(o.amount) AS amount FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.region"


# ----- schema linking -----------------------------------------------------------------------
def pick_tables(monkeypatch, tables):
    menus = []

    def fake(self, prompt, *, system, schema):
        menus.append(prompt)
        return {"tables": tables}

    monkeypatch.setattr(SqlGenerator, "complete_json", fake)
    return menus


def test_prompt_holds_the_relevant_tables_their_joins_and_real_values(client, tmp_path, monkeypatch):
    # No word of this question appears in an English schema: the model picks the fact table,
    # and the join graph brings in the dimension holding 地区.
    menus = pick_tables(monkeypatch, ["orders"])
    calls = generator_replies(monkeypatch, [GOOD])
    answer = ask(client, "各地区订单金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200, answer.text
    schema = calls[0]["schema"]
    for expected in ("orders", "customers", "orders(customer_id) → customers(customer_id)", "来源：外键", "已完成"):
        assert expected in schema
    # The unrelated tables are named, in case they matter, but their columns stay out.
    assert "audit_log_3" in schema and "payload" not in schema and "audit_log_3" in menus[0]
    # A phone number is low-cardinality here, and still never goes to the model.
    assert "13800000000" not in schema


def test_without_any_lead_nothing_is_pruned(client, tmp_path, monkeypatch):
    """The model is unreachable and no word matches: an arbitrary few could hide the right table."""
    calls = generator_replies(monkeypatch, [GOOD])
    assert ask(client, "各地区订单金额", source(client, warehouse(tmp_path / "w.duckdb"))).status_code == 200
    assert "region" in calls[0]["schema"] and "payload" in calls[0]["schema"]


def test_step_reports_how_the_schema_was_narrowed(client, tmp_path, monkeypatch):
    pick_tables(monkeypatch, ["orders"])
    generator_replies(monkeypatch, [GOOD])
    with client.stream("POST", "/api/query/stream", json={"question": "各地区订单金额", "datasource_id": source(client, warehouse(tmp_path / "w.duckdb"))}) as response:
        text = "".join(response.iter_text())
    assert "Schema Linking" in text and "15 张表" in text


def test_tables_connecting_two_matches_are_added():
    table = lambda name, *columns: TableInfo(None, name, columns=[{"name": column, "type": "INT"} for column in columns])
    tables = [table("orders_fact", "order_id", "bridge_id"), table("bridge", "bridge_id", "loc_id"), table("city", "loc_id", "city_name"), *[table(f"noise_{i}", "a") for i in range(12)]]
    edges = [JoinEdge("orders_fact", ["bridge_id"], "bridge", ["bridge_id"], "外键"), JoinEdge("bridge", ["loc_id"], "city", ["loc_id"], "外键")]
    link = link_schema("每个 city 的 fact", tables, edges)
    assert {item.name for item in link.tables} == {"orders_fact", "bridge", "city"} and "bridge" in link.connectors and len(link.edges) == 2


def test_a_small_schema_is_shown_whole():
    tables = [TableInfo(None, f"t{i}", columns=[{"name": "a", "type": "INT"}]) for i in range(5)]
    link = link_schema("完全无关的问题", tables, [])
    assert len(link.tables) == 5 and link.others == []


def test_foreign_keys_are_read_from_the_database(tmp_path):
    connector = make_connector("duckdb", {"path": str(warehouse(tmp_path / "w.duckdb"))})
    keys = connector.foreign_keys("main")
    assert {"table": "orders", "columns": ["customer_id"], "referenced_table": "customers", "referenced_columns": ["customer_id"]} in keys


def test_joins_without_declared_keys_are_inferred_only_when_the_data_agrees(client, tmp_path, monkeypatch):
    path = warehouse(tmp_path / "w.duckdb", keys=False)
    with duckdb.connect(str(path)) as con:
        # Named like a key of orders, but its values match nothing there.
        con.execute("CREATE TABLE shipments(shipment_id INTEGER, order_id INTEGER)")
        con.execute("INSERT INTO shipments VALUES (1, 900), (2, 901)")
    source_id = source(client, path)
    queries = client.app.state.queries
    _, edges, _ = queries._index_schema(queries._record(source_id))
    described = {edge.text(): edge.source for edge in edges}
    assert described["orders(customer_id) → customers(customer_id)"] == INFERRED
    assert described["items(order_id) → orders(order_id)"] == INFERRED
    assert not any("shipments" in text for text in described)


def test_sibling_fact_tables_are_not_candidates_for_a_join():
    table = lambda name, *columns: TableInfo(None, name, columns=[{"name": column, "type": "INT"} for column in columns])
    found = join_candidates([table("items", "order_id"), table("payments", "order_id"), table("orders", "order_id")], [])
    assert {(many.name, one.name) for many, _, one, _ in found} == {("items", "orders"), ("payments", "orders")}
    assert singular_stem("t_lattice_categories") == "category" and singular_stem("dim.address") == "address"


# ----- correction loop ------------------------------------------------------------------------
WRONG_KEY = "SELECT c.region, SUM(o.amount) AS amount FROM orders o JOIN customers c ON o.order_id = c.customer_id GROUP BY c.region"


def test_join_on_the_wrong_column_goes_back_to_the_model(client, tmp_path, monkeypatch):
    calls = generator_replies(monkeypatch, [WRONG_KEY, GOOD])
    answer = ask(client, "各地区订单金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200, answer.text
    assert len(calls) == 2 and "关联字段不对" in calls[1]["question"] and "orders(customer_id)" in calls[1]["question"]
    assert {row[0]: row[1] for row in answer.json()["rows"]} == {"华东": 150.0, "华南": 70.0}


def test_model_insisting_on_the_wrong_column_is_refused(client, tmp_path, monkeypatch):
    calls = generator_replies(monkeypatch, [WRONG_KEY])
    answer = ask(client, "各地区订单金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 422 and "关联字段不对" in answer.json()["detail"] and len(calls) == 3


def test_engine_error_is_shown_to_the_model(client, tmp_path, monkeypatch):
    broken = "SELECT c.region, NO_SUCH_FUNCTION(o.amount) AS amount FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.region"
    calls = generator_replies(monkeypatch, [broken, GOOD])
    answer = ask(client, "各地区订单金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200, answer.text
    assert "执行错误" in calls[1]["question"] and "NO_SUCH_FUNCTION" in calls[1]["question"]


def test_engine_errors_stop_after_the_allowed_corrections(client, tmp_path, monkeypatch):
    calls = generator_replies(monkeypatch, ["SELECT NO_SUCH_FUNCTION(amount) AS x FROM orders"])
    assert ask(client, "订单金额", source(client, warehouse(tmp_path / "w.duckdb"))).status_code == 400
    assert len(calls) == 3


def test_filter_value_the_data_lacks_gets_one_correction_then_a_warning(client, tmp_path, monkeypatch):
    invented = "SELECT SUM(amount) AS amount FROM orders WHERE LOWER(status) LIKE '%refund%'"
    calls = generator_replies(monkeypatch, [invented, "SELECT SUM(amount) AS amount FROM orders WHERE status = '已退款'"])
    answer = ask(client, "已退款订单的金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200, answer.text
    assert "refund" in calls[1]["question"] and "没有匹配" in calls[1]["question"]
    verification = answer.json()["verification"]
    assert verification["status"] == "skipped" and any("已退款" in check["detail"] for check in verification["checks"])


def test_correction_cannot_drop_the_filter_and_answer_another_question(client, tmp_path, monkeypatch):
    """Seen with a real model: asked about refunds in data with none, it summed every status."""
    wanted = "SELECT SUM(amount) AS amount FROM orders WHERE status = '已退款'"
    overview = "SELECT status, SUM(amount) AS amount FROM orders GROUP BY status"
    calls = generator_replies(monkeypatch, [wanted, overview, wanted])
    answer = ask(client, "已退款订单的总金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200, answer.text
    assert len(calls) == 3 and "不再按 orders.status 筛选" in calls[2]["question"]
    payload = answer.json()
    assert payload["rows"] == [[None]] and payload["verification"]["status"] == "skipped"
    assert any("已退款" in check["detail"] for check in payload["verification"]["checks"])
    # A model that will not keep the condition gets no answer at all.
    generator_replies(monkeypatch, [wanted, overview])
    refused = ask(client, "已退款订单的总金额", source(client, warehouse(tmp_path / "w2.duckdb")))
    assert refused.status_code == 422 and "不再按 orders.status 筛选" in refused.json()["detail"]


def test_real_filter_value_passes(client, tmp_path, monkeypatch):
    calls = generator_replies(monkeypatch, ["SELECT SUM(amount) AS amount FROM orders WHERE status = '已完成'"])
    answer = ask(client, "已完成订单金额", source(client, warehouse(tmp_path / "w.duckdb")))
    assert answer.status_code == 200 and len(calls) == 1 and answer.json()["rows"] == [[170.0]]


def test_audit_treats_inferred_relationships_as_advice():
    known = {"a": {"x", "y"}, "b": {"x", "y"}}
    inferred = [JoinEdge("a", ["x"], "b", ["x"], INFERRED)]
    declared = [JoinEdge("a", ["x"], "b", ["x"], "外键")]
    sql = "SELECT a.x FROM a JOIN b ON a.y = b.y"
    assert audit_sql(sql, "duckdb", known, edges=inferred).errors == []
    assert any("关联字段不对" in text for text in audit_sql(sql, "duckdb", known, edges=inferred).warnings)
    assert any("关联字段不对" in text for text in audit_sql(sql, "duckdb", known, edges=declared).errors)
    assert audit_sql("SELECT a.x FROM a JOIN b ON a.x = b.x", "duckdb", known, edges=declared).errors == []
    reversed_sides = "SELECT a.x FROM b JOIN a ON b.x = a.x"
    assert audit_sql(reversed_sides, "duckdb", known, edges=declared).errors == []
    unknown = [JoinEdge("a", ["x"], "c", ["x"], "外键")]
    assert any("没有已知的关联" in text for text in audit_sql("SELECT a.x FROM a JOIN b ON a.x = b.x", "duckdb", known, edges=unknown).warnings)


def test_value_probe_statement():
    assert "LOWER(`status`) LIKE '%refund%'" in value_probe_sql("s", "t", "status", "%Refund%", True, "mysql")


# ----- scope ----------------------------------------------------------------------------------
def test_denied_table_is_hidden_and_refused(client, tmp_path, monkeypatch):
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    assert client.post(f"/api/datasources/{source_id}/question-scope", json={"deny_tables": ["customers", "audit_*"]}).status_code == 200
    assert client.get(f"/api/datasources/{source_id}/question-scope").json()["deny_tables"] == ["audit_*", "customers"]
    calls = generator_replies(monkeypatch, [GOOD])
    answer = ask(client, "各地区订单金额", source_id)
    assert answer.status_code == 422 and "不在智能问数允许访问的范围内" in answer.json()["detail"]
    assert "customers" not in calls[0]["schema"].replace("customer_id", "") and "audit_log" not in calls[0]["schema"]


def test_denied_column_is_hidden_and_refused(client, tmp_path, monkeypatch):
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    client.post(f"/api/datasources/{source_id}/question-scope", json={"deny_columns": ["customers.region"]})
    calls = generator_replies(monkeypatch, [GOOD])
    assert ask(client, "各地区订单金额", source_id).status_code == 422
    assert "region" not in calls[0]["schema"] and "华东" not in calls[0]["schema"]
    # SELECT * and an unqualified name would both read the column without naming its table.
    generator_replies(monkeypatch, ["SELECT * FROM customers"])
    assert "SELECT *" in ask(client, "客户", source_id).json()["detail"]
    generator_replies(monkeypatch, ["SELECT region FROM orders o JOIN customers c ON o.customer_id = c.customer_id"])
    assert "customers.region" in ask(client, "客户", source_id).json()["detail"]


def test_allow_list_limits_tables_and_bad_column_rule_is_rejected(tmp_path):
    scope = QuestionScope(tmp_path / "scope.json")
    scope.save("s", ["dwd_*"], ["dwd_secret"], ["dwd_users.phone"])
    assert scope.table_allowed("s", "dwd_orders") and scope.table_allowed("s", "main.dwd_orders")
    assert not scope.table_allowed("s", "ods_orders") and not scope.table_allowed("s", "dwd_secret")
    assert not scope.column_allowed("s", "DWD_USERS", "PHONE") and scope.column_allowed("s", "dwd_users", "name")
    assert scope.table_allowed("other", "anything")
    with pytest.raises(ValueError, match="表.字段"):
        scope.save("s", [], [], ["phone"])


def test_scope_applies_to_the_semantic_layer_too(client, monkeypatch):
    plan_replies(monkeypatch, {**PLAN, "status": "unmapped", "reason": "没有这个维度。", "metrics": []})
    client.post("/api/datasources/local-sample/question-scope", json={"deny_columns": ["t_lattice_customers.region"]})
    # 地区 is a word the semantic model knows, so the plan is made and then stopped by the scope.
    denied = ask(client, "各地区的销售额", "local-sample")
    assert denied.status_code == 403 and "t_lattice_customers.region" in denied.json()["detail"]
    # The region values are not usable as filter words either.
    assert ask(client, "华东的销售额", "local-sample").status_code == 422
    assert ask(client, "各类别的销售额", "local-sample").status_code == 200
    # A model reading an excluded table is not offered at all, so its metrics cannot answer.
    client.post("/api/datasources/local-sample/question-scope", json={"deny_tables": ["t_lattice_payments"]})
    queries = client.app.state.queries
    assert not [model for model in queries.semantic.models_for(queries._record("local-sample")) if queries._model_in_scope(queries._record("local-sample"), model) and model.name == "lattice_demo_sales"]


# ----- confirmed answers -------------------------------------------------------------------------
PLAN = {"status": "ok", "reason": "", "title": "订单金额", "metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_customers.region", "grain": None}], "filters": [], "order_by": [], "limit": None, "steps": ["映射"]}


def plan_replies(monkeypatch, reply):
    calls = []

    def fake(self, prompt, *, system, schema):
        calls.append(prompt)
        return reply

    monkeypatch.setattr(SqlGenerator, "complete_json", fake)
    return calls


def test_confirmed_plan_answers_without_the_model_until_the_definition_changes(client, monkeypatch):
    calls = plan_replies(monkeypatch, PLAN)
    first = ask(client, "按大区看成交规模怎么样", "local-sample").json()
    assert first["semantic"]["planner"] == "llm" and first["question"] == "按大区看成交规模怎么样" and len(calls) == 1
    confirmed = client.post("/api/query/confirmed", json={"result_id": first["id"]})
    assert confirmed.status_code == 200 and confirmed.json()["kind"] == "semantic"
    second = ask(client, "按大区，看成交规模怎么样？", "local-sample").json()
    assert second["semantic"]["planner"] == "verified" and second["sql"] == first["sql"] and len(calls) == 1
    # Definitions change: the confirmation described something that no longer exists.
    engine = client.app.state.semantic_engine
    original = engine.builtin

    def edited():
        document = original()
        document["semantic_model"][0]["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(t_lattice_order_items.price) * 2"
        return document

    monkeypatch.setattr(engine, "builtin", edited)
    engine.invalidate()
    third = ask(client, "按大区看成交规模怎么样", "local-sample").json()
    assert third["semantic"]["planner"] == "llm" and len(calls) == 2


def test_only_confirmed_answers_become_examples(client, monkeypatch):
    calls = plan_replies(monkeypatch, PLAN)
    first = ask(client, "大区成交规模", "local-sample").json()
    ask(client, "大区成交规模如何", "local-sample")
    assert "人工确认" not in calls[1]
    client.post("/api/query/confirmed", json={"result_id": first["id"]})
    ask(client, "大区成交规模如何", "local-sample")
    assert "人工确认" in calls[2] and "大区成交规模" in calls[2] and "t_lattice_customers.region" in calls[2]


def test_confirmed_sql_is_reused_and_dropped_when_it_stops_holding_up(client, tmp_path, monkeypatch):
    path = warehouse(tmp_path / "w.duckdb")
    source_id = source(client, path)
    calls = generator_replies(monkeypatch, [GOOD])
    first = ask(client, "各地区订单金额", source_id).json()
    client.post("/api/query/confirmed", json={"result_id": first["id"]})
    again = ask(client, "各地区订单金额", source_id).json()
    assert len(calls) == 1 and again["verification"]["checks"][0]["name"] == "人工确认"
    similar = ask(client, "各地区订单金额汇总情况", source_id)
    assert similar.status_code == 200 and len(calls) == 2 and "人工确认" in calls[1]["schema"] and "o.customer_id" in calls[1]["schema"]
    # The confirmed statement now reads a table outside the scope, so it cannot be used.
    client.post(f"/api/datasources/{source_id}/question-scope", json={"deny_tables": ["customers"]})
    generator_replies(monkeypatch, ["SELECT status, SUM(amount) AS amount FROM orders GROUP BY status"])
    assert ask(client, "各地区订单金额", source_id).json()["provider"] == "llm"


def test_confirmation_can_be_listed_and_removed_and_needs_a_question(client, monkeypatch):
    plan_replies(monkeypatch, PLAN)
    first = ask(client, "大区成交规模", "local-sample").json()
    item = client.post("/api/query/confirmed", json={"result_id": first["id"]}).json()
    assert [row["id"] for row in client.get("/api/query/confirmed", params={"datasource_id": "local-sample"}).json()["items"]] == [item["id"]]
    assert client.post(f"/api/query/confirmed/{item['id']}/delete", json={}).json() == {"deleted": True}
    assert client.get("/api/query/confirmed").json()["items"] == []
    written = client.post("/api/query", json={"sql": "SELECT 1 AS one"}).json()
    assert client.post("/api/query/confirmed", json={"result_id": written["id"]}).status_code == 400
    assert client.post("/api/query/confirmed", json={"result_id": "missing"}).status_code == 404


def test_every_question_is_logged_including_refusals(client, monkeypatch):
    plan_replies(monkeypatch, {**PLAN, "status": "unmapped", "reason": "模型没有退款金额。", "metrics": []})
    ask(client, "退款金额趋势", "local-sample")
    ask(client, "各地区的销售额", "local-sample")
    items = client.get("/api/query/log").json()["items"]
    outcomes = {item["question"]: item for item in items}
    assert outcomes["退款金额趋势"]["outcome"] == "refused" and "退款金额" in outcomes["退款金额趋势"]["detail"]
    assert outcomes["各地区的销售额"]["outcome"] == "answered" and outcomes["各地区的销售额"]["planner"] == "rules" and "SUM" in outcomes["各地区的销售额"]["sql"]
    assert [item["question"] for item in client.get("/api/query/log", params={"outcome": "refused"}).json()["items"]] == ["退款金额趋势"]


def test_catalog_glossary_reaches_the_planner(client, monkeypatch):
    calls = plan_replies(monkeypatch, PLAN)
    client.app.state.queries.annotator = lambda record: "业务语义：大盘成交额即销售总额。"
    ask(client, "大盘成交额", "local-sample")
    assert "大盘成交额即销售总额" in calls[0]


# ----- wide tables -----------------------------------------------------------------------------
WIDE = {
    "name": "wide",
    "datasets": [{"name": "sales_wide", "source": "SELECT i.price, c.region FROM t_lattice_order_items i JOIN t_lattice_orders o ON i.order_id = o.order_id JOIN t_lattice_customers c ON o.customer_id = c.customer_id", "fields": [
        {"name": "price", "datatype": "Decimal", "expression": "price"},
        {"name": "region", "datatype": "String", "expression": "region", "ai_context": {"synonyms": ["地区"]}},
    ]}],
    "metrics": [{"name": "amount", "description": "金额", "expression": "SUM(sales_wide.price)", "ai_context": {"synonyms": ["宽表金额"]}}],
}


def test_model_over_a_wide_table_query_is_offered_and_answers(client):
    queries = client.app.state.queries
    engine = SemanticQueryEngine(queries=queries, builtin=lambda: WIDE)
    record = queries._record("local-sample")
    assert [model.name for model in engine.models_for(record)] == ["wide"]
    queries.semantic = engine
    answer = ask(client, "各地区宽表金额", "local-sample")
    assert answer.status_code == 200, answer.text
    assert len(answer.json()["rows"]) == 3 and "JOIN" in answer.json()["sql"]


def test_source_tables_of_each_kind_of_dataset():
    assert source_tables("local.main.orders") == ["orders"]
    assert source_tables("WITH a AS (SELECT * FROM x) SELECT * FROM a JOIN y ON a.i = y.i") == ["x", "y"]
    assert source_tables("SELECT FROM (") != []
