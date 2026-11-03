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
"""The four guarantees that used to have conditions attached.

* A word that could mean several measures is asked about, not guessed, and stays settled.
* Grains are found in the data, and too little evidence is reported rather than ignored.
* A wide table is compared with its detail for every metric and group, however it was planned.
* The one-to-many verification data exists on every engine (scripts/verify-questions.py runs it).
"""

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlglot import exp

from webapi import app as app_module
from webapi.llm import LlmError, SqlGenerator
from webapi.query import VERIFY_TABLES, WIDE_SALES_SQL
from webapi.questions import compare_results
from webapi.semantic_query import BUILTIN_WIDE, SemanticQueryEngine, SemanticQueryError
from webapi.table_profile import MIN_REPEATED_KEYS, profile_table


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    for name in ("LATTICE_LLM_PROVIDER", "LATTICE_LLM_API_KEY", "LATTICE_LLM_MODEL", "LATTICE_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as test_client:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        yield test_client


def configure(client):
    assert client.post("/api/llm", json={"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}).status_code == 200


def warehouse(path):
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE customers(customer_id INTEGER, region VARCHAR)")
        con.execute("INSERT INTO customers VALUES (1, '华东'), (2, '华南')")
        con.execute("CREATE TABLE orders(order_id INTEGER, customer_id INTEGER, order_amount DECIMAL(10,2))")
        con.execute("INSERT INTO orders SELECT i, i % 2 + 1, 100 + i FROM range(1, 9) t(i)")
        con.execute("CREATE TABLE order_items(item_id INTEGER, order_id INTEGER, item_price DECIMAL(10,2))")
        con.execute("INSERT INTO order_items SELECT row_number() OVER (), o.order_id, 10 + k FROM orders o, range(1, 3) t(k)")
        con.execute("""CREATE TABLE dws_order_wide AS SELECT i.item_id, i.order_id, o.customer_id, c.region, i.item_price, o.order_amount
            FROM order_items i JOIN orders o USING (order_id) JOIN customers c USING (customer_id)""")
        for index in range(8):
            con.execute(f"CREATE TABLE ods_{index}(a INTEGER)")
    return path


def source(client, path):
    return client.post("/api/datasources", json={"name": "仓库", "type": "duckdb", "config": {"path": str(path)}}).json()["id"]


def script(monkeypatch, *, tables=("dws_order_wide",), measures=None, statements=("SELECT 1 AS one",)):
    calls = {"json": [], "sql": []}

    def complete_json(self, prompt, *, system, schema):
        calls["json"].append(prompt)
        if "tables" in schema["properties"]:
            return {"tables": list(tables)}
        return {"measures": measures or []}

    def generate(self, question, dialect_label, source_label, schema_text):
        calls["sql"].append({"question": question, "schema": schema_text})
        sql = statements[min(len(calls["sql"]) - 1, len(statements) - 1)]
        return {"title": "结果", "sql": sql, "steps": [], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "complete_json", complete_json)
    monkeypatch.setattr(SqlGenerator, "generate", generate)
    return calls


def ask(client, question, source_id):
    return client.post("/api/query", json={"question": question, "datasource_id": source_id, "refresh": True})


AMBIGUOUS = [{"term": "销售额", "candidates": ["dws_order_wide.item_price", "dws_order_wide.order_amount", "dws_order_wide.nope"]}]
BY_PRICE = "SELECT region, SUM(item_price) AS sales FROM dws_order_wide GROUP BY region"
BY_AMOUNT = "SELECT region, SUM(order_amount) AS sales FROM (SELECT DISTINCT order_id, region, order_amount FROM dws_order_wide) t GROUP BY region"


# ----- 1. a word with two meanings ---------------------------------------------------------------
def test_a_measure_the_question_does_not_name_stops_before_running(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    calls = script(monkeypatch, statements=[BY_PRICE])
    answer = ask(client, "各地区销售额", source_id).json()
    assert answer["needs_clarification"] is True and "rows" not in answer
    term = answer["clarification"]["terms"][0]
    assert term["term"] == "销售额"
    assert {option["column"] for option in term["options"]} == {"item_price", "order_amount"}
    # No model is asked whether the word is ambiguous: the check reads the statement.
    assert not any("度量字段" in prompt for prompt in calls["json"])
    assert client.get("/api/query/log", params={"outcome": "clarify"}).json()["items"][0]["question"] == "各地区销售额"


@pytest.mark.parametrize(
    "question, statement, asks",
    [
        ("各地区订单金额", BY_AMOUNT, False),  # Every part of order_amount is in the question.
        ("各地区商品价格", BY_PRICE, False),
        ("各地区金额", BY_AMOUNT, True),  # 金额 names only part of order_amount.
        ("各地区收入", BY_AMOUNT, True),
        ("各地区业绩", BY_PRICE, True),
        ("各地区的 order_amount", BY_AMOUNT, False),
    ],
)
def test_only_the_words_of_the_question_count_as_naming_a_measure(client, tmp_path, monkeypatch, question, statement, asks):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    script(monkeypatch, statements=[statement])
    assert bool(ask(client, question, source_id).json().get("needs_clarification")) is asks


def test_stream_carries_the_question_back(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    script(monkeypatch, statements=[BY_PRICE])
    with client.stream("POST", "/api/query/stream", json={"question": "各地区销售额", "datasource_id": source_id}) as response:
        text = "".join(response.iter_text())
    assert "event: clarify" in text and "item_price" in text and "event: result" not in text and "event: done" in text


def test_settled_word_is_not_asked_again_and_the_sql_must_use_it(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    saved = client.post("/api/query/terms", json={"datasource_id": source_id, "term": "销售额", "table": "dws_order_wide", "column": "item_price"})
    assert saved.status_code == 200, saved.text
    # The model writes the other column first.
    calls = script(monkeypatch, statements=[BY_AMOUNT, BY_PRICE, BY_PRICE])
    answer = ask(client, "各地区销售额", source_id)
    assert answer.status_code == 200, answer.text
    assert "needs_clarification" not in answer.json()
    assert "口径词典" in calls["sql"][0]["schema"] and "dws_order_wide.item_price" in calls["sql"][0]["schema"]
    assert "已确认为 dws_order_wide.item_price" in calls["sql"][1]["question"]
    assert dict(answer.json()["rows"]) == {"华东": 92.0, "华南": 92.0}
    checks = {check["name"]: check for check in answer.json()["verification"]["checks"]}
    assert checks["口径词典"]["status"] == "passed" and "模型选用的度量" not in checks
    assert client.post(f"/api/query/terms/{saved.json()['id']}/delete", json={}).json() == {"deleted": True}
    assert client.get("/api/query/terms", params={"datasource_id": source_id}).json()["items"] == []


def test_settling_a_word_needs_a_real_allowed_column(client, tmp_path):
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    assert client.post("/api/query/terms", json={"datasource_id": source_id, "term": "销售额", "table": "dws_order_wide", "column": "nope"}).status_code == 400
    client.post(f"/api/datasources/{source_id}/question-scope", json={"deny_columns": ["dws_order_wide.order_amount"]})
    assert client.post("/api/query/terms", json={"datasource_id": source_id, "term": "销售额", "table": "dws_order_wide", "column": "order_amount"}).status_code == 400
    assert client.post("/api/query/terms", json={"datasource_id": source_id, "term": "销售额", "table": "@metric", "column": "nope"}).status_code == 400


def test_the_same_column_in_a_wide_table_and_its_source_is_not_a_choice(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    script(monkeypatch, tables=["dws_order_wide", "orders", "order_items"], statements=[BY_AMOUNT])
    options = ask(client, "各地区业绩", source_id).json()["clarification"]["terms"][0]["options"]
    assert [option["column"] for option in options].count("order_amount") == 1
    assert next(option for option in options if option["column"] == "order_amount")["table"] == "dws_order_wide"


def test_one_measure_on_offer_is_no_choice(client, tmp_path, monkeypatch):
    configure(client)
    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE sales(region VARCHAR, amount DECIMAL(10,2))")
        con.execute("INSERT INTO sales VALUES ('华东', 1)")
    source_id = source(client, path)
    script(monkeypatch, tables=["sales"], statements=["SELECT region, SUM(amount) AS a FROM sales GROUP BY region"])
    assert ask(client, "各地区业绩", source_id).json()["rows"] == [["华东", 1.0]]


PLANNED = {"status": "ok", "reason": "", "title": "规模", "metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_customers.region", "grain": None}], "filters": [], "order_by": [], "limit": None, "steps": []}


def test_a_metric_the_model_read_into_a_word_is_asked_about_then_settled(client, monkeypatch):
    configure(client)
    client.app.state.semantic_engine.builtin_wide = None
    client.app.state.semantic_engine.invalidate()
    calls = []

    def plan(self, prompt, *, system, schema):
        calls.append(prompt)
        return PLANNED

    monkeypatch.setattr(SqlGenerator, "complete_json", plan)
    answer = ask(client, "各地区的交易规模", "local-sample").json()
    assert answer["needs_clarification"] is True
    options = answer["clarification"]["terms"][0]["options"]
    assert options[0]["column"] == "total_sales" and options[0]["table"] == "@metric" and len(options) > 1
    saved = client.post("/api/query/terms", json={"datasource_id": "local-sample", "term": "交易规模", "table": "@metric", "column": "total_payment"})
    assert saved.status_code == 200, saved.text
    # What was settled wins over what the model first read, and no model is needed any more.
    settled = ask(client, "各地区的交易规模", "local-sample").json()
    assert settled["semantic"]["metrics"] == ["total_payment"] and settled["semantic"]["planner"] == "rules" and len(calls) == 1


def wide_plan(**changes):
    return {"status": "ok", "reason": "", "title": "规模", "metrics": ["total_sales"], "dimensions": [], "filters": [], "order_by": [], "limit": None, "steps": [], **changes}


def replies(monkeypatch, *plans):
    calls = []

    def fake(self, prompt, *, system, schema):
        calls.append(prompt)
        return plans[min(len(calls) - 1, len(plans) - 1)]

    monkeypatch.setattr(SqlGenerator, "complete_json", fake)
    return calls


def test_a_dimension_the_question_does_not_mention_is_sent_back(client, monkeypatch):
    """Seen with a real model: 每个每年贡献的销售总额 came back grouped by customer."""
    configure(client)
    by_customer = wide_plan(dimensions=[{"field": "sales_wide.customer_name", "grain": None}])
    by_month = wide_plan(dimensions=[{"field": "sales_wide.order_date", "grain": "month"}])
    calls = replies(monkeypatch, by_customer, by_month)
    payload = ask(client, "想了解每月贡献的销售额怎么分布", "local-sample").json()
    assert len(calls) == 2 and "没有提到维度" in calls[1] and payload["semantic"]["dimensions"][0]["grain"] == "month"
    # A model that insists gets no answer rather than a confident wrong one.
    replies(monkeypatch, by_customer)
    refused = ask(client, "想了解销售额怎么分布", "local-sample")
    assert refused.status_code == 422 and "没有提到维度" in refused.json()["detail"]


def test_a_filter_the_question_does_not_mention_is_sent_back(client, monkeypatch):
    configure(client)
    calls = replies(monkeypatch, wide_plan(filters=[{"field": "sales_wide.status", "op": "=", "value": "已完成"}]), wide_plan())
    assert ask(client, "想了解销售额的水平", "local-sample").status_code == 200
    assert "没有提到" in calls[1]
    calls = replies(monkeypatch, wide_plan(filters=[{"field": "sales_wide.order_date", "op": "between", "value": ["2018-01-01", "2018-12-31"]}]), wide_plan())
    assert ask(client, "想了解销售额的水平如何", "local-sample").status_code == 200 and "时间范围" in calls[1]
    # Named by the question, a relative period included, they stay.
    calls = replies(monkeypatch, wide_plan(filters=[{"field": "sales_wide.order_date", "op": "between", "value": ["2018-01-01", "2018-12-31"]}]))
    assert ask(client, "想了解去年销售额的水平", "local-sample").status_code == 200 and len(calls) == 1
    calls = replies(monkeypatch, wide_plan(filters=[{"field": "sales_wide.region", "op": "!=", "value": "华东"}]))
    assert ask(client, "不看华东时销售额的水平", "local-sample").status_code == 200 and len(calls) == 1


QUARTER = [{"field": "sales_wide.order_date", "op": "between", "value": ["2018-01-01", "2018-12-31"]}]
MARCH = [{"field": "sales_wide.order_date", "op": "between", "value": ["2018-03-01", "2018-03-31"]}]


def test_a_stated_period_has_to_arrive_exactly(client, monkeypatch):
    """Seen with a real model: 2018年3月 按季度 came back filtered to the whole of 2018."""
    configure(client)
    grained = [{"field": "sales_wide.order_date", "grain": "quarter"}]
    calls = replies(monkeypatch, wide_plan(dimensions=grained, filters=QUARTER), wide_plan(dimensions=grained, filters=MARCH))
    answer = ask(client, "2018年3月不同按季度的销售额对比", "local-sample")
    assert answer.status_code == 200, answer.text
    assert "2018-03-01 到 2018-03-31" in calls[1] and answer.json()["semantic"]["filters"][0]["value"] == ["2018-03-01", "2018-03-31"]
    replies(monkeypatch, wide_plan(dimensions=grained, filters=QUARTER))
    assert ask(client, "2018年3月不同按季度的销售额对比", "local-sample").status_code == 422


def test_a_grouping_the_question_asks_for_cannot_be_dropped(client, monkeypatch):
    configure(client)
    monthly = [{"field": "sales_wide.order_date", "grain": "month"}]
    calls = replies(monkeypatch, wide_plan(), wide_plan(dimensions=monthly))
    assert ask(client, "想了解每月贡献的销售额", "local-sample").status_code == 200
    assert "缺少这个时间粒度" in calls[1]
    # 2018年第一季度 names a period, not a grouping, whatever it contains.
    calls = replies(monkeypatch, wide_plan(filters=[{"field": "sales_wide.order_date", "op": "between", "value": ["2018-01-01", "2018-03-31"]}]))
    assert ask(client, "想了解2018年第一季度的销售额", "local-sample").status_code == 200 and len(calls) == 1


def test_one_value_of_a_field_is_a_filter_not_a_group(client, monkeypatch):
    configure(client)
    region = [{"field": "sales_wide.region", "grain": None}]
    replies(monkeypatch, wide_plan(dimensions=[*region, {"field": "sales_wide.order_date", "grain": "month"}], filters=[{"field": "sales_wide.region", "op": "=", "value": "华南"}]))
    payload = ask(client, "想了解华南每月贡献的销售额", "local-sample").json()
    assert payload["columns"] == ["order_date", "total_sales"]
    # Several values are still compared side by side.
    replies(monkeypatch, wide_plan(dimensions=region, filters=[{"field": "sales_wide.region", "op": "in", "value": ["华南", "华东"]}]))
    assert ask(client, "想了解华南和华东贡献的销售额", "local-sample").json()["columns"] == ["region", "total_sales"]


def test_a_metric_wrapped_in_an_object_is_understood(client, monkeypatch):
    """Seen with a real model: {"name": "total_sales"} instead of the name."""
    configure(client)
    replies(monkeypatch, wide_plan(metrics=[{"name": "total_sales"}]))
    assert ask(client, "想了解销售额的水平", "local-sample").status_code == 200


def test_leaving_the_semantic_layer_takes_a_computation_it_cannot_express(client, monkeypatch):
    """Seen with a real model: 对比 was called derived, which skipped every check on the plan."""
    configure(client)
    generated = []

    def generate(self, question, dialect_label, source_label, schema_text):
        generated.append(question)
        return {"title": "t", "sql": "SELECT 1 AS one", "steps": [], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", generate)
    derived = wide_plan(status="derived", reason="需要对比。")
    regional = wide_plan(dimensions=[{"field": "sales_wide.region", "grain": None}])
    calls = replies(monkeypatch, derived, regional)
    answer = ask(client, "想了解不同地区的销售额对比", "local-sample")
    assert answer.status_code == 200, answer.text
    assert answer.json()["provider"] == "semantic" and generated == [] and "status 为 ok" in calls[1]
    # A model that insists gets no free SQL.
    replies(monkeypatch, derived)
    assert ask(client, "想了解不同地区的销售额对比情况", "local-sample").status_code == 422 and generated == []
    # A real computation still goes there.
    replies(monkeypatch, wide_plan(status="derived", reason="需要环比。"))
    ask(client, "销售额环比怎么样", "local-sample")
    assert generated


def test_free_sql_is_held_to_a_grain_the_semantic_model_declares(client, monkeypatch):
    """On demo data the key repeats too rarely for the data to tell; the declaration still does."""
    configure(client)
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("INSERT INTO t_lattice_orders VALUES (9001, 1, DATE '2018-03-05', '已完成')")
        con.execute("INSERT INTO t_lattice_order_items VALUES (9001, 1, 1, 100.00, DATE '2018-03-05'), (9001, 2, 2, 50.00, DATE '2018-03-05')")
        con.execute("INSERT INTO t_lattice_payments VALUES (9001, 150.00, '在线支付')")
        con.execute(WIDE_SALES_SQL)
    replies(monkeypatch, wide_plan(status="derived", reason="需要占比。"))
    naive = "SELECT region, SUM(payment_value) AS paid FROM dws_sales_wide GROUP BY region"
    reduced = "SELECT region, SUM(payment_value) AS paid FROM (SELECT DISTINCT order_id, region, payment_value FROM dws_sales_wide) t GROUP BY region"
    generated = []

    def generate(self, question, dialect_label, source_label, schema_text):
        generated.append(question)
        return {"title": "t", "sql": naive if len(generated) == 1 else reduced, "steps": [], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", generate)
    answer = ask(client, "各地区支付额的占比", "local-sample")
    assert answer.status_code == 200, answer.text
    assert len(generated) >= 2 and "重复累计" in generated[1]


def test_an_order_written_with_the_dataset_name_is_understood(client, monkeypatch):
    """Seen with a real model: 排序字段 sales_wide.order_date 不在查询结果中."""
    configure(client)
    replies(monkeypatch, wide_plan(dimensions=[{"field": "sales_wide.order_date", "grain": "month"}], order_by=[{"field": "sales_wide.order_date", "desc": False}]))
    answer = ask(client, "把销售额按每月列出来给我", "local-sample")
    assert answer.status_code == 200, answer.text
    # A field that is not in the result is dropped, not an error.
    replies(monkeypatch, wide_plan(dimensions=[{"field": "sales_wide.region", "grain": None}], order_by=[{"field": "sales_wide.status", "desc": True}]))
    assert ask(client, "把各地区的销售额列出来给我", "local-sample").status_code == 200


def test_saying_a_defined_word_is_missing_is_sent_back(client, monkeypatch):
    """Seen with a real model: 销售件数 was called undefined though it is a declared synonym."""
    configure(client)
    calls = replies(monkeypatch, wide_plan(status="unmapped", reason="未定义。", metrics=[]), wide_plan(metrics=["item_count"]))
    answer = ask(client, "想了解销售件数的水平", "local-sample")
    assert answer.status_code == 200, answer.text
    assert "item_count" in calls[1] and answer.json()["semantic"]["metrics"] == ["item_count"]
    # A concept that really is missing is still refused, after one try.
    calls = replies(monkeypatch, wide_plan(status="unmapped", reason="没有退款。", metrics=[]))
    assert ask(client, "退款金额怎么样", "local-sample").status_code == 422 and len(calls) == 1


# ----- 2. grains from the data -----------------------------------------------------------------
def undeclared_engine(client, rows_sql):
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("CREATE OR REPLACE TABLE lattice_verify_wide(item_id INTEGER, order_id INTEGER, customer_id INTEGER, region VARCHAR, category VARCHAR, price DECIMAL(10,2), amount DECIMAL(10,2))")
        con.execute(rows_sql)
    model = {
        "name": "m",
        "datasets": [{"name": "w", "source": "main.lattice_verify_wide", "primary_key": ["item_id"], "fields": [{"name": name, "datatype": kind, "expression": name} for name, kind in (("item_id", "Integer"), ("order_id", "Integer"), ("customer_id", "Integer"), ("region", "String"), ("category", "String"), ("price", "Decimal"), ("amount", "Decimal"))]}],
        "metrics": [{"name": "sales", "expression": "SUM(w.price)"}, {"name": "total", "expression": "SUM(w.amount)"}, {"name": "orders", "expression": "COUNT(DISTINCT w.order_id)"}],
    }
    queries = client.app.state.queries
    return SemanticQueryEngine(queries=queries, hidden_models={"m": model}), queries._record("local-sample")


PLENTY = "INSERT INTO lattice_verify_wide SELECT row_number() OVER (), o, o % 3, CASE o % 3 WHEN 0 THEN '华东' ELSE '华南' END, CASE k WHEN 1 THEN 'A' ELSE 'B' END, 10 + k, 100 + o FROM range(1, 11) a(o), range(1, 3) b(k)"


def test_undeclared_grain_is_found_and_the_measure_is_reduced(client):
    engine, record = undeclared_engine(client, PLENTY)
    payload = engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    assert "DISTINCT" in payload["sql"] and any("数据探测" in text for text in payload["semantic"]["warnings"])
    expected = {"华东": sum(100 + o for o in (3, 6, 9)), "华南": sum(100 + o for o in (1, 2, 4, 5, 7, 8, 10))}
    assert {row[0]: float(row[1]) for row in payload["rows"]} == expected


def test_undeclared_measure_by_a_finer_dimension_is_refused(client):
    engine, _ = undeclared_engine(client, PLENTY)
    with pytest.raises(SemanticQueryError, match="重复累计"):
        engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.category"}]})
    # A row-level measure and a distinct count stay computable by the same dimension.
    assert engine.query("m", {"metrics": ["sales"], "dimensions": [{"field": "w.category"}]})["rows"]
    counted = engine.query("m", {"metrics": ["orders"], "dimensions": [{"field": "w.category"}]})
    assert any("多个分组" in text for text in counted["semantic"]["warnings"])


def test_too_little_evidence_is_reported_not_ignored(client):
    engine, _ = undeclared_engine(client, "INSERT INTO lattice_verify_wide VALUES (1, 1, 1, '华东', 'A', 10, 100), (2, 1, 1, '华东', 'B', 20, 100), (3, 2, 1, '华东', 'A', 10, 70)")
    payload = engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    assert payload["verification"]["status"] == "skipped"
    assert any(check["name"] == "粒度待确认" and "w.amount" in check["detail"] for check in payload["verification"]["checks"])
    assert "DISTINCT" not in payload["sql"]  # Not acted on: it might be a real row-level amount.


def test_a_measure_constant_within_two_keys_is_refused_until_declared(client):
    """A fee charged monthly: reducing by customer would count one month instead of all."""
    engine, _ = undeclared_engine(client, "INSERT INTO lattice_verify_wide SELECT row_number() OVER (), o, o % 5, CASE o % 5 WHEN 0 THEN '华东' ELSE '华南' END, CASE k WHEN 1 THEN 'A' ELSE 'B' END, 10 + k, (o % 5) * 10 + 99 FROM range(1, 31) a(o), range(1, 3) b(k)")
    with pytest.raises(SemanticQueryError, match="无法确定指标 total 的统计粒度"):
        engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    # Measures with a settled grain on the same table stay available.
    assert engine.query("m", {"metrics": ["sales"], "dimensions": [{"field": "w.region"}]})["rows"]
    # A declaration settles it, and is what gets checked against the data.
    engine.hidden_models["m"]["datasets"][0]["fields"][-1]["ai_context"] = {"grain": ["order_id"]}
    engine.invalidate()
    payload = engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    expected = {"华东": 0.0, "华南": 0.0}
    for order in range(1, 31):
        expected["华东" if order % 5 == 0 else "华南"] += (order % 5) * 10 + 99
    assert {row[0]: float(row[1]) for row in payload["rows"]} == expected


def test_keys_are_listed_coarsest_first_and_a_constant_says_nothing():
    con = duckdb.connect()
    con.execute("CREATE TABLE t(order_id INTEGER, customer_id INTEGER, limit_amount INTEGER, order_amount INTEGER, qty INTEGER, price INTEGER)")
    con.execute("INSERT INTO t SELECT o, o % 6, (o % 6) * 10, o * 10, 1, o * 10 + k FROM range(1, 31) a(o), range(1, 3) b(k)")
    run = lambda statement: [list(row) for row in con.execute(statement).fetchall()]  # noqa: E731
    profile = profile_table(run, exp.to_table("t"), ["order_id", "customer_id", "limit_amount", "order_amount", "qty", "price"], "duckdb")
    assert profile.constant["limit_amount"] == [("customer_id",), ("order_id",)]
    assert profile.key_of("limit_amount") == ()  # Two keys fit: the data cannot say which.
    assert profile.key_of("order_amount") == ("order_id",)
    assert "qty" not in profile.constant and "price" not in profile.constant


def test_two_columns_together_can_be_the_key():
    """An order number restarts per tenant, so neither column alone identifies an order."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t(tenant_id INTEGER, order_no INTEGER, amount INTEGER, price INTEGER)")
    con.execute("INSERT INTO t SELECT t, o, t * 100 + o, k * 3 + o FROM range(1, 4) a(t), range(1, 11) b(o), range(1, 3) c(k)")
    run = lambda statement: [list(row) for row in con.execute(statement).fetchall()]  # noqa: E731
    profile = profile_table(run, exp.to_table("t"), ["tenant_id", "order_no", "amount", "price"], "duckdb")
    assert profile.key_of("amount") == ("tenant_id", "order_no")
    assert "price" not in profile.constant


def test_a_column_merely_shaped_like_a_key_is_reported_and_never_acted_on():
    """单据 identifies orders, but tax is also constant within amount: acting on such a finding
    could merge different orders, so only a key named as one is trusted."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t(ref VARCHAR, region VARCHAR, order_total INTEGER, price INTEGER)")
    con.execute("INSERT INTO t SELECT 'R' || o, 'A', o * 10, k * 3 + o FROM range(1, 31) a(o), range(1, 3) b(k)")
    run = lambda statement: [list(row) for row in con.execute(statement).fetchall()]  # noqa: E731
    columns = ["ref", "region", "order_total", "price"]
    kinds = {"ref": "VARCHAR", "region": "VARCHAR", "order_total": "INTEGER", "price": "INTEGER"}
    profile = profile_table(run, exp.to_table("t"), columns, "duckdb", kinds=kinds)
    assert "order_total" not in profile.constant and profile.uncertain["order_total"] == [("ref",)]
    # A low-cardinality column is not taken for a key at all, and a measure's name rules it out.
    assert ("region",) not in profile.keys and ("order_total",) not in profile.keys


def test_composite_key_reduces_a_measure_in_the_semantic_layer(client):
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("CREATE OR REPLACE TABLE lattice_verify_wide(tenant_id INTEGER, order_no INTEGER, region VARCHAR, price INTEGER, amount INTEGER)")
        con.execute("INSERT INTO lattice_verify_wide SELECT t, o, CASE t WHEN 1 THEN '华东' ELSE '华南' END, k * 3 + o, t * 100 + o FROM range(1, 4) a(t), range(1, 11) b(o), range(1, 3) c(k)")
    model = {
        "name": "m",
        "datasets": [{"name": "w", "source": "main.lattice_verify_wide", "fields": [{"name": name, "datatype": kind, "expression": name} for name, kind in (("tenant_id", "Integer"), ("order_no", "Integer"), ("region", "String"), ("price", "Integer"), ("amount", "Integer"))]}],
        "metrics": [{"name": "total", "expression": "SUM(w.amount)"}],
    }
    engine = SemanticQueryEngine(queries=client.app.state.queries, hidden_models={"m": model})
    payload = engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    assert {row[0]: float(row[1]) for row in payload["rows"]} == {"华东": float(sum(100 + o for o in range(1, 11))), "华南": float(sum(200 + o for o in range(1, 11)) + sum(300 + o for o in range(1, 11)))}


def test_free_sql_reports_a_measure_constant_within_two_keys(client, tmp_path, monkeypatch):
    configure(client)
    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE dws_fee(order_id INTEGER, customer_id INTEGER, fee DECIMAL(10,2))")
        con.execute("INSERT INTO dws_fee SELECT o, o % 5, (o % 5) * 10 + 99 FROM range(1, 31) a(o), range(1, 3) b(k)")
    source_id = source(client, path)
    statement = "SELECT SUM(fee) AS total FROM (SELECT DISTINCT order_id, fee FROM dws_fee) AS t"
    script(monkeypatch, tables=["dws_fee"], statements=["SELECT SUM(fee) AS total FROM dws_fee", statement])
    answer = ask(client, "费用合计", source_id)
    assert answer.status_code == 200, answer.text


def test_free_sql_reports_a_measure_it_cannot_settle(client, tmp_path, monkeypatch):
    configure(client)
    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE dws_few(order_id INTEGER, amount DECIMAL(10,2))")
        con.execute("INSERT INTO dws_few VALUES (1, 5), (1, 5), (2, 6), (3, 7)")
    source_id = source(client, path)
    script(monkeypatch, tables=["dws_few"], statements=["SELECT SUM(amount) AS a FROM dws_few"])
    payload = ask(client, "金额合计", source_id).json()
    assert MIN_REPEATED_KEYS > 1 and payload["verification"]["status"] == "skipped"
    assert any("无法判断" in check["detail"] for check in payload["verification"]["checks"])


# ----- 3. a wide table against its detail --------------------------------------------------------
def inject(client):
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("INSERT INTO t_lattice_orders VALUES (9001, 1, DATE '2018-03-05', '已完成')")
        con.execute("INSERT INTO t_lattice_order_items VALUES (9001, 1, 1, 100.00, DATE '2018-03-05'), (9001, 2, 2, 50.00, DATE '2018-03-05')")
        con.execute("INSERT INTO t_lattice_payments VALUES (9001, 90.00, '在线支付'), (9001, 60.00, '银行转账')")
        con.execute(WIDE_SALES_SQL)


def check_named(payload):
    return next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细核对")


def test_counts_and_ratios_are_compared_too(client):
    inject(client)
    for question in ("各地区的订单量", "各地区的客单价"):
        payload = ask(client, question, "local-sample").json()
        assert payload["semantic"]["route"] == "wide"
        assert check_named(payload)["status"] == "passed" and "3 个分组" in check_named(payload)["detail"]


def test_rows_in_the_wrong_group_are_caught_even_when_the_total_agrees(client):
    inject(client)
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("UPDATE dws_sales_wide SET region = '华东' WHERE order_id = 9001")
    payload = ask(client, "各地区的销售额", "local-sample").json()
    assert payload["semantic"]["route"] == "join" and check_named(payload)["status"] == "skipped"
    assert "不一致" in payload["semantic"]["route_note"]


def test_a_stale_distinct_count_is_caught(client):
    inject(client)
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("DELETE FROM dws_sales_wide WHERE order_id = 9001")
    payload = ask(client, "各地区的订单量", "local-sample").json()
    assert payload["semantic"]["route"] == "join" and check_named(payload)["status"] == "skipped"


def test_a_plan_from_the_language_model_is_compared_too(client, monkeypatch):
    configure(client)
    inject(client)
    plan = {"status": "ok", "reason": "", "title": "规模", "metrics": ["total_sales"], "dimensions": [{"field": "sales_wide.region", "grain": None}], "filters": [], "order_by": [], "limit": None, "steps": []}
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: plan)
    payload = ask(client, "随便瞧瞧大区的销售额", "local-sample").json()
    assert payload["semantic"]["planner"] == "llm" and payload["semantic"]["route"] == "wide" and check_named(payload)["status"] == "passed"


def test_with_no_detail_model_the_result_says_so(client):
    engine = client.app.state.semantic_engine
    client.app.state.queries.semantic = SemanticQueryEngine(queries=client.app.state.queries, builtin_wide=engine.builtin_wide)
    payload = ask(client, "各地区的销售额", "local-sample").json()
    check = check_named(payload)
    assert check["status"] == "info" and "只来自宽表" in check["detail"] and payload["verification"]["status"] == "passed"


def wide_only(path):
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE dws_alone(order_id INTEGER, region VARCHAR, amount DECIMAL(10,2), created DATE)")
        con.execute("INSERT INTO dws_alone SELECT o, CASE WHEN o % 10 = 0 THEN NULL ELSE '华东' END, o * 3, DATE '2024-05-01' + o::INTEGER FROM range(1, 41) t(o)")
    return path


def checks_of(payload, name):
    return [item for item in payload["verification"]["checks"] if item["name"] == name]


def test_a_wide_table_with_no_detail_reports_itself_and_says_it_is_not_a_comparison(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, wide_only(tmp_path / "w.duckdb"))
    script(monkeypatch, tables=["dws_alone"], statements=["SELECT region, SUM(amount) AS a FROM dws_alone GROUP BY region"])
    payload = ask(client, "各地区金额", source_id).json()
    cross = checks_of(payload, "宽表与明细交叉验证")[0]
    assert cross["status"] == "info" and "不等于" in cross["detail"]
    assert any("空值" in item["detail"] and "region" in item["detail"] for item in checks_of(payload, "宽表自检"))
    assert any("2024-06-10" in item["detail"] for item in checks_of(payload, "数据新鲜度"))


def test_rows_written_twice_are_caught_with_nothing_to_compare_with(client, tmp_path, monkeypatch):
    configure(client)
    path = wide_only(tmp_path / "w.duckdb")
    with duckdb.connect(str(path)) as con:
        con.execute("INSERT INTO dws_alone SELECT * FROM dws_alone WHERE order_id < 5")
    source_id = source(client, path)
    script(monkeypatch, tables=["dws_alone"], statements=["SELECT region, SUM(amount) AS a FROM dws_alone GROUP BY region"])
    payload = ask(client, "各地区金额", source_id).json()
    assert payload["verification"]["status"] == "skipped"
    assert any("重复行" in item["detail"] and item["status"] == "skipped" for item in checks_of(payload, "宽表自检"))


def test_semantic_wide_model_with_no_detail_runs_the_self_check(client):
    engine = client.app.state.semantic_engine
    queries = client.app.state.queries
    queries.semantic = SemanticQueryEngine(queries=queries, builtin_wide=engine.builtin_wide)
    payload = ask(client, "各地区的销售额", "local-sample").json()
    assert checks_of(payload, "数据新鲜度") and "不等于" in check_named(payload)["detail"]
    with duckdb.connect(str(client.app.state.store.db)) as con:
        con.execute("INSERT INTO dws_sales_wide SELECT * FROM dws_sales_wide LIMIT 3")
    queries.semantic.invalidate()
    again = ask(client, "各地区的销售额", "local-sample").json()
    assert again["verification"]["status"] == "skipped" and any(item["status"] == "skipped" for item in checks_of(again, "宽表自检"))


def test_a_grain_that_could_not_be_read_is_reported_not_taken_for_a_clean_table(client):
    engine, _ = undeclared_engine(client, PLENTY)
    queries = client.app.state.queries
    real = queries.execute

    def flaky(record, sql, limit=None, *, refresh=False):
        if "lattice_groups" in sql or "lattice_rows" in sql:
            from webapi.questions import QueryError

            raise QueryError(400, "操作超过 30 秒未完成，已取消。")
        return real(record, sql, limit, refresh=refresh)

    queries.execute = flaky
    try:
        payload = engine.query("m", {"metrics": ["total"], "dimensions": [{"field": "w.region"}]})
    finally:
        queries.execute = real
    assert payload["verification"]["status"] == "skipped"
    assert any("未能探测" in check["detail"] for check in payload["verification"]["checks"])


def test_free_sql_says_when_a_wide_table_could_not_be_profiled(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, wide_only(tmp_path / "w.duckdb"))
    script(monkeypatch, tables=["dws_alone"], statements=["SELECT SUM(amount) AS a FROM dws_alone"])
    queries = client.app.state.queries
    real = queries.execute

    def flaky(record, sql, limit=None, *, refresh=False):
        if "lattice_groups" in sql or '"lattice_rows"' in sql:
            from webapi.questions import QueryError

            raise QueryError(400, "操作超过 30 秒未完成，已取消。")
        return real(record, sql, limit, refresh=refresh)

    queries.execute = flaky
    try:
        payload = ask(client, "金额合计", source_id).json()
    finally:
        queries.execute = real
    assert any("未能探测" in check["detail"] for check in payload["verification"]["checks"])


def test_physical_wide_table_is_recomputed_by_the_compiler_without_a_model(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    calls = script(monkeypatch, statements=[BY_PRICE])
    payload = ask(client, "各地区商品价格合计", source_id).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    assert check["status"] == "passed" and "确定性" in check["detail"]
    assert len(calls["sql"]) == 1  # The second statement did not come from the model.


def test_reduced_measure_and_filter_are_restated_too(client, tmp_path, monkeypatch):
    configure(client)
    path = warehouse(tmp_path / "w.duckdb")
    source_id = source(client, path)
    script(monkeypatch, statements=[BY_AMOUNT])
    check = next(item for item in ask(client, "各地区订单金额", source_id).json()["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    assert check["status"] == "passed" and "确定性" in check["detail"]


def test_compiled_second_path_disagreeing_is_shown_with_both_numbers(client, tmp_path, monkeypatch):
    configure(client)
    path = warehouse(tmp_path / "w.duckdb")
    with duckdb.connect(str(path)) as con:
        con.execute("UPDATE dws_order_wide SET item_price = item_price + 100 WHERE order_id = 1")
    source_id = source(client, path)
    script(monkeypatch, statements=[BY_PRICE])
    payload = ask(client, "各地区商品价格合计", source_id).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    assert check["status"] == "skipped" and "不一致" in check["detail"] and payload["verification"]["status"] == "skipped"


def test_a_key_on_several_tables_is_read_from_the_table_it_identifies(client, tmp_path, monkeypatch):
    """Seen on every engine: counting order_id on payments counted only the paid orders, and
    a correct answer was reported as disagreeing with the detail."""
    configure(client)
    path = warehouse(tmp_path / "w.duckdb")
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE payments(payment_id INTEGER, order_id INTEGER, paid DECIMAL(10,2))")
        con.execute("INSERT INTO payments VALUES (1, 1, 5), (2, 2, 5)")
    source_id = source(client, path)
    script(monkeypatch, statements=["SELECT region, COUNT(DISTINCT order_id) AS n FROM dws_order_wide GROUP BY region"])
    payload = ask(client, "各地区的订单数", source_id).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    assert check["status"] == "passed" and "确定性" in check["detail"]


def test_a_column_that_cannot_be_placed_is_not_compared_on_a_guess():
    from webapi.questions import detail_statement
    from webapi.schema_linking import JoinEdge, TableInfo

    table = lambda name, *columns: TableInfo(None, name, columns=[{"name": column, "type": "INT"} for column in columns])  # noqa: E731
    wide = table("dws_w", "region", "amount")
    details = [table("a", "id", "amount"), table("b", "id", "a_id", "amount")]
    edges = [JoinEdge("b", ["a_id"], "a", ["id"], "外键")]
    assert detail_statement("SELECT SUM(amount) AS s FROM dws_w", wide, details, edges, "duckdb") is None


def test_a_longer_outage_is_ridden_out_before_giving_up(monkeypatch):
    import webapi.questions as questions

    monkeypatch.setattr(questions.time, "sleep", lambda seconds: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LlmError("无法连接模型接口")
        return "ok"

    assert questions.retrying(flaky) == "ok" and len(calls) == 3
    # A rejected key is not a blip, and is reported at once.
    denied = []

    def rejected():
        denied.append(1)
        raise LlmError("模型接口拒绝了 API Key（HTTP 401）")

    with pytest.raises(LlmError):
        questions.retrying(rejected)
    assert len(denied) == 1


ELABORATE = "SELECT region, SUM(CASE WHEN item_price > 0 THEN item_price END) AS sales FROM dws_order_wide GROUP BY region"


def test_elaborate_query_falls_back_to_a_model_and_says_it_is_weaker(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    detail = "SELECT c.region, SUM(i.item_price) AS sales FROM order_items i JOIN orders o ON i.order_id = o.order_id JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.region"
    calls = script(monkeypatch, statements=[ELABORATE, detail])
    payload = ask(client, "各地区商品价格合计", source_id).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    # Agreement between two model-written statements is not a pass.
    assert check["status"] == "info" and "相同的理解错误" in check["detail"] and "不要使用宽表 dws_order_wide" in calls["sql"][1]["question"]


def test_second_path_that_keeps_using_the_wide_table_is_not_counted(client, tmp_path, monkeypatch):
    configure(client)
    source_id = source(client, warehouse(tmp_path / "w.duckdb"))
    script(monkeypatch, statements=[ELABORATE])
    payload = ask(client, "各地区商品价格合计", source_id).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细交叉验证")
    assert check["status"] == "info" and payload["verification"]["status"] == "passed"


def test_compare_results_matches_rows_by_label_not_position():
    base = {"rows": [["华东", 1.0, 2.0], ["华南", 3.0, 4.0]]}
    assert compare_results(base, {"rows": [["华南", 4.0, 3.0], ["华东", 2.0, 1.0]]}) == ""
    assert "华东" in compare_results(base, {"rows": [["华东", 1.0, 9.0], ["华南", 3.0, 4.0]]})
    assert "分组不同" in compare_results(base, {"rows": [["华北", 1.0, 2.0], ["华南", 3.0, 4.0]]})
    assert compare_results({**base, "truncated": True}, base) is None
    # Hive returns decimals as text: the same number, not another group.
    assert compare_results(base, {"rows": [["华东", "1.00", "2"], ["华南", "3.0", 4]]}) == ""
    assert compare_results(base, {"rows": [["华东", 1.0], ["华南", 3.0]]}) is None


# ----- 4. the verification data -----------------------------------------------------------------
def test_verification_tables_really_have_one_to_many_rows_and_stay_out_of_questions(client):
    tables = {table["name"] for table in client.app.state.store.metadata()}
    assert not tables & set(VERIFY_TABLES)
    with duckdb.connect(str(client.app.state.store.db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM (SELECT order_id FROM lattice_verify_items GROUP BY 1 HAVING count(*) > 1)").fetchone()[0] > 20
        assert con.execute("SELECT count(*) FROM (SELECT order_id FROM lattice_verify_payments GROUP BY 1 HAVING count(*) > 1)").fetchone()[0] > 5
        naive = con.execute("SELECT sum(order_amount) FROM lattice_verify_wide").fetchone()[0]
        real = con.execute("SELECT sum(order_amount) FROM lattice_verify_orders").fetchone()[0]
    assert naive > real
    queries = client.app.state.queries
    assert not [model for model in queries.semantic.models_for(queries._record("local-sample")) if "verify" in model.name]


def test_hidden_models_answer_by_exact_reference(client):
    amount = client.post("/api/metrics/query", json={"model": "builtin:verify-wide", "metrics": ["amount"], "dimensions": [{"field": "w.region"}]})
    assert amount.status_code == 200, amount.text
    star = client.post("/api/metrics/query", json={"model": "builtin:verify-star", "metrics": ["amount"], "dimensions": [{"field": "customers.region"}]}).json()
    assert sorted(amount.json()["rows"]) == sorted(star["rows"])
    assert client.post("/api/metrics/query", json={"model": "builtin:verify-wide", "metrics": ["amount"], "dimensions": [{"field": "w.category"}]}).status_code == 400
    assert BUILTIN_WIDE != "builtin:verify-wide"
