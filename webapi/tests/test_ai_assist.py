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

"""AI 增强: rule suggestions, semantic model drafts and lineage root-cause analysis."""

import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.ai_assist import AiAssistError, AiAssistService, sql_datatype
from webapi.llm import LlmError, SqlGenerator

COLUMNS = [
    {"name": "order_id", "type": "INTEGER", "nullable": False, "primary_key": True},
    {"name": "customer_id", "type": "INTEGER", "nullable": True},
    {"name": "amount", "type": "DECIMAL(16,2)", "nullable": True},
    {"name": "email", "type": "VARCHAR", "nullable": True},
    {"name": "status", "type": "VARCHAR", "nullable": True},
    {"name": "updated_at", "type": "TIMESTAMP", "nullable": True},
]


class FakeConnector:
    dialect = "postgres"
    type_id = "postgresql"

    def __init__(self, tables):
        self.tables = tables

    def default_schema(self):
        return "public"

    def table(self, schema, name):
        if name not in self.tables:
            raise ValueError(f"未找到表 {schema}.{name}。")
        return {"schema": schema, "name": name, "rows": 100, "columns": self.tables[name]}


class FakeSources:
    def __init__(self, tables):
        self.connector_value = FakeConnector(tables)

    def connector(self, source_id):
        return self.connector_value


class FakeQuality:
    def __init__(self):
        self.prepared = []

    def prepare_rule(self, rule):
        if rule.get("metric") == "nope":
            raise ValueError("不支持的核查类型：nope。")
        self.prepared.append(rule)
        return {**rule, "dimension": "x"}

    def options(self):
        return {"dimensions": [{"metrics": [{"id": "column_null", "label": "空值检查", "needs_column": True, "fields": [{"name": "filter", "type": "text"}]}]}]}


class FakeQualityStore:
    def __init__(self):
        self.created = []
        self.results = []

    def create_rule(self, data):
        self.created.append(data)
        return {"id": len(self.created), **data}

    def recent_results(self, names, days=7, limit=200):
        return [row for row in self.results if row["table_name"] in names]


class ConfiguredLlm:
    def configured(self):
        return True

    def status(self):
        return {"provider": "openai", "model": "gpt-test"}


@pytest.fixture
def service():
    return AiAssistService(sources=FakeSources({"orders": COLUMNS, "customers": [{"name": "customer_id", "type": "INT", "primary_key": True}, {"name": "phone", "type": "VARCHAR"}]}), quality=FakeQuality(), quality_store=FakeQualityStore(), catalog=FakeQuality().options)


def test_datatypes_map_engine_names_onto_ossie_types():
    assert [sql_datatype(t) for t in ("INTEGER", "DECIMAL(16,2)", "double", "VARCHAR", "DATE", "timestamp without time zone", "boolean", "bigint")] == ["Integer", "Decimal", "Float", "String", "Date", "DateTime", "Boolean", "Integer"]


def test_heuristic_rules_follow_keys_measures_formats_and_freshness(service):
    answer = service.suggest_rules("pg", None, "orders", use_model=False)
    assert answer["used_model"] is False and answer["table"]["schema_name"] == "public"
    by_key = {(item["metric"], item["column_name"]): item for item in answer["suggestions"]}
    assert ("column_duplicate", "order_id") in by_key and by_key[("column_duplicate", "order_id")]["level"] == "high"
    assert ("column_null", "customer_id") in by_key
    assert by_key[("column_value_between", "amount")]["config"] == {"min": 0}
    assert by_key[("column_match_regex", "email")]["config"]["regexp"].startswith("^[^@")
    assert by_key[("table_freshness", "updated_at")]["config"] == {"interval_value": 1, "interval_unit": "day"}
    assert all(item["valid"] for item in answer["suggestions"]) and answer["suggestions"][0]["rule"]["datasource_id"] == "pg"
    with pytest.raises(ValueError, match="未找到表"):
        service.suggest_rules("pg", None, "missing", use_model=False)


def test_model_proposals_are_merged_and_validated(service, monkeypatch):
    service.llm = ConfiguredLlm()
    monkeypatch.setattr(SqlGenerator, "__init__", lambda self, settings: None)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"rules": [
        {"name": "状态枚举", "metric": "column_not_in_enums", "column_name": "status", "config": {"enum_list": "paid,new"}, "threshold": 0, "reason": "状态只有两种"},
        {"name": "重复的主键", "metric": "column_duplicate", "column_name": "order_id"},
        {"name": "坏规则", "metric": "nope", "column_name": "amount"},
    ]})
    answer = service.suggest_rules("pg", "public", "orders")
    assert answer["used_model"] is True and answer["provider"] == "openai"
    sources = [(item["metric"], item["source"], item["valid"]) for item in answer["suggestions"]]
    assert ("column_not_in_enums", "model", True) in sources and ("nope", "model", False) in sources
    assert sum(1 for metric, _, _ in sources if metric == "column_duplicate") == 1  # the model's duplicate was merged away
    bad = next(item for item in answer["suggestions"] if item["metric"] == "nope")
    assert "不支持" in bad["message"]
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: (_ for _ in ()).throw(LlmError("配额用尽")))
    with pytest.raises(AiAssistError, match="模型生成规则失败"):
        service.suggest_rules("pg", "public", "orders")
    applied = service.apply_rules([answer["suggestions"][0]["rule"], {"name": "x", "metric": "nope", "datasource_id": "pg", "table_name": "orders"}])
    assert len(applied["created"]) == 1 and applied["errors"][0]["name"] == "x"


def test_semantic_model_draft_infers_relationships_and_metrics(service):
    draft = service.suggest_semantic_model("pg", "public", ["orders", "customers"], name="shop model", use_model=False)
    document = draft["document"]
    assert document["name"] == "shop_model" and [d["name"] for d in document["datasets"]] == ["orders", "customers"]
    orders = document["datasets"][0]
    assert orders["source"] == "pg.public.orders" and orders["primary_key"] == ["order_id"]
    fields = {field["name"]: field for field in orders["fields"]}
    assert fields["updated_at"]["dimension"] == {"is_time": True} and fields["status"]["dimension"] == {"is_time": False}
    assert fields["amount"]["datatype"] == "Decimal" and "dimension" not in fields["amount"]
    assert document["relationships"] == [{"name": "orders_to_customers", "from": "orders", "to": "customers", "from_columns": ["customer_id"], "to_columns": ["customer_id"]}]
    metrics = {metric["name"]: metric for metric in document["metrics"]}
    assert metrics["orders_count"]["expression"]["dialects"][0]["expression"] == "COUNT(DISTINCT orders.order_id)"
    assert metrics["total_amount"]["expression"]["dialects"][0]["expression"] == "SUM(orders.amount)"
    assert draft["yaml"].startswith("version:") and draft["summary"] == {"datasets": 2, "relationships": 1, "metrics": 3}
    with pytest.raises(AiAssistError, match="至少选择一张表"):
        service.suggest_semantic_model("pg", None, [], use_model=False)


def test_model_refinement_keeps_only_references_that_exist(service, monkeypatch):
    service.llm = ConfiguredLlm()
    service.validator = lambda document: (["missing description"] if not document["semantic_model"][0].get("description") else [], ["note"])
    monkeypatch.setattr(SqlGenerator, "__init__", lambda self, settings: None)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {
        "description": "订单与客户",
        "datasets": [{"name": "orders", "description": "订单", "synonyms": ["order"], "fields": [{"name": "amount", "description": "金额", "synonyms": ["GMV"]}, {"name": "ghost", "description": "x"}]}],
        "metrics": [
            {"name": "gmv", "expression": "SUM(orders.amount)", "description": "成交额", "datatype": "Decimal", "synonyms": ["总额"]},
            {"name": "bad", "expression": "SUM(orders.nothing)"},
        ],
        "relationships": [{"from": "orders", "to": "customers", "from_columns": ["customer_id"], "to_columns": ["customer_id"]}, {"from": "orders", "to": "ghosts", "from_columns": ["x"], "to_columns": ["y"]}],
    })
    draft = service.suggest_semantic_model("pg", "public", ["orders", "customers"])
    document = draft["document"]
    assert document["description"] == "订单与客户" and document["datasets"][0]["ai_context"] == {"synonyms": ["order"]}
    amount = next(field for field in document["datasets"][0]["fields"] if field["name"] == "amount")
    assert amount["description"] == "金额" and amount["ai_context"] == {"synonyms": ["GMV"]}
    assert [metric["name"] for metric in document["metrics"]][:2] == ["gmv", "orders_count"] and all(metric["name"] != "bad" for metric in document["metrics"])
    assert len(document["relationships"]) == 1
    assert draft["valid"] is True and draft["warnings"] == ["note"] and draft["used_model"] is True


class FakeMetadata:
    def __init__(self):
        self.versions_by_fqn = {}

    def row(self, entity_type, ref):
        return {"entity_type": "table", "fqn": ref, "name": ref.split(".")[-1], "deleted": False}

    def versions(self, entity_type, fqn):
        return {"versions": self.versions_by_fqn.get(fqn, [])}


class FakeLineage:
    def graph(self, entity_type, fqn, upstream_depth=3, downstream_depth=0):
        return {
            "upstream_count": 2,
            "nodes": [
                {"fqn": fqn, "name": "report", "entity_type": "table", "depth": 0, "service_fqn": "pg"},
                {"fqn": "pg.db.s.orders", "name": "orders", "entity_type": "table", "depth": -1, "service_fqn": "pg"},
                {"fqn": "mysql.db.s.raw_orders", "name": "raw_orders", "entity_type": "table", "depth": -2, "service_fqn": "mysql", "deleted": True},
            ],
        }


class FakeIngestor:
    def runs(self, **filters):
        return {"items": [{"service_fqn": "pg", "status": "failed", "started_at": 9_999_999_999_999, "message": "连接超时"}, {"service_fqn": "pg", "status": "success", "started_at": 9_999_999_999_999}]}


def test_root_cause_ranks_upstream_signals_by_strength_and_distance():
    metadata = FakeMetadata()
    now_ms = 10_000_000_000_000
    metadata.versions_by_fqn["pg.db.s.orders"] = [
        {"version": 1.0, "updated_at": now_ms - 1000, "updated_by": "eve", "change": {"fields_deleted": [{"name": "columns.discount"}], "fields_updated": [{"name": "columns.amount.data_type_display"}]}},
        {"version": 0.2, "updated_at": now_ms - 30 * 86400 * 1000, "updated_by": "old", "change": {"fields_deleted": [{"name": "columns.legacy"}]}},
    ]
    metadata.versions_by_fqn["pg.db.s.report"] = [{"version": 0.2, "updated_at": now_ms - 500, "updated_by": "root", "change": {"fields_updated": [{"name": "description"}]}, "summary": "更新了描述"}]
    store = FakeQualityStore()
    store.results = [{"table_name": "orders", "state": 2, "rule_name": "金额非负", "actual_value": 12, "operator": "lte", "threshold": 0, "check_time": "2026-09-16T01:00:00"}, {"table_name": "orders", "state": 1, "rule_name": "非空"}]
    service = AiAssistService(sources=FakeSources({}), quality_store=store, metadata=metadata, lineage=FakeLineage(), ingestor=FakeIngestor(), clock=lambda: now_ms / 1000)
    report = service.root_cause("table", "pg.db.s.report", days=7, use_model=False)
    assert report["target"]["fqn"] == "pg.db.s.report" and report["upstream_count"] == 2
    ranked = [(item["name"], item["score"], [signal["kind"] for signal in item["signals"]]) for item in report["suspects"]]
    assert ranked[0][0] == "orders" and ranked[0][2] == ["quality", "schema", "ingestion"]
    assert ranked[0][1] == pytest.approx((4 + 3 + 3) * 0.9)
    assert ranked[1][0] == "raw_orders" and ranked[1][2] == ["deleted"] and ranked[1][1] == pytest.approx(10 * 0.7)
    assert ranked[2][0] == "report" and ranked[2][2] == ["edit", "ingestion"]
    assert "上游 1 层的 orders" in report["summary"] and "金额非负" in report["summary"]
    assert "版本 1.0：删除字段 discount；类型变更 amount（eve）" in [s["detail"] for s in report["suspects"][0]["signals"]]
    class LoneLineage:
        def graph(self, entity_type, fqn, upstream_depth=3, downstream_depth=0):
            return {"upstream_count": 0, "nodes": [{"fqn": fqn, "name": "other", "entity_type": "table", "depth": 0}]}

    quiet = AiAssistService(sources=FakeSources({}), metadata=FakeMetadata(), lineage=LoneLineage(), clock=lambda: now_ms / 1000)
    assert "没有质量失败" in quiet.root_cause("table", "x.y.z.other", use_model=False)["summary"]
    with pytest.raises(AiAssistError, match="尚未初始化"):
        AiAssistService(sources=FakeSources({})).root_cause("table", "x")


def test_root_cause_narrative_comes_from_the_model_when_configured(monkeypatch):
    metadata = FakeMetadata()
    metadata.versions_by_fqn["pg.db.s.orders"] = [{"version": 1.0, "updated_at": 9_999_999_999_999, "updated_by": "eve", "change": {"fields_deleted": [{"name": "columns.discount"}]}}]
    service = AiAssistService(sources=FakeSources({}), metadata=metadata, lineage=FakeLineage(), llm=ConfiguredLlm(), clock=lambda: 10_000_000_000)
    monkeypatch.setattr(SqlGenerator, "__init__", lambda self, settings: None)
    monkeypatch.setattr(SqlGenerator, "complete_text", lambda self, prompt, *, system: "上游 orders 删除了 discount 字段，最可能是根因。")
    report = service.root_cause(None, "pg.db.s.report")
    assert report["used_model"] is True and report["summary"].startswith("上游 orders")
    monkeypatch.setattr(SqlGenerator, "complete_text", lambda self, prompt, *, system: (_ for _ in ()).throw(LlmError("超时")))
    report = service.root_cause(None, "pg.db.s.report")
    assert report["used_model"] is False and "模型分析失败" in report["summary"]


# ----- routes -----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    monkeypatch.setattr(app_module, "POLARIS_RUNTIME", tmp_path / "polaris")
    monkeypatch.setattr(app_module, "ENGINES_RUNTIME", tmp_path / "engines")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(app_module.app.state.polaris, "status", lambda: {"status": "online", "version": "1.7.0"})
        yield value


def test_ai_routes_work_without_a_model_on_the_sample_data(client):
    status = client.get("/api/ai/status").json()
    assert status["features"] == {"quality_rules": True, "semantic_model": True, "root_cause": True}
    rules = client.post("/api/ai/quality-rules/suggest", json={"datasource_id": "local-sample", "schema_name": "main", "table_name": "t_lattice_orders", "use_model": False}).json()
    metrics = {(item["metric"], item["column_name"]) for item in rules["suggestions"]}
    # The sample DuckDB reports no primary keys, so identifiers get the not-null rule.
    assert ("column_null", "order_id") in metrics and ("table_freshness", "order_date") in metrics
    assert all(item["valid"] for item in rules["suggestions"])
    applied = client.post("/api/ai/quality-rules/apply", json={"rules": [rules["suggestions"][0]["rule"]]}).json()
    assert len(applied["created"]) == 1 or applied["errors"]  # the quality database may be unreachable in CI
    draft = client.post("/api/ai/semantic-model/suggest", json={"datasource_id": "local-sample", "schema_name": "main", "tables": ["t_lattice_orders", "t_lattice_customers"], "use_model": False}).json()
    assert draft["valid"] is True and draft["summary"]["relationships"] == 1 and "semantic_model" in draft["yaml"]
    assert client.post("/api/validate", json={"yaml": draft["yaml"]}).json()["valid"] is True
    client.post("/api/metadata/entities", json={"entity_type": "glossary", "name": "ai-root"})
    report = client.post("/api/ai/root-cause", json={"ref": "ai-root", "entity_type": "glossary", "use_model": False}).json()
    assert report["target"]["fqn"] == "ai-root" and report["suspects"][0]["depth"] == 0
    assert client.post("/api/ai/root-cause", json={"ref": "nope.table", "use_model": False}).status_code == 404
