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
"""Multi-table questions: definitions stay aligned and the numbers are right.

The demo data has one item and one payment per order, which hides every join mistake,
so this data adds orders with several items and payments. Expected values are computed
from one table at a time, never through the joins under test.
"""

import duckdb
import pytest
import sqlglot
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.llm import SqlGenerator
from webapi.semantic_query import Compiler, SemanticQueryError, parse_model
from webapi.sql_audit import audit_sql, duplicate_key_probe_sql, parse_schema_context


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    for name in ("LATTICE_LLM_PROVIDER", "LATTICE_LLM_API_KEY", "LATTICE_LLM_MODEL", "LATTICE_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as test_client:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        store = test_client.app.state.store
        with duckdb.connect(str(store.db)) as con:
            # Customer 1 (华南, since 1 % 3 == 1) buys two products from two sellers in one
            # order and pays in two instalments, so every join through the order fans out.
            con.execute("INSERT INTO t_lattice_orders VALUES (9001, 1, DATE '2018-03-05', '已完成')")
            con.execute("INSERT INTO t_lattice_order_items VALUES (9001, 1, 1, 100.00, DATE '2018-03-05'), (9001, 2, 2, 50.00, DATE '2018-03-05')")
            con.execute("INSERT INTO t_lattice_payments VALUES (9001, 90.00, '在线支付'), (9001, 60.00, '银行转账')")
            # An order nobody paid for, in a status of its own.
            con.execute("INSERT INTO t_lattice_orders VALUES (9002, 2, DATE '2018-04-01', '已取消')")
            con.execute("INSERT INTO t_lattice_order_items VALUES (9002, 3, 3, 30.00, DATE '2018-04-01')")
        yield test_client


def truth(client, sql):
    with duckdb.connect(str(client.app.state.store.db), read_only=True) as con:
        return con.execute(sql).fetchall()


def ask(client, question, **body):
    return client.post("/api/query", json={"question": question, "datasource_id": "local-sample", **body})


def table(answer):
    payload = answer.json()
    return [dict(zip(payload["columns"], row)) for row in payload["rows"]]


SALES_BY_REGION = """SELECT c.region, sum(i.price) FROM t_lattice_order_items i
    JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) GROUP BY 1"""
PAYMENT_BY_REGION = """SELECT c.region, sum(p.payment_value) FROM t_lattice_payments p
    JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) GROUP BY 1"""


def test_two_fact_tables_keep_the_values_each_has_alone(client):
    answer = ask(client, "各地区的销售额和支付额")
    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["provider"] == "semantic" and payload["semantic"]["planner"] == "rules"
    rows = {row["region"]: row for row in table(answer)}
    sales, payments = dict(truth(client, SALES_BY_REGION)), dict(truth(client, PAYMENT_BY_REGION))
    assert set(rows) == set(sales)
    for region, row in rows.items():
        assert float(row["total_sales"]) == pytest.approx(float(sales[region]))
        assert float(row["total_payment"]) == pytest.approx(float(payments.get(region, 0)))
    # The join a naive query would write really does inflate this data.
    naive = dict(truth(client, """SELECT c.region, sum(i.price) FROM t_lattice_order_items i
        JOIN t_lattice_payments p USING (order_id) JOIN t_lattice_orders o USING (order_id)
        JOIN t_lattice_customers c USING (customer_id) GROUP BY 1"""))
    assert any(float(naive[region]) != pytest.approx(float(sales[region])) for region in naive)
    alone = {row["region"]: row["total_sales"] for row in table(ask(client, "各地区的销售额"))}
    assert {region: float(value) for region, value in alone.items()} == pytest.approx({region: float(row["total_sales"]) for region, row in rows.items()})
    assert any("不同的事实表" in text for text in payload["semantic"]["warnings"])
    assert payload["verification"]["governed"] is True and payload["verification"]["status"] == "passed"
    assert any("合计" in check["detail"] for check in payload["verification"]["checks"])


def test_three_table_filter_and_ranking(client):
    answer = ask(client, "华南地区销售额最高的前3个商家")
    assert answer.status_code == 200, answer.text
    expected = truth(client, """SELECT s.seller_name, sum(i.price) AS v FROM t_lattice_order_items i
        JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id)
        JOIN t_lattice_sellers s USING (seller_id) WHERE c.region = '华南' GROUP BY 1 ORDER BY v DESC, 1 LIMIT 3""")
    assert [(row["seller_name"], pytest.approx(float(row["total_sales"]))) for row in table(answer)] == [(name, pytest.approx(float(value))) for name, value in expected]
    filters = answer.json()["semantic"]["filters"]
    assert filters == [{"field": "t_lattice_customers.region", "op": "=", "value": "华南"}]
    assert any("合计" in check["name"] for check in answer.json()["verification"]["checks"])


def test_time_filter_and_grain_use_the_declared_order_date(client):
    rows = table(ask(client, "2018年各类别的订单量"))
    expected = truth(client, """SELECT p.category, count(DISTINCT o.order_id) FROM t_lattice_orders o
        JOIN t_lattice_order_items i USING (order_id) JOIN t_lattice_products p USING (product_id)
        WHERE o.order_date BETWEEN DATE '2018-01-01' AND DATE '2018-12-31' GROUP BY 1""")
    assert {row["category"]: row["order_count"] for row in rows} == dict(expected)
    answer = ask(client, "2018年3月的销售额")
    expected_march = truth(client, """SELECT sum(i.price) FROM t_lattice_order_items i JOIN t_lattice_orders o USING (order_id)
        WHERE o.order_date BETWEEN DATE '2018-03-01' AND DATE '2018-03-31'""")[0][0]
    assert float(table(answer)[0]["total_sales"]) == pytest.approx(float(expected_march))
    monthly = ask(client, "各地区每月销售额").json()
    assert monthly["columns"][:2] == ["order_date", "region"] and monthly["semantic"]["dimensions"][0] == {"field": "t_lattice_orders.order_date", "grain": "month"}
    quarter = ask(client, "2018年第一季度订单量")
    assert table(quarter)[0]["order_count"] == truth(client, "SELECT count(*) FROM t_lattice_orders WHERE order_date BETWEEN DATE '2018-01-01' AND DATE '2018-03-31'")[0][0]


def test_distinct_metric_across_a_fan_out_warns_and_never_inflates(client):
    answer = ask(client, "各类别订单量")
    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert "INNER JOIN" in payload["sql"] or "JOIN" in payload["sql"]
    assert any("多个分组" in text for text in payload["semantic"]["warnings"])
    # Order 9001 spans two categories, so it counts once in each and groups exceed the total.
    total = truth(client, "SELECT count(*) FROM t_lattice_orders")[0][0]
    assert sum(row["order_count"] for row in table(answer)) > total
    assert table(ask(client, "订单量"))[0]["order_count"] == total


def test_additive_metric_across_a_fan_out_is_refused(client):
    answer = ask(client, "各商品类别的支付额")
    assert answer.status_code == 400
    assert "重复累计" in answer.json()["detail"] and "一对多" in answer.json()["detail"]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("退款金额趋势", "退款"),  # A concept the model does not define.
        ("未下单的客户数", "未"),  # A word that reverses the meaning must not be dropped.
        ("非华东地区销售额", "非"),
        ("销售额环比增长率", "环比"),
        ("各地区", "没有识别到语义模型定义的指标"),
        # Half of these words looks like something known; none may be read as its other half.
        ("前月销售额", "前"),
        ("本月销售额", "本"),
        ("上个月销售额", "上个"),
        ("货款金额", "货款"),
    ],
)
def test_question_with_unexplained_words_is_refused_without_a_model(client, question, expected):
    answer = ask(client, question)
    assert answer.status_code == 422, answer.text
    assert expected in answer.json()["detail"] and "现有指标" in answer.json()["detail"]


def test_same_question_compiles_to_the_same_sql(client):
    first, second = ask(client, "华东地区各商品类别的销售额").json(), ask(client, "华东地区各商品类别的销售额").json()
    assert first["sql"] == second["sql"] and "region" in first["sql"] and first["semantic"]["filters"][0]["value"] == "华东"
    assert {row[0] for row in first["rows"]} == {row[0] for row in truth(client, """SELECT DISTINCT p.category FROM t_lattice_order_items i
        JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id)
        JOIN t_lattice_products p USING (product_id) WHERE c.region = '华东'""")}


def test_several_values_compare_side_by_side(client):
    rows = table(ask(client, "华东和华南的销售额"))
    assert {row["region"] for row in rows} == {"华东", "华南"}


def test_duplicate_dimension_key_fails_the_reconciliation(client):
    """A dimension key the model declares unique but the data repeats would inflate
    every group; the totals disagree, so no number is returned."""
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("INSERT INTO t_lattice_customers VALUES (1, '客户 1 副本', '华东')")
    client.app.state.semantic_engine.invalidate()
    answer = ask(client, "各地区的销售额")
    assert answer.status_code == 409, answer.text
    assert "口径校验" in answer.json()["detail"] and "不唯一" in answer.json()["detail"]


def test_verification_is_recorded_in_history(client):
    answer = ask(client, "各地区的销售额").json()
    stored = client.get("/api/history").json()["items"][0]
    assert stored["id"] == answer["id"] and stored["verification"]["status"] == "passed" and stored["semantic"]["definitions"][0]["name"] == "total_sales"


# ----- with a language model --------------------------------------------------------------
def configure(client):
    assert client.post("/api/llm", json={"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}).status_code == 200


def plan_replies(monkeypatch, replies):
    calls = []

    def fake(self, prompt, *, system, schema):
        calls.append(prompt)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(SqlGenerator, "complete_json", fake)
    return calls


def sql_replies(monkeypatch, statements):
    calls = []

    def fake(self, question, dialect_label, source_label, schema_text):
        calls.append((question, schema_text))
        sql = statements[min(len(calls) - 1, len(statements) - 1)]
        return {"title": "t", "sql": sql, "steps": ["s"], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", fake)
    return calls


PLAN = {"status": "ok", "reason": "", "title": "华东订单金额", "metrics": ["total_sales"], "dimensions": [], "filters": [], "order_by": [], "limit": 0, "steps": ["映射"]}


def test_understood_question_never_calls_the_model(client, monkeypatch):
    configure(client)
    calls = plan_replies(monkeypatch, [PLAN])
    assert ask(client, "各地区的销售额").json()["semantic"]["planner"] == "rules"
    assert calls == []


def test_model_plan_that_drops_a_filter_is_sent_back(client, monkeypatch):
    configure(client)
    fixed = {**PLAN, "filters": [{"field": "t_lattice_customers.region", "op": "=", "value": "华东地区"}]}
    calls = plan_replies(monkeypatch, [PLAN, fixed])
    answer = ask(client, "华东的订单金额是什么水平")
    assert answer.status_code == 200, answer.text
    assert len(calls) == 2 and "华东" in calls[1] and "没有按" in calls[1]
    payload = answer.json()
    assert payload["semantic"]["planner"] == "llm"
    # A loose value from the model is resolved to the value the data really holds.
    assert payload["semantic"]["filters"] == [{"field": "t_lattice_customers.region", "op": "=", "value": "华东"}]


def test_model_cannot_invent_names_or_values(client, monkeypatch):
    configure(client)
    plan_replies(monkeypatch, [{**PLAN, "metrics": ["refund_amount"], "filters": [{"field": "t_lattice_customers.region", "op": "=", "value": "火星"}]}])
    answer = ask(client, "退款水平怎么样")
    assert answer.status_code == 422
    assert "refund_amount" in answer.json()["detail"] and "火星" in answer.json()["detail"]


def test_undefined_concept_is_refused_and_never_reaches_free_sql(client, monkeypatch):
    configure(client)
    plan_replies(monkeypatch, [{**PLAN, "status": "unmapped", "reason": "模型没有退款金额。", "metrics": []}])
    generated = sql_replies(monkeypatch, ["SELECT 1 AS refund"])
    answer = ask(client, "退款金额趋势")
    assert answer.status_code == 422 and "退款金额" in answer.json()["detail"] and "没有猜测口径" in answer.json()["detail"]
    assert generated == []


def test_derived_question_gets_definitions_and_a_fan_out_audit(client, monkeypatch):
    configure(client)
    plan_replies(monkeypatch, [{**PLAN, "status": "derived", "reason": "需要计算占比。"}])
    fanned = "SELECT o.status, SUM(p.payment_value) AS v FROM t_lattice_payments p JOIN t_lattice_orders o ON p.order_id = o.order_id JOIN t_lattice_order_items i ON i.order_id = o.order_id GROUP BY o.status"
    correct = "SELECT o.status, SUM(p.payment_value) AS v FROM t_lattice_payments p JOIN t_lattice_orders o ON p.order_id = o.order_id GROUP BY o.status"
    calls = sql_replies(monkeypatch, [fanned, correct])
    answer = ask(client, "各状态的支付额占比")
    assert answer.status_code == 200, answer.text
    assert len(calls) == 2 and "重复累计" in calls[1][0] and "SUM(t_lattice_payments.payment_value)" in calls[0][1]
    payload = answer.json()
    assert payload["provider"] == "llm" and payload["verification"]["governed"] is False and "t_lattice_order_items" not in payload["sql"]
    assert any("未经过语义层" in step for step in payload["steps"])
    # A model that insists on the wrong join gets no result at all.
    sql_replies(monkeypatch, [fanned])
    refused = ask(client, "各状态的支付额占比")
    assert refused.status_code == 422 and "重复累计" in refused.json()["detail"]


def test_model_outage_is_reported_not_answered_from_a_narrower_rule(client, monkeypatch):
    from webapi.llm import LlmError

    configure(client)

    def down(self, prompt, *, system, schema):
        raise LlmError("API Key 无效")

    monkeypatch.setattr(SqlGenerator, "complete_json", down)
    answer = ask(client, "销售额环比增长率")
    assert answer.status_code == 502 and "API Key 无效" in answer.json()["detail"]


def test_derived_question_is_not_answered_by_keyword_rules_when_the_model_fails(client, monkeypatch):
    from webapi.llm import LlmError

    configure(client)
    plan_replies(monkeypatch, [{**PLAN, "status": "derived", "reason": "需要计算环比。"}])

    def down(self, question, dialect_label, source_label, schema_text):
        raise LlmError("连接中断")

    monkeypatch.setattr(SqlGenerator, "generate", down)
    answer = ask(client, "每月销售额环比增长率")
    assert answer.status_code == 502 and "连接中断" in answer.json()["detail"]


# ----- the compiler on a model with its own traps ----------------------------------------
ROLES = {
    "name": "roles",
    "datasets": [
        {"name": "orders", "source": "orders", "primary_key": ["id"], "fields": [{"name": "amount", "datatype": "Decimal", "expression": "amount"}]},
        {"name": "ship_city", "source": "city", "primary_key": ["id"], "fields": [{"name": "city", "datatype": "String", "expression": "city"}]},
        {"name": "hub", "source": "hub", "primary_key": ["id"], "fields": []},
        {"name": "depot", "source": "depot", "primary_key": ["id"], "fields": []},
    ],
    "relationships": [
        {"name": "via_hub_1", "from": "orders", "to": "hub", "from_columns": ["hub_id"], "to_columns": ["id"]},
        {"name": "via_hub_2", "from": "hub", "to": "ship_city", "from_columns": ["city_id"], "to_columns": ["id"]},
        {"name": "via_depot_1", "from": "orders", "to": "depot", "from_columns": ["depot_id"], "to_columns": ["id"]},
        {"name": "via_depot_2", "from": "depot", "to": "ship_city", "from_columns": ["city_id"], "to_columns": ["id"]},
    ],
    "metrics": [{"name": "amount", "datatype": "Decimal", "expression": "SUM(orders.amount)"}],
}


def test_two_equal_routes_are_refused_until_one_is_named():
    model = parse_model(ROLES)
    request = {"metrics": ["amount"], "dimensions": ["ship_city.city"]}
    with pytest.raises(SemanticQueryError, match="多条关联路径"):
        Compiler(model, "duckdb").compile(request)
    chosen = Compiler(model, "duckdb").compile({**request, "relationships": ["via_depot_1", "via_depot_2"]})
    assert '"depot"' in chosen.sql and '"hub"' not in chosen.sql


def test_metric_mixing_two_grains_is_refused():
    document = {
        "name": "mixed",
        "datasets": [
            {"name": "items", "source": "items", "primary_key": ["id"], "fields": []},
            {"name": "orders", "source": "orders", "primary_key": ["id"], "fields": []},
        ],
        "relationships": [{"name": "r", "from": "items", "to": "orders", "from_columns": ["order_id"], "to_columns": ["id"]}],
        "metrics": [{"name": "ratio", "expression": "SUM(items.price) / SUM(orders.freight)"}, {"name": "rows", "expression": "COUNT(*)"}],
    }
    model = parse_model(document)
    with pytest.raises(SemanticQueryError, match="重复累计"):
        Compiler(model, "duckdb").compile({"metrics": ["ratio"]})
    with pytest.raises(SemanticQueryError, match="无法确定统计的是哪张表"):
        Compiler(model, "duckdb").compile({"metrics": ["rows"]})


def test_one_to_one_join_is_not_a_fan_out():
    document = {
        "name": "extension",
        "datasets": [
            {"name": "orders", "source": "orders", "primary_key": ["id"], "fields": [{"name": "amount", "datatype": "Decimal", "expression": "amount"}]},
            {"name": "details", "source": "details", "primary_key": ["order_id"], "fields": [{"name": "note", "datatype": "String", "expression": "note"}]},
        ],
        "relationships": [{"name": "r", "from": "details", "to": "orders", "from_columns": ["order_id"], "to_columns": ["id"]}],
        "metrics": [{"name": "amount", "expression": "SUM(orders.amount)"}],
    }
    assert "JOIN" in Compiler(parse_model(document), "duckdb").compile({"metrics": ["amount"], "dimensions": ["details.note"]}).sql


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "mysql", "clickhouse", "starrocks", "doris", "hive"])
def test_combining_fact_tables_compiles_for_every_engine(client, dialect):
    model = client.app.state.semantic_engine.load_model("builtin")
    compiled = Compiler(model, dialect, bare_schemas=("main",)).compile({"metrics": ["total_sales", "total_payment"], "dimensions": [{"field": "t_lattice_customers.region"}], "filters": [{"field": "total_sales", "op": ">", "value": 1}], "limit": 5})
    parsed = sqlglot.parse_one(compiled.sql, read=dialect)
    assert parsed.find(sqlglot.exp.Union) is not None and "FULL" not in compiled.sql.upper()
    assert [column["name"] for column in compiled.columns] == ["region", "total_sales", "total_payment"]


# ----- the audit of free SQL ---------------------------------------------------------------
def test_order_by_never_emulates_null_position_on_any_engine(client):
    """MySQL's ONLY_FULL_GROUP_BY rejects the CASE WHEN that emulation would add."""
    model = client.app.state.semantic_engine.load_model("builtin")
    for dialect in ("mysql", "postgres", "clickhouse", "hive", "starrocks", "doris"):
        sql = Compiler(model, dialect).compile({"metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_orders.order_date", "grain": "month"}, {"field": "t_lattice_customers.region"}]}).sql
        assert "CASE WHEN" not in sql, dialect


@pytest.mark.parametrize("grain, call", [("month", "'yyyy-MM-01'"), ("quarter", "QUARTER("), ("year", "'yyyy-01-01'"), ("day", "'yyyy-MM-dd'"), ("week", "NEXT_DAY(")])
def test_hive_groups_periods_with_functions_that_work_there(client, grain, call):
    """Hive 4.0.1 returns 1970-01-01 for TRUNC(date, 'MM') inside GROUP BY, with no error."""
    model = client.app.state.semantic_engine.load_model("builtin")
    sql = Compiler(model, "hive").compile({"metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_orders.order_date", "grain": grain}]}).sql
    assert call in sql and "DATE_TRUNC" not in sql and "TRUNC(" not in sql
    sqlglot.parse_one(sql, read="hive")


def test_combined_metrics_share_one_type_in_every_branch(client):
    """ClickHouse has no common type for countDistinct's UInt64 and a NULL Int64."""
    model = client.app.state.semantic_engine.load_model("builtin")
    sql = Compiler(model, "clickhouse").compile({"metrics": ["total_sales", "order_count"], "dimensions": [{"field": "t_lattice_customers.region"}]}).sql
    assert sql.count("Nullable(Int64)") == 2 and sql.count("Nullable(Decimal(38, 6))") == 2


def test_question_with_no_match_goes_to_the_richest_model(client, monkeypatch):
    """A small unrelated model on the same tables must not answer for the main one."""
    configure(client)
    engine = client.app.state.semantic_engine
    main = engine.load_model("builtin")
    tiny = parse_model({"name": "tiny", "datasets": [{"name": "t_lattice_orders", "source": "t_lattice_orders", "fields": [{"name": "status", "datatype": "String", "expression": "status"}]}], "metrics": [{"name": "n", "expression": "COUNT(t_lattice_orders.order_id)"}]}, model_id="tiny")
    monkeypatch.setattr(engine, "models_for", lambda record, question="": [tiny, main])
    monkeypatch.setattr(engine, "dimension_values", lambda model, record: {})
    prompts = plan_replies(monkeypatch, [{**PLAN, "status": "unmapped", "reason": "没有定义。", "metrics": []}])
    answer = ask(client, "退款金额趋势")
    assert answer.status_code == 422 and "lattice_demo_sales" in answer.json()["detail"] and "lattice_demo_sales" in prompts[0]


def test_model_reply_that_is_not_json_is_asked_again(client, monkeypatch):
    from webapi.llm import LlmError

    configure(client)
    calls = []

    def flaky(self, prompt, *, system, schema):
        calls.append(prompt)
        if len(calls) == 1:
            raise LlmError("模型返回的 JSON 无法解析。")
        return PLAN

    monkeypatch.setattr(SqlGenerator, "complete_json", flaky)
    assert ask(client, "随便看看销售水平").status_code == 200 and len(calls) == 2


def test_model_can_filter_on_a_metric(client, monkeypatch):
    configure(client)
    plan_replies(monkeypatch, [{**PLAN, "dimensions": [{"field": "t_lattice_customers.region", "grain": None}], "filters": [{"field": "total_sales", "op": ">", "value": 6065000}], "limit": None}])
    answer = ask(client, "销售额超过一定规模的地区")
    assert answer.status_code == 200, answer.text
    expected = [region for region, value in truth(client, SALES_BY_REGION) if float(value) > 6065000]
    assert sorted(row[0] for row in answer.json()["rows"]) == sorted(expected)


KNOWN = {"orders": {"id", "customer"}, "items": {"order_id", "price"}, "payments": {"order_id", "amount"}, "customers": {"customer", "region"}}
# Orders repeat per customer, as they do in any real data set.
DUPLICATED = {("items", ("order_id",)), ("payments", ("order_id",)), ("orders", ("customer",))}


def probe(schema, name, columns):
    return (name, tuple(sorted(columns))) not in DUPLICATED


@pytest.mark.parametrize(
    "sql, problem",
    [
        ("SELECT o.customer, SUM(p.amount) FROM payments p JOIN orders o ON p.order_id = o.id JOIN items i ON i.order_id = o.id GROUP BY 1", "重复累计"),
        ("WITH a AS (SELECT order_id, SUM(amount) v FROM payments GROUP BY order_id) SELECT SUM(a.v), SUM(i.price) FROM a JOIN items i ON a.order_id = i.order_id", "重复累计"),
        ("SELECT SUM(i.price) FROM items i, orders o", "笛卡尔积"),
        ("SELECT SUM(i.price) FROM items i CROSS JOIN orders o", "笛卡尔积"),
        ("SELECT nope FROM orders", "不在所查询的表中"),
        ("SELECT o.nope FROM orders o", "没有列"),
        ("SELECT 1 AS one FROM missing", "不存在"),
    ],
)
def test_audit_rejects(sql, problem):
    assert any(problem in text for text in audit_sql(sql, "duckdb", KNOWN, probe).errors)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.region, SUM(i.price) FROM items i JOIN orders o ON i.order_id = o.id JOIN customers c ON o.customer = c.customer GROUP BY 1",
        "SELECT c.region, COUNT(DISTINCT o.id) FROM orders o JOIN items i ON i.order_id = o.id JOIN customers c ON c.customer = o.customer GROUP BY 1",
        "WITH a AS (SELECT order_id, SUM(amount) v FROM payments GROUP BY order_id), b AS (SELECT order_id, SUM(price) s FROM items GROUP BY order_id) SELECT SUM(a.v), SUM(b.s) FROM a JOIN b ON a.order_id = b.order_id",
        "SELECT SUM(i.price) FROM items i, orders o WHERE i.order_id = o.id",
        # Pre-aggregated per order, then rolled up through the order's customer: customers
        # repeat per order, which does not repeat the rows being summed.
        "WITH s AS (SELECT order_id, SUM(price) v FROM items GROUP BY order_id) SELECT c.region, SUM(s.v) FROM s JOIN orders o ON s.order_id = o.id JOIN customers c ON o.customer = c.customer GROUP BY 1",
    ],
)
def test_audit_accepts_correct_joins(sql):
    assert audit_sql(sql, "duckdb", KNOWN, probe).errors == []


def test_ties_are_settled_so_a_limit_always_picks_the_same_rows(client):
    first = ask(client, "销售额最高的前3个商家").json()
    assert 'DESC' in first["sql"] and first["sql"].rstrip().endswith("LIMIT 3") and '"seller_name"' in first["sql"].split("ORDER BY")[1]


def test_ranking_a_time_grain_orders_by_the_metric(client):
    rows = ask(client, "销售额最高的月份").json()
    assert len(rows["rows"]) == 1 and rows["semantic"]["dimensions"][0]["grain"] == "month"
    assert float(rows["rows"][0][1]) == pytest.approx(max(float(row[1]) for row in truth(client, """SELECT date_trunc('month', o.order_date), sum(i.price)
        FROM t_lattice_order_items i JOIN t_lattice_orders o USING (order_id) GROUP BY 1""")))


def test_audit_explains_what_count_star_counts():
    report = audit_sql("SELECT COUNT(*) FROM orders o JOIN items i ON i.order_id = o.id", "duckdb", KNOWN, probe)
    assert report.errors == [] and any("关联后的行数" in text for text in report.warnings)


def test_audit_says_when_it_could_not_verify():
    report = audit_sql("SELECT SUM(i.price) FROM items i JOIN orders o ON i.order_id = o.id", "duckdb", KNOWN, lambda *_: None)
    assert report.errors == [] and any("无法排除" in text for text in report.warnings)
    assert audit_sql("not sql at all (", "duckdb").warnings


def test_probe_and_schema_listing_helpers():
    assert "HAVING COUNT(*) > 1" in duplicate_key_probe_sql("s", "t", ["a"], "mysql") and "`s`.`t`" in duplicate_key_probe_sql("s", "t", ["a"], "mysql")
    assert parse_schema_context("- demo.orders，约 3 行：id INT, price DECIMAL(16, 2)\n- plain：a TEXT") == {"orders": {"id", "price"}, "plain": {"a"}}
