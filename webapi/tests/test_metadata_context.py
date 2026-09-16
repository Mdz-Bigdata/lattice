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

"""The context layer (cards, semantics, assistant, MCP), data insights and alerts."""

import datetime as dt
import hashlib
import hmac

import pytest

from webapi import metadata_insights
from webapi.llm import SqlGenerator
from webapi.metadata_context import AGENT_USER, LATEST_PROTOCOL, ContextService, McpServer
from webapi.metadata_insights import AlertService, InsightsService
from webapi.metadata_lineage import LineageService
from webapi.metadata_service import SECRET_MASK, MetadataError, MetadataService
from webapi.metadata_store import MetadataStore

SCHEMA = "pg.blog.public"
ORDERS = f"{SCHEMA}.orders"
REFUNDS = f"{SCHEMA}.refunds"


class FakeLlm:
    def __init__(self, configured):
        self._configured = configured

    def configured(self):
        return self._configured

    def status(self):
        return {"provider": "anthropic", "model": "claude-opus-5"}


@pytest.fixture
def world():
    store = MetadataStore("sqlite:///:memory:")
    store.bootstrap()
    service = MetadataService(store)
    service.seed()
    service.create("databaseService", {"name": "pg", "service_type": "Postgres", "datasource_id": "quality-postgres"})
    service.create("database", {"name": "blog", "parent_fqn": "pg"})
    service.create("databaseSchema", {"name": "public", "parent_fqn": "pg.blog"})
    service.create("table", {"name": "orders", "parent_fqn": SCHEMA, "source_schema": "public", "columns": [{"name": "id", "type": "int"}, {"name": "amount", "type": "numeric"}]})
    service.create("table", {"name": "refunds", "parent_fqn": SCHEMA, "source_schema": "public", "columns": [{"name": "id", "type": "int"}]})
    service.create("glossary", {"name": "Sales", "display_name": "销售"})
    service.create("glossaryTerm", {"name": "GMV", "parent_fqn": "Sales", "description": "成交总额，含税", "synonyms": ["成交额"]})
    lineage = LineageService(store, service)
    insights = InsightsService(store, service)
    context = ContextService(service, lineage, insights, llm=FakeLlm(False))
    context.ensure_bot()
    yield service, lineage, insights, context
    store.close()


def describe_orders(service, lineage):
    service.update("table", ORDERS, {"description": "订单事实表", "owners": ["lattice"], "columns": [{"name": "amount", "description": "订单金额（元）"}]})
    service.set_tags(f"{ORDERS}.amount", [{"tag_fqn": "Sales.GMV", "source": "glossary"}])
    lineage.add_edge({"from_fqn": ORDERS, "to_fqn": REFUNDS, "description": "退款来自订单"})
    lineage.record_query({"id": "q1", "sql": "SELECT sum(amount) FROM public.orders", "datasource_id": "quality-postgres"})


# ----- context cards and semantics -------------------------------------------------------------
def test_entity_context_carries_terms_lineage_columns_and_queries(world):
    service, lineage, _, context = world
    describe_orders(service, lineage)
    card = context.entity_context("table", ORDERS)
    assert card["glossary"][0]["description"] == "成交总额，含税"
    assert card["lineage"]["downstream"][0]["fqn"] == REFUNDS
    markdown = card["markdown"]
    for expected in ("订单事实表", "| amount | numeric | 订单金额（元） | Sales.GMV |", "## 术语定义", "同义词：成交额", "下游：" + REFUNDS, "## 近期查询"):
        assert expected in markdown


def test_datasource_semantics_list_only_described_tables(world):
    service, lineage, _, context = world
    assert context.datasource_context("quality-postgres") == ""
    describe_orders(service, lineage)
    text = context.datasource_context({"id": "quality-postgres"})
    assert "public.orders：订单事实表；责任人：Lattice" in text
    assert "  - amount：订单金额（元）；术语：Sales.GMV" in text
    assert "Sales.GMV｜GMV（同义词：成交额）：成交总额，含税" in text
    assert "refunds" not in text
    assert context.datasource_context("unknown") == ""


def test_assistant_without_a_model_returns_the_context(world, monkeypatch):
    service, lineage, _, context = world
    describe_orders(service, lineage)
    reply = context.ask("订单金额是什么")
    assert reply["configured"] is False and reply["answer"] == ""
    assert "## 相关资产" in reply["context"]["markdown"]
    captured = {}

    def complete_text(self, prompt, *, system):
        captured["prompt"], captured["system"] = prompt, system
        return "订单金额是订单的成交金额。"

    monkeypatch.setattr(SqlGenerator, "complete_text", complete_text)
    context.llm = FakeLlm(True)
    answered = context.ask("订单金额是什么", entity_type="table", ref=ORDERS, datasource_id="quality-postgres")
    assert answered["answer"] == "订单金额是订单的成交金额。"
    assert "用户问题：订单金额是什么" in captured["prompt"] and "## 当前对象" in captured["prompt"]
    assert "目录中未记录" in captured["system"]
    with pytest.raises(MetadataError):
        context.ask("   ")


def test_description_suggestions_need_a_model_and_known_columns(world, monkeypatch):
    service, _, _, context = world
    with pytest.raises(MetadataError, match="尚未配置模型"):
        context.suggest_descriptions("table", ORDERS)
    monkeypatch.setattr(SqlGenerator, "complete_json", lambda self, prompt, *, system, schema: {"description": "订单", "columns": [{"name": "amount", "description": "金额"}, {"name": "ghost", "description": "x"}]})
    context.llm = FakeLlm(True)
    suggestion = context.suggest_descriptions("table", ORDERS)
    assert suggestion["description"] == "订单"
    assert suggestion["columns"] == [{"name": "amount", "description": "金额", "current": ""}]


# ----- MCP -----------------------------------------------------------------------------------------
def rpc(server, method, params=None, identifier=1):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def test_mcp_session_batches_and_protocol_errors(world):
    server = McpServer(world[3])
    response, session = server.handle_http({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01", "clientInfo": {"name": "test"}}})
    assert response["result"]["protocolVersion"] == LATEST_PROTOCOL and server.known_session(session)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    batch = server.handle([{"jsonrpc": "2.0", "id": 7, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/cancelled"}])
    assert batch == [{"jsonrpc": "2.0", "id": 7, "result": {}}]
    assert server.handle({"jsonrpc": "1.0", "id": 5, "method": "ping"})["error"]["code"] == -32600
    assert server.handle([])["error"]["code"] == -32600
    assert rpc(server, "no/such")["error"]["code"] == -32601
    assert rpc(server, "tools/call", {"name": "drop_everything"})["error"]["code"] == -32602
    assert server.close_session(session) and not server.known_session(session)


def test_mcp_tools_read_and_write_the_catalog(world):
    service, lineage, _, context = world
    describe_orders(service, lineage)
    server = McpServer(context)
    names = [tool["name"] for tool in rpc(server, "tools/list")["result"]["tools"]]
    assert "search_metadata" in names and "query_datasource" not in names  # no query service here
    found = rpc(server, "tools/call", {"name": "search_metadata", "arguments": {"query": "订单"}})["result"]
    assert found["isError"] is False and found["structuredContent"]["items"][0]["fqn"] == ORDERS
    missing = rpc(server, "tools/call", {"name": "get_entity_details", "arguments": {"fqn": "pg.nope"}})["result"]
    assert missing["isError"] is True and "不存在" in missing["content"][0]["text"]
    patched = rpc(server, "tools/call", {"name": "patch_entity", "arguments": {"fqn": REFUNDS, "description": "退款记录", "tags": ["PII.None"]}})["result"]
    assert patched["structuredContent"]["description"] == "退款记录"
    assert service.feed(entity_fqn=REFUNDS)["items"][0]["user_name"] == AGENT_USER
    term = rpc(server, "tools/call", {"name": "create_glossary_term", "arguments": {"glossary": "Sales", "name": "AOV", "description": "客单价"}})["result"]
    assert term["structuredContent"]["fqn"] == "Sales.AOV"
    resource = rpc(server, "resources/read", {"uri": f"lattice://entity/table/{ORDERS}"})["result"]["contents"][0]
    assert resource["mimeType"] == "text/markdown" and "订单事实表" in resource["text"]
    assert "客单价" in rpc(server, "resources/read", {"uri": "lattice://glossary"})["result"]["contents"][0]["text"]
    assert rpc(server, "resources/read", {"uri": "lattice://nope"})["error"]["code"] == -32603
    prompt = rpc(server, "prompts/get", {"name": "impact_analysis", "arguments": {"fqn": ORDERS}})["result"]
    assert REFUNDS in prompt["messages"][0]["content"]["text"]


# ----- insights, KPIs and alerts -------------------------------------------------------------------
def test_insights_percentages_and_kpi_status(world):
    service, lineage, insights, _ = world
    describe_orders(service, lineage)
    figures = insights.compute()
    assert figures["assets"] == 2 and figures["description_percent"] == 50.0 and figures["owner_percent"] == 50.0
    assert figures["with_lineage"] == 2 and figures["governance"]["glossary_terms"] == 1
    today = dt.date.today()
    day = lambda offset: (today + dt.timedelta(days=offset)).isoformat()  # noqa: E731
    service.create("kpi", {"name": "future", "chart": "percentage_of_entities_with_description_by_type", "target_value": 80, "start_date": day(5), "end_date": day(30)})
    service.create("kpi", {"name": "missed", "chart": "percentage_of_entities_with_owner_by_type", "target_value": 90, "start_date": day(-30), "end_date": day(-1)})
    service.create("kpi", {"name": "done", "chart": "total_entities_by_type", "metric_type": "NUMBER", "target_value": 2, "start_date": day(-1), "end_date": day(10)})
    status = {kpi["name"]: kpi["status"] for kpi in insights.kpis()}
    assert status == {"future": "pending", "missed": "expired", "done": "achieved"}
    with pytest.raises(MetadataError):
        service.create("kpi", {"name": "bad", "chart": "percentage_of_entities_with_owner_by_type", "target_value": 120, "start_date": day(0), "end_date": day(1)})
    insights.snapshot()
    assert insights.series(7)[-1]["assets"] == 2


def test_alerts_match_filters_and_sign_webhooks(world, monkeypatch):
    service, _, _, _ = world
    alerts = AlertService(service.store, service, synchronous=True)
    alerts.attach()
    sent = []

    class FakeResponse:
        status_code = 204

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, content, headers):
            sent.append((url, content, headers))
            return FakeResponse()

    monkeypatch.setattr(metadata_insights.httpx, "Client", FakeClient)
    with pytest.raises(MetadataError):
        alerts.validate({"name": "x", "destinations": [{"type": "webhook", "url": "ftp://example"}]})
    document = alerts.validate(
        {"name": "descriptions", "filters": {"entity_types": ["table"], "event_types": ["entityUpdated"], "change_kinds": ["description"], "fqn_prefix": SCHEMA},
         "destinations": [{"type": "in_app"}, {"type": "webhook", "url": "http://127.0.0.1:9/hook", "secret": "k"}]}
    )
    subscription = service.create("eventSubscription", document)
    service.update("table", ORDERS, {"description": "订单"})
    service.set_tags(ORDERS, ["PII.None"])
    service.create("glossaryTerm", {"name": "AOV", "parent_fqn": "Sales"})
    listing = alerts.notifications()
    assert listing["total"] == 2 and listing["counts"] == {"delivered": 1, "unread": 1}
    url, body, headers = sent[0]
    assert headers["X-Lattice-Signature"] == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    kept = alerts.validate({**document, "destinations": [{"type": "webhook", "url": url, "secret": SECRET_MASK}]}, service.store.get_entity("eventSubscription", id=subscription["id"])["json"]["destinations"])
    assert kept["destinations"][0]["secret"] == "k"
    assert alerts.mark_read(all_unread=True)["updated"] == 1


def test_tool_arguments_are_checked_against_their_schema(world):
    service, lineage, _, context = world
    with pytest.raises(MetadataError, match="limit"):
        context.call_tool("search_metadata", {"query": "订单", "limit": [5]})
    with pytest.raises(MetadataError, match="不支持参数"):
        context.call_tool("search_metadata", {"query": "订单", "unknown": 1})
    with pytest.raises(MetadataError, match="缺少参数"):
        context.call_tool("get_entity_details", {})
    server = McpServer(context)
    assert rpc(server, "prompts/get", {"name": "describe_asset", "arguments": ["x"]})["error"]["code"] == -32602
    refused = rpc(server, "tools/call", {"name": "patch_entity", "arguments": {"fqn": ORDERS, "tags": "PII.None"}})["result"]
    assert refused["isError"] is True


def test_filtered_insights_do_not_compare_against_unfiltered_history(world):
    service, lineage, insights, _ = world
    describe_orders(service, lineage)
    service.set_tags(ORDERS, ["Tier.Tier1"])
    filtered = insights.overview(7, tier="Tier.Tier1")
    assert filtered["filtered"] and filtered["current"]["assets"] == 1
    assert filtered["change"] == {} and [point["assets"] for point in filtered["series"]] == [1]
    assert insights.overview(7)["change"]["assets"] == 0


def test_editing_a_webhook_url_keeps_or_asks_for_its_secret(world):
    service = world[0]
    alerts = AlertService(service.store, service, synchronous=True)
    stored = [{"type": "webhook", "url": "http://127.0.0.1:9/hook", "secret": "k"}]
    moved = alerts.validate({"name": "a", "destinations": [{"type": "webhook", "url": "http://127.0.0.1:9/hooks", "secret": SECRET_MASK}]}, stored)
    assert moved["destinations"][0]["secret"] == "k"
    with pytest.raises(MetadataError, match="签名密钥"):
        alerts.validate(
            {"name": "a", "destinations": [
                {"type": "webhook", "url": "http://x.example/a", "secret": SECRET_MASK},
                {"type": "webhook", "url": "http://x.example/b", "secret": SECRET_MASK},
            ]},
            stored,
        )
