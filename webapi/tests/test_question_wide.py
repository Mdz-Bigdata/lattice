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
"""Wide tables first, joins for the rest, and the same number either way.

Order 9001 holds two items and two payments, so its payment total sits on both of its rows
in the wide table. Expected values come from one base table at a time.
"""

import duckdb
import pytest
import sqlglot
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.app import wide_model_document
from webapi.llm import SqlGenerator
from webapi.query import WIDE_SALES_SQL
from webapi.schema_linking import TableInfo, link_schema
from webapi.semantic_query import BUILTIN_WIDE, Compiler, SemanticQueryError, extract_document, parse_model


def connect(client):
    return duckdb.connect(str(client.app.state.store.db))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    for name in ("LATTICE_LLM_PROVIDER", "LATTICE_LLM_API_KEY", "LATTICE_LLM_MODEL", "LATTICE_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as test_client:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        with connect(test_client) as con:
            con.execute("INSERT INTO t_lattice_orders VALUES (9001, 1, DATE '2018-03-05', '已完成')")
            con.execute("INSERT INTO t_lattice_order_items VALUES (9001, 1, 1, 100.00, DATE '2018-03-05'), (9001, 2, 2, 50.00, DATE '2018-03-05')")
            con.execute("INSERT INTO t_lattice_payments VALUES (9001, 90.00, '在线支付'), (9001, 60.00, '银行转账')")
            con.execute(WIDE_SALES_SQL)
        yield test_client


def truth(client, sql):
    with duckdb.connect(str(client.app.state.store.db), read_only=True) as con:
        return con.execute(sql).fetchall()


def ask(client, question):
    return client.post("/api/query", json={"question": question, "datasource_id": "local-sample", "refresh": True})


def as_map(payload, key, metric):
    return {row[payload["columns"].index(key)]: float(row[payload["columns"].index(metric)]) for row in payload["rows"]}


PAYMENT_BY_REGION = """SELECT c.region, sum(p.payment_value)::DOUBLE FROM t_lattice_payments p
    JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) GROUP BY 1"""
SALES_BY_REGION = """SELECT c.region, sum(i.price)::DOUBLE FROM t_lattice_order_items i
    JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) GROUP BY 1"""


def test_wide_table_answers_first_with_no_join_and_agrees_with_the_detail(client):
    answer = ask(client, "各地区的销售额")
    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["semantic"]["route"] == "wide" and payload["semantic"]["model"] == "lattice_demo_sales_wide"
    assert "JOIN" not in payload["sql"] and "dws_sales_wide" in payload["sql"]
    assert as_map(payload, "region", "total_sales") == pytest.approx(dict(truth(client, SALES_BY_REGION)))
    checks = {check["name"]: check for check in payload["verification"]["checks"]}
    assert checks["宽表与明细核对"]["status"] == "passed" and payload["verification"]["status"] == "passed"


def test_repeated_measure_is_counted_once_per_order(client):
    payload = ask(client, "各地区的支付额").json()
    assert payload["semantic"]["route"] == "wide"
    expected = dict(truth(client, PAYMENT_BY_REGION))
    assert as_map(payload, "region", "total_payment") == pytest.approx(expected)
    # A plain SUM over the wide table really is wrong on this data.
    naive = dict(truth(client, "SELECT region, sum(payment_value)::DOUBLE FROM dws_sales_wide GROUP BY 1"))
    assert any(naive[region] != pytest.approx(expected[region]) for region in expected)
    assert any("去重" in text for text in payload["semantic"]["warnings"])
    assert any(check["name"] == "声明粒度核对" and check["status"] == "passed" for check in payload["verification"]["checks"])


def test_row_level_and_order_level_measures_combine(client):
    payload = ask(client, "各地区的销售额和支付额").json()
    assert payload["semantic"]["route"] == "wide" and "UNION ALL" in payload["sql"]
    assert as_map(payload, "region", "total_sales") == pytest.approx(dict(truth(client, SALES_BY_REGION)))
    assert as_map(payload, "region", "total_payment") == pytest.approx(dict(truth(client, PAYMENT_BY_REGION)))


def test_order_level_measure_by_item_level_dimension_is_refused(client):
    answer = ask(client, "各商品类别的支付额")
    assert answer.status_code == 400 and "重复累计" in answer.json()["detail"]


def test_distinct_count_by_item_level_dimension_warns(client):
    payload = ask(client, "各类别订单量").json()
    assert payload["semantic"]["route"] == "wide" and any("多个分组" in text for text in payload["semantic"]["warnings"])
    assert as_map(payload, "category", "order_count") == pytest.approx({key: float(value) for key, value in truth(client, """SELECT p.category, count(DISTINCT i.order_id)
        FROM t_lattice_order_items i JOIN t_lattice_products p USING (product_id) GROUP BY 1""")})


def test_item_level_filter_on_an_order_level_measure_says_what_it_means(client):
    payload = ask(client, "家居生活类别的支付额").json()
    assert any("更细" in text for text in payload["semantic"]["warnings"])


def test_ratio_metric_matches_the_detail(client):
    payload = ask(client, "各地区的客单价").json()
    assert payload["semantic"]["route"] == "wide"
    expected = {key: round(float(value), 2) for key, value in truth(client, """SELECT c.region, sum(i.price) / count(DISTINCT o.order_id) FROM t_lattice_order_items i
        JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) GROUP BY 1""")}
    assert as_map(payload, "region", "avg_order_value") == pytest.approx(expected)


def test_what_the_wide_table_lacks_goes_to_the_joined_tables(client):
    # The wide table has no payment type: one table holds it.
    payload = ask(client, "各支付方式的支付额").json()
    assert payload["semantic"]["route"] == "single" and payload["semantic"]["model"] == "lattice_demo_sales"
    assert "优先查询宽表" in payload["semantic"]["route_note"] and any("优先查询宽表" in step for step in payload["steps"])
    assert "lattice_demo_sales_wide" in payload["semantic"]["route_note"] and "支付方式" in payload["semantic"]["route_note"]
    assert as_map(payload, "payment_type", "total_payment") == pytest.approx(dict(truth(client, "SELECT payment_type, sum(payment_value)::DOUBLE FROM t_lattice_payments GROUP BY 1")))
    # With a filter on the customer's region, it takes a real multi-table join.
    joined = ask(client, "华南地区各支付方式的支付额")
    assert joined.status_code == 200, joined.text
    assert joined.json()["semantic"]["route"] == "join" and "JOIN" in joined.json()["sql"]
    assert as_map(joined.json(), "payment_type", "total_payment") == pytest.approx(dict(truth(client, """SELECT p.payment_type, sum(p.payment_value)::DOUBLE FROM t_lattice_payments p
        JOIN t_lattice_orders o USING (order_id) JOIN t_lattice_customers c USING (customer_id) WHERE c.region = '华南' GROUP BY 1""")))


def test_note_names_the_wide_table_the_question_came_closest_to(client, monkeypatch):
    """An unrelated single-table model on the same source must not be named, or answer."""
    engine = client.app.state.semantic_engine
    other = parse_model({"name": "unrelated", "datasets": [{"name": "t_lattice_orders", "source": "t_lattice_orders", "fields": [{"name": "status", "datatype": "String", "expression": "status"}]}], "metrics": [{"name": "n", "expression": "COUNT(t_lattice_orders.order_id)"}]}, model_id="unrelated")
    real = engine.models_for
    monkeypatch.setattr(engine, "models_for", lambda record, question="": [other, *real(record)])
    monkeypatch.setattr(engine, "load_model", lambda ref, original=engine.load_model: other if ref == "unrelated" else original(ref))
    payload = ask(client, "华南地区各支付方式的支付额").json()
    assert "lattice_demo_sales_wide" in payload["semantic"]["route_note"] and "unrelated" not in payload["semantic"]["route_note"]
    assert ask(client, "各地区的销售额").json()["semantic"]["model"] == "lattice_demo_sales_wide"


def test_stale_wide_table_gives_way_to_the_detail(client):
    with connect(client) as con:
        con.execute("INSERT INTO t_lattice_orders VALUES (9100, 1, DATE '2018-05-05', '已完成')")
        con.execute("INSERT INTO t_lattice_order_items VALUES (9100, 1, 1, 1000.00, DATE '2018-05-05')")
    payload = ask(client, "各地区的销售额").json()
    assert payload["semantic"]["route"] == "join" and "不一致" in payload["semantic"]["route_note"]
    assert as_map(payload, "region", "total_sales") == pytest.approx(dict(truth(client, SALES_BY_REGION)))
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "宽表与明细核对")
    assert check["status"] == "skipped" and "1,000.00" in check["detail"].replace("-1,000.00", "1,000.00")
    assert payload["verification"]["status"] == "skipped"


def test_declared_grain_the_data_contradicts_returns_no_number(client):
    with connect(client) as con:
        con.execute("UPDATE dws_sales_wide SET payment_value = payment_value + 1 WHERE order_id = 9001 AND product_id = 1")
    engine = client.app.state.semantic_engine
    with pytest.raises(SemanticQueryError, match="声明"):
        engine.query(BUILTIN_WIDE, {"metrics": ["total_payment"], "dimensions": [{"field": "sales_wide.region"}]})


def test_wide_model_is_valid_listed_first_and_fingerprints_its_grains(client):
    tables = client.app.state.store.metadata()
    document = wide_model_document(tables)
    import yaml
    assert client.post("/api/validate", json={"yaml": yaml.safe_dump(document, allow_unicode=True)}).json()["valid"]
    queries = client.app.state.queries
    assert [model.name for model in queries.semantic.models_for(queries._record("local-sample"))][:2] == ["lattice_demo_sales_wide", "lattice_demo_sales"]
    original = parse_model(extract_document(document))
    document["semantic_model"][0]["datasets"][0]["fields"][-1]["ai_context"]["grain"] = ["customer_id"]
    assert parse_model(extract_document(document)).fingerprint() != original.fingerprint()
    assert wide_model_document([]) is None


def test_mixing_grains_inside_one_metric_is_refused():
    document = {
        "name": "m",
        "datasets": [{"name": "w", "source": "w", "fields": [
            {"name": "price", "datatype": "Decimal", "expression": "price"},
            {"name": "amount", "datatype": "Decimal", "expression": "amount", "ai_context": {"grain": ["order_id"]}},
        ]}],
        "metrics": [{"name": "ratio", "expression": "SUM(w.price) / SUM(w.amount)"}],
    }
    with pytest.raises(SemanticQueryError, match="不同粒度"):
        Compiler(parse_model(document), "duckdb").compile({"metrics": ["ratio"]})


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "mysql", "clickhouse", "starrocks", "doris", "hive"])
def test_reduced_measures_compile_for_every_engine(client, dialect):
    model = client.app.state.semantic_engine.load_model(BUILTIN_WIDE)
    compiled = Compiler(model, dialect, bare_schemas=("main",)).compile({"metrics": ["total_payment"], "dimensions": [{"field": "sales_wide.order_date", "grain": "month"}], "filters": [{"field": "sales_wide.status", "op": "=", "value": "已完成"}], "limit": 3})
    assert "DISTINCT" in compiled.sql.upper() and compiled.grain_checks
    sqlglot.parse_one(compiled.sql, read=dialect)


# ----- a physical wide table with no semantic model ---------------------------------------------
def wide_source(client, tmp_path):
    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE dws_order_wide(order_id INTEGER, product VARCHAR, region VARCHAR, price DECIMAL(10,2), order_amount DECIMAL(10,2), quantity INTEGER)")
        for order in range(1, 9):
            con.execute(f"INSERT INTO dws_order_wide VALUES ({order}, 'A', '华东', 10, {order * 100}, 1), ({order}, 'B', '华东', 20, {order * 100}, 1)")
        con.execute("CREATE TABLE sparse_wide(order_id INTEGER, amount DECIMAL(10,2))")
        con.execute("INSERT INTO sparse_wide VALUES (1, 5), (1, 5), (2, 6), (3, 7)")
        for index in range(10):
            con.execute(f"CREATE TABLE stg_{index}(a INTEGER)")
    return client.post("/api/datasources", json={"name": "宽表库", "type": "duckdb", "config": {"path": str(path)}}).json()["id"]


def configure(client):
    assert client.post("/api/llm", json={"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}).status_code == 200


def generator_replies(monkeypatch, statements):
    calls = []

    def fake(self, question, dialect_label, source_label, schema_text):
        calls.append({"question": question, "schema": schema_text})
        sql = statements[min(len(calls) - 1, len(statements) - 1)]
        return {"title": "结果", "sql": sql, "steps": ["思路"], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", fake)
    return calls


def test_free_sql_is_told_about_and_held_to_repeated_measures(client, tmp_path, monkeypatch):
    configure(client)
    source_id = wide_source(client, tmp_path)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"tables": ["dws_order_wide"]})
    naive = "SELECT region, SUM(order_amount) AS amount FROM dws_order_wide GROUP BY region"
    reduced = "SELECT region, SUM(order_amount) AS amount FROM (SELECT DISTINCT order_id, region, order_amount FROM dws_order_wide) AS t GROUP BY region"
    calls = generator_replies(monkeypatch, [naive, reduced])
    answer = client.post("/api/query", json={"question": "各地区订单金额", "datasource_id": source_id})
    assert answer.status_code == 200, answer.text
    assert "重复度量" in calls[0]["schema"] and "order_amount：按 order_id 去重" in calls[0]["schema"] and "宽表" in calls[0]["schema"]
    assert "重复累计" in calls[1]["question"]
    assert answer.json()["rows"] == [["华东", 3600.0]]


def test_a_row_level_measure_and_a_constant_are_not_mistaken_for_repeats(client, tmp_path, monkeypatch):
    configure(client)
    source_id = wide_source(client, tmp_path)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"tables": ["dws_order_wide"]})
    calls = generator_replies(monkeypatch, ["SELECT region, SUM(price) AS p, SUM(quantity) AS q FROM dws_order_wide GROUP BY region"])
    answer = client.post("/api/query", json={"question": "各地区价格和数量", "datasource_id": source_id})
    assert answer.status_code == 200 and len(calls) == 1 and answer.json()["rows"] == [["华东", 240.0, 16.0]]


def test_too_few_repeated_keys_are_not_evidence(client, tmp_path, monkeypatch):
    configure(client)
    source_id = wide_source(client, tmp_path)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"tables": ["sparse_wide"]})
    calls = generator_replies(monkeypatch, ["SELECT SUM(amount) AS a FROM sparse_wide"])
    assert client.post("/api/query", json={"question": "金额合计", "datasource_id": source_id}).status_code == 200 and len(calls) == 1


def test_free_sql_shows_which_measure_the_model_chose(client, tmp_path, monkeypatch):
    """Nothing can tell whether 销售额 meant price or order_amount, so a person must see it."""
    configure(client)
    source_id = wide_source(client, tmp_path)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"tables": ["dws_order_wide"]})
    generator_replies(monkeypatch, ["SELECT region, SUM(price) AS sales FROM dws_order_wide GROUP BY region"])
    payload = client.post("/api/query", json={"question": "各地区价格", "datasource_id": source_id}).json()
    check = next(item for item in payload["verification"]["checks"] if item["name"] == "模型选用的度量")
    assert check["status"] == "info" and "SUM(price)" in check["detail"] and payload["verification"]["governed"] is False
    assert payload["verification"]["status"] == "passed"


def test_malformed_json_from_the_model_is_asked_for_again(client, tmp_path, monkeypatch):
    """A real reply: a stray quote after the steps array."""
    from webapi.llm import LlmError, extract_json

    configure(client)
    source_id = wide_source(client, tmp_path)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"tables": ["dws_order_wide"]})
    calls = []

    def flaky(self, question, dialect_label, source_label, schema_text):
        calls.append(question)
        if len(calls) == 1:
            extract_json('{"title":"t","sql":"SELECT 1","steps":["a"]","dimension":""}')
        return {"title": "结果", "sql": "SELECT COUNT(DISTINCT order_id) AS n FROM dws_order_wide", "steps": [], "dimension": "", "metric": "", "chart_type": "table"}

    monkeypatch.setattr(SqlGenerator, "generate", flaky)
    assert client.post("/api/query", json={"question": "订单数", "datasource_id": source_id}).json()["rows"] == [[8]] and len(calls) == 2
    # A multi-line SQL string with raw line breaks is valid enough to read.
    assert extract_json('{"sql":"SELECT\n1"}')["sql"] == "SELECT\n1"
    with pytest.raises(LlmError):
        extract_json('{"steps":["a"]","x":1}')


def test_link_ranks_a_wide_table_above_a_table_matching_equally():
    table = lambda name, *columns: TableInfo(None, name, columns=[{"name": column, "type": "INT"} for column in columns])
    narrow = table("sales", "amount")
    wide = table("dws_sales", "amount")
    assert wide.is_wide and not narrow.is_wide and table("many", *[f"c{i}" for i in range(12)]).is_wide
    link = link_schema("sales amount", [narrow, wide, *[table(f"noise{i}", "x") for i in range(8)]], [], top_k=1)
    assert link.tables[0].name == "dws_sales"
