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

"""AI 增强: model-assisted quality rules and semantic models, lineage root-cause analysis.

Three helpers, each usable without a model and better with one:

* **quality rule suggestions** — read a table's columns (and what the catalog
  knows about them), propose rules from naming and typing conventions, let the
  configured model add or refine proposals, and validate every proposal with
  the same code that saves a rule, so what the user sees can be created as is;
* **semantic model drafts** — turn a set of tables into an Ossie model:
  datasets with typed fields, relationships inferred from key names, metrics
  from measure-like columns, then optional model refinement (descriptions,
  synonyms, better metrics), checked by the Ossie validator before it is shown;
* **root-cause analysis** — walk an asset's upstream lineage and collect the
  signals the platform already records (failed checks, schema changes, deleted
  assets, ingestion failures, recent edits), rank the suspects by strength and
  distance, and summarise — by template, or by the model when one is configured.

Nothing here writes on its own: suggestions are returned for a person to apply.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any, Callable

import yaml

from .llm import LlmError, SqlGenerator
from .observability import observe

MAX_TABLES = 30
MAX_COLUMNS = 200
def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Anthropic's structured output needs every object closed and every property required."""
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        schema = {**schema, "properties": {key: _strict(value) for key, value in properties.items()}, "required": list(properties), "additionalProperties": False}
    elif schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        schema = {**schema, "items": _strict(schema["items"])}
    return schema


PAIRS = {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}}
RULE_SCHEMA = _strict({
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "metric": {"type": "string"},
                    "column_name": {"type": "string", "description": "核查字段；整表核查时为空字符串"},
                    "config": {**PAIRS, "description": "核查参数键值对，值一律写成字符串"},
                    "expected_type": {"type": "string", "description": "fix_value 或 table_total_rows"},
                    "result_formula": {"type": "string", "description": "actual 或 percentage"},
                    "operator": {"type": "string", "description": "lte、lt、gte、gt、eq、neq"},
                    "threshold": {"type": "number"},
                    "level": {"type": "string", "description": "high、medium 或 low"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
})
MODEL_SCHEMA = _strict({
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "datasets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "synonyms": {"type": "array", "items": {"type": "string"}},
                    "fields": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "synonyms": {"type": "array", "items": {"type": "string"}}}}},
                },
            },
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "expression": {"type": "string"},
                    "description": {"type": "string"},
                    "datatype": {"type": "string", "description": "Integer、Decimal 或 Float"},
                    "synonyms": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "from_columns": {"type": "array", "items": {"type": "string"}},
                    "to_columns": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
})
RULE_SYSTEM_PROMPT = """你是数据质量工程师。根据给出的表结构、字段含义与可用的核查类型，为这张表推荐一组数据质量规则。
只使用给出的核查类型标识（metric）与其参数（config 的键），字段名必须来自表结构；阈值用数量（result_formula=actual）或百分比（result_formula=percentage）。
每条规则给出简短的中文 reason。只返回 JSON。"""
MODEL_SYSTEM_PROMPT = """你是数据建模专家。根据给出的表结构草稿（Apache Ossie 语义模型：datasets、fields、relationships、metrics），补充中文业务描述与同义词，
修正或补充数据集之间的关系（from 是多的一方、to 是一的一方，from_columns / to_columns 一一对应），并给出有业务意义的指标：
指标表达式用 ANSI SQL 聚合函数，字段写作 数据集名.字段名，例如 SUM(orders.amount) / COUNT(DISTINCT orders.order_id)。
只能引用给出的数据集与字段。只返回 JSON。"""
ROOT_CAUSE_SYSTEM_PROMPT = """你是数据平台的值班工程师。根据目标数据资产及其上游血缘上收集到的信号（质量核查失败、表结构变更、资产下线、拾取失败、近期修改），
用中文写一段简短的根因分析：最可能的原因是什么、证据是什么、建议先检查什么。不要编造信号之外的事实。"""

MEASURE_WORDS = ("amount", "price", "value", "total", "qty", "quantity", "count", "cost", "fee", "revenue", "sales", "profit", "score", "weight", "num", "金额", "数量", "价格")
TIME_WORDS = ("_at", "_date", "_time", "date", "time", "timestamp", "created", "updated", "modified")
ENUM_WORDS = ("status", "type", "state", "category", "kind", "level", "stage", "channel", "region")
EMAIL_REGEX = "^[^@ ]+@[^@ ]+[.][^@ ]+$"
PHONE_REGEX = "^1[3-9][0-9]{9}$"
SIGNAL_WEIGHTS = {"deleted": 10, "quality": 4, "schema": 3, "ingestion": 3, "edit": 1}
DEPTH_WEIGHTS = {0: 1.0, -1: 0.9, -2: 0.7, -3: 0.5}


class AiAssistError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def sql_datatype(raw: Any) -> str:
    """Map an engine type name onto an Ossie DataType."""
    text = str(raw or "").lower()
    if any(word in text for word in ("bool",)):
        return "Boolean"
    if "timestamp" in text or "datetime" in text:
        return "DateTime"
    if text.startswith("date") or text == "date":
        return "Date"
    if text.startswith("time"):
        return "Time"
    if any(word in text for word in ("int", "serial", "bigint", "smallint", "tinyint", "long", "short")):
        return "Integer"
    if any(word in text for word in ("decimal", "numeric", "money", "number(")):
        return "Decimal"
    if any(word in text for word in ("float", "double", "real")):
        return "Float"
    return "String"


def guess_keys(table_name: str, columns: list[dict[str, Any]]) -> list[str]:
    """Primary key columns: what the engine reports, else the ``<stem>_id`` naming convention.

    Sample and lake engines report no keys at all; ``customer_id`` in a table called
    ``t_lattice_customers`` is still unmistakably its key.
    """
    reported = [str(column["name"]) for column in columns if column.get("primary_key")]
    if reported:
        return reported
    stems = re.split(r"[^a-z0-9]+", table_name.lower())
    words = {stem for stem in stems if stem} | {stem.rstrip("s") for stem in stems if stem} | {stem + "s" for stem in stems if stem}
    for column in columns:
        name = str(column.get("name") or "")
        lowered = name.lower()
        if lowered == "id":
            return [name]
        if lowered.endswith("_id") and (lowered[:-3] in words or lowered[:-3] + "s" in words or lowered[:-3].rstrip("s") in words):
            return [name]
    return []


def _is_time_column(name: str, datatype: str) -> bool:
    lowered = name.lower()
    return datatype in {"Date", "DateTime", "Time"} or any(lowered.endswith(word) or lowered == word.strip("_") for word in TIME_WORDS)


def _is_measure(name: str, datatype: str) -> bool:
    lowered = name.lower()
    return datatype in {"Integer", "Decimal", "Float"} and not lowered.endswith("id") and any(word in lowered for word in MEASURE_WORDS)


class AiAssistService:
    def __init__(
        self,
        *,
        sources: Any,
        quality: Any = None,
        quality_store: Any = None,
        metadata: Any = None,
        lineage: Any = None,
        context: Any = None,
        ingestor: Any = None,
        llm: Any = None,
        validator: Callable[[dict[str, Any]], tuple[list[Any], list[Any]]] | None = None,
        catalog: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.sources = sources
        self.quality = quality
        self.quality_store = quality_store
        self.metadata = metadata
        self.lineage = lineage
        self.context = context
        self.ingestor = ingestor
        self.llm = llm
        self.validator = validator
        self.catalog = catalog
        self._clock = clock

    # ----- shared -------------------------------------------------------------------------
    def model_ready(self) -> bool:
        try:
            return bool(self.llm is not None and self.llm.configured())
        except Exception:  # noqa: BLE001 - a broken settings file means "no model"
            return False

    def status(self) -> dict[str, Any]:
        ready = self.model_ready()
        info = self.llm.status() if ready else {}
        return {
            "model_configured": ready,
            "provider": info.get("provider", ""),
            "model": info.get("model", ""),
            "features": {
                "quality_rules": self.quality is not None,
                "semantic_model": True,
                "root_cause": self.metadata is not None and self.lineage is not None,
            },
        }

    def _provider(self, used_model: bool) -> dict[str, Any]:
        if not used_model:
            return {"provider": "", "model": "", "used_model": False}
        info = self.llm.status()
        return {"provider": info.get("provider", ""), "model": info.get("model", ""), "used_model": True}

    def _table(self, datasource_id: str, schema_name: str | None, table_name: str) -> dict[str, Any]:
        connector = self.sources.connector(datasource_id)
        schema = schema_name or connector.default_schema() or ""
        detail = connector.table(schema, table_name) if schema else connector.table("", table_name)
        columns = [dict(item) for item in (detail.get("columns") or [])][:MAX_COLUMNS]
        return {"datasource_id": datasource_id, "schema_name": schema, "table_name": table_name, "rows": detail.get("rows"), "columns": columns}

    def _catalog_columns(self, datasource_id: str, schema_name: str, table_name: str) -> dict[str, dict[str, Any]]:
        """What the metadata catalog knows about the table's columns, if anything."""
        if self.metadata is None or self.lineage is None:
            return {}
        try:
            row = self.lineage.find_table(datasource_id, schema_name or None, table_name)
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            return {}
        columns = row.get("json", {}).get("columns") or []
        return {str(item.get("name")): item for item in columns if isinstance(item, dict) and item.get("name")}

    # ----- quality rules -----------------------------------------------------------------
    def suggest_rules(self, datasource_id: str, schema_name: str | None, table_name: str, *, use_model: bool = True) -> dict[str, Any]:
        if self.quality is None:
            raise AiAssistError(503, "数据质量模块尚未初始化。")
        table = self._table(datasource_id, schema_name, table_name)
        known = self._catalog_columns(datasource_id, table["schema_name"], table_name)
        proposals = self._heuristic_rules(table, known)
        used_model = False
        if use_model and self.model_ready():
            with observe("suggest_rules", "llm", table=table_name):
                proposals = self._merge(proposals, self._model_rules(table, known))
            used_model = True
        suggestions = [self._validate_rule(table, item) for item in proposals]
        return {"table": {**table, "columns": [{**column, "known_description": (known.get(column["name"]) or {}).get("description", "")} for column in table["columns"]]}, "suggestions": suggestions, **self._provider(used_model)}

    def _heuristic_rules(self, table: dict[str, Any], known: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        freshness_done = False
        keys = set(guess_keys(table["table_name"], table["columns"]))
        for column in table["columns"]:
            name = str(column.get("name") or "")
            datatype = sql_datatype(column.get("type"))
            lowered = name.lower()
            tags = " ".join(str(tag.get("tag_fqn") or "") for tag in (known.get(name) or {}).get("tags") or [] if isinstance(tag, dict)).lower()
            if name in keys:
                rules.append(self._rule(f"{name} 唯一", "column_duplicate", name, {}, 0, "high", "主键字段不允许重复"))
                rules.append(self._rule(f"{name} 非空", "column_null", name, {}, 0, "high", "主键字段不允许为空"))
                continue
            if lowered.endswith("_id") or lowered == "id":
                rules.append(self._rule(f"{name} 非空", "column_null", name, {}, 0, "medium", "外键 / 标识字段通常不允许为空"))
            if _is_time_column(name, datatype) and not freshness_done and any(word in lowered for word in ("updated", "created", "modified", "date", "time", "_at")):
                rules.append(self._rule(f"{table['table_name']} 当日已更新", "table_freshness", name, {"interval_value": 1, "interval_unit": "day"}, 0, "low", f"按 {name} 检查表是否在一天内更新过"))
                freshness_done = True
            if _is_measure(name, datatype):
                rules.append(self._rule(f"{name} 非负", "column_value_between", name, {"min": 0}, 0, "medium", "金额 / 数量类字段不应小于 0"))
            if datatype == "String" and ("email" in lowered or "邮箱" in lowered):
                rules.append(self._rule(f"{name} 邮箱格式", "column_match_regex", name, {"regexp": EMAIL_REGEX}, 0, "medium", "邮箱字段应符合 name@domain 形式"))
            if datatype == "String" and any(word in lowered for word in ("phone", "mobile", "tel", "手机")):
                rules.append(self._rule(f"{name} 手机号格式", "column_match_regex", name, {"regexp": PHONE_REGEX}, 0, "medium", "手机号字段应为 11 位号码"))
            if "pii" in tags or "sensitive" in tags:
                rules.append(self._rule(f"{name} 非空（敏感字段）", "column_null", name, {}, 5, "medium", "目录中标记为敏感数据的字段，空值率不应超过 5%", formula="percentage"))
        return rules

    @staticmethod
    def _rule(name: str, metric: str, column: str | None, config: dict[str, Any], threshold: float, level: str, reason: str, *, formula: str = "actual") -> dict[str, Any]:
        return {"name": name[:200], "metric": metric, "column_name": column, "config": config, "expected_type": "fix_value", "result_formula": formula, "operator": "lte", "threshold": threshold, "level": level, "reason": reason, "source": "heuristic"}

    def _model_rules(self, table: dict[str, Any], known: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        catalog = self.catalog() if self.catalog else {"dimensions": []}
        metric_lines = []
        for dimension in catalog.get("dimensions", []):
            for metric in dimension.get("metrics", []):
                fields = ", ".join(f"{item['name']}({item.get('type')})" for item in metric.get("fields", []) if item.get("name") != "filter")
                metric_lines.append(f"- {metric['id']}：{metric['label']}；{'需要字段' if metric.get('needs_column') else '整表'}；参数：{fields or '无'}")
        column_lines = [
            f"- {column.get('name')} {column.get('type')}{'，主键' if column.get('primary_key') else ''}{'，可空' if column.get('nullable') else ''}" + (f"，含义：{(known.get(str(column.get('name'))) or {}).get('description')}" if (known.get(str(column.get('name'))) or {}).get("description") else "")
            for column in table["columns"]
        ]
        prompt = (
            f"表：{table['schema_name'] + '.' if table['schema_name'] else ''}{table['table_name']}（约 {table.get('rows') or '未知'} 行）\n字段：\n" + "\n".join(column_lines)
            + "\n\n可用核查类型：\n" + "\n".join(metric_lines)
            + "\n\n请推荐 3 到 8 条规则；config 写成 [{key, value}] 键值对，值一律为字符串；整表核查的 column_name 为空字符串。"
        )
        try:
            data = SqlGenerator(self.llm).complete_json(prompt, system=RULE_SYSTEM_PROMPT, schema=RULE_SCHEMA)
        except LlmError as error:
            raise AiAssistError(502, f"模型生成规则失败：{error}") from error
        proposals = []
        for item in data.get("rules") or []:
            if not isinstance(item, dict) or not item.get("metric"):
                continue
            proposals.append({
                "name": str(item.get("name") or f"{item.get('column_name') or table['table_name']} {item['metric']}")[:200],
                "metric": str(item["metric"]),
                "column_name": str(item["column_name"]) if item.get("column_name") else None,
                "config": _pairs_to_config(item.get("config")),
                "expected_type": str(item.get("expected_type") or "fix_value"),
                "result_formula": str(item.get("result_formula") or "actual"),
                "operator": str(item.get("operator") or "lte"),
                "threshold": item.get("threshold") if isinstance(item.get("threshold"), (int, float)) else 0,
                "level": str(item.get("level") or "medium"),
                "reason": str(item.get("reason") or "")[:500],
                "source": "model",
            })
        return proposals

    @staticmethod
    def _merge(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = {(item["metric"], item.get("column_name") or "") for item in base}
        merged = list(base)
        for item in extra:
            key = (item["metric"], item.get("column_name") or "")
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _validate_rule(self, table: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
        rule = {
            **proposal,
            "datasource_id": table["datasource_id"],
            "schema_name": table["schema_name"] or None,
            "table_name": table["table_name"],
            "state": 1,
            "comment": f"AI 推荐：{proposal.get('reason', '')}"[:2000],
        }
        rule.pop("source", None)
        rule.pop("reason", None)
        try:
            prepared = self.quality.prepare_rule(rule)
            return {**proposal, "valid": True, "message": "", "rule": {key: prepared[key] for key in ("name", "metric", "dimension", "level", "datasource_id", "schema_name", "table_name", "column_name", "config", "expected_type", "result_formula", "operator", "threshold") if key in prepared}}
        except Exception as error:  # noqa: BLE001 - every proposal is reported, valid or not
            return {**proposal, "valid": False, "message": str(error)[:300], "rule": None}

    def apply_rules(self, rules: list[dict[str, Any]]) -> dict[str, Any]:
        if self.quality is None or self.quality_store is None:
            raise AiAssistError(503, "数据质量模块尚未初始化。")
        created, errors = [], []
        for item in rules[:50]:
            try:
                prepared = self.quality.prepare_rule({**item, "state": item.get("state", 1), "comment": item.get("comment") or "AI 推荐"})
                created.append(self.quality_store.create_rule(prepared))
            except Exception as error:  # noqa: BLE001
                errors.append({"name": item.get("name"), "message": str(error)[:300]})
        return {"created": created, "errors": errors}

    # ----- semantic models ---------------------------------------------------------------
    def suggest_semantic_model(self, datasource_id: str, schema_name: str | None, tables: list[str], *, name: str | None = None, use_model: bool = True) -> dict[str, Any]:
        names = [str(item).strip() for item in tables if str(item).strip()][:MAX_TABLES]
        if not names:
            raise AiAssistError(400, "请至少选择一张表。")
        details = [self._table(datasource_id, schema_name, table) for table in names]
        known = {table: self._catalog_columns(datasource_id, detail["schema_name"], table) for table, detail in zip(names, details)}
        document = self._draft_model(datasource_id, details, known, name or f"{datasource_id}_model")
        used_model = False
        if use_model and self.model_ready():
            with observe("suggest_model", "llm", tables=len(names)):
                document = self._refine_model(document)
            used_model = True
        text = yaml.safe_dump({"version": "0.2.0.dev0", "semantic_model": [document]}, allow_unicode=True, sort_keys=False)
        valid, errors, warnings = True, [], []
        if self.validator is not None:
            try:
                failures, notes = self.validator({"version": "0.2.0.dev0", "semantic_model": [document]})
                valid, errors, warnings = not failures, [str(item)[:500] for item in failures[:20]], [str(item)[:500] for item in notes[:20]]
            except Exception as error:  # noqa: BLE001
                valid, errors = False, [str(error)[:500]]
        return {
            "name": document["name"],
            "yaml": text,
            "document": document,
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "summary": {"datasets": len(document["datasets"]), "relationships": len(document.get("relationships") or []), "metrics": len(document.get("metrics") or [])},
            **self._provider(used_model),
        }

    def _draft_model(self, datasource_id: str, details: list[dict[str, Any]], known: dict[str, dict[str, dict[str, Any]]], name: str) -> dict[str, Any]:
        datasets = []
        primary_keys: dict[str, str] = {}
        for detail in details:
            table = detail["table_name"]
            keys = guess_keys(table, detail["columns"])
            if len(keys) == 1:
                primary_keys[keys[0].lower()] = table
            fields = []
            for column in detail["columns"]:
                column_name = str(column.get("name") or "")
                datatype = sql_datatype(column.get("type"))
                field: dict[str, Any] = {"name": column_name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": column_name}]}, "datatype": datatype}
                description = (known.get(table, {}).get(column_name) or {}).get("description")
                if description:
                    field["description"] = str(description)[:500]
                if _is_time_column(column_name, datatype):
                    field["dimension"] = {"is_time": True}
                elif datatype == "String" or column_name.lower().endswith("_id"):
                    field["dimension"] = {"is_time": False}
                fields.append(field)
            source = f"{datasource_id}.{detail['schema_name']}.{table}" if detail["schema_name"] else table
            dataset: dict[str, Any] = {"name": table, "source": source, "fields": fields}
            if keys:
                dataset["primary_key"] = keys
            datasets.append(dataset)
        relationships = []
        for detail in details:
            table = detail["table_name"]
            for column in detail["columns"]:
                column_name = str(column.get("name") or "")
                owner = primary_keys.get(column_name.lower())
                if owner and owner != table and column_name not in guess_keys(table, detail["columns"]):
                    relationships.append({"name": f"{table}_to_{owner}", "from": table, "to": owner, "from_columns": [column_name], "to_columns": [column_name]})
        metrics = []
        for detail in details:
            table = detail["table_name"]
            keys = guess_keys(table, detail["columns"])
            if len(keys) == 1:
                metrics.append({"name": f"{table}_count", "datatype": "Integer", "description": f"{table} 的记录数", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": f"COUNT(DISTINCT {table}.{keys[0]})"}]}})
            for column in detail["columns"]:
                column_name = str(column.get("name") or "")
                if _is_measure(column_name, sql_datatype(column.get("type"))):
                    metrics.append({"name": f"total_{column_name}", "datatype": "Decimal", "description": f"{table}.{column_name} 之和", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": f"SUM({table}.{column_name})"}]}})
        document: dict[str, Any] = {"name": re.sub(r"[^A-Za-z0-9_\-]", "_", name)[:120] or "model", "description": f"由 {datasource_id} 的 {len(datasets)} 张表生成的语义模型草稿", "datasets": datasets}
        if relationships:
            document["relationships"] = relationships
        if metrics:
            document["metrics"] = metrics[:50]
        return document

    def _refine_model(self, document: dict[str, Any]) -> dict[str, Any]:
        outline = []
        for dataset in document["datasets"]:
            outline.append(f"- 数据集 {dataset['name']}（source {dataset['source']}，主键 {', '.join(dataset.get('primary_key') or []) or '无'}）：" + ", ".join(f"{field['name']} {field.get('datatype')}" for field in dataset["fields"]))
        prompt = (
            "表结构草稿：\n" + "\n".join(outline)
            + "\n已推断的关系：\n" + "\n".join(f"- {r['from']}.{','.join(r['from_columns'])} → {r['to']}.{','.join(r['to_columns'])}" for r in document.get("relationships") or []) 
            + "\n已推断的指标：\n" + "\n".join(f"- {m['name']} = {m['expression']['dialects'][0]['expression']}" for m in document.get("metrics") or [])
            + "\n\n请返回 {description, datasets:[{name, description, synonyms, fields:[{name, description, synonyms}]}], metrics:[{name, expression, description, datatype, synonyms}], relationships:[{from, to, from_columns, to_columns}]}。"
        )
        try:
            data = SqlGenerator(self.llm).complete_json(prompt, system=MODEL_SYSTEM_PROMPT, schema=MODEL_SCHEMA)
        except LlmError as error:
            raise AiAssistError(502, f"模型完善语义模型失败：{error}") from error
        by_name = {dataset["name"]: dataset for dataset in document["datasets"]}
        if data.get("description"):
            document["description"] = str(data["description"])[:2000]
        for item in data.get("datasets") or []:
            dataset = by_name.get(str(item.get("name"))) if isinstance(item, dict) else None
            if dataset is None:
                continue
            if item.get("description"):
                dataset["description"] = str(item["description"])[:2000]
            if isinstance(item.get("synonyms"), list) and item["synonyms"]:
                dataset["ai_context"] = {"synonyms": [str(s)[:100] for s in item["synonyms"][:10]]}
            fields = {field["name"]: field for field in dataset["fields"]}
            for column in item.get("fields") or []:
                field = fields.get(str(column.get("name"))) if isinstance(column, dict) else None
                if field is None:
                    continue
                if column.get("description"):
                    field["description"] = str(column["description"])[:500]
                if isinstance(column.get("synonyms"), list) and column["synonyms"]:
                    field["ai_context"] = {"synonyms": [str(s)[:100] for s in column["synonyms"][:10]]}
        valid_fields = {(dataset["name"], field["name"]) for dataset in document["datasets"] for field in dataset["fields"]}
        relationships = []
        for item in data.get("relationships") or []:
            if not isinstance(item, dict):
                continue
            from_dataset, to_dataset = str(item.get("from") or ""), str(item.get("to") or "")
            from_columns, to_columns = [str(c) for c in item.get("from_columns") or []], [str(c) for c in item.get("to_columns") or []]
            if from_dataset in by_name and to_dataset in by_name and from_columns and len(from_columns) == len(to_columns) and all((from_dataset, c) in valid_fields for c in from_columns) and all((to_dataset, c) in valid_fields for c in to_columns):
                relationships.append({"name": f"{from_dataset}_to_{to_dataset}", "from": from_dataset, "to": to_dataset, "from_columns": from_columns, "to_columns": to_columns})
        if relationships:
            existing = {(r["from"], r["to"], tuple(r["from_columns"])) for r in document.get("relationships") or []}
            document["relationships"] = [*(document.get("relationships") or []), *[r for r in relationships if (r["from"], r["to"], tuple(r["from_columns"])) not in existing]]
        metrics = []
        for item in data.get("metrics") or []:
            if not isinstance(item, dict) or not item.get("name") or not item.get("expression"):
                continue
            expression = str(item["expression"]).strip()
            referenced = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", expression)
            if referenced and not all((dataset, column) in valid_fields for dataset, column in referenced):
                continue
            metric: dict[str, Any] = {"name": re.sub(r"[^A-Za-z0-9_]", "_", str(item["name"]))[:100], "datatype": str(item.get("datatype") or "Decimal") if str(item.get("datatype") or "") in {"Integer", "Decimal", "Float"} else "Decimal", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression[:2000]}]}}
            if item.get("description"):
                metric["description"] = str(item["description"])[:2000]
            if isinstance(item.get("synonyms"), list) and item["synonyms"]:
                metric["ai_context"] = {"synonyms": [str(s)[:100] for s in item["synonyms"][:10]]}
            metrics.append(metric)
        if metrics:
            names = {metric["name"] for metric in metrics}
            document["metrics"] = [*metrics, *[m for m in document.get("metrics") or [] if m["name"] not in names]][:50]
        return document

    # ----- root cause ------------------------------------------------------------------------
    def root_cause(self, entity_type: str | None, ref: str, *, days: int = 7, use_model: bool = True) -> dict[str, Any]:
        if self.metadata is None or self.lineage is None:
            raise AiAssistError(503, "元数据目录尚未初始化，无法做根因分析。")
        span = max(1, min(int(days or 7), 90))
        target = self.metadata.row(entity_type, ref)
        try:
            graph = self.lineage.graph(target["entity_type"], target["fqn"], upstream_depth=3, downstream_depth=0)
        except Exception as error:  # noqa: BLE001 - an entity without lineage is analysed on its own
            graph = {"nodes": [{**target, "depth": 0}], "upstream_count": 0, "note": str(error)[:200]}
        nodes = sorted(graph.get("nodes") or [], key=lambda node: -int(node.get("depth") or 0))
        since_ms = int((self._clock() - span * 86400) * 1000)
        table_names = sorted({str(node.get("name") or str(node.get("fqn")).split(".")[-1]) for node in nodes})
        quality_by_table = self._failed_checks(table_names, span)
        runs_by_service = self._failed_runs(since_ms)
        suspects = []
        for node in nodes:
            depth = int(node.get("depth") or 0)
            signals = self._signals(node, since_ms, quality_by_table, runs_by_service)
            score = sum(SIGNAL_WEIGHTS.get(signal["kind"], 1) for signal in signals) * DEPTH_WEIGHTS.get(depth, 0.4)
            suspects.append({"fqn": node.get("fqn"), "name": node.get("name") or node.get("fqn"), "entity_type": node.get("entity_type"), "depth": depth, "score": round(score, 1), "signals": signals})
        suspects.sort(key=lambda item: (-item["score"], item["depth"]))
        summary = self._root_cause_summary(target, suspects, span)
        used_model = False
        if use_model and self.model_ready() and any(item["signals"] for item in suspects):
            with observe("root_cause", "llm", target=target["fqn"]):
                try:
                    summary = SqlGenerator(self.llm).complete_text(self._root_cause_prompt(target, suspects, span), system=ROOT_CAUSE_SYSTEM_PROMPT).strip()[:4000] or summary
                    used_model = True
                except LlmError as error:
                    summary = f"{summary}\n（模型分析失败：{error}）"
        return {
            "target": {"fqn": target["fqn"], "name": target.get("name"), "entity_type": target["entity_type"]},
            "window_days": span,
            "upstream_count": graph.get("upstream_count", 0),
            "suspects": suspects,
            "signals_checked": ["质量核查未通过", "表结构变更", "资产下线", "拾取失败", "近期修改"],
            "summary": summary,
            **self._provider(used_model),
        }

    def _failed_checks(self, table_names: list[str], days: int) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        if self.quality_store is None or not table_names:
            return grouped
        try:
            rows = self.quality_store.recent_results(table_names, days)
        except Exception:  # noqa: BLE001 - a down quality database only loses this signal
            return grouped
        for row in rows or []:
            if int(row.get("state") or 0) == 2:
                grouped.setdefault(str(row.get("table_name") or ""), []).append(row)
        return grouped

    def _failed_runs(self, since_ms: int) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        if self.ingestor is None:
            return grouped
        try:
            listing = self.ingestor.runs(size=200)
        except Exception:  # noqa: BLE001
            return grouped
        for run in (listing or {}).get("items", []):
            if run.get("status") in {"failed", "partial"} and int(run.get("started_at") or 0) >= since_ms:
                grouped.setdefault(str(run.get("service_fqn") or ""), []).append(run)
        return grouped

    def _signals(self, node: dict[str, Any], since_ms: int, quality: dict[str, list[dict[str, Any]]], runs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        fqn = str(node.get("fqn") or "")
        name = str(node.get("name") or fqn.split(".")[-1])
        if node.get("deleted"):
            signals.append({"kind": "deleted", "label": "资产已下线", "detail": f"{name} 在目录中已被删除"})
        for row in quality.get(name, [])[:5]:
            signals.append({"kind": "quality", "label": "质量核查未通过", "detail": f"{row.get('rule_name')}：不合规 {row.get('actual_value')}，期望 {row.get('operator')} {row.get('threshold')}（{str(row.get('check_time'))[:19]}）"})
        try:
            history = self.metadata.versions(node.get("entity_type"), fqn).get("versions", [])
        except Exception:  # noqa: BLE001
            history = []
        for version in history:
            if int(version.get("updated_at") or 0) < since_ms:
                continue
            change = version.get("change") or {}
            removed = [item.get("name") for item in change.get("fields_deleted") or [] if str(item.get("name", "")).startswith("columns.")]
            retyped = [item.get("name") for item in change.get("fields_updated") or [] if str(item.get("name", "")).startswith("columns.") and str(item.get("name", "")).endswith("data_type_display")]
            if removed or retyped:
                signals.append({"kind": "schema", "label": "表结构变更", "detail": f"版本 {version.get('version')}：" + ("删除字段 " + "、".join(str(item).split('.')[1] for item in removed) if removed else "") + ("；" if removed and retyped else "") + ("类型变更 " + "、".join(str(item).split('.')[1] for item in retyped) if retyped else "") + f"（{version.get('updated_by')}）"})
            elif change.get("fields_updated") or change.get("fields_added"):
                signals.append({"kind": "edit", "label": "近期修改", "detail": f"版本 {version.get('version')}：{version.get('summary') or '字段变更'}（{version.get('updated_by')}）"})
        service_fqn = str(node.get("service_fqn") or fqn.split(".")[0])
        for run in runs.get(service_fqn, [])[:3]:
            signals.append({"kind": "ingestion", "label": "拾取失败", "detail": f"服务 {service_fqn} 的拾取 {run.get('status')}：{str(run.get('message') or '')[:120]}"})
        return signals

    @staticmethod
    def _root_cause_summary(target: dict[str, Any], suspects: list[dict[str, Any]], days: int) -> str:
        flagged = [item for item in suspects if item["signals"]]
        if not flagged:
            return f"最近 {days} 天内，{target['fqn']} 及其上游 {max(len(suspects) - 1, 0)} 个资产没有质量失败、结构变更、下线或拾取失败的记录；问题更可能来自数据内容本身或目录未覆盖的环节。"
        top = flagged[0]
        where = "自身" if top["depth"] == 0 else f"上游 {abs(top['depth'])} 层的 {top['name']}"
        signals = "；".join(signal["detail"] for signal in top["signals"][:3])
        rest = "、".join(item["name"] for item in flagged[1:4])
        return f"最可能的原因在{where}：{signals}。" + (f"其他有信号的资产：{rest}。" if rest else "") + "建议先核对该资产最近的变更与核查结果，再沿血缘向下确认影响。"

    @staticmethod
    def _root_cause_prompt(target: dict[str, Any], suspects: list[dict[str, Any]], days: int) -> str:
        lines = [f"目标资产：{target['fqn']}（{target.get('entity_type')}），分析窗口：最近 {days} 天。", "沿上游血缘收集到的信号（depth 为负表示上游层数）："]
        for item in suspects:
            if not item["signals"]:
                continue
            lines.append(f"- {item['fqn']}（depth {item['depth']}，得分 {item['score']}）")
            for signal in item["signals"]:
                lines.append(f"  · {signal['label']}：{signal['detail']}")
        if len(lines) == 2:
            lines.append("（没有收集到任何信号）")
        return "\n".join(lines)


def _pairs_to_config(raw: Any) -> dict[str, Any]:
    """The model answers config as key/value pairs (a closed schema cannot hold free keys)."""
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if value not in (None, "")}
    config: dict[str, Any] = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("key") and item.get("value") not in (None, ""):
            config[str(item["key"])] = item["value"]
    return config


def local_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
