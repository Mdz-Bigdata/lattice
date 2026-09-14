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

"""Registry of data sources: private on-disk records plus builtin local engines.

User-created sources are stored in ``datasources.json`` (owner-only permissions)
with their secrets; every API-facing view masks secrets. Builtin sources are
derived on demand from the private runtime files written by the Polaris and
local-engine launchers, so credential rotation never requires editing records.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .connectors import (
    CONNECTORS,
    ConnectorError,
    connector_types,
    make_connector,
    mask_config,
    merge_secrets,
)

SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}\Z")
MAX_SOURCES = 200
METADATA_TTL = 60


class DataSourceError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise DataSourceError(400, "数据源名称必须是文本。")
    name = name.strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise DataSourceError(400, "数据源名称须为 1–80 个字符，不能包含控制字符。")
    return name


def validate_description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 500:
        raise DataSourceError(400, "描述须为不超过 500 个字符的文本。")
    return value.strip()


class DataSourceRegistry:
    def __init__(self, runtime: Path, polaris_runtime: Path, engines_runtime: Path):
        self.runtime = runtime
        self.polaris_runtime = polaris_runtime
        self.engines_runtime = engines_runtime
        self.path = runtime / "datasources.json"
        self._lock = threading.RLock()
        self._metadata_cache: dict[str, tuple[float, str]] = {}
        runtime.mkdir(parents=True, exist_ok=True)

    # ----- persistence --------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"sources": {}, "tests": {}}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise DataSourceError(500, f"数据源配置文件无法读取：{self.path}") from error
        if not isinstance(data, dict):
            raise DataSourceError(500, "数据源配置文件格式无效。")
        data.setdefault("sources", {})
        data.setdefault("tests", {})
        # Builtin sources are derived from the runtime files on every read, so a
        # removal has to be remembered here or the entry would come back on the
        # next start.
        data.setdefault("hidden", [])
        if not isinstance(data["hidden"], list):
            data["hidden"] = []
        return data

    def _write(self, data: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, prefix=".datasources-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    # ----- builtin sources ------------------------------------------------------
    def _read_private(self, path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def builtin_records(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        sample = self.runtime / "sample.duckdb"
        records["local-sample"] = {
            "id": "local-sample",
            "name": "本地示例数据 · DuckDB",
            "type": "duckdb",
            "description": "启动时生成的六张示例业务表，用于演示问数、SQL 与语义模型。",
            "config": {"path": str(sample), "read_only": True},
        }
        credentials = self._read_private(self.polaris_runtime / "credentials.json")
        s3 = self._read_private(self.polaris_runtime / "local-s3.json") or {}
        if credentials and all(
            isinstance(credentials.get(key), str) and credentials[key]
            for key in ("base_url", "client_id", "client_secret", "realm")
        ):
            headers = [f"Polaris-Realm: {credentials['realm']}", "X-Iceberg-Access-Delegation: vended-credentials"]
            records["iceberg-local"] = {
                "id": "iceberg-local",
                "name": "Apache Polaris · Iceberg REST（本地）",
                "type": "iceberg",
                "description": "本项目启动的 Apache Polaris 1.7.0 REST Catalog（catalog lattice），数据存放在本地 MinIO。",
                "config": {
                    "catalog_type": "rest",
                    "uri": credentials["base_url"].rstrip("/") + "/api/catalog",
                    "warehouse": "lattice",
                    "credential": f"{credentials['client_id']}:{credentials['client_secret']}",
                    "token": "",
                    "scope": "PRINCIPAL_ROLE:ALL",
                    "extra_headers": "\n".join(headers),
                    "s3_endpoint": str(s3.get("endpoint") or ""),
                    "s3_access_key": str(s3.get("access_key") or ""),
                    "s3_secret_key": str(s3.get("secret_key") or ""),
                    "s3_region": str(s3.get("region") or "us-east-1"),
                    "namespace": "demo",
                },
            }
        engines = {
            "mysql-local": ("mysql.json", "mysql", "本地 MySQL（示例数据）", "本项目启动的 MySQL 实例（Homebrew），端口 33306。"),
            "clickhouse-local": ("clickhouse.json", "clickhouse", "本地 ClickHouse（示例数据）", "本项目启动的官方 ClickHouse 二进制，HTTP 端口 18123。"),
            "paimon-local": ("paimon.json", "paimon", "本地 Paimon 仓库（示例数据）", "本地文件系统上的 Paimon warehouse（pypaimon 写入）。"),
            "starrocks-local": ("starrocks.json", "starrocks", "本地 StarRocks（示例数据）", "本项目启动的官方 StarRocks 容器，FE MySQL 协议端口 19030。"),
            "doris-local": ("doris.json", "doris", "本地 Apache Doris（示例数据）", "本项目启动的官方 Doris FE/BE 容器，FE MySQL 协议端口 29030。"),
            "hive-local": ("hive.json", "hive", "本地 Apache Hive（示例数据）", "本项目启动的官方 Hive 容器，HiveServer2 端口 20000。"),
            # One PostgreSQL entry, and it is the real business database: the
            # demo schema in the project's own cluster is still seeded by the
            # engine manager, but listing it as a second PostgreSQL source only
            # produced two entries the reader had to tell apart.
            "quality-postgres": ("quality-datasource.json", "postgresql", "PostgreSQL", "本机业务库 blog_converter；数据质量模块的质量元数据保存在其 lattice_quality 模式中。"),
        }
        for source_id, (file_name, type_id, name, description) in engines.items():
            data = self._read_private(self.engines_runtime / file_name)
            if not data or data.get("type") != type_id:
                continue
            allowed = {item["name"] for item in CONNECTORS[type_id].fields}
            config = {key: value for key, value in data.items() if key in allowed}
            if type_id == "postgresql":
                config.setdefault("schema", "public")
            records[source_id] = {
                "id": source_id,
                "name": name,
                "type": type_id,
                "description": description,
                "config": config,
            }
        for record in records.values():
            record["builtin"] = True
            record.setdefault("created_at", "")
            record.setdefault("updated_at", "")
        return records

    # ----- views ----------------------------------------------------------------
    def _public(self, record: dict[str, Any], tests: dict[str, Any]) -> dict[str, Any]:
        connector_class = CONNECTORS[record["type"]]
        try:
            connector = connector_class(record["config"])
            summary = connector.summary()
        except ConnectorError as error:
            summary = f"配置无效：{error}"
        return {
            "id": record["id"],
            "name": record["name"],
            "type": record["type"],
            "type_label": connector_class.label,
            "category": connector_class.category,
            "dialect": connector_class.dialect,
            "builtin": bool(record.get("builtin")),
            "description": record.get("description", ""),
            "config": mask_config(record["type"], record["config"]),
            "summary": summary,
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
            "last_test": tests.get(record["id"]),
        }

    def _records(self, data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        data = data or self._read()
        hidden = {str(value) for value in data.get("hidden", [])}
        records = {
            source_id: record
            for source_id, record in self.builtin_records().items()
            if source_id not in hidden
        }
        for source_id, record in data["sources"].items():
            if (
                isinstance(record, dict)
                and record.get("type") in CONNECTORS
                and isinstance(record.get("config"), dict)
                and source_id not in records
            ):
                records[source_id] = {**record, "id": source_id, "builtin": False}
        return records

    def list(self) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            records = self._records(data)
            return {
                "items": [self._public(record, data["tests"]) for record in records.values()],
                "types": connector_types(),
            }

    def record(self, source_id: str) -> dict[str, Any]:
        if not isinstance(source_id, str) or not SOURCE_ID.match(source_id):
            raise DataSourceError(400, "数据源 ID 无效。")
        with self._lock:
            records = self._records()
        record = records.get(source_id)
        if record is None:
            raise DataSourceError(404, "数据源不存在。")
        return record

    def get(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            record = self._records(data).get(source_id)
            if record is None or not SOURCE_ID.match(str(source_id)):
                raise DataSourceError(404, "数据源不存在。")
            return self._public(record, data["tests"])

    def connector(self, source_id: str):
        record = self.record(source_id)
        try:
            return make_connector(record["type"], record["config"])
        except ConnectorError as error:
            raise DataSourceError(400, f"数据源配置无效：{error}") from error

    # ----- mutations ------------------------------------------------------------
    def create(self, name: Any, type_id: Any, config: Any, description: Any = None) -> dict[str, Any]:
        if type_id not in CONNECTORS:
            raise DataSourceError(400, "不支持的数据源类型。")
        name = validate_name(name)
        description = validate_description(description)
        try:
            normalized = CONNECTORS[type_id].normalize(config)
        except ConnectorError as error:
            raise DataSourceError(400, str(error)) from error
        with self._lock:
            data = self._read()
            if len(data["sources"]) >= MAX_SOURCES:
                raise DataSourceError(400, f"最多注册 {MAX_SOURCES} 个数据源。")
            source_id = "ds_" + secrets.token_hex(6)
            while source_id in data["sources"] or source_id in self.builtin_records():
                source_id = "ds_" + secrets.token_hex(6)
            stamp = now()
            record = {
                "name": name,
                "type": type_id,
                "description": description,
                "config": normalized,
                "created_at": stamp,
                "updated_at": stamp,
            }
            data["sources"][source_id] = record
            self._write(data)
            return self._public({**record, "id": source_id, "builtin": False}, data["tests"])

    def update(self, source_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            record = self._records(data).get(source_id)
            if record is None:
                raise DataSourceError(404, "数据源不存在。")
            if record.get("builtin"):
                raise DataSourceError(400, "内置数据源的连接由本地服务管理，不能修改。")
            updated = dict(data["sources"][source_id])
            if "name" in patch and patch["name"] is not None:
                updated["name"] = validate_name(patch["name"])
            if "description" in patch:
                updated["description"] = validate_description(patch["description"])
            if "config" in patch and patch["config"] is not None:
                if not isinstance(patch["config"], dict):
                    raise DataSourceError(400, "连接配置必须是对象。")
                merged = merge_secrets(record["type"], patch["config"], record["config"])
                try:
                    updated["config"] = CONNECTORS[record["type"]].normalize(merged)
                except ConnectorError as error:
                    raise DataSourceError(400, str(error)) from error
            updated["updated_at"] = now()
            data["sources"][source_id] = updated
            data["tests"].pop(source_id, None)
            self._metadata_cache.pop(source_id, None)
            self._write(data)
            return self._public({**updated, "id": source_id, "builtin": False}, data["tests"])

    def delete(self, source_id: str) -> None:
        """Remove a source from the list; a builtin one is remembered as hidden."""
        with self._lock:
            data = self._read()
            record = self._records(data).get(source_id)
            if record is None:
                raise DataSourceError(404, "数据源不存在。")
            if record.get("builtin"):
                hidden = [str(value) for value in data.get("hidden", [])]
                if source_id not in hidden:
                    hidden.append(source_id)
                data["hidden"] = hidden
            data["sources"].pop(source_id, None)
            data["tests"].pop(source_id, None)
            self._metadata_cache.pop(source_id, None)
            self._write(data)

    def restore(self) -> dict[str, Any]:
        """Bring back every hidden builtin source; user-created ones are untouched."""
        with self._lock:
            data = self._read()
            restored = [str(value) for value in data.get("hidden", [])]
            data["hidden"] = []
            self._write(data)
            self._metadata_cache.clear()
        return {"restored": restored}

    # ----- connectivity ---------------------------------------------------------
    @staticmethod
    def _failure(error: Exception, started: float) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "offline",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "detail": str(error)[:600],
            "tested_at": now(),
        }

    def test_config(self, type_id: Any, config: Any, source_id: str | None = None) -> dict[str, Any]:
        if type_id not in CONNECTORS:
            raise DataSourceError(400, "不支持的数据源类型。")
        if not isinstance(config, dict):
            raise DataSourceError(400, "连接配置必须是对象。")
        stored = None
        if source_id:
            record = self.record(source_id)
            if record["type"] != type_id:
                raise DataSourceError(400, "数据源类型与已保存的记录不一致。")
            stored = record["config"]
        merged = merge_secrets(type_id, config, stored)
        started = time.monotonic()
        try:
            connector = make_connector(type_id, merged)
        except ConnectorError as error:
            return {**self._failure(error, started), "status": "error"}
        try:
            result = connector.test()
        except ConnectorError as error:
            return self._failure(error, started)
        result["tested_at"] = now()
        return result

    def test(self, source_id: str) -> dict[str, Any]:
        record = self.record(source_id)
        result = self.test_config(record["type"], record["config"])
        with self._lock:
            data = self._read()
            data["tests"][source_id] = result
            self._write(data)
        return result

    def statuses(self) -> list[dict[str, Any]]:
        """Cheap summary using stored test results only (no network)."""
        listing = self.list()
        return [
            {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "type_label": item["type_label"],
                "status": (item["last_test"] or {}).get("status", "unknown"),
            }
            for item in listing["items"]
        ]

    # ----- metadata for natural-language SQL --------------------------------------
    def schema_context(self, source_id: str, max_tables: int = 40) -> str:
        cached = self._metadata_cache.get(source_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        connector = self.connector(source_id)
        default = connector.default_schema()
        schemas = [default] if default else [item["name"] for item in connector.schemas()[:3]]
        lines: list[str] = []
        count = 0
        for schema in schemas:
            for table in connector.tables(schema):
                if count >= max_tables:
                    break
                try:
                    detail = connector.table(schema, table["name"])
                except ConnectorError:
                    continue
                columns = ", ".join(f"{col['name']} {col['type']}" for col in detail["columns"][:60])
                qualified = f"{schema}.{table['name']}" if schema else table["name"]
                rows = f"，约 {detail['rows']} 行" if detail.get("rows") is not None else ""
                lines.append(f"- {qualified}{rows}：{columns}")
                count += 1
        text = "\n".join(lines)
        self._metadata_cache[source_id] = (time.monotonic() + METADATA_TTL, text)
        return text

    def invalidate(self, source_id: str | None = None) -> None:
        if source_id is None:
            self._metadata_cache.clear()
        else:
            self._metadata_cache.pop(source_id, None)
