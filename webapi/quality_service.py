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

"""Execution engine and cron scheduler for the 数据质量 module.

A rule is executed entirely through the existing read-only connector layer: the
metric catalog turns it into three self-contained SELECT statements, two of them
are run for their single aggregate value, and the verdict is computed here. No
view, temporary table or any other object is created on the target datasource,
and nothing but the two counts leaves it.

Design decisions this file embodies:

* **An execution row is never left in status 0.** Every failure path — an
  unknown datasource, a metric configuration the dialect cannot express, a
  connector timeout, a broken statement — closes the execution with status 2 and
  a Chinese message *before* the error is raised, so 质量执行日志 always shows a
  terminal state.
* **The verdict is one exhaustive matrix.** ``result_formula`` reduces the two
  counts to a single number and ``operator`` compares it with the rule's
  threshold; both are validated, and a rule that examined nothing yields a
  percentage of 0 rather than a division by zero.
* **The score has one definition for all six categories** (upstream datavines
  has a different one per category): ``(checked - errors) / checked * 100``,
  supplied by :func:`webapi.quality_store.quality_score`.
* **Scheduling is misfire-skipping.** The next firing is always computed from
  the moment the scheduler decided to fire, so a slow run or a gateway that was
  down loses the missed slots instead of replaying them, and one schedule never
  has two firings in flight at once.
* **Only the built-in task is dispatchable.** ``bean_name`` / ``method_name``
  are stored and displayed because the screens show them, but any value other
  than ``QualityTask`` / ``run`` is rejected when the schedule is saved.

Labels (核查类型, 规则级别, 状态 …) are attached here rather than in the store,
which deliberately knows nothing about the metric catalog.

The primary agent wires this module in ``webapi/app.py``; see the docstring of
``webapi/quality_api.py`` for the exact lines.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import replace
import time
from operator import eq, ge, gt, le, lt, ne
from typing import Any, Callable

from croniter import croniter

from .observability import observe
from .tasks import TaskError, TaskRegistry, json_detail, summarize

from .connectors import ConnectorError
from .datasources import DataSourceError, DataSourceRegistry
from .quality_metrics import (
    METRICS,
    MetricError,
    MetricSql,
    build,
    catalog,
    dimension_label,
    make_context,
    metric_label,
    reference_aggregate_sql,
    validate_config,
)
from .quality_store import (
    DIMENSION_LABELS,
    LEVEL_LABELS,
    MAX_BACKFILL,
    MAX_RETRY_DELAY,
    MAX_RETRY_LIMIT,
    MISFIRE_LABELS,
    TASK_RUN_STATUS_LABELS,
    QualityStore,
    QualityStoreError,
    plain_number,
    quality_score,
)

OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "eq": eq,
    "ne": ne,
    "lt": lt,
    "lte": le,
    "gt": gt,
    "gte": ge,
}
OPERATOR_LABELS = {
    "eq": "等于",
    "ne": "不等于",
    "lt": "小于",
    "lte": "小于等于",
    "gt": "大于",
    "gte": "大于等于",
}
EXPECTED_TYPES = {"fix_value": "固定值", "table_total_rows": "表总行数"}
RESULT_FORMULAS = {"actual": "实际值", "percentage": "百分比"}
RULE_STATE_LABELS = {1: "启用", 0: "禁用"}
SCHEDULE_STATE_LABELS = {1: "运行", 0: "停止"}
EXECUTION_STATUS_LABELS = {0: "运行中", 1: "成功", 2: "失败"}
RESULT_STATE_LABELS = {1: "成功", 2: "失败"}
TRIGGER_LABELS = {"manual": "手动", "schedule": "调度", "retry": "重试", "backfill": "补跑"}
SUPPORTED_BEAN = "QualityTask"
SUPPORTED_METHOD = "run"
SCAN_LIMIT_NOTE = "「该数据源按有限快照扫描（数据湖表最多 50 万行，文档 / 消息按快照行数），统计可能不完整。」"
DEFAULT_SAMPLE_ROWS = 20
MAX_SAMPLE_ROWS = 200
MAX_CRON_LENGTH = 120
CRON_FIELDS = (5, 6)
SCHEDULER_INTERVAL = 5.0
MAX_BATCH_TARGETS = 200
#: Seeded once into dv_rule_template; users edit, delete and add their own.
BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"name": "主键唯一", "description": "核查字段不允许出现重复值。", "metric": "column_duplicate", "level": "high", "threshold": 0},
    {"name": "字段非空", "description": "核查字段不允许为 NULL。", "metric": "column_null", "level": "high", "threshold": 0},
    {"name": "字段非空白", "description": "核查字段不允许为空字符串或仅有空白。", "metric": "column_blank", "threshold": 0},
    {"name": "空值率不超过 5%", "description": "允许少量空值，按百分比比较。", "metric": "column_null", "result_formula": "percentage", "operator": "lte", "threshold": 5},
    {"name": "手机号格式", "description": "中国大陆 11 位手机号。", "metric": "column_match_regex", "config": {"regexp": "^1[3-9][0-9]{9}$"}, "threshold": 0},
    {"name": "邮箱格式", "description": "形如 name@domain 的邮箱地址。", "metric": "column_match_regex", "config": {"regexp": "^[^@ ]+@[^@ ]+[.][^@ ]+$"}, "threshold": 0},
    {"name": "金额非负", "description": "数值字段不允许小于 0。", "metric": "column_value_between", "config": {"min": 0}, "threshold": 0},
    {"name": "当日已更新", "description": "表内最新时间字段距今不超过 1 天。", "metric": "table_freshness", "config": {"interval_value": 1, "interval_unit": "day"}, "threshold": 0},
    {"name": "行数与参照库一致", "description": "下发时填写参照数据源与参照表；差异行数不超过阈值。", "metric": "cross_source_compare", "config": {"aggregate": "count"}, "threshold": 0},
    {"name": "自定义 SQL 无异常行", "description": "把 WHERE 条件改成异常记录的判断条件，返回的每一行都算不合规。", "metric": "custom_sql", "config": {"mode": "rows", "sql": "SELECT * FROM ${table} WHERE 1 = 0"}, "threshold": 0},
)
MAX_MESSAGE = 2000


class QualityError(ValueError):
    """A user-facing engine error; the message is Chinese and safe to display."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# The failures a rule can legitimately produce: each already carries a Chinese
# message, and each becomes a failed execution rather than a gateway error.
ENGINE_ERRORS = (
    QualityError,
    QualityStoreError,
    MetricError,
    DataSourceError,
    ConnectorError,
)


# ---------------------------------------------------------------------------
# Small conversions shared by the engine and the scheduler
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, label: str) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise QualityError(400, f"{label}必须是数字。")
    try:
        return float(value)
    except ValueError as error:
        raise QualityError(400, f"{label}必须是数字。") from error


def _reason(error: BaseException) -> str:
    """A one-line, already-Chinese reason suitable for an execution message."""
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return text[:600]


def _local(moment: dt.datetime) -> dt.datetime:
    """Compare everything in naive local time; the store returns aware values."""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return _local(value)
    if isinstance(value, str) and value.strip():
        try:
            return _local(dt.datetime.fromisoformat(value.strip()))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Cron: Quartz 6-field (seconds first) plus 5-field Unix
# ---------------------------------------------------------------------------
def cron_fields(expression: Any) -> list[str]:
    """Split a cron expression, translating Quartz's ``?`` into ``*``."""
    text = " ".join(str(expression or "").split())
    if not text:
        raise QualityError(400, "cron 表达式不能为空。")
    if len(text) > MAX_CRON_LENGTH:
        raise QualityError(400, f"cron 表达式不能超过 {MAX_CRON_LENGTH} 个字符。")
    fields = text.split(" ")
    if len(fields) not in CRON_FIELDS:
        raise QualityError(
            400, "cron 表达式只支持 5 位（分 时 日 月 周）或 6 位（秒 分 时 日 月 周）。"
        )
    return ["*" if field == "?" else field for field in fields]


def next_fire_time(expression: Any, base: dt.datetime | None = None) -> dt.datetime:
    """The next local firing after ``base``; raises on an expression croniter rejects."""
    fields = cron_fields(expression)
    moment = _local(base) if base is not None else dt.datetime.now()
    try:
        # croniter reads a 6-field expression as "seconds last" unless told that
        # the Quartz order — seconds first — is meant.
        cursor = croniter(" ".join(fields), moment, second_at_beginning=len(fields) == 6)
        return cursor.get_next(dt.datetime)
    except (ValueError, KeyError, OverflowError) as error:  # CroniterError is a ValueError
        raise QualityError(400, f"cron 表达式无效：{_reason(error)}。") from error


# ---------------------------------------------------------------------------
# Row decoration: the labels §6 of the contract promises the frontend
# ---------------------------------------------------------------------------
def _dimension_label(value: Any) -> str:
    dimension = _text(value)
    return dimension_label(dimension) or DIMENSION_LABELS.get(dimension, dimension)


def _as_int(value: Any) -> int:
    """Coerce an identifier or a state code; -1 marks a value that is neither."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def decorate_rule(row: dict[str, Any]) -> dict[str, Any]:
    rule = dict(row or {})
    rule["metric_label"] = metric_label(_text(rule.get("metric")))
    rule["dimension_label"] = _dimension_label(rule.get("dimension"))
    rule["level_label"] = LEVEL_LABELS.get(_text(rule.get("level")), "")
    rule["state_label"] = RULE_STATE_LABELS.get(_as_int(rule.get("state")), "")
    return rule


def decorate_schedule(row: dict[str, Any], registry: TaskRegistry | None = None) -> dict[str, Any]:
    schedule = dict(row or {})
    schedule["state_label"] = SCHEDULE_STATE_LABELS.get(_as_int(schedule.get("state")), "")
    schedule["task"] = f"{schedule.get('bean_name') or SUPPORTED_BEAN}.{schedule.get('method_name') or SUPPORTED_METHOD}"
    schedule["task_label"] = (
        registry.label_of(schedule.get("bean_name"), schedule.get("method_name")) if registry else schedule["task"]
    )
    schedule["misfire_policy_label"] = MISFIRE_LABELS.get(_text(schedule.get("misfire_policy")) or "skip", "")
    schedule["last_status_label"] = TASK_RUN_STATUS_LABELS.get(_text(schedule.get("last_status")), "")
    return schedule


def decorate_template(row: dict[str, Any]) -> dict[str, Any]:
    template = dict(row or {})
    metric = METRICS.get(_text(template.get("metric")))
    template["metric_label"] = metric.label if metric else _text(template.get("metric"))
    template["dimension_label"] = _dimension_label(template.get("dimension"))
    template["level_label"] = LEVEL_LABELS.get(_text(template.get("level")), "")
    template["needs_column"] = bool(metric.needs_column) if metric else False
    template["builtin"] = _as_int(template.get("builtin")) == 1
    return template


def decorate_task_run(row: dict[str, Any]) -> dict[str, Any]:
    run = dict(row or {})
    run["trigger_label"] = TRIGGER_LABELS.get(_text(run.get("trigger_type")), "")
    run["status_label"] = TASK_RUN_STATUS_LABELS.get(_text(run.get("status")), "")
    return run


def decorate_result(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row or {})
    result["metric_label"] = metric_label(_text(result.get("metric_name")))
    result["dimension_label"] = _dimension_label(result.get("metric_dimension"))
    result["level_label"] = LEVEL_LABELS.get(_text(result.get("rule_level")), "")
    result["state_label"] = RESULT_STATE_LABELS.get(_as_int(result.get("state")), "")
    return result


def decorate_execution(row: dict[str, Any]) -> dict[str, Any]:
    execution = dict(row or {})
    execution["trigger_label"] = TRIGGER_LABELS.get(_text(execution.get("trigger_type")), "")
    execution["status_label"] = EXECUTION_STATUS_LABELS.get(_as_int(execution.get("status")), "")
    if isinstance(execution.get("results"), list):
        execution["results"] = [decorate_result(item) for item in execution["results"]]
    return execution


def decorate_page(page: dict[str, Any], decorate: Callable[[dict], dict]) -> dict[str, Any]:
    listing = dict(page or {})
    listing["items"] = [decorate(item) for item in listing.get("items") or ()]
    return listing


class QualityService:
    """Runs rules: SQL generation, execution through a connector, verdict, history."""

    def __init__(self, store: QualityStore, sources: DataSourceRegistry, registry: TaskRegistry | None = None):
        self.store = store
        self.sources = sources
        #: Dispatch table of the 调度 screen; ``None`` means only QualityTask.run exists.
        self.registry = registry

    # ----- form metadata --------------------------------------------------------
    def options(self) -> dict[str, Any]:
        """Everything 核查规则编辑 needs to render its form."""
        data = catalog()
        data["operators"] = [
            {"value": key, "label": OPERATOR_LABELS[key]} for key in OPERATORS
        ]
        data["expected_types"] = [
            {"value": key, "label": label} for key, label in EXPECTED_TYPES.items()
        ]
        data["result_formulas"] = [
            {"value": key, "label": label} for key, label in RESULT_FORMULAS.items()
        ]
        data["levels"] = [{"value": key, "label": label} for key, label in LEVEL_LABELS.items()]
        data["states"] = [
            {"value": state, "label": label} for state, label in RULE_STATE_LABELS.items()
        ]
        data["scheduler"] = {"bean_name": SUPPORTED_BEAN, "method_name": SUPPORTED_METHOD}
        data["tasks"] = self.registry.options() if self.registry else []
        data["misfire_policies"] = [{"value": key, "label": label} for key, label in MISFIRE_LABELS.items()]
        return data

    # ----- saving ---------------------------------------------------------------
    def prepare_rule(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a rule and prove it can be executed before it is stored.

        Generating the SQL here costs nothing — the metric catalog is pure and
        the connector is only constructed, never connected — and it means a rule
        that the target's dialect cannot express is refused at save time instead
        of failing on every later run.
        """
        rule = dict(data or {})
        metric_id = _text(rule.get("metric"))
        metric = METRICS.get(metric_id)
        if metric is None:
            raise QualityError(400, f"不支持的核查类型：{metric_id[:40] or '（空）'}。")
        dimension = _text(rule.get("dimension")) or metric.dimension
        if dimension not in DIMENSION_LABELS:
            raise QualityError(400, f"不支持的规则分类：{dimension[:40]}。")
        if dimension != metric.dimension:
            raise QualityError(400, "核查类型与规则分类不一致。")
        level = _text(rule.get("level")) or "medium"
        if level not in LEVEL_LABELS:
            raise QualityError(400, "规则级别只能是 high、medium 或 low。")
        operator = _text(rule.get("operator")) or "lte"
        if operator not in OPERATORS:
            raise QualityError(400, "比较方式只能是 " + "、".join(OPERATORS) + "。")
        expected_type = _text(rule.get("expected_type")) or "fix_value"
        if expected_type not in EXPECTED_TYPES:
            raise QualityError(400, "期望值类型只能是 fix_value 或 table_total_rows。")
        formula = _text(rule.get("result_formula")) or "actual"
        if formula not in RESULT_FORMULAS:
            raise QualityError(400, "结果计算方式只能是 actual 或 percentage。")
        if metric.needs_column and not _text(rule.get("column_name")):
            raise QualityError(400, f"{metric.label} 需要指定核查字段。")
        aggregate = _text((rule.get("config") or {}).get("aggregate")) or "count"
        if metric_id == "cross_source_compare" and aggregate != "count" and not _text(rule.get("column_name")):
            raise QualityError(400, "按求和、平均、最小或最大值跨库比对时需要指定核查字段。")
        source_id = _text(rule.get("datasource_id"))
        if not source_id:
            raise QualityError(400, "请选择数据源。")
        rule.update(
            {
                "metric": metric_id,
                "dimension": dimension,
                "level": level,
                "operator": operator,
                "expected_type": expected_type,
                "result_formula": formula,
                "threshold": _number(rule.get("threshold"), "阈值"),
                "config": validate_config(metric_id, rule.get("config")),
                "datasource_id": source_id,
                # The stored name follows the datasource record, so a renamed
                # source is not shown under its old name on the 规则 screen.
                "datasource_name": _text(self.sources.record(source_id).get("name")),
            }
        )
        self._build(rule, self.sources.connector(source_id))
        return rule

    def prepare_schedule(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a schedule: a registered task, a parseable cron, sane policies."""
        schedule = dict(data or {})
        bean = _text(schedule.get("bean_name")) or SUPPORTED_BEAN
        method = _text(schedule.get("method_name")) or SUPPORTED_METHOD
        if self.registry is not None:
            task = self.registry.get(bean, method)
            params = self.registry.check_params(task, schedule.get("method_params"))
        elif bean != SUPPORTED_BEAN or method != SUPPORTED_METHOD:
            raise QualityError(
                400, f"调度任务仅支持内置的 {SUPPORTED_BEAN}.{SUPPORTED_METHOD}。"
            )
        else:
            params = _text(schedule.get("method_params"))
        fields = cron_fields(schedule.get("cron_expression"))
        next_fire_time(" ".join(fields))
        policy = _text(schedule.get("misfire_policy")) or "skip"
        if policy not in MISFIRE_LABELS:
            raise QualityError(400, "补跑策略只能是 skip（跳过）、once（补跑一次）或 all（逐次补跑）。")
        schedule.update(
            {
                "bean_name": bean,
                "method_name": method,
                "method_params": params,
                "cron_expression": " ".join(str(schedule.get("cron_expression") or "").split()),
                "retry_limit": _int_between(schedule.get("retry_limit"), "失败重试次数", 0, 0, MAX_RETRY_LIMIT),
                "retry_delay_seconds": _int_between(schedule.get("retry_delay_seconds"), "重试间隔", 60, 1, MAX_RETRY_DELAY),
                "misfire_policy": policy,
                "max_backfill": _int_between(schedule.get("max_backfill"), "补跑上限", 10, 1, MAX_BACKFILL),
            }
        )
        return schedule

    # ----- templates and batches --------------------------------------------------
    def prepare_template(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a template: a rule definition without a target; required
        parameters may stay empty until the template is applied."""
        template = dict(data or {})
        metric_id = _text(template.get("metric"))
        metric = METRICS.get(metric_id)
        if metric is None:
            raise QualityError(400, f"不支持的核查类型：{metric_id[:40] or '（空）'}。")
        level = _text(template.get("level")) or "medium"
        if level not in LEVEL_LABELS:
            raise QualityError(400, "规则级别只能是 high、medium 或 low。")
        operator = _text(template.get("operator")) or "lte"
        if operator not in OPERATORS:
            raise QualityError(400, "比较方式只能是 " + "、".join(OPERATORS) + "。")
        expected_type = _text(template.get("expected_type")) or "fix_value"
        if expected_type not in EXPECTED_TYPES:
            raise QualityError(400, "期望值类型只能是 fix_value 或 table_total_rows。")
        formula = _text(template.get("result_formula")) or "actual"
        if formula not in RESULT_FORMULAS:
            raise QualityError(400, "结果计算方式只能是 actual 或 percentage。")
        if not _text(template.get("name")):
            raise QualityError(400, "请填写模板名称。")
        template.update(
            {
                "name": _text(template.get("name")),
                "description": _text(template.get("description")),
                "metric": metric_id,
                "dimension": metric.dimension,
                "level": level,
                "operator": operator,
                "expected_type": expected_type,
                "result_formula": formula,
                "threshold": _number(template.get("threshold"), "阈值"),
                "config": validate_config(metric_id, template.get("config"), partial=True),
            }
        )
        return template

    def template_from_rule(self, rule: dict[str, Any], name: str | None = None, description: str | None = None) -> dict[str, Any]:
        """A template carrying a rule's definition, with the target left out."""
        return self.prepare_template(
            {
                "name": _text(name) or _text(rule.get("name")),
                "description": _text(description) or f"由规则「{_text(rule.get('name'))}」保存",
                "metric": rule.get("metric"),
                "level": rule.get("level"),
                "config": rule.get("config") or {},
                "expected_type": rule.get("expected_type"),
                "result_formula": rule.get("result_formula"),
                "operator": rule.get("operator"),
                "threshold": rule.get("threshold"),
            }
        )

    def ensure_templates(self) -> None:
        """Seed the built-in templates once; a database that is down is left to health()."""
        try:
            if self.store.count_templates() > 0:
                return
            for item in BUILTIN_TEMPLATES:
                self.store.create_template({**self.prepare_template(item), "builtin": 1})
        except (QualityStoreError, QualityError, MetricError):
            return

    def batch_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one definition to many targets; every target is validated on its own."""
        data = dict(payload or {})
        template = self.store.get_template(data["template_id"]) if data.get("template_id") else None
        base = template or data.get("rule") or {}
        if not base:
            raise QualityError(400, "请选择规则模板或提供规则定义。")
        targets = [dict(item) for item in data.get("targets") or [] if isinstance(item, dict)]
        if not targets:
            raise QualityError(400, "请至少选择一个目标表。")
        if len(targets) > MAX_BATCH_TARGETS:
            raise QualityError(400, f"一次最多下发 {MAX_BATCH_TARGETS} 个目标。")
        overrides = {key: value for key, value in (data.get("config") or {}).items() if value not in (None, "")}
        prefix = _text(data.get("name_prefix")) or _text(base.get("name")) or METRICS[_text(base.get("metric"))].label
        created: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for target in targets:
            table = _text(target.get("table_name"))
            column = _text(target.get("column_name")) or None
            name = f"{prefix} · {table}" + (f".{column}" if column else "")
            rule = {
                "name": name[:200],
                "metric": base.get("metric"),
                "dimension": base.get("dimension"),
                "level": base.get("level"),
                "config": {**(base.get("config") or {}), **overrides},
                "expected_type": base.get("expected_type"),
                "result_formula": base.get("result_formula"),
                "operator": base.get("operator"),
                "threshold": base.get("threshold"),
                "datasource_id": target.get("datasource_id"),
                "schema_name": _text(target.get("schema_name")) or None,
                "table_name": table,
                "column_name": column,
                "state": 1 if data.get("state") is None else data.get("state"),
                "comment": _text(data.get("comment")) or (f"由模板「{template['name']}」批量下发" if template else "批量下发"),
            }
            try:
                created.append(decorate_rule(self.store.create_rule(self.prepare_rule(rule))))
            except (*ENGINE_ERRORS, ValueError) as error:
                errors.append(
                    {
                        "datasource_id": target.get("datasource_id"),
                        "schema_name": rule["schema_name"],
                        "table_name": table,
                        "column_name": column,
                        "message": _reason(error),
                    }
                )
        return {"total": len(targets), "created": created, "errors": errors}

    # ----- SQL ------------------------------------------------------------------
    def _build(self, rule: dict[str, Any], connector: Any) -> MetricSql:
        context = make_context(
            connector.dialect,
            rule.get("schema_name") or None,
            _text(rule.get("table_name")),
            rule.get("column_name") or None,
            rule.get("config") or {},
            source_type=getattr(connector, "type_id", ""),
        )
        built = build(_text(rule.get("metric")), context)
        if built.compare == "aggregate":
            # The reference side runs on another engine, so it is generated in that
            # engine's dialect from the reference connector, never from the source's.
            config = rule.get("config") or {}
            reference_id = _text(config.get("reference_datasource"))
            if not reference_id:
                raise QualityError(400, "跨库比对需要选择参照数据源。")
            record = self.sources.record(reference_id)
            reference = self.sources.connector(reference_id)
            built = replace(
                built,
                reference_datasource=reference_id,
                reference_label=_text(record.get("name")) or reference_id,
                reference_sql=reference_aggregate_sql(
                    reference.dialect,
                    config.get("reference_schema"),
                    config.get("reference_table"),
                    config.get("reference_column"),
                    config.get("aggregate") or "count",
                    config.get("reference_filter") or "",
                ),
            )
        return built

    def preview(self, rule_id: Any) -> dict[str, Any]:
        """The three statements of a stored rule, without executing anything."""
        rule = self.store.get_rule(rule_id)
        connector = self.sources.connector(_text(rule.get("datasource_id")))
        built = self._build(rule, connector)
        return {
            "rule_id": rule.get("id"),
            "rule_name": rule.get("name") or "",
            "dialect": connector.dialect,
            "actual_sql": built.actual_sql,
            "total_sql": built.total_sql,
            "invalidate_sql": built.invalidate_sql,
            "predicate": built.predicate,
            "scan_limited": built.scan_limited,
            "compare": built.compare,
            "reference_datasource": built.reference_datasource,
            "reference_label": built.reference_label,
            "reference_sql": built.reference_sql,
        }

    def sample_failures(self, rule_id: Any, limit: Any = DEFAULT_SAMPLE_ROWS) -> dict[str, Any]:
        """Read-only sample of non-conforming rows; nothing is ever written back."""
        rule = self.store.get_rule(rule_id)
        connector = self.sources.connector(_text(rule.get("datasource_id")))
        built = self._build(rule, connector)
        rows = connector.query(built.invalidate_sql, _sample_limit(limit))
        return {
            "rule_id": rule.get("id"),
            "rule_name": rule.get("name") or "",
            "sql": built.invalidate_sql,
            "columns": rows.get("columns") or [],
            "rows": rows.get("rows") or [],
            "truncated": bool(rows.get("truncated")),
            "elapsed_ms": rows.get("elapsed_ms"),
            "warning": SCAN_LIMIT_NOTE if built.scan_limited else "",
        }

    # ----- execution ------------------------------------------------------------
    @staticmethod
    def _scalar(connector: Any, sql: str, label: str) -> float:
        """Read the single aggregate value of one generated statement."""
        with observe(label, "quality"):
            answer = connector.query(sql, 1)
        rows = answer.get("rows") or []
        value = rows[0][0] if rows and rows[0] else 0
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise QualityError(502, f"{label}的查询结果不是数字。")
        try:
            return float(value)
        except ValueError as error:
            raise QualityError(502, f"{label}的查询结果不是数字。") from error

    def _verdict(self, rule: dict[str, Any], checked: float, actual: float) -> dict[str, Any]:
        """Reduce the two counts to a number, compare it, and score the rule."""
        expected_type = _text(rule.get("expected_type")) or "fix_value"
        formula = _text(rule.get("result_formula")) or "actual"
        operator = _text(rule.get("operator")) or "lte"
        if expected_type not in EXPECTED_TYPES:
            raise QualityError(400, "期望值类型只能是 fix_value 或 table_total_rows。")
        if formula not in RESULT_FORMULAS:
            raise QualityError(400, "结果计算方式只能是 actual 或 percentage。")
        if operator not in OPERATORS:
            raise QualityError(400, "比较方式只能是 " + "、".join(OPERATORS) + "。")
        threshold = _number(rule.get("threshold"), "阈值")
        expected = threshold if expected_type == "fix_value" else checked
        # A rule that examined no row has no ratio; 0 keeps the verdict defined
        # instead of dividing by zero.
        value = actual if formula == "actual" else (actual / checked * 100 if checked > 0 else 0.0)
        return {
            # The compared number and the recorded one are different things.
            # 不合规数量 is always the raw count of non-conforming rows, as in
            # DataVines; the formula only decides what is compared with the
            # threshold, so a percentage rule does not turn the count column
            # into a percentage that every screen would then mislabel.
            "actual_value": plain_number(actual),
            "value": plain_number(value),
            "expected_value": plain_number(expected),
            "threshold": plain_number(threshold),
            "score": quality_score(checked, actual),
            "state": 1 if OPERATORS[operator](value, threshold) else 2,
        }

    def _abort(self, execution_id: int, started: float, message: str) -> None:
        """Close a failed execution; the caller raises the original error next."""
        try:
            self.store.finish_execution(execution_id, 2, _elapsed(started), message[:MAX_MESSAGE])
        except QualityStoreError:
            # The metadata database has gone away mid-run. The failure that is
            # worth reporting is the one the caller is about to raise, and this
            # one is visible again on the next 质量健康 check.
            return

    def run_saved(
        self, rule_id: Any, *, trigger: str = "manual", schedule_id: Any = None
    ) -> dict[str, Any]:
        """Load a rule by id and run it once."""
        return self.run_rule(self.store.get_rule(rule_id), trigger=trigger, schedule_id=schedule_id)

    def run_rule(
        self, rule: dict[str, Any], *, trigger: str = "manual", schedule_id: Any = None
    ) -> dict[str, Any]:
        """Execute one rule; the execution row always reaches a terminal status."""
        rule = dict(rule or {})
        name = _text(rule.get("name"))
        execution_id = self.store.start_execution(
            rule_id=rule.get("id"),
            rule_name=name,
            trigger_type=trigger if trigger in TRIGGER_LABELS else "manual",
            schedule_id=schedule_id,
            message="核查执行中。",
        )
        started = time.monotonic()
        try:
            connector = self.sources.connector(_text(rule.get("datasource_id")))
            built = self._build(rule, connector)
            summary = ""
            if built.compare == "aggregate":
                source_value = self._scalar(connector, built.actual_sql, "本库聚合值")
                reference_value = self._scalar(
                    self.sources.connector(built.reference_datasource), built.reference_sql, "参照库聚合值"
                )
                checked, actual = reference_value, abs(source_value - reference_value)
                summary = (
                    f"跨库比对完成：本库 {plain_number(source_value)}，"
                    f"参照库（{built.reference_label}）{plain_number(reference_value)}，差异 {plain_number(actual)}"
                )
            else:
                checked = self._scalar(connector, built.total_sql, "核查数量")
                actual = self._scalar(connector, built.actual_sql, "不合规数量")
            verdict = self._verdict(rule, checked, actual)
        except Exception as error:  # noqa: BLE001 - the row must never stay 运行中
            message = f"规则「{name}」执行失败：{_reason(error)}"
            self._abort(execution_id, started, message)
            if isinstance(error, ENGINE_ERRORS):
                raise QualityError(_status(error), message) from error
            # Not a user-facing failure: close the row, then let the real error
            # travel on untouched rather than disguise a defect as a bad rule.
            raise

        message = (
            f"{summary}，得分 {verdict['score']}。"
            if summary
            else f"核查完成：核查 {plain_number(checked)} 行，不合规 {plain_number(actual)} 行，得分 {verdict['score']}。"
        )
        if built.scan_limited:
            message += SCAN_LIMIT_NOTE
        try:
            result = self.store.record_result(
                execution_id,
                {
                    "rule_id": rule.get("id"),
                    "rule_name": name,
                    "metric_name": _text(rule.get("metric")),
                    "metric_dimension": _text(rule.get("dimension")),
                    "datasource_id": _text(rule.get("datasource_id")),
                    "datasource_name": _text(rule.get("datasource_name")),
                    "database_name": rule.get("schema_name") or connector.default_schema() or "",
                    "table_name": _text(rule.get("table_name")),
                    "column_name": rule.get("column_name") or None,
                    "rule_level": _text(rule.get("level")) or "medium",
                    "checked_count": int(checked),
                    "actual_value": verdict["actual_value"],
                    "expected_value": verdict["expected_value"],
                    "expected_type": _text(rule.get("expected_type")) or "fix_value",
                    "result_formula": _text(rule.get("result_formula")) or "actual",
                    "operator": _text(rule.get("operator")) or "lte",
                    "threshold": verdict["threshold"],
                    "score": verdict["score"],
                    "state": verdict["state"],
                    "invalidate_sql": built.invalidate_sql,
                },
            )
            execution = self.store.finish_execution(
                execution_id, 1, _elapsed(started), message[:MAX_MESSAGE]
            )
        except Exception as error:  # noqa: BLE001 - the row must never stay 运行中
            failure = f"规则「{name}」的核查结果写入失败：{_reason(error)}"
            self._abort(execution_id, started, failure)
            if isinstance(error, ENGINE_ERRORS):
                raise QualityError(_status(error), failure) from error
            raise
        return {
            "execution": decorate_execution(execution),
            "result": decorate_result(result),
        }

    def run_enabled(self, *, trigger: str = "manual", schedule_id: Any = None) -> dict[str, Any]:
        """Run every enabled rule; one failing rule never stops the others."""
        rules = self.store.enabled_rules() or []
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        passed = 0
        failed = 0
        for rule in rules:
            try:
                outcome = self.run_rule(rule, trigger=trigger, schedule_id=schedule_id)
            except ENGINE_ERRORS as error:
                errors.append(
                    {
                        "rule_id": rule.get("id"),
                        "rule_name": rule.get("name") or "",
                        "message": _reason(error),
                    }
                )
                continue
            items.append(outcome["result"])
            if _as_int(outcome["result"].get("state")) == 1:
                passed += 1
            else:
                failed += 1
        return {
            "total": len(rules),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "items": items,
        }

    # ----- analysis -------------------------------------------------------------
    def statistics(self, name: str | None = None) -> dict[str, Any]:
        """质量统计分析: the sidebar counts plus the latest result of every rule."""
        counts = self.store.dimension_error_counts()
        return {
            "tree": [
                {
                    "dimension": dimension,
                    "dimension_label": label,
                    "error_count": counts.get(dimension, 0),
                }
                for dimension, label in DIMENSION_LABELS.items()
            ],
            "items": [decorate_result(row) for row in self.store.latest_results(name) or ()],
        }


def _elapsed(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))


def _status(error: BaseException) -> int:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else 400


def _int_between(value: Any, label: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise QualityError(400, f"{label}必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise QualityError(400, f"{label}必须是整数。") from error
    if number < minimum or number > maximum:
        raise QualityError(400, f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return number


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def missed_slots(expression: Any, start: dt.datetime, end: dt.datetime, limit: int) -> list[dt.datetime]:
    """Every cron firing from ``start`` (inclusive) up to ``end``, at most ``limit``."""
    slots: list[dt.datetime] = []
    cursor = start - dt.timedelta(seconds=1)
    while len(slots) < max(1, limit):
        slot = next_fire_time(expression, cursor)
        if slot > end:
            break
        slots.append(slot)
        cursor = slot
    return slots


def _sample_limit(limit: Any) -> int:
    if limit is None or limit == "":
        return DEFAULT_SAMPLE_ROWS
    try:
        value = int(limit)
    except (TypeError, ValueError) as error:
        raise QualityError(400, "采样条数必须是整数。") from error
    return max(1, min(value, MAX_SAMPLE_ROWS))


class QualityScheduler:
    """In-process cron scheduler for 质量调度管理 and every registered task.

    One daemon thread polls the schedules in state 运行 and fires those whose
    cron time has arrived, dispatching each through the task registry. The loop
    body is fully guarded: a schedule with an unparseable cron is stopped and
    reported through an execution row instead of killing the thread, and a
    schedule that is already firing is skipped so two runs of the same task can
    never overlap. A failed run is retried up to the schedule's ``retry_limit``
    after ``retry_delay_seconds``; firings missed while the gateway was down
    follow its ``misfire_policy``: skipped (the default, planned again from the
    moment of the decision), replayed once, or replayed slot by slot up to
    ``max_backfill``. Every attempt leaves a row in ``dv_task_run``.
    """

    def __init__(
        self,
        store: QualityStore,
        service: QualityService,
        interval: float = SCHEDULER_INTERVAL,
        registry: TaskRegistry | None = None,
    ):
        self.store = store
        self.service = service
        self.interval = max(0.1, float(interval))
        self.registry = registry
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._planned: dict[int, dt.datetime] = {}
        self._firing: set[int] = set()
        self._retries: dict[int, tuple[dt.datetime, int]] = {}
        self._error = ""

    # ----- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        """Start the daemon thread; calling it twice is harmless."""
        if self.running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lattice-quality-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the loop to finish and wait for the thread to end."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def tasks(self) -> TaskRegistry:
        """The dispatch table; without one, only the built-in quality job exists."""
        registry = self.registry or getattr(self.service, "registry", None)
        if registry is None:
            registry = TaskRegistry()
            registry.add(
                SUPPORTED_BEAN,
                SUPPORTED_METHOD,
                "质量核查全量执行",
                lambda params, context: self.service.run_enabled(
                    trigger=str(context.get("trigger") or "schedule"), schedule_id=context.get("schedule_id")
                ),
                description="执行全部启用的核查规则。",
                category="数据质量",
            )
            self.registry = registry
        return registry

    def status(self) -> dict[str, Any]:
        """What 质量健康 shows: the thread's state and the jobs it is watching."""
        jobs: list[dict[str, Any]] = []
        error = self._error
        try:
            schedules = self.store.running_schedules() or []
        except QualityStoreError as failure:
            schedules = []
            error = _reason(failure)
        registry = self.tasks()
        for schedule in schedules:
            identifier = _as_int(schedule.get("id"))
            planned = self._planned.get(identifier)
            retry = self._retries.get(identifier)
            jobs.append(
                {
                    "id": schedule.get("id"),
                    "name": schedule.get("name") or "",
                    "task": f"{schedule.get('bean_name')}.{schedule.get('method_name')}",
                    "task_label": registry.label_of(schedule.get("bean_name"), schedule.get("method_name")),
                    "next_fire_time": (
                        planned.isoformat() if planned else schedule.get("next_fire_time")
                    ),
                    "retry_due": retry[0].isoformat() if retry else None,
                    "retry_attempt": retry[1] if retry else None,
                    "last_status": schedule.get("last_status") or "",
                    "last_message": schedule.get("last_message") or "",
                    "firing": identifier in self._firing,
                }
            )
        return {"running": self.running(), "jobs": jobs, "last_error": error, "tasks": registry.keys()}

    # ----- loop -----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def tick(self, now: dt.datetime | None = None) -> list[int]:
        """One scheduling pass. Never raises: the thread must outlive any error."""
        moment = _local(now) if now is not None else dt.datetime.now()
        try:
            schedules = self.store.running_schedules() or []
        except QualityStoreError as error:
            self._error = _reason(error)
            return []
        fired: list[int] = []
        live: set[int] = set()
        for schedule in schedules:
            identifier = _as_int(schedule.get("id"))
            if identifier <= 0:
                continue
            live.add(identifier)
            try:
                if self._due(schedule, identifier, moment):
                    fired.append(identifier)
            except Exception as error:  # noqa: BLE001 - one bad job must not stop the rest
                self._error = f"调度任务「{schedule.get('name') or identifier}」执行失败：{_reason(error)}"
        # A schedule that was stopped is re-planned from its cron when it starts again.
        for identifier in set(self._planned) - live:
            self._planned.pop(identifier, None)
        for identifier in set(self._retries) - live:
            self._retries.pop(identifier, None)
        return fired

    def forget(self, schedule_id: Any) -> None:
        """Drop a cached firing plan so the stored one is used again.

        Editing a running task's cron rewrites ``next_fire_time`` in the store,
        but this thread would keep comparing against the plan it made for the
        previous expression until the task fired once on the old cadence.
        """
        identifier = _as_int(schedule_id)
        if identifier is not None:
            with self._lock:
                self._planned.pop(identifier, None)
                self._retries.pop(identifier, None)

    def fire(self, schedule_id: Any) -> dict[str, Any]:
        """Fire one schedule immediately, as 立即执行 on the 调度 screen does.

        A manual run is never retried automatically: the person who pressed the
        button is watching and gets the failure back as an error.
        """
        schedule = self.store.get_schedule(schedule_id)
        identifier = _as_int(schedule.get("id"))
        moment = dt.datetime.now()
        planned = self._planned.get(identifier) or _parse_time(schedule.get("next_fire_time"))
        with self._lock:
            if identifier in self._firing:
                raise QualityError(409, "该调度任务正在执行中，请稍后再试。")
            self._firing.add(identifier)
        try:
            result = self._run_task(
                schedule, identifier, moment, attempt=1, trigger="manual", planned_time=moment, retry_allowed=False
            )
            self._remember(identifier, moment, planned)
        finally:
            with self._lock:
                self._firing.discard(identifier)
        if not result["ok"]:
            raise QualityError(result["status_code"], result["message"])
        outcome = result["outcome"]
        return {**(outcome if isinstance(outcome, dict) else {}), **result}

    # ----- one schedule ---------------------------------------------------------
    def _due(self, schedule: dict[str, Any], identifier: int, moment: dt.datetime) -> bool:
        retry = self._retries.get(identifier)
        if retry is not None:
            due, attempt = retry
            if moment < due:
                return False
            return self._fire(schedule, identifier, moment, attempt=attempt, trigger="retry", planned_time=due)
        planned = self._planned.get(identifier)
        if planned is None:
            planned = self._plan(schedule, identifier, moment)
            if planned is None:
                return False
        if moment < planned:
            return False
        return self._fire(schedule, identifier, moment)

    def _plan(
        self, schedule: dict[str, Any], identifier: int, moment: dt.datetime
    ) -> dt.datetime | None:
        planned = self._next(schedule, identifier, moment)
        if planned is None:
            return None
        stored = _parse_time(schedule.get("next_fire_time"))
        last: Any = schedule.get("last_fire_time")
        if stored is not None and moment < stored < planned:
            # A restart honours a future firing already announced on the 调度 screen.
            planned = stored
        elif stored is not None and stored <= moment and self._backfill(schedule, identifier, moment, stored):
            last = moment
        self._planned[identifier] = planned
        self._remember(identifier, last, planned)
        return planned

    def _backfill(
        self, schedule: dict[str, Any], identifier: int, moment: dt.datetime, stored: dt.datetime
    ) -> bool:
        """Replay firings missed while the gateway was down, per the schedule's policy."""
        policy = str(schedule.get("misfire_policy") or "skip")
        if policy == "skip":
            return False
        if policy == "all":
            slots = missed_slots(schedule.get("cron_expression"), stored, moment, _int_or(schedule.get("max_backfill"), 10))
        else:
            slots = [stored]
        with self._lock:
            if identifier in self._firing:
                return False
            self._firing.add(identifier)
        try:
            for slot in slots:
                if self._stop.is_set():
                    break
                self._run_task(
                    schedule, identifier, moment, attempt=1, trigger="backfill", planned_time=slot, retry_allowed=False
                )
        finally:
            with self._lock:
                self._firing.discard(identifier)
        return bool(slots)

    def _next(
        self, schedule: dict[str, Any], identifier: int, moment: dt.datetime
    ) -> dt.datetime | None:
        try:
            return next_fire_time(schedule.get("cron_expression"), moment)
        except QualityError as error:
            self._disable(schedule, identifier, _reason(error))
            return None

    def _fire(
        self,
        schedule: dict[str, Any],
        identifier: int,
        moment: dt.datetime,
        *,
        attempt: int = 1,
        trigger: str = "schedule",
        planned_time: dt.datetime | None = None,
    ) -> bool:
        with self._lock:
            if identifier in self._firing:
                return False
            self._firing.add(identifier)
        try:
            if trigger == "schedule":
                planned_time = self._planned.get(identifier) or moment
                planned = self._next(schedule, identifier, moment)
                if planned is None:
                    return False
                # Computed before the run and from ``moment``: an overrunning run
                # skips the slots it covered instead of replaying them.
                self._planned[identifier] = planned
                self._retries.pop(identifier, None)
            self._run_task(schedule, identifier, moment, attempt=attempt, trigger=trigger, planned_time=planned_time)
            if trigger == "schedule":
                self._remember(identifier, moment, self._planned.get(identifier))
            return True
        finally:
            with self._lock:
                self._firing.discard(identifier)

    def _run_task(
        self,
        schedule: dict[str, Any],
        identifier: int,
        moment: dt.datetime,
        *,
        attempt: int,
        trigger: str,
        planned_time: dt.datetime | None,
        retry_allowed: bool = True,
    ) -> dict[str, Any]:
        """Dispatch one attempt, record it, and decide whether a retry follows."""
        registry = self.tasks()
        bean = str(schedule.get("bean_name") or SUPPORTED_BEAN)
        method = str(schedule.get("method_name") or SUPPORTED_METHOD)
        name = str(schedule.get("name") or identifier)
        run_id = self._open_run(schedule, identifier, f"{bean}.{method}", registry.label_of(bean, method), trigger, attempt, planned_time)
        started = time.monotonic()
        context = {
            # 立即执行 of a schedule is still a scheduled job to the task it runs: the
            # quality executions it writes carry that schedule, as they always did.
            "trigger": "schedule" if trigger == "manual" else trigger,
            "schedule_id": identifier,
            "schedule_name": name,
            "attempt": attempt,
            "planned_time": planned_time.isoformat() if planned_time else None,
        }
        try:
            outcome = registry.run(bean, method, schedule.get("method_params") or "", context)
        except Exception as error:  # noqa: BLE001 - any task failure is recorded, and maybe retried
            message = _reason(error)
            limit = _int_or(schedule.get("retry_limit"), 0) if retry_allowed and not isinstance(error, TaskError) else 0
            if attempt <= limit:
                delay = max(1, _int_or(schedule.get("retry_delay_seconds"), 60))
                due = moment + dt.timedelta(seconds=delay)
                self._retries[identifier] = (due, attempt + 1)
                status = "retrying"
                text = f"第 {attempt} 次执行失败，{due:%H:%M:%S} 进行第 {attempt + 1} 次尝试：{message}"
            else:
                self._retries.pop(identifier, None)
                status = "failed"
                text = f"执行失败（共尝试 {attempt} 次）：{message}" if attempt > 1 else f"执行失败：{message}"
            self._close_run(run_id, status, started, text, {})
            self._outcome(identifier, status, text)
            self._error = f"调度任务「{name}」{text}"
            return {"ok": False, "status": status, "message": text, "run_id": run_id, "outcome": None, "status_code": _status(error)}
        self._retries.pop(identifier, None)
        text = summarize(outcome) or "执行完成"
        if attempt > 1:
            text = f"第 {attempt} 次尝试成功：{text}"
        self._close_run(run_id, "success", started, text, json_detail(outcome))
        self._outcome(identifier, "success", text)
        return {"ok": True, "status": "success", "message": text, "run_id": run_id, "outcome": outcome, "status_code": 200}

    # ----- bookkeeping ----------------------------------------------------------
    def _open_run(
        self, schedule: dict[str, Any], identifier: int, task: str, label: str, trigger: str, attempt: int, planned_time: Any
    ) -> int | None:
        try:
            return self.store.start_task_run(
                schedule_id=identifier,
                schedule_name=str(schedule.get("name") or "")[:200],
                task=task,
                task_label=label,
                trigger_type=trigger,
                attempt=attempt,
                planned_time=planned_time,
            )
        except (QualityStoreError, AttributeError) as error:
            self._error = f"任务执行记录未能写入元数据库：{_reason(error)}"
            return None

    def _close_run(self, run_id: int | None, status: str, started: float, message: str, detail: dict[str, Any]) -> None:
        if run_id is None:
            return
        try:
            self.store.finish_task_run(run_id, status, _elapsed(started), message[:MAX_MESSAGE], detail)
        except (QualityStoreError, AttributeError) as error:
            self._error = f"任务执行记录未能写入元数据库：{_reason(error)}"

    def _outcome(self, identifier: int, status: str, message: str) -> None:
        try:
            self.store.record_schedule_outcome(identifier, status, message[:MAX_MESSAGE])
        except (QualityStoreError, AttributeError) as error:
            self._error = f"调度结果未能写入元数据库：{_reason(error)}"

    def _disable(self, schedule: dict[str, Any], identifier: int, reason: str) -> None:
        """Stop a schedule whose cron cannot be parsed, and say so in 执行日志."""
        name = schedule.get("name") or identifier
        message = f"调度任务「{name}」的 cron 表达式无效，已停止运行：{reason}"
        self._error = message
        self._planned.pop(identifier, None)
        self._retries.pop(identifier, None)
        try:
            self.store.set_schedule_state(identifier, 0)
            execution_id = self.store.start_execution(
                rule_name=str(name),
                trigger_type="schedule",
                schedule_id=identifier,
                message=message[:MAX_MESSAGE],
            )
            self.store.finish_execution(execution_id, 2, 0, message[:MAX_MESSAGE])
        except QualityStoreError as error:
            self._error = f"{message}（该状态未能写入元数据库：{_reason(error)}）"

    def _remember(self, identifier: int, last: Any, planned: Any) -> None:
        try:
            self.store.mark_fired(identifier, last, planned)
        except QualityStoreError as error:
            self._error = _reason(error)
