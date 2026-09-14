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

"""Metric catalog and per-dialect SQL generation for the data-quality module.

A rule becomes three self-contained read-only statements — the number of
non-conforming rows, the number of examined rows, and the SELECT that lists the
non-conforming rows for sampling. Nothing else is needed to execute a check, so a
rule runs through the existing read-only connector layer without a view, a
temporary table or any other object on the target datasource.

The catalog follows the six categories of the product screens rather than
datavines' own grouping: datavines files ``column_match_regex``, ``column_length``
and ``column_value_between`` under COMPLETENESS and its cross-table check under
ACCURACY, while this module groups the conformance checks under 准确性校验 and the
cross-table existence check under 关联性校验. Every metric is negative-direction:
the actual value is the count of non-conforming rows.

Engine differences are isolated in ``SCRIPTS``, in the spirit of datavines'
``MetricScript``. The freshness check deliberately does not copy datavines'
``DATE_FORMAT`` script, which is invalid on PostgreSQL; timestamps are compared
directly with each engine's own interval arithmetic instead. Iceberg and Paimon
run through DuckDB over a bounded Arrow scan, so their results carry
``MetricSql.scan_limited``.

The module is pure — it opens no connection and performs no I/O. Identifiers go
through ``check_identifier`` plus the dialect's quoting, every value that reaches
SQL as a literal is validated and quoted here, and every statement returned has
already been accepted by ``guard_sql``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import sqlglot
from sqlglot import exp

from .connectors import ConnectorError, check_identifier, guard_sql

MAX_VALUE_LENGTH = 4096
SCAN_LIMITED_TYPES = ("iceberg", "paimon")
SQL_COMMENTS = ("--", "/*", "*/", "#")
COMPARATORS = ("=", "!=", ">", ">=", "<", "<=")
INTERVAL_UNITS = ("minute", "hour", "day")
SOURCE_ALIAS = "lattice_src"
REFERENCE_ALIAS = "lattice_ref"


class MetricError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    type: str
    required: bool = False
    default: object = None
    placeholder: str = ""
    help: str = ""
    options: tuple = ()


@dataclass(frozen=True)
class MetricContext:
    """Everything the generator needs about the rule's target.

    ``source_type`` is the datasource type id rather than the dialect: Iceberg and
    Paimon both speak the DuckDB dialect, so only the type tells the generator that
    the scan is bounded.
    """

    dialect: str
    schema: str | None
    table: str
    column: str | None
    config: dict
    quote: Callable[[str], str]
    qualified: Callable[[str | None, str], str]
    source_type: str = ""


@dataclass(frozen=True)
class MetricSql:
    actual_sql: str
    total_sql: str
    invalidate_sql: str
    predicate: str
    scan_limited: bool = False


@dataclass(frozen=True)
class DialectScript:
    """The SQL fragments that differ per engine, as datavines' MetricScript does."""

    quote: str
    not_match_regex: str
    length: str
    now: str
    older_than: str
    day_only: bool = False


@dataclass(frozen=True)
class Dimension:
    id: str
    label: str
    metrics: tuple[str, ...]


@dataclass(frozen=True)
class _Target:
    """A metric's inputs with identifiers already checked and quoted."""

    ctx: MetricContext
    script: DialectScript
    table: str
    column: str
    config: dict[str, Any]
    filter: str


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    dimension: str
    level: str
    needs_column: bool
    fields: tuple[ConfigField, ...]
    sql: Callable[[_Target], tuple[str, str]]
    refine: Callable[[dict[str, Any]], None] | None = None


# ---------------------------------------------------------------------------
# Dialect scripts
# ---------------------------------------------------------------------------
_MYSQL_SCRIPT = DialectScript(
    quote="`",
    not_match_regex="{column} NOT REGEXP '{regexp}'",
    length="length({column})",
    now="now()",
    older_than="{column} < DATE_SUB({now}, INTERVAL {value} {unit_upper})",
)

SCRIPTS: dict[str, DialectScript] = {
    "postgres": DialectScript(
        quote='"',
        not_match_regex="{column} !~ '{regexp}'",
        length="length({column}::text)",
        now="now()",
        older_than="{column} < {now} - interval '{value} {unit}'",
    ),
    "mysql": _MYSQL_SCRIPT,
    "starrocks": _MYSQL_SCRIPT,
    "doris": _MYSQL_SCRIPT,
    "clickhouse": DialectScript(
        quote="`",
        not_match_regex="NOT match({column}, '{regexp}')",
        length="length(toString({column}))",
        now="now()",
        older_than="{column} < {now} - INTERVAL {value} {unit_upper}",
    ),
    "hive": DialectScript(
        quote="`",
        not_match_regex="NOT ({column} RLIKE '{regexp}')",
        length="length({column})",
        now="current_timestamp()",
        # Hive's date_sub takes whole days, which is why the unit is restricted.
        older_than="{column} < date_sub({now}, {days})",
        day_only=True,
    ),
    "duckdb": DialectScript(
        quote='"',
        not_match_regex="NOT regexp_matches({column}, '{regexp}')",
        length="length(CAST({column} AS VARCHAR))",
        now="now()",
        older_than="{column} < {now} - INTERVAL {value} {unit}",
    ),
}
DIALECTS: tuple[str, ...] = tuple(sorted(SCRIPTS))


def quoter(dialect: str) -> Callable[[str], str]:
    """Identifier quoting for a dialect, with the same doubling rule as Connector."""
    quote = _script(dialect).quote

    def quote_identifier(name: str) -> str:
        return quote + name.replace(quote, quote + quote) + quote

    return quote_identifier


def qualifier(dialect: str) -> Callable[[str | None, str], str]:
    quote_identifier = quoter(dialect)

    def qualified(schema: str | None, name: str) -> str:
        if schema:
            return f"{quote_identifier(schema)}.{quote_identifier(name)}"
        return quote_identifier(name)

    return qualified


def make_context(
    dialect: str,
    schema: str | None,
    table: str,
    column: str | None = None,
    config: dict | None = None,
    *,
    source_type: str = "",
) -> MetricContext:
    """Build a context for callers that hold a dialect rather than a connector."""
    return MetricContext(
        dialect=dialect,
        schema=schema,
        table=table,
        column=column,
        config=dict(config or {}),
        quote=quoter(dialect),
        qualified=qualifier(dialect),
        source_type=source_type,
    )


# ---------------------------------------------------------------------------
# Literal and identifier safety
# ---------------------------------------------------------------------------
def _script(dialect: Any) -> DialectScript:
    script = SCRIPTS.get(dialect) if isinstance(dialect, str) else None
    if script is None:
        raise MetricError(400, f"不支持的数据源方言：{str(dialect)[:40]}。")
    return script


def _identifier(value: Any, what: str) -> str:
    """Reject anything that could break out of a quoted identifier."""
    try:
        return check_identifier(value, what)
    except ConnectorError as error:
        raise MetricError(400, str(error)) from error


def _check_literal(value: str, label: str, *, newlines: bool = False) -> str:
    """Reject anything that could end the statement or hide inside a literal.

    Newlines are tolerated only for the multi-line fields, whose individual values
    are checked again once split.
    """
    if len(value) > MAX_VALUE_LENGTH:
        raise MetricError(400, f"{label} 包含非法内容或过长。")
    allowed = "\r\n" if newlines else ""
    if ";" in value or any(ord(char) < 0x20 and char not in allowed for char in value):
        raise MetricError(400, f"{label} 不能包含分号或控制字符。")
    return value


def _string_literal(value: str, label: str) -> str:
    """Quote a validated value as a SQL string literal, doubling single quotes."""
    return "'" + _check_literal(value, label).replace("'", "''") + "'"


def _number(value: Any, label: str) -> int | float:
    if isinstance(value, bool):
        raise MetricError(400, f"{label} 必须是数字。")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        number = value
    else:
        if not isinstance(value, str):
            raise MetricError(400, f"{label} 必须是数字。")
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError as error:
            raise MetricError(400, f"{label} 必须是数字。") from error
    if not math.isfinite(number):
        raise MetricError(400, f"{label} 必须是有限数字。")
    return int(number) if number.is_integer() else number


def _integer(value: Any, label: str) -> int:
    number = _number(value, label)
    if not isinstance(number, int):
        raise MetricError(400, f"{label} 必须是整数。")
    return number


def _number_literal(value: int | float) -> str:
    return str(value) if isinstance(value, int) else repr(value)


def _check_filter(value: str, dialect: str) -> str:
    """Accept a user filter only as a plain boolean expression of this dialect."""
    text = value.strip()
    if not text:
        return ""
    _check_literal(text, "过滤条件")
    if any(marker in text for marker in SQL_COMMENTS):
        raise MetricError(400, "过滤条件不允许包含 SQL 注释。")
    try:
        tree = sqlglot.parse_one(f"SELECT 1 WHERE {text}", read=dialect)
    except sqlglot.errors.SqlglotError as error:
        raise MetricError(400, "过滤条件不是合法的布尔表达式。") from error
    where = tree.args.get("where") if isinstance(tree, exp.Select) else None
    if where is None:
        raise MetricError(400, "过滤条件不是合法的布尔表达式。")
    for node in where.walk():
        if isinstance(node, (exp.Query, exp.Command, exp.DDL, exp.DML, exp.Transaction)):
            raise MetricError(400, "过滤条件不允许包含子查询或其他语句。")
    return text


def _enum_values(text: str) -> list[str]:
    """Split the enum list on commas and newlines, keeping the first of each value."""
    values: list[str] = []
    for chunk in text.replace("\r", "\n").replace("\n", ",").split(","):
        item = chunk.strip()
        if item and item not in values:
            values.append(item)
    return values


# ---------------------------------------------------------------------------
# Per-metric configuration refinement
# ---------------------------------------------------------------------------
def _refine_regex(config: dict[str, Any]) -> None:
    # The pattern is embedded in a string literal, so a quote is rejected outright
    # rather than escaped: a doubled quote would silently change the pattern.
    if "'" in config["regexp"]:
        raise MetricError(400, "正则表达式不能包含单引号。")
    _check_literal(config["regexp"], "正则表达式")


def _refine_length(config: dict[str, Any]) -> None:
    length = _integer(config["length"], "长度")
    if length < 0:
        raise MetricError(400, "长度必须是不小于 0 的整数。")
    config["length"] = length


def _refine_value_between(config: dict[str, Any]) -> None:
    if config["min"] is None and config["max"] is None:
        raise MetricError(400, "请至少填写最小值或最大值。")
    if config["min"] is not None and config["max"] is not None and config["min"] > config["max"]:
        raise MetricError(400, "最小值不能大于最大值。")


def _refine_enums(config: dict[str, Any]) -> None:
    values = _enum_values(config["enum_list"])
    if not values:
        raise MetricError(400, "请填写 枚举值列表。")
    for value in values:
        _check_literal(value, "枚举值列表")
    # Stored in one normalised form so the value round-trips through the database.
    config["enum_list"] = ",".join(values)


def _refine_freshness(config: dict[str, Any]) -> None:
    interval = _integer(config["interval_value"], "时间间隔")
    if interval < 1:
        raise MetricError(400, "时间间隔必须是不小于 1 的整数。")
    config["interval_value"] = interval


# ---------------------------------------------------------------------------
# Per-metric SQL. Each function returns (predicate, invalidate_sql); an empty
# invalidate_sql means the default `SELECT * FROM <table> WHERE <predicate>` shape.
# ---------------------------------------------------------------------------
def _and_filter(predicate: str, filter_sql: str) -> str:
    """AND the user filter last, as datavines addFiltersIntoInvalidateItemsSql does."""
    return f"{predicate} AND ({filter_sql})" if filter_sql else predicate


def _sql_duplicate(target: _Target) -> tuple[str, str]:
    column, table = target.column, target.table
    where = f" WHERE ({target.filter})" if target.filter else ""
    invalidate = (
        f"SELECT {column} FROM {table}{where}"
        f" GROUP BY {column} HAVING count({column}) > 1"
    )
    return f"count({column}) > 1", invalidate


def _sql_null(target: _Target) -> tuple[str, str]:
    return f"({target.column} IS NULL)", ""


def _sql_blank(target: _Target) -> tuple[str, str]:
    column = target.column
    return f"({column} IS NULL OR {column} = '')", ""


def _sql_match_regex(target: _Target) -> tuple[str, str]:
    column = target.column
    regexp = _check_literal(target.config["regexp"], "正则表达式")
    not_match = target.script.not_match_regex.format(column=column, regexp=regexp)
    return f"({column} IS NOT NULL AND {not_match})", ""


def _sql_length(target: _Target) -> tuple[str, str]:
    column = target.column
    length = target.script.length.format(column=column)
    conforming = f"{length} {target.config['comparator']} {target.config['length']}"
    return f"({column} IS NOT NULL AND NOT ({conforming}))", ""


def _sql_value_between(target: _Target) -> tuple[str, str]:
    column = target.column
    bounds = []
    if target.config["min"] is not None:
        bounds.append(f"{column} >= {_number_literal(target.config['min'])}")
    if target.config["max"] is not None:
        bounds.append(f"{column} <= {_number_literal(target.config['max'])}")
    conforming = " AND ".join(bounds)
    return f"({column} IS NOT NULL AND NOT ({conforming}))", ""


def _sql_not_in_enums(target: _Target) -> tuple[str, str]:
    column = target.column
    values = ", ".join(
        _string_literal(value, "枚举值列表") for value in _enum_values(target.config["enum_list"])
    )
    return f"({column} IS NOT NULL AND {column} NOT IN ({values}))", ""


def _sql_not_in_reference(target: _Target) -> tuple[str, str]:
    config = target.config
    schema = config["reference_schema"] or None
    if schema:
        _identifier(schema, "关联模式名")
    reference = target.ctx.qualified(schema, _identifier(config["reference_table"], "关联表名"))
    reference_column = target.ctx.quote(_identifier(config["reference_column"], "关联字段"))
    source = f"{SOURCE_ALIAS}.{target.column}"
    predicate = (
        f"({source} IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {reference} {REFERENCE_ALIAS}"
        f" WHERE {REFERENCE_ALIAS}.{reference_column} = {source}))"
    )
    invalidate = (
        f"SELECT * FROM {target.table} {SOURCE_ALIAS}"
        f" WHERE {_and_filter(predicate, target.filter)}"
    )
    return predicate, invalidate


def _sql_freshness(target: _Target) -> tuple[str, str]:
    script = target.script
    value = target.config["interval_value"]
    unit = target.config["interval_unit"]
    if script.day_only and unit != "day":
        raise MetricError(400, "Hive 数据源的数据新鲜度检查仅支持按天，请将时间单位改为天。")
    predicate = script.older_than.format(
        column=target.column,
        now=script.now,
        value=value,
        unit=unit,
        unit_upper=unit.upper(),
        days=value,
    )
    return f"({predicate})", ""


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
FILTER_FIELD = ConfigField(
    "filter",
    "过滤条件",
    "text",
    placeholder="status = 'active'",
    help="附加的 SQL 布尔表达式，与核查条件以 AND 相连。",
)


def _fields(*items: ConfigField) -> tuple[ConfigField, ...]:
    return (*items, FILTER_FIELD)


METRICS: dict[str, Metric] = {
    metric.id: metric
    for metric in (
        Metric(
            id="column_duplicate",
            label="重复值检查",
            dimension="uniqueness",
            level="column",
            needs_column=True,
            fields=_fields(),
            sql=_sql_duplicate,
        ),
        Metric(
            id="column_null",
            label="空值检查",
            dimension="completeness",
            level="column",
            needs_column=True,
            fields=_fields(),
            sql=_sql_null,
        ),
        Metric(
            id="column_blank",
            label="空字符串检查",
            dimension="completeness",
            level="column",
            needs_column=True,
            fields=_fields(),
            sql=_sql_blank,
        ),
        Metric(
            id="column_match_regex",
            label="正则匹配检查",
            dimension="accuracy",
            level="column",
            needs_column=True,
            fields=_fields(
                ConfigField(
                    "regexp",
                    "正则表达式",
                    "text",
                    required=True,
                    placeholder="^[0-9]{6}$",
                    help="不匹配该正则的非空值计为不合规。",
                )
            ),
            sql=_sql_match_regex,
            refine=_refine_regex,
        ),
        Metric(
            id="column_length",
            label="字段长度检查",
            dimension="accuracy",
            level="column",
            needs_column=True,
            fields=_fields(
                ConfigField(
                    "comparator",
                    "比较符",
                    "select",
                    required=True,
                    default="<=",
                    options=(
                        ("=", "等于"),
                        ("!=", "不等于"),
                        (">", "大于"),
                        (">=", "大于等于"),
                        ("<", "小于"),
                        ("<=", "小于等于"),
                    ),
                ),
                ConfigField("length", "长度", "number", required=True, placeholder="20"),
            ),
            sql=_sql_length,
            refine=_refine_length,
        ),
        Metric(
            id="column_value_between",
            label="区间检查",
            dimension="accuracy",
            level="column",
            needs_column=True,
            fields=_fields(
                ConfigField("min", "最小值", "number", help="留空表示不限制下限。"),
                ConfigField("max", "最大值", "number", help="留空表示不限制上限。"),
            ),
            sql=_sql_value_between,
            refine=_refine_value_between,
        ),
        Metric(
            id="column_not_in_enums",
            label="枚举值检查",
            dimension="standard",
            level="column",
            needs_column=True,
            fields=_fields(
                ConfigField(
                    "enum_list",
                    "枚举值列表",
                    "textarea",
                    required=True,
                    placeholder="男,女",
                    help="使用英文逗号或换行分隔，按字符串比较。",
                )
            ),
            sql=_sql_not_in_enums,
            refine=_refine_enums,
        ),
        Metric(
            id="column_not_in_reference",
            label="关联存在性检查",
            dimension="relation",
            level="column",
            needs_column=True,
            fields=_fields(
                ConfigField("reference_schema", "关联模式名", "text", placeholder="dim"),
                ConfigField("reference_table", "关联表名", "text", required=True),
                ConfigField("reference_column", "关联字段", "text", required=True),
            ),
            sql=_sql_not_in_reference,
        ),
        Metric(
            id="table_freshness",
            label="数据新鲜度检查",
            dimension="timeliness",
            level="table",
            needs_column=True,
            fields=_fields(
                ConfigField("interval_value", "时间间隔", "number", required=True, default=1),
                ConfigField(
                    "interval_unit",
                    "时间单位",
                    "select",
                    required=True,
                    default="day",
                    options=(("minute", "分钟"), ("hour", "小时"), ("day", "天")),
                    help="Hive 数据源仅支持按天比较。",
                ),
            ),
            sql=_sql_freshness,
            refine=_refine_freshness,
        ),
    )
}

DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("uniqueness", "唯一性校验", ("column_duplicate",)),
    Dimension("completeness", "完整性校验", ("column_null", "column_blank")),
    Dimension(
        "accuracy",
        "准确性校验",
        ("column_match_regex", "column_length", "column_value_between"),
    ),
    Dimension("standard", "数据标准校验", ("column_not_in_enums",)),
    Dimension("relation", "关联性校验", ("column_not_in_reference",)),
    Dimension("timeliness", "及时性校验", ("table_freshness",)),
)


def metric_label(metric_id: str) -> str:
    metric = METRICS.get(metric_id)
    return metric.label if metric else ""


def dimension_label(dimension_id: str) -> str:
    return next((item.label for item in DIMENSIONS if item.id == dimension_id), "")


def _field_json(item: ConfigField) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "name": item.name,
        "label": item.label,
        "type": item.type,
        "required": item.required,
    }
    if item.default is not None:
        descriptor["default"] = item.default
    if item.placeholder:
        descriptor["placeholder"] = item.placeholder
    if item.help:
        descriptor["help"] = item.help
    if item.options:
        descriptor["options"] = [
            {"value": value, "label": label} for value, label in item.options
        ]
    return descriptor


def catalog() -> dict[str, Any]:
    """The metric tree the rule form renders, in the order of the product screens."""
    return {
        "dimensions": [
            {
                "id": dimension.id,
                "label": dimension.label,
                "metrics": [
                    {
                        "id": metric_id,
                        "label": METRICS[metric_id].label,
                        "level": METRICS[metric_id].level,
                        "fields": [_field_json(item) for item in METRICS[metric_id].fields],
                        "needs_column": METRICS[metric_id].needs_column,
                    }
                    for metric_id in dimension.metrics
                ],
            }
            for dimension in DIMENSIONS
        ]
    }


# ---------------------------------------------------------------------------
# Validation and generation
# ---------------------------------------------------------------------------
def _metric(metric_id: Any) -> Metric:
    metric = METRICS.get(metric_id) if isinstance(metric_id, str) else None
    if metric is None:
        raise MetricError(400, f"不支持的核查类型：{str(metric_id)[:40]}。")
    return metric


def validate_config(metric_id: str, config: Any) -> dict[str, Any]:
    """Normalise a metric's configuration, rejecting anything unsafe or incomplete.

    The ``filter`` expression is only checked for dangerous characters here; it is
    parsed in :func:`build`, where the dialect is known.
    """
    metric = _metric(metric_id)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise MetricError(400, "核查配置必须是对象。")
    unknown = set(config) - {item.name for item in metric.fields}
    if unknown:
        raise MetricError(400, "未知的核查参数：" + "、".join(sorted(unknown)) + "。")
    normalized: dict[str, Any] = {}
    for item in metric.fields:
        value = config.get(item.name, item.default)
        if item.type == "number":
            if value in (None, ""):
                if item.required:
                    raise MetricError(400, f"请填写 {item.label}。")
                normalized[item.name] = None
                continue
            normalized[item.name] = _number(value, item.label)
            continue
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise MetricError(400, f"{item.label} 必须是文本。")
        value = value.strip()
        if not value and isinstance(item.default, str):
            value = item.default
        _check_literal(value, item.label, newlines=item.type == "textarea")
        if item.type == "select" and value:
            if value not in {option[0] for option in item.options}:
                raise MetricError(400, f"{item.label} 的取值无效。")
        if item.required and not value:
            raise MetricError(400, f"请填写 {item.label}。")
        normalized[item.name] = value
    if metric.refine is not None:
        metric.refine(normalized)
    return normalized


def _guard(sql: str, dialect: str) -> str:
    try:
        guard_sql(sql, dialect)
    except ConnectorError as error:
        detail = str(error).rstrip("。")
        raise MetricError(400, f"生成的核查 SQL 未通过安全校验：{detail}。") from error
    return sql


def build(metric_id: str, ctx: MetricContext) -> MetricSql:
    """Generate the three statements of one rule for the context's dialect."""
    metric = _metric(metric_id)
    script = _script(ctx.dialect)
    config = validate_config(metric_id, ctx.config)
    schema = ctx.schema or None
    if schema:
        _identifier(schema, "模式名")
    table = ctx.qualified(schema, _identifier(ctx.table, "表名"))
    column = ""
    if metric.needs_column:
        if not ctx.column:
            raise MetricError(400, f"{metric.label} 需要指定核查字段。")
        column = ctx.quote(_identifier(ctx.column, "核查字段"))
    target = _Target(
        ctx=ctx,
        script=script,
        table=table,
        column=column,
        config=config,
        filter=_check_filter(config.get("filter", ""), ctx.dialect),
    )
    predicate, invalidate_sql = metric.sql(target)
    if not invalidate_sql:
        invalidate_sql = f"SELECT * FROM {table} WHERE {_and_filter(predicate, target.filter)}"
    actual_sql = f"SELECT count(1) AS actual_value FROM (\n{invalidate_sql}\n) t"
    total_sql = f"SELECT count(1) AS checked_count FROM {table}" + (
        f" WHERE ({target.filter})" if target.filter else ""
    )
    for statement in (invalidate_sql, actual_sql, total_sql):
        _guard(statement, ctx.dialect)
    return MetricSql(
        actual_sql=actual_sql,
        total_sql=total_sql,
        invalidate_sql=invalidate_sql,
        predicate=predicate,
        scan_limited=ctx.source_type in SCAN_LIMITED_TYPES,
    )
