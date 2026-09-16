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

"""HTTP routes of the 数据质量 module.

This is the one place in the project that uses ``APIRouter``: the quality
screens need twenty-one endpoints, and declaring them on the shared ``app``
object would double the size of ``webapi/app.py`` for one feature. Everything
else follows the conventions of that file — synchronous handlers, services read
off ``request.app.state``, ``<Verb><Noun>Input`` models that forbid unknown
fields and bound every string, and service errors turned into ``HTTPException``
with the error's own status code so the Chinese message reaches the user
unchanged.

Path parameters stay strings on purpose: the store validates them and answers
with a Chinese message, where an ``int`` path type would produce FastAPI's
English 422.

The primary agent wires the module into ``webapi/app.py``; nothing here imports
that module, so the dependency runs one way only:

    from .quality_api import router as quality_router
    from .quality_service import QualityScheduler, QualityService
    from .quality_store import QualityStore

    # inside lifespan(), after app.state.sources exists
    app.state.quality_store = QualityStore()
    app.state.quality_store.bootstrap()
    app.state.quality = QualityService(app.state.quality_store, app.state.sources)
    app.state.quality_scheduler = QualityScheduler(
        app.state.quality_store, app.state.quality
    )
    app.state.quality_scheduler.start()
    yield
    app.state.quality_scheduler.stop()
    app.state.quality_store.close()

    # after the FastAPI object is created
    app.include_router(quality_router)

The three ``app.state`` attributes this router reads are therefore
``quality_store``, ``quality`` and ``quality_scheduler``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .connectors import ConnectorError
from .datasources import DataSourceError
from .quality_metrics import MetricError
from .quality_service import (
    DEFAULT_SAMPLE_ROWS,
    MAX_SAMPLE_ROWS,
    QualityError,
    QualityScheduler,
    QualityService,
    decorate_execution,
    decorate_page,
    decorate_rule,
    decorate_schedule,
    decorate_task_run,
    decorate_template,
    next_fire_time,
)
from .tasks import TaskError, TaskRegistry
from .quality_store import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MISFIRE_LABELS,
    TASK_RUN_STATUS_LABELS,
    QualityStore,
    QualityStoreError,
)

router = APIRouter(prefix="/api/quality")


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=64)
    dimension: str | None = Field(default=None, max_length=32)
    level: str | None = Field(default=None, max_length=8)
    datasource_id: str = Field(min_length=1, max_length=64)
    datasource_name: str | None = Field(default=None, max_length=200)
    schema_name: str | None = Field(default=None, max_length=200)
    table_name: str = Field(min_length=1, max_length=200)
    column_name: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    expected_type: str | None = Field(default=None, max_length=32)
    result_formula: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=8)
    threshold: float | None = None
    state: int | None = Field(default=None, ge=0, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class UpdateRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    metric: str | None = Field(default=None, min_length=1, max_length=64)
    dimension: str | None = Field(default=None, max_length=32)
    level: str | None = Field(default=None, max_length=8)
    datasource_id: str | None = Field(default=None, min_length=1, max_length=64)
    datasource_name: str | None = Field(default=None, max_length=200)
    schema_name: str | None = Field(default=None, max_length=200)
    table_name: str | None = Field(default=None, min_length=1, max_length=200)
    column_name: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] | None = None
    expected_type: str | None = Field(default=None, max_length=32)
    result_formula: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=8)
    threshold: float | None = None
    state: int | None = Field(default=None, ge=0, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class CreateScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    bean_name: str | None = Field(default=None, max_length=120)
    method_name: str | None = Field(default=None, max_length=120)
    method_params: str | None = Field(default=None, max_length=500)
    cron_expression: str = Field(min_length=1, max_length=120)
    state: int | None = Field(default=None, ge=0, le=1)
    retry_limit: int | None = Field(default=None, ge=0, le=10)
    retry_delay_seconds: int | None = Field(default=None, ge=1, le=3600)
    misfire_policy: str | None = Field(default=None, max_length=16)
    max_backfill: int | None = Field(default=None, ge=1, le=50)


class UpdateScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    bean_name: str | None = Field(default=None, max_length=120)
    method_name: str | None = Field(default=None, max_length=120)
    method_params: str | None = Field(default=None, max_length=500)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=120)
    state: int | None = Field(default=None, ge=0, le=1)
    retry_limit: int | None = Field(default=None, ge=0, le=10)
    retry_delay_seconds: int | None = Field(default=None, ge=1, le=3600)
    misfire_policy: str | None = Field(default=None, max_length=16)
    max_backfill: int | None = Field(default=None, ge=1, le=50)


class ToggleScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: int | None = Field(default=None, ge=0, le=1)


class CreateTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    metric: str = Field(min_length=1, max_length=64)
    level: str | None = Field(default=None, max_length=8)
    config: dict[str, Any] = Field(default_factory=dict)
    expected_type: str | None = Field(default=None, max_length=32)
    result_formula: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=8)
    threshold: float | None = None


class UpdateTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    metric: str | None = Field(default=None, min_length=1, max_length=64)
    level: str | None = Field(default=None, max_length=8)
    config: dict[str, Any] | None = None
    expected_type: str | None = Field(default=None, max_length=32)
    result_formula: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=8)
    threshold: float | None = None


class SaveTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class BatchTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str = Field(min_length=1, max_length=64)
    schema_name: str | None = Field(default=None, max_length=200)
    table_name: str = Field(min_length=1, max_length=200)
    column_name: str | None = Field(default=None, max_length=200)


class BatchRulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: int | None = Field(default=None, ge=1)
    rule: CreateTemplateInput | None = None
    targets: list[BatchTargetInput] = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    name_prefix: str | None = Field(default=None, max_length=200)
    state: int | None = Field(default=None, ge=0, le=1)
    comment: str | None = Field(default=None, max_length=2000)


def guarded(operation, *args, **kwargs):
    """Run a service call, mapping its own error into an HTTP response.

    ``app.failure`` is not reused because importing ``webapi.app`` here would
    close a cycle: the application includes this router, never the reverse.
    """
    try:
        return operation(*args, **kwargs)
    except HTTPException:
        raise
    except (
        QualityError,
        QualityStoreError,
        TaskError,
        MetricError,
        DataSourceError,
        ConnectorError,
        ValueError,
        KeyError,
        OSError,
    ) as error:
        status = getattr(error, "status_code", None)
        raise HTTPException(
            status if isinstance(status, int) else 400, str(error)[:800]
        ) from error


def _service(request: Request) -> QualityService:
    return _from_state(request, "quality")


def _store(request: Request) -> QualityStore:
    return _from_state(request, "quality_store")


def _scheduler(request: Request) -> QualityScheduler:
    return _from_state(request, "quality_scheduler")


def _registry(request: Request) -> TaskRegistry | None:
    registry = getattr(request.app.state, "tasks", None)
    return registry if registry is not None else getattr(_service(request), "registry", None)


def _schedule_view(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    return decorate_schedule(row, _registry(request))


def _from_state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(503, "数据质量模块尚未初始化。")
    return value


# ----- catalog and health ---------------------------------------------------------
@router.get("/metrics")
def quality_metrics(request: Request):
    return guarded(_service(request).options)


@router.get("/health")
def quality_health(request: Request):
    health = guarded(_store(request).healthy)
    return {**health, "scheduler": guarded(_scheduler(request).status)}


# ----- rules ----------------------------------------------------------------------
@router.get("/rules")
def list_rules(
    request: Request,
    dimension: str | None = Query(default=None, max_length=32),
    name: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    listing = guarded(_store(request).list_rules, dimension, name, page, size)
    return decorate_page(listing, decorate_rule)


@router.post("/rules")
def create_rule(payload: CreateRuleInput, request: Request):
    prepared = guarded(_service(request).prepare_rule, payload.model_dump())
    return decorate_rule(guarded(_store(request).create_rule, prepared))


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, request: Request):
    return decorate_rule(guarded(_store(request).get_rule, rule_id))


@router.post("/rules/{rule_id}/update")
def update_rule(rule_id: str, payload: UpdateRuleInput, request: Request):
    store = _store(request)
    stored = guarded(store.get_rule, rule_id)
    merged = {**stored, **payload.model_dump(exclude_unset=True)}
    prepared = guarded(_service(request).prepare_rule, merged)
    return decorate_rule(guarded(store.update_rule, rule_id, prepared))


@router.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: str, payload: EmptyInput, request: Request):
    return guarded(_store(request).delete_rule, rule_id)


@router.post("/rules/{rule_id}/run")
def run_rule(rule_id: str, payload: EmptyInput, request: Request):
    return guarded(_service(request).run_saved, rule_id, trigger="manual")


@router.post("/rules/{rule_id}/preview")
def preview_rule(rule_id: str, payload: EmptyInput, request: Request):
    return guarded(_service(request).preview, rule_id)


@router.get("/rules/{rule_id}/failures")
def rule_failures(
    request: Request,
    rule_id: str,
    limit: int = Query(default=DEFAULT_SAMPLE_ROWS, ge=1, le=MAX_SAMPLE_ROWS),
):
    return guarded(_service(request).sample_failures, rule_id, limit)


@router.post("/rules/batch")
def batch_rules(payload: BatchRulesInput, request: Request):
    return guarded(_service(request).batch_create, payload.model_dump())


@router.post("/rules/{rule_id}/template")
def save_rule_as_template(rule_id: str, payload: SaveTemplateInput, request: Request):
    service = _service(request)
    store = _store(request)
    rule = guarded(store.get_rule, rule_id)
    prepared = guarded(service.template_from_rule, rule, payload.name, payload.description)
    return decorate_template(guarded(store.create_template, prepared))


# ----- rule templates -------------------------------------------------------------
@router.get("/templates")
def list_templates(
    request: Request,
    name: str | None = Query(default=None, max_length=200),
    metric: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    listing = guarded(_store(request).list_templates, name, metric, page, size)
    return decorate_page(listing, decorate_template)


@router.post("/templates")
def create_template(payload: CreateTemplateInput, request: Request):
    prepared = guarded(_service(request).prepare_template, payload.model_dump())
    return decorate_template(guarded(_store(request).create_template, prepared))


@router.post("/templates/{template_id}/update")
def update_template(template_id: str, payload: UpdateTemplateInput, request: Request):
    store = _store(request)
    stored = guarded(store.get_template, template_id)
    merged = {**stored, **payload.model_dump(exclude_unset=True)}
    prepared = guarded(_service(request).prepare_template, merged)
    return decorate_template(guarded(store.update_template, template_id, prepared))


@router.post("/templates/{template_id}/delete")
def delete_template(template_id: str, payload: EmptyInput, request: Request):
    return guarded(_store(request).delete_template, template_id)


@router.post("/run")
def run_all(payload: EmptyInput, request: Request):
    return guarded(_service(request).run_enabled, trigger="manual")


# ----- schedules ------------------------------------------------------------------
@router.get("/schedules")
def list_schedules(
    request: Request,
    name: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    listing = guarded(_store(request).list_schedules, name, page, size)
    return decorate_page(listing, lambda row: _schedule_view(request, row))


@router.post("/schedules")
def create_schedule(payload: CreateScheduleInput, request: Request):
    prepared = guarded(_service(request).prepare_schedule, payload.model_dump())
    return _schedule_view(request, guarded(_store(request).create_schedule, prepared))


@router.post("/schedules/{schedule_id}/update")
def update_schedule(schedule_id: str, payload: UpdateScheduleInput, request: Request):
    store = _store(request)
    stored = guarded(store.get_schedule, schedule_id)
    merged = {**stored, **payload.model_dump(exclude_unset=True)}
    prepared = guarded(_service(request).prepare_schedule, merged)
    updated = guarded(store.update_schedule, schedule_id, prepared)
    if str(updated.get("state")) == "1" and str(updated.get("cron_expression")) != str(
        stored.get("cron_expression")
    ):
        # A running task keeps the firing time planned for its previous cron
        # unless it is recomputed here, so an edited expression would not take
        # effect until the task happened to fire on the old plan.
        planned = guarded(next_fire_time, updated.get("cron_expression"))
        updated = guarded(store.set_schedule_state, schedule_id, 1, planned)
        _scheduler(request).forget(schedule_id)
    return _schedule_view(request, updated)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(schedule_id: str, payload: EmptyInput, request: Request):
    return guarded(_store(request).delete_schedule, schedule_id)


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str, payload: ToggleScheduleInput, request: Request):
    store = _store(request)
    stored = guarded(store.get_schedule, schedule_id)
    current = 1 if str(stored.get("state")) == "1" else 0
    target = payload.state if payload.state is not None else 1 - current
    planned = None
    if target == 1:
        # Starting a task validates its cron now, so a broken expression is
        # refused here instead of stopping the task again on the next tick.
        planned = guarded(next_fire_time, stored.get("cron_expression"))
    return _schedule_view(request, guarded(store.set_schedule_state, schedule_id, target, planned))


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: str, payload: EmptyInput, request: Request):
    return guarded(_scheduler(request).fire, schedule_id)


# ----- registered tasks and their runs --------------------------------------------
@router.get("/tasks")
def list_tasks(request: Request):
    registry = _registry(request)
    return {
        "items": registry.options() if registry else [],
        "misfire_policies": [{"value": key, "label": label} for key, label in MISFIRE_LABELS.items()],
        "run_statuses": [{"value": key, "label": label} for key, label in TASK_RUN_STATUS_LABELS.items()],
        "scheduler": _scheduler(request).status(),
    }


@router.get("/task-runs")
def list_task_runs(
    request: Request,
    schedule_id: str | None = Query(default=None, max_length=20),
    status: str | None = Query(default=None, max_length=16),
    task: str | None = Query(default=None, max_length=240),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    listing = guarded(_store(request).list_task_runs, schedule_id or None, status or None, task or None, page, size)
    return decorate_page(listing, decorate_task_run)


@router.get("/task-runs/{run_id}")
def get_task_run(run_id: str, request: Request):
    return decorate_task_run(guarded(_store(request).get_task_run, run_id))


# ----- analysis and history -------------------------------------------------------
@router.get("/report")
def quality_report(request: Request, date: str | None = Query(default=None, max_length=10)):
    return guarded(_store(request).report, date)


@router.get("/statistics")
def quality_statistics(request: Request, name: str | None = Query(default=None, max_length=200)):
    return guarded(_service(request).statistics, name)


@router.get("/executions")
def list_executions(
    request: Request,
    status: int | None = Query(default=None, ge=0, le=2),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    listing = guarded(_store(request).list_executions, status, page, size)
    return decorate_page(listing, decorate_execution)


@router.get("/executions/{execution_id}")
def get_execution(execution_id: str, request: Request):
    return decorate_execution(guarded(_store(request).get_execution, execution_id))
