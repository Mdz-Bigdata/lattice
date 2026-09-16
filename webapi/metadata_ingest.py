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

"""Metadata ingestion (元数据拾取) of the 元数据管理 module.

OpenMetadata fills its catalog with *ingestion workflows* that crawl a
service and write what they find as entities. Lattice already knows how to
talk to nine engines, so the crawler here is the platform's own connector
surface (``schemas`` → ``tables`` → ``table``) walked once per service:

* every registered data source becomes a *database service* whose service
  type is the OpenMetadata connector name (``Mysql``, ``Postgres`` …); a
  crawl writes the database, schemas, tables and columns under it, versions
  what changed, restores what came back and soft-deletes what disappeared;
* the local Apache Polaris instance becomes the ``polaris`` service: every
  catalog is a database, every namespace a schema, Iceberg tables carry the
  columns of their current schema and Generic Tables registered through
  数据接入 carry the columns of their origin table plus a lineage edge back
  to it;
* semantic models stored through the Lattice model store or the native
  Polaris semantic-model API become ``semanticModel`` assets with a lineage
  edge from every dataset's source table.

Descriptions typed by people are never overwritten by a crawl: a source
comment only fills an empty description, and column display names and
descriptions survive a schema refresh. Every run is recorded in
``ingestion_run`` with counts and the errors it swallowed, and the scheduler
thread repeats runs at the interval configured per service.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from .connectors import ConnectorError, mask_config
from .datasources import DataSourceError, DataSourceRegistry
from .metadata_entities import (
    LATTICE_CONNECTOR_TYPES,
    POLARIS_SERVICE_TYPE,
    SERVICE_CATEGORIES,
    SERVICE_TYPES,
    category_of_service,
    build_fqn,
    child_fqn,
    data_length,
    normalize_data_type,
    type_label,
)
from .metadata_lineage import LineageService
from .metadata_service import SECRET_FIELD, MetadataError, MetadataService, summary_of
from .metadata_store import MetadataStoreError, new_id, now_ms

BOT_USER = "ingestion-bot"
POLARIS_SERVICE = "polaris"
POLARIS_DATASOURCE = "iceberg-local"
SEMANTIC_STORE_CATALOG = "lattice"
SEMANTIC_STORE_NAMESPACE = ["demo"]
MAX_SCHEMAS = 200
MAX_TABLES = 5000
MAX_ERRORS = 100
RUN_KEEP = 500
SCHEDULER_INTERVAL = 30.0
DATASOURCE_SYNC_MINUTES = 10
#: Engine schemas that hold system catalogs, never user data.
SYSTEM_SCHEMAS: dict[str, set[str]] = {
    # lattice_metadata / lattice_quality are the platform's own control schemas.
    "postgresql": {"information_schema", "pg_catalog", "pg_toast", "lattice_metadata", "lattice_quality"},
    "mysql": {"information_schema", "performance_schema", "mysql", "sys"},
    "starrocks": {"information_schema", "_statistics_", "sys"},
    "doris": {"information_schema", "__internal_schema", "mysql"},
    "clickhouse": {"system", "information_schema"},
    "duckdb": {"information_schema", "pg_catalog"},
    "oracle": {
        "sys", "system", "outln", "xdb", "ctxsys", "mdsys", "dbsnmp", "appqossys", "audsys", "ojvmsys", "wmsys",
        "ordsys", "orddata", "gsmadmin_internal", "dvsys", "lbacsys", "olapsys", "dbsfwuser", "remote_scheduler_agent",
        "sys$umf", "ggsys", "anonymous", "xs$null",
    },
    "mssql": {"sys", "information_schema", "guest"},
    "mongodb": {"admin", "local", "config"},
}
#: Engines whose ``schemas()`` are databases; OpenMetadata files them under "default".
DEFAULT_DATABASE_TYPES = {"mysql", "starrocks", "doris", "clickhouse", "hive", "paimon", "mongodb", "elasticsearch", "kafka"}
#: Settings remembering which data sources were already given a service.
KNOWN_DATASOURCES = "ingestion.known_datasources"
POLARIS_KNOWN = "ingestion.polaris_service"
DEFAULT_INTERVAL_MINUTES = 1440


class IngestionError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class MetadataIngestor:
    """Crawl services into the catalog and record every run."""

    def __init__(
        self, service: MetadataService, lineage: LineageService, registry: DataSourceRegistry | None, *,
        polaris: Any = None, models: Any = None, semantic: Any = None,
    ):
        self.service = service
        self.store = service.store
        self.lineage = lineage
        self.registry = registry
        self.polaris = polaris
        self.models = models
        self.semantic = semantic
        self._running: set[str] = set()
        self._running_lock = threading.Lock()
        self.last_error = ""

    # =====================================================================================
    # services
    # =====================================================================================
    def ensure_bot(self) -> None:
        if self.store.get_entity("user", fqn=build_fqn(BOT_USER)) is None:
            try:
                self.service.create(
                    "user",
                    {"name": BOT_USER, "display_name": "元数据拾取", "email": f"{BOT_USER}@localhost", "is_bot": True},
                    user=BOT_USER, silent=True,
                )
            except MetadataError:
                pass

    def sync_datasources(self, *, user: str | None = None, force: bool = False) -> dict[str, Any]:
        """Give every registered data source a database service, once.

        A data source is bound to a service the first time it is seen; later
        passes only refresh that service. A service someone deleted stays
        deleted, and is not created again, unless ``force`` brings every data
        source back — the 同步数据源 button does that.
        """
        if self.registry is None:
            return {"created": 0, "updated": 0, "restored": 0, "services": 0, "skipped": []}
        self.ensure_bot()
        user = user or BOT_USER
        created = updated = restored = 0
        skipped: list[str] = []
        listing = self.registry.list()
        statuses = {item["id"]: item for item in listing["items"]}
        known = {str(value) for value in self.store.get_setting(KNOWN_DATASOURCES, []) or []}
        bound: dict[str, dict[str, Any]] = {}
        for existing in self.store.all_entities("databaseService", deleted=None, limit=2000):
            key = str(existing["json"].get("datasource_id") or "")
            if not key or existing["service_type"] == POLARIS_SERVICE_TYPE:
                continue
            if key not in bound or (bound[key]["deleted"] and not existing["deleted"]):
                bound[key] = existing
        for item in listing["items"]:
            if item["id"] == POLARIS_DATASOURCE and self.polaris is not None:
                # The Polaris service covers this catalog with richer metadata.
                skipped.append(item["id"])
                continue
            service_type = LATTICE_CONNECTOR_TYPES.get(item["type"], "CustomDatabase")
            document = {
                "display_name": item["name"],
                "service_type": service_type,
                "datasource_id": item["id"],
                "connection": {
                    "type": item["type"],
                    "type_label": item["type_label"],
                    "summary": item.get("summary") or "",
                    "config": item.get("config") or {},
                    "builtin": bool(item.get("builtin")),
                },
            }
            row = bound.get(item["id"])
            if row is None:
                if item["id"] in known and not force:
                    skipped.append(item["id"])
                    continue
                name = item["id"]
                if self.store.get_entity("databaseService", fqn=build_fqn(name)) is not None:
                    name = f"{item['id']}-{new_id()[:6]}"
                self.service.create(
                    "databaseService",
                    {"name": name, "description": item.get("description") or "", **document, "ingestion": _default_ingestion(automated=True)},
                    user=user,
                )
                known.add(item["id"])
                created += 1
                continue
            known.add(item["id"])
            if row["deleted"]:
                if not force:
                    skipped.append(item["id"])
                    continue
                self.service.restore("databaseService", row["id"], user=user)
                row = self.store.get_entity("databaseService", id=row["id"]) or row
                restored += 1
            merged = dict(document)
            if row["display_name"]:
                merged.pop("display_name")
            if not row["description"] and item.get("description"):
                merged["description"] = item["description"]
            latest = self.service.touch(row, merged, user=user)
            if latest["version"] != row["version"]:
                updated += 1
        self.store.set_setting(KNOWN_DATASOURCES, sorted(known))
        if self.polaris is not None:
            self._ensure_polaris_service(user, statuses, force=force)
        return {"created": created, "updated": updated, "restored": restored, "services": len(listing["items"]), "skipped": skipped}

    def _ensure_polaris_service(self, user: str, statuses: dict[str, Any], *, force: bool = False) -> dict[str, Any] | None:
        row = self.store.get_entity("databaseService", fqn=build_fqn(POLARIS_SERVICE))
        seen_before = bool(self.store.get_setting(POLARIS_KNOWN, False))
        if (row is None and seen_before and not force) or (row is not None and row["deleted"] and not force):
            return None
        version = ""
        try:
            version = str((self.polaris.status() or {}).get("version") or "") if hasattr(self.polaris, "status") else ""
        except Exception:  # noqa: BLE001 - a Polaris outage is reported by the crawl, not here
            version = ""
        document = {
            "display_name": "Apache Polaris（本地）",
            "service_type": POLARIS_SERVICE_TYPE,
            "datasource_id": POLARIS_DATASOURCE if POLARIS_DATASOURCE in statuses else "",
            "connection": {"type": "polaris", "version": version or "1.7.0"},
        }
        self.store.set_setting(POLARIS_KNOWN, True)
        if row is None:
            return self.service.create(
                "databaseService",
                {"name": POLARIS_SERVICE, "description": "本项目运行的 Apache Polaris 1.7.0：Catalog、命名空间、Iceberg 表、通用表与语义模型。", **document, "ingestion": _default_ingestion(automated=True)},
                user=user,
            )
        if row["deleted"]:
            self.service.restore("databaseService", row["id"], user=user)
            row = self.store.get_entity("databaseService", id=row["id"]) or row
        merged = {k: v for k, v in document.items() if k != "display_name" or not row["display_name"]}
        self.service.touch(row, merged, user=user)
        return self.service.decorate(self.store.get_entity("databaseService", id=row["id"]) or row)

    def create_service(self, payload: dict[str, Any], *, user: str | None = None) -> dict[str, Any]:
        """Register a service from the 添加新服务 wizard."""
        if not isinstance(payload, dict):
            raise IngestionError(400, "请求体必须是对象。")
        category = str(payload.get("category") or "")
        spec = SERVICE_CATEGORIES.get(category)
        if spec is None:
            raise IngestionError(400, "服务类别无效。")
        service_type = str(payload.get("service_type") or "")
        if service_type not in spec["connectors"]:
            raise IngestionError(400, f"{spec['label']}类别不支持连接器 {service_type}。")
        connection = payload.get("connection") or {}
        if not isinstance(connection, dict):
            raise IngestionError(400, "连接信息必须是对象。")
        for key, value in connection.items():
            if SECRET_FIELD.search(str(key)) and value not in (None, ""):
                raise IngestionError(400, f"连接信息不保存密码、令牌等机密字段（{key}）；需要自动拾取的数据库请绑定平台数据源。")
        document: dict[str, Any] = {
            "name": payload.get("name"),
            "display_name": payload.get("display_name") or "",
            "description": payload.get("description") or "",
            "service_type": service_type,
            "connection": {str(k)[:64]: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:2000]) for k, v in list(connection.items())[:50]},
        }
        if payload.get("owners") is not None:
            document["owners"] = payload.get("owners")
        datasource_id = str(payload.get("datasource_id") or "")
        automated = False
        if datasource_id:
            if category != "database" or self.registry is None:
                raise IngestionError(400, "只有数据库服务可以绑定平台数据源。")
            try:
                record = self.registry.record(datasource_id)
            except DataSourceError as error:
                raise IngestionError(error.status_code, str(error)) from error
            expected = LATTICE_CONNECTOR_TYPES.get(record["type"])
            if expected != service_type:
                raise IngestionError(400, f"数据源 {record['name']} 的类型是 {expected}，与所选连接器 {service_type} 不一致。")
            document["datasource_id"] = datasource_id
            document["connection"] = {**document["connection"], "type": record["type"], "summary": ""}
            automated = True
        minutes = payload.get("interval_minutes")
        ingestion = _default_ingestion(automated=automated)
        if minutes is not None:
            try:
                ingestion["interval_minutes"] = max(0, min(int(minutes), 10080))
            except (TypeError, ValueError) as error:
                raise IngestionError(400, "拾取间隔必须是整数分钟。") from error
        if payload.get("schedule_enabled") is not None:
            ingestion["enabled"] = bool(payload.get("schedule_enabled")) and automated
        document["ingestion"] = ingestion
        created = self.service.create(spec["entity_type"], document, user=user)
        if datasource_id:
            known = {str(value) for value in self.store.get_setting(KNOWN_DATASOURCES, []) or []}
            known.add(datasource_id)
            self.store.set_setting(KNOWN_DATASOURCES, sorted(known))
        return created

    def services(self) -> list[dict[str, Any]]:
        """Every service with its ingestion settings, last run and data-source status."""
        rows = self.store.all_entities(sorted(SERVICE_TYPES), deleted=False, limit=2000)
        latest = self.store.latest_runs()
        statuses: dict[str, dict[str, Any]] = {}
        if self.registry is not None:
            try:
                statuses = {item["id"]: item for item in self.registry.statuses()}
            except (DataSourceError, ConnectorError, OSError, ValueError):
                statuses = {}
        items = self.service.decorate_many(rows, full=False)
        for item, row in zip(items, rows):
            document = row["json"]
            ingestion = {**_default_ingestion(), **(document.get("ingestion") or {})}
            item["ingestion"] = ingestion
            item["last_run"] = latest.get(row["fqn"])
            item["running"] = row["fqn"] in self._running
            datasource_id = document.get("datasource_id") or ""
            item["datasource"] = statuses.get(datasource_id) if datasource_id else None
            item["automated"] = self.can_run(row)
            item["asset_count"] = self._asset_count(row)
        items.sort(key=lambda i: (i["entity_type"], i["name"]))
        return items

    def _asset_count(self, row: dict[str, Any]) -> int:
        category = category_of_service(row["entity_type"])
        children = SERVICE_CATEGORIES[category]["children"] if category else []
        total = 0
        for child in children:
            if child in {"database", "databaseSchema"}:
                continue
            total += self.store.list_entities(child, service_fqn=row["fqn"], deleted=False, page=1, size=1)["total"]
        return total

    def can_run(self, row: dict[str, Any]) -> bool:
        if row["entity_type"] != "databaseService":
            return False
        if row["service_type"] == POLARIS_SERVICE_TYPE:
            return self.polaris is not None
        return bool(row["json"].get("datasource_id")) and self.registry is not None

    def is_running(self, service_fqn: str) -> bool:
        with self._running_lock:
            return service_fqn in self._running

    def check_runnable(self, row: dict[str, Any]) -> None:
        """Raise the reason a service cannot be crawled now, if there is one."""
        if row["entity_type"] not in SERVICE_TYPES:
            raise IngestionError(400, f"{type_label(row['entity_type'])}不是服务，不能拾取。")
        if row["deleted"]:
            raise IngestionError(400, "已删除的服务不能拾取，请先恢复。")
        if not self.can_run(row):
            raise IngestionError(400, f"连接器 {row['service_type']} 暂不支持自动拾取；可以通过导入 OpenMetadata JSON、MCP 工具或页面登记资产。")
        datasource_id = str(row["json"].get("datasource_id") or "")
        if row["service_type"] != POLARIS_SERVICE_TYPE and self.registry is not None:
            try:
                self.registry.record(datasource_id)
            except DataSourceError as error:
                raise IngestionError(400, f"该服务关联的数据源 {datasource_id} 已不存在，请重新绑定或删除该服务。") from error

    def schedule(self, service_ref: str, *, interval_minutes: Any, enabled: Any, options: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.service.row(None, service_ref)
        if row["entity_type"] not in SERVICE_TYPES:
            raise IngestionError(400, "只能为服务配置拾取计划。")
        try:
            minutes = int(interval_minutes)
        except (TypeError, ValueError) as error:
            raise IngestionError(400, "拾取间隔必须是整数分钟。") from error
        if minutes < 0 or minutes > 7 * 24 * 60:
            raise IngestionError(400, "拾取间隔必须在 0–10080 分钟之间（0 表示只手动执行）。")
        ingestion = {**_default_ingestion(), **(row["json"].get("ingestion") or {})}
        ingestion["interval_minutes"] = minutes
        ingestion["enabled"] = bool(enabled)
        if options is not None:
            ingestion["options"] = _clean_options(options)
        ingestion["next_run_at"] = (now_ms() + minutes * 60000) if (minutes and bool(enabled)) else None
        self._write_ingestion(row, ingestion)
        return {**summary_of(row), "ingestion": ingestion}

    def _write_ingestion(self, row: dict[str, Any], ingestion: dict[str, Any]) -> None:
        latest = self.store.get_entity(row["entity_type"], id=row["id"]) or row
        self.store.update_entity({**latest, "json": {**latest["json"], "ingestion": ingestion}})

    # =====================================================================================
    # runs
    # =====================================================================================
    def run(self, service_ref: str, *, trigger: str = "manual", user: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.service.row(None, service_ref)
        self.check_runnable(row)
        with self._running_lock:
            if row["fqn"] in self._running:
                raise IngestionError(409, "该服务正在拾取中，请稍后再试。")
            self._running.add(row["fqn"])
        try:
            self.ensure_bot()
            user = user or BOT_USER
            stored = (row["json"].get("ingestion") or {}).get("options")
            merged_options = {**_default_ingestion()["options"], **(stored if isinstance(stored, dict) else {}), **_clean_options(options or {})}
            run = {
                "id": new_id(),
                "service_fqn": row["fqn"],
                "run_type": "metadata",
                "status": "running",
                "trigger": trigger,
                "started_at": now_ms(),
                "finished_at": None,
                "summary": {},
                "message": "",
            }
            self.store.insert_run(run)
        except BaseException:
            with self._running_lock:
                self._running.discard(row["fqn"])
            raise
        summary = _empty_summary()
        try:
            if row["service_type"] == POLARIS_SERVICE_TYPE:
                self._crawl_polaris(row, summary, user, merged_options)
            else:
                self._crawl_datasource(row, summary, user, merged_options)
            self.lineage.invalidate()
            if merged_options.get("view_lineage", True):
                try:
                    views = self.lineage.sync_views(row["fqn"], user=user)
                    summary["view_edges"] = views["edges"]
                except (MetadataError, MetadataStoreError) as error:
                    _note(summary, f"视图血缘：{error}")
            self.lineage.invalidate()
            run["status"] = "partial" if summary["errors"] else "success"
            run["message"] = _run_message(summary)
        except (DataSourceError, ConnectorError, MetadataError, MetadataStoreError, IngestionError, OSError, ValueError, KeyError) as error:
            run["status"] = "failed"
            run["message"] = f"拾取失败：{str(error)[:800]}"
            _note(summary, str(error))
            self.last_error = run["message"]
        except Exception as error:  # noqa: BLE001 - a run must always be closed
            run["status"] = "failed"
            run["message"] = f"拾取异常：{type(error).__name__}: {str(error)[:600]}"
            _note(summary, run["message"])
            self.last_error = run["message"]
        finally:
            with self._running_lock:
                self._running.discard(row["fqn"])
            run["finished_at"] = now_ms()
            run["summary"] = summary
            try:
                self.store.update_run(run)
                ingestion = {**_default_ingestion(), **((self.store.get_entity(row["entity_type"], id=row["id"]) or row)["json"].get("ingestion") or {})}
                ingestion["last_run_at"] = run["finished_at"]
                ingestion["last_status"] = run["status"]
                ingestion["last_run_id"] = run["id"]
                minutes = int(ingestion.get("interval_minutes") or 0)
                ingestion["next_run_at"] = run["finished_at"] + minutes * 60000 if minutes and ingestion.get("enabled") else None
                self._write_ingestion(row, ingestion)
                self.store.prune_runs(RUN_KEEP)
            except (MetadataStoreError, ValueError):
                pass
        try:
            return self.store.get_run(run["id"]) or run
        except MetadataStoreError:
            return run

    def run_all(self, *, trigger: str = "manual", user: str | None = None) -> list[dict[str, Any]]:
        results = []
        for row in self.store.all_entities("databaseService", deleted=False, limit=2000):
            if not self.can_run(row):
                continue
            try:
                results.append(self.run(row["fqn"], trigger=trigger, user=user))
            except IngestionError as error:
                results.append({"service_fqn": row["fqn"], "status": "skipped", "message": str(error)})
        return results

    def due_services(self, now: int | None = None) -> list[dict[str, Any]]:
        moment = now or now_ms()
        due = []
        for row in self.store.all_entities("databaseService", deleted=False, limit=2000):
            ingestion = row["json"].get("ingestion") or {}
            minutes = int(ingestion.get("interval_minutes") or 0)
            if not ingestion.get("enabled") or minutes <= 0 or not self.can_run(row):
                continue
            last = int(ingestion.get("last_run_at") or 0)
            if last + minutes * 60000 <= moment and row["fqn"] not in self._running:
                due.append(row)
        return due

    def runs(self, **filters: Any) -> dict[str, Any]:
        listing = self.store.list_runs(**filters)
        services = {r["fqn"]: r for r in self.store.get_entities_by_fqn(sorted({i["service_fqn"] for i in listing["items"]}))}
        for item in listing["items"]:
            service = services.get(item["service_fqn"])
            item["service"] = summary_of(service) if service else None
            item["elapsed_ms"] = (item["finished_at"] - item["started_at"]) if item.get("finished_at") else None
        return listing

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise IngestionError(404, "拾取记录不存在。")
        service = self.store.get_entity(None, fqn=run["service_fqn"])
        run["service"] = summary_of(service) if service else None
        run["elapsed_ms"] = (run["finished_at"] - run["started_at"]) if run.get("finished_at") else None
        return run

    def status(self) -> dict[str, Any]:
        return {"running": sorted(self._running), "last_error": self.last_error}

    # =====================================================================================
    # crawlers
    # =====================================================================================
    def _crawl_datasource(self, service_row: dict[str, Any], summary: dict[str, Any], user: str, options: dict[str, Any]) -> None:
        assert self.registry is not None
        datasource_id = str(service_row["json"].get("datasource_id") or "")
        record = self.registry.record(datasource_id)
        connector = self.registry.connector(datasource_id)
        database_name = _database_name(record)
        database = self._ensure("database", service_row, database_name, {"source_name": database_name}, summary, user)
        excluded = {s.lower() for s in SYSTEM_SCHEMAS.get(record["type"], set())} | {s.lower() for s in options.get("exclude_schemas", [])}
        included = {s.lower() for s in options.get("schemas", [])}
        try:
            schemas = connector.schemas()
        except ConnectorError as error:
            raise IngestionError(502, f"读取 Schema 列表失败：{error}") from error
        seen_schemas: set[str] = set()
        for schema in schemas[:MAX_SCHEMAS]:
            name = str(schema.get("name") or "")
            if not name or name.lower() in excluded or (included and name.lower() not in included):
                continue
            seen_schemas.add(name)
            schema_row = self._ensure("databaseSchema", database, name, {"source_name": name}, summary, user)
            self._crawl_tables(connector, record, schema_row, name, summary, user, options)
        if options.get("mark_deleted", True):
            self._mark_missing("databaseSchema", database, seen_schemas, summary, user)

    def _crawl_tables(self, connector: Any, record: dict[str, Any], schema_row: dict[str, Any], schema: str, summary: dict[str, Any], user: str, options: dict[str, Any]) -> None:
        try:
            tables = connector.tables(schema)
        except ConnectorError as error:
            _note(summary, f"{schema}: 读取表列表失败：{error}")
            return
        pattern = _compile(options.get("table_pattern"))
        seen: set[str] = set()
        for table in tables[:MAX_TABLES]:
            name = str(table.get("name") or "")
            if not name or (pattern and not pattern.search(name)):
                continue
            # Listed means it exists: a failed detail read keeps the catalog entry as it is.
            seen.add(name)
            try:
                detail = connector.table(schema, name)
            except ConnectorError as error:
                _note(summary, f"{schema}.{name}: 读取表结构失败，保留目录中的现有记录：{error}")
                continue
            kind = str(table.get("kind") or detail.get("kind") or "table").lower()
            properties = detail.get("properties") if isinstance(detail.get("properties"), dict) else {}
            document: dict[str, Any] = {
                "table_type": _table_type(kind, record["type"]),
                "columns": _columns_of(detail.get("columns") or []),
                "row_count": detail.get("rows") if isinstance(detail.get("rows"), int) else table.get("rows") if isinstance(table.get("rows"), int) else None,
                "datasource_id": record["id"],
                "source_schema": schema,
                "source_table": name,
            }
            comment = str(table.get("comment") or detail.get("comment") or "").strip()
            if comment:
                document["description"] = comment
            definition = properties.get("view_definition") or properties.get("definition") or ""
            if not definition and "view" in kind:
                try:
                    definition = connector.view_definition(schema, name) or ""
                except (ConnectorError, AttributeError) as error:
                    _note(summary, f"{schema}.{name}: 读取视图定义失败：{error}")
                    definition = ""
            if isinstance(definition, str) and definition.strip():
                document["view_definition"] = definition.strip()[:50000]
            if properties.get("location"):
                document["location"] = str(properties["location"])[:2000]
            if properties.get("format") or properties.get("file_format"):
                document["file_format"] = str(properties.get("format") or properties.get("file_format"))[:64]
            self._ensure("table", schema_row, name, document, summary, user)
            summary["columns"] += len(document["columns"])
        if options.get("mark_deleted", True):
            self._mark_missing("table", schema_row, seen, summary, user)

    def _crawl_polaris(self, service_row: dict[str, Any], summary: dict[str, Any], user: str, options: dict[str, Any]) -> None:
        catalogs = self._polaris("management", "listCatalogs", {}, "读取 Catalog 列表")
        names = [c.get("name") for c in (catalogs.get("catalogs") if isinstance(catalogs, dict) else []) if isinstance(c, dict) and c.get("name")]
        seen_catalogs: set[str] = set()
        for catalog in names[:50]:
            seen_catalogs.add(catalog)
            database = self._ensure("database", service_row, catalog, {"source_name": catalog}, summary, user)
            namespaces = self._polaris("catalog", "listNamespaces", {"prefix": catalog}, "读取命名空间")
            listed = [n for n in (namespaces.get("namespaces") if isinstance(namespaces, dict) else []) if isinstance(n, list) and n]
            seen_namespaces: set[str] = set()
            for namespace in listed[:MAX_SCHEMAS]:
                schema_name = ".".join(str(part) for part in namespace)
                seen_namespaces.add(schema_name)
                schema_row = self._ensure("databaseSchema", database, schema_name, {"source_name": schema_name}, summary, user)
                path = {"prefix": catalog, "namespace": "\x1f".join(str(part) for part in namespace)}
                seen_tables: set[str] = set()
                listed = self._polaris_iceberg_tables(catalog, namespace, path, schema_row, seen_tables, summary, user)
                listed = self._polaris_generic_tables(catalog, namespace, path, schema_row, seen_tables, summary, user) and listed
                # A namespace whose listing failed is not evidence that its tables are gone.
                if options.get("mark_deleted", True) and listed:
                    self._mark_missing("table", schema_row, seen_tables, summary, user)
            if options.get("mark_deleted", True):
                self._mark_missing("databaseSchema", database, seen_namespaces, summary, user)
        if options.get("mark_deleted", True):
            self._mark_missing("database", service_row, seen_catalogs, summary, user)
        if options.get("semantic_models", True):
            self._crawl_semantic_models(summary, user)

    def _polaris_iceberg_tables(self, catalog: str, namespace: list[Any], path: dict[str, str], schema_row: dict[str, Any], seen: set[str], summary: dict[str, Any], user: str) -> bool:
        """Crawl a namespace's Iceberg tables; False when the listing itself failed."""
        try:
            tables = self._polaris("catalog", "listTables", path, "读取 Iceberg 表列表")
        except IngestionError as error:
            _note(summary, str(error))
            return False
        for identifier in (tables.get("identifiers") if isinstance(tables, dict) else []) or []:
            name = identifier.get("name") if isinstance(identifier, dict) else None
            if not name:
                continue
            seen.add(name)
            document: dict[str, Any] = {"table_type": "Iceberg", "columns": [], "datasource_id": POLARIS_DATASOURCE, "source_schema": ".".join(str(p) for p in namespace), "source_table": name, "polaris": {"catalog": catalog, "namespace": list(namespace), "kind": "iceberg"}}
            try:
                loaded = self._polaris("catalog", "loadTable", {**path, "table": name}, f"读取 Iceberg 表 {name}")
                metadata = loaded.get("metadata") if isinstance(loaded, dict) else {}
                document["columns"] = _iceberg_columns(metadata if isinstance(metadata, dict) else {})
                if isinstance(metadata, dict):
                    if metadata.get("location"):
                        document["location"] = str(metadata["location"])[:2000]
                    if isinstance(metadata.get("properties"), dict) and metadata["properties"].get("comment"):
                        document["description"] = str(metadata["properties"]["comment"])
            except IngestionError as error:
                _note(summary, str(error))
            self._ensure("table", schema_row, name, document, summary, user)
            summary["columns"] += len(document["columns"])
        return True

    def _polaris_generic_tables(self, catalog: str, namespace: list[Any], path: dict[str, str], schema_row: dict[str, Any], seen: set[str], summary: dict[str, Any], user: str) -> bool:
        """Crawl a namespace's Generic Tables; False when the listing itself failed."""
        try:
            generic = self.polaris.request("catalog", "listGenericTables", path_params=path)
        except Exception as error:  # noqa: BLE001 - Polaris errors are reported per namespace
            _note(summary, f"读取通用表列表失败：{str(error)[:300]}")
            return False
        if generic.get("status") in {404, 501}:
            return True  # this Polaris has no generic tables here
        if generic.get("status") != 200 or not isinstance(generic.get("body"), dict):
            _note(summary, f"读取通用表列表失败：Polaris 返回 HTTP {generic.get('status')}。")
            return False
        for identifier in generic["body"].get("identifiers", [])[:MAX_TABLES]:
            name = identifier.get("name") if isinstance(identifier, dict) else None
            if not name:
                continue
            seen.add(name)
            try:
                loaded = self.polaris.request("catalog", "loadGenericTable", path_params={**path, "generic-table": name})
            except Exception as error:  # noqa: BLE001
                _note(summary, f"读取通用表 {name} 失败，保留目录中的现有记录：{str(error)[:200]}")
                continue
            table = loaded.get("body", {}).get("table") if loaded.get("status") == 200 else None
            if not isinstance(table, dict):
                _note(summary, f"读取通用表 {name} 失败（HTTP {loaded.get('status')}），保留目录中的现有记录。")
                continue
            properties = table.get("properties") or {}
            fmt = str(table.get("format") or "")
            if fmt in {"lattice", "lattice-semantic-model"}:
                seen.discard(name)
                continue  # semantic-model records are ingested as semantic models
            try:
                raw_columns = json.loads(properties.get("lattice.columns") or "[]")
            except ValueError:
                raw_columns = []
            document: dict[str, Any] = {
                "table_type": "External",
                "columns": _columns_of([c for c in raw_columns if isinstance(c, dict)]),
                "datasource_id": POLARIS_DATASOURCE,
                "source_schema": ".".join(str(p) for p in namespace),
                "source_table": name,
                "file_format": fmt[:64],
                "polaris": {"catalog": catalog, "namespace": list(namespace), "kind": "generic", "origin_datasource": properties.get("lattice.source-datasource", ""), "origin_schema": properties.get("lattice.source-schema", ""), "origin_table": properties.get("lattice.source-table", "")},
            }
            if table.get("doc"):
                document["description"] = str(table["doc"])
            if table.get("base-location"):
                document["location"] = str(table["base-location"])[:2000]
            if properties.get("lattice.row-count", "").isdigit():
                document["row_count"] = int(properties["lattice.row-count"])
            row = self._ensure("table", schema_row, name, document, summary, user)
            origin = self.lineage.find_table(str(properties.get("lattice.source-datasource") or ""), str(properties.get("lattice.source-schema") or "") or None, str(properties.get("lattice.source-table") or ""))
            if origin is not None and origin["id"] != row["id"]:
                try:
                    self.store.upsert_edge({"from_fqn": origin["fqn"], "from_type": "table", "to_fqn": row["fqn"], "to_type": "table", "source": "polaris", "description": "通过 Lattice 数据接入注册为 Polaris 通用表", "columns": _same_columns(origin, row)})
                    summary["lineage_edges"] += 1
                except MetadataStoreError as error:
                    _note(summary, f"{row['fqn']}: 写入血缘失败：{error}")
        return True

    def _polaris(self, spec: str, operation: str, path: dict[str, Any], action: str, **kwargs: Any) -> Any:
        try:
            response = self.polaris.request(spec, operation, path_params=path, **kwargs)
        except Exception as error:  # noqa: BLE001 - httpx/Polaris errors become one message
            raise IngestionError(502, f"{action}失败：{str(error)[:300]}") from error
        status = response.get("status")
        if status in {200, 201}:
            return response.get("body") or {}
        raise IngestionError(502 if status not in {401, 403, 404} else status, f"{action}失败：Polaris 返回 HTTP {status}。")

    # ----- semantic models ---------------------------------------------------------------------
    def sync_semantic_models(self, *, user: str | None = None) -> dict[str, Any]:
        summary = _empty_summary()
        self.ensure_bot()
        self._crawl_semantic_models(summary, user or BOT_USER)
        self.lineage.invalidate()
        return summary

    def _crawl_semantic_models(self, summary: dict[str, Any], user: str) -> None:
        self.lineage.invalidate()
        found: list[dict[str, Any]] = []
        if self.models is not None:
            try:
                listing = self.models.list()
                for item in listing.get("items", []):
                    model_id = item.get("id") or item.get("name")
                    if not model_id:
                        continue
                    try:
                        loaded = self.models.get(str(model_id))
                    except Exception as error:  # noqa: BLE001
                        _note(summary, f"语义模型 {model_id}: {str(error)[:200]}")
                        continue
                    document = _semantic_document(loaded)
                    if document is None:
                        continue
                    found.append({"name": str(item.get("name") or model_id), "storage": "lattice-model-store", "model_id": str(model_id), "catalog": listing.get("catalog", ""), "namespace": listing.get("namespace", ""), "document": document, "yaml": str(loaded.get("yaml") or "")})
            except Exception as error:  # noqa: BLE001
                _note(summary, f"读取 Lattice 模型存储失败：{str(error)[:300]}")
        if self.semantic is not None:
            try:
                listing = self.semantic.catalog_listing(SEMANTIC_STORE_CATALOG, SEMANTIC_STORE_NAMESPACE)
                for item in listing.get("items", []):
                    name = item.get("name")
                    if not name:
                        continue
                    try:
                        response = self.semantic.load(SEMANTIC_STORE_CATALOG, SEMANTIC_STORE_NAMESPACE, str(name))
                    except Exception as error:  # noqa: BLE001
                        _note(summary, f"原生语义模型 {name}: {str(error)[:200]}")
                        continue
                    body = response.get("body") if isinstance(response, dict) else None
                    if not isinstance(body, dict) or response.get("status") != 200:
                        continue
                    document = _semantic_document(body)
                    if document is None:
                        continue
                    found.append({"name": str(name), "storage": "polaris-native", "model_id": str(name), "catalog": SEMANTIC_STORE_CATALOG, "namespace": ".".join(SEMANTIC_STORE_NAMESPACE), "entity_version": str(body.get("entity-version") or ""), "document": document, "yaml": ""})
            except Exception as error:  # noqa: BLE001
                _note(summary, f"读取原生语义模型失败：{str(error)[:300]}")
        seen: set[str] = set()
        for model in found:
            document = model["document"]
            payload = {
                "storage": {"kind": model["storage"], "catalog": model["catalog"], "namespace": model["namespace"], "model_id": model["model_id"], "entity_version": model.get("entity_version", "")},
                "catalog": model["catalog"],
                "namespace": model["namespace"],
                "model_id": model["model_id"],
                "entity_version": model.get("entity_version", ""),
                "datasets": [_dataset_summary(d) for d in document.get("datasets") or [] if isinstance(d, dict)][:500],
                "metrics": [_metric_summary(m) for m in document.get("metrics") or [] if isinstance(m, dict)][:500],
                "yaml": model["yaml"][:200000],
            }
            if document.get("description"):
                payload["description"] = str(document["description"])[:20000]
            name = str(document.get("name") or model["name"])
            seen.add(name)
            row = self._ensure("semanticModel", None, name, payload, summary, user)
            for dataset in payload["datasets"]:
                source = dataset.get("source") or ""
                parts = [p for p in re.split(r"\.", source) if p]
                if not parts:
                    continue
                table = self.lineage.find_table_anywhere(parts[-2] if len(parts) > 1 else None, parts[-1])
                if table is None:
                    continue
                try:
                    self.store.upsert_edge({"from_fqn": table["fqn"], "from_type": "table", "to_fqn": row["fqn"], "to_type": "semanticModel", "source": "semantic", "description": f"语义模型数据集 {dataset.get('name')} 的来源表"})
                    summary["lineage_edges"] += 1
                except MetadataStoreError as error:
                    _note(summary, f"{row['fqn']}: 写入血缘失败：{error}")
        summary["semantic_models"] = len(found)

    # =====================================================================================
    # entity upkeep
    # =====================================================================================
    def _ensure(self, entity_type: str, parent: dict[str, Any] | None, name: str, document: dict[str, Any], summary: dict[str, Any], user: str) -> dict[str, Any]:
        fqn = child_fqn(parent["fqn"], name) if parent else build_fqn(name)
        row = self.store.get_entity(entity_type, fqn=fqn)
        if row is None:
            payload = {"name": name, **document}
            if parent is not None:
                payload["parent_fqn"] = parent["fqn"]
            created = self.service.create(entity_type, payload, user=user)
            summary["created"] += 1
            summary[_counter(entity_type)] += 1
            return self.store.get_entity(entity_type, id=created["id"]) or created
        if row["deleted"]:
            self.service.restore(entity_type, row["id"], user=user)
            row = self.store.get_entity(entity_type, id=row["id"]) or row
            summary["restored"] += 1
        merged = dict(document)
        if row["description"]:
            merged.pop("description", None)
        if "columns" in merged:
            merged["columns"] = _merge_columns(row["json"].get("columns") or [], merged["columns"])
        latest = self.service.touch(row, merged, user=user)
        if latest["version"] != row["version"]:
            summary["updated"] += 1
        else:
            summary["unchanged"] += 1
        summary[_counter(entity_type)] += 1
        return latest

    def _mark_missing(self, entity_type: str, parent: dict[str, Any], seen: set[str], summary: dict[str, Any], user: str) -> None:
        listing = self.store.list_entities(entity_type, parent_fqn=parent["fqn"], deleted=False, page=1, size=200)
        rows = list(listing["items"])
        page = 2
        while len(rows) < listing["total"] and page <= 200:
            rows.extend(self.store.list_entities(entity_type, parent_fqn=parent["fqn"], deleted=False, page=page, size=200)["items"])
            page += 1
        for row in rows:
            if row["name"] in seen:
                continue
            try:
                self.service.delete(entity_type, row["id"], user=user)
                summary["deleted"] += 1
            except (MetadataError, MetadataStoreError) as error:
                _note(summary, f"{row['fqn']}: 标记删除失败：{error}")


class MetadataScheduler:
    """Daemon thread: due ingestion runs, the daily insight snapshot, data-source sync."""

    def __init__(self, ingestor: MetadataIngestor, insights: Any = None, *, interval: float = SCHEDULER_INTERVAL, sync_datasources: bool = True):
        self.ingestor = ingestor
        self.insights = insights
        self.interval = max(0.1, float(interval))
        self.sync_datasources = sync_datasources
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sync = 0
        self.last_error = ""
        self.ticks = 0

    def start(self) -> None:
        if self.running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lattice-metadata-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {"running": self.running(), "ticks": self.ticks, "last_error": self.last_error, "interval_seconds": self.interval}

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def tick(self, now: int | None = None) -> list[str]:
        """One pass; never raises."""
        self.ticks += 1
        fired: list[str] = []
        moment = now or now_ms()
        try:
            if self.sync_datasources and moment - self._last_sync >= DATASOURCE_SYNC_MINUTES * 60000:
                self._last_sync = moment
                self.ingestor.sync_datasources()
            for row in self.ingestor.due_services(moment):
                if self._stop.is_set():
                    break
                try:
                    self.ingestor.run(row["fqn"], trigger="scheduled")
                    fired.append(row["fqn"])
                except IngestionError as error:
                    self.last_error = str(error)
            if self.insights is not None:
                self.insights.ensure_snapshot()
        except Exception as error:  # noqa: BLE001 - the thread must outlive any error
            self.last_error = f"{type(error).__name__}: {str(error)[:300]}"
        return fired


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _default_ingestion(*, automated: bool = False) -> dict[str, Any]:
    """Crawlable services refresh daily by default, as OpenMetadata's ingestion pipelines do."""
    return {
        "enabled": automated,
        "interval_minutes": DEFAULT_INTERVAL_MINUTES if automated else 0,
        "last_run_at": None,
        "last_status": "",
        "last_run_id": "",
        "next_run_at": None,
        "options": {"mark_deleted": True, "view_lineage": True, "semantic_models": True, "schemas": [], "exclude_schemas": [], "table_pattern": ""},
    }


def _clean_options(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise IngestionError(400, "拾取选项必须是对象。")
    clean: dict[str, Any] = {}
    for key in ("mark_deleted", "view_lineage", "semantic_models"):
        if key in options:
            clean[key] = bool(options[key])
    for key in ("schemas", "exclude_schemas"):
        if key in options:
            values = options[key]
            if isinstance(values, str):
                values = [v.strip() for v in values.split(",")]
            if not isinstance(values, list):
                raise IngestionError(400, f"{key} 必须是数组。")
            clean[key] = [str(v)[:256] for v in values if str(v).strip()][:200]
    if "table_pattern" in options:
        pattern = str(options.get("table_pattern") or "")[:200]
        _compile(pattern)
        clean["table_pattern"] = pattern
    return clean


def _compile(pattern: Any) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(str(pattern), re.IGNORECASE)
    except re.error as error:
        raise IngestionError(400, f"表名过滤正则无效：{error}") from error


def _empty_summary() -> dict[str, Any]:
    return {
        "databases": 0, "schemas": 0, "tables": 0, "semantic_models": 0, "columns": 0,
        "created": 0, "updated": 0, "unchanged": 0, "restored": 0, "deleted": 0,
        "lineage_edges": 0, "view_edges": 0, "errors": [],
    }


def _counter(entity_type: str) -> str:
    return {"database": "databases", "databaseSchema": "schemas", "table": "tables", "semanticModel": "semantic_models"}.get(entity_type, "tables")


def _note(summary: dict[str, Any], message: str) -> None:
    if len(summary["errors"]) < MAX_ERRORS:
        summary["errors"].append(message[:500])


def _run_message(summary: dict[str, Any]) -> str:
    parts = [f"{summary['tables']} 张表", f"{summary['columns']} 个字段", f"新增 {summary['created']}", f"更新 {summary['updated']}"]
    if summary["deleted"]:
        parts.append(f"标记删除 {summary['deleted']}")
    if summary["restored"]:
        parts.append(f"恢复 {summary['restored']}")
    if summary["lineage_edges"] or summary["view_edges"]:
        parts.append(f"血缘 {summary['lineage_edges'] + summary['view_edges']} 条")
    if summary["errors"]:
        parts.append(f"{len(summary['errors'])} 处错误")
    return "，".join(parts)


def _database_name(record: dict[str, Any]) -> str:
    config = record.get("config") or {}
    kind = record.get("type")
    if kind == "postgresql":
        return str(config.get("database") or "postgres")
    if kind == "duckdb":
        path = str(config.get("path") or "")
        return Path(path).stem if path and path != ":memory:" else "memory"
    if kind == "iceberg":
        return str(config.get("warehouse") or config.get("catalog_type") or "iceberg")
    if kind in DEFAULT_DATABASE_TYPES:
        return "default"
    return str(config.get("database") or "default")


def _table_type(kind: str, source_type: str) -> str:
    if "materialized" in kind:
        return "MaterializedView"
    if "view" in kind:
        return "View"
    if "external" in kind or "foreign" in kind:
        return "External"
    if source_type == "iceberg":
        return "Iceberg"
    if source_type == "paimon":
        return "Paimon"
    if "partition" in kind:
        return "Partitioned"
    return "Regular"


def _columns_of(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:2000]):
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        raw_type = str(item.get("type") or item.get("data_type_display") or item.get("data_type") or "")
        if item.get("primary_key"):
            constraint = "PRIMARY_KEY"
        elif "nullable" in item:
            constraint = "NULL" if item.get("nullable", True) else "NOT_NULL"
        else:
            constraint = str(item.get("constraint") or "")
        columns.append(
            {
                "name": name,
                "display_name": "",
                "description": str(item.get("comment") or item.get("description") or "").strip()[:20000],
                "data_type_display": raw_type[:256],
                "data_type": normalize_data_type(raw_type),
                "data_length": data_length(raw_type),
                "constraint": constraint,
                "ordinal_position": index + 1,
            }
        )
    return columns


def _merge_columns(existing: list[dict[str, Any]], crawled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Crawled structure, human text kept: display names and descriptions people typed win."""
    previous = {c.get("name"): c for c in existing if isinstance(c, dict)}
    merged = []
    for column in crawled:
        before = previous.get(column["name"])
        if before:
            column = {**column}
            if before.get("display_name"):
                column["display_name"] = before["display_name"]
            if before.get("description"):
                column["description"] = before["description"]
            if before.get("children") and not column.get("children"):
                column["children"] = before["children"]
        merged.append(column)
    return merged


def _same_columns(origin: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    names = {c.get("name") for c in target["json"].get("columns") or [] if isinstance(c, dict)}
    return [
        {"from_columns": [child_fqn(origin["fqn"], c["name"])], "to_column": child_fqn(target["fqn"], c["name"]), "function": ""}
        for c in origin["json"].get("columns") or [] if isinstance(c, dict) and c.get("name") in names
    ][:500]


def _iceberg_columns(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    schemas = metadata.get("schemas") if isinstance(metadata.get("schemas"), list) else []
    current = metadata.get("current-schema-id")
    schema = next((s for s in schemas if isinstance(s, dict) and s.get("schema-id") == current), schemas[0] if schemas else None)
    if schema is None and isinstance(metadata.get("schema"), dict):
        schema = metadata["schema"]
    fields = schema.get("fields") if isinstance(schema, dict) else []
    raw = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        raw.append({"name": field.get("name"), "type": _iceberg_type(field.get("type")), "nullable": not field.get("required", False), "comment": field.get("doc") or ""})
    return _columns_of(raw)


def _iceberg_type(value: Any) -> str:
    if isinstance(value, dict):
        kind = str(value.get("type") or "struct")
        if kind == "list":
            return f"list<{_iceberg_type(value.get('element'))}>"
        if kind == "map":
            return f"map<{_iceberg_type(value.get('key'))},{_iceberg_type(value.get('value'))}>"
        return kind
    return str(value or "")


def _semantic_document(loaded: Any) -> dict[str, Any] | None:
    """The semantic-model object out of a model-store record or a native API body."""
    if not isinstance(loaded, dict):
        return None
    document = loaded.get("document")
    parsed: Any = None
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
        try:
            data = yaml.safe_load(loaded["yaml"])
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            parsed = data.get("semantic_model", data)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else None
    return parsed if isinstance(parsed, dict) else None


def _dataset_summary(dataset: dict[str, Any]) -> dict[str, Any]:
    fields = dataset.get("fields") if isinstance(dataset.get("fields"), list) else []
    return {
        "name": str(dataset.get("name") or ""),
        "source": str(dataset.get("source") or ""),
        "description": str(dataset.get("description") or "")[:2000],
        "primary_key": [str(k) for k in dataset.get("primary_key") or []][:20],
        "fields": [{"name": str(f.get("name") or ""), "datatype": str(f.get("datatype") or ""), "description": str(f.get("description") or "")[:500]} for f in fields if isinstance(f, dict)][:500],
    }


def _metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
    expression = metric.get("expression")
    text = ""
    if isinstance(expression, dict):
        dialects = expression.get("dialects") if isinstance(expression.get("dialects"), list) else []
        text = next((str(d.get("expression")) for d in dialects if isinstance(d, dict) and d.get("expression")), "")
    elif isinstance(expression, str):
        text = expression
    return {"name": str(metric.get("name") or ""), "datatype": str(metric.get("datatype") or ""), "description": str(metric.get("description") or "")[:2000], "expression": text[:2000]}


def masked_connection(type_id: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return mask_config(type_id, config)
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["MetadataIngestor", "MetadataScheduler", "IngestionError", "BOT_USER", "POLARIS_SERVICE"]
