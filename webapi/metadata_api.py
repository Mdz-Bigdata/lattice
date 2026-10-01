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

"""HTTP routes of the 元数据管理 module: an OpenMetadata-style catalog, lineage and context layer.

Like ``quality_api`` this module owns an ``APIRouter`` (``/api/metadata``)
and never imports ``webapi.app``: the application calls :func:`start_metadata`
from its lifespan and includes :data:`router`. Conventions follow the rest of
the platform — synchronous handlers, services read off ``request.app.state``,
``<Verb><Noun>Input`` bodies that forbid unknown fields, and service errors
turned into ``HTTPException`` with their own status code so the Chinese
message reaches the reader unchanged.

Entities are addressed by type plus a reference (id or fully qualified name)
passed as query parameters or in the body, because an FQN may contain any
character a path segment cannot. Two routes are not JSON-over-REST: the MCP
endpoint speaks JSON-RPC 2.0 over Streamable HTTP for agents, and the export
returns a bundle meant to be saved.

``app.state`` attributes: ``metadata_store``, ``metadata`` (the catalog
service), ``metadata_lineage``, ``metadata_insights``, ``metadata_alerts``,
``metadata_ingest``, ``metadata_context``, ``metadata_mcp`` and
``metadata_scheduler``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import duckdb
import sqlglot
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import env
from .connectors import ConnectorError
from .datasources import DataSourceError
from .llm import LlmError
from .metadata_context import PROTOCOL_VERSIONS, ContextService, McpServer
from .metadata_entities import DATA_ASSET_TYPES, SERVICE_CATEGORIES
from .metadata_ingest import IngestionError, MetadataIngestor, MetadataScheduler
from .metadata_insights import DESTINATION_TYPES, AlertService, InsightsService, kpi_charts, tier_options
from .metadata_lineage import EDGE_SOURCES, MAX_DEPTH, LineageService
from .metadata_service import MetadataError, MetadataService, summary_of
from .metadata_store import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MetadataStore, MetadataStoreError
from .questions import LOCAL_SAMPLE, QueryError

router = APIRouter(prefix="/api/metadata")
MCP_PATH = "/api/metadata/mcp"
MAX_SAMPLE_ROWS = 200
FQN = 4096
TYPE = 64


# =========================================================================================
# lifecycle
# =========================================================================================
def start_metadata(state: Any, runtime: Path) -> None:
    """Build the catalog services on ``state``; never raises out of the lifespan."""
    store = MetadataStore(fallback_path=runtime / "metadata.sqlite")
    state.metadata_store = store
    store.bootstrap()
    service = MetadataService(store)
    lineage = LineageService(store, service)
    insights = InsightsService(store, service)
    alerts = AlertService(store, service)
    ingestor = MetadataIngestor(
        service, lineage, getattr(state, "sources", None),
        polaris=getattr(state, "polaris", None), models=getattr(state, "models", None),
        semantic=getattr(state, "semantic", None),
    )
    context = ContextService(
        service, lineage, insights, llm=getattr(state, "llm", None),
        queries=getattr(state, "queries", None), registry=getattr(state, "sources", None),
        metrics=getattr(state, "semantic_engine", None),
    )
    state.metadata = service
    state.metadata_lineage = lineage
    state.metadata_insights = insights
    state.metadata_alerts = alerts
    state.metadata_ingest = ingestor
    state.metadata_context = context
    state.metadata_mcp = McpServer(context)
    state.metadata_boot_error = ""
    alerts.attach()
    if store.healthy().get("ok"):
        try:
            service.seed()
            ingestor.ensure_bot()
            context.ensure_bot()
        except (MetadataError, MetadataStoreError) as error:
            state.metadata_boot_error = str(error)
    queries = getattr(state, "queries", None)
    if queries is not None and hasattr(queries, "observers"):
        queries.observers.append(lineage.record_query)
        queries.annotator = context.datasource_context
    alerts.start()
    scheduler = MetadataScheduler(ingestor, insights)
    state.metadata_scheduler = scheduler
    if env("METADATA_SCHEDULER", "1").strip().lower() not in {"0", "false", "off", "no"}:
        scheduler.start()


def stop_metadata(state: Any) -> None:
    for name, method in (("metadata_scheduler", "stop"), ("metadata_alerts", "stop"), ("metadata_store", "close")):
        target = getattr(state, name, None)
        if target is None:
            continue
        try:
            getattr(target, method)()
        except Exception:  # noqa: BLE001 - shutdown continues with the next service
            continue


# =========================================================================================
# plumbing
# =========================================================================================
def guarded(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except HTTPException:
        raise
    except LlmError as error:
        raise HTTPException(502, f"模型调用失败：{str(error)[:600]}") from error
    except (
        MetadataError, MetadataStoreError, IngestionError, QueryError, DataSourceError, ConnectorError,
        sqlglot.errors.SqlglotError, duckdb.Error, ValueError, KeyError, OSError,
    ) as error:
        status = getattr(error, "status_code", None)
        raise HTTPException(status if isinstance(status, int) else 400, str(error)[:800]) from error


def _state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(503, "元数据管理模块尚未初始化。")
    return value


def _service(request: Request) -> MetadataService:
    return _state(request, "metadata")


def _lineage(request: Request) -> LineageService:
    return _state(request, "metadata_lineage")


def _insights(request: Request) -> InsightsService:
    return _state(request, "metadata_insights")


def _alerts(request: Request) -> AlertService:
    return _state(request, "metadata_alerts")


def _ingestor(request: Request) -> MetadataIngestor:
    return _state(request, "metadata_ingest")


def _context(request: Request) -> ContextService:
    return _state(request, "metadata_context")


def _mcp(request: Request) -> McpServer:
    return _state(request, "metadata_mcp")


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _payload(model: BaseModel, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """Explicit fields win over the free-form ``fields`` object of the same body."""
    data = model.model_dump(exclude_unset=True)
    extra = data.pop("fields", None) or {}
    for key in drop:
        data.pop(key, None)
    return {**extra, **data}


def _background(name: str, target: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    def run() -> None:
        try:
            target(*args, **kwargs)
        except Exception:  # noqa: BLE001 - every run records its own failure
            return

    threading.Thread(target=run, name=name, daemon=True).start()


# =========================================================================================
# input models
# =========================================================================================
Ref = str | dict[str, Any]


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityFieldsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=200_000)
    owners: list[Ref] | None = Field(default=None, max_length=50)
    tags: list[Ref] | None = Field(default=None, max_length=100)
    tier: str | None = Field(default=None, max_length=512)
    domain: str | None = Field(default=None, max_length=FQN)
    data_products: list[str] | None = Field(default=None, max_length=100)
    experts: list[Ref] | None = Field(default=None, max_length=50)
    reviewers: list[Ref] | None = Field(default=None, max_length=50)
    users: list[Ref] | None = Field(default=None, max_length=500)
    teams: list[Ref] | None = Field(default=None, max_length=50)
    parent_team: str | None = Field(default=None, max_length=256)
    assets: list[Ref] | None = Field(default=None, max_length=2000)
    related_terms: list[str] | None = Field(default=None, max_length=100)
    columns: list[dict[str, Any]] | None = Field(default=None, max_length=2000)
    extension: dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CreateEntityInput(EntityFieldsInput):
    entity_type: str = Field(min_length=1, max_length=TYPE)
    name: str = Field(min_length=1, max_length=256)
    parent_fqn: str | None = Field(default=None, max_length=FQN)


class UpdateEntityInput(EntityFieldsInput):
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)


class RenameEntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)
    name: str = Field(min_length=1, max_length=256)


class DeleteEntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)
    hard: bool = False


class RestoreEntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)


class FollowEntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)
    follow: bool = True
    user: str | None = Field(default=None, max_length=64)


class SetTagsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_fqn: str = Field(min_length=1, max_length=FQN)
    target_type: str | None = Field(default=None, max_length=TYPE)
    tags: list[Ref] = Field(default_factory=list, max_length=100)


class AddLineageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_fqn: str = Field(min_length=1, max_length=FQN)
    to_fqn: str = Field(min_length=1, max_length=FQN)
    from_type: str | None = Field(default=None, max_length=TYPE)
    to_type: str | None = Field(default=None, max_length=TYPE)
    description: str | None = Field(default=None, max_length=4000)
    sql: str | None = Field(default=None, max_length=20_000)
    columns: list[dict[str, Any]] | None = Field(default=None, max_length=500)
    pipeline_fqn: str | None = Field(default=None, max_length=FQN)
    source: str | None = Field(default=None, max_length=16)


class DeleteLineageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_fqn: str = Field(min_length=1, max_length=FQN)
    to_fqn: str = Field(min_length=1, max_length=FQN)


class ParseLineageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20_000)
    dialect: str | None = Field(default=None, max_length=32)
    datasource_id: str | None = Field(default=None, max_length=64)
    service_fqn: str | None = Field(default=None, max_length=FQN)
    target: str | None = Field(default=None, max_length=FQN)
    apply: bool = False


class SyncViewLineageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_fqn: str | None = Field(default=None, max_length=FQN)


class CreateThreadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_type: str = Field(default="Conversation", max_length=16)
    about_fqn: str | None = Field(default=None, max_length=FQN)
    about_type: str | None = Field(default=None, max_length=TYPE)
    message: str | None = Field(default=None, max_length=20_000)
    task_type: str | None = Field(default=None, max_length=32)
    assignees: list[Ref] | None = Field(default=None, max_length=20)
    suggestion: Any = None
    column: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=256)
    start_ts: int | None = Field(default=None, ge=0)
    end_ts: int | None = Field(default=None, ge=0)


class CreatePostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20_000)


class ResolveTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool
    message: str | None = Field(default=None, max_length=20_000)


class DefinePropertyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=256)
    property_type: str = Field(default="string", max_length=32)
    description: str | None = Field(default=None, max_length=4000)
    config: dict[str, Any] = Field(default_factory=dict)


class UpdatePropertyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=4000)
    config: dict[str, Any] | None = None


class DeletePropertyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    name: str = Field(min_length=1, max_length=64)


class ImportBundleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(default="lattice", pattern=r"^(lattice|openmetadata)$")
    bundle: dict[str, Any]
    dry_run: bool = False


class CreateAlertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    enabled: bool = True
    filters: dict[str, Any] = Field(default_factory=dict)
    destinations: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    owners: list[Ref] | None = Field(default=None, max_length=50)


class UpdateAlertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=FQN)
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    enabled: bool | None = None
    filters: dict[str, Any] | None = None
    destinations: list[dict[str, Any]] | None = Field(default=None, max_length=10)
    owners: list[Ref] | None = Field(default=None, max_length=50)


class TestAlertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=FQN)


class ReadNotificationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(default_factory=list, max_length=500)
    all: bool = False


class CreateServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=32)
    service_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=20_000)
    datasource_id: str | None = Field(default=None, max_length=64)
    connection: dict[str, Any] = Field(default_factory=dict)
    owners: list[Ref] | None = Field(default=None, max_length=50)
    interval_minutes: int | None = Field(default=None, ge=0, le=10080)
    schedule_enabled: bool | None = None
    run_now: bool = False


class RunIngestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_fqn: str = Field(min_length=1, max_length=FQN)
    options: dict[str, Any] | None = None
    background: bool = True


class ScheduleIngestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_fqn: str = Field(min_length=1, max_length=FQN)
    interval_minutes: int = Field(ge=0, le=10080)
    enabled: bool
    options: dict[str, Any] | None = None


class SyncDatasourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class AskCatalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    entity_type: str | None = Field(default=None, max_length=TYPE)
    ref: str | None = Field(default=None, max_length=FQN)
    datasource_id: str | None = Field(default=None, max_length=64)


class SuggestDescriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(min_length=1, max_length=TYPE)
    ref: str = Field(min_length=1, max_length=FQN)


class CallToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


# =========================================================================================
# catalog of types, health and overview
# =========================================================================================
@router.get("/health")
def metadata_health(request: Request):
    store: MetadataStore = _state(request, "metadata_store")
    health = store.healthy()
    state = request.app.state
    scheduler = getattr(state, "metadata_scheduler", None)
    alerts = getattr(state, "metadata_alerts", None)
    ingestor = getattr(state, "metadata_ingest", None)
    mcp = getattr(state, "metadata_mcp", None)
    health["boot_error"] = getattr(state, "metadata_boot_error", "")
    health["scheduler"] = scheduler.status() if scheduler is not None else None
    health["ingestion"] = ingestor.status() if ingestor is not None else None
    health["mcp"] = mcp.status() if mcp is not None else None
    try:
        health["alerts"] = alerts.status() if alerts is not None else None
    except (MetadataStoreError, MetadataError) as error:
        health["alerts"] = {"running": False, "last_error": str(error)}
    return health


@router.get("/types")
def metadata_types(request: Request):
    return {
        **guarded(_service(request).types),
        "kpi_charts": kpi_charts(),
        "tier_options": tier_options(),
        "edge_sources": list(EDGE_SOURCES),
        "destination_types": list(DESTINATION_TYPES),
        "max_lineage_depth": MAX_DEPTH,
    }


@router.get("/summary")
def metadata_summary(request: Request):
    return guarded(_service(request).summary)


@router.get("/tree")
def metadata_tree(request: Request, deleted: bool = False):
    return guarded(_service(request).tree, deleted=deleted)


# =========================================================================================
# entities
# =========================================================================================
@router.get("/entities")
def list_entities(
    request: Request,
    entity_type: str = Query(min_length=1, max_length=TYPE),
    q: str | None = Query(default=None, max_length=200),
    parent_fqn: str | None = Query(default=None, max_length=FQN),
    service_fqn: str | None = Query(default=None, max_length=FQN),
    service_type: str | None = Query(default=None, max_length=TYPE),
    deleted: bool = False,
    tier: str | None = Query(default=None, max_length=512),
    domain: str | None = Query(default=None, max_length=FQN),
    sort: str = Query(default="name", max_length=16),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(
        _service(request).list, entity_type, parent_fqn=parent_fqn, service_fqn=service_fqn,
        service_type=service_type, deleted=deleted, q=q, tier=tier, domain_fqn=domain, sort=sort,
        page=page, size=size,
    )


@router.post("/entities")
def create_entity(payload: CreateEntityInput, request: Request):
    body = _payload(payload, drop=("entity_type",))
    created = guarded(_service(request).create, payload.entity_type, body)
    _lineage(request).invalidate()
    return created


@router.post("/entities/update")
def update_entity(payload: UpdateEntityInput, request: Request):
    body = _payload(payload, drop=("entity_type", "ref"))
    return guarded(_service(request).update, payload.entity_type, payload.ref, body)


@router.post("/entities/rename")
def rename_entity(payload: RenameEntityInput, request: Request):
    renamed = guarded(_service(request).rename, payload.entity_type, payload.ref, payload.name)
    _lineage(request).invalidate()
    return renamed


@router.post("/entities/delete")
def delete_entity(payload: DeleteEntityInput, request: Request):
    result = guarded(_service(request).delete, payload.entity_type, payload.ref, hard=payload.hard)
    _lineage(request).invalidate()
    return result


@router.post("/entities/restore")
def restore_entity(payload: RestoreEntityInput, request: Request):
    restored = guarded(_service(request).restore, payload.entity_type, payload.ref)
    _lineage(request).invalidate()
    return restored


@router.post("/entities/follow")
def follow_entity(payload: FollowEntityInput, request: Request):
    service = _service(request)
    return guarded(service.follow, payload.entity_type, payload.ref, payload.user or service.user, follow=payload.follow)


@router.post("/entities/tags")
def set_entity_tags(payload: SetTagsInput, request: Request):
    return guarded(_service(request).set_tags, payload.target_fqn, payload.tags, target_type=payload.target_type)


@router.get("/entity")
def get_entity(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    view: bool = False,
):
    entity = guarded(_service(request).get, entity_type, ref)
    if view and entity["entity_type"] in DATA_ASSET_TYPES and not entity["deleted"]:
        _insights(request).record_view(entity["fqn"])
    return entity


@router.get("/entity/children")
def entity_children(
    request: Request,
    entity_type: str = Query(min_length=1, max_length=TYPE),
    ref: str = Query(min_length=1, max_length=FQN),
    deleted: bool = False,
):
    return guarded(_service(request).children, entity_type, ref, deleted=deleted)


@router.get("/entity/versions")
def entity_versions(request: Request, entity_type: str = Query(min_length=1, max_length=TYPE), ref: str = Query(min_length=1, max_length=FQN)):
    return guarded(_service(request).versions, entity_type, ref)


@router.get("/entity/version")
def entity_version(
    request: Request,
    entity_type: str = Query(min_length=1, max_length=TYPE),
    ref: str = Query(min_length=1, max_length=FQN),
    version: str = Query(min_length=1, max_length=16),
):
    return guarded(_service(request).version, entity_type, ref, version)


@router.get("/entity/context")
def entity_context(request: Request, ref: str = Query(min_length=1, max_length=FQN), entity_type: str | None = Query(default=None, max_length=TYPE)):
    return guarded(_context(request).entity_context, entity_type, ref)


@router.get("/entity/owned")
def entity_owned(request: Request, entity_type: str = Query(min_length=1, max_length=TYPE), ref: str = Query(min_length=1, max_length=FQN)):
    return guarded(_service(request).owned_by, entity_type, ref)


@router.get("/entity/assets")
def entity_assets(request: Request, entity_type: str = Query(min_length=1, max_length=TYPE), ref: str = Query(min_length=1, max_length=FQN)):
    return guarded(_service(request).domain_assets, entity_type, ref)


@router.get("/entity/usage")
def entity_usage(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    days: int = Query(default=30, ge=1, le=365),
):
    return guarded(_lineage(request).usage, entity_type, ref, days=days)


@router.get("/entity/queries")
def entity_queries(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    limit: int = Query(default=50, ge=1, le=200),
):
    return guarded(_lineage(request).queries_for, entity_type, ref, limit=limit)


@router.get("/entity/sample")
def entity_sample(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str = Query(default="table", max_length=TYPE),
    limit: int = Query(default=50, ge=1, le=MAX_SAMPLE_ROWS),
):
    """样本数据: a few rows read from the engine the table was crawled from."""
    row = guarded(_service(request).row, entity_type, ref)
    if row["entity_type"] != "table":
        raise HTTPException(400, "只有数据表可以查看样本数据。")
    document = row["json"]
    polaris = document.get("polaris") if isinstance(document.get("polaris"), dict) else {}
    datasource_id = str(document.get("datasource_id") or "")
    schema = str(document.get("source_schema") or "")
    name = str(document.get("source_table") or row["name"])
    if polaris.get("kind") == "generic" and polaris.get("origin_datasource"):
        datasource_id = str(polaris["origin_datasource"])
        schema = str(polaris.get("origin_schema") or "")
        name = str(polaris.get("origin_table") or name)
    if not datasource_id:
        raise HTTPException(400, "该数据表没有关联可查询的平台数据源，无法读取样本数据。")
    registry = _state(request, "sources")

    def read() -> dict[str, Any]:
        if datasource_id == LOCAL_SAMPLE:
            connector = registry.connector(LOCAL_SAMPLE)
            queries = _state(request, "queries")
            result = queries.store.execute_sql(f"SELECT * FROM {connector.quote(name)} LIMIT {int(limit)}")
        else:
            result = registry.connector(datasource_id).preview(schema or None, name, limit)
        return {
            "columns": result["columns"],
            "rows": result["rows"][:limit],
            "truncated": bool(result.get("truncated")),
            "sql": result.get("sql", ""),
            "elapsed_ms": result.get("elapsed_ms"),
            "datasource_id": datasource_id,
            "schema": schema,
            "table": name,
        }

    return guarded(read)


# =========================================================================================
# search
# =========================================================================================
@router.get("/search")
def search_assets(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    entity_types: str | None = Query(default=None, max_length=1000),
    service_type: str | None = Query(default=None, max_length=TYPE),
    service_fqn: str | None = Query(default=None, max_length=FQN),
    owner: str | None = Query(default=None, max_length=256),
    tag: str | None = Query(default=None, max_length=FQN),
    tier: str | None = Query(default=None, max_length=512),
    domain: str | None = Query(default=None, max_length=FQN),
    deleted: bool = False,
    sort: str = Query(default="name", max_length=16),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(
        _service(request).search, q, entity_types=_split(entity_types), service_type=service_type,
        service_fqn=service_fqn, owner=owner, tag=tag, tier=tier, domain=domain, deleted=deleted,
        sort=sort, page=page, size=size,
    )


# =========================================================================================
# lineage
# =========================================================================================
@router.get("/lineage")
def lineage_graph(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    upstream_depth: int = Query(default=3, ge=0, le=MAX_DEPTH),
    downstream_depth: int = Query(default=3, ge=0, le=MAX_DEPTH),
    include_deleted: bool = False,
):
    return guarded(
        _lineage(request).graph, entity_type, ref, upstream_depth=upstream_depth,
        downstream_depth=downstream_depth, include_deleted=include_deleted,
    )


@router.get("/lineage/edge")
def lineage_edge(request: Request, from_fqn: str = Query(min_length=1, max_length=FQN), to_fqn: str = Query(min_length=1, max_length=FQN)):
    return guarded(_lineage(request).edge, from_fqn, to_fqn)


@router.get("/lineage/edges")
def lineage_edges(request: Request, ref: str = Query(min_length=1, max_length=FQN), entity_type: str | None = Query(default=None, max_length=TYPE)):
    return guarded(_lineage(request).edges_of, entity_type, ref)


@router.post("/lineage/edges")
def add_lineage_edge(payload: AddLineageInput, request: Request):
    return guarded(_lineage(request).add_edge, payload.model_dump(exclude_none=True))


@router.post("/lineage/edges/delete")
def delete_lineage_edge(payload: DeleteLineageInput, request: Request):
    return guarded(_lineage(request).remove_edge, payload.from_fqn, payload.to_fqn)


@router.post("/lineage/sql")
def parse_lineage_sql(payload: ParseLineageInput, request: Request):
    return guarded(
        _lineage(request).from_sql, payload.sql, dialect=payload.dialect, datasource_id=payload.datasource_id,
        service_fqn=payload.service_fqn, target=payload.target, apply=payload.apply,
    )


@router.get("/lineage/impact")
def lineage_impact(
    request: Request,
    ref: str = Query(min_length=1, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    depth: int = Query(default=MAX_DEPTH, ge=1, le=MAX_DEPTH),
):
    return guarded(_lineage(request).impact, entity_type, ref, depth=depth)


@router.post("/lineage/views/sync")
def sync_view_lineage(payload: SyncViewLineageInput, request: Request):
    return guarded(_lineage(request).sync_views, payload.service_fqn)


@router.get("/lineage/summary")
def lineage_summary(request: Request):
    return guarded(_lineage(request).summary)


# =========================================================================================
# governance: glossaries, classifications, domains, teams
# =========================================================================================
@router.get("/glossaries")
def list_glossaries(request: Request, deleted: bool = False):
    return {"items": guarded(_service(request).glossaries, deleted=deleted)}


@router.get("/glossaries/terms")
def glossary_terms(request: Request, glossary: str = Query(min_length=1, max_length=FQN), deleted: bool = False):
    return guarded(_service(request).glossary_terms, glossary, deleted=deleted)


@router.get("/classifications")
def list_classifications(request: Request, deleted: bool = False):
    return {"items": guarded(_service(request).classifications, deleted=deleted)}


@router.get("/classifications/tags")
def classification_tags(request: Request, classification: str = Query(min_length=1, max_length=FQN), deleted: bool = False):
    return guarded(_service(request).classification_tags, classification, deleted=deleted)


@router.get("/tags/usage")
def tag_usage(request: Request, tag: str = Query(min_length=1, max_length=FQN)):
    return guarded(_service(request).tag_usage, tag)


@router.get("/tags/options")
def tag_options(request: Request, q: str | None = Query(default=None, max_length=200)):
    """Every tag and glossary term a picker may offer, flattened."""
    service = _service(request)
    store = service.store

    def load() -> dict[str, Any]:
        rows = store.all_entities(["tag", "glossaryTerm"], deleted=False, limit=20000)
        words = [w for w in (q or "").lower().split() if w]
        items = []
        for row in rows:
            if words and not all(w in row["search_text"] for w in words):
                continue
            document = row["json"]
            if row["entity_type"] == "tag" and document.get("disabled"):
                continue
            items.append(
                {
                    "fqn": row["fqn"],
                    "name": row["name"],
                    "display_name": row["display_name"] or row["name"],
                    "description": row["description"][:300],
                    "source": "classification" if row["entity_type"] == "tag" else "glossary",
                    "root": row["service_fqn"] or row["fqn"],
                    "status": document.get("status", ""),
                    "style": document.get("style") or {},
                }
            )
        items.sort(key=lambda item: (item["source"], item["fqn"]))
        return {"items": items[:2000], "total": len(items)}

    return guarded(load)


@router.get("/domains")
def list_domains(request: Request, deleted: bool = False):
    return guarded(_service(request).domains, deleted=deleted)


@router.get("/teams")
def list_teams(request: Request):
    return guarded(_service(request).teams)


@router.get("/people/options")
def people_options(request: Request):
    store = _service(request).store

    def load() -> dict[str, Any]:
        rows = store.all_entities(["user", "team"], deleted=False, limit=20000)
        return {"items": [{**summary_of(row), "is_bot": bool(row["json"].get("is_bot"))} for row in rows]}

    return guarded(load)


# =========================================================================================
# activity feed, conversations, tasks and announcements
# =========================================================================================
@router.get("/feed")
def activity_feed(
    request: Request,
    entity_fqn: str | None = Query(default=None, max_length=FQN),
    entity_type: str | None = Query(default=None, max_length=TYPE),
    event_type: str | None = Query(default=None, max_length=32),
    user: str | None = Query(default=None, max_length=64),
    include_children: bool = False,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(
        _service(request).feed, entity_fqn=entity_fqn, entity_type=entity_type, event_type=event_type,
        user_name=user, include_children=include_children, page=page, size=size,
    )


@router.get("/threads")
def list_threads(
    request: Request,
    about_fqn: str | None = Query(default=None, max_length=FQN),
    thread_type: str | None = Query(default=None, max_length=16),
    resolved: bool | None = None,
    include_children: bool = False,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(
        _service(request).threads, about_fqn=about_fqn, thread_type=thread_type, resolved=resolved,
        include_children=include_children, page=page, size=size,
    )


@router.post("/threads")
def create_thread(payload: CreateThreadInput, request: Request):
    return guarded(_service(request).create_thread, payload.model_dump(exclude_none=True))


@router.post("/threads/{thread_id}/posts")
def add_thread_post(thread_id: str, payload: CreatePostInput, request: Request):
    return guarded(_service(request).add_post, thread_id, payload.message)


@router.post("/threads/{thread_id}/resolve")
def resolve_thread_task(thread_id: str, payload: ResolveTaskInput, request: Request):
    return guarded(_service(request).resolve_task, thread_id, accept=payload.accept, message=payload.message)


@router.post("/threads/{thread_id}/close")
def close_thread(thread_id: str, payload: EmptyInput, request: Request):
    return guarded(_service(request).close_thread, thread_id)


@router.post("/threads/{thread_id}/delete")
def delete_thread(thread_id: str, payload: EmptyInput, request: Request):
    return guarded(_service(request).delete_thread, thread_id)


# =========================================================================================
# custom properties, import and export
# =========================================================================================
@router.get("/properties")
def list_properties(request: Request, entity_type: str | None = Query(default=None, max_length=TYPE)):
    return {"items": guarded(_service(request).property_definitions, entity_type)}


@router.post("/properties")
def define_property(payload: DefinePropertyInput, request: Request):
    body = payload.model_dump()
    entity_type = body.pop("entity_type")
    return guarded(_service(request).define_property, entity_type, body)


@router.post("/properties/update")
def update_property(payload: UpdatePropertyInput, request: Request):
    body = payload.model_dump(exclude={"entity_type", "name"})
    return guarded(_service(request).update_property, payload.entity_type, payload.name, body)


@router.post("/properties/delete")
def delete_property(payload: DeletePropertyInput, request: Request):
    return guarded(_service(request).remove_property, payload.entity_type, payload.name)


@router.get("/export")
def export_bundle(
    request: Request,
    entity_types: str | None = Query(default=None, max_length=1000),
    service_fqn: str | None = Query(default=None, max_length=FQN),
    include_deleted: bool = False,
):
    return guarded(_service(request).export, entity_types=_split(entity_types), service_fqn=service_fqn, include_deleted=include_deleted)


@router.post("/import")
def import_bundle(payload: ImportBundleInput, request: Request):
    result = guarded(_service(request).import_bundle, payload.bundle, fmt=payload.format, dry_run=payload.dry_run)
    _lineage(request).invalidate()
    return result


# =========================================================================================
# data insights and KPIs
# =========================================================================================
@router.get("/insights")
def data_insights(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    team: str | None = Query(default=None, max_length=256),
    tier: str | None = Query(default=None, max_length=512),
    domain: str | None = Query(default=None, max_length=FQN),
):
    return guarded(_insights(request).overview, days, team=team, tier=tier, domain=domain)


@router.post("/insights/snapshot")
def insight_snapshot(payload: EmptyInput, request: Request):
    return guarded(_insights(request).snapshot)


@router.get("/kpis")
def list_kpis(request: Request):
    return {"items": guarded(_insights(request).kpis)}


@router.get("/kpis/detail")
def kpi_detail(request: Request, ref: str = Query(min_length=1, max_length=FQN)):
    return guarded(_insights(request).kpi, ref)


# =========================================================================================
# alerts and notifications
# =========================================================================================
@router.get("/alerts")
def list_alerts(request: Request):
    service = _service(request)
    alerts = _alerts(request)

    def load() -> dict[str, Any]:
        listing = service.list("eventSubscription", deleted=False, page=1, size=MAX_PAGE_SIZE)
        return {**listing, "status": alerts.status()}

    return guarded(load)


@router.post("/alerts")
def create_alert(payload: CreateAlertInput, request: Request):
    document = guarded(_alerts(request).validate, payload.model_dump(exclude_none=True))
    return guarded(_service(request).create, "eventSubscription", document)


@router.post("/alerts/update")
def update_alert(payload: UpdateAlertInput, request: Request):
    service = _service(request)
    row = guarded(service.row, "eventSubscription", payload.ref)
    stored = row["json"]
    data = payload.model_dump(exclude_unset=True)
    data.pop("ref", None)
    merged = {
        "filters": stored.get("filters") or {},
        "destinations": stored.get("destinations") or [],
        "enabled": stored.get("enabled", True),
        **{key: value for key, value in data.items() if value is not None},
    }
    document = guarded(_alerts(request).validate, merged, stored.get("destinations") or [])
    return guarded(service.update, "eventSubscription", row["id"], document)


@router.post("/alerts/test")
def test_alert(payload: TestAlertInput, request: Request):
    return guarded(_alerts(request).test, payload.ref)


@router.get("/notifications")
def list_notifications(
    request: Request,
    subscription_id: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=16),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(_alerts(request).notifications, subscription_id=subscription_id, status=status, page=page, size=size)


@router.post("/notifications/read")
def read_notifications(payload: ReadNotificationsInput, request: Request):
    return guarded(_alerts(request).mark_read, payload.ids, all_unread=payload.all)


# =========================================================================================
# ingestion (元数据拾取)
# =========================================================================================
@router.get("/ingestion/categories")
def ingestion_categories(request: Request):
    types = guarded(_service(request).types)
    return {"items": types["service_categories"], "lattice_connectors": types["lattice_connectors"]}


@router.get("/ingestion/services")
def ingestion_services(request: Request):
    return {"items": guarded(_ingestor(request).services), "status": _ingestor(request).status()}


@router.post("/ingestion/services")
def create_ingestion_service(payload: CreateServiceInput, request: Request):
    ingestor = _ingestor(request)
    body = payload.model_dump(exclude_unset=True)
    run_now = bool(body.pop("run_now", False))
    created = guarded(ingestor.create_service, body)
    if run_now and body.get("datasource_id"):
        _background("lattice-metadata-ingest", ingestor.run, created["fqn"], trigger="manual")
    return created


@router.post("/ingestion/sync-datasources")
def sync_datasources(payload: SyncDatasourcesInput, request: Request):
    return guarded(_ingestor(request).sync_datasources, force=payload.force)


@router.post("/ingestion/run")
def run_ingestion(payload: RunIngestionInput, request: Request):
    ingestor = _ingestor(request)
    row = guarded(_service(request).row, None, payload.service_fqn)
    guarded(ingestor.check_runnable, row)
    if not payload.background:
        return guarded(ingestor.run, row["fqn"], trigger="manual", options=payload.options)
    if ingestor.is_running(row["fqn"]):
        raise HTTPException(409, "该服务正在拾取中，请稍后再试。")
    _background("lattice-metadata-ingest", ingestor.run, row["fqn"], trigger="manual", options=payload.options)
    return {"started": True, "service": summary_of(row)}


@router.post("/ingestion/run-all")
def run_all_ingestion(payload: EmptyInput, request: Request):
    _background("lattice-metadata-ingest-all", _ingestor(request).run_all, trigger="manual")
    return {"started": True}


@router.post("/ingestion/schedule")
def schedule_ingestion(payload: ScheduleIngestionInput, request: Request):
    return guarded(
        _ingestor(request).schedule, payload.service_fqn, interval_minutes=payload.interval_minutes,
        enabled=payload.enabled, options=payload.options,
    )


@router.post("/ingestion/semantic-models/sync")
def sync_semantic_models(payload: EmptyInput, request: Request):
    return guarded(_ingestor(request).sync_semantic_models)


@router.get("/ingestion/runs")
def ingestion_runs(
    request: Request,
    service_fqn: str | None = Query(default=None, max_length=FQN),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    return guarded(_ingestor(request).runs, service_fqn=service_fqn, page=page, size=size)


@router.get("/ingestion/runs/{run_id}")
def ingestion_run(run_id: str, request: Request):
    return guarded(_ingestor(request).run_detail, run_id)


# =========================================================================================
# context layer: assistant, data-source semantics, tools, MCP
# =========================================================================================
@router.get("/context/datasource")
def datasource_context(request: Request, datasource_id: str = Query(min_length=1, max_length=64)):
    return guarded(_context(request).call_tool, "get_datasource_context", {"datasource_id": datasource_id})


@router.post("/context/ask")
def ask_catalog(payload: AskCatalogInput, request: Request):
    return guarded(
        _context(request).ask, payload.question, entity_type=payload.entity_type, ref=payload.ref,
        datasource_id=payload.datasource_id,
    )


@router.post("/context/suggest")
def suggest_descriptions(payload: SuggestDescriptionInput, request: Request):
    return guarded(_context(request).suggest_descriptions, payload.entity_type, payload.ref)


@router.get("/context/tools")
def context_tools(request: Request):
    tools = guarded(_context(request).tools)
    return {
        "mcp": tools,
        "openai": [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in tools],
        "anthropic": [{"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]} for t in tools],
    }


@router.post("/context/tools/call")
def call_context_tool(payload: CallToolInput, request: Request):
    return {"name": payload.name, "result": guarded(_context(request).call_tool, payload.name, payload.arguments)}


@router.get("/context/mcp")
def mcp_status(request: Request):
    server = _mcp(request)
    host = request.headers.get("host", "127.0.0.1:8787")
    url = f"{request.url.scheme}://{host}{MCP_PATH}"
    context = server.context
    return {
        **server.status(),
        "url": url,
        "clients": {
            "claude_code": f"claude mcp add --transport http lattice-metadata {url}",
            "claude_desktop": {"mcpServers": {"lattice-metadata": {"command": "npx", "args": ["-y", "mcp-remote", url]}}},
            "cursor": {"mcpServers": {"lattice-metadata": {"url": url}}},
            "http": {"type": "streamable-http", "url": url},
        },
        "tool_definitions": guarded(context.tools),
        "resources": context.resources(),
        "resource_templates": context.resource_templates(),
        "prompts": context.prompts(),
    }


def _rpc_error(code: int, message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


def _initializes(message: Any) -> bool:
    items = message if isinstance(message, list) else [message]
    return any(isinstance(item, dict) and item.get("method") == "initialize" for item in items)


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request):
    """Model Context Protocol, Streamable HTTP transport (JSON responses)."""
    server = getattr(request.app.state, "metadata_mcp", None)
    if server is None:
        return _rpc_error(-32603, "元数据管理模块尚未初始化。", 503)
    version = request.headers.get("mcp-protocol-version")
    if version and version not in PROTOCOL_VERSIONS:
        return _rpc_error(-32600, f"Unsupported MCP-Protocol-Version: {version}", 400)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return _rpc_error(-32600, "Content-Type must be application/json", 415)
    raw = await request.body()
    try:
        message = json.loads(raw) if raw else None
    except ValueError:
        return _rpc_error(-32700, "Parse error", 400)
    if message is None:
        return _rpc_error(-32600, "Invalid Request", 400)
    session = request.headers.get("mcp-session-id")
    if session and not _initializes(message) and not server.known_session(session):
        return _rpc_error(-32001, "Session not found", 404)
    response, new_session = await run_in_threadpool(server.handle_http, message)
    headers = {"Cache-Control": "no-store"}
    if new_session:
        headers["Mcp-Session-Id"] = new_session
    if response is None:
        return Response(status_code=202, headers=headers)
    return JSONResponse(response, headers=headers)


@router.get("/mcp", include_in_schema=False)
def mcp_stream_unsupported():
    return Response(status_code=405, headers={"Allow": "POST, DELETE", "Cache-Control": "no-store"})


@router.delete("/mcp", include_in_schema=False)
def mcp_close_session(request: Request):
    server = getattr(request.app.state, "metadata_mcp", None)
    session = request.headers.get("mcp-session-id")
    if server is None or not session:
        return Response(status_code=400, headers={"Cache-Control": "no-store"})
    if not server.close_session(session):
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/categories")
def service_categories():
    return {"items": [{"id": key, "label": spec["label"], "entity_type": spec["entity_type"]} for key, spec in SERVICE_CATEGORIES.items()]}
