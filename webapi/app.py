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

"""Same-origin local WebUI and API for Lattice, Apache Polaris and data sources."""

from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import httpx
from pydantic import BaseModel, ConfigDict, Field
import sqlglot
import yaml

from validation import validate as validator
from .connectors import CONNECTORS, ConnectorError
from .datasources import DataSourceError, DataSourceRegistry
from .ingest import IngestError, IngestService
from .llm import LlmError, LlmSettings, SqlGenerator
from .models import ModelStoreError, PolarisModelStore, parse_document
from .polaris import PolarisClient, PolarisError
from .query import QUESTIONS, QueryStore
from .quality_api import router as quality_router
from .quality_service import QualityScheduler, QualityService
from .quality_store import QualityStore
from .questions import QueryError, QueryService
from .semantic_models import (
    OPERATIONS as SEMANTIC_OPERATIONS,
    SemanticModelError,
    SemanticModelService,
    check_prefix,
    document_from_yaml,
    document_yaml,
    error_response,
    namespace_parts,
)
from .specs import SpecRegistry

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("LATTICE_WEB_RUNTIME", str(ROOT / ".runtime" / "webui")))
POLARIS_RUNTIME = ROOT / ".runtime" / "polaris"
ENGINES_RUNTIME = ROOT / ".runtime" / "engines"
STATIC = ROOT / "web" / "dist"
MAX_BODY = 1024 * 1024
POLARIS_PORT = 8181
POLARIS_HEALTH_PORT = 8182


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str | None = Field(default=None, max_length=2000)
    sql: str | None = Field(default=None, max_length=30000)
    datasource_id: str | None = Field(default=None, max_length=64)


class ValidationInput(BaseModel):
    yaml: str = Field(max_length=MAX_BODY)


class PolarisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: str
    operation_id: str
    path_params: dict = Field(default_factory=dict)
    query: dict = Field(default_factory=dict)
    headers: dict = Field(default_factory=dict)
    body: object = None


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    yaml: str = Field(max_length=256 * 1024)


class SemanticPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog: str = Field(default="lattice", max_length=255)
    namespace: str | list[str] = "demo"
    name: str = Field(max_length=200)
    yaml: str = Field(max_length=256 * 1024)
    entity_version: str | None = Field(default=None, max_length=255)


class SemanticDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog: str = Field(default="lattice", max_length=255)
    namespace: str | list[str] = "demo"
    name: str = Field(max_length=200)
    entity_version: str = Field(max_length=255)


class ModelDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)
    type: str = Field(max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    description: str | None = Field(default=None, max_length=500)


class SourceUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] | None = None


class SourceTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    id: str | None = Field(default=None, max_length=64)


class SourceQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(max_length=30000)
    limit: int | None = Field(default=None, ge=1, le=5000)


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    name: str = Field(max_length=255)
    limit: int | None = Field(default=None, ge=1, le=5000)


class LlmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)


class IngestRegisterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    datasource_id: str = Field(max_length=64)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    name: str = Field(max_length=255)
    catalog: str | None = Field(default=None, max_length=128)
    namespace: str | list[str] | None = None
    table_name: str | None = Field(default=None, max_length=128)


class IngestUnregisterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog: str | None = Field(default=None, max_length=128)
    namespace: str | list[str] | None = None
    name: str = Field(max_length=128)


def model_document(tables):
    primary_keys = {
        "t_lattice_order_items": "order_id",
        "t_lattice_orders": "order_id",
        "t_lattice_customers": "customer_id",
        "t_lattice_products": "product_id",
        "t_lattice_sellers": "seller_id",
        "t_lattice_payments": "order_id",
    }

    def datatype(value):
        return (
            "Date"
            if value == "DATE"
            else (
                "Decimal"
                if "DECIMAL" in value
                else "Integer" if "INT" in value else "String"
            )
        )

    datasets = [
        {
            "name": table["name"],
            "source": "local.main." + table["name"],
            "description": table["label"] + "（本地生成的示例数据）",
            "primary_key": [primary_keys[table["name"]]],
            "fields": [
                {
                    "name": column["name"],
                    "datatype": datatype(column["type"]),
                    "expression": {
                        "dialects": [
                            {"dialect": "ANSI_SQL", "expression": column["name"]}
                        ]
                    },
                }
                for column in table["columns"]
            ],
        }
        for table in tables
    ]
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "lattice_demo_sales",
                "description": "Lattice 本地演示销售模型，数据不来自真实业务系统",
                "datasets": datasets,
                "metrics": [
                    {
                        "name": "total_sales",
                        "datatype": "Decimal",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "SUM(t_lattice_order_items.price)",
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


@asynccontextmanager
async def lifespan(app):
    app.state.store = QueryStore(RUNTIME)
    app.state.registry = SpecRegistry(ROOT / "integrations" / "polaris" / "spec")
    app.state.polaris = PolarisClient(POLARIS_RUNTIME / "credentials.json", app.state.registry)
    app.state.models = PolarisModelStore(app.state.polaris)
    app.state.semantic = SemanticModelService(app.state.polaris)
    app.state.sources = DataSourceRegistry(RUNTIME, POLARIS_RUNTIME, ENGINES_RUNTIME)
    app.state.llm = LlmSettings(RUNTIME / "llm.json")
    app.state.queries = QueryService(app.state.store, app.state.sources, app.state.llm)
    app.state.ingest = IngestService(app.state.polaris, app.state.sources)
    # The quality metadata lives in a real PostgreSQL database. An unreachable
    # database must not stop the rest of the platform from starting: bootstrap
    # reports through health() instead of raising out of the lifespan.
    app.state.quality_store = QualityStore()
    app.state.quality_store.bootstrap()
    app.state.quality = QualityService(app.state.quality_store, app.state.sources)
    app.state.quality_scheduler = QualityScheduler(
        app.state.quality_store, app.state.quality
    )
    app.state.quality_scheduler.start()
    try:
        yield
    finally:
        app.state.quality_scheduler.stop()
        app.state.quality_store.close()


app = FastAPI(
    title="Lattice 数据平台",
    version="0.2.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


app.include_router(quality_router)


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    host = request.headers.get("host", "")
    try:
        hostname = urlsplit("//" + host).hostname
    except ValueError:
        hostname = None
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return JSONResponse({"detail": "只允许本机地址访问。"}, status_code=403)
    if request.url.path.startswith("/api/"):
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "拒绝跨站请求。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{host}":
            return JSONResponse({"detail": "拒绝跨来源请求。"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (
                request.headers.get("content-type", "").split(";")[0].strip()
                != "application/json"
            ):
                return JSONResponse(
                    {"detail": "API 请求必须使用 application/json。"}, status_code=415
                )
            try:
                declared_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度。"}, status_code=400)
            if declared_length > MAX_BODY:
                return JSONResponse(
                    {"detail": "请求体不能超过 1 MB。"}, status_code=413
                )
            # Check even chunked requests without trusting Content-Length.
            body = await request.body()
            if len(body) > MAX_BODY:
                return JSONResponse(
                    {"detail": "请求体不能超过 1 MB。"}, status_code=413
                )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def failure(error: Exception) -> HTTPException:
    """Map service errors to HTTP responses without leaking tracebacks."""
    if isinstance(error, HTTPException):
        return error
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return HTTPException(status, str(error))
    if isinstance(error, (ConnectorError, LlmError, ValueError)):
        return HTTPException(400, str(error)[:800])
    if isinstance(error, (PolarisError, httpx.HTTPError, OSError, KeyError)):
        return HTTPException(502, "Polaris 请求失败：" + str(error)[:300])
    return HTTPException(500, str(error)[:300])


def guarded(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (
        HTTPException,
        DataSourceError,
        QueryError,
        IngestError,
        ModelStoreError,
        ConnectorError,
        LlmError,
        PolarisError,
        httpx.HTTPError,
        OSError,
        KeyError,
        ValueError,
        sqlglot.errors.SqlglotError,
        duckdb.Error,
    ) as error:
        raise failure(error) from error


def links(request: Request) -> dict[str, str]:
    host = request.headers.get("host", "127.0.0.1:8787")
    return {
        "webapi": host,
        "polaris": f"127.0.0.1:{POLARIS_PORT}",
        "polaris_health": f"127.0.0.1:{POLARIS_HEALTH_PORT}/q/health",
    }


def build_id() -> str:
    """Identify the built WebUI so an open tab can notice it has gone stale.

    A single-page app never re-requests its entry document while the tab stays
    open, so a rebuild alone does not reach it — the page keeps running the
    bundle it loaded.  That document names the content-hashed bundles, so its
    digest changes on every rebuild and on nothing else; a tab that sees a
    different one knows to reload."""
    try:
        return hashlib.sha256((STATIC / "index.html").read_bytes()).hexdigest()[:16]
    except OSError:  # the WebUI is not built; the page cannot be stale either
        return ""


@app.get("/api/health")
def health(request: Request):
    return {
        "status": "ok",
        "application": "lattice-webui",
        "build": build_id(),
        "instance_id": os.environ.get("LATTICE_WEB_INSTANCE_ID", ""),
        "services": {"polaris": request.app.state.polaris.status()},
        "demo": True,
    }


@app.get("/api/bootstrap")
def bootstrap(request: Request):
    state = request.app.state
    tables = state.store.metadata()
    statuses = guarded(state.sources.statuses)
    return {
        "tables": tables,
        "example_questions": QUESTIONS,
        "model_yaml": yaml.safe_dump(
            model_document(tables), allow_unicode=True, sort_keys=False
        ),
        "polaris": state.polaris.status(),
        "datasources": {
            "count": len(statuses),
            "online": sum(1 for item in statuses if item["status"] == "online"),
            "items": statuses,
        },
        "llm": state.llm.status(),
        "links": links(request),
        "capabilities": [
            "多数据源问数与只读 SQL",
            "SSE 流式思考过程",
            "Apache Ossie 规范校验",
            "Polaris Management",
            "Iceberg REST Catalog",
            "通用表（数据接入）",
            "策略治理",
            "Polaris 原生语义模型接口（Lattice 网关实现）",
            "Lattice 模型存储（Generic Table 扩展）",
            *[f"数据源：{connector.label}" for connector in CONNECTORS.values()],
        ],
    }


def _check_query_input(payload: QueryInput) -> None:
    if bool(payload.question and payload.question.strip()) == bool(
        payload.sql and payload.sql.strip()
    ):
        raise HTTPException(400, "请只提供 question 或 sql 其中一项。")


@app.post("/api/query")
def query(payload: QueryInput, request: Request):
    _check_query_input(payload)
    return guarded(
        request.app.state.queries.complete,
        payload.question,
        payload.sql,
        payload.datasource_id,
    )


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/query/stream")
def query_stream(payload: QueryInput, request: Request):
    _check_query_input(payload)
    service = request.app.state.queries

    def events():
        try:
            for name, data in service.answer(payload.question, payload.sql, payload.datasource_id):
                yield sse(name, data)
        except (
            DataSourceError,
            QueryError,
            ConnectorError,
            LlmError,
            ValueError,
            sqlglot.errors.SqlglotError,
            duckdb.Error,
        ) as error:
            yield sse("error", {"detail": str(error)[:800]})
        except (PolarisError, httpx.HTTPError, OSError, KeyError) as error:
            yield sse("error", {"detail": "服务请求失败：" + str(error)[:300]})
        yield sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
def history(request: Request):
    return {"items": request.app.state.store.history()}


@app.post("/api/validate")
def validate(payload: ValidationInput):
    try:
        data = parse_document(payload.yaml)
        schema = json.loads((ROOT / "core-spec" / "ossie-schema.json").read_text())
        failures = validator.validate_schema(data, schema)
        warnings = []
        if not failures:
            messages = (
                validator.validate_unique_names(data)
                + validator.validate_references(data)
                + validator.validate_sql(data)
            )
            warning_prefixes = ("[Reference] Warning:", "[SQL] Warning:")
            warnings = [
                value for value in messages if value.startswith(warning_prefixes)
            ]
            failures = [
                value for value in messages if not value.startswith(warning_prefixes)
            ]
        return {
            "valid": not failures,
            "errors": [str(item)[:1000] for item in failures[:50]],
            "warnings": [str(item)[:1000] for item in warnings[:50]],
        }
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": []}


# ----- data sources ---------------------------------------------------------------
@app.get("/api/datasources")
def list_sources(request: Request):
    return guarded(request.app.state.sources.list)


@app.post("/api/datasources")
def create_source(payload: SourceCreateInput, request: Request):
    return guarded(
        request.app.state.sources.create,
        payload.name,
        payload.type,
        payload.config,
        payload.description,
    )


@app.post("/api/datasources/test")
def test_source_config(payload: SourceTestInput, request: Request):
    return guarded(
        request.app.state.sources.test_config, payload.type, payload.config, payload.id
    )


@app.get("/api/datasources/{source_id}")
def get_source(source_id: str, request: Request):
    return guarded(request.app.state.sources.get, source_id)


@app.post("/api/datasources/{source_id}/update")
def update_source(source_id: str, payload: SourceUpdateInput, request: Request):
    return guarded(
        request.app.state.sources.update,
        source_id,
        payload.model_dump(exclude_unset=True),
    )


@app.post("/api/datasources/{source_id}/delete")
def delete_source(source_id: str, payload: EmptyInput, request: Request):
    guarded(request.app.state.sources.delete, source_id)
    return {"deleted": True}


@app.post("/api/datasources/restore")
def restore_sources(payload: EmptyInput, request: Request):
    """Bring back builtin data sources that were removed from the list."""
    return guarded(request.app.state.sources.restore)


@app.post("/api/datasources/{source_id}/test")
def test_source(source_id: str, payload: EmptyInput, request: Request):
    return guarded(request.app.state.sources.test, source_id)


@app.get("/api/datasources/{source_id}/schemas")
def source_schemas(source_id: str, request: Request):
    connector = guarded(request.app.state.sources.connector, source_id)
    return {"items": guarded(connector.schemas)}


@app.get("/api/datasources/{source_id}/tables")
def source_tables(source_id: str, request: Request, schema: str | None = None):
    connector = guarded(request.app.state.sources.connector, source_id)
    if not schema:
        schema = connector.default_schema()
        if not schema:
            schemas = guarded(connector.schemas)
            if not schemas:
                return {"items": [], "schema": None}
            schema = schemas[0]["name"]
    return {"items": guarded(connector.tables, schema), "schema": schema}


@app.get("/api/datasources/{source_id}/table")
def source_table(source_id: str, name: str, request: Request, schema: str | None = None):
    connector = guarded(request.app.state.sources.connector, source_id)
    schema = schema or connector.default_schema() or ""
    return guarded(connector.table, schema, name)


@app.post("/api/datasources/{source_id}/preview")
def source_preview(source_id: str, payload: PreviewInput, request: Request):
    return guarded(
        request.app.state.queries.preview,
        source_id,
        payload.schema_name,
        payload.name,
        payload.limit,
    )


@app.post("/api/datasources/{source_id}/query")
def source_query(source_id: str, payload: SourceQueryInput, request: Request):
    return guarded(request.app.state.queries.run_sql, source_id, payload.sql, payload.limit)


# ----- LLM settings ---------------------------------------------------------------
@app.get("/api/llm")
def llm_status(request: Request):
    return request.app.state.llm.status()


@app.post("/api/llm")
def llm_update(payload: LlmInput, request: Request):
    result = guarded(request.app.state.llm.save, payload.model_dump(exclude_unset=True))
    request.app.state.sources.invalidate()
    return result


@app.get("/api/llm/catalog")
def llm_catalog(request: Request):
    """Providers and their known models, so the UI configures by choosing."""
    return request.app.state.llm.catalog()


@app.post("/api/llm/models")
def llm_models(payload: LlmInput, request: Request):
    """Models the chosen provider actually serves, merged with the known list."""
    return guarded(
        request.app.state.llm.models, payload.model_dump(exclude_unset=True)
    )


@app.post("/api/llm/test")
def llm_test(payload: EmptyInput, request: Request):
    return guarded(SqlGenerator(request.app.state.llm).test)


# ----- Polaris ingestion ----------------------------------------------------------
@app.get("/api/ingest/catalog")
def ingest_catalog(request: Request, catalog: str | None = None):
    return guarded(request.app.state.ingest.catalog, catalog)


@app.post("/api/ingest/register")
def ingest_register(payload: IngestRegisterInput, request: Request):
    return guarded(
        request.app.state.ingest.register,
        payload.datasource_id,
        payload.schema_name,
        payload.name,
        payload.catalog,
        payload.namespace,
        payload.table_name,
    )


@app.post("/api/ingest/unregister")
def ingest_unregister(payload: IngestUnregisterInput, request: Request):
    guarded(
        request.app.state.ingest.unregister, payload.catalog, payload.namespace, payload.name
    )
    return {"deleted": True}


# ----- Polaris gateway ------------------------------------------------------------
@app.get("/api/polaris/specs")
def specs(request: Request):
    return request.app.state.registry.public_specs()


@app.get("/api/polaris/openapi/{spec_id}.json")
def openapi(spec_id: str, request: Request):
    try:
        return request.app.state.registry.bundled(spec_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/polaris/overview")
def polaris_overview(request: Request):
    return request.app.state.polaris.overview()


@app.post("/api/polaris/request")
def polaris_request(payload: PolarisInput, request: Request):
    if payload.spec == "catalog" and payload.operation_id in SEMANTIC_OPERATIONS:
        # Polaris 1.7.0 returns 501 for these; the gateway serves the documented contract.
        try:
            request.app.state.registry.operation(payload.spec, payload.operation_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return request.app.state.semantic.handle(
            payload.operation_id, payload.path_params, payload.query, payload.body
        )
    try:
        return request.app.state.polaris.request(
            payload.spec,
            payload.operation_id,
            payload.path_params,
            payload.query,
            payload.body,
            payload.headers,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (PolarisError, httpx.HTTPError, OSError, KeyError) as exc:
        raise HTTPException(502, "Polaris 请求失败：" + str(exc)) from exc


# ----- Native semantic-model API (Polaris contract, served by the gateway) ---------
def _semantic_namespace(value) -> str:
    if isinstance(value, list):
        if not all(isinstance(part, str) for part in value):
            raise HTTPException(400, "namespace 必须是文本或文本数组。")
        return "\x1f".join(value)
    return str(value)


def _semantic_target(catalog, namespace):
    try:
        return check_prefix(catalog), namespace_parts(_semantic_namespace(namespace))
    except SemanticModelError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _semantic_result(response: dict, success: set[int]):
    if response["status"] in success:
        return response["body"]
    body = response.get("body")
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    raise HTTPException(
        response["status"] if 400 <= response["status"] < 600 else 502,
        message or f"语义模型接口返回 HTTP {response['status']}",
    )


@app.get("/api/semantic-models")
def semantic_models(request: Request, catalog: str = "lattice", namespace: str = "demo"):
    prefix, parts = _semantic_target(catalog, namespace)
    try:
        return request.app.state.semantic.catalog_listing(prefix, parts)
    except SemanticModelError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.get("/api/semantic-models/load")
def semantic_model_load(request: Request, name: str, catalog: str = "lattice", namespace: str = "demo"):
    prefix, parts = _semantic_target(catalog, namespace)
    try:
        response = request.app.state.semantic.load(prefix, parts, name)
    except SemanticModelError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    body = _semantic_result(response, {200})
    try:
        rendered = document_yaml(body["document"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, "存储的模型文档无法转换为 YAML。") from exc
    return {
        "catalog": prefix,
        "namespace": parts,
        "name": name,
        "entity_version": body["entity-version"],
        "document": body["document"],
        "yaml": rendered,
    }


@app.post("/api/semantic-models/publish")
def semantic_model_publish(payload: SemanticPublishInput, request: Request):
    prefix, parts = _semantic_target(payload.catalog, payload.namespace)
    service = request.app.state.semantic
    try:
        document = document_from_yaml(payload.yaml)
        if payload.entity_version:
            response = service.update(
                prefix, parts, payload.name,
                {"document": document, "entity-version": payload.entity_version},
            )
            action = "updated"
        else:
            response = service.create(prefix, parts, {"name": payload.name, "document": document})
            action = "created"
    except SemanticModelError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    body = _semantic_result(response, {200})
    return {
        "catalog": prefix,
        "namespace": parts,
        "name": payload.name,
        "action": action,
        "entity_version": body["entity-version"],
        "document": body["document"],
    }


@app.post("/api/semantic-models/delete")
def semantic_model_delete(payload: SemanticDeleteInput, request: Request):
    prefix, parts = _semantic_target(payload.catalog, payload.namespace)
    service = request.app.state.semantic
    try:
        response = service.drop(
            prefix, parts, payload.name, entity_version=payload.entity_version
        )
    except SemanticModelError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    _semantic_result(response, {204})
    return {"deleted": True, "name": payload.name}


SEMANTIC_REST_BASE = "/polaris/v1/{prefix}/namespaces/{namespace}/semantic-models"


def _semantic_error(status: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        error_response(status, error_type, message)["body"],
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def _semantic_body(request: Request):
    """Read a bounded JSON body, refusing an oversized one before buffering it."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None, _semantic_error(415, "UnsupportedMediaTypeException", "application/json required")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY:
                raise ValueError
        except ValueError:
            return None, _semantic_error(413, "PayloadTooLargeException", "request body exceeds 1 MB")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return None, _semantic_error(413, "PayloadTooLargeException", "request body exceeds 1 MB")
    try:
        return (json.loads(raw) if raw else None), None
    except ValueError:
        return None, _semantic_error(400, "BadRequestException", "malformed JSON body")


async def _semantic_rest(request: Request, operation_id: str, prefix: str, namespace: str, name: str | None):
    """Same-path REST access for external clients, acting as their own Polaris principal."""
    service = request.app.state.semantic
    try:
        checked_prefix = check_prefix(prefix)
        parts = namespace_parts(namespace)
        # Authenticate before reading any request body.
        token = await run_in_threadpool(
            service.authorize, request.headers.get("authorization"), checked_prefix, parts
        )
    except SemanticModelError as exc:
        return _semantic_error(exc.status, exc.error_type, str(exc))
    body = None
    if request.method in {"POST", "PUT"}:
        body, failure = await _semantic_body(request)
        if failure is not None:
            return failure
    path_params = {"prefix": checked_prefix, "namespace": namespace}
    if name is not None:
        path_params["semantic-model-name"] = name
    response = await run_in_threadpool(
        service.handle, operation_id, path_params, dict(request.query_params), body, token
    )
    headers = {**response.get("headers", {}), "Cache-Control": "no-store"}
    if response["status"] == 204:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(response["body"], status_code=response["status"], headers=headers)


@app.post(SEMANTIC_REST_BASE, include_in_schema=False)
async def semantic_rest_create(prefix: str, namespace: str, request: Request):
    return await _semantic_rest(request, "createSemanticModel", prefix, namespace, None)


@app.get(SEMANTIC_REST_BASE, include_in_schema=False)
async def semantic_rest_list(prefix: str, namespace: str, request: Request):
    return await _semantic_rest(request, "listSemanticModels", prefix, namespace, None)


@app.get(SEMANTIC_REST_BASE + "/{name}", include_in_schema=False)
async def semantic_rest_load(prefix: str, namespace: str, name: str, request: Request):
    return await _semantic_rest(request, "loadSemanticModel", prefix, namespace, name)


@app.put(SEMANTIC_REST_BASE + "/{name}", include_in_schema=False)
async def semantic_rest_update(prefix: str, namespace: str, name: str, request: Request):
    return await _semantic_rest(request, "updateSemanticModel", prefix, namespace, name)


@app.delete(SEMANTIC_REST_BASE + "/{name}", include_in_schema=False)
async def semantic_rest_drop(prefix: str, namespace: str, name: str, request: Request):
    return await _semantic_rest(request, "dropSemanticModel", prefix, namespace, name)


def model_call(operation, *args):
    try:
        return operation(*args)
    except ModelStoreError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (PolarisError, httpx.HTTPError, OSError, KeyError) as exc:
        raise HTTPException(502, "模型存储请求失败：" + str(exc)) from exc


@app.get("/api/models")
def models(request: Request):
    return model_call(request.app.state.models.list)


@app.post("/api/models")
def create_model(payload: ModelInput, request: Request):
    return model_call(request.app.state.models.create, payload.name, payload.yaml)


@app.get("/api/models/{model_id}")
def get_model(model_id: str, request: Request):
    return model_call(request.app.state.models.get, model_id)


@app.post("/api/models/{model_id}/delete")
def delete_model(model_id: str, payload: ModelDeleteInput, request: Request):
    return model_call(request.app.state.models.delete, model_id, payload.sha256)


class HashedAssets(StaticFiles):
    """The bundler content-hashes every name under /assets, so a file never
    changes in place: a new build writes a new name.  Freezing them in the
    browser cache is therefore safe, and keeps the revalidation below cheap."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if (STATIC / "assets").is_dir():
    app.mount("/assets", HashedAssets(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "API 不存在。")
    if not (STATIC / "index.html").is_file():
        raise HTTPException(503, "WebUI 尚未构建，请运行 ./start-web.sh。")
    # Without an explicit directive these carry only ETag/Last-Modified, which
    # lets a browser cache them heuristically (RFC 9111 §4.2.2) and skip
    # revalidation.  The entry document names the hashed bundles, so a stale
    # copy keeps loading the bundle it names: a rebuilt WebUI then never
    # reaches an open tab.  "no-cache" still stores the body and still answers
    # 304 from the ETag; it only forbids using it without asking.
    headers = {"Cache-Control": "no-cache"}
    if path == "favicon.svg" and (STATIC / path).is_file():
        return FileResponse(STATIC / path, headers=headers)
    return FileResponse(STATIC / "index.html", headers=headers)
