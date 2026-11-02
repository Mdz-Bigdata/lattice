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

Joining is where a correct definition turns into a wrong number, so the compiler
reasons about it instead of emitting every join it can find. An Ossie relationship
points from the many side to the one side, which gives each join a direction:

* Following it never repeats a row. Against it repeats a row once per match (a
  fan-out), and SUM, AVG and plain COUNT then count those rows again. Only
  COUNT(DISTINCT), MIN and MAX survive that, so those metrics may cross such a join
  (with a warning, since a record can then fall into several groups) and the others
  are refused rather than answered with an inflated number.
* Metrics measured on different datasets (sales on order items, payments on
  payments) are aggregated separately and combined afterwards, so asking for two
  together gives each the same value it has when asked alone.
* Two equally short routes between datasets are two possible meanings, so they are
  refused until the request names the relationships to use.

``SemanticQueryEngine.query`` then checks the outcome: the groups of an additive
metric have to add up to its total, which also catches a key the model declares
unique but whose data is not.
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
from .sql_audit import parse_schema_context

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
#: Text dimensions read into the value dictionary, and how many values one may have.
MAX_VALUE_COLUMNS = 40
MAX_DIMENSION_VALUES = 200
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
    #: The date a question about this metric means by "per month" (``dataset.field``).
    time_field: str = ""


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


def _time_field(node: Any) -> str:
    context = node.get("ai_context") if isinstance(node, dict) else None
    return str(context.get("time_field") or "") if isinstance(context, dict) else ""


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
        metrics[str(raw["name"])] = Metric(str(raw["name"]), expressions, str(raw.get("datatype") or ""), str(raw.get("description") or ""), _synonyms(raw), _time_field(raw))
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
    #: What the reader must know to read the numbers correctly.
    warnings: list[str] = field(default_factory=list)
    #: Result aliases of the metrics whose groups must add up to their total.
    additive: list[str] = field(default_factory=list)
    #: Metrics aggregated apart from each other because they measure different datasets.
    branches: list[dict[str, Any]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {"sql": self.sql, "dialect": self.dialect, "dialect_label": DIALECT_LABELS.get(self.dialect, self.dialect), "datasets": self.datasets, "joins": self.joins, "columns": self.columns, "chart": self.chart, "warnings": self.warnings, "branches": self.branches}


@dataclass
class MetricShape:
    """How a metric aggregates, which decides the joins it can safely cross."""

    name: str
    #: Dataset the metric's rows are counted on; its branch starts here.
    home: str
    #: Datasets whose rows must not repeat: the grains of SUM, AVG and plain COUNT.
    grains: list[str]
    additive: bool


@dataclass
class JoinStep:
    relationship: Relationship
    parent: str
    child: str
    fans_out: bool


def _is_repeat_safe(aggregate: exp.Expression) -> bool:
    """Whether an aggregate returns the same value when its input rows repeat."""
    if isinstance(aggregate, (exp.Min, exp.Max, exp.ApproxDistinct)):
        return True
    return isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct)


def _null_type(metric: Metric) -> str:
    """The type a metric has in every branch when metrics are combined across datasets."""
    if metric.datatype == "Integer":
        return "BIGINT"
    if metric.datatype == "Float":
        return "DOUBLE"
    if metric.datatype == "String":
        return "VARCHAR"
    return "DECIMAL(38, 6)"


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
        #: Relationship names a request narrowed the joins to; None means all of them.
        self.allowed: set[str] | None = None

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

    # ----- how datasets connect -------------------------------------------------------------
    def _one_to_one(self, relationship: Relationship) -> bool:
        """The many side's foreign key is its own primary key, so neither side repeats."""
        key = self.model.datasets[relationship.from_dataset].primary_key
        return bool(key) and set(key) == set(relationship.from_columns)

    def _steps_from(self, dataset: str) -> list[JoinStep]:
        """Every join leaving a dataset; ``fans_out`` when it runs against the relationship."""
        steps = []
        for relationship in self.model.relationships:
            if self.allowed is not None and relationship.name not in self.allowed:
                continue
            if relationship.from_dataset == dataset:
                steps.append(JoinStep(relationship, dataset, relationship.to_dataset, False))
            elif relationship.to_dataset == dataset:
                steps.append(JoinStep(relationship, dataset, relationship.from_dataset, not self._one_to_one(relationship)))
        return steps

    def _shortest_routes(self, root: str, target: str, *, repeat_free: bool) -> list[list[JoinStep]]:
        """All the shortest routes between two datasets; more than one means an ambiguity."""
        layer: list[list[JoinStep]] = [[]]
        for _ in range(MAX_JOINS):
            found: list[list[JoinStep]] = []
            following: list[list[JoinStep]] = []
            for route in layer:
                visited = {root, *(step.child for step in route)}
                for step in self._steps_from(route[-1].child if route else root):
                    if step.child in visited or (repeat_free and step.fans_out):
                        continue
                    (found if step.child == target else following).append([*route, step])
            if found:
                return found
            if not following:
                return []
            layer = following
        return []

    def _reaches_without_repeat(self, dataset: str, others: set[str]) -> bool:
        return all(other == dataset or self._shortest_routes(dataset, other, repeat_free=True) for other in others)

    def _grain_of(self, datasets: set[str]) -> str:
        """The dataset whose rows an expression over these datasets is computed on."""
        return next((name for name in self.model.datasets if name in datasets and self._reaches_without_repeat(name, datasets)), "")

    def _shape(self, metric: Metric, expression: exp.Expression) -> MetricShape:
        datasets = {column.table for column in expression.find_all(exp.Column) if column.table}
        grains: list[str] = []
        repeat_sensitive = False
        for aggregate in expression.find_all(exp.AggFunc):
            if _is_repeat_safe(aggregate):
                continue
            repeat_sensitive = True
            inputs = {column.table for column in aggregate.find_all(exp.Column) if column.table}
            if not inputs:
                continue  # COUNT(*): counts the rows of the metric's own dataset, resolved below.
            grain = self._grain_of(inputs)
            if not grain:
                raise _bad(f"指标 {metric.name} 的聚合跨越了 {'、'.join(sorted(inputs))}，它们之间没有多对一关系，无法确定按哪张表的行计算。")
            if grain not in grains:
                grains.append(grain)
        home = grains[0] if grains else (self._grain_of(datasets) or next(iter(sorted(datasets)), ""))
        if not home:
            if len(self.model.datasets) > 1:
                raise _bad(f"指标 {metric.name} 没有引用任何字段，无法确定统计的是哪张表的行；请写明数据集，例如 COUNT(数据集.主键)。")
            home = next(iter(self.model.datasets))
        if repeat_sensitive and not grains:
            grains = [home]
        root = expression.unalias() if isinstance(expression, exp.Alias) else expression
        additive = isinstance(root, exp.Sum) or (isinstance(root, exp.Count) and not _is_repeat_safe(root))
        return MetricShape(metric.name, home, grains, additive)

    def _join_tree(self, root: str, needed: list[str]) -> list[JoinStep]:
        """The joins connecting every dataset in use to the root.

        A route that never repeats a row wins over a shorter one that does; among
        equals there is no telling which one the question meant, so it is refused.
        """
        tree: dict[str, JoinStep] = {}
        missing = []
        for target in needed:
            if target == root or target in tree:
                continue
            routes = self._shortest_routes(root, target, repeat_free=True) or self._shortest_routes(root, target, repeat_free=False)
            if not routes:
                missing.append(target)
                continue
            if len(routes) > 1:
                described = "；".join(" → ".join(step.relationship.name for step in route) for route in routes)
                raise _bad(f"从 {root} 到 {target} 有多条关联路径（{described}），口径不确定。请在请求的 relationships 中指定要使用的关系。")
            for step in routes[0]:
                existing = tree.get(step.child)
                if existing and existing.relationship.name != step.relationship.name:
                    raise _bad(f"{step.child} 同时经由 {existing.relationship.name} 和 {step.relationship.name} 关联，口径不确定。请在请求的 relationships 中指定要使用的关系。")
                tree[step.child] = step
        if missing:
            raise _bad(f"数据集 {root} 与 {'、'.join(missing)} 之间没有定义关系（relationships），无法关联查询。")
        if len(tree) > MAX_JOINS:
            raise _bad(f"一次查询最多关联 {MAX_JOINS} 个数据集。")
        return list(tree.values())

    @staticmethod
    def _fan_out_seen_from(dataset: str, tree: list[JoinStep], *, one_to_one: Callable[[Relationship], bool]) -> JoinStep | None:
        """The join that repeats this dataset's rows anywhere in the tree, if any."""
        reached = {dataset}
        remaining = list(tree)
        progress = True
        while remaining and progress:
            progress = False
            for step in list(remaining):
                ends = {step.parent, step.child}
                near = ends & reached
                if not near:
                    continue
                remaining.remove(step)
                progress = True
                current = next(iter(near))
                reached |= ends
                if len(near) == 2:
                    continue
                relationship = step.relationship
                if current == relationship.to_dataset and not one_to_one(relationship):
                    return step
        return None

    # ----- the statement ------------------------------------------------------------------
    def _branch(
        self,
        metric_names: list[str],
        root: str,
        dimensions: list[dict[str, Any]],
        filters: list[dict[str, Any]],
        shapes: dict[str, MetricShape],
    ) -> dict[str, Any]:
        """One aggregation: the metrics of one dataset over the shared dimensions and filters."""
        self.used = [root]
        expressions = {name: self._metric_expression(self.model.metrics[name]) for name in metric_names}
        grouped: list[dict[str, Any]] = []
        for item in dimensions:
            dataset, column = self._find_field(item.get("field"))
            expression = self._field_expression(dataset, column)
            grain = str(item.get("grain") or "").lower()
            if grain:
                if grain not in GRAINS:
                    raise _bad("时间粒度只能是 day、week、month、quarter 或 year。")
                if not column.is_time:
                    raise _bad(f"字段 {dataset.name}.{column.name} 不是时间维度，不能按 {GRAIN_LABELS[grain]}聚合。")
                expression = self._truncate(expression, grain)
            grouped.append({"dataset": dataset, "column": column, "grain": grain, "expression": expression, "alias": str(item.get("alias") or "")})
        where: list[exp.Expression] = []
        having: list[exp.Expression] = []
        for item in filters:
            target = str(item.get("field") or "")
            operator = str(item.get("op") or "=").lower()
            if operator not in OPERATORS:
                raise _bad("筛选条件的比较方式只能是 " + "、".join(OPERATORS) + "。")
            if target in self.model.metrics:
                having.append(self._condition(self._metric_expression(self.model.metrics[target]), operator, item.get("value")))
            else:
                dataset, column = self._find_field(target)
                where.append(self._condition(self._field_expression(dataset, column), operator, item.get("value")))
        tree = self._join_tree(root, [name for name in self.used if name != root])
        for name in metric_names:
            for grain in shapes[name].grains:
                culprit = self._fan_out_seen_from(grain, tree, one_to_one=self._one_to_one)
                if culprit is not None:
                    relationship = culprit.relationship
                    raise _bad(
                        f"无法按当前维度或筛选计算指标 {name}：{relationship.to_dataset} 与 {relationship.from_dataset} 是一对多关系（{relationship.name}），"
                        f"关联后 {grain} 的一行会对应多行，{name} 会被重复累计而偏大，已拒绝执行。请改用这个指标自身所在数据集可以多对一到达的维度。"
                    )
        warnings = []
        fan_targets = {step.child for step in tree if step.fans_out}
        for step in tree:
            if step.parent in fan_targets:
                fan_targets.add(step.child)
        for item in grouped:
            if item["dataset"].name in fan_targets:
                for name in metric_names:
                    warnings.append(f"指标 {name} 按 {item['dataset'].name}.{item['column'].name} 分组时，同一条记录可能同时归入多个分组（去重计数不重复累计），各分组之和可能大于总数。")
        return {"root": root, "metrics": expressions, "grouped": grouped, "where": where, "having": having, "tree": tree, "warnings": warnings}

    def _ordered(self, alias: str, desc: bool) -> exp.Ordered:
        """Order by a result column, leaving NULLs where this engine puts them by itself.

        Without an explicit choice sqlglot emulates a fixed NULL position with
        ``CASE WHEN x IS NULL``, and MySQL's ONLY_FULL_GROUP_BY rejects that for a column
        that is only grouped through an expression.
        """
        ordering = sqlglot.Dialect.get_or_raise(self.dialect).NULL_ORDERING
        if ordering == "nulls_are_small":
            nulls_first = not desc
        elif ordering == "nulls_are_large":
            nulls_first = desc
        else:
            nulls_first = False
        return exp.Ordered(this=exp.to_identifier(alias, quoted=True), desc=desc, nulls_first=nulls_first)

    def _truncate(self, expression: exp.Expression, grain: str) -> exp.Expression:
        """The start of the period.

        Hive has no DATE_TRUNC, and its TRUNC is not an option: inside GROUP BY, Hive 4.0.1
        returns 1970-01-01 for every row of a DATE column, with no error. DATE_FORMAT groups
        correctly, so Hive's periods are built from it and come back as ``YYYY-MM-DD`` text.
        """
        if self.dialect != "hive":
            return exp.DateTrunc(unit=exp.Literal.string(grain), this=expression)

        def call(name: str, *arguments: exp.Expression) -> exp.Expression:
            return exp.Anonymous(this=name, expressions=list(arguments))

        if grain == "quarter":
            first_month = sqlglot.parse_one("(QUARTER(x) - 1) * 3 + 1", read="hive").transform(lambda node: expression.copy() if isinstance(node, exp.Column) else node)
            text = exp.cast(first_month, "STRING")
            return call("CONCAT", call("YEAR", expression.copy()), exp.Literal.string("-"), call("LPAD", text, exp.Literal.number(2), exp.Literal.string("0")), exp.Literal.string("-01"))
        if grain == "week":
            monday = call("DATE_SUB", call("NEXT_DAY", expression, exp.Literal.string("MO")), exp.Literal.number(7))
            return call("DATE_FORMAT", monday, exp.Literal.string("yyyy-MM-dd"))
        pattern = {"day": "yyyy-MM-dd", "month": "yyyy-MM-01", "year": "yyyy-01-01"}[grain]
        return call("DATE_FORMAT", expression, exp.Literal.string(pattern))

    def distinct_values_sql(self, dataset: Dataset, item: Field, limit: int) -> str:
        expression = self._field_expression(dataset, item)
        select = exp.Select(expressions=[exp.alias_(expression, exp.to_identifier("value", quoted=True))]).from_(self._table(dataset))
        return select.group_by(expression.copy()).limit(limit).sql(dialect=self.dialect)

    def _from(self, branch: dict[str, Any]) -> exp.Select:
        select = exp.Select().from_(self._table(self.model.datasets[branch["root"]]))
        for step in branch["tree"]:
            relationship = step.relationship
            if relationship.from_dataset == step.parent:
                near_columns, far_columns = relationship.from_columns, relationship.to_columns
            else:
                near_columns, far_columns = relationship.to_columns, relationship.from_columns
            condition = exp.and_(*[
                exp.EQ(this=exp.Column(this=exp.to_identifier(a, quoted=True), table=self._alias(step.parent)), expression=exp.Column(this=exp.to_identifier(b, quoted=True), table=self._alias(step.child)))
                for a, b in zip(near_columns, far_columns)
            ])
            # Following a relationship keeps fact rows whose dimension is missing, so the
            # groups still add up to the total. A fan-out keeps only rows that have a match:
            # a customer without orders is not a customer of any product category.
            select = select.join(self._table(self.model.datasets[step.child]), on=condition, join_type="INNER" if step.fans_out else "LEFT")
        if branch["where"]:
            select = select.where(exp.and_(*branch["where"]))
        return select

    def compile(self, request: dict[str, Any], *, ordered: bool = True) -> Compiled:
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
        chosen = request.get("relationships")
        self.allowed = {str(name) for name in chosen} if chosen else None

        for name in metric_names:
            if name not in self.model.metrics:
                raise _bad(f"语义模型中没有指标 {name}。")
        metric_filters = [item for item in filters if str(item.get("field") or "") in self.model.metrics]
        dimension_filters = [item for item in filters if item not in metric_filters]
        hidden = [str(item["field"]) for item in metric_filters if str(item["field"]) not in metric_names]
        shapes: dict[str, MetricShape] = {}
        for name in [*metric_names, *hidden]:
            metric = self.model.metrics[name]
            self.used = []
            shapes[name] = self._shape(metric, self._metric_expression(metric))
        homes: list[str] = []
        for name in [*metric_names, *hidden]:
            if shapes[name].home not in homes:
                homes.append(shapes[name].home)
        if len(homes) == 1:
            plan = [self._branch(metric_names, homes[0], dimensions, filters, shapes)]
        else:
            plan = [self._branch([name for name in [*metric_names, *hidden] if shapes[name].home == home], home, dimensions, dimension_filters, shapes) for home in homes]

        # Result columns: dimensions first, then the metrics asked for.
        columns: list[dict[str, Any]] = []
        seen_aliases: set[str] = set()
        time_alias = ""
        dimension_aliases: list[str] = []
        for item in plan[0]["grouped"]:
            dataset, column = item["dataset"], item["column"]
            alias = item["alias"] or column.name
            if alias in seen_aliases:
                alias = f"{dataset.name}_{column.name}"
            seen_aliases.add(alias)
            dimension_aliases.append(alias)
            columns.append({"name": alias, "kind": "dimension", "field": f"{dataset.name}.{column.name}", "grain": item["grain"] or None, "datatype": column.datatype})
            if column.is_time and not time_alias:
                time_alias = alias
        metric_aliases: dict[str, str] = {}
        for name in metric_names:
            alias = name if name not in seen_aliases else f"metric_{name}"
            seen_aliases.add(alias)
            metric_aliases[name] = alias
            columns.append({"name": alias, "kind": "metric", "metric": name, "datatype": self.model.metrics[name].datatype})

        if len(plan) == 1:
            branch = plan[0]
            select = self._from(branch)
            for item, alias in zip(branch["grouped"], dimension_aliases):
                select = select.select(exp.alias_(item["expression"], exp.to_identifier(alias, quoted=True)), append=True)
            for name in metric_names:
                select = select.select(exp.alias_(branch["metrics"][name], exp.to_identifier(metric_aliases[name], quoted=True)), append=True)
            if branch["grouped"]:
                select = select.group_by(*[item["expression"].copy() for item in branch["grouped"]])
            if branch["having"]:
                select = select.having(exp.and_(*branch["having"]))
        else:
            select = self._across(plan, metric_names, hidden, dimension_aliases, metric_aliases, metric_filters)

        aliases = {*dimension_aliases, *metric_aliases.values()}
        orders = [dict(item) if isinstance(item, dict) else {"field": str(item)} for item in request.get("order_by") or []]
        for item in orders:
            if str(item.get("field") or "") not in aliases:
                raise _bad(f"排序字段 {item.get('field') or ''} 不在查询结果中。")
        if ordered:
            first_metric = metric_aliases[metric_names[0]]
            if orders:
                for item in orders:
                    select = select.order_by(self._ordered(str(item["field"]), bool(item.get("desc"))))
            elif time_alias:
                select = select.order_by(self._ordered(time_alias, False))
            elif dimensions:
                select = select.order_by(self._ordered(first_metric, True))
            # Equal values have no order of their own, and LIMIT would then pick among them
            # differently from one run to the next. The dimensions settle it.
            placed = {str(item["field"]) for item in orders} | ({time_alias} if not orders and time_alias else set())
            for alias in dimension_aliases:
                if alias not in placed:
                    select = select.order_by(self._ordered(alias, False))
            limit = request.get("limit")
            if limit:
                select = select.limit(int(limit))
        try:
            sql = select.sql(dialect=self.dialect, pretty=True)
        except sqlglot.errors.SqlglotError as error:
            raise _bad(f"无法为 {DIALECT_LABELS.get(self.dialect, self.dialect)} 生成语句：{str(error)[:160]}") from error

        datasets: list[str] = []
        joins: list[str] = []
        warnings: list[str] = []
        for branch in plan:
            for name in [branch["root"], *(step.child for step in branch["tree"])]:
                if name not in datasets:
                    datasets.append(name)
            for step in branch["tree"]:
                text = f"{step.relationship.name}: {step.parent} → {step.child}"
                if text not in joins:
                    joins.append(text)
            for text in branch["warnings"]:
                if text not in warnings:
                    warnings.append(text)
        self.used = datasets
        # One axis cannot show two dimensions: the same month would appear once per region.
        chart_type = "table" if len(dimension_aliases) > 1 else ("line" if time_alias and dimension_aliases[0] == time_alias else "bar")
        chart = {"dimension": dimension_aliases[0] if dimension_aliases else "", "metric": metric_aliases[metric_names[0]], "type": chart_type}
        branches = []
        if len(plan) > 1:
            branches = [{"dataset": branch["root"], "metrics": [name for name in metric_names if name in branch["metrics"]]} for branch in plan]
            warnings.append("这些指标分别来自不同的事实表（" + "、".join(branch["root"] for branch in plan) + "），已各自聚合后再按维度合并，避免相互放大。")
        additive = [metric_aliases[name] for name in metric_names if shapes[name].additive]
        return Compiled(sql=sql, dialect=self.dialect, datasets=datasets, joins=joins, columns=columns, chart=chart, warnings=warnings, additive=additive, branches=branches)

    def _across(
        self,
        plan: list[dict[str, Any]],
        metric_names: list[str],
        hidden: list[str],
        dimension_aliases: list[str],
        metric_aliases: dict[str, str],
        metric_filters: list[dict[str, Any]],
    ) -> exp.Select:
        """Combine separately aggregated metrics by their dimensions.

        UNION ALL with an outer GROUP BY is used instead of FULL OUTER JOIN because
        MySQL has no FULL OUTER JOIN and a NULL dimension would not match itself in one.
        Inner columns have neutral names: ClickHouse lets an alias shadow a column inside
        the aggregate that defines it, which ``MAX(total_sales) AS total_sales`` would hit.
        """
        every_metric = [*metric_names, *hidden]
        inner = {name: f"lattice_m{index}" for index, name in enumerate(every_metric)}
        dimension_columns = [f"lattice_d{index}" for index in range(len(dimension_aliases))]
        combined: exp.Expression | None = None
        for branch in plan:
            part = self._from(branch)
            for item, column in zip(branch["grouped"], dimension_columns):
                part = part.select(exp.alias_(item["expression"], exp.to_identifier(column, quoted=True)), append=True)
            for name in every_metric:
                # The branches must agree on a type: ClickHouse has no common type for its
                # UInt64 counts and a NULL Int64, and a smaller scale could round a decimal.
                value = branch["metrics"].get(name)
                value = exp.cast(exp.Null() if value is None else value, _null_type(self.model.metrics[name]))
                part = part.select(exp.alias_(value, exp.to_identifier(inner[name], quoted=True)), append=True)
            if branch["grouped"]:
                part = part.group_by(*[item["expression"].copy() for item in branch["grouped"]])
            combined = part if combined is None else exp.union(combined, part, distinct=False)
        outer = exp.Select().from_(combined.subquery("lattice_metrics"))
        for column, alias in zip(dimension_columns, dimension_aliases):
            outer = outer.select(exp.alias_(exp.column(exp.to_identifier(column, quoted=True)), exp.to_identifier(alias, quoted=True)), append=True)
        for name in metric_names:
            outer = outer.select(exp.alias_(exp.Max(this=exp.column(exp.to_identifier(inner[name], quoted=True))), exp.to_identifier(metric_aliases[name], quoted=True)), append=True)
        if dimension_columns:
            outer = outer.group_by(*[exp.column(exp.to_identifier(column, quoted=True)) for column in dimension_columns])
        conditions = [
            self._condition(exp.Max(this=exp.column(exp.to_identifier(inner[str(item["field"])], quoted=True))), str(item.get("op") or "=").lower(), item.get("value"))
            for item in metric_filters
        ]
        if conditions:
            outer = outer.having(exp.and_(*conditions))
        return outer

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
        self._values: dict[tuple[str, str], tuple[float, dict[str, list[str]]]] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._listing = None
            self._cache.clear()
            self._values.clear()

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
        return self._compiler(model, record).compile(request)

    def _compiler(self, model: Model, record: dict[str, Any]) -> Compiler:
        dialect = CONNECTORS[record["type"]].dialect if record.get("type") in CONNECTORS else "duckdb"
        if record["id"] == LOCAL_SAMPLE:
            return Compiler(model, dialect, bare_schemas=DEFAULT_SCHEMA_ALIASES)
        # A model written against DuckDB's ``main`` or the Ossie examples' ``public`` runs
        # against the data source's own default schema elsewhere (lattice_demo on the local
        # engines, public on PostgreSQL); the dialect's own default-schema name stays as is.
        default = ""
        try:
            default = str(self.queries.registry.connector(record["id"]).default_schema() or "")
        except Exception:  # noqa: BLE001 - an unreachable engine still gets a statement
            default = ""
        aliases = tuple(name for name in DEFAULT_SCHEMA_ALIASES if name != NATIVE_DEFAULT_SCHEMAS.get(str(record.get("type") or "")))
        return Compiler(model, dialect, schema_map={name: default for name in aliases}, bare_schemas=() if default else aliases)

    def models_for(self, record: dict[str, Any], question: str = "") -> list[Model]:
        """Models whose every table exists on this data source; user-defined ones come first."""
        tables = self._source_tables(record)
        if not tables:
            return []
        found: list[Model] = []
        for item in self.list_models():
            if not item.get("id"):
                continue
            try:
                model = self.load_model(item["id"])
            except (SemanticQueryError, ValueError):
                continue
            if model.metrics and all(self._source_table_name(dataset.source) in tables for dataset in model.datasets.values()):
                found.append(model)
        found.sort(key=lambda model: model.storage == "builtin")
        return found

    @staticmethod
    def _source_table_name(source: str) -> str:
        text = source.strip()
        if re.match(r"^\s*(select|with)\b", text, re.IGNORECASE):
            return ""  # A query as a dataset: its tables cannot be matched by name.
        return text.rsplit(".", 1)[-1].lower()

    def _source_tables(self, record: dict[str, Any]) -> set[str]:
        if self.queries is None:
            return set()
        try:
            if record["id"] == LOCAL_SAMPLE:
                return {str(table["name"]).lower() for table in self.queries.store.metadata()}
            return set(parse_schema_context(self.queries.registry.schema_context(record["id"])))
        except Exception:  # noqa: BLE001 - an unreachable source simply has no model to offer
            return set()

    def dimension_values(self, model: Model, record: dict[str, Any]) -> dict[str, list[str]]:
        """The real values of low-cardinality text dimensions on this data source.

        A filter can only be right if it uses a value the data contains, so these are
        what question words are matched against and what a language model must choose
        from. A column with too many values is left out rather than truncated: seeing
        only part of it would make a real value look unknown.
        """
        key = (model.id, record["id"])
        with self._lock:
            cached = self._values.get(key)
            if cached and self._clock() - cached[0] < CACHE_SECONDS:
                return cached[1]
        compiler = self._compiler(model, record)
        values: dict[str, list[str]] = {}
        count = 0
        for dataset in model.datasets.values():
            for item in dataset.fields.values():
                if not item.is_dimension or item.is_time or item.datatype not in {"String", ""} or count >= MAX_VALUE_COLUMNS:
                    continue
                count += 1
                try:
                    result = self.queries.execute(record, compiler.distinct_values_sql(dataset, item, MAX_DIMENSION_VALUES + 1), MAX_DIMENSION_VALUES + 1)
                except Exception:  # noqa: BLE001 - no dictionary for this column; its filters are unchecked
                    continue
                options = [str(row[0]) for row in result["rows"] if row and row[0] not in (None, "")]
                if options and len(options) <= MAX_DIMENSION_VALUES:
                    values[f"{dataset.name}.{item.name}"] = sorted(options)
        with self._lock:
            self._values[key] = (self._clock(), values)
        return values

    def definitions(self, model: Model, metric_names: list[str]) -> list[dict[str, str]]:
        return [
            {"name": name, "description": model.metrics[name].description, "expression": expression_for(model.metrics[name].expressions, "duckdb"), "time_field": model.metrics[name].time_field}
            for name in metric_names
            if name in model.metrics
        ]

    def _verify(self, model: Model, request: dict[str, Any], compiled: Compiled, record: dict[str, Any], result: dict[str, Any], refresh: bool) -> list[dict[str, Any]]:
        """Check the answer against what the metric definitions promise.

        The groups of an additive metric must add up to its total computed without any
        grouping. A difference means a join repeated or dropped rows, typically a key the
        model declares unique that has duplicates in the data, so it is an error: a number
        known to be wrong is not an answer. Failing to run the check is reported as such.
        """
        checks: list[dict[str, Any]] = []
        if compiled.joins:
            checks.append({"name": "关联路径", "status": "passed", "detail": "按语义模型声明的关系生成：" + "；".join(compiled.joins)})
        if compiled.branches:
            checks.append({"name": "多事实表口径", "status": "passed", "detail": "；".join(f"{item['dataset']}：{'、'.join(item['metrics'])}" for item in compiled.branches) + "，各自聚合后合并。"})
        dimensions = request.get("dimensions") or []
        filters = request.get("filters") or []
        metric_filtered = any(str(item.get("field")) in model.metrics for item in filters if isinstance(item, dict))
        if not dimensions or not compiled.additive or metric_filtered:
            return checks
        names = [str(column["metric"]) for column in compiled.columns if column["kind"] == "metric" and column["name"] in compiled.additive]
        aliases = {str(column["metric"]): column["name"] for column in compiled.columns if column["kind"] == "metric"}
        try:
            compiler = self._compiler(model, record)
            whole = compiler.compile({"metrics": names, "filters": filters, "relationships": request.get("relationships")}, ordered=False)
            baseline = self.queries.execute(record, whole.sql, refresh=refresh)
            totals = dict(zip(baseline["columns"], baseline["rows"][0])) if baseline["rows"] else {}
            if request.get("limit") or result.get("truncated"):
                grouped = compiler.compile({**request, "limit": None, "order_by": None}, ordered=False)
                tree = sqlglot.parse_one(grouped.sql, read=compiled.dialect)
                wrapper = exp.Select(expressions=[exp.alias_(exp.Sum(this=exp.column(exp.to_identifier(aliases[name], quoted=True))), exp.to_identifier(aliases[name], quoted=True)) for name in names]).from_(tree.subquery("lattice_total"))
                summed = self.queries.execute(record, wrapper.sql(dialect=compiled.dialect), refresh=refresh)
                parts = dict(zip(summed["columns"], summed["rows"][0])) if summed["rows"] else {}
            else:
                parts = {aliases[name]: sum(float(row[result["columns"].index(aliases[name])] or 0) for row in result["rows"]) for name in names}
        except Exception as error:  # noqa: BLE001 - the answer stands, and says it was not reconciled
            checks.append({"name": "分组合计与总数核对", "status": "skipped", "detail": f"核对未能执行：{str(error)[:200]}"})
            return checks
        for name in names:
            alias = aliases[name]
            expected, actual = totals.get(name), parts.get(alias)
            if expected is None and actual in (None, 0, 0.0):
                continue  # Nothing matched the filters on either side.
            expected_value, actual_value = float(expected or 0), float(actual or 0)
            if abs(expected_value - actual_value) > max(0.01, abs(expected_value) * 1e-9):
                raise SemanticQueryError(
                    409,
                    f"结果未通过口径校验，已拒绝返回：指标 {name} 各分组合计 {actual_value:,.2f}，不分组的总数为 {expected_value:,.2f}。"
                    "这通常表示关联键在数据中不唯一（语义模型声明的主键与实际数据不符）。请检查相关维表后重试。",
                )
            checks.append({"name": f"{name} 分组合计与总数核对", "status": "passed", "detail": f"各分组合计 {actual_value:,.2f} = 总数 {expected_value:,.2f}"})
        return checks

    def query(
        self,
        reference: Any,
        request: dict[str, Any],
        *,
        datasource_id: str | None = None,
        refresh: bool = False,
        title: str | None = None,
        steps: list[str] | None = None,
        planner: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        model = self.load_model(reference)
        record = self._record(datasource_id)
        started = time.monotonic()
        compiled = self._compile(model, request, record)
        with observe("semantic_query", "semantic", model=model.name, datasource=record["id"]):
            result = self.queries.execute(record, compiled.sql, request.get("limit"), refresh=refresh)
        checks = self._verify(model, request, compiled, record, result, refresh)
        metrics = ", ".join(str(name) for name in request.get("metrics") or [])
        dims = ", ".join(str(item.get("field") if isinstance(item, dict) else item) for item in request.get("dimensions") or [])
        heading = title or f"{model.name}：{metrics}" + (f" 按 {dims}" if dims else "")
        reasoning = [
            *(steps or []),
            f"读取语义模型 {model.name}（{len(model.datasets)} 个数据集，{len(model.metrics)} 个指标）。",
            f"关联数据集 {' → '.join(compiled.datasets)}" + ("，关系：" + "；".join(compiled.joins) if compiled.joins else "") + "。",
            *compiled.warnings,
            f"按 {DIALECT_LABELS.get(compiled.dialect, compiled.dialect)} 生成聚合语句并在 {record.get('name') or record['id']} 上执行，返回 {len(result['rows'])} 行。",
            *(f"校验通过：{check['detail']}" for check in checks if check["status"] == "passed" and "核对" in check["name"]),
        ]
        names = [str(name) for name in request.get("metrics") or []]
        extra = {
            "semantic": {
                "model": model.name,
                "model_id": model.id,
                "datasets": compiled.datasets,
                "joins": compiled.joins,
                "columns": compiled.columns,
                "metrics": list(request.get("metrics") or []),
                "dimensions": list(request.get("dimensions") or []),
                "filters": list(request.get("filters") or []),
                "definitions": self.definitions(model, names),
                "warnings": compiled.warnings,
                "planner": planner,
                **({"question": question} if question else {}),
            },
            "verification": {"governed": True, "status": "skipped" if any(check["status"] == "skipped" for check in checks) else "passed", "checks": checks},
        }
        return self.queries.payload(record, heading, result, "semantic", reasoning, compiled.chart, started, extra=extra)
