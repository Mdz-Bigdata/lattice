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
"""Map a question onto the metrics and dimensions a semantic model defines.

The plan names governed metrics and dimensions, never SQL, so a definition cannot
drift from one answer to the next: the compiler in :mod:`webapi.semantic_query` alone
decides the joins and the aggregation.

Two planners produce the same request:

* ``plan_locally`` matches the question against the model's own vocabulary. It accounts
  for every character of the question: anything it cannot explain makes it give up
  rather than answer a narrower question than the one asked ("退货金额" must not
  turn into a total because "金额" looked familiar), and the same words always give
  the same SQL.
* ``plan_with_model`` asks the language model when the words need interpreting. Its
  plan is checked against the model's names and the data's real values, and against the
  literals in the question, because a confident plan can still drop a filter.

Neither guesses at a concept the model does not define: that is reported as unmapped.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LlmError
from .schema_linking import SENSITIVE_COLUMN
from .semantic_query import GRAINS, Model, OPERATORS

MAX_VALUES_SHOWN = 30
GRAIN_TERMS = {
    "day": ("每日", "每天", "按日", "按天", "日度", "逐日", "各日", "日期"),
    "week": ("每周", "按周", "周度", "逐周", "各周"),
    "month": ("每月", "按月", "月度", "逐月", "各月", "月份", "分月", "月"),
    "quarter": ("每季度", "按季度", "各季度", "分季度", "季度", "按季", "每季"),
    "year": ("每年", "按年", "年度", "逐年", "各年", "分年", "年份"),
}
DESCENDING = ("最高", "最多", "最大", "最好", "排名", "排行", "排序", "降序", "从高到低", "从大到小")
ASCENDING = ("最低", "最少", "最小", "最差", "升序", "从低到高", "从小到大", "倒数")
SUPERLATIVE = ("最高", "最多", "最大", "最好", "最低", "最少", "最小", "最差")
TREND = ("趋势", "走势", "变化")
#: Words that carry no meaning of their own in these questions. Anything else unexplained
#: is a reason to give up: a word like 未 / 非 / 除 / 环比 changes the answer. A single
#: character belongs here only if it cannot be half of another word: 款 is half of 退款
#: and 前 half of 前月, which would otherwise read as "per month".
FILLER = (
    "帮我", "请问", "请", "我想", "我要", "想看", "看看", "看一下", "查一下", "查询", "查看", "统计", "统计一下",
    "分析", "计算", "展示", "显示", "列出", "给我", "告诉我", "一下", "分别", "分布", "情况", "数据", "结果", "明细",
    "是多少", "有多少", "多少", "是什么", "哪些", "哪个", "哪家", "怎么样", "如何", "对比", "比较", "汇总", "总计", "合计",
    "总", "全部", "所有", "整体", "各个", "每个", "各", "每", "按照", "按", "根据", "分", "以及", "和", "与", "及", "跟", "还有",
    "的", "了", "吗", "呢", "吧", "啊", "中", "里", "在", "为", "是", "属于", "等于", "之间", "期间", "时候",
    "维度", "指标", "分组",
)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_DAY_RANGE = re.compile(r"(\d{4})年(\d{1,2})月(?:(\d{1,2})[日号])?(?:到|至|~|-)(?:(\d{4})年)?(\d{1,2})月(?:(\d{1,2})[日号])?")
_DAY = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]|(\d{4})-(\d{1,2})-(\d{1,2})")
_QUARTER = re.compile(r"(\d{4})年?(?:第?([一二三四1-4])季度|q([1-4]))")
_MONTH = re.compile(r"(\d{4})年(\d{1,2})月(?:份)?|(\d{4})[-/.](\d{1,2})(?!\d)")
_YEAR = re.compile(r"(\d{4})年(?:度)?")
_TOP = re.compile(r"(?:前|top)(\d+|[一二两三四五六七八九十]+)(?:名|个|家|位|款|大|条)?")
_BOTTOM = re.compile(r"(?:后|倒数)(\d+|[一二两三四五六七八九十]+)(?:名|个|家|位|款|条)?")


@dataclass
class Plan:
    status: str  # ok | derived | unmapped
    request: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    steps: list[str] = field(default_factory=list)
    reason: str = ""
    planner: str = "rules"
    #: Terms of the question a complete local parse would have needed.
    unexplained: list[str] = field(default_factory=list)
    score: int = 0


def normalize(text: str) -> str:
    table = str.maketrans({"，": " ", "。": " ", "？": " ", "！": " ", "、": " ", "；": " ", "：": " ", "（": " ", "）": " ", "“": " ", "”": " ", "?": " ", "!": " ", ",": " ", ";": " ", ":": " ", "(": " ", ")": " "})
    return re.sub(r"\s+", "", str(text or "").translate(table)).lower()


def _number(text: str) -> int:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_CN_DIGITS.get(tens, 1)) * 10 + _CN_DIGITS.get(ones, 0)
    return _CN_DIGITS.get(text, 0)


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def time_range(text: str) -> tuple[list[tuple[int, int]], list[tuple[str, str]]]:
    """Absolute periods in a normalized question: their spans and date bounds."""
    spans: list[tuple[int, int]] = []
    bounds: list[tuple[str, str]] = []

    def free(match: re.Match) -> bool:
        return not any(match.start() < end and start < match.end() for start, end in spans)

    def add(match: re.Match, start: dt.date, end: dt.date) -> None:
        spans.append(match.span())
        bounds.append((start.isoformat(), end.isoformat()))

    try:
        for match in _DAY_RANGE.finditer(text):
            year, month, day, end_year, end_month, end_day = match.groups()
            first = dt.date(int(year), int(month), int(day or 1))
            last_year = int(end_year or year)
            last = dt.date(last_year, int(end_month), int(end_day)) if end_day else _month_end(last_year, int(end_month))
            if free(match):
                add(match, first, last)
        for match in _DAY.finditer(text):
            parts = [item for item in match.groups() if item]
            day = dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
            if free(match):
                add(match, day, day)
        for match in _QUARTER.finditer(text):
            quarter = _number(match.group(2) or match.group(3))
            year = int(match.group(1))
            if free(match):
                add(match, dt.date(year, quarter * 3 - 2, 1), _month_end(year, quarter * 3))
        for match in _MONTH.finditer(text):
            parts = [item for item in match.groups() if item]
            year, month = int(parts[0]), int(parts[1])
            if free(match):
                add(match, dt.date(year, month, 1), _month_end(year, month))
        for match in _YEAR.finditer(text):
            year = int(match.group(1))
            if free(match):
                add(match, dt.date(year, 1, 1), dt.date(year, 12, 31))
    except ValueError:  # 2018年13月: not a date, so leave it unexplained.
        return [], []
    return spans, bounds


def _terms_of(model: Model, values: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """(normalized term, kind, target) for every word the model gives a meaning to."""
    terms: list[tuple[str, str, str]] = []
    seen: dict[str, tuple[str, str]] = {}

    def add(term: str, kind: str, target: str) -> None:
        key = normalize(term)
        if not key:
            return
        previous = seen.get(key)
        if previous and previous != (kind, target):
            # One word for two meanings is resolved in favour of the longer-lived
            # definition (metrics, then dimensions) only when kinds differ; two values or
            # two dimensions sharing a word stay ambiguous and are dropped by the caller.
            terms.append((key, "ambiguous", f"{previous[1]}|{target}"))
            return
        seen[key] = (kind, target)
        terms.append((key, kind, target))

    for metric in model.metrics.values():
        add(metric.name, "metric", metric.name)
        for synonym in metric.synonyms:
            add(synonym, "metric", metric.name)
    for dataset in model.datasets.values():
        for item in dataset.fields.values():
            target = f"{dataset.name}.{item.name}"
            if item.is_time:
                add(item.name, "time_field", target)
                for synonym in item.synonyms:
                    add(synonym, "time_field", target)
            elif item.is_dimension:
                add(item.name, "dimension", target)
                for synonym in item.synonyms:
                    add(synonym, "dimension", target)
    for target, options in values.items():
        for value in options:
            add(value, "value", f"{target}\x1f{value}")
    return terms


def _default_time_field(model: Model, metric_names: list[str]) -> str:
    """The date a metric's "per month" refers to, declared by the model or else unique."""
    for name in metric_names:
        declared = model.metrics[name].time_field
        if declared:
            return declared
    candidates = [f"{dataset.name}.{item.name}" for dataset in model.datasets.values() for item in dataset.fields.values() if item.is_time]
    return candidates[0] if len(candidates) == 1 else ""


def plan_locally(question: str, model: Model, values: dict[str, list[str]]) -> Plan:
    text = normalize(question)
    taken = [False] * len(text)

    def mark(start: int, end: int) -> None:
        for index in range(start, end):
            taken[index] = True

    def is_free(start: int, end: int) -> bool:
        return not any(taken[start:end])

    time_spans, periods = time_range(text)
    for start, end in time_spans:
        mark(start, end)
    limit = None
    descending = None
    for pattern, direction in ((_TOP, True), (_BOTTOM, False)):
        match = pattern.search(text)
        if match and is_free(*match.span()):
            limit = _number(match.group(1))
            descending = direction
            mark(*match.span())
            break

    # Longest terms first, so 商品类别 is never read as 商品, and 客户数 not as 客户.
    terms = _terms_of(model, values)
    for words, kind in (
        (DESCENDING, "desc"), (ASCENDING, "asc"), (TREND, "trend"),
        *((words, f"grain:{grain}") for grain, words in GRAIN_TERMS.items()),
    ):
        terms.extend((word, kind, "") for word in words)
    terms.sort(key=lambda item: (-len(item[0]), item[1] == "ambiguous"))
    found: list[tuple[int, int, str, str]] = []
    ambiguous: list[str] = []
    for term, kind, target in terms:
        position = 0
        while (position := text.find(term, position)) >= 0:
            end = position + len(term)
            # 商品 1 must not match inside 商品 10.
            digit_boundary = term[-1].isdigit() and end < len(text) and text[end].isdigit()
            if is_free(position, end) and not digit_boundary:
                mark(position, end)
                if kind == "ambiguous":
                    ambiguous.append(term)
                else:
                    found.append((position, end, kind, target))
            position = end
    found.sort()

    # Whatever is left must be filler; anything else is a meaning this parse would lose.
    residue = "".join(character if not used else "\x00" for character, used in zip(text, taken))
    for word in sorted(FILLER, key=len, reverse=True):
        residue = residue.replace(word, "\x00" * len(word))
    unexplained = [piece for piece in residue.split("\x00") if piece]

    metrics: list[str] = []
    dimensions: list[dict[str, Any]] = []
    selected: dict[str, list[str]] = {}
    grain = ""
    trend = False
    superlative = False
    time_field = ""
    for index, (start, end, kind, target) in enumerate(found):
        word = text[start:end]
        if kind == "metric" and target not in metrics:
            metrics.append(target)
        elif kind == "value":
            field_name, _, value = target.partition("\x1f")
            selected.setdefault(field_name, [])
            if value not in selected[field_name]:
                selected[field_name].append(value)
        elif kind == "dimension":
            # 华东地区 / 地区为华东: the word names the filter, it does not ask to group.
            neighbours = found[max(0, index - 1):index] + found[index + 1:index + 2]
            close = [item for item in neighbours if item[2] == "value" and item[3].partition("\x1f")[0] == target and (item[1] == start or text[end:item[0]] in ("", "为", "是", "属于", "等于"))]
            if not close and not any(item["field"] == target for item in dimensions):
                dimensions.append({"field": target})
        elif kind == "time_field":
            time_field = target
        elif kind.startswith("grain:"):
            grain = kind.split(":", 1)[1]
        elif kind == "trend":
            trend = True
        elif kind == "desc":
            descending = True if descending is None else descending
            superlative = superlative or word in SUPERLATIVE
        elif kind == "asc":
            descending = False if descending is None else descending
            superlative = superlative or word in SUPERLATIVE

    score = len(metrics) * 2 + len(dimensions) + len(selected) + len(periods)
    if ambiguous:
        return Plan("unmapped", reason=f"“{'、'.join(ambiguous)}”在语义模型中对应多个含义。", unexplained=ambiguous, score=score)
    if unexplained:
        return Plan("unmapped", reason=f"“{'、'.join(unexplained)}”没有对应的语义定义。", unexplained=unexplained, score=score)
    if not metrics:
        return Plan("unmapped", reason="问题中没有识别到语义模型定义的指标。", score=score)
    if len(periods) > 1:
        return Plan("unmapped", reason="问题包含多个时间范围，需要进一步解读。", unexplained=[question], score=score)

    if trend and not grain:
        grain = "month"
    if (grain or periods) and not time_field:
        time_field = _default_time_field(model, metrics)
        if not time_field:
            return Plan("unmapped", reason=f"指标 {metrics[0]} 没有声明时间口径（ai_context.time_field），无法确定按哪个日期统计。", score=score)
    if grain:
        dimensions.insert(0, {"field": time_field, "grain": grain})
    elif time_field and not periods:
        dimensions.insert(0, {"field": time_field})
    filters: list[dict[str, Any]] = []
    for field_name, options in selected.items():
        filters.append({"field": field_name, "op": "=", "value": options[0]} if len(options) == 1 else {"field": field_name, "op": "in", "value": options})
        # 华东和华南的销售额: naming several values asks to see them side by side.
        if len(options) > 1 and not any(item["field"] == field_name for item in dimensions):
            dimensions.append({"field": field_name})
    for start, end in periods:
        filters.append({"field": time_field, "op": "between", "value": [start, end]})
    request: dict[str, Any] = {"metrics": metrics, "dimensions": dimensions, "filters": filters}
    if dimensions:
        if limit is None and superlative:
            limit = 1
        if descending is not None:
            request["order_by"] = [{"field": metrics[0], "desc": descending}]
        if limit:
            request["limit"] = limit
            if descending is None:
                request["order_by"] = [{"field": metrics[0], "desc": True}]
    elif limit or superlative:
        return Plan("unmapped", reason="问题要求排名，但没有指明按什么维度排名。", score=score)
    return Plan("ok", request=request, title=question.strip()[:80], steps=[describe(request, model)], planner="rules", score=score)


def describe(request: dict[str, Any], model: Model) -> str:
    def named(metric: str) -> str:
        definition = model.metrics[metric]
        return f"{metric}（{definition.description}）" if definition.description else metric

    parts = ["指标：" + "、".join(named(name) for name in request["metrics"])]
    if request.get("dimensions"):
        parts.append("维度：" + "、".join(f"{item['field']}" + (f" 按{item['grain']}" if item.get("grain") else "") for item in request["dimensions"]))
    if request.get("filters"):
        parts.append("筛选：" + "、".join(f"{item['field']} {item['op']} {item.get('value')}" for item in request["filters"]))
    if request.get("limit"):
        parts.append(f"取前 {request['limit']} 条")
    return f"映射到语义模型 {model.name}：" + "；".join(parts) + "。"


# ----- the language model --------------------------------------------------------------
PLAN_SYSTEM = """你是企业数据平台的语义层规划器。把用户问题映射到语义模型已定义的指标、维度和筛选，由语义层生成 SQL，所以你不写 SQL。
规则：
1. metrics 只能取“指标”一节列出的 name，dimensions 和 filters 的 field 只能取“维度”一节列出的 数据集.字段。不要虚构，也不要用相近的指标代替。
2. 用户说的业务概念如果在模型里没有定义（例如模型没有退款金额），status 返回 "unmapped"，在 reason 中说明缺少什么。绝不要用别的字段近似。
3. 问题能用已定义的指标回答、但需要语义层不支持的计算（环比、同比、占比、累计等），status 返回 "derived"，metrics 列出涉及的指标，reason 说明需要的计算。
4. 问题里的每一个条件都必须体现：地区、类别、状态等筛选值必须从“维度”一节的取值中原样选择；时间范围用 between，取值为 YYYY-MM-DD；“今年”“最近三个月”等相对时间按给出的今天日期换算。
5. 按时间聚合时在 dimensions 写 grain（day/week/month/quarter/year），并使用指标声明的时间口径字段。
6. 排名用 order_by（field 取指标 name，desc 表示降序）和 limit。不要加问题没有要求的筛选或维度。
7. 语义层支持按指标的聚合结果筛选（相当于 HAVING）：filters 的 field 可以取指标 name，例如“销售额大于 600 万”写成 {"field":"total_sales","op":">","value":6000000}。这种问题 status 是 "ok"，不是 "derived"。
8. 输出 JSON：status、reason、title（中文标题）、metrics、dimensions（field、grain）、filters（field、op、value）、order_by（field、desc）、limit、steps（中文分析思路，2-3 句）。"""
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "derived", "unmapped"]},
        "reason": {"type": "string"},
        "title": {"type": "string"},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "grain": {"type": "string"}}, "required": ["field", "grain"], "additionalProperties": False}},
        "filters": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "op": {"type": "string"}, "value": {}}, "required": ["field", "op", "value"], "additionalProperties": False}},
        "order_by": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "desc": {"type": "boolean"}}, "required": ["field", "desc"], "additionalProperties": False}},
        "limit": {"type": "integer"},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "reason", "title", "metrics", "dimensions", "filters", "order_by", "limit", "steps"],
    "additionalProperties": False,
}


def catalog_text(model: Model, values: dict[str, list[str]], hidden: set[str] | None = None) -> str:
    """What the language model may choose from: definitions, not table structures."""
    hidden = hidden or set()
    lines = [f"语义模型：{model.name}" + (f"（{model.description}）" if model.description else ""), "指标："]
    for metric in model.metrics.values():
        synonyms = f"；又称 {'、'.join(metric.synonyms)}" if metric.synonyms else ""
        time_note = f"；时间口径 {metric.time_field}" if metric.time_field else ""
        lines.append(f"- {metric.name}：{metric.description or '（无说明）'}{synonyms}{time_note}")
    lines.append("维度：")
    for dataset in model.datasets.values():
        for item in dataset.fields.values():
            if not item.is_dimension:
                continue
            target = f"{dataset.name}.{item.name}"
            if target in hidden:
                continue
            notes = []
            if item.is_time:
                notes.append("时间")
            if item.description:
                notes.append(item.description)
            if item.synonyms:
                notes.append("又称 " + "、".join(item.synonyms))
            options = values.get(target) or []
            if options and not SENSITIVE_COLUMN.search(item.name):
                notes.append("取值：" + "、".join(options[:MAX_VALUES_SHOWN]) + ("等" if len(options) > MAX_VALUES_SHOWN else ""))
            lines.append(f"- {target}" + (f"（{'；'.join(notes)}）" if notes else ""))
    return "\n".join(lines)


def check_plan(data: dict[str, Any], question: str, model: Model, values: dict[str, list[str]]) -> tuple[dict[str, Any], list[str]]:
    """Normalize a model's plan, reporting anything the answer could not be trusted with."""
    problems: list[str] = []
    fields = {f"{dataset.name}.{item.name}": item for dataset in model.datasets.values() for item in dataset.fields.values()}
    metrics = [str(name) for name in data.get("metrics") or []]
    for name in metrics:
        if name not in model.metrics:
            problems.append(f"指标 {name} 不在语义模型中。")
    if not metrics:
        problems.append("没有选择指标。")
    dimensions = []
    for item in data.get("dimensions") or []:
        target = str((item or {}).get("field") or "")
        grain = str((item or {}).get("grain") or "").lower()
        if target not in fields:
            problems.append(f"维度 {target} 不在语义模型中。")
            continue
        if grain and grain not in GRAINS:
            problems.append(f"时间粒度 {grain} 无效。")
            continue
        if grain and not fields[target].is_time:
            problems.append(f"维度 {target} 不是时间字段，不能按 {grain} 聚合。")
            continue
        dimensions.append({"field": target, **({"grain": grain} if grain else {})})
    filters = []
    for item in data.get("filters") or []:
        target = str((item or {}).get("field") or "")
        operator = str((item or {}).get("op") or "=").lower()
        value = (item or {}).get("value")
        if operator not in OPERATORS:
            problems.append(f"筛选比较方式 {operator} 无效。")
            continue
        if target in model.metrics:
            filters.append({"field": target, "op": operator, "value": value})
            continue
        if target not in fields:
            problems.append(f"筛选字段 {target} 不在语义模型中。")
            continue
        known = values.get(target)
        if known and operator in {"=", "!=", "in", "not_in"}:
            given = value if isinstance(value, list) else [item.strip() for item in str(value or "").split(",") if item.strip()] if operator in {"in", "not_in"} else [value]
            matched = []
            for candidate in given:
                text = normalize(str(candidate))
                hit = next((option for option in known if normalize(option) == text), None)
                near = [option for option in known if normalize(option) in text or text in normalize(option)] if hit is None and text else []
                if hit is None and len(near) == 1:
                    hit = near[0]
                if hit is None:
                    problems.append(f"筛选值 {candidate} 不是 {target} 的实际取值（{'、'.join(known[:MAX_VALUES_SHOWN])}）。")
                else:
                    matched.append(hit)
            value = matched if isinstance(value, list) or operator in {"in", "not_in"} else (matched[0] if matched else value)
        elif operator == "like" and known:
            problems.append(f"{target} 有明确取值，请用等值筛选而不是 like。")
        if fields[target].is_time and operator == "between":
            bounds = value if isinstance(value, list) else []
            if len(bounds) != 2 or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(bound)) for bound in bounds):
                problems.append(f"时间范围 {value} 必须是两个 YYYY-MM-DD。")
        filters.append({"field": target, "op": operator, "value": value})

    # The question's own literals have to survive into the plan.
    text = normalize(question)
    for target, options in values.items():
        named = [option for option in options if normalize(option) in text]
        if named and not any(item["field"] == target for item in filters):
            problems.append(f"问题提到了“{'、'.join(named)}”，但计划没有按 {target} 筛选。")
    time_filters = " ".join(str(item.get("value")) for item in filters if item["field"] in fields and fields[item["field"]].is_time)
    for year in re.findall(r"(\d{4})年", text):
        if year not in time_filters:
            problems.append(f"问题限定了 {year} 年，但计划没有对应的时间筛选。")
    top = _TOP.search(text)
    limit = data.get("limit")
    if top and limit != _number(top.group(1)):
        problems.append(f"问题要求前 {_number(top.group(1))} 名，但计划的 limit 是 {limit}。")

    request: dict[str, Any] = {"metrics": metrics, "dimensions": dimensions, "filters": filters}
    orders = [{"field": str(item.get("field")), "desc": bool(item.get("desc"))} for item in data.get("order_by") or [] if isinstance(item, dict) and item.get("field")]
    if orders:
        request["order_by"] = orders
    if isinstance(limit, int) and limit > 0:
        request["limit"] = limit
    return request, problems


def plan_with_model(
    generator: Any,
    question: str,
    model: Model,
    values: dict[str, list[str]],
    *,
    today: dt.date | None = None,
    hidden: set[str] | None = None,
    examples: list[dict[str, Any]] | None = None,
    glossary: str = "",
) -> Plan:
    today = today or dt.date.today()
    sections = [f"今天日期：{today.isoformat()}", catalog_text(model, values, hidden)]
    if glossary:
        sections.append(glossary)
    if examples:
        # Confirmed by a person, so these settle what the company's words mean.
        sections.append("已由人工确认的相似问题，映射方式应与之保持一致：\n" + "\n".join(
            f"- 问题：{item['question']}\n  映射：{json.dumps({key: value for key, value in item['definition']['request'].items() if value}, ensure_ascii=False)}"
            for item in examples
        ))
    prompt = "\n\n".join([*sections, f"用户问题：{question.strip()[:2000]}"])
    feedback = ""
    problems: list[str] = []
    for attempt in range(2):
        try:
            data = generator.complete_json(prompt + feedback, system=PLAN_SYSTEM, schema=PLAN_SCHEMA)
        except LlmError as error:
            # Some services occasionally return text that is not valid JSON; once is worth repeating.
            if attempt == 0 and "JSON" in str(error):
                continue
            raise
        status = str(data.get("status") or "")
        reason = str(data.get("reason") or "").strip()[:400]
        steps = [str(item).strip() for item in (data.get("steps") or []) if str(item).strip()][:4]
        if status == "unmapped":
            return Plan("unmapped", reason=reason or "模型判断语义模型没有这个口径。", planner="llm", steps=steps)
        if status == "derived":
            return Plan("derived", reason=reason, planner="llm", steps=steps, request={"metrics": [str(item) for item in data.get("metrics") or [] if str(item) in model.metrics]})
        if status != "ok":
            return Plan("unmapped", reason="模型没有返回有效的映射结果。", planner="llm")
        request, problems = check_plan(data, question, model, values)
        if not problems:
            title = str(data.get("title") or "").strip()[:80] or question.strip()[:80]
            return Plan("ok", request=request, title=title, steps=[*steps, describe(request, model)], planner="llm")
        if attempt == 0:
            feedback = "\n\n你上一次的结果：" + json.dumps(data, ensure_ascii=False)[:2000] + "\n存在以下问题，请修正后重新输出：\n- " + "\n- ".join(problems)
    return Plan("unmapped", reason="模型的映射未通过校验：" + "；".join(problems)[:600], planner="llm")
