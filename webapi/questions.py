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

"""Answer SQL and natural-language questions against any registered source.

``QueryService.answer`` is a generator of ``(event, data)`` pairs so the same
logic backs the JSON endpoint and the SSE stream: ``step`` events describe the
reasoning, ``sql`` carries the statement, ``result`` the executed payload.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any, Callable, Iterator

import duckdb
import sqlglot
from sqlglot import exp

from .connectors import CONNECTORS, DIALECT_LABELS, ConnectorError, chart_hint, guard_sql, referenced_tables
from .datasources import DataSourceError, DataSourceRegistry
from .llm import LlmError, LlmSettings, SqlGenerator
from .query import QueryStore
from .observability import observe
from .query_cache import QueryCache
from .question_memory import QuestionMemory
from .schema_linking import (
    INFERRED,
    SEMANTIC,
    SENSITIVE_COLUMN,
    TEXT_TYPE,
    TOP_K,
    KEY_COLUMN,
    JoinEdge,
    QuestionScope,
    SchemaLink,
    TableInfo,
    edges_from_foreign_keys,
    join_candidates,
    link_schema,
    render_link,
    table_menu,
)
from .semantic_planner import Plan, catalog_text, plan_locally, plan_with_model
from .semantic_query import SemanticQueryError, source_tables
from .sql_audit import AuditReport, audit_sql, duplicate_key_probe_sql, parse_schema_context, value_probe_sql

LOCAL_SAMPLE = "local-sample"
#: Tables every local demo engine is seeded with; the built-in rules need them.
SAMPLE_TABLES = (
    "t_lattice_order_items",
    "t_lattice_orders",
    "t_lattice_customers",
    "t_lattice_products",
    "t_lattice_sellers",
    "t_lattice_payments",
)


#: How many times a model may rewrite a statement after seeing what was wrong with it.
MAX_CORRECTIONS = 2
SCHEMA_INDEX_SECONDS = 300
#: Reading table structures stops here on a slow engine; questions then work from what was read.
SCHEMA_INDEX_BUDGET_SECONDS = 20
MAX_INDEXED_TABLES = 300
MAX_JOIN_PROBES = 30
JOIN_SAMPLE = 200
#: A key this share of whose sampled values exist in the other table is taken to reference it.
JOIN_MATCH_RATIO = 0.95
MAX_ENUM_COLUMNS = 15
MAX_ENUM_VALUES = 30
PICK_TABLES_SYSTEM = (
    "你负责为数据问题挑选需要用到的表。根据表名、注释和字段判断，只选回答问题必需的表，包括用于筛选、分组和关联的表，"
    '不要超过 8 张。输出 JSON：{"tables": ["表名"]}。'
)
PICK_TABLES_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": {"type": "string"}}},
    "required": ["tables"],
    "additionalProperties": False,
}


class QueryError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def source_label(record: dict[str, Any]) -> str:
    type_label = CONNECTORS[record["type"]].label
    name = record["name"]
    return name if type_label in name else f"{name} · {type_label}"


class QueryService:
    def __init__(
        self, store: QueryStore, registry: DataSourceRegistry, llm: LlmSettings, cache: QueryCache | None = None
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.generator = SqlGenerator(llm)
        # Results are read-only, so repeating a question or a report query answers
        # from here until the entry expires or is invalidated; ``refresh`` bypasses it.
        self.cache = cache if cache is not None else QueryCache.from_env()
        # Called with every executed payload (the metadata catalog attributes usage).
        self.observers: list[Callable[[dict[str, Any]], Any]] = []
        # Returns business semantics for a data source, appended to the model's schema.
        self.annotator: Callable[[dict[str, Any]], str] | None = None
        # The semantic query engine: when a model covers the source, its metric definitions
        # answer the question and the language model only maps words onto them.
        self.semantic: Any = None
        # Confirmed answers and the record of every question live beside the query history.
        self.memory = QuestionMemory(store.history_db)
        self.scope = QuestionScope(store.history_db.parent / "question-scope.json")
        self._schema_index: dict[str, tuple[float, list[TableInfo], list[JoinEdge], bool]] = {}

    # ----- execution ---------------------------------------------------------------
    def _record(self, source_id: str | None) -> dict[str, Any]:
        try:
            return self.registry.record(source_id or LOCAL_SAMPLE)
        except DataSourceError as error:
            raise QueryError(error.status_code, str(error)) from error

    def execute(self, record: dict[str, Any], sql: str, limit: Any = None, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            hit = self.cache.get(record["id"], sql, limit)
            if hit is not None:
                return hit
        try:
            with observe("query", "engine", datasource=record["id"], engine=record.get("type", "")):
                if record["id"] == LOCAL_SAMPLE:
                    result = self.store.execute_sql(sql)
                else:
                    result = self.registry.connector(record["id"]).query(sql, limit)
        except DataSourceError as error:
            raise QueryError(error.status_code, str(error)) from error
        except (ConnectorError, sqlglot.errors.SqlglotError, duckdb.Error) as error:
            raise QueryError(400, str(error)[:800]) from error
        except ValueError as error:
            raise QueryError(400, str(error)[:800]) from error
        self.cache.put(record["id"], sql, limit, result, self._tables(record, sql))
        return {**result, "cached": False}

    def _tables(self, record: dict[str, Any], sql: str) -> list[tuple[str | None, str]]:
        """Tables a statement reads, so a table-level invalidation can find its entries."""
        try:
            return referenced_tables(guard_sql(sql, CONNECTORS[record["type"]].dialect))
        except Exception:  # noqa: BLE001 - an unparseable statement is cached without table tags
            return []

    def payload(
        self,
        record: dict[str, Any],
        title: str,
        result: dict[str, Any],
        provider: str,
        steps: list[str],
        chart: dict[str, str] | None = None,
        started: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        columns, rows = result["columns"], result["rows"]
        hint = chart_hint(columns, rows)
        if chart and chart.get("dimension") in columns and chart.get("metric") in columns:
            hint = {
                "dimension": chart["dimension"],
                "metric": chart["metric"],
                "type": chart.get("type") or "bar",
            }
        elapsed = result.get("elapsed_ms")
        if started is not None:
            elapsed = round((time.monotonic() - started) * 1000, 1)
        cached = bool(result.get("cached"))
        age = result.get("cache_age_seconds")
        if cached:
            steps = [*steps, f"命中查询缓存，返回 {age if age is not None else 0} 秒前执行的结果；需要最新数据时请刷新。"]
        payload = {
            "id": str(uuid.uuid4()),
            "title": title,
            "sql": result["sql"],
            "columns": columns,
            "rows": rows,
            "chart": hint,
            "steps": steps,
            "source": source_label(record),
            "provider": provider,
            "elapsed_ms": elapsed,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "truncated": bool(result.get("truncated")),
            "datasource_id": record["id"],
            "datasource_name": record["name"],
            "dialect": CONNECTORS[record["type"]].dialect,
            "cached": cached,
            "cache_age_seconds": age if cached else None,
            **(extra or {}),
        }
        self.store.record(payload)
        for observer in list(self.observers):
            try:
                observer(payload)
            except Exception:  # noqa: BLE001 - an observer must never fail a query
                continue
        return payload

    def run_sql(self, source_id: str | None, sql: str, limit: Any = None, refresh: bool = False) -> dict[str, Any]:
        record = self._record(source_id)
        started = time.monotonic()
        result = self.execute(record, sql, limit, refresh=refresh)
        steps = [
            f"在 {source_label(record)} 上执行只读 SQL（{DIALECT_LABELS.get(CONNECTORS[record['type']].dialect, '')}）。",
            f"返回 {len(result['rows'])} 行" + ("（已截断）。" if result.get("truncated") else "。"),
        ]
        return self.payload(record, "SQL 查询结果", result, "sql", steps, started=started)

    def preview(self, source_id: str, schema: str | None, name: str, limit: Any = None) -> dict[str, Any]:
        record = self._record(source_id)
        started = time.monotonic()
        try:
            if record["id"] == LOCAL_SAMPLE:
                connector = self.registry.connector(record["id"])
                result = self.store.execute_sql(
                    f"SELECT * FROM {connector.quote(name)}"
                    if not schema or schema == "main"
                    else f"SELECT * FROM {connector.qualified(schema, name)}"
                )
                if limit:
                    result["rows"] = result["rows"][: int(limit)]
            else:
                result = self.registry.connector(record["id"]).preview(schema, name, limit)
        except DataSourceError as error:
            raise QueryError(error.status_code, str(error)) from error
        except (ConnectorError, sqlglot.errors.SqlglotError, duckdb.Error, ValueError) as error:
            raise QueryError(400, str(error)[:800]) from error
        title = f"{schema + '.' if schema else ''}{name} 预览"
        return self.payload(record, title, result, "sql", [f"预览 {title}。"], started=started)

    # ----- natural language ----------------------------------------------------------
    def answer(
        self, question: str | None, sql: str | None, source_id: str | None, refresh: bool = False
    ) -> Iterator[tuple[str, Any]]:
        """Answer, keeping a record of every question: a refusal is as informative as an answer."""
        if sql or not (question or "").strip():
            yield from self._answer(question, sql, source_id, refresh)
            return
        text = str(question).strip()
        source = source_id or LOCAL_SAMPLE
        try:
            for name, data in self._answer(question, sql, source_id, refresh):
                if name == "result":
                    self.memory.log(
                        source, text, "answered", provider=data.get("provider", ""), planner=(data.get("semantic") or {}).get("planner") or "",
                        sql=data.get("sql", ""), result_id=data.get("id", ""),
                    )
                yield name, data
        except Exception as error:
            refused = isinstance(error, QueryError) and error.status_code in {400, 409, 422}
            self.memory.log(source, text, "refused" if refused else "error", detail=str(error))
            raise

    def _answer(
        self, question: str | None, sql: str | None, source_id: str | None, refresh: bool = False
    ) -> Iterator[tuple[str, Any]]:
        record = self._record(source_id)
        if sql:
            yield "step", {"text": f"在 {source_label(record)} 上执行只读 SQL。"}
            payload = self.run_sql(record["id"], sql, refresh=refresh)
            yield "sql", {"sql": payload["sql"], "title": payload["title"]}
            yield "result", payload
            return
        question = (question or "").strip()
        if not question:
            raise QueryError(400, "请输入问题。")
        dialect = CONNECTORS[record["type"]].dialect
        dialect_label = DIALECT_LABELS.get(dialect, dialect)
        label = source_label(record)
        started = time.monotonic()
        yield "step", {"text": f"数据源：{label}；目标方言：{dialect_label}。"}
        governed = yield from self._answer_from_model(record, question, refresh, label)
        if governed is True:
            return
        context = governed if isinstance(governed, str) else ""
        if self.llm.configured():
            status = self.llm.status()
            yield "step", {"text": f"读取表结构，交给模型 {status['model']} 生成 SQL。"}
            try:
                pinned = self.memory.exact(record["id"], question)
                if pinned and pinned["kind"] == "sql":
                    answered = yield from self._run_confirmed_sql(record, pinned, question, dialect, dialect_label, refresh, started)
                    if answered:
                        return
                link = self._linked_schema(record, question)
                schema_text = render_link(link, self._enum_values(record, link)) if link else self._schema_context(record)
                if link:
                    schema_text = self._annotate(record, schema_text)
                    if link.seeds:
                        yield "step", {"text": f"Schema Linking：从 {link.total} 张表中选出 {'、'.join(table.name for table in link.tables)}，已知关联 {len(link.edges)} 条。"}
                    elif link.edges:
                        yield "step", {"text": f"读取到 {len(link.edges)} 条已知的表关联，作为关联路径的约束。"}
                if context:
                    schema_text = f"{schema_text}\n\n已定义的业务口径，涉及这些指标时必须按此计算：\n{context}"
                examples = self.memory.examples(record["id"], question, "sql")
                if examples:
                    schema_text += "\n\n已由人工确认的相似问答，业务口径应与之保持一致：\n" + "\n".join(f"- 问题：{item['question']}\n  SQL：{item['definition']['sql']}" for item in examples)
                    yield "step", {"text": f"参考 {len(examples)} 条人工确认过的相似问答。"}
                outcome = yield from self._generate_checked(record, question, dialect, dialect_label, label, schema_text, refresh)
                plan, result, repaired, audit = outcome
            except LlmError as error:
                # A question the semantic layer already set aside must not fall through to
                # the keyword rules, which would answer 月度环比 as plain monthly sales.
                if record["id"] != LOCAL_SAMPLE or context:
                    raise QueryError(502, f"模型生成 SQL 失败：{error}") from error
                yield "step", {"text": f"模型不可用（{error}），改用本地规则回答示例问题。"}
            except (DataSourceError, ConnectorError) as error:
                raise QueryError(400, f"读取表结构失败：{error}") from error
            else:
                for step in plan["steps"]:
                    yield "step", {"text": step}
                steps = list(plan["steps"])
                if repaired:
                    steps.append(f"模型返回的 SQL 不能在该引擎直接执行，已按 {dialect_label} 方言转换后重试。")
                    yield "step", {"text": steps[-1]}
                steps.append("这条 SQL 由模型直接生成，未经过语义层：" + ("结果中的指标口径请结合 SQL 确认。" if not context else "已要求其按语义模型的指标定义计算，请结合 SQL 确认。"))
                yield "step", {"text": steps[-1]}
                steps.append(f"执行只读 SQL，返回 {len(result['rows'])} 行" + ("（已截断）。" if result.get("truncated") else "。"))
                chart = {"dimension": plan["dimension"], "metric": plan["metric"], "type": plan["chart_type"]}
                extra = {"verification": self._verification_of(audit), "question": question}
                yield "result", self.payload(record, plan["title"], result, "llm", steps, chart, started, extra=extra)
                return
        try:
            title, proposed = QueryStore.translate(question)
        except ValueError as error:
            raise QueryError(400, str(error)) from error
        self.require_sample_tables(record, proposed, label)
        yield "step", {"text": "识别问题中的维度与指标，匹配示例业务表（本地规则）。"}
        proposed = self.localize(proposed, dialect, dialect_label)
        yield "sql", {"sql": proposed.strip(), "title": title}
        result = self.execute(record, proposed, refresh=refresh)
        steps = [
            "识别问题中的维度与指标，匹配示例业务表。",
            f"按 {dialect_label} 方言生成只读 SQL 并执行安全检查。",
            f"在{label}中执行，返回 {len(result['rows'])} 行"
            + ("（已截断至 1000 行）。" if result.get("truncated") else "。"),
        ]
        yield "result", self.payload(record, title, result, "rules", steps, started=started)

    # ----- the semantic layer ----------------------------------------------------------
    def _answer_from_model(
        self, record: dict[str, Any], question: str, refresh: bool, label: str
    ) -> Iterator[tuple[str, Any]]:
        """Answer from a semantic model when one covers the source.

        Returns True when answered, a catalog text when the question needs a computation
        the semantic layer lacks (free SQL is then told the definitions), and None when
        no model covers the source. A concept the model does not define is refused: it
        must not reach free SQL, where a language model would make up a definition.
        """
        if self.semantic is None:
            return None
        models = [model for model in self.semantic.models_for(record) if self._model_in_scope(record, model)]
        if not models:
            return None
        pinned = self.memory.exact(record["id"], question)
        if pinned and pinned["kind"] == "semantic":
            definition = pinned["definition"]
            model = next((item for item in models if item.id == definition.get("model_id")), None)
            if model is not None and model.fingerprint() == definition.get("fingerprint"):
                plan = Plan("ok", request=definition["request"], title=pinned["title"], planner="verified", steps=["这个问题的映射已由人工确认，直接使用。"])
                yield from self._run_plan(record, model, plan, question, refresh)
                return True
            yield "step", {"text": "这个问题确认时使用的口径定义已经变化，确认不再生效，重新解读。"}
        attempts: list[tuple[Any, dict[str, list[str]], Plan]] = []
        for model in models:
            values = {key: options for key, options in self.semantic.dimension_values(model, record).items() if self._field_in_scope(record, model, key)}
            plan = plan_locally(question, model, values)
            if plan.status == "ok":
                yield from self._run_plan(record, model, plan, question, refresh)
                return True
            attempts.append((model, values, plan))
        # Words that need interpreting go to the model most of the question matched.
        # With no word matched anywhere, the richest model is the one most likely to define it.
        model, values, local = max(attempts, key=lambda item: (item[2].score, len(item[0].metrics)))
        supported = "、".join(sorted((metric.description.split("：")[0] or metric.name) for metric in model.metrics.values()))
        if not self.llm.configured():
            raise QueryError(
                422,
                f"无法按已定义的口径回答：{local.reason}语义模型 {model.name} 现有指标：{supported}。"
                "请换用这些指标提问，或配置模型后再试。其他分析请使用 SQL 工作台。",
            )
        yield "step", {"text": f"问题需要进一步解读（{local.reason}），由模型 {self.llm.status()['model']} 映射到语义模型 {model.name}。"}
        try:
            examples = [item for item in self.memory.examples(record["id"], question, "semantic") if item["definition"].get("fingerprint") == model.fingerprint()]
            if examples:
                yield "step", {"text": f"参考 {len(examples)} 条人工确认过的相似问答。"}
            hidden = {f"{dataset.name}.{item.name}" for dataset in model.datasets.values() for item in dataset.fields.values() if not self._field_in_scope(record, model, f"{dataset.name}.{item.name}")}
            plan = plan_with_model(self.generator, question, model, values, hidden=hidden, examples=examples, glossary=self._annotate(record, "").strip())
        except LlmError as error:
            raise QueryError(
                502,
                f"模型不可用（{error}），而规则无法解读这个问题：{local.reason}语义模型 {model.name} 现有指标：{supported}。",
            ) from error
        if plan.status == "ok":
            yield from self._run_plan(record, model, plan, question, refresh)
            return True
        if plan.status == "unmapped":
            raise QueryError(422, f"无法按已定义的口径回答：{plan.reason} 语义模型 {model.name} 现有指标：{supported}。没有猜测口径，请先在语义模型中定义它。")
        yield "step", {"text": f"这个问题需要语义层之外的计算（{plan.reason}），将生成 SQL 并按语义模型的指标定义校验。"}
        return catalog_text(model, values) + "\n" + "\n".join(
            f"- {item['name']} = {item['expression']}" for item in self.semantic.definitions(model, list(model.metrics))
        ) + "\n关系（多的一侧 → 一的一侧）：\n" + "\n".join(
            f"- {r.from_dataset}({', '.join(r.from_columns)}) → {r.to_dataset}({', '.join(r.to_columns)})" for r in model.relationships
        )

    def _dataset_tables(self, model: Any, dataset_name: str) -> list[str]:
        return source_tables(model.datasets[dataset_name].source)

    def _model_in_scope(self, record: dict[str, Any], model: Any) -> bool:
        return all(self.scope.table_allowed(record["id"], table) for name in model.datasets for table in self._dataset_tables(model, name))

    def _field_in_scope(self, record: dict[str, Any], model: Any, target: str) -> bool:
        dataset_name, _, column = target.partition(".")
        if dataset_name not in model.datasets:
            return True
        return all(self.scope.column_allowed(record["id"], table, column) for table in self._dataset_tables(model, dataset_name))

    def _check_plan_scope(self, record: dict[str, Any], model: Any, request: dict[str, Any]) -> None:
        targets = [str(item.get("field") if isinstance(item, dict) else item) for key in ("dimensions", "filters") for item in request.get(key) or []]
        for name in request.get("metrics") or []:
            metric = model.metrics.get(name)
            if metric is None:
                continue
            for text in metric.expressions.values():
                try:
                    targets += [f"{column.table}.{column.name}" for column in sqlglot.parse_one(text, read="duckdb").find_all(exp.Column) if column.table]
                except sqlglot.errors.SqlglotError:
                    continue
        for target in targets:
            if not self._field_in_scope(record, model, target):
                raise QueryError(403, f"字段 {target} 不在智能问数允许访问的范围内。")

    def _run_plan(self, record: dict[str, Any], model: Any, plan: Plan, question: str, refresh: bool) -> Iterator[tuple[str, Any]]:
        self._check_plan_scope(record, model, plan.request)
        for step in plan.steps:
            yield "step", {"text": step}
        try:
            compiled = self.semantic.compile(model.id, plan.request, record["id"])
            yield "sql", {"sql": compiled["sql"], "title": plan.title}
            yield "result", self.semantic.query(
                model.id, plan.request, datasource_id=record["id"], refresh=refresh,
                title=plan.title, steps=plan.steps, planner=plan.planner, question=question,
            )
        except SemanticQueryError as error:
            raise QueryError(error.status_code, str(error)) from error

    # ----- free SQL: what the model sees, and what it is held to --------------------------
    def _index_schema(self, record: dict[str, Any]) -> tuple[list[TableInfo], list[JoinEdge], bool]:
        """Every readable table of the source and how they join, kept for a few minutes."""
        cached = self._schema_index.get(record["id"])
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2], cached[3]
        tables: list[TableInfo] = []
        declared: list[JoinEdge] = []
        complete = True
        if record["id"] == LOCAL_SAMPLE:
            tables = [TableInfo(None, item["name"], item.get("label", ""), item.get("rows"), item["columns"]) for item in self.store.metadata()]
        else:
            connector = self.registry.connector(record["id"])
            default = connector.default_schema()
            schemas = [default] if default else [item["name"] for item in connector.schemas()[:3]]
            deadline = time.monotonic() + SCHEMA_INDEX_BUDGET_SECONDS
            for schema in schemas:
                listed = connector.tables(schema)
                complete = complete and len(listed) <= MAX_INDEXED_TABLES
                for entry in listed[:MAX_INDEXED_TABLES]:
                    columns: list[dict[str, Any]] = []
                    rows = entry.get("rows")
                    if time.monotonic() < deadline:
                        try:
                            detail = connector.table(schema, entry["name"])
                            columns, rows = detail["columns"], detail.get("rows", rows)
                        except ConnectorError:
                            complete = False
                    else:
                        complete = False
                    tables.append(TableInfo(schema, entry["name"], str(entry.get("comment") or ""), rows, columns))
                try:
                    declared += edges_from_foreign_keys(connector.foreign_keys(schema))
                except Exception:  # noqa: BLE001 - keys are a help; engines without them still answer
                    pass
        tables = [table for table in tables if self.scope.table_allowed(record["id"], table.name)]
        for table in tables:
            table.columns = [column for column in table.columns if self.scope.column_allowed(record["id"], table.name, column["name"])]
        edges = [*self._model_edges(record), *declared]
        edges += self._inferred_edges(record, tables, edges)
        self._schema_index[record["id"]] = (time.monotonic() + SCHEMA_INDEX_SECONDS, tables, edges, complete)
        return tables, edges, complete

    def _model_edges(self, record: dict[str, Any]) -> list[JoinEdge]:
        if self.semantic is None:
            return []
        edges = []
        try:
            models = self.semantic.models_for(record)
        except Exception:  # noqa: BLE001 - other sources of joins remain
            return []
        for model in models:
            for relationship in model.relationships:
                many, one = source_tables(model.datasets[relationship.from_dataset].source), source_tables(model.datasets[relationship.to_dataset].source)
                if len(many) == 1 and len(one) == 1:
                    edges.append(JoinEdge(many[0], relationship.from_columns, one[0], relationship.to_columns, SEMANTIC))
        return edges

    def _inferred_edges(self, record: dict[str, Any], tables: list[TableInfo], known: list[JoinEdge]) -> list[JoinEdge]:
        """Keep a name match only if the data agrees: a unique target holding the sampled keys."""
        dialect = CONNECTORS[record["type"]].dialect
        edges: list[JoinEdge] = []
        for many, many_column, one, one_column in join_candidates(tables, known)[:MAX_JOIN_PROBES]:
            try:
                if self.execute(record, duplicate_key_probe_sql(one.schema, one.name, [one_column], dialect), 1)["rows"]:
                    continue
                column = exp.column(exp.to_identifier(many_column, quoted=True))
                source = exp.Table(this=exp.to_identifier(many.name, quoted=True), db=exp.to_identifier(many.schema, quoted=True) if many.schema else None)
                sample = exp.Select(expressions=[column]).from_(source).where(exp.Not(this=exp.Is(this=column.copy(), expression=exp.Null()))).group_by(column.copy()).limit(JOIN_SAMPLE)
                keys = [row[0] for row in self.execute(record, sample.sql(dialect=dialect), JOIN_SAMPLE)["rows"]]
                if not keys:
                    continue
                literals = [exp.Literal.number(key) if isinstance(key, (int, float)) and not isinstance(key, bool) else exp.Literal.string(str(key)) for key in keys]
                target_column = exp.column(exp.to_identifier(one_column, quoted=True))
                target = exp.Table(this=exp.to_identifier(one.name, quoted=True), db=exp.to_identifier(one.schema, quoted=True) if one.schema else None)
                check = exp.Select(expressions=[exp.alias_(exp.Count(this=exp.Star()), "lattice_hits")]).from_(target).where(exp.In(this=target_column, expressions=literals))
                hits = self.execute(record, check.sql(dialect=dialect), 1)["rows"][0][0]
                if float(hits or 0) / len(keys) >= JOIN_MATCH_RATIO:
                    edges.append(JoinEdge(many.name, [many_column], one.name, [one_column], INFERRED))
            except (QueryError, IndexError, TypeError, ValueError):
                continue
        return edges

    def _linked_schema(self, record: dict[str, Any], question: str) -> SchemaLink | None:
        """The part of the schema this question is about; None falls back to the plain listing."""
        try:
            tables, edges, _ = self._index_schema(record)
        except (DataSourceError, ConnectorError):
            return None
        tables = [table for table in tables if table.columns]
        if not tables:
            return None
        annotation = self._annotate(record, "")
        if annotation:
            for table in tables:
                table.described = "\n".join(line for line in annotation.splitlines() if table.name in line)
        return link_schema(question, tables, edges, picked=self._pick_tables(question, tables))

    def _pick_tables(self, question: str, tables: list[TableInfo]) -> list[str]:
        """Let the model name the tables a question needs, when there are too many to show."""
        if len(tables) <= TOP_K or not self.llm.configured():
            return []
        prompt = f"数据库中的表：\n{table_menu(tables)}\n\n用户问题：{question[:2000]}"
        try:
            picked = self.generator.complete_json(prompt, system=PICK_TABLES_SYSTEM, schema=PICK_TABLES_SCHEMA).get("tables")
        except LlmError:
            return []  # Word matching and the full listing still work without it.
        return [str(name) for name in picked or []][:TOP_K] if isinstance(picked, list) else []

    def _enum_values(self, record: dict[str, Any], link: SchemaLink) -> dict[str, list[str]]:
        """Real values of text columns with few of them, so 华东 finds the column that holds it."""
        dialect = CONNECTORS[record["type"]].dialect
        values: dict[str, list[str]] = {}
        checked = 0
        for table in link.tables:
            target = exp.Table(this=exp.to_identifier(table.name, quoted=True), db=exp.to_identifier(table.schema, quoted=True) if table.schema else None)
            for column in table.columns:
                name = column["name"]
                if checked >= MAX_ENUM_COLUMNS:
                    return values
                if not TEXT_TYPE.search(str(column.get("type") or "")) or KEY_COLUMN.search(name) or SENSITIVE_COLUMN.search(name):
                    continue
                checked += 1
                identifier = exp.column(exp.to_identifier(name, quoted=True))
                statement = exp.Select(expressions=[identifier]).from_(target).group_by(identifier.copy()).limit(MAX_ENUM_VALUES + 1)
                try:
                    rows = self.execute(record, statement.sql(dialect=dialect), MAX_ENUM_VALUES + 1)["rows"]
                except QueryError:
                    continue
                options = sorted(str(row[0]) for row in rows if row[0] not in (None, ""))
                if options and len(options) <= MAX_ENUM_VALUES:
                    values[f"{table.name}.{name}"] = options
        return values

    def _audit(self, record: dict[str, Any], sql: str, dialect: str) -> AuditReport:
        def probe(schema: str | None, table: str, columns: list[str]) -> bool | None:
            try:
                statement = duplicate_key_probe_sql(schema, table, columns, dialect)
                return not self.execute(record, statement, 1)["rows"]
            except QueryError:
                return None

        def value_probe(schema: str | None, table: str, column: str, value: str, like: bool) -> bool | None:
            try:
                return bool(self.execute(record, value_probe_sql(schema, table, column, value, like, dialect), 1)["rows"])
            except QueryError:
                return None

        known: dict[str, set[str]] | None = None
        edges: list[JoinEdge] = []
        try:
            tables, edges, complete = self._index_schema(record)
            if complete and all(table.columns for table in tables):
                known = {table.name.lower(): {column["name"].lower() for column in table.columns} for table in tables}
        except (DataSourceError, ConnectorError):
            pass
        source = record["id"]
        return audit_sql(
            sql, dialect, known, probe, edges=edges, value_probe=value_probe,
            table_allowed=lambda table: self.scope.table_allowed(source, table),
            column_allowed=lambda table, column: self.scope.column_allowed(source, table, column),
            hides_columns=lambda table: self.scope.hides_columns_of(source, table),
        )

    def _generate_checked(
        self, record: dict[str, Any], question: str, dialect: str, dialect_label: str, label: str, schema_text: str, refresh: bool
    ) -> Iterator[tuple[str, Any]]:
        """Generate SQL, showing the model what was wrong until it holds up or the tries run out.

        What is wrong comes from two places: the audit before running, which catches statements
        that would run and mislead, and the engine, which catches the rest. A filter value the
        data lacks gets one correction and then only a warning: it may just not have occurred.
        """
        prompt = question
        value_feedback_given = False
        #: Conditions the question asked for; a correction may change a value, not drop the condition.
        required_filters: set[str] = set()
        reported_values: list[str] = []
        for attempt in range(MAX_CORRECTIONS + 1):
            plan = self.generator.generate(prompt, dialect_label, label, schema_text)
            audit = self._audit(record, plan["sql"], dialect)
            problems = list(audit.errors)
            dropped = sorted(required_filters - audit.filtered_columns)
            if dropped:
                # "Fixing" a filter the data cannot satisfy by removing it answers another question.
                problems.append(f"修正后的 SQL 不再按 {'、'.join(dropped)} 筛选，答的已经不是原来的问题。" + "；".join(reported_values)[:300])
            if audit.value_issues and not value_feedback_given:
                problems += audit.value_issues
                required_filters |= audit.value_columns
                reported_values = list(audit.value_issues)
                value_feedback_given = True
            if problems:
                if attempt == MAX_CORRECTIONS:
                    raise QueryError(422, "模型生成的 SQL 未通过校验，已拒绝执行：" + "；".join(problems)[:600])
                yield "step", {"text": "SQL 未通过校验，要求模型修正：" + "；".join(problems)[:300]}
                prompt = self._correction(question, plan["sql"], problems)
                continue
            yield "sql", {"sql": plan["sql"], "title": plan["title"]}
            try:
                result, repaired = self.run_generated(record, plan["sql"], dialect, dialect_label, refresh)
            except QueryError as error:
                if attempt == MAX_CORRECTIONS:
                    raise
                yield "step", {"text": "SQL 执行出错，把错误信息交给模型修正：" + str(error)[:200]}
                prompt = self._correction(question, plan["sql"], ["执行错误：" + str(error)[:500]])
                continue
            # The reader needs the finding, not the instructions that were meant for the model.
            audit.warnings = [*audit.warnings, *(issue.split("。")[0] + "，结果可能为空。" for issue in audit.value_issues)]
            if repaired:
                yield "sql", {"sql": repaired, "title": plan["title"]}
            return plan, result, repaired, audit
        raise QueryError(500, "模型没有生成可用的 SQL。")

    @staticmethod
    def _correction(question: str, sql: str, problems: list[str]) -> str:
        return f"{question}\n\n你上一次生成的 SQL：\n{sql}\n存在以下问题，请修正：\n- " + "\n- ".join(problems)

    def _run_confirmed_sql(
        self, record: dict[str, Any], pinned: dict[str, Any], question: str, dialect: str, dialect_label: str, refresh: bool, started: float
    ) -> Iterator[tuple[str, Any]]:
        """Run the statement a person confirmed for this question, if it still holds up."""
        sql = pinned["definition"]["sql"]
        audit = self._audit(record, sql, dialect)
        if audit.errors:
            yield "step", {"text": "这个问题确认过的 SQL 已不再通过校验，重新生成：" + "；".join(audit.errors)[:200]}
            return False
        yield "step", {"text": "这个问题的 SQL 已由人工确认，直接使用。"}
        yield "sql", {"sql": sql, "title": pinned["title"]}
        try:
            result = self.execute(record, sql, refresh=refresh)
        except QueryError as error:
            yield "step", {"text": "这个问题确认过的 SQL 执行失败，重新生成：" + str(error)[:200]}
            return False
        verification = self._verification_of(audit)
        verification["checks"].insert(0, {"name": "人工确认", "status": "passed", "detail": f"{pinned['created_at'][:10]} 确认过这条 SQL。"})
        steps = ["这个问题的 SQL 已由人工确认，直接使用。", f"执行只读 SQL，返回 {len(result['rows'])} 行。"]
        yield "result", self.payload(record, pinned["title"], result, "llm", steps, None, started, extra={"verification": verification, "question": question})
        return True

    def confirm(self, result_id: str, created_by: str = "") -> dict[str, Any]:
        payload = self.store.find(result_id)
        if payload is None:
            raise QueryError(404, "没有找到这条结果。")
        try:
            return self.memory.confirm(payload, created_by)
        except ValueError as error:
            raise QueryError(400, str(error)) from error

    def invalidate_schema(self, source_id: str | None = None) -> None:
        if source_id:
            self._schema_index.pop(source_id, None)
        else:
            self._schema_index.clear()

    @staticmethod
    def _verification_of(audit: AuditReport) -> dict[str, Any]:
        checks = [{"name": "SQL 静态与关联键校验", "status": "passed", "detail": text} for text in audit.passed]
        checks += [{"name": "未能完成的校验", "status": "skipped", "detail": text} for text in audit.warnings]
        return {"governed": False, "status": "skipped" if audit.warnings else "passed", "checks": checks}

    def run_generated(
        self, record: dict[str, Any], sql: str, dialect: str, dialect_label: str, refresh: bool = False
    ) -> tuple[dict[str, Any], str]:
        """Execute model SQL, repairing its dialect once if the engine rejects it.

        A model is told which dialect to target but is not bound by it, and one
        wrong function name is enough for an engine to refuse the whole query.
        The six sample tables are identical everywhere, so a rejected statement
        is retried once after translating it from DuckDB, which is the dialect
        the schema context and the built-in examples are written in. The retry
        only runs after a genuine failure, and the translated SQL is shown to
        the user rather than silently substituted.
        """
        try:
            return self.execute(record, sql, refresh=refresh), ""
        except QueryError as first:
            if dialect == "duckdb":
                raise
            try:
                repaired = sqlglot.transpile(sql, read="duckdb", write=dialect)
            except sqlglot.errors.SqlglotError:
                raise first from None
            if len(repaired) != 1 or repaired[0].strip() == sql.strip():
                raise first from None
            try:
                return self.execute(record, repaired[0], refresh=refresh), repaired[0]
            except QueryError:
                raise first from None

    def require_sample_tables(self, record: dict[str, Any], sql: str, label: str) -> None:
        """Refuse a built-in rule on a source that does not hold the sample tables.

        The rules are written against the six demo tables every local engine is
        seeded with. A source without them would otherwise answer with the
        engine's own "relation does not exist" text, which reads as a fault in
        the platform rather than as a question this source cannot answer.
        """
        if record["id"] == LOCAL_SAMPLE:
            return
        needed = sorted({name for name in SAMPLE_TABLES if name in sql})
        if not needed:
            return
        try:
            context = self.registry.schema_context(record["id"])
        except (DataSourceError, ConnectorError):
            return  # A source that cannot be inspected fails later with its own message.
        missing = [name for name in needed if name not in context]
        if missing:
            joined = "、".join(missing)
            raise QueryError(
                400,
                f"{label}中没有内置规则所需的示例表（{joined}）。"
                "请配置模型后按该数据源的真实表结构提问，或改用 SQL 工作台。",
            )

    @staticmethod
    def localize(sql: str, dialect: str, dialect_label: str) -> str:
        """Rewrite a built-in rule query for the selected engine.

        The rules are written once in DuckDB SQL over the six sample tables that
        every local engine holds, so a question answers on any of them without a
        model. Translation is sqlglot's, and a dialect it cannot express is
        reported rather than sent to the engine as invalid SQL.
        """
        if dialect == "duckdb":
            return sql
        try:
            statements = sqlglot.transpile(sql, read="duckdb", write=dialect)
        except sqlglot.errors.SqlglotError as error:
            raise QueryError(
                400, f"本地规则无法转换为 {dialect_label}：{str(error)[:200]}。请配置模型或改用 SQL 工作台。"
            ) from error
        if len(statements) != 1:
            raise QueryError(400, "本地规则生成了多条语句，已拒绝执行。")
        return statements[0]

    def _schema_context(self, record: dict[str, Any]) -> str:
        if record["id"] == LOCAL_SAMPLE:
            text = "\n".join(
                f"- {table['name']}（{table['label']}，{table['rows']} 行）："
                + ", ".join(f"{column['name']} {column['type']}" for column in table["columns"])
                for table in self.store.metadata()
            )
        else:
            text = self.registry.schema_context(record["id"])
        return self._annotate(record, text)

    def _annotate(self, record: dict[str, Any], text: str) -> str:
        """Append the catalog's business semantics; a failing annotator changes nothing."""
        if self.annotator is None:
            return text
        try:
            semantics = self.annotator(record)
        except Exception:  # noqa: BLE001 - the model still gets the plain schema
            return text
        return f"{text}\n\n{semantics}" if semantics else text

    def complete(
        self, question: str | None, sql: str | None, source_id: str | None, refresh: bool = False
    ) -> dict[str, Any]:
        result = None
        for name, data in self.answer(question, sql, source_id, refresh):
            if name == "result":
                result = data
        if result is None:
            raise QueryError(500, "查询没有返回结果。")
        return result
