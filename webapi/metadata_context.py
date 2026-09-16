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

"""The context layer of the 元数据管理 module: data context for people, assistants and agents.

OpenMetadata calls itself a *context layer for data and AI*: the catalog is
not only browsed by people, it is read by the assistants and agents that
write SQL, answer questions and automate governance. This module is that
surface for Lattice:

* **context cards** — one entity rendered as JSON and as Markdown with
  everything an assistant needs: description, owners, tier, domain, tags,
  glossary terms *with their definitions*, columns, lineage neighbours,
  recent queries and usage;
* **data-source semantics** — the business meaning of the tables of one data
  source (descriptions, column descriptions, tags, terms, owners), appended
  to the schema the 智能问数 model receives so it writes SQL against the
  vocabulary of the business, not only against column names;
* **the catalog assistant** — a question answered by the configured model
  from the catalog context alone, with the context shown to the reader;
* **an MCP server** — the Model Context Protocol over Streamable HTTP
  (JSON-RPC 2.0 at ``POST /api/metadata/mcp``) exposing tools, resources and
  prompts modelled on OpenMetadata's MCP server, so Claude Desktop, Cursor and
  any agent framework can search the catalog, read an asset, walk lineage,
  patch descriptions, create glossary terms and run read-only SQL. The same
  tool definitions are served as plain function-calling schemas.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any, Callable

from .llm import LlmError, LlmSettings, SqlGenerator
from .metadata_entities import DATA_ASSET_TYPES, ENTITY_TYPES, SERVICE_TYPES, split_fqn, type_label
from .metadata_insights import InsightsService
from .metadata_lineage import LineageService
from .metadata_service import MetadataError, MetadataService, summary_of
from .metadata_store import MetadataStoreError, day_of, now_ms

SERVER_NAME = "lattice-metadata"
SERVER_VERSION = "0.2.0"
#: Catalog user the MCP tools act as; every write they make is attributed to it.
AGENT_USER = "mcp-agent"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]
MAX_CONTEXT_CHARS = 12000
MAX_ASK_CHARS = 30000
MAX_QUESTION = 2000
MIN_SCORE = 2
#: Question words that say nothing about which asset is meant.
STOPWORDS = {
    "什么", "哪些", "哪个", "怎么", "怎样", "多少", "是否", "如何", "为什么", "一下", "请问", "这个", "那个",
    "我们", "你们", "他们", "数据", "可以", "需要", "查询", "告诉", "帮我", "意思", "是什", "有没", "没有",
    "the", "what", "which", "how", "show", "table", "data", "is", "of", "in", "for", "and",
}
CATALOG_SYSTEM_PROMPT = """你是 Lattice 数据平台的元数据助手，服务于数据使用者、数据管理员和其他 AI 代理。
你只能依据「元数据目录上下文」中给出的信息回答：数据资产的含义、字段说明、业务术语、标签与分级、责任人、所属数据域、上下游血缘、使用情况。
规则：
1. 用中文回答，先给结论，再给依据；引用资产时写出完整名称（FQN）。
2. 上下文中没有的信息要明确说「目录中未记录」，不要编造表、字段、责任人或血缘。
3. 用户如果询问如何取数，可以给出基于上下文中真实表和字段的 SQL 思路，但要说明需要在 SQL 工作台或智能问数中执行。
4. 回答保持简洁，必要时用列表。"""

SUGGEST_SYSTEM_PROMPT = """你是数据目录的文档助手。根据给出的元数据（表名、字段名、类型、已有描述、标签、术语、血缘、近期查询），为数据资产和它的字段撰写简洁的中文业务描述。
规则：
1. 只根据给出的信息推断，不要编造业务事实；无法确定时使用「可能」「推测」等措辞，并保持简短。
2. 资产描述 1–3 句；每个字段的描述不超过 1 句，说明字段含义与取值特点。
3. 已有描述的字段可以给出更清晰的版本，但不要改变原意。
4. 输出 JSON：{"description": "资产描述", "columns": [{"name": "字段名", "description": "字段描述"}]}。"""
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["description", "columns"],
    "additionalProperties": False,
}

class ContextService:
    """Context cards, data-source semantics, the assistant and the tool surface."""

    def __init__(
        self, service: MetadataService, lineage: LineageService, insights: InsightsService | None = None, *,
        llm: LlmSettings | None = None, queries: Any = None, registry: Any = None, metrics: Any = None,
    ):
        self.service = service
        self.store = service.store
        self.lineage = lineage
        self.insights = insights
        self.llm = llm
        self.queries = queries
        self.registry = registry
        #: The semantic query engine (指标平台), when the application provides one.
        self.metrics = metrics

    # =====================================================================================
    # context cards
    # =====================================================================================
    def entity_context(self, entity_type: str | None, ref: str, *, include_columns: bool = True) -> dict[str, Any]:
        row = self.service.row(entity_type, ref)
        entity = self.service.decorate(row, full=True)
        is_asset = row["entity_type"] in DATA_ASSET_TYPES
        edges = self.lineage.edges_of(row["entity_type"], row["fqn"]) if is_asset else {"upstream": [], "downstream": []}
        neighbours = {e["from_fqn"] for e in edges["upstream"]} | {e["to_fqn"] for e in edges["downstream"]}
        neighbour_rows = {r["fqn"]: r for r in self.lineage._asset_rows(sorted(neighbours))} if neighbours else {}
        term_fqns = {t["tag_fqn"] for t in entity.get("glossary_terms", [])}
        for column in entity.get("columns", []) if include_columns else []:
            term_fqns |= {t["tag_fqn"] for t in column.get("glossary_terms", [])}
        terms = self._terms(sorted(term_fqns))
        queries = self.store.queries_for(row["fqn"], limit=5) if is_asset else []
        usage = entity.get("usage") or {"queries": 0, "views": 0}
        children = self.service.children(row["entity_type"], row["id"]) if row["entity_type"] in {"databaseService", "database", "databaseSchema", "glossary", "classification", "domain", "apiCollection", "container", "messagingService", "dashboardService", "pipelineService", "mlmodelService", "storageService", "searchService", "apiService"} else None
        if not include_columns:
            entity.pop("columns", None)
        context = {
            "entity": entity,
            "glossary": terms,
            "lineage": {
                "upstream": [self._neighbour(e["from_fqn"], neighbour_rows, e) for e in edges["upstream"]],
                "downstream": [self._neighbour(e["to_fqn"], neighbour_rows, e) for e in edges["downstream"]],
            },
            "queries": [{"sql": q["sql"][:2000], "datasource_id": q["datasource_id"], "ts": q["ts"]} for q in queries],
            "usage": usage,
            "children": [{"entity_type": g["entity_type"], "label": g["label"], "total": g["total"]} for g in (children or {}).get("groups", [])] if children else [],
            "generated_at": now_ms(),
        }
        context["markdown"] = entity_markdown(context)
        return context

    @staticmethod
    def _neighbour(fqn: str, rows: dict[str, dict[str, Any]], edge: dict[str, Any]) -> dict[str, Any]:
        row = rows.get(fqn)
        base = summary_of(row) if row else {"fqn": fqn, "entity_type": "", "type_label": "未登记", "name": split_fqn(fqn)[-1], "display_name": split_fqn(fqn)[-1]}
        return {**base, "description": (row["description"] if row else "")[:300], "source": edge["source"], "columns": len(edge.get("columns") or []), "edge_description": edge.get("description") or ""}

    def _terms(self, fqns: list[str]) -> list[dict[str, Any]]:
        if not fqns:
            return []
        rows = [r for r in self.store.get_entities_by_fqn(fqns) if r["entity_type"] == "glossaryTerm"]
        return [term_view(r) for r in rows]

    # =====================================================================================
    # data-source semantics for 智能问数
    # =====================================================================================
    def datasource_context(self, record: Any) -> str:
        datasource_id = record.get("id") if isinstance(record, dict) else str(record or "")
        if not datasource_id:
            return ""
        try:
            services = self.lineage.services_of_datasource(datasource_id)
            if not services:
                return ""
            lines: list[str] = ["业务语义（来自元数据目录；说明表和字段的业务含义，请优先按这些含义理解用户问题）："]
            term_fqns: set[str] = set()
            count = 0
            for service in services:
                tables = self.store.all_entities("table", deleted=False, service_fqn=service["fqn"], limit=2000)
                owners = self.service.owners_for([t["id"] for t in tables]) if tables else {}
                for table in tables:
                    if count >= 80:
                        break
                    document = table["json"]
                    tags = self.store.tags_for_prefix(table["fqn"])
                    table_tags = [t for t in tags if t["target_fqn"] == table["fqn"]]
                    column_tags: dict[str, list[dict[str, Any]]] = {}
                    for tag in tags:
                        if tag["target_fqn"] != table["fqn"]:
                            column_tags.setdefault(split_fqn(tag["target_fqn"])[-1], []).append(tag)
                    columns = [c for c in document.get("columns") or [] if isinstance(c, dict)]
                    described = [c for c in columns if c.get("description") or c.get("display_name") or column_tags.get(c.get("name"))]
                    has_meaning = bool(table["description"] or table["display_name"] or table_tags or described or owners.get(table["id"]))
                    if not has_meaning:
                        continue
                    count += 1
                    qualified = f"{document.get('source_schema') or split_fqn(table['fqn'])[-2]}.{table['name']}"
                    head = [qualified]
                    if table["display_name"]:
                        head.append(f"（{table['display_name']}）")
                    parts = []
                    if table["description"]:
                        parts.append(_one_line(table["description"], 300))
                    if owners.get(table["id"]):
                        parts.append("责任人：" + "、".join(o["display_name"] for o in owners[table["id"]][:3]))
                    if table["domain_fqn"]:
                        parts.append("数据域：" + table["domain_fqn"])
                    if table["tier"]:
                        parts.append("分级：" + split_fqn(table["tier"])[-1])
                    classifications = [t["tag_fqn"] for t in table_tags if t["source"] == "classification"]
                    glossary = [t["tag_fqn"] for t in table_tags if t["source"] == "glossary"]
                    term_fqns |= set(glossary)
                    if classifications:
                        parts.append("标签：" + "、".join(classifications))
                    if glossary:
                        parts.append("术语：" + "、".join(glossary))
                    lines.append("- " + "".join(head) + ("：" + "；".join(parts) if parts else ""))
                    for column in described[:60]:
                        bits = []
                        if column.get("display_name"):
                            bits.append(column["display_name"])
                        if column.get("description"):
                            bits.append(_one_line(column["description"], 160))
                        for tag in column_tags.get(column.get("name"), []):
                            if tag["source"] == "glossary":
                                term_fqns.add(tag["tag_fqn"])
                                bits.append("术语：" + tag["tag_fqn"])
                            else:
                                bits.append("标签：" + tag["tag_fqn"])
                        lines.append(f"  - {column.get('name')}：{'；'.join(bits)}")
            if count == 0:
                return ""
            terms = self._terms(sorted(term_fqns))
            if terms:
                lines.append("术语定义：")
                for term in terms[:40]:
                    label = term["display_name"] if term["display_name"] != term["name"] else term["name"]
                    synonyms = f"（同义词：{'、'.join(term['synonyms'])}）" if term["synonyms"] else ""
                    lines.append(f"- {term['fqn']}｜{label}{synonyms}：{_one_line(term['description'], 300) or '（无描述）'}")
            text = "\n".join(lines)
            return text[:MAX_CONTEXT_CHARS]
        except (MetadataError, MetadataStoreError):
            return ""

    # =====================================================================================
    # search and glossary
    # =====================================================================================
    def search(self, query: Any, *, entity_types: list[str] | None = None, limit: Any = 10, deleted: bool = False) -> list[dict[str, Any]]:
        """Substring search first, then assets ranked by how much of the question they mention.

        A question typed in Chinese has no spaces, so it rarely matches any asset as a
        whole; its words and two-to-four character pieces are scored against every
        asset's search text instead.
        """
        q = str(query or "").strip()
        size = max(1, min(int(limit or 10), 50))
        types = [t for t in (entity_types or []) if t in ENTITY_TYPES] or None
        listing = self.service.search(q or None, entity_types=types, deleted=deleted, page=1, size=size)
        items = list(listing["items"])
        if q and not deleted and len(items) < size:
            seen = {item["id"] for item in items}
            extra = [row for row in self._ranked(q, types or DATA_ASSET_TYPES, size) if row["id"] not in seen]
            items.extend(self.service.decorate_many(extra[: size - len(items)], full=False))
        return [self._hit(item) for item in items]

    def _ranked(self, text: str, entity_types: list[str], limit: int) -> list[dict[str, Any]]:
        tokens = query_tokens(text)
        if not tokens:
            return []
        lowered = text.lower()
        scored: list[tuple[int, str]] = []
        for row in self.store.search_rows(entity_types):
            score = _score(tokens, str(row["search_text"] or ""))
            name = str(row["name"] or "").lower()
            if len(name) >= 2 and name in lowered:
                score += 6
            if score >= MIN_SCORE:
                scored.append((score, row["id"]))
        scored.sort(key=lambda item: -item[0])
        ids = [identifier for _, identifier in scored[:limit]]
        rows = {row["id"]: row for row in self.store.get_entities(ids)}
        return [rows[identifier] for identifier in ids if identifier in rows]

    @staticmethod
    def _hit(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "fqn": item["fqn"],
            "entity_type": item["entity_type"],
            "type_label": item["type_label"],
            "name": item["name"],
            "display_name": item["display_name"] or item["name"],
            "description": (item.get("description") or "")[:500],
            "service_type": item.get("service_type", ""),
            "tier": item.get("tier"),
            "domain": (item.get("domain") or {}).get("fqn") if item.get("domain") else None,
            "owners": [o["display_name"] for o in item.get("owners", [])],
            "tags": [t["tag_fqn"] for t in item.get("tags", [])],
            "glossary_terms": [t["tag_fqn"] for t in item.get("glossary_terms", [])],
            "columns": [c["name"] for c in item.get("columns", [])][:80] if "columns" in item else [],
            "deleted": item.get("deleted", False),
        }

    def glossary_terms(self, *, query: Any = None, glossary: Any = None, limit: Any = 50) -> list[dict[str, Any]]:
        size = max(1, min(int(limit or 50), 500))
        service_fqn = None
        if glossary:
            service_fqn = self.service.row("glossary", str(glossary))["fqn"]
        rows = self.store.all_entities("glossaryTerm", deleted=False, service_fqn=service_fqn, limit=5000)
        q = str(query or "").strip().lower()
        if q:
            words = q.split()
            exact = [r for r in rows if all(w in r["search_text"] for w in words)]
            if exact:
                rows = exact
            else:
                tokens = query_tokens(q)
                scored = sorted(((_score(tokens, r["search_text"]), r) for r in rows), key=lambda item: -item[0])
                rows = [r for score, r in scored if score >= MIN_SCORE]
        return [term_view(r) for r in rows[:size]]

    def services(self) -> list[dict[str, Any]]:
        rows = self.store.all_entities(sorted(SERVICE_TYPES), deleted=False, limit=2000)
        result = []
        for row in rows:
            result.append({**summary_of(row), "description": row["description"][:300], "datasource_id": row["json"].get("datasource_id", ""), "tables": self.store.list_entities("table", service_fqn=row["fqn"], deleted=False, page=1, size=1)["total"] if row["entity_type"] == "databaseService" else 0})
        return result

    # =====================================================================================
    # assistant
    # =====================================================================================
    def ask(self, question: Any, *, entity_type: str | None = None, ref: str | None = None, datasource_id: str | None = None) -> dict[str, Any]:
        text = str(question or "").strip()
        if not text:
            raise MetadataError(400, "请输入问题。")
        if len(text) > MAX_QUESTION:
            raise MetadataError(400, f"问题不能超过 {MAX_QUESTION} 个字符。")
        parts: list[str] = []
        focus = None
        if ref:
            focus = self.entity_context(entity_type, ref)
            parts.append("## 当前对象\n" + focus["markdown"])
        if datasource_id:
            semantics = self.datasource_context(datasource_id)
            if semantics:
                parts.append("## 数据源语义\n" + semantics)
        hits = self.search(text, limit=6)
        if hits:
            parts.append("## 相关资产\n" + "\n".join(_hit_line(h) for h in hits))
        terms = self.glossary_terms(query=text, limit=8)
        if terms:
            parts.append("## 相关术语\n" + "\n".join(f"- {t['fqn']}｜{t['display_name']}：{_one_line(t['description'], 300) or '（无描述）'}" for t in terms))
        summary = self.service.summary()
        parts.append(f"## 目录概况\n数据资产 {summary['assets']} 个，服务 {summary['services']} 个，术语 {summary['glossary_terms']} 个，标签 {summary['tags']} 个，血缘 {summary['lineage_edges']} 条。")
        context_text = "\n\n".join(parts)[:MAX_ASK_CHARS]
        configured = bool(self.llm and self.llm.configured())
        answer = ""
        provider = self.llm.status() if self.llm else {"provider": "none", "model": ""}
        if configured:
            prompt = f"元数据目录上下文：\n{context_text}\n\n用户问题：{text}"
            answer = SqlGenerator(self.llm).complete_text(prompt, system=CATALOG_SYSTEM_PROMPT)
        return {
            "question": text,
            "answer": answer.strip(),
            "configured": configured,
            "provider": provider.get("provider", "none"),
            "model": provider.get("model", ""),
            "context": {"markdown": context_text, "entities": hits, "terms": terms, "focus": summary_of_context(focus) if focus else None},
            "detail": "" if configured else "尚未配置模型：下面是助手本应看到的目录上下文；在「智能问数 → 模型设置」配置模型后即可生成回答。",
        }

    # =====================================================================================
    # bots and AI documentation
    # =====================================================================================
    def ensure_bot(self) -> None:
        if self.store.get_entity("user", fqn=AGENT_USER) is None:
            try:
                self.service.create(
                    "user",
                    {"name": AGENT_USER, "display_name": "AI 代理（MCP）", "email": f"{AGENT_USER}@localhost", "is_bot": True},
                    user=AGENT_USER, silent=True,
                )
            except MetadataError:
                pass

    def suggest_descriptions(self, entity_type: str | None, ref: str) -> dict[str, Any]:
        """Descriptions for an asset and its columns drafted by the configured model."""
        if not self.llm or not self.llm.configured():
            raise MetadataError(400, "尚未配置模型，无法生成描述建议。请先在「智能问数 → 模型设置」中配置模型。")
        context = self.entity_context(entity_type, ref)
        entity = context["entity"]
        prompt = "请为下面的数据资产生成描述建议。\n\n" + context["markdown"][:MAX_ASK_CHARS]
        data = SqlGenerator(self.llm).complete_json(prompt, system=SUGGEST_SYSTEM_PROMPT, schema=SUGGEST_SCHEMA)
        current = {c["name"]: c for c in entity.get("columns") or []}
        columns = []
        for item in data.get("columns") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            text = str(item.get("description") or "").strip()
            if name in current and text:
                columns.append({"name": name, "description": text[:2000], "current": current[name].get("description") or ""})
        status = self.llm.status()
        return {
            "entity": summary_of_context(context),
            "description": str(data.get("description") or "").strip()[:4000],
            "current_description": entity.get("description") or "",
            "columns": columns,
            "provider": status.get("provider", ""),
            "model": status.get("model", ""),
        }

    # =====================================================================================
    # tools (MCP and function calling)
    # =====================================================================================
    def tools(self) -> list[dict[str, Any]]:
        string = {"type": "string"}
        strings = {"type": "array", "items": {"type": "string"}}
        tools = [
            _tool("search_metadata", "在元数据目录中搜索数据资产（表、仪表板、工作流、术语等），返回名称、描述、责任人、标签和字段名。", {"query": {**string, "description": "关键词，支持中文与英文"}, "entity_types": {**strings, "description": "限定实体类型，例如 table、dashboard、glossaryTerm"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"]),
            _tool("get_entity_details", "读取一个数据资产的完整上下文：描述、责任人、分级、数据域、标签、术语定义、字段、上下游血缘、近期查询与使用情况。", {"fqn": {**string, "description": "完整名称（FQN）或实体 ID"}, "entity_type": {**string, "description": "实体类型，缺省时自动识别"}, "include_columns": {"type": "boolean", "default": True}}, ["fqn"]),
            _tool("get_entity_lineage", "读取数据资产的血缘图：上游与下游节点、边及字段级映射。", {"fqn": string, "entity_type": string, "upstream_depth": {"type": "integer", "minimum": 0, "maximum": 6, "default": 3}, "downstream_depth": {"type": "integer", "minimum": 0, "maximum": 6, "default": 3}}, ["fqn"]),
            _tool("get_glossary_terms", "列出业务术语及其定义、同义词、状态，可按关键词或术语库过滤。", {"query": string, "glossary": {**string, "description": "术语库名称"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, []),
            _tool("create_glossary_term", "在术语库中创建一个业务术语。", {"glossary": {**string, "description": "术语库名称（FQN）"}, "name": string, "description": string, "display_name": string, "synonyms": strings, "parent": {**string, "description": "上级术语 FQN，可选"}}, ["glossary", "name", "description"]),
            _tool("patch_entity", "更新数据资产的描述、显示名称、标签、术语、责任人、分级或字段描述；未提供的字段保持不变。", {"fqn": string, "entity_type": string, "description": string, "display_name": string, "tags": {**strings, "description": "分类标签 FQN 列表，例如 PII.Sensitive；提供时替换全部标签"}, "glossary_terms": {**strings, "description": "术语 FQN 列表；提供时替换全部术语"}, "owners": {**strings, "description": "用户或团队名称"}, "tier": {**string, "description": "Tier1–Tier5，空字符串清除"}, "columns": {"type": "array", "items": {"type": "object", "properties": {"name": string, "description": string, "display_name": string}, "required": ["name"]}}}, ["fqn"]),
            _tool("add_lineage", "在两个数据资产之间登记一条血缘边（上游 → 下游）。", {"from_fqn": string, "to_fqn": string, "description": string, "sql": string}, ["from_fqn", "to_fqn"]),
            _tool("list_services", "列出目录中的服务（数据库、消息、仪表板、工作流等）及其资产数量。", {}, []),
            _tool("list_datasources", "列出平台已注册的数据源及其连接状态，供 query_datasource 使用。", {}, []),
            _tool("get_datasource_context", "读取一个数据源的表结构与业务语义（描述、字段含义、标签、术语），用于生成 SQL。", {"datasource_id": string}, ["datasource_id"]),
            _tool("get_data_insights", "读取目录健康度：资产数量、描述/责任人/分级覆盖率、按类型统计与 KPI 进展。", {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}}, []),
        ]
        if self.queries is not None:
            tools.append(_tool("query_datasource", "在指定数据源上执行一条只读 SQL（仅 SELECT/WITH，自动限制行数），返回列名与数据行。", {"datasource_id": string, "sql": string, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["datasource_id", "sql"]))
        if self.metrics is not None:
            tools.append(_tool("list_metrics", "列出语义模型中定义的业务指标（唯一口径）：名称、含义、计算表达式、所属模型与可用维度。", {"query": {**string, "description": "按名称、同义词或说明过滤，可选"}}, []))
            tools.append(_tool(
                "query_metric",
                "按语义模型计算指标：给定模型、指标名和维度（可带时间粒度）与筛选条件，引擎生成 SQL 并在数据源上执行，返回聚合结果。",
                {
                    "model": {**string, "description": "语义模型名称或 ID，见 list_metrics"},
                    "metrics": {**strings, "description": "指标名称列表"},
                    "dimensions": {"type": "array", "items": {"type": "object", "properties": {"field": string, "grain": {**string, "description": "时间粒度 day/week/month/quarter/year，仅时间字段"}}, "required": ["field"]}, "description": "分组维度，字段写作 数据集.字段"},
                    "filters": {"type": "array", "items": {"type": "object", "properties": {"field": string, "op": string, "value": {}}, "required": ["field"]}, "description": "筛选条件，op 支持 =、!=、>、>=、<、<=、in、not_in、like、between、is_null、not_null"},
                    "datasource_id": {**string, "description": "执行数据源，缺省为本地示例数据"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                ["model", "metrics"],
            ))
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        args = arguments if arguments is not None else {}
        if not isinstance(args, dict):
            raise MetadataError(400, "参数必须是对象。")
        handler = self._tool_handlers().get(name)
        tool = next((item for item in self.tools() if item["name"] == name), None)
        if handler is None or tool is None:
            raise MetadataError(404, f"未知的工具：{name}")
        check_arguments(tool, args)
        return handler(args)

    def _tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "search_metadata": lambda a: {"items": self.search(a.get("query"), entity_types=a.get("entity_types"), limit=a.get("limit", 10))},
            "get_entity_details": self._entity_tool,
            "get_entity_lineage": lambda a: self.lineage.graph(a.get("entity_type"), _required(a, "fqn"), upstream_depth=a.get("upstream_depth", 3), downstream_depth=a.get("downstream_depth", 3)),
            "get_glossary_terms": lambda a: {"items": self.glossary_terms(query=a.get("query"), glossary=a.get("glossary"), limit=a.get("limit", 50))},
            "create_glossary_term": self._create_term,
            "patch_entity": self._patch_entity,
            "add_lineage": lambda a: self.lineage.add_edge({"from_fqn": _required(a, "from_fqn"), "to_fqn": _required(a, "to_fqn"), "description": a.get("description") or "", "sql": a.get("sql") or "", "source": "manual"}, user=AGENT_USER),
            "list_services": lambda a: {"items": self.services()},
            "list_metrics": lambda a: self.metrics.catalog(a.get("query")),
            "query_metric": lambda a: self.metrics.query(
                _required(a, "model"),
                {"metrics": a.get("metrics") or [], "dimensions": a.get("dimensions") or [], "filters": a.get("filters") or [], "limit": a.get("limit")},
                datasource_id=a.get("datasource_id"),
            ),
            "list_datasources": lambda a: {"items": self.registry.statuses() if self.registry is not None else []},
            "get_datasource_context": self._datasource_tool,
            "get_data_insights": lambda a: _insights_view(self.insights.overview(a.get("days", 7))) if self.insights else {},
            "query_datasource": self._query_tool,
        }

    def _entity_tool(self, a: dict[str, Any]) -> Any:
        return self.entity_context(a.get("entity_type"), _required(a, "fqn"), include_columns=a.get("include_columns", True) is not False)

    def _create_term(self, a: dict[str, Any]) -> Any:
        glossary = self.service.row("glossary", _required(a, "glossary"))
        payload = {
            "name": _required(a, "name"),
            "description": a.get("description") or "",
            "display_name": a.get("display_name") or "",
            "synonyms": [str(s) for s in a.get("synonyms") or []],
            "parent_fqn": str(a["parent"]) if a.get("parent") else glossary["fqn"],
        }
        return self.service.create("glossaryTerm", payload, user=AGENT_USER)

    def _patch_entity(self, a: dict[str, Any]) -> Any:
        row = self.service.row(a.get("entity_type"), _required(a, "fqn"))
        patch: dict[str, Any] = {}
        for key in ("description", "display_name"):
            if key in a and a[key] is not None:
                patch[key] = str(a[key])
        if "owners" in a and a["owners"] is not None:
            patch["owners"] = [str(o) for o in a["owners"]]
        if "tier" in a and a["tier"] is not None:
            patch["tier"] = str(a["tier"])
        if ("tags" in a and a["tags"] is not None) or ("glossary_terms" in a and a["glossary_terms"] is not None):
            current = self.store.tags_for_targets([row["fqn"]])
            keep_tags = [t["tag_fqn"] for t in current if t["source"] == "classification"] if a.get("tags") is None else [str(t) for t in a["tags"]]
            keep_terms = [t["tag_fqn"] for t in current if t["source"] == "glossary"] if a.get("glossary_terms") is None else [str(t) for t in a["glossary_terms"]]
            patch["tags"] = [{"tag_fqn": t, "source": "classification"} for t in keep_tags] + [{"tag_fqn": t, "source": "glossary"} for t in keep_terms]
        if isinstance(a.get("columns"), list) and a["columns"]:
            columns = []
            for item in a["columns"]:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                column = {"name": str(item["name"])}
                if item.get("description") is not None:
                    column["description"] = str(item["description"])
                if item.get("display_name") is not None:
                    column["display_name"] = str(item["display_name"])
                columns.append(column)
            if columns:
                patch["columns"] = columns
        if not patch:
            raise MetadataError(400, "没有可更新的字段。")
        return self.service.update(row["entity_type"], row["id"], patch, user=AGENT_USER)

    def _datasource_tool(self, a: dict[str, Any]) -> Any:
        datasource_id = _required(a, "datasource_id")
        schema = ""
        if self.registry is not None:
            record = self.registry.record(datasource_id)
            schema = self.registry.schema_context(datasource_id)
            label = record["name"]
        else:
            label = datasource_id
        return {"datasource_id": datasource_id, "name": label, "schema": schema, "semantics": self.datasource_context(datasource_id)}

    def _query_tool(self, a: dict[str, Any]) -> Any:
        if self.queries is None:
            raise MetadataError(400, "当前实例未开放数据查询工具。")
        result = self.queries.run_sql(_required(a, "datasource_id"), _required(a, "sql"), a.get("limit"))
        return {"columns": result["columns"], "rows": result["rows"][:1000], "truncated": bool(result.get("truncated")), "elapsed_ms": result.get("elapsed_ms"), "sql": result["sql"], "source": result["source"]}

    # =====================================================================================
    # resources and prompts
    # =====================================================================================
    def resources(self) -> list[dict[str, Any]]:
        return [
            {"uri": "lattice://catalog/summary", "name": "目录概况", "description": "数据资产、服务、术语、标签与血缘的数量。", "mimeType": "application/json"},
            {"uri": "lattice://glossary", "name": "业务术语表", "description": "全部术语库与术语的定义（Markdown）。", "mimeType": "text/markdown"},
            {"uri": "lattice://services", "name": "服务列表", "description": "目录中的服务及资产数量。", "mimeType": "application/json"},
            {"uri": "lattice://insights", "name": "目录健康度", "description": "描述、责任人与分级覆盖率及 KPI 进展。", "mimeType": "application/json"},
        ]

    def resource_templates(self) -> list[dict[str, Any]]:
        return [
            {"uriTemplate": "lattice://entity/{entity_type}/{fqn}", "name": "数据资产上下文", "description": "一个资产的完整上下文（Markdown）。", "mimeType": "text/markdown"},
            {"uriTemplate": "lattice://lineage/{fqn}", "name": "血缘图", "description": "一个资产上下游三层的血缘图。", "mimeType": "application/json"},
            {"uriTemplate": "lattice://datasource/{datasource_id}/context", "name": "数据源语义", "description": "一个数据源的表结构与业务语义，用于生成 SQL。", "mimeType": "text/plain"},
        ]

    def read_resource(self, uri: Any) -> dict[str, Any]:
        text = str(uri or "")
        if not text.startswith("lattice://"):
            raise MetadataError(404, f"未知的资源：{text}")
        path = text[len("lattice://"):]
        if path == "catalog/summary":
            return {"uri": text, "mimeType": "application/json", "text": _json(self.service.summary())}
        if path == "glossary":
            return {"uri": text, "mimeType": "text/markdown", "text": self.glossary_markdown()}
        if path == "services":
            return {"uri": text, "mimeType": "application/json", "text": _json({"items": self.services()})}
        if path == "insights":
            return {"uri": text, "mimeType": "application/json", "text": _json(_insights_view(self.insights.overview(7)) if self.insights else {})}
        match = re.match(r"^entity/([A-Za-z]+)/(.+)$", path)
        if match:
            context = self.entity_context(match.group(1), match.group(2))
            return {"uri": text, "mimeType": "text/markdown", "text": context["markdown"]}
        match = re.match(r"^lineage/(.+)$", path)
        if match:
            return {"uri": text, "mimeType": "application/json", "text": _json(self.lineage.graph(None, match.group(1)))}
        match = re.match(r"^datasource/([^/]+)/context$", path)
        if match:
            return {"uri": text, "mimeType": "text/plain", "text": _json(self._datasource_tool({"datasource_id": match.group(1)}))}
        raise MetadataError(404, f"未知的资源：{text}")

    def glossary_markdown(self) -> str:
        lines = ["# 业务术语表"]
        for glossary in self.service.glossaries():
            lines.append(f"\n## {glossary['display_name'] or glossary['name']}（{glossary['fqn']}）")
            if glossary.get("description"):
                lines.append(_one_line(glossary["description"], 500))
            for term in self.service.glossary_terms(glossary["fqn"])["terms"]:
                synonyms = f"；同义词：{'、'.join(term.get('synonyms') or [])}" if term.get("synonyms") else ""
                lines.append(f"- **{term['display_name'] or term['name']}**（{term['fqn']}，{term.get('status', '')}）：{_one_line(term.get('description') or '', 400) or '（无描述）'}{synonyms}")
        return "\n".join(lines)

    def prompts(self) -> list[dict[str, Any]]:
        return [
            {"name": "describe_asset", "description": "根据目录中的元数据为一个数据资产撰写业务说明。", "arguments": [{"name": "fqn", "description": "资产的完整名称", "required": True}]},
            {"name": "impact_analysis", "description": "分析修改某个数据资产会影响哪些下游对象。", "arguments": [{"name": "fqn", "description": "资产的完整名称", "required": True}]},
            {"name": "write_sql", "description": "结合表结构与业务语义，为一个数据源上的问题生成只读 SQL。", "arguments": [{"name": "datasource_id", "description": "数据源 ID", "required": True}, {"name": "question", "description": "业务问题", "required": True}]},
        ]

    def get_prompt(self, name: Any, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        if name == "describe_asset":
            context = self.entity_context(None, _required(args, "fqn"))
            text = f"请根据下面的元数据，用中文为数据资产 {context['entity']['fqn']} 撰写一段面向业务用户的说明（用途、关键字段、责任人、注意事项），不要编造目录中没有的信息。\n\n{context['markdown']}"
            return {"description": "撰写数据资产说明", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if name == "impact_analysis":
            impact = self.lineage.impact(None, _required(args, "fqn"))
            text = f"下面是 {impact['entity']['fqn']} 的下游影响范围（JSON）。请用中文说明修改该对象会影响哪些对象、按类型分组，并给出发布变更前应通知的责任方和检查清单。\n\n{_json(impact)}"
            return {"description": "影响分析", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if name == "write_sql":
            data = self._datasource_tool({"datasource_id": _required(args, "datasource_id")})
            text = f"数据源：{data['name']}\n可用表与字段：\n{data['schema'] or '（未能读取表结构）'}\n\n{data['semantics'] or ''}\n\n请只生成一条只读 SELECT 查询回答：{_required(args, 'question')}"
            return {"description": "生成只读 SQL", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        raise MetadataError(404, f"未知的提示：{name}")


class McpServer:
    """Model Context Protocol server: JSON-RPC 2.0 messages in, responses out."""

    MAX_SESSIONS = 1000

    def __init__(self, context: ContextService):
        self.context = context
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self._lock = threading.Lock()

    # ----- transport helpers ---------------------------------------------------------------------
    def handle_http(self, message: Any) -> tuple[Any, str | None]:
        """Handle one HTTP body; the second value is a new session id to announce."""
        meta: dict[str, Any] = {}
        response = self.handle(message, meta=meta)
        return response, meta.get("session")

    def known_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.sessions

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            return self.sessions.pop(session_id, None) is not None

    # ----- JSON-RPC ----------------------------------------------------------------------------------
    def handle(self, message: Any, *, meta: dict[str, Any] | None = None) -> Any:
        """One message or a batch; ``None`` means nothing to send back (notifications)."""
        if isinstance(message, list):
            if not message:
                return _error(None, -32600, "Invalid Request")
            responses = [r for r in (self.handle(item, meta=meta) for item in message) if r is not None]
            return responses or None
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        identifier = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if method is None and ("result" in message or "error" in message):
                return None  # a client response to a server request; none are pending
            return _error(identifier, -32600, "Invalid Request")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(identifier, -32602, "params must be an object") if identifier is not None else None
        if identifier is None:
            return None  # notifications (initialized, cancelled …) need no answer
        try:
            result = self._dispatch(method, params, meta)
        except _MethodNotFound as error:
            return _error(identifier, -32601, str(error))
        except _InvalidParams as error:
            return _error(identifier, -32602, str(error))
        except (MetadataError, MetadataStoreError, LlmError, ValueError, KeyError, OSError, TypeError, AttributeError) as error:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": identifier, "result": {"content": [{"type": "text", "text": str(error)[:2000]}], "isError": True}}
            return _error(identifier, -32603, str(error)[:2000])
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any], meta: dict[str, Any] | None) -> Any:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or LATEST_PROTOCOL)
            version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL
            session = uuid.uuid4().hex
            with self._lock:
                if len(self.sessions) >= self.MAX_SESSIONS:
                    oldest = min(self.sessions, key=lambda key: self.sessions[key]["ts"])
                    self.sessions.pop(oldest, None)
                client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
                self.sessions[session] = {"client": client, "protocol": version, "ts": now_ms()}
            if meta is not None:
                meta["session"] = session
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Lattice 元数据目录", "version": SERVER_VERSION},
                "instructions": "Lattice 数据平台的元数据目录：用 search_metadata 查找资产，用 get_entity_details 读取描述、字段、术语与血缘，用 get_datasource_context 获取生成 SQL 所需的表结构与业务语义，用 query_datasource 执行只读 SQL。写操作（patch_entity、create_glossary_term、add_lineage）以用户 mcp-agent 的身份记录到活动信息流。",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.context.tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in {t["name"] for t in self.context.tools()}:
                raise _InvalidParams(f"Unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _InvalidParams("arguments must be an object")
            with self._lock:
                self.calls += 1
            result = self.context.call_tool(name, arguments)
            text = result if isinstance(result, str) else _json(result)
            structured = result if isinstance(result, dict) else {"result": result}
            return {"content": [{"type": "text", "text": text[:200000]}], "structuredContent": structured, "isError": False}
        if method == "resources/list":
            return {"resources": self.context.resources()}
        if method == "resources/templates/list":
            return {"resourceTemplates": self.context.resource_templates()}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise _InvalidParams("uri is required")
            return {"contents": [self.context.read_resource(uri)]}
        if method == "prompts/list":
            return {"prompts": self.context.prompts()}
        if method == "prompts/get":
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _InvalidParams("arguments must be an object")
            return self.context.get_prompt(params.get("name"), arguments)
        if method in {"logging/setLevel", "completion/complete"}:
            return {} if method == "logging/setLevel" else {"completion": {"values": [], "hasMore": False}}
        raise _MethodNotFound(f"Method not found: {method}")

    def status(self) -> dict[str, Any]:
        with self._lock:
            sessions, calls = len(self.sessions), self.calls
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocol_versions": list(PROTOCOL_VERSIONS),
            "sessions": sessions,
            "calls": calls,
            "tools": [t["name"] for t in self.context.tools()],
            "endpoint": "/api/metadata/mcp",
            "user": AGENT_USER,
        }


class _MethodNotFound(Exception):
    pass


class _InvalidParams(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------------------------
def query_tokens(text: str) -> list[tuple[str, int]]:
    """Words and CJK pieces of a question, each with a weight (longer pieces weigh more)."""
    tokens: dict[str, int] = {}
    lowered = str(text or "").lower()
    for word in re.findall(r"[a-z0-9_]{2,}", lowered):
        if word not in STOPWORDS:
            tokens[word] = max(tokens.get(word, 0), 3)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        for size in (4, 3, 2):
            for start in range(0, len(run) - size + 1):
                piece = run[start : start + size]
                if piece not in STOPWORDS:
                    tokens[piece] = max(tokens.get(piece, 0), size)
    return list(tokens.items())[:300]


def _score(tokens: list[tuple[str, int]], haystack: str) -> int:
    return sum(weight for token, weight in tokens if token in haystack)


def entity_markdown(context: dict[str, Any]) -> str:
    entity = context["entity"]
    lines = [f"# {entity['display_name'] or entity['name']}（{entity['type_label']}）", f"- FQN：`{entity['fqn']}`"]
    if entity.get("service_type"):
        lines.append(f"- 服务类型：{entity['service_type']}")
    lines.append(f"- 描述：{_one_line(entity.get('description') or '', 2000) or '（目录中未记录）'}")
    owners = entity.get("owners") or []
    lines.append("- 责任人：" + ("、".join(f"{o['display_name']}（{o['entity_type']}）" for o in owners) if owners else "（无）"))
    if entity.get("tier"):
        lines.append(f"- 分级：{split_fqn(entity['tier'])[-1]}")
    if entity.get("domain"):
        lines.append(f"- 数据域：{entity['domain']['display_name']}（{entity['domain']['fqn']}）")
    if entity.get("data_products"):
        lines.append("- 数据产品：" + "、".join(p["display_name"] for p in entity["data_products"]))
    tags = entity.get("tags") or []
    if tags:
        lines.append("- 标签：" + "、".join(t["tag_fqn"] for t in tags))
    terms = context.get("glossary") or []
    entity_terms = [t["tag_fqn"] for t in entity.get("glossary_terms") or []]
    if entity_terms:
        lines.append("- 术语：" + "、".join(entity_terms))
    extension = entity.get("extension") or {}
    if extension:
        lines.append("- 自定义属性：" + "；".join(f"{k}={_one_line(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 120)}" for k, v in list(extension.items())[:20]))
    if entity.get("table_type"):
        lines.append(f"- 表类型：{entity['table_type']}" + (f"，约 {entity['row_count']} 行" if entity.get("row_count") is not None else ""))
    usage = context.get("usage") or {}
    if usage:
        lines.append(f"- 近 30 天使用：查询 {usage.get('queries', 0)} 次，浏览 {usage.get('views', 0)} 次")
    columns = entity.get("columns") or []
    if columns:
        lines.append(f"\n## 字段（{len(columns)}）")
        lines.append("| 字段 | 类型 | 说明 | 标签/术语 |")
        lines.append("| --- | --- | --- | --- |")
        for column in columns[:200]:
            labels = [t["tag_fqn"] for t in column.get("tags") or []] + [t["tag_fqn"] for t in column.get("glossary_terms") or []]
            name = column["name"] + (f"（{column['display_name']}）" if column.get("display_name") else "")
            constraint = " 主键" if column.get("constraint") == "PRIMARY_KEY" else ""
            lines.append(f"| {name} | {column.get('data_type_display') or column.get('data_type') or ''}{constraint} | {_one_line(column.get('description') or '', 200)} | {'、'.join(labels)} |")
    if terms:
        lines.append("\n## 术语定义")
        for term in terms:
            synonyms = f"；同义词：{'、'.join(term['synonyms'])}" if term["synonyms"] else ""
            lines.append(f"- {term['fqn']}｜{term['display_name']}：{_one_line(term['description'], 400) or '（无描述）'}{synonyms}")
    lineage = context.get("lineage") or {}
    if lineage.get("upstream") or lineage.get("downstream"):
        lines.append("\n## 血缘")
        for item in lineage.get("upstream", []):
            lines.append(f"- 上游：{item['fqn']}（{item['type_label']}，来源 {item['source']}）" + (f"：{_one_line(item['description'], 120)}" if item.get("description") else ""))
        for item in lineage.get("downstream", []):
            lines.append(f"- 下游：{item['fqn']}（{item['type_label']}，来源 {item['source']}）" + (f"：{_one_line(item['description'], 120)}" if item.get("description") else ""))
    children = context.get("children") or []
    if children:
        lines.append("\n## 下级对象")
        for group in children:
            lines.append(f"- {group['label']}：{group['total']} 个")
    queries = context.get("queries") or []
    if queries:
        lines.append("\n## 近期查询")
        for query in queries[:3]:
            lines.append("```sql\n" + query["sql"][:600] + "\n```")
    return "\n".join(lines)


def term_view(row: dict[str, Any]) -> dict[str, Any]:
    document = row["json"]
    return {
        "fqn": row["fqn"],
        "name": row["name"],
        "display_name": row["display_name"] or row["name"],
        "description": row["description"],
        "glossary": split_fqn(row["fqn"])[0],
        "status": document.get("status", ""),
        "synonyms": [str(s) for s in document.get("synonyms") or []],
        "related_terms": [str(t) for t in document.get("related_terms") or []],
        "references": document.get("references") or [],
        "parent_fqn": row["parent_fqn"],
    }


def summary_of_context(context: dict[str, Any]) -> dict[str, Any]:
    entity = context["entity"]
    return {"fqn": entity["fqn"], "entity_type": entity["entity_type"], "type_label": entity["type_label"], "display_name": entity["display_name"] or entity["name"]}


def _insights_view(overview: dict[str, Any]) -> dict[str, Any]:
    current = overview.get("current") or {}
    return {
        "period": overview.get("period"),
        "assets": current.get("assets"),
        "description_percent": current.get("description_percent"),
        "owner_percent": current.get("owner_percent"),
        "tier_percent": current.get("tier_percent"),
        "tag_percent": current.get("tag_percent"),
        "lineage_percent": current.get("lineage_percent"),
        "by_type": current.get("by_type"),
        "governance": current.get("governance"),
        "kpis": [{"name": k["name"], "chart_label": k.get("chart_label"), "current_value": k.get("current_value"), "target_value": k.get("target_value"), "progress": k.get("progress"), "status": k.get("status")} for k in overview.get("kpis") or []],
        "top_viewed": overview.get("top_viewed"),
        "top_queried": overview.get("top_queried"),
    }


def _hit_line(hit: dict[str, Any]) -> str:
    bits = [f"- {hit['fqn']}（{hit['type_label']}）"]
    if hit["display_name"] != hit["name"]:
        bits.append(hit["display_name"])
    if hit["description"]:
        bits.append("：" + _one_line(hit["description"], 200))
    if hit["owners"]:
        bits.append("；责任人：" + "、".join(hit["owners"]))
    if hit["tags"] or hit["glossary_terms"]:
        bits.append("；标签/术语：" + "、".join(hit["tags"] + hit["glossary_terms"]))
    if hit["columns"]:
        bits.append("；字段：" + "、".join(hit["columns"][:30]))
    return "".join(bits)


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


JSON_TYPES = {"string": "文本", "integer": "整数", "boolean": "布尔值", "array": "数组", "object": "对象"}


def check_arguments(tool: dict[str, Any], args: dict[str, Any]) -> None:
    """Hold tool arguments to the tool's input schema, answering in Chinese instead of failing later."""
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    for key in args:
        if key not in properties:
            raise MetadataError(400, f"工具 {tool['name']} 不支持参数 {key}。")
    for key in schema.get("required") or []:
        if args.get(key) is None or args.get(key) == "":
            raise MetadataError(400, f"缺少参数 {key}。")
    for key, value in args.items():
        if value is None:
            continue
        spec = properties[key]
        kind = spec.get("type")
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }.get(kind, True)
        if valid and kind == "array":
            item_type = (spec.get("items") or {}).get("type")
            if item_type == "string":
                valid = all(isinstance(item, str) for item in value)
            elif item_type == "object":
                valid = all(isinstance(item, dict) for item in value)
        if not valid:
            raise MetadataError(400, f"参数 {key} 应为{JSON_TYPES.get(kind, kind)}。")
        if kind == "integer" and (value < spec.get("minimum", value) or value > spec.get("maximum", value)):
            raise MetadataError(400, f"参数 {key} 必须在 {spec.get('minimum', '')}–{spec.get('maximum', '')} 之间。")


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise MetadataError(400, f"缺少参数 {key}。")
    return str(value).strip()


def _without(data: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != key}


def _one_line(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


__all__ = ["ContextService", "McpServer", "entity_markdown", "term_view", "CATALOG_SYSTEM_PROMPT", "type_label", "day_of"]
