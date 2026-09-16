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

"""The registry of schedulable tasks behind 质量调度管理.

The scheduler used to know exactly one job, ``QualityTask.run``. Everything else
that had to happen on a clock grew its own daemon thread, which meant every new
background job also needed its own retry rule, its own history and its own place
in the UI. This module inverts that: a task is a name, a label and a callable,
and the scheduler dispatches whatever is registered under the name a schedule
carries. Registration happens in the application lifespan, so this module
imports none of the services it schedules and stays testable with plain fakes.

A task callable receives the schedule's ``method_params`` string and a context
dict (trigger, schedule id, attempt number, planned firing time) and returns a
JSON-safe summary. It reports failure by raising; the scheduler turns that into
a retry, a run record and a message on the 调度 screen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Bean and method of the built-in quality job, kept for schedules stored before
#: the registry existed.
QUALITY_TASK = ("QualityTask", "run")
MAX_PARAMS = 500


class TaskError(ValueError):
    """A user-facing task error; the message is Chinese and safe to display."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TaskDefinition:
    """One dispatchable job: how it is named, described and invoked."""

    bean: str
    method: str
    label: str
    run: Callable[[str, dict[str, Any]], Any]
    description: str = ""
    params_label: str = "方法参数"
    params_hint: str = "该任务不需要参数。"
    params_required: bool = False
    category: str = "平台"

    @property
    def key(self) -> str:
        return f"{self.bean}.{self.method}"

    def view(self) -> dict[str, Any]:
        """What the 调度 form needs to render this task as a choice."""
        return {
            "key": self.key,
            "bean_name": self.bean,
            "method_name": self.method,
            "label": self.label,
            "description": self.description,
            "params_label": self.params_label,
            "params_hint": self.params_hint,
            "params_required": self.params_required,
            "category": self.category,
        }


@dataclass
class TaskRegistry:
    """Name to task. Registration is idempotent: the last definition wins."""

    _tasks: dict[str, TaskDefinition] = field(default_factory=dict)

    def register(self, definition: TaskDefinition) -> TaskDefinition:
        self._tasks[definition.key] = definition
        return definition

    def add(self, bean: str, method: str, label: str, run: Callable[[str, dict[str, Any]], Any], **extra: Any) -> TaskDefinition:
        return self.register(TaskDefinition(bean=bean, method=method, label=label, run=run, **extra))

    def keys(self) -> list[str]:
        return sorted(self._tasks)

    def has(self, bean: Any, method: Any) -> bool:
        return f"{bean}.{method}" in self._tasks

    def find(self, bean: Any, method: Any) -> TaskDefinition | None:
        return self._tasks.get(f"{bean}.{method}")

    def get(self, bean: Any, method: Any) -> TaskDefinition:
        task = self.find(bean, method)
        if task is None:
            known = "、".join(self.keys()) or "（无）"
            raise TaskError(400, f"未注册的调度任务：{bean}.{method}。可用任务：{known}。")
        return task

    def options(self) -> list[dict[str, Any]]:
        return [self._tasks[key].view() for key in self.keys()]

    def label_of(self, bean: Any, method: Any) -> str:
        task = self.find(bean, method)
        return task.label if task else f"{bean}.{method}"

    def check_params(self, task: TaskDefinition, params: Any) -> str:
        text = str(params or "").strip()
        if len(text) > MAX_PARAMS:
            raise TaskError(400, f"方法参数不能超过 {MAX_PARAMS} 个字符。")
        if task.params_required and not text:
            raise TaskError(400, f"任务「{task.label}」需要填写{task.params_label}：{task.params_hint}")
        return text

    def run(self, bean: Any, method: Any, params: Any = "", context: dict[str, Any] | None = None) -> Any:
        task = self.get(bean, method)
        return task.run(self.check_params(task, params), dict(context or {}))


def summarize(outcome: Any) -> str:
    """One line describing a task's result, for the run record and the UI."""
    if isinstance(outcome, str):
        return outcome[:400]
    if not isinstance(outcome, dict):
        return ""
    for key in ("summary", "message", "detail"):
        if isinstance(outcome.get(key), str) and outcome[key].strip():
            return outcome[key].strip()[:400]
    parts: list[str] = []
    labels = {
        "total": "共",
        "passed": "通过",
        "failed": "未通过",
        "services": "服务",
        "tables": "数据表",
        "columns": "字段",
        "created": "新增",
        "updated": "更新",
        "deleted": "下线",
        "views": "视图",
        "edges": "血缘",
        "errors": "错误",
        "assets": "资产",
        "removed": "清理",
        "entries": "条目",
    }
    for key, label in labels.items():
        value = outcome.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            parts.append(f"{label} {value:g}" if isinstance(value, float) else f"{label} {value}")
        elif isinstance(value, list):
            parts.append(f"{label} {len(value)}")
    return "，".join(parts)[:400]


def json_detail(outcome: Any) -> dict[str, Any]:
    """The part of a task result worth storing: small, flat and JSON-safe."""
    if not isinstance(outcome, dict):
        return {}
    detail: dict[str, Any] = {}
    for key, value in outcome.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            detail[key] = value[:400] if isinstance(value, str) else value
        elif isinstance(value, list):
            detail[f"{key}_count"] = len(value)
        if len(detail) >= 24:
            break
    return detail


def register_platform_tasks(
    registry: TaskRegistry,
    *,
    quality: Any = None,
    ingestor: Any = None,
    insights: Any = None,
    lineage: Any = None,
    cache: Any = None,
) -> TaskRegistry:
    """Register the jobs the platform ships with; absent services are skipped.

    Every callable here is a thin adapter: it reads the schedule's parameter
    string, calls one service method and returns that method's own summary.
    """
    if quality is not None:
        registry.add(
            "QualityTask",
            "run",
            "质量核查全量执行",
            lambda params, context: quality.run_enabled(
                trigger=str(context.get("trigger") or "schedule"), schedule_id=context.get("schedule_id")
            ),
            description="执行全部启用的核查规则，并写入执行日志与质量报告。",
            params_hint="该任务不需要参数。",
            category="数据质量",
        )
    if ingestor is not None:
        registry.add(
            "MetadataTask",
            "ingest",
            "元数据拾取",
            lambda params, context: _ingest(ingestor, params, context),
            description="拾取一个服务的库表与字段；留空则拾取所有到期的服务。",
            params_label="服务 FQN",
            params_hint="填写服务的完全限定名，例如 mysql-local；留空表示按各服务自己的周期拾取到期的服务。",
            category="元数据",
        )
    if insights is not None:
        registry.add(
            "MetadataTask",
            "snapshot",
            "元数据洞察快照",
            lambda params, context: insights.snapshot(params or None),
            description="写入当天的目录健康快照，供洞察趋势与 KPI 使用。",
            params_label="日期",
            params_hint="可填 YYYY-MM-DD 指定补写某一天；留空表示当天。",
            category="元数据",
        )
    if lineage is not None:
        registry.add(
            "MetadataTask",
            "syncViews",
            "视图血缘同步",
            lambda params, context: lineage.sync_views(params or None),
            description="解析视图定义，刷新视图与来源表之间的血缘。",
            params_label="服务 FQN",
            params_hint="填写服务的完全限定名；留空表示全部服务。",
            category="元数据",
        )
    if cache is not None:
        registry.add(
            "QueryCacheTask",
            "sweep",
            "清理过期查询缓存",
            lambda params, context: cache.sweep(),
            description="移除已过期的查询结果缓存条目，释放内存。",
            category="查询",
        )
        registry.add(
            "QueryCacheTask",
            "clear",
            "清空查询缓存",
            lambda params, context: {"removed": cache.invalidate(params or None), **cache.stats()},
            description="立即让缓存的查询结果失效；填写数据源 ID 时只清空该数据源。",
            params_label="数据源 ID",
            params_hint="可选。留空表示清空全部数据源的缓存结果。",
            category="查询",
        )
    return registry


def _ingest(ingestor: Any, params: str, context: dict[str, Any]) -> dict[str, Any]:
    trigger = str(context.get("trigger") or "scheduled")
    if params:
        run = ingestor.run(params, trigger=trigger)
        return {"services": 1, "status": run.get("status"), **(run.get("summary") or {})}
    runs = [ingestor.run(row["fqn"], trigger=trigger) for row in ingestor.due_services()]
    totals: dict[str, Any] = {"services": len(runs)}
    for run in runs:
        for key, value in (run.get("summary") or {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals
