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

"""The semantic-layer query engine and the metric registry (语义层查询引擎 / 指标平台).

An Apache Ossie semantic model already says everything a query needs: which
physical tables its datasets stand for, how datasets join (relationships), what
each field means and which ones are dimensions or time, and how each metric is
computed. This module turns a request such as "total_sales by month and region"
into one SQL statement for the engine that holds the data, runs it through the
same read-only path every other query takes (guard, cache, history, usage), and
hands back a result the existing report component can draw.

Metrics are never defined anywhere else: the registry is read from the models
themselves (the built-in demo model, the Lattice model store and the native
Polaris interface), so the semantic model stays the single source of truth.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import sqlglot
import yaml
from sqlglot import exp

from .connectors import CONNECTORS, DIALECT_LABELS
from .observability import observe

LOCAL_SAMPLE = "local-sample"
#: Which Ossie dialect expression to prefer for a sqlglot dialect, in order.
OSSIE_DIALECTS: dict[str, tuple[str, ...]] = {
    "snowflake": ("SNOWFLAKE", "ANSI_SQL"),
    "bigquery": ("BIGQUERY", "ANSI_SQL"),
    "databricks": ("DATABRICKS", "ANSI_SQL"),
}
GRAINS = ("day", "week", "month", "quarter", "year")
GRAIN_LABELS = {"day": "按日", "week": "按周", "month": "按月", "quarter": "按季", "year": "按年"}
OPERATORS = ("=", "!=", ">", ">=", "<", "<=", "in", "not_in", "like", "between", "is_null", "not_null")
NUMERIC_TYPES = {"Integer", "Decimal", "Float"}
TIME_TYPES = {"Date", "DateTime", "DateTimeTz", "Time"}
MAX_METRICS = 20
MAX_DIMENSIONS = 10
MAX_FILTERS = 20
MAX_JOINS = 8
CACHE_SECONDS = 60
SEMANTIC_STORE_CATALOG = "lattice"
SEMANTIC_STORE_NAMESPACE = ["demo"]
#: Schema names that models use to mean "the engine's default": DuckDB's ``main``, the
#: PostgreSQL / Ossie-example ``public`` and ClickHouse's ``default``. On a data source
#: whose default schema is one of them the name is left alone; elsewhere it is rewritten to
#: the data source's own default (``lattice_demo`` on the local StarRocks and Doris).
DEFAULT_SCHEMA_ALIASES = ("main", "public", "default")
#: The alias that is a real schema on a data source type and must therefore never be
#: rewritten. Keyed by connector type, not dialect: Iceberg and Paimon are read through
#: DuckDB but their namespaces have no ``main``.
NATIVE_DEFAULT_SCHEMAS = {"duckdb": "main", "postgresql": "public", "clickhouse": "default"}


class SemanticQueryError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _bad(message: str) -> SemanticQueryError:
    return SemanticQueryError(400, message)


# ----- the model ------------------------------------------------------------------------
@dataclass
class Field:
    name: str
    expressions: dict[str, str]
    datatype: str = ""
    is_time: bool = False
    is_dimension: bool = False
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    name: str
    source: str
    primary_key: list[str]
    fields: dict[str, Field]
    description: str = ""


@dataclass
class Relationship:
    name: str
    from_dataset: str
    to_dataset: str
    from_columns: list[str]
    to_columns: list[str]


@dataclass
class Metric:
    name: str
    expressions: dict[str, str]
    datatype: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Model:
    id: str
    name: str
    storage: str
    description: str
    datasets: dict[str, Dataset]
    relationships: list[Relationship]
    metrics: dict[str, Metric]

    def dimensions(self) -> list[dict[str, Any]]:
        items = []
        for dataset in self.datasets.values():
            for item in dataset.fields.values():
                if item.is_dimension:
                    items.append({"field": f"{dataset.name}.{item.name}", "dataset": dataset.name, "name": item.name, "datatype": item.datatype, "is_time": item.is_time, "description": item.description})
        return items


def _expressions(raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        return {"ANSI_SQL": raw.strip()}
    if isinstance(raw, dict):
        dialects = raw.get("dialects")
        if isinstance(dialects, list):
            found = {}
            for item in dialects:
                if isinstance(item, dict) and item.get("expression"):
                    found[str(item.get("dialect") or "ANSI_SQL").upper()] = str(item["expression"]).strip()
            return found
        if raw.get("expression"):
            return {"ANSI_SQL": str(raw["expression"]).strip()}
    return {}


def _synonyms(node: Any) -> list[str]:
    context = node.get("ai_context") if isinstance(node, dict) else None
    if isinstance(context, dict) and isinstance(context.get("synonyms"), list):
        return [str(item) for item in context["synonyms"] if item][:20]
    return []


def extract_document(loaded: Any) -> dict[str, Any] | None:
    """The semantic-model object from a model-store record, a native body or a YAML text."""
    if isinstance(loaded, str):
        try:
            data = yaml.safe_load(loaded)
        except yaml.YAMLError:
            return None
        return extract_document(data)
    if not isinstance(loaded, dict):
        return None
    parsed: Any = None
    document = loaded.get("document")
    if isinstance(document, dict):
        model = document.get("semantic_model")
        if isinstance(model, str):
            try:
                parsed = json.loads(model)
            except ValueError:
                parsed = None
        elif isinstance(model, (list, dict)):
            parsed = model
        elif isinstance(document.get("datasets"), list) or document.get("name"):
            parsed = document
    if parsed is None and isinstance(loaded.get("yaml"), str) and loaded["yaml"].strip():
        return extract_document(loaded["yaml"])
    if parsed is None and ("semantic_model" in loaded or isinstance(loaded.get("datasets"), list)):
        parsed = loaded.get("semantic_model", loaded)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else None
    return parsed if isinstance(parsed, dict) else None


def parse_model(document: dict[str, Any], *, model_id: str = "", storage: str = "") -> Model:
    """Read an Ossie semantic model into the structures the engine works with."""
    if not isinstance(document, dict) or not document.get("name"):
        raise _bad("语义模型缺少 name。")
    datasets: dict[str, Dataset] = {}
    for raw in document.get("datasets") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        fields: dict[str, Field] = {}
        for item in raw.get("fields") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            datatype = str(item.get("datatype") or "")
            dimension = item.get("dimension")
            is_time = bool(dimension.get("is_time")) if isinstance(dimension, dict) else False
            if not is_time and datatype in TIME_TYPES:
                is_time = True
            is_dimension = isinstance(dimension, dict) or datatype not in NUMERIC_TYPES or is_time
            fields[str(item["name"])] = Field(
                name=str(item["name"]),
                expressions=_expressions(item.get("expression")) or {"ANSI_SQL": str(item["name"])},
                datatype=datatype,
                is_time=is_time,
                is_dimension=is_dimension,
                description=str(item.get("description") or ""),
                synonyms=_synonyms(item),
            )
        datasets[str(raw["name"])] = Dataset(
            name=str(raw["name"]),
            source=str(raw.get("source") or ""),
            primary_key=[str(key) for key in raw.get("primary_key") or []],
            fields=fields,
            description=str(raw.get("description") or ""),
        )
    if not datasets:
        raise _bad("语义模型没有数据集。")
    relationships = []
    for raw in document.get("relationships") or []:
        if not isinstance(raw, dict):
            continue
        from_dataset, to_dataset = str(raw.get("from") or ""), str(raw.get("to") or "")
        from_columns = [str(item) for item in raw.get("from_columns") or []]
        to_columns = [str(item) for item in raw.get("to_columns") or []]
        if from_dataset in datasets and to_dataset in datasets and from_columns and len(from_columns) == len(to_columns):
            relationships.append(Relationship(str(raw.get("name") or f"{from_dataset}_{to_dataset}"), from_dataset, to_dataset, from_columns, to_columns))
    metrics: dict[str, Metric] = {}
    for raw in document.get("metrics") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        expressions = _expressions(raw.get("expression"))
        if not expressions:
            continue
        metrics[str(raw["name"])] = Metric(str(raw["name"]), expressions, str(raw.get("datatype") or ""), str(raw.get("description") or ""), _synonyms(raw))
    name = str(document["name"])
    return Model(id=model_id or name, name=name, storage=storage or "document", description=str(document.get("description") or ""), datasets=datasets, relationships=relationships, metrics=metrics)


def expression_for(expressions: dict[str, str], dialect: str) -> str:
    for candidate in OSSIE_DIALECTS.get(dialect, ()) + ("ANSI_SQL",):
        if expressions.get(candidate):
            return expressions[candidate]
    return next(iter(expressions.values()), "")


# ----- compilation ----------------------------------------------------------------------
@dataclass
class Compiled:
    sql: str
    dialect: str
    datasets: list[str]
    joins: list[str]
    columns: list[dict[str, Any]]
    chart: dict[str, str]

    def view(self) -> dict[str, Any]:
        return {"sql": self.sql, "dialect": self.dialect, "dialect_label": DIALECT_LABELS.get(self.dialect, self.dialect), "datasets": self.datasets, "joins": self.joins, "columns": self.columns, "chart": self.chart}


def _literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    if isinstance(value, (list, tuple)):
        return exp.Tuple(expressions=[_literal(item) for item in value])
    return exp.Literal.string(str(value))


class Compiler:
    """Turns one request against one model into a SELECT for one dialect."""

    def __init__(self, model: Model, dialect: str, *, bare_schemas: tuple[str, ...] = (), schema_map: dict[str, str] | None = None):
        self.model = model
        self.dialect = dialect
        self.bare_schemas = tuple(name.lower() for name in bare_schemas)
        #: Model schemas that mean "the engine's default" on this data source (DuckDB's ``main``).
        self.schema_map = {key.lower(): value for key, value in (schema_map or {}).items()}
        self.used: list[str] = []

    # ----- references -------------------------------------------------------------------
    def _alias(self, dataset: str) -> exp.Identifier:
        return exp.to_identifier(dataset, quoted=True)

    def _use(self, dataset: str) -> None:
        if dataset not in self.model.datasets:
            raise _bad(f"语义模型中没有数据集 {dataset}。")
        if dataset not in self.used:
            self.used.append(dataset)

    def _find_field(self, reference: str) -> tuple[Dataset, Field]:
        text = str(reference or "").strip()
        if "." in text:
            dataset_name, field_name = text.rsplit(".", 1)
            dataset = self.model.datasets.get(dataset_name)
            if dataset is None:
                raise _bad(f"语义模型中没有数据集 {dataset_name}。")
            item = dataset.fields.get(field_name)
            if item is None:
                raise _bad(f"数据集 {dataset_name} 中没有字段 {field_name}。")
            return dataset, item
        matches = [(dataset, dataset.fields[text]) for dataset in self.model.datasets.values() if text in dataset.fields]
        if not matches:
            raise _bad(f"语义模型中没有字段 {text}；请使用 数据集.字段 的写法。")
        if len(matches) > 1:
            names = "、".join(f"{dataset.name}.{text}" for dataset, _ in matches)
            raise _bad(f"字段 {text} 在多个数据集中出现（{names}），请写明数据集。")
        return matches[0]

    def _field_expression(self, dataset: Dataset, item: Field) -> exp.Expression:
        text = expression_for(item.expressions, self.dialect)
        try:
            tree = sqlglot.parse_one(text, read="duckdb")
        except sqlglot.errors.SqlglotError as error:
            raise _bad(f"字段 {dataset.name}.{item.name} 的表达式无法解析：{str(error)[:120]}") from error
        self._use(dataset.name)

        def qualify(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            table = node.table
            owner = dataset.name if not table or table == dataset.name else table
            if owner not in self.model.datasets:
                owner = dataset.name
            self._use(owner)
            return exp.Column(this=exp.to_identifier(node.name, quoted=True), table=self._alias(owner))

        # transform() also replaces a bare-column root, which replace() on the root cannot.
        return tree.transform(qualify)

    def _metric_expression(self, metric: Metric) -> exp.Expression:
        text = expression_for(metric.expressions, self.dialect)
        try:
            tree = sqlglot.parse_one(text, read="duckdb")
        except sqlglot.errors.SqlglotError as error:
            raise _bad(f"指标 {metric.name} 的表达式无法解析：{str(error)[:120]}") from error
        # A bare column that is not a declared field is read as a physical column of the
        # dataset the metric measures, when that dataset is unambiguous: the model's only
        # dataset, or the only dataset the expression names. Native models written by hand
        # (``SUM(ss_ext_sales_price)`` over one dataset without fields) then still compile.
        named = {column.table for column in tree.find_all(exp.Column) if column.table and column.table in self.model.datasets}
        implied = next(iter(self.model.datasets)) if len(self.model.datasets) == 1 else (next(iter(named)) if len(named) == 1 else "")

        def raw_column(dataset: str, name: str) -> exp.Expression:
            self._use(dataset)
            return exp.Column(this=exp.to_identifier(name, quoted=True), table=self._alias(dataset))

        def resolve(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            name, table = node.name, node.table
            if table and table in self.model.datasets:
                dataset = self.model.datasets[table]
                item = dataset.fields.get(name)
                if item is not None:
                    return self._field_expression(dataset, item)
                return raw_column(table, name)
            if not table:
                matches = [dataset for dataset in self.model.datasets.values() if name in dataset.fields]
                if len(matches) == 1:
                    return self._field_expression(matches[0], matches[0].fields[name])
                if len(matches) > 1:
                    names = "、".join(f"{dataset.name}.{name}" for dataset in matches)
                    raise _bad(f"指标 {metric.name} 引用的字段 {name} 在多个数据集中出现（{names}），请写明数据集。")
                if implied:
                    return raw_column(implied, name)
                raise _bad(f"指标 {metric.name} 引用了字段 {name}，但语义模型中没有这个字段；请使用 数据集.字段 的写法。")
            raise _bad(f"指标 {metric.name} 引用了未知的数据集 {table}。")

        return tree.transform(resolve)

    # ----- the statement ------------------------------------------------------------------
    def _table(self, dataset: Dataset) -> exp.Expression:
        source = dataset.source.strip()
        if not source:
            raise _bad(f"数据集 {dataset.name} 没有 source。")
        if re.match(r"^\s*(select|with)\b", source, re.IGNORECASE):
            try:
                subquery = sqlglot.parse_one(source, read="duckdb")
            except sqlglot.errors.SqlglotError as error:
                raise _bad(f"数据集 {dataset.name} 的查询无法解析：{str(error)[:120]}") from error
            return exp.Subquery(this=subquery, alias=exp.TableAlias(this=self._alias(dataset.name)))
        parts = [part for part in re.split(r"\.", source) if part]
        table_name = parts[-1]
        schema = parts[-2] if len(parts) >= 2 else None
        if schema and schema.lower() in self.schema_map:
            schema = self.schema_map[schema.lower()] or None
        if schema and schema.lower() in self.bare_schemas:
            schema = None
        table = exp.Table(this=exp.to_identifier(table_name, quoted=True), db=exp.to_identifier(schema, quoted=True) if schema else None)
        return exp.alias_(table, self._alias(dataset.name), table=True)

    def _joins(self, root: str) -> list[tuple[Relationship, str, str]]:
        """Relationships that connect every used dataset to the root, breadth first."""
        needed = [name for name in self.used if name != root]
        if not needed:
            return []
        edges: dict[str, list[tuple[Relationship, str]]] = {}
        for relationship in self.model.relationships:
            edges.setdefault(relationship.from_dataset, []).append((relationship, relationship.to_dataset))
            edges.setdefault(relationship.to_dataset, []).append((relationship, relationship.from_dataset))
        reached = {root}
        order: list[tuple[Relationship, str, str]] = []
        frontier = [root]
        while frontier and not set(needed) <= reached:
            current = frontier.pop(0)
            for relationship, other in edges.get(current, []):
                if other in reached:
                    continue
                reached.add(other)
                order.append((relationship, current, other))
                frontier.append(other)
        missing = [name for name in needed if name not in reached]
        if missing:
            raise _bad(f"数据集 {root} 与 {'、'.join(missing)} 之间没有定义关系（relationships），无法关联查询。")
        # Only joins on the paths to the datasets in use are emitted.
        keep: set[str] = set(needed)
        parents = {other: current for _, current, other in order}
        for name in needed:
            cursor = name
            while cursor in parents:
                keep.add(cursor)
                cursor = parents[cursor]
        result = [(relationship, current, other) for relationship, current, other in order if other in keep]
        if len(result) > MAX_JOINS:
            raise _bad(f"一次查询最多关联 {MAX_JOINS} 个数据集。")
        return result

    def compile(self, request: dict[str, Any]) -> Compiled:
        metric_names = [str(name) for name in request.get("metrics") or []]
        if not metric_names:
            raise _bad("请至少选择一个指标。")
        if len(metric_names) > MAX_METRICS:
            raise _bad(f"一次最多查询 {MAX_METRICS} 个指标。")
        dimensions = [dict(item) if isinstance(item, dict) else {"field": str(item)} for item in request.get("dimensions") or []]
        if len(dimensions) > MAX_DIMENSIONS:
            raise _bad(f"一次最多按 {MAX_DIMENSIONS} 个维度分组。")
        filters = [dict(item) for item in request.get("filters") or [] if isinstance(item, dict)]
        if len(filters) > MAX_FILTERS:
            raise _bad(f"一次最多 {MAX_FILTERS} 个筛选条件。")

        selects: list[exp.Expression] = []
        columns: list[dict[str, Any]] = []
        group_by: list[exp.Expression] = []
        aliases: dict[str, exp.Expression] = {}
        seen_aliases: set[str] = set()
        time_alias = ""
        # Metrics are resolved first so the dataset they measure becomes the root of
        # the joins; dimensions and filters then hang off it with LEFT JOINs.
        metric_expressions: dict[str, exp.Expression] = {}
        for name in metric_names:
            metric = self.model.metrics.get(name)
            if metric is None:
                raise _bad(f"语义模型中没有指标 {name}。")
            metric_expressions[name] = self._metric_expression(metric)
        for item in dimensions:
            dataset, column = self._find_field(item.get("field"))
            expression = self._field_expression(dataset, column)
            grain = str(item.get("grain") or "").lower()
            if grain:
                if grain not in GRAINS:
                    raise _bad("时间粒度只能是 day、week、month、quarter 或 year。")
                if not column.is_time:
                    raise _bad(f"字段 {dataset.name}.{column.name} 不是时间维度，不能按 {GRAIN_LABELS[grain]}聚合。")
                expression = exp.DateTrunc(unit=exp.Literal.string(grain), this=expression)
            alias = str(item.get("alias") or column.name)
            if alias in seen_aliases:
                alias = f"{dataset.name}_{column.name}"
            seen_aliases.add(alias)
            aliases[alias] = expression
            group_by.append(expression.copy())
            selects.append(exp.alias_(expression, exp.to_identifier(alias, quoted=True)))
            columns.append({"name": alias, "kind": "dimension", "field": f"{dataset.name}.{column.name}", "grain": grain or None, "datatype": column.datatype})
            if column.is_time and not time_alias:
                time_alias = alias
        for name in metric_names:
            metric = self.model.metrics[name]
            expression = metric_expressions[name]
            alias = name if name not in seen_aliases else f"metric_{name}"
            seen_aliases.add(alias)
            aliases[alias] = expression
            selects.append(exp.alias_(expression, exp.to_identifier(alias, quoted=True)))
            columns.append({"name": alias, "kind": "metric", "metric": name, "datatype": metric.datatype})

        where: list[exp.Expression] = []
        having: list[exp.Expression] = []
        for item in filters:
            target = str(item.get("field") or "")
            operator = str(item.get("op") or "=").lower()
            if operator not in OPERATORS:
                raise _bad("筛选条件的比较方式只能是 " + "、".join(OPERATORS) + "。")
            if target in self.model.metrics:
                expression = self._metric_expression(self.model.metrics[target])
                bucket = having
            else:
                dataset, column = self._find_field(target)
                expression = self._field_expression(dataset, column)
                bucket = where
            bucket.append(self._condition(expression, operator, item.get("value")))

        root = self.used[0] if self.used else next(iter(self.model.datasets))
        joins = self._joins(root)
        select = exp.Select(expressions=selects).from_(self._table(self.model.datasets[root]))
        join_names = []
        for relationship, current, other in joins:
            if relationship.from_dataset == current:
                left, right = current, other
                left_columns, right_columns = relationship.from_columns, relationship.to_columns
            else:
                left, right = current, other
                left_columns, right_columns = relationship.to_columns, relationship.from_columns
            condition = exp.and_(*[
                exp.EQ(this=exp.Column(this=exp.to_identifier(a, quoted=True), table=self._alias(left)), expression=exp.Column(this=exp.to_identifier(b, quoted=True), table=self._alias(right)))
                for a, b in zip(left_columns, right_columns)
            ])
            select = select.join(self._table(self.model.datasets[other]), on=condition, join_type="LEFT")
            join_names.append(f"{relationship.name}: {current} → {other}")
        if where:
            select = select.where(exp.and_(*where))
        if group_by:
            select = select.group_by(*group_by)
        if having:
            select = select.having(exp.and_(*having))
        orders = [dict(item) if isinstance(item, dict) else {"field": str(item)} for item in request.get("order_by") or []]
        if orders:
            for item in orders:
                alias = str(item.get("field") or "")
                if alias not in aliases:
                    raise _bad(f"排序字段 {alias} 不在查询结果中。")
                select = select.order_by(exp.Ordered(this=exp.to_identifier(alias, quoted=True), desc=bool(item.get("desc"))))
        elif time_alias:
            select = select.order_by(exp.Ordered(this=exp.to_identifier(time_alias, quoted=True), desc=False))
        elif group_by:
            select = select.order_by(exp.Ordered(this=exp.to_identifier(metric_names[0], quoted=True), desc=True))
        limit = request.get("limit")
        if limit:
            select = select.limit(int(limit))
        try:
            sql = select.sql(dialect=self.dialect, pretty=True)
        except sqlglot.errors.SqlglotError as error:
            raise _bad(f"无法为 {DIALECT_LABELS.get(self.dialect, self.dialect)} 生成语句：{str(error)[:160]}") from error
        chart = {"dimension": columns[0]["name"] if dimensions else "", "metric": metric_names[0], "type": "line" if time_alias and columns[0]["name"] == time_alias else "bar"}
        return Compiled(sql=sql, dialect=self.dialect, datasets=list(self.used), joins=join_names, columns=columns, chart=chart)

    @staticmethod
    def _condition(expression: exp.Expression, operator: str, value: Any) -> exp.Expression:
        if operator == "is_null":
            return exp.Is(this=expression, expression=exp.Null())
        if operator == "not_null":
            return exp.Not(this=exp.Is(this=expression, expression=exp.Null()))
        if operator in {"in", "not_in"}:
            values = value if isinstance(value, (list, tuple)) else [item.strip() for item in str(value or "").split(",") if item.strip()]
            if not values:
                raise _bad("in / not_in 需要至少一个取值。")
            condition = exp.In(this=expression, expressions=[_literal(item) for item in values])
            return exp.Not(this=condition) if operator == "not_in" else condition
        if operator == "between":
            bounds = value if isinstance(value, (list, tuple)) else [item.strip() for item in str(value or "").split(",")]
            if len(bounds) != 2:
                raise _bad("between 需要两个取值。")
            return exp.Between(this=expression, low=_literal(bounds[0]), high=_literal(bounds[1]))
        if operator == "like":
            return exp.Like(this=expression, expression=_literal(str(value or "")))
        classes = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}
        return classes[operator](this=expression, expression=_literal(value))


# ----- the engine --------------------------------------------------------------------------
class SemanticQueryEngine:
    """Reads models from every store, answers the metric catalog and runs queries."""

    def __init__(
        self,
        *,
        models: Any = None,
        semantic: Any = None,
        queries: Any = None,
        builtin: Callable[[], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.models = models
        self.semantic = semantic
        self.queries = queries
        self.builtin = builtin
        self._clock = clock
        self._lock = threading.RLock()
        self._listing: tuple[float, list[dict[str, Any]]] | None = None
        self._cache: dict[str, tuple[float, Model]] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._listing = None
            self._cache.clear()

    # ----- discovery --------------------------------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._listing and self._clock() - self._listing[0] < CACHE_SECONDS:
                return list(self._listing[1])
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        if self.builtin is not None:
            try:
                document = self.builtin()
                if document:
                    model = parse_model(extract_document(document) or {}, model_id="builtin", storage="builtin")
                    items.append(self._summary(model, "builtin", "内置演示模型"))
            except (SemanticQueryError, ValueError) as error:
                errors.append(f"内置模型：{error}")
        if self.models is not None:
            try:
                for item in (self.models.list() or {}).get("items", []):
                    model_id = str(item.get("id") or item.get("name") or "")
                    if model_id:
                        items.append({"id": f"store:{model_id}", "name": str(item.get("name") or model_id), "storage": "lattice-model-store", "storage_label": "Lattice 模型存储", "description": "", "datasets": None, "metrics": None})
            except Exception as error:  # noqa: BLE001 - one store down must not hide the others
                errors.append(f"模型存储：{str(error)[:200]}")
        if self.semantic is not None:
            try:
                listing = self.semantic.catalog_listing(SEMANTIC_STORE_CATALOG, SEMANTIC_STORE_NAMESPACE)
                for item in listing.get("items", []):
                    name = str(item.get("name") or "")
                    if name:
                        items.append({"id": f"native:{name}", "name": name, "storage": "polaris-native", "storage_label": "Polaris 原生语义模型", "description": "", "datasets": None, "metrics": None})
            except Exception as error:  # noqa: BLE001
                errors.append(f"原生语义模型：{str(error)[:200]}")
        with self._lock:
            self._listing = (self._clock(), items)
        if errors:
            items = [*items, *({"id": "", "name": text, "storage": "error", "storage_label": "读取失败", "description": text, "datasets": None, "metrics": None} for text in errors)]
        return items

    @staticmethod
    def _summary(model: Model, model_id: str, storage_label: str) -> dict[str, Any]:
        return {"id": model_id, "name": model.name, "storage": model.storage, "storage_label": storage_label, "description": model.description, "datasets": len(model.datasets), "metrics": len(model.metrics)}

    def load_model(self, reference: Any) -> Model:
        ref = str(reference or "").strip()
        if not ref:
            raise _bad("请指定语义模型。")
        with self._lock:
            cached = self._cache.get(ref)
            if cached and self._clock() - cached[0] < CACHE_SECONDS:
                return cached[1]
        model = self._read(ref)
        with self._lock:
            self._cache[ref] = (self._clock(), model)
            self._cache[model.name] = (self._clock(), model)
        return model

    def _read(self, ref: str) -> Model:
        if ref in {"builtin", "lattice_demo_sales"} or ref.startswith("builtin:"):
            if self.builtin is None:
                raise SemanticQueryError(404, "没有内置演示模型。")
            return parse_model(extract_document(self.builtin()) or {}, model_id="builtin", storage="builtin")
        if ref.startswith("store:") and self.models is not None:
            loaded = self.models.get(ref[len("store:"):])
            document = extract_document(loaded)
            if document is None:
                raise SemanticQueryError(404, f"模型 {ref} 不是可解析的语义模型。")
            return parse_model(document, model_id=ref, storage="lattice-model-store")
        if ref.startswith("native:") and self.semantic is not None:
            response = self.semantic.load(SEMANTIC_STORE_CATALOG, SEMANTIC_STORE_NAMESPACE, ref[len("native:"):])
            body = response.get("body") if isinstance(response, dict) else None
            if not isinstance(body, dict) or response.get("status") != 200:
                raise SemanticQueryError(404, f"原生语义模型 {ref} 不存在。")
            document = extract_document(body)
            if document is None:
                raise SemanticQueryError(404, f"原生语义模型 {ref} 不是可解析的语义模型。")
            return parse_model(document, model_id=ref, storage="polaris-native")
        # A bare name: the first store that has it wins.
        for item in self.list_models():
            if item["id"] and (item["name"] == ref or item["id"] == ref):
                return self._read(item["id"])
        raise SemanticQueryError(404, f"语义模型 {ref} 不存在。")

    def describe(self, reference: Any) -> dict[str, Any]:
        model = self.load_model(reference)
        return {
            "id": model.id,
            "name": model.name,
            "storage": model.storage,
            "description": model.description,
            "datasets": [
                {
                    "name": dataset.name,
                    "source": dataset.source,
                    "description": dataset.description,
                    "primary_key": dataset.primary_key,
                    "fields": [
                        {"name": item.name, "field": f"{dataset.name}.{item.name}", "datatype": item.datatype, "is_time": item.is_time, "is_dimension": item.is_dimension, "description": item.description, "expression": expression_for(item.expressions, "duckdb"), "synonyms": item.synonyms}
                        for item in dataset.fields.values()
                    ],
                }
                for dataset in model.datasets.values()
            ],
            "relationships": [{"name": r.name, "from": r.from_dataset, "to": r.to_dataset, "from_columns": r.from_columns, "to_columns": r.to_columns} for r in model.relationships],
            "metrics": [self._metric_view(model, metric) for metric in model.metrics.values()],
            "dimensions": model.dimensions(),
            "grains": [{"value": grain, "label": label} for grain, label in GRAIN_LABELS.items()],
            "operators": list(OPERATORS),
        }

    def _metric_view(self, model: Model, metric: Metric) -> dict[str, Any]:
        expression = expression_for(metric.expressions, "duckdb")
        datasets = sorted({name for name in model.datasets if re.search(rf"\b{re.escape(name)}\.", expression)})
        return {
            "model": model.name,
            "model_id": model.id,
            "storage": model.storage,
            "name": metric.name,
            "description": metric.description,
            "expression": expression,
            "datatype": metric.datatype,
            "datasets": datasets,
            "synonyms": metric.synonyms,
        }

    def catalog(self, query: str | None = None) -> dict[str, Any]:
        """Every metric of every readable model, the registry behind 指标平台."""
        needle = str(query or "").strip().lower()
        metrics: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        for item in self.list_models():
            if not item["id"]:
                models.append(item)
                continue
            try:
                model = self.load_model(item["id"])
            except (SemanticQueryError, ValueError) as error:
                models.append({**item, "description": f"无法解析：{error}", "storage": "error"})
                continue
            models.append({**self._summary(model, item["id"], item.get("storage_label", "")), "name": item["name"], "model_name": model.name, "dimensions": len(model.dimensions())})
            for metric in model.metrics.values():
                view = self._metric_view(model, metric)
                haystack = " ".join([metric.name, metric.description, view["expression"], *metric.synonyms, model.name]).lower()
                if needle and needle not in haystack:
                    continue
                metrics.append(view)
        return {"models": models, "metrics": metrics, "total": len(metrics)}

    # ----- queries -------------------------------------------------------------------------
    def compile(self, reference: Any, request: dict[str, Any], datasource_id: str | None = None) -> dict[str, Any]:
        model = self.load_model(reference)
        record = self._record(datasource_id)
        compiled = self._compile(model, request, record)
        return {"model": model.name, "model_id": model.id, "datasource_id": record["id"], "datasource_name": record.get("name", ""), **compiled.view()}

    def _record(self, datasource_id: str | None) -> dict[str, Any]:
        if self.queries is None:
            raise SemanticQueryError(503, "查询服务尚未初始化。")
        return self.queries._record(datasource_id or LOCAL_SAMPLE)

    def _compile(self, model: Model, request: dict[str, Any], record: dict[str, Any]) -> Compiled:
        dialect = CONNECTORS[record["type"]].dialect if record.get("type") in CONNECTORS else "duckdb"
        if record["id"] == LOCAL_SAMPLE:
            return Compiler(model, dialect, bare_schemas=DEFAULT_SCHEMA_ALIASES).compile(request)
        # A model written against DuckDB's ``main`` or the Ossie examples' ``public`` runs
        # against the data source's own default schema elsewhere (lattice_demo on the local
        # engines, public on PostgreSQL); the dialect's own default-schema name stays as is.
        default = ""
        try:
            default = str(self.queries.registry.connector(record["id"]).default_schema() or "")
        except Exception:  # noqa: BLE001 - an unreachable engine still gets a statement
            default = ""
        aliases = tuple(name for name in DEFAULT_SCHEMA_ALIASES if name != NATIVE_DEFAULT_SCHEMAS.get(str(record.get("type") or "")))
        return Compiler(model, dialect, schema_map={name: default for name in aliases}, bare_schemas=() if default else aliases).compile(request)

    def query(self, reference: Any, request: dict[str, Any], *, datasource_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
        model = self.load_model(reference)
        record = self._record(datasource_id)
        started = time.monotonic()
        compiled = self._compile(model, request, record)
        with observe("semantic_query", "semantic", model=model.name, datasource=record["id"]):
            result = self.queries.execute(record, compiled.sql, request.get("limit"), refresh=refresh)
        metrics = ", ".join(str(name) for name in request.get("metrics") or [])
        dims = ", ".join(str(item.get("field") if isinstance(item, dict) else item) for item in request.get("dimensions") or [])
        title = f"{model.name}：{metrics}" + (f" 按 {dims}" if dims else "")
        steps = [
            f"读取语义模型 {model.name}（{len(model.datasets)} 个数据集，{len(model.metrics)} 个指标）。",
            f"关联数据集 {' → '.join(compiled.datasets)}" + ("，关系：" + "；".join(compiled.joins) if compiled.joins else "") + "。",
            f"按 {DIALECT_LABELS.get(compiled.dialect, compiled.dialect)} 生成聚合语句并在 {record.get('name') or record['id']} 上执行，返回 {len(result['rows'])} 行。",
        ]
        payload = self.queries.payload(record, title, result, "semantic", steps, compiled.chart, started)
        payload["semantic"] = {"model": model.name, "model_id": model.id, "datasets": compiled.datasets, "joins": compiled.joins, "columns": compiled.columns, "metrics": list(request.get("metrics") or []), "dimensions": list(request.get("dimensions") or [])}
        return payload
