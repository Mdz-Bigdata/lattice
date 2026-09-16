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

from .connectors import CONNECTORS, DIALECT_LABELS, ConnectorError, chart_hint, guard_sql, referenced_tables
from .datasources import DataSourceError, DataSourceRegistry
from .llm import LlmError, LlmSettings, SqlGenerator
from .query import QueryStore
from .observability import observe
from .query_cache import QueryCache

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
        if self.llm.configured():
            status = self.llm.status()
            yield "step", {"text": f"读取表结构，交给模型 {status['model']} 生成 SQL。"}
            try:
                schema_text = self._schema_context(record)
                plan = self.generator.generate(question, dialect_label, label, schema_text)
            except LlmError as error:
                if record["id"] != LOCAL_SAMPLE:
                    raise QueryError(502, f"模型生成 SQL 失败：{error}") from error
                yield "step", {"text": f"模型不可用（{error}），改用本地规则回答示例问题。"}
            except (DataSourceError, ConnectorError) as error:
                raise QueryError(400, f"读取表结构失败：{error}") from error
            else:
                for step in plan["steps"]:
                    yield "step", {"text": step}
                yield "sql", {"sql": plan["sql"], "title": plan["title"]}
                result, repaired = self.run_generated(record, plan["sql"], dialect, dialect_label, refresh)
                steps = list(plan["steps"])
                if repaired:
                    steps.append(f"模型返回的 SQL 不能在该引擎直接执行，已按 {dialect_label} 方言转换后重试。")
                    yield "step", {"text": steps[-1]}
                    yield "sql", {"sql": repaired, "title": plan["title"]}
                steps.append(f"执行只读 SQL，返回 {len(result['rows'])} 行" + ("（已截断）。" if result.get("truncated") else "。"))
                chart = {"dimension": plan["dimension"], "metric": plan["metric"], "type": plan["chart_type"]}
                yield "result", self.payload(record, plan["title"], result, "llm", steps, chart, started)
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
