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

"""Engine verdicts, execution bookkeeping, cron scheduling and the HTTP routes.

Everything runs against fakes: a store that records rows in memory and a
connector that answers the two generated aggregates. The properties under test
are the ones a live database cannot demonstrate any better — that an execution
row always reaches a terminal status, that the operator × formula matrix is
exhaustive, that one broken schedule neither fires twice nor kills the thread,
and that both cron dialects of the 调度 screen are understood.
"""

import datetime as dt
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapi import quality_api
from webapi.connectors import ConnectorError
from webapi.datasources import DataSourceError
from webapi.quality_service import (
    OPERATORS,
    RESULT_FORMULAS,
    SCAN_LIMIT_NOTE,
    QualityError,
    QualityScheduler,
    QualityService,
    cron_fields,
    next_fire_time,
)
from webapi.quality_metrics import MetricError
from webapi.quality_store import DIMENSION_LABELS, QualityStoreError
from webapi.tasks import TaskError, TaskRegistry, register_platform_tasks

BASE = dt.datetime(2026, 9, 12, 11, 59, 30)
SOURCE_ID = "quality-postgres"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeConnector:
    """Answers the generated aggregates without touching an engine."""

    dialect = "postgres"
    type_id = "postgresql"

    def __init__(self, checked=100, actual=4, error=None, sample=None):
        self.checked = checked
        self.actual = actual
        self.error = error
        self.sample = sample or (["order_id"], [[7], [9]])
        self.queries = []

    def default_schema(self):
        return "public"

    def query(self, sql, limit=None):
        self.queries.append((sql, limit))
        if self.error is not None:
            raise self.error
        if "checked_count" in sql:
            return self._answer(["checked_count"], [[self.checked]], sql)
        if "actual_value" in sql:
            return self._answer(["actual_value"], [[self.actual]], sql)
        columns, rows = self.sample
        return self._answer(columns, rows, sql)

    @staticmethod
    def _answer(columns, rows, sql):
        return {
            "columns": columns,
            "rows": rows,
            "truncated": False,
            "sql": sql,
            "elapsed_ms": 1.0,
        }


class LakeConnector(FakeConnector):
    """Iceberg speaks DuckDB but scans at most 500 000 rows."""

    dialect = "duckdb"
    type_id = "iceberg"


class FakeSources:
    def __init__(self, connector=None, known=(SOURCE_ID,)):
        self.connector_value = connector or FakeConnector()
        self.known = set(known)

    def record(self, source_id):
        if source_id not in self.known:
            raise DataSourceError(404, "数据源不存在。")
        return {"id": source_id, "name": "业务库 PostgreSQL", "type": "postgresql"}

    def connector(self, source_id):
        self.record(source_id)
        return self.connector_value


class FakeStore:
    """In-memory stand-in with the real store's signatures and 404 behaviour."""

    def __init__(self):
        self.rules = {}
        self.schedules = {}
        self.executions = {}
        self.results = []
        self.counters = {"rule": 0, "schedule": 0, "execution": 0, "result": 0, "task_run": 0, "template": 0}
        self.failure = None
        self.task_runs = {}
        self.templates = {}

    # ----- helpers ----------------------------------------------------------
    def _claim(self, kind):
        self.counters[kind] += 1
        return self.counters[kind]

    @staticmethod
    def _identifier(value, label):
        try:
            identifier = int(value)
        except (TypeError, ValueError) as error:
            raise QualityStoreError(400, f"{label}无效。") from error
        if identifier <= 0:
            raise QualityStoreError(400, f"{label}无效。")
        return identifier

    def _check(self):
        if self.failure is not None:
            raise self.failure

    @staticmethod
    def _moment(value):
        if isinstance(value, dt.datetime):
            return value.isoformat()
        return value or None

    # ----- lifecycle --------------------------------------------------------
    def healthy(self):
        self._check()
        return {
            "ok": True,
            "detail": "质量元数据库连接正常。",
            "database": "blog_converter",
            "schema": "lattice_quality",
        }

    # ----- rules ------------------------------------------------------------
    def list_rules(self, dimension=None, name=None, page=1, size=20):
        self._check()
        items = [
            row
            for row in self.rules.values()
            if (not dimension or row["dimension"] == dimension)
            and (not name or name in row["name"])
        ]
        return {"items": items, "total": len(items)}

    def get_rule(self, rule_id):
        self._check()
        row = self.rules.get(self._identifier(rule_id, "规则编号"))
        if row is None:
            raise QualityStoreError(404, "核查规则不存在。")
        return dict(row)

    def create_rule(self, data):
        self._check()
        row = {**data, "id": self._claim("rule"), "create_time": "2026-09-12T08:30:00"}
        # The real store defaults a missing rule state to 启用 and a schedule to 停止.
        row["state"] = 1 if data.get("state") is None else int(data["state"])
        self.rules[row["id"]] = row
        return dict(row)

    def update_rule(self, rule_id, data):
        identifier = self._identifier(rule_id, "规则编号")
        stored = self.get_rule(identifier)
        row = {**stored, **data, "id": identifier}
        self.rules[identifier] = row
        return dict(row)

    def delete_rule(self, rule_id):
        identifier = self._identifier(rule_id, "规则编号")
        self.get_rule(identifier)
        self.rules.pop(identifier)
        return {"id": identifier, "deleted": True}

    def enabled_rules(self):
        self._check()
        return [dict(row) for row in self.rules.values() if row.get("state") == 1]

    # ----- schedules --------------------------------------------------------
    def list_schedules(self, name=None, page=1, size=20):
        self._check()
        items = [row for row in self.schedules.values() if not name or name in row["name"]]
        return {"items": items, "total": len(items)}

    def get_schedule(self, schedule_id):
        self._check()
        row = self.schedules.get(self._identifier(schedule_id, "调度编号"))
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return dict(row)

    def create_schedule(self, data):
        self._check()
        row = {
            "bean_name": "QualityTask",
            "method_name": "run",
            "method_params": "",
            "state": 0,
            "last_fire_time": None,
            "next_fire_time": None,
            **data,
            "id": self._claim("schedule"),
        }
        row["state"] = 0 if data.get("state") is None else int(data["state"])
        self.schedules[row["id"]] = row
        return dict(row)

    def update_schedule(self, schedule_id, data):
        identifier = self._identifier(schedule_id, "调度编号")
        stored = self.get_schedule(identifier)
        row = {**stored, **data, "id": identifier}
        self.schedules[identifier] = row
        return dict(row)

    def delete_schedule(self, schedule_id):
        identifier = self._identifier(schedule_id, "调度编号")
        self.get_schedule(identifier)
        self.schedules.pop(identifier)
        return {"id": identifier, "deleted": True}

    def set_schedule_state(self, schedule_id, state, next_fire_time=None):
        identifier = self._identifier(schedule_id, "调度编号")
        row = self.get_schedule(identifier)
        row["state"] = int(state)
        row["next_fire_time"] = self._moment(next_fire_time)
        self.schedules[identifier] = row
        return dict(row)

    def mark_fired(self, schedule_id, last, next):
        identifier = self._identifier(schedule_id, "调度编号")
        row = self.get_schedule(identifier)
        row["last_fire_time"] = self._moment(last)
        row["next_fire_time"] = self._moment(next)
        self.schedules[identifier] = row
        return dict(row)

    def running_schedules(self):
        self._check()
        return [dict(row) for row in self.schedules.values() if row.get("state") == 1]

    # ----- executions -------------------------------------------------------
    # ----- templates --------------------------------------------------------
    def count_templates(self):
        self._check()
        return len(self.templates)

    def list_templates(self, name=None, metric=None, page=1, size=20):
        self._check()
        items = [dict(row) for row in self.templates.values() if (not name or name in row["name"]) and (not metric or row["metric"] == metric)]
        return {"items": items, "total": len(items)}

    def get_template(self, template_id):
        self._check()
        row = self.templates.get(self._identifier(template_id, "模板编号"))
        if row is None:
            raise QualityStoreError(404, "规则模板不存在。")
        return dict(row)

    def create_template(self, data):
        self._check()
        row = {"description": "", "level": "medium", "config": {}, "expected_type": "fix_value", "result_formula": "actual", "operator": "lte", "threshold": 0, "builtin": 0, **data, "id": self._claim("template")}
        self.templates[row["id"]] = row
        return dict(row)

    def update_template(self, template_id, data):
        identifier = self._identifier(template_id, "模板编号")
        row = {**self.get_template(identifier), **data, "id": identifier}
        self.templates[identifier] = row
        return dict(row)

    def delete_template(self, template_id):
        identifier = self._identifier(template_id, "模板编号")
        self.get_template(identifier)
        self.templates.pop(identifier)
        return {"id": identifier, "deleted": True}

    def record_schedule_outcome(self, schedule_id, status, message=""):
        identifier = self._identifier(schedule_id, "调度编号")
        row = self.get_schedule(identifier)
        row["last_status"] = status
        row["last_message"] = message
        self.schedules[identifier] = row
        return dict(row)

    # ----- task runs --------------------------------------------------------
    def start_task_run(self, *, schedule_id=None, schedule_name="", task="", task_label="", trigger_type="schedule", attempt=1, planned_time=None, message=""):
        self._check()
        identifier = self._claim("task_run")
        self.task_runs[identifier] = {
            "id": identifier,
            "schedule_id": schedule_id,
            "schedule_name": schedule_name,
            "task": task,
            "task_label": task_label,
            "trigger_type": trigger_type,
            "attempt": attempt,
            "status": "running",
            "planned_time": self._moment(planned_time),
            "start_time": "2026-09-12T12:00:00",
            "end_time": None,
            "elapsed_ms": None,
            "message": message,
            "detail": {},
        }
        return identifier

    def finish_task_run(self, run_id, status, elapsed_ms=None, message="", detail=None):
        self._check()
        row = self.task_runs.get(self._identifier(run_id, "执行编号"))
        if row is None:
            raise QualityStoreError(404, "任务执行记录不存在。")
        row.update({"status": status, "end_time": "2026-09-12T12:00:01", "elapsed_ms": elapsed_ms, "message": message, "detail": detail or {}})
        return dict(row)

    def list_task_runs(self, schedule_id=None, status=None, task=None, page=1, size=20):
        self._check()
        items = [
            dict(row)
            for row in sorted(self.task_runs.values(), key=lambda row: -row["id"])
            if (schedule_id in (None, "") or str(row["schedule_id"]) == str(schedule_id))
            and (not status or row["status"] == status)
            and (not task or row["task"] == task)
        ]
        return {"items": items, "total": len(items)}

    def get_task_run(self, run_id):
        row = self.task_runs.get(self._identifier(run_id, "执行编号"))
        if row is None:
            raise QualityStoreError(404, "任务执行记录不存在。")
        return dict(row)

    def start_execution(
        self, rule_id=None, rule_name="", trigger_type="manual", schedule_id=None, message=""
    ):
        self._check()
        identifier = self._claim("execution")
        self.executions[identifier] = {
            "id": identifier,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "schedule_id": schedule_id,
            "trigger_type": trigger_type,
            "status": 0,
            "start_time": "2026-09-12T12:00:00",
            "end_time": None,
            "elapsed_ms": None,
            "message": message,
        }
        return identifier

    def finish_execution(self, execution_id, status, elapsed_ms=None, message=""):
        self._check()
        identifier = self._identifier(execution_id, "执行编号")
        row = self.executions.get(identifier)
        if row is None:
            raise QualityStoreError(404, "执行记录不存在。")
        row.update(
            {
                "status": int(status),
                "elapsed_ms": elapsed_ms,
                "message": message,
                "end_time": "2026-09-12T12:00:01",
            }
        )
        return dict(row)

    def record_result(self, execution_id, result_row):
        self._check()
        row = {
            **result_row,
            "id": self._claim("result"),
            "job_execution_id": self._identifier(execution_id, "执行编号"),
            "check_time": "2026-09-12T12:00:01",
        }
        self.results.append(row)
        return dict(row)

    def list_executions(self, status=None, page=1, size=20):
        self._check()
        items = [
            row for row in self.executions.values() if status is None or row["status"] == status
        ]
        return {"items": items, "total": len(items)}

    def get_execution(self, execution_id):
        self._check()
        identifier = self._identifier(execution_id, "执行编号")
        row = self.executions.get(identifier)
        if row is None:
            raise QualityStoreError(404, "执行记录不存在。")
        return {**row, "results": self.results_for_execution(identifier)}

    def results_for_execution(self, execution_id):
        identifier = self._identifier(execution_id, "执行编号")
        return [dict(row) for row in self.results if row["job_execution_id"] == identifier]

    # ----- analysis ---------------------------------------------------------
    def latest_results(self, name=None):
        self._check()
        return [dict(row) for row in reversed(self.results) if not name or name in row["rule_name"]]

    def dimension_error_counts(self):
        self._check()
        return {dimension: 0 for dimension in DIMENSION_LABELS}

    def report(self, date=None):
        self._check()
        return {
            "date": date or "2026-09-12",
            "datasource_errors": [],
            "rule_errors": [],
            "dimension_sections": [],
            "total_checked": 0,
            "total_errors": 0,
            "score": 100.0,
            "level_label": "优秀",
        }


class RecordingService:
    """A QualityService stand-in that reports whether two firings overlapped."""

    def __init__(self, gate=None):
        self.gate = gate
        self.entered = threading.Event()
        self.calls = []
        self.overlapped = False
        self._lock = threading.Lock()
        self._inside = 0

    def run_enabled(self, *, trigger="manual", schedule_id=None):
        with self._lock:
            self.calls.append((trigger, schedule_id))
            self._inside += 1
            if self._inside > 1:
                self.overlapped = True
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        with self._lock:
            self._inside -= 1
        return {"total": 0, "passed": 0, "failed": 0, "errors": [], "items": []}


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------
def make_rule(**overrides):
    rule = {
        "id": 7,
        "name": "订单号唯一",
        "metric": "column_duplicate",
        "dimension": "uniqueness",
        "level": "high",
        "datasource_id": SOURCE_ID,
        "datasource_name": "业务库 PostgreSQL",
        "schema_name": "public",
        "table_name": "orders",
        "column_name": "order_id",
        "config": {"filter": ""},
        "expected_type": "fix_value",
        "result_formula": "actual",
        "operator": "lte",
        "threshold": 0,
        "state": 1,
        "comment": "",
    }
    rule.update(overrides)
    return rule


def make_schedule(**overrides):
    schedule = {
        "id": 1,
        "name": "每日质量核查",
        "bean_name": "QualityTask",
        "method_name": "run",
        "method_params": "",
        "cron_expression": "0 0 12 * * ?",
        "state": 1,
        "last_fire_time": None,
        "next_fire_time": None,
    }
    schedule.update(overrides)
    return schedule


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def connector():
    return FakeConnector()


@pytest.fixture
def service(store, connector):
    return QualityService(store, FakeSources(connector))


def statuses(store):
    return [row["status"] for row in store.executions.values()]


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------
def test_cron_reads_quartz_six_fields_and_unix_five_fields():
    assert cron_fields("0 0 12 * * ?") == ["0", "0", "12", "*", "*", "*"]
    assert next_fire_time("0 0 12 * * ?", BASE) == dt.datetime(2026, 9, 12, 12, 0, 0)
    assert next_fire_time("0 12 * * *", BASE) == dt.datetime(2026, 9, 12, 12, 0, 0)
    # Seconds first: the 6-field form must not be read as "seconds last".
    assert next_fire_time("*/5 * * * * ?", BASE) == dt.datetime(2026, 9, 12, 11, 59, 35)
    assert next_fire_time("  0   0  12 ? * *  ", BASE) == dt.datetime(2026, 9, 12, 12, 0, 0)


@pytest.mark.parametrize(
    "expression",
    ["", "   ", "每天中午", "1 2 3 4", "0 0 12 * * ? 2026", "99 0 12 * * ?", "0 0 12 * * ?" * 20],
)
def test_cron_rejects_unusable_expressions(expression):
    with pytest.raises(QualityError) as error:
        next_fire_time(expression, BASE)
    assert error.value.status_code == 400
    assert str(error.value).endswith("。")


# ---------------------------------------------------------------------------
# Verdict matrix
# ---------------------------------------------------------------------------
VERDICTS = [
    ("actual", "eq", 4, 1),
    ("actual", "ne", 4, 2),
    ("actual", "lt", 4, 2),
    ("actual", "lte", 4, 1),
    ("actual", "gt", 4, 2),
    ("actual", "gte", 4, 1),
    ("actual", "eq", 5, 2),
    ("actual", "ne", 5, 1),
    ("actual", "lt", 5, 1),
    ("actual", "lte", 5, 1),
    ("actual", "gt", 5, 2),
    ("actual", "gte", 5, 2),
    ("percentage", "eq", 2, 1),
    ("percentage", "ne", 2, 2),
    ("percentage", "lt", 2, 2),
    ("percentage", "lte", 2, 1),
    ("percentage", "gt", 2, 2),
    ("percentage", "gte", 2, 1),
    ("percentage", "eq", 3, 2),
    ("percentage", "ne", 3, 1),
    ("percentage", "lt", 3, 1),
    ("percentage", "lte", 3, 1),
    ("percentage", "gt", 3, 2),
    ("percentage", "gte", 3, 2),
]


def test_verdict_matrix_covers_every_operator_and_formula():
    assert {formula for formula, _, _, _ in VERDICTS} == set(RESULT_FORMULAS)
    assert {operator for _, operator, _, _ in VERDICTS} == set(OPERATORS)


@pytest.mark.parametrize("formula, operator, threshold, expected", VERDICTS)
def test_verdict_compares_the_formula_against_the_threshold(
    store, formula, operator, threshold, expected
):
    # 4 non-conforming rows out of 200 is an actual value of 4 and 2 percent,
    # so the two formulas cannot accidentally agree.
    service = QualityService(store, FakeSources(FakeConnector(checked=200, actual=4)))
    outcome = service.run_rule(
        make_rule(result_formula=formula, operator=operator, threshold=threshold)
    )
    assert outcome["result"]["state"] == expected
    assert outcome["result"]["state_label"] == ("成功" if expected == 1 else "失败")
    # 不合规数量 is the row count whatever the formula; only the compared value
    # differs, so a percentage rule never turns the count column into a percent.
    assert outcome["result"]["actual_value"] == 4
    assert outcome["result"]["checked_count"] == 200
    assert outcome["result"]["score"] == 98.0
    assert statuses(store) == [1]


def test_score_and_expected_value_follow_the_single_definition(store):
    service = QualityService(store, FakeSources(FakeConnector(checked=200, actual=4)))
    outcome = service.run_rule(make_rule(expected_type="table_total_rows"))
    result = outcome["result"]
    assert result["checked_count"] == 200
    assert result["score"] == 98.0
    assert result["expected_value"] == 200
    assert outcome["execution"]["status_label"] == "成功"
    assert "得分 98.0。" in outcome["execution"]["message"]


@pytest.mark.parametrize("formula", sorted(RESULT_FORMULAS))
def test_an_empty_table_scores_full_marks_without_dividing_by_zero(store, formula):
    service = QualityService(store, FakeSources(FakeConnector(checked=0, actual=0)))
    outcome = service.run_rule(make_rule(result_formula=formula, operator="lte", threshold=0))
    assert outcome["result"]["actual_value"] == 0
    assert outcome["result"]["score"] == 100.0
    assert outcome["result"]["state"] == 1


def test_labels_promised_to_the_frontend_are_attached(store, service):
    outcome = service.run_rule(make_rule())
    result = outcome["result"]
    assert result["metric_label"] == "重复值检查"
    assert result["dimension_label"] == "唯一性校验"
    assert result["level_label"] == "高"
    assert outcome["execution"]["trigger_label"] == "手动"


def test_a_bounded_lake_scan_is_called_out_in_the_message(store):
    service = QualityService(store, FakeSources(LakeConnector(checked=10, actual=1)))
    outcome = service.run_rule(make_rule(schema_name="demo"))
    assert outcome["execution"]["message"].endswith(SCAN_LIMIT_NOTE)


# ---------------------------------------------------------------------------
# An execution row never stays in status 0
# ---------------------------------------------------------------------------
def test_a_connector_failure_finishes_the_execution_as_failed(store):
    service = QualityService(
        store, FakeSources(FakeConnector(error=ConnectorError("操作超过 30 秒未完成，已取消。")))
    )
    with pytest.raises(QualityError) as error:
        service.run_rule(make_rule())
    assert statuses(store) == [2]
    row = store.executions[1]
    assert row["end_time"] and row["elapsed_ms"] is not None
    assert "执行失败" in row["message"] and "已取消" in row["message"]
    assert str(error.value) == row["message"]
    assert store.results == []


def test_a_missing_datasource_finishes_the_execution_as_failed(store):
    service = QualityService(store, FakeSources(known=()))
    with pytest.raises(QualityError) as error:
        service.run_rule(make_rule())
    assert error.value.status_code == 404
    assert statuses(store) == [2]
    assert "数据源不存在" in store.executions[1]["message"]


def test_an_unusable_metric_config_finishes_the_execution_as_failed(store, service):
    with pytest.raises(QualityError) as error:
        service.run_rule(make_rule(metric="column_match_regex", dimension="accuracy", config={}))
    assert error.value.status_code == 400
    assert statuses(store) == [2]
    assert "正则表达式" in store.executions[1]["message"]


def test_an_unknown_operator_finishes_the_execution_as_failed(store, service):
    with pytest.raises(QualityError):
        service.run_rule(make_rule(operator="between"))
    assert statuses(store) == [2]
    assert "比较方式" in store.executions[1]["message"]


def test_a_non_numeric_aggregate_finishes_the_execution_as_failed(store):
    connector = FakeConnector()
    connector.checked = "未知"
    service = QualityService(store, FakeSources(connector))
    with pytest.raises(QualityError) as error:
        service.run_rule(make_rule())
    assert error.value.status_code == 502
    assert statuses(store) == [2]


def test_an_unexpected_driver_error_closes_the_execution_and_travels_on(store):
    service = QualityService(store, FakeSources(FakeConnector(error=RuntimeError("boom"))))
    with pytest.raises(RuntimeError):
        service.run_rule(make_rule())
    assert statuses(store) == [2]
    assert "boom" in store.executions[1]["message"]


def test_a_metadata_failure_while_recording_still_closes_the_execution(store, service):
    class FailingOnce(FakeStore):
        def record_result(self, execution_id, result_row):
            raise QualityStoreError(500, "质量元数据库操作失败：connection closed。")

    broken = FailingOnce()
    service = QualityService(broken, FakeSources(FakeConnector()))
    with pytest.raises(QualityError):
        service.run_rule(make_rule())
    assert statuses(broken) == [2]
    assert "写入失败" in broken.executions[1]["message"]


# ---------------------------------------------------------------------------
# Batch runs
# ---------------------------------------------------------------------------
def test_run_enabled_keeps_going_when_one_rule_cannot_run(store, connector):
    service = QualityService(store, FakeSources(connector))
    store.create_rule(make_rule(id=None, name="订单号唯一"))
    store.create_rule(make_rule(id=None, name="缺失数据源", datasource_id="gone"))
    store.create_rule(make_rule(id=None, name="已禁用", state=0))
    summary = service.run_enabled(trigger="schedule", schedule_id=3)
    assert summary["total"] == 2
    assert summary["passed"] + summary["failed"] == 1
    assert [item["rule_name"] for item in summary["errors"]] == ["缺失数据源"]
    assert sorted(statuses(store)) == [1, 2]
    assert {row["trigger_type"] for row in store.executions.values()} == {"schedule"}


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
def test_prepare_rule_normalises_and_proves_the_rule_can_run(service):
    prepared = service.prepare_rule(
        {
            "name": "邮编格式",
            "metric": "column_match_regex",
            "datasource_id": SOURCE_ID,
            "table_name": "customers",
            "column_name": "zip",
            "config": {"regexp": "^[0-9]{6}$"},
        }
    )
    assert prepared["dimension"] == "accuracy"
    assert prepared["level"] == "medium"
    assert prepared["operator"] == "lte"
    assert prepared["datasource_name"] == "业务库 PostgreSQL"
    assert prepared["config"] == {"regexp": "^[0-9]{6}$", "filter": ""}


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"metric": "column_unknown"}, "核查类型"),
        ({"dimension": "completeness"}, "规则分类不一致"),
        ({"dimension": "nonsense"}, "规则分类"),
        ({"level": "urgent"}, "规则级别"),
        ({"operator": "between"}, "比较方式"),
        ({"expected_type": "guess"}, "期望值类型"),
        ({"result_formula": "ratio"}, "结果计算方式"),
        ({"column_name": ""}, "核查字段"),
        ({"datasource_id": ""}, "数据源"),
        ({"config": {"filter": "1=1; DROP TABLE orders"}}, "过滤条件"),
    ],
)
def test_prepare_rule_rejects_unusable_values(service, patch, fragment):
    with pytest.raises((QualityError, ValueError)) as error:
        service.prepare_rule({**make_rule(), **patch})
    assert getattr(error.value, "status_code", 400) == 400
    assert fragment in str(error.value)
    assert str(error.value).endswith("。")


def test_prepare_schedule_accepts_only_the_builtin_task(service):
    prepared = service.prepare_schedule({"name": "每日", "cron_expression": " 0 0 12 * * ? "})
    assert prepared["bean_name"] == "QualityTask"
    assert prepared["method_name"] == "run"
    assert prepared["cron_expression"] == "0 0 12 * * ?"
    with pytest.raises(QualityError) as error:
        service.prepare_schedule(
            {"name": "外部", "cron_expression": "0 0 12 * * ?", "bean_name": "ShellTask"}
        )
    assert "QualityTask.run" in str(error.value)
    with pytest.raises(QualityError):
        service.prepare_schedule({"name": "坏的", "cron_expression": "每天中午"})


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
@pytest.fixture
def scheduler(store):
    recorder = RecordingService()
    value = QualityScheduler(store, recorder, interval=0.05)
    yield value
    value.stop(timeout=5)


def test_scheduler_fires_a_due_schedule_exactly_once(store, scheduler):
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?")
    noon = dt.datetime(2026, 9, 12, 12, 0, 0)
    assert scheduler.tick(noon) == []
    assert store.schedules[1]["next_fire_time"] == "2026-09-12T12:01:00"
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 1, 0)) == [1]
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 1, 1)) == []
    assert scheduler.service.calls == [("schedule", 1)]
    assert store.schedules[1]["last_fire_time"] == "2026-09-12T12:01:00"


def test_scheduler_skips_missed_firings_instead_of_catching_up(store, scheduler):
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?")
    scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0))
    late = dt.datetime(2026, 9, 12, 12, 5, 30)
    assert scheduler.tick(late) == [1]
    assert len(scheduler.service.calls) == 1
    # The next firing is computed from `late`, not from the four missed slots.
    assert store.schedules[1]["next_fire_time"] == "2026-09-12T12:06:00"
    assert scheduler.tick(late + dt.timedelta(seconds=1)) == []


def test_scheduler_ignores_a_stopped_schedule(store, scheduler):
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?", state=0)
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 1, 0)) == []
    assert scheduler.service.calls == []


def test_scheduler_stops_a_broken_cron_and_keeps_running(store, scheduler):
    store.schedules[1] = make_schedule(cron_expression="每天中午")
    store.schedules[2] = make_schedule(id=2, name="健康任务", cron_expression="0 * * * * ?")
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0)) == []
    assert store.schedules[1]["state"] == 0
    assert statuses(store) == [2]
    assert "cron 表达式无效" in store.executions[1]["message"]
    # The healthy schedule was planned in the same pass and still fires.
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 1, 0)) == [2]


def test_scheduler_survives_an_unreachable_metadata_database(store, scheduler):
    store.failure = QualityStoreError(503, "质量元数据库连接失败：connection refused。")
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0)) == []
    assert "连接失败" in scheduler.status()["last_error"]
    store.failure = None
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?")
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0)) == []


def test_scheduler_never_runs_two_firings_of_one_schedule(store):
    gate = threading.Event()
    recorder = RecordingService(gate=gate)
    scheduler = QualityScheduler(store, recorder, interval=0.05)
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?")
    noon = dt.datetime(2026, 9, 12, 12, 1, 0)
    scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0))
    fired = []
    worker = threading.Thread(target=lambda: fired.append(scheduler.tick(noon)))
    worker.start()
    try:
        assert recorder.entered.wait(5)
        assert scheduler.tick(noon) == []
    finally:
        gate.set()
        worker.join(5)
    assert fired == [[1]]
    assert recorder.calls == [("schedule", 1)]
    assert recorder.overlapped is False


def test_start_runs_the_thread_and_stop_joins_it(store):
    recorder = RecordingService()
    scheduler = QualityScheduler(store, recorder, interval=0.05)
    store.schedules[1] = make_schedule(cron_expression="* * * * * ?")
    scheduler.start()
    scheduler.start()
    try:
        assert scheduler.running()
        assert "lattice-quality-scheduler" in [thread.name for thread in threading.enumerate()]
        assert recorder.entered.wait(5)
        assert scheduler.status()["jobs"][0]["name"] == "每日质量核查"
    finally:
        scheduler.stop(timeout=5)
    assert scheduler.running() is False
    assert "lattice-quality-scheduler" not in [thread.name for thread in threading.enumerate()]


def test_manual_fire_refuses_to_overlap_a_running_firing(store):
    gate = threading.Event()
    recorder = RecordingService(gate=gate)
    scheduler = QualityScheduler(store, recorder, interval=0.05)
    store.schedules[1] = make_schedule(cron_expression="0 * * * * ?", state=0)
    worker = threading.Thread(target=scheduler.fire, args=(1,))
    worker.start()
    try:
        assert recorder.entered.wait(5)
        with pytest.raises(QualityError) as error:
            scheduler.fire(1)
        assert error.value.status_code == 409
    finally:
        gate.set()
        worker.join(5)
    assert recorder.calls == [("schedule", 1)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@pytest.fixture
def client(store, service):
    application = FastAPI()
    application.include_router(quality_api.router)
    application.state.quality_store = store
    application.state.quality = service
    application.state.quality_scheduler = QualityScheduler(store, service, interval=0.05)
    with TestClient(application, base_url="http://127.0.0.1:8787") as value:
        yield value


def create(client, **overrides):
    payload = {
        "name": "订单号唯一",
        "metric": "column_duplicate",
        "datasource_id": SOURCE_ID,
        "schema_name": "public",
        "table_name": "orders",
        "column_name": "order_id",
        "config": {},
    }
    payload.update(overrides)
    return client.post("/api/quality/rules", json=payload)


def test_metrics_route_returns_the_whole_form_vocabulary(client):
    body = client.get("/api/quality/metrics").json()
    assert [item["id"] for item in body["dimensions"]] == list(DIMENSION_LABELS)
    assert [item["value"] for item in body["operators"]] == list(OPERATORS)
    assert {item["value"] for item in body["expected_types"]} == {"fix_value", "table_total_rows"}
    assert body["scheduler"] == {"bean_name": "QualityTask", "method_name": "run"}


def test_health_route_merges_store_and_scheduler(client):
    body = client.get("/api/quality/health").json()
    assert body["ok"] is True
    assert body["schema"] == "lattice_quality"
    assert body["scheduler"]["running"] is False


def test_rule_crud_run_preview_and_failures(client, store):
    created = create(client)
    assert created.status_code == 200
    rule = created.json()
    assert rule["dimension"] == "uniqueness"
    assert rule["metric_label"] == "重复值检查"
    assert rule["state_label"] == "启用"

    listing = client.get("/api/quality/rules", params={"dimension": "uniqueness"}).json()
    assert listing["total"] == 1
    assert listing["items"][0]["dimension_label"] == "唯一性校验"

    updated = client.post(
        f"/api/quality/rules/{rule['id']}/update", json={"level": "low", "threshold": 10}
    ).json()
    assert updated["level_label"] == "低"
    assert updated["threshold"] == 10

    preview = client.post(f"/api/quality/rules/{rule['id']}/preview", json={}).json()
    assert preview["total_sql"].startswith("SELECT count(1) AS checked_count")
    assert "HAVING count" in preview["invalidate_sql"]

    outcome = client.post(f"/api/quality/rules/{rule['id']}/run", json={}).json()
    assert outcome["execution"]["status_label"] == "成功"
    assert outcome["result"]["rule_name"] == "订单号唯一"

    failures = client.get(f"/api/quality/rules/{rule['id']}/failures", params={"limit": 5})
    assert failures.json()["columns"] == ["order_id"]

    log = client.get("/api/quality/executions").json()
    assert log["items"][0]["trigger_label"] == "手动"
    detail = client.get(f"/api/quality/executions/{log['items'][0]['id']}").json()
    assert detail["results"][0]["state_label"] == "成功"

    assert client.post(f"/api/quality/rules/{rule['id']}/delete", json={}).json() == {
        "id": rule["id"],
        "deleted": True,
    }
    assert store.rules == {}


def test_routes_answer_bad_input_in_chinese(client):
    unknown = create(client, metric="column_unknown")
    assert unknown.status_code == 400
    assert "核查类型" in unknown.json()["detail"]

    missing = client.get("/api/quality/rules/999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "核查规则不存在。"

    assert client.get("/api/quality/rules", params={"page": 0}).status_code == 422
    assert client.get("/api/quality/rules", params={"size": 500}).status_code == 422
    create(client)
    assert (
        client.get("/api/quality/rules/1/failures", params={"limit": 500}).status_code == 422
    )
    assert client.post("/api/quality/rules", json={"name": "x"}).status_code == 422


def test_schedule_routes_toggle_and_fire(client, store):
    created = client.post(
        "/api/quality/schedules", json={"name": "每日质量核查", "cron_expression": "0 0 12 * * ?"}
    ).json()
    assert created["state_label"] == "停止"
    assert created["bean_name"] == "QualityTask"

    started = client.post(f"/api/quality/schedules/{created['id']}/toggle", json={}).json()
    assert started["state_label"] == "运行"
    assert started["next_fire_time"]
    stopped = client.post(
        f"/api/quality/schedules/{created['id']}/toggle", json={"state": 0}
    ).json()
    assert stopped["state_label"] == "停止"

    listing = client.get("/api/quality/schedules", params={"name": "每日"}).json()
    assert listing["total"] == 1

    fired = client.post(f"/api/quality/schedules/{created['id']}/run", json={}).json()
    assert fired["total"] == 0

    broken = client.post(
        f"/api/quality/schedules/{created['id']}/update", json={"cron_expression": "每天中午"}
    )
    assert broken.status_code == 400
    assert "cron" in broken.json()["detail"]

    foreign = client.post(
        "/api/quality/schedules",
        json={"name": "外部", "cron_expression": "0 0 12 * * ?", "bean_name": "ShellTask"},
    )
    assert foreign.status_code == 400
    assert "QualityTask.run" in foreign.json()["detail"]

    assert client.post(f"/api/quality/schedules/{created['id']}/delete", json={}).json()["deleted"]
    assert store.schedules == {}


def test_analysis_routes_expose_the_report_and_statistics(client):
    create(client)
    client.post("/api/quality/rules/1/run", json={})
    statistics = client.get("/api/quality/statistics").json()
    assert [item["dimension"] for item in statistics["tree"]] == list(DIMENSION_LABELS)
    assert statistics["items"][0]["metric_label"] == "重复值检查"
    report = client.get("/api/quality/report", params={"date": "2026-09-12"}).json()
    assert report["level_label"] == "优秀"
    assert client.post("/api/quality/run", json={}).json()["total"] == 1


def test_routes_report_an_uninitialised_module():
    application = FastAPI()
    application.include_router(quality_api.router)
    with TestClient(application, base_url="http://127.0.0.1:8787") as value:
        response = value.get("/api/quality/rules")
    assert response.status_code == 503
    assert response.json()["detail"] == "数据质量模块尚未初始化。"


def test_editing_a_running_cron_replans_the_next_firing(client):
    """A running task kept the plan made for its previous expression, so an edited
    cron only took effect after one more firing on the old cadence."""
    created = client.post(
        "/api/quality/schedules",
        json={"name": "重新计算测试", "cron_expression": "0 0 12 * * ?"},
    )
    assert created.status_code == 200, created.text
    schedule_id = created.json()["id"]
    started = client.post(f"/api/quality/schedules/{schedule_id}/toggle", json={})
    assert started.status_code == 200, started.text
    first = started.json()["next_fire_time"]
    assert first

    changed = client.post(
        f"/api/quality/schedules/{schedule_id}/update",
        json={"cron_expression": "0 30 3 * * ?"},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["cron_expression"] == "0 30 3 * * ?"
    assert body["state_label"] == "运行"
    assert body["next_fire_time"] != first, "the firing time must follow the new cron"
    assert body["next_fire_time"].split("T")[1].startswith("03:30")



def test_percentage_rule_keeps_the_count_column_a_count(store):
    """A percentage rule used to store 2 (percent) in 不合规数量 for 4 bad rows,
    contradicting its own score and the 质量统计分析 totals that sum that column."""
    service = QualityService(store, FakeSources(FakeConnector(checked=200, actual=4)))
    by_count = service.run_rule(make_rule(result_formula="actual", operator="lte", threshold=3))
    by_ratio = service.run_rule(make_rule(result_formula="percentage", operator="lte", threshold=3))
    assert by_count["result"]["actual_value"] == by_ratio["result"]["actual_value"] == 4
    assert by_count["result"]["score"] == by_ratio["result"]["score"] == 98.0
    # The formula still decides the verdict: 4 > 3 fails, 2% <= 3% passes.
    assert by_count["result"]["state"] == 2
    assert by_ratio["result"]["state"] == 1
    assert "不合规 4 行" in by_ratio["execution"]["message"]


# ---------------------------------------------------------------------------
# Task registry: any registered job, retries, missed firings, run history
# ---------------------------------------------------------------------------
class FlakyTask:
    """Fails the first ``failures`` calls, then answers with a summary."""

    def __init__(self, failures=0, error=None):
        self.failures = failures
        self.error = error or RuntimeError("目标库连接超时")
        self.calls = []

    def __call__(self, params, context):
        self.calls.append((params, dict(context)))
        if len(self.calls) <= self.failures:
            raise self.error
        return {"tables": 3, "params": params}


def registry_with(task, **extra):
    registry = TaskRegistry()
    registry.add("MetadataTask", "ingest", "元数据拾取", task, params_label="服务 FQN", **extra)
    return registry


def run_rows(store):
    return [(row["trigger_type"], row["attempt"], row["status"]) for row in store.task_runs.values()]


def test_scheduler_dispatches_any_registered_task(store):
    task = FlakyTask()
    scheduler = QualityScheduler(store, RecordingService(), interval=0.05, registry=registry_with(task))
    store.schedules[1] = make_schedule(bean_name="MetadataTask", method_name="ingest", method_params="mysql-local", cron_expression="0 * * * * ?")
    scheduler.tick(dt.datetime(2026, 9, 12, 12, 0, 0))
    assert scheduler.tick(dt.datetime(2026, 9, 12, 12, 1, 0)) == [1]
    assert task.calls[0][0] == "mysql-local"
    assert task.calls[0][1]["trigger"] == "schedule" and task.calls[0][1]["schedule_id"] == 1
    assert scheduler.service.calls == []
    assert run_rows(store) == [("schedule", 1, "success")]
    assert store.task_runs[1]["task"] == "MetadataTask.ingest" and store.task_runs[1]["detail"] == {"tables": 3, "params": "mysql-local"}
    assert store.schedules[1]["last_status"] == "success" and "数据表 3" in store.schedules[1]["last_message"]
    job = scheduler.status()["jobs"][0]
    assert job["task_label"] == "元数据拾取" and job["last_status"] == "success"


def test_failed_runs_are_retried_after_the_delay_until_the_limit(store):
    task = FlakyTask(failures=2)
    scheduler = QualityScheduler(store, RecordingService(), interval=0.05, registry=registry_with(task))
    store.schedules[1] = make_schedule(bean_name="MetadataTask", method_name="ingest", cron_expression="0 0 12 * * ?", retry_limit=2, retry_delay_seconds=30)
    scheduler.tick(dt.datetime(2026, 9, 12, 11, 0, 0))
    noon = dt.datetime(2026, 9, 12, 12, 0, 0)
    assert scheduler.tick(noon) == [1]
    assert store.schedules[1]["last_status"] == "retrying"
    assert scheduler.status()["jobs"][0]["retry_attempt"] == 2
    assert scheduler.tick(noon + dt.timedelta(seconds=10)) == []
    assert scheduler.tick(noon + dt.timedelta(seconds=30)) == [1]
    assert scheduler.tick(noon + dt.timedelta(seconds=45)) == []
    assert scheduler.tick(noon + dt.timedelta(seconds=60)) == [1]
    assert run_rows(store) == [("schedule", 1, "retrying"), ("retry", 2, "retrying"), ("retry", 3, "success")]
    assert store.schedules[1]["last_message"].startswith("第 3 次尝试成功")
    assert scheduler.status()["jobs"][0]["retry_due"] is None
    # The regular cadence is untouched by the retries.
    assert store.schedules[1]["next_fire_time"] == "2026-09-13T12:00:00"


def test_retries_stop_at_the_limit_and_a_task_error_is_never_retried(store):
    task = FlakyTask(failures=9)
    scheduler = QualityScheduler(store, RecordingService(), interval=0.05, registry=registry_with(task))
    store.schedules[1] = make_schedule(bean_name="MetadataTask", method_name="ingest", cron_expression="0 0 12 * * ?", retry_limit=1, retry_delay_seconds=5)
    noon = dt.datetime(2026, 9, 12, 12, 0, 0)
    scheduler.tick(dt.datetime(2026, 9, 12, 11, 0, 0))
    scheduler.tick(noon)
    scheduler.tick(noon + dt.timedelta(seconds=5))
    assert [row[2] for row in run_rows(store)] == ["retrying", "failed"]
    assert store.schedules[1]["last_message"].startswith("执行失败（共尝试 2 次）")
    assert scheduler.tick(noon + dt.timedelta(seconds=10)) == []
    store.schedules[2] = make_schedule(id=2, name="未注册", bean_name="Nope", method_name="run", cron_expression="0 0 13 * * ?", retry_limit=3)
    scheduler.tick(dt.datetime(2026, 9, 12, 12, 30, 0))
    scheduler.tick(dt.datetime(2026, 9, 12, 13, 0, 0))
    assert store.schedules[2]["last_status"] == "failed" and "未注册的调度任务" in store.schedules[2]["last_message"]
    assert scheduler.status()["jobs"][1]["retry_due"] is None


def test_missed_firings_follow_the_misfire_policy(store):
    task = FlakyTask()
    scheduler = QualityScheduler(store, RecordingService(), interval=0.05, registry=registry_with(task))
    stored = "2026-09-12T12:00:00"
    store.schedules[1] = make_schedule(id=1, name="跳过", bean_name="MetadataTask", method_name="ingest", cron_expression="0 * * * * ?", next_fire_time=stored)
    store.schedules[2] = make_schedule(id=2, name="补跑一次", bean_name="MetadataTask", method_name="ingest", cron_expression="0 * * * * ?", next_fire_time=stored, misfire_policy="once")
    store.schedules[3] = make_schedule(id=3, name="逐次补跑", bean_name="MetadataTask", method_name="ingest", cron_expression="0 * * * * ?", next_fire_time=stored, misfire_policy="all", max_backfill=3)
    late = dt.datetime(2026, 9, 12, 12, 5, 30)
    assert scheduler.tick(late) == []
    runs = [(row["schedule_id"], row["trigger_type"], row["planned_time"]) for row in store.task_runs.values()]
    assert runs == [
        (2, "backfill", "2026-09-12T12:00:00"),
        (3, "backfill", "2026-09-12T12:00:00"),
        (3, "backfill", "2026-09-12T12:01:00"),
        (3, "backfill", "2026-09-12T12:02:00"),
    ]
    assert len(task.calls) == 4 and task.calls[0][1]["planned_time"] == "2026-09-12T12:00:00"
    for identifier in (1, 2, 3):
        assert store.schedules[identifier]["next_fire_time"] == "2026-09-12T12:06:00"
    assert store.schedules[1]["last_fire_time"] is None
    assert store.schedules[3]["last_fire_time"] == "2026-09-12T12:05:30"


def test_manual_fire_reports_a_failure_and_records_it(store):
    task = FlakyTask(failures=1)
    scheduler = QualityScheduler(store, RecordingService(), interval=0.05, registry=registry_with(task))
    store.schedules[1] = make_schedule(bean_name="MetadataTask", method_name="ingest", state=0, retry_limit=3)
    with pytest.raises(QualityError) as error:
        scheduler.fire(1)
    assert "目标库连接超时" in str(error.value)
    assert run_rows(store) == [("manual", 1, "failed")]
    assert scheduler.status()["jobs"] == []
    outcome = scheduler.fire(1)
    assert outcome["ok"] and outcome["tables"] == 3 and outcome["run_id"] == 2


def test_prepare_schedule_checks_the_registry_and_policies(store):
    task = FlakyTask()
    registry = registry_with(task, params_required=True, params_hint="填写服务 FQN。")
    service = QualityService(store, sources=None, registry=registry)
    with pytest.raises(TaskError, match="未注册的调度任务"):
        service.prepare_schedule({"name": "x", "bean_name": "Nope", "method_name": "run", "cron_expression": "0 0 12 * * ?"})
    with pytest.raises(TaskError, match="需要填写服务 FQN"):
        service.prepare_schedule({"name": "x", "bean_name": "MetadataTask", "method_name": "ingest", "cron_expression": "0 0 12 * * ?"})
    with pytest.raises(QualityError, match="补跑策略"):
        service.prepare_schedule({"name": "x", "bean_name": "MetadataTask", "method_name": "ingest", "method_params": "pg", "cron_expression": "0 0 12 * * ?", "misfire_policy": "later"})
    with pytest.raises(QualityError, match="失败重试次数"):
        service.prepare_schedule({"name": "x", "bean_name": "MetadataTask", "method_name": "ingest", "method_params": "pg", "cron_expression": "0 0 12 * * ?", "retry_limit": 11})
    prepared = service.prepare_schedule({"name": "x", "bean_name": "MetadataTask", "method_name": "ingest", "method_params": " pg ", "cron_expression": "0 0 12 * * ?"})
    assert prepared["method_params"] == "pg" and prepared["misfire_policy"] == "skip"
    assert (prepared["retry_limit"], prepared["retry_delay_seconds"], prepared["max_backfill"]) == (0, 60, 10)
    assert [item["key"] for item in service.options()["tasks"]] == ["MetadataTask.ingest"]


def test_platform_tasks_adapt_the_services_they_wrap():
    class Ingestor:
        def __init__(self):
            self.calls = []

        def run(self, ref, *, trigger="manual"):
            self.calls.append((ref, trigger))
            return {"status": "success", "summary": {"tables": 2, "columns": 9}}

        def due_services(self):
            return [{"fqn": "a"}, {"fqn": "b"}]

    class Insights:
        def snapshot(self, day=None):
            return {"day": day or "today", "assets": 5}

    class Lineage:
        def sync_views(self, service_fqn=None):
            return {"views": 1, "edges": 2, "service": service_fqn}

    ingestor = Ingestor()
    registry = register_platform_tasks(TaskRegistry(), ingestor=ingestor, insights=Insights(), lineage=Lineage())
    assert registry.keys() == ["MetadataTask.ingest", "MetadataTask.snapshot", "MetadataTask.syncViews"]
    assert registry.run("MetadataTask", "ingest", "mysql-local", {"trigger": "schedule"}) == {"services": 1, "status": "success", "tables": 2, "columns": 9}
    assert registry.run("MetadataTask", "ingest", "", {}) == {"services": 2, "tables": 4, "columns": 18}
    assert ingestor.calls == [("mysql-local", "schedule"), ("a", "scheduled"), ("b", "scheduled")]
    assert registry.run("MetadataTask", "snapshot", "2026-09-01", {})["day"] == "2026-09-01"
    assert registry.run("MetadataTask", "syncViews", "", {})["service"] is None


def test_task_routes_list_tasks_and_runs(store, service):
    registry = registry_with(FlakyTask(failures=1))
    service.registry = registry
    application = FastAPI()
    application.include_router(quality_api.router)
    application.state.quality_store = store
    application.state.quality = service
    application.state.tasks = registry
    application.state.quality_scheduler = QualityScheduler(store, service, interval=0.05, registry=registry)
    with TestClient(application, base_url="http://127.0.0.1:8787") as client:
        listing = client.get("/api/quality/tasks").json()
        assert [item["key"] for item in listing["items"]] == ["MetadataTask.ingest"]
        assert [item["value"] for item in listing["misfire_policies"]] == ["skip", "once", "all"]
        refused = client.post("/api/quality/schedules", json={"name": "x", "bean_name": "Nope", "method_name": "run", "cron_expression": "0 0 12 * * ?"})
        assert refused.status_code == 400 and "未注册的调度任务" in refused.json()["detail"]
        created = client.post("/api/quality/schedules", json={"name": "拾取", "bean_name": "MetadataTask", "method_name": "ingest", "cron_expression": "0 0 12 * * ?", "retry_limit": 2, "misfire_policy": "once"}).json()
        assert created["task_label"] == "元数据拾取" and created["misfire_policy_label"] == "补跑一次" and created["retry_limit"] == 2
        failed = client.post(f"/api/quality/schedules/{created['id']}/run", json={})
        assert failed.status_code == 400 and "目标库连接超时" in failed.json()["detail"]
        assert client.post(f"/api/quality/schedules/{created['id']}/run", json={}).json()["ok"] is True
        runs = client.get("/api/quality/task-runs", params={"schedule_id": created["id"]}).json()
        assert [(item["status"], item["status_label"], item["trigger_label"]) for item in runs["items"]] == [("success", "成功", "手动"), ("failed", "失败", "手动")]
        assert client.get("/api/quality/task-runs", params={"status": "failed"}).json()["total"] == 1
        assert client.get(f"/api/quality/task-runs/{runs['items'][0]['id']}").json()["task"] == "MetadataTask.ingest"
        assert client.get("/api/quality/task-runs/999").status_code == 404
        schedules = client.get("/api/quality/schedules").json()["items"]
        assert schedules[0]["last_status_label"] == "成功"


# ---------------------------------------------------------------------------
# Custom SQL, cross-source comparison, templates and batch creation
# ---------------------------------------------------------------------------
class TwoSources(FakeSources):
    """A registry with one connector per data source, for cross-source rules."""

    def __init__(self, connectors):
        super().__init__(known=tuple(connectors))
        self.connectors = connectors

    def record(self, source_id):
        if source_id not in self.connectors:
            raise DataSourceError(404, "数据源不存在。")
        return {"id": source_id, "name": f"库 {source_id}", "type": "postgresql"}

    def connector(self, source_id):
        self.record(source_id)
        return self.connectors[source_id]


def test_cross_source_rule_compares_aggregates_from_two_engines(store):
    source = FakeConnector(checked=0, actual=1000)  # actual_value: this engine's row count
    reference = FakeConnector(checked=997, actual=0)  # checked_count: the reference engine's
    service = QualityService(store, TwoSources({"pg": source, "ch": reference}))
    rule = service.prepare_rule({
        "name": "订单行数一致", "metric": "cross_source_compare", "datasource_id": "pg", "schema_name": "public", "table_name": "orders",
        "config": {"aggregate": "count", "reference_datasource": "ch", "reference_schema": "dw", "reference_table": "orders"},
        "result_formula": "percentage", "operator": "lte", "threshold": 1,
    })
    assert rule["dimension"] == "consistency" and rule["datasource_name"] == "库 pg"
    outcome = service.run_rule(store.create_rule(rule))
    result = outcome["result"]
    assert (result["checked_count"], result["actual_value"], result["state"]) == (997, 3, 1)
    assert "本库 1000" in outcome["execution"]["message"] and "参照库（库 ch）997" in outcome["execution"]["message"]
    assert reference.queries[-1][0] == 'SELECT count(1) AS checked_count FROM "dw"."orders"'
    assert source.queries[-1][0] == 'SELECT count(1) AS actual_value FROM "public"."orders"'
    preview = service.preview(rule["id"] if "id" in rule else 1)
    assert preview["compare"] == "aggregate" and preview["reference_label"] == "库 ch" and "checked_count" in preview["reference_sql"]
    with pytest.raises(QualityError, match="核查字段"):
        service.prepare_rule({**rule, "config": {**rule["config"], "aggregate": "sum"}, "column_name": None})
    with pytest.raises(DataSourceError):
        service.prepare_rule({**rule, "config": {**rule["config"], "reference_datasource": "nope"}})


def test_custom_sql_rule_runs_in_both_modes(store, connector, service):
    rows = service.prepare_rule({
        "name": "负金额", "metric": "custom_sql", "datasource_id": SOURCE_ID, "table_name": "orders",
        "config": {"mode": "rows", "sql": "SELECT * FROM ${table} WHERE amount < 0"}, "threshold": 0,
    })
    outcome = service.run_rule(store.create_rule(rows))
    assert outcome["result"]["actual_value"] == 4 and outcome["result"]["state"] == 2
    assert "lattice_src" in outcome["result"]["invalidate_sql"]
    value = service.prepare_rule({
        "name": "负金额合计", "metric": "custom_sql", "datasource_id": SOURCE_ID, "table_name": "orders",
        "config": {"mode": "value", "sql": "SELECT 4 AS actual_value"}, "operator": "lte", "threshold": 10,
    })
    assert service.run_rule(store.create_rule(value))["result"]["state"] == 1
    with pytest.raises(MetricError, match="注释"):
        service.prepare_rule({**rows, "config": {"mode": "rows", "sql": "SELECT * FROM ${table} -- x"}})


def test_templates_are_seeded_once_and_saved_from_rules(store, service):
    service.ensure_templates()
    service.ensure_templates()
    names = [row["name"] for row in store.templates.values()]
    assert names[:2] == ["主键唯一", "字段非空"] and len(names) == 10
    assert all(row["builtin"] == 1 for row in store.templates.values())
    rule = store.create_rule(service.prepare_rule({
        "name": "订单号唯一", "metric": "column_duplicate", "datasource_id": SOURCE_ID, "table_name": "orders", "column_name": "order_id", "level": "high",
    }))
    template = service.template_from_rule(rule, name="订单号唯一模板")
    assert (template["metric"], template["level"], template["dimension"], template["description"]) == ("column_duplicate", "high", "uniqueness", "由规则「订单号唯一」保存")
    with pytest.raises(QualityError, match="核查类型"):
        service.prepare_template({"name": "x", "metric": "nope"})
    with pytest.raises(QualityError, match="模板名称"):
        service.prepare_template({"metric": "column_null"})


def test_batch_create_validates_each_target_and_reports_the_rest(store, service):
    template = store.create_template(service.prepare_template({"name": "字段非空", "metric": "column_null", "threshold": 0}))
    outcome = service.batch_create({
        "template_id": template["id"],
        "targets": [
            {"datasource_id": SOURCE_ID, "schema_name": "public", "table_name": "orders", "column_name": "order_id"},
            {"datasource_id": SOURCE_ID, "table_name": "customers", "column_name": "customer_id"},
            {"datasource_id": SOURCE_ID, "table_name": "payments"},
            {"datasource_id": "missing", "table_name": "orders", "column_name": "id"},
        ],
        "state": 0,
    })
    assert outcome["total"] == 4 and [rule["name"] for rule in outcome["created"]] == ["字段非空 · orders.order_id", "字段非空 · customers.customer_id"]
    assert [error["table_name"] for error in outcome["errors"]] == ["payments", "orders"]
    assert "核查字段" in outcome["errors"][0]["message"] and outcome["created"][0]["state"] == 0
    assert outcome["created"][0]["comment"] == "由模板「字段非空」批量下发"
    overridden = service.batch_create({
        "rule": {"name": "手机号", "metric": "column_match_regex", "config": {"regexp": "^1[0-9]{10}$"}},
        "targets": [{"datasource_id": SOURCE_ID, "table_name": "users", "column_name": "phone"}],
        "config": {"regexp": "^1[3-9][0-9]{9}$"},
        "name_prefix": "手机号格式",
    })
    assert overridden["created"][0]["config"]["regexp"] == "^1[3-9][0-9]{9}$" and overridden["created"][0]["name"] == "手机号格式 · users.phone"
    with pytest.raises(QualityError, match="目标表"):
        service.batch_create({"template_id": template["id"], "targets": []})


def test_template_and_batch_routes(client, store):
    listing = client.get("/api/quality/templates").json()
    assert listing["total"] == 0
    created = client.post("/api/quality/templates", json={"name": "行数一致", "metric": "cross_source_compare", "config": {"aggregate": "count"}}).json()
    assert created["dimension_label"] == "一致性校验" and created["needs_column"] is False and created["builtin"] is False
    updated = client.post(f"/api/quality/templates/{created['id']}/update", json={"description": "参照库行数"}).json()
    assert updated["description"] == "参照库行数"
    assert client.post("/api/quality/templates", json={"name": "坏", "metric": "nope"}).status_code == 400
    batch = client.post("/api/quality/rules/batch", json={
        "template_id": created["id"],
        "targets": [{"datasource_id": SOURCE_ID, "table_name": "orders"}],
        "config": {"reference_datasource": SOURCE_ID, "reference_table": "orders_copy"},
    }).json()
    assert len(batch["created"]) == 1 and batch["created"][0]["metric_label"] == "跨库比对" and batch["errors"] == []
    rule_id = batch["created"][0]["id"]
    saved = client.post(f"/api/quality/rules/{rule_id}/template", json={"name": "从规则保存"}).json()
    assert saved["metric"] == "cross_source_compare" and saved["config"]["reference_table"] == "orders_copy"
    assert client.get("/api/quality/templates", params={"metric": "cross_source_compare"}).json()["total"] == 2
    assert client.post(f"/api/quality/templates/{saved['id']}/delete", json={}).json()["deleted"] is True
    assert client.post("/api/quality/templates/999/delete", json={}).status_code == 404
    assert client.post("/api/quality/rules/batch", json={"targets": [{"datasource_id": SOURCE_ID, "table_name": "t"}]}).status_code == 400
