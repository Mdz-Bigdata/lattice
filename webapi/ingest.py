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

"""Register external tables in Apache Polaris as Generic Tables (数据接入).

Polaris 1.7.0 Generic Tables are format-agnostic catalog entries. Lattice uses
them to make MySQL/PostgreSQL/ClickHouse/StarRocks/Doris/Hive/Paimon tables
discoverable next to Iceberg tables. Only metadata is stored (source id, schema,
table, columns); never credentials.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import httpx

from .connectors import ConnectorError
from .datasources import DataSourceError, DataSourceRegistry
from .polaris import PolarisClient, PolarisError

DEFAULT_CATALOG = "lattice"
DEFAULT_NAMESPACE = "demo"
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
MAX_LISTED = 200
MODEL_FORMAT = "lattice"
SEMANTIC_MODEL_FORMAT = "lattice-semantic-model"


class IngestError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise IngestError(400, f"{what}只能包含字母、数字和下划线，且以字母或下划线开头。")
    return value


def parse_namespace(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [part for part in value.split(".") if part]
    elif isinstance(value, list):
        parts = [str(part) for part in value]
    else:
        parts = []
    if not parts:
        return [DEFAULT_NAMESPACE]
    return [check_name(part, "命名空间") for part in parts]


class IngestService:
    def __init__(self, polaris: PolarisClient, registry: DataSourceRegistry):
        self.polaris = polaris
        self.registry = registry

    def _request(self, operation: str, path: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        try:
            return self.polaris.request("catalog", operation, path_params=path, **kwargs)
        except ValueError as error:
            raise IngestError(400, str(error)) from error
        except (PolarisError, httpx.HTTPError, OSError, KeyError) as error:
            raise IngestError(502, "Polaris 请求失败：" + str(error)[:300]) from error

    @staticmethod
    def _body(response: dict[str, Any], allowed: set[int], action: str) -> Any:
        status = response.get("status")
        if status in allowed:
            return response.get("body")
        if status == 404:
            raise IngestError(404, f"{action}失败：目标不存在。")
        if status == 409:
            raise IngestError(409, f"{action}失败：同名对象已存在。")
        if status in {401, 403}:
            raise IngestError(403, f"{action}失败：Polaris 拒绝了该操作（HTTP {status}）。")
        raise IngestError(502, f"{action}失败：Polaris 返回 HTTP {status}。")

    def catalog(self, catalog: Any) -> dict[str, Any]:
        catalog = check_name(catalog or DEFAULT_CATALOG, "Catalog 名称")
        namespaces = self._body(
            self._request("listNamespaces", {"prefix": catalog}), {200}, "读取命名空间"
        )
        listed = namespaces.get("namespaces", []) if isinstance(namespaces, dict) else []
        tables: list[dict[str, Any]] = []
        for namespace in listed[:50]:
            if not isinstance(namespace, list):
                continue
            path = {"prefix": catalog, "namespace": "".join(namespace)}
            iceberg = self._body(self._request("listTables", path), {200}, "读取表列表")
            for item in (iceberg.get("identifiers", []) if isinstance(iceberg, dict) else []):
                tables.append(
                    {"catalog": catalog, "namespace": namespace, "name": item.get("name"), "kind": "iceberg"}
                )
            generic = self._request("listGenericTables", path)
            if generic.get("status") == 200 and isinstance(generic.get("body"), dict):
                for item in generic["body"].get("identifiers", [])[:MAX_LISTED]:
                    entry = {"catalog": catalog, "namespace": namespace, "name": item.get("name"), "kind": "generic"}
                    loaded = self._request("loadGenericTable", {**path, "generic-table": item.get("name", "")})
                    table = loaded.get("body", {}).get("table") if loaded.get("status") == 200 else None
                    if isinstance(table, dict):
                        properties = table.get("properties") or {}
                        entry["format"] = table.get("format")
                        entry["base_location"] = table.get("base-location")
                        entry["doc"] = table.get("doc")
                        entry["source_datasource"] = properties.get("lattice.source-datasource")
                        entry["source_table"] = properties.get("lattice.source-table")
                        entry["properties"] = {
                            key: str(value)[:200]
                            for key, value in properties.items()
                            if key != "lattice.columns" and key != "lattice.document"
                        }
                    if entry.get("format") in (MODEL_FORMAT, SEMANTIC_MODEL_FORMAT):
                        entry["kind"] = "lattice-model"
                    tables.append(entry)
            if len(tables) >= MAX_LISTED:
                break
        return {"catalog": catalog, "namespaces": listed, "tables": tables[:MAX_LISTED]}

    def ensure_namespace(self, catalog: str, namespace: list[str]) -> None:
        path = {"prefix": catalog, "namespace": "".join(namespace)}
        response = self._request("loadNamespaceMetadata", path)
        if response.get("status") == 404:
            self._body(
                self._request("createNamespace", {"prefix": catalog}, body={"namespace": namespace}),
                {200, 201, 409},
                "创建命名空间",
            )
        else:
            self._body(response, {200}, "读取命名空间")

    def register(
        self,
        datasource_id: Any,
        schema: Any,
        name: Any,
        catalog: Any = None,
        namespace: Any = None,
        table_name: Any = None,
    ) -> dict[str, Any]:
        catalog = check_name(catalog or DEFAULT_CATALOG, "Catalog 名称")
        parts = parse_namespace(namespace)
        try:
            record = self.registry.record(str(datasource_id))
            connector = self.registry.connector(record["id"])
            detail = connector.table(str(schema or ""), str(name or ""))
        except DataSourceError as error:
            raise IngestError(error.status_code, str(error)) from error
        except ConnectorError as error:
            raise IngestError(400, "读取源表结构失败：" + str(error)) from error
        target = check_name(table_name or re.sub(r"[^A-Za-z0-9_]", "_", str(name)), "Polaris 表名")
        if record["type"] == "iceberg":
            raise IngestError(400, "Iceberg 表已经在 Catalog 中，无需再注册为通用表。")
        connector_summary = connector.summary()
        properties = {
            "lattice.source-datasource": record["id"],
            "lattice.source-name": record["name"],
            "lattice.source-type": record["type"],
            "lattice.source-summary": connector_summary,
            "lattice.source-schema": detail["schema"],
            "lattice.source-table": detail["name"],
            "lattice.columns": json.dumps(
                [{"name": c["name"], "type": c["type"]} for c in detail["columns"]][:200],
                ensure_ascii=False,
            )[:16000],
            "lattice.registered-at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if detail.get("rows") is not None:
            properties["lattice.row-count"] = str(detail["rows"])
        body: dict[str, Any] = {
            "name": target,
            "format": record["type"],
            "doc": f"{record['name']} 中的表 {detail['schema']}.{detail['name']}（通过 Lattice 数据接入注册）",
            "properties": properties,
        }
        base_location = None
        if record["type"] == "paimon":
            warehouse = str(record["config"].get("warehouse") or "").rstrip("/")
            if warehouse:
                base_location = f"{warehouse}/{detail['schema']}.db/{detail['name']}"
        elif isinstance(detail.get("properties"), dict) and detail["properties"].get("location"):
            base_location = detail["properties"]["location"]
        if base_location:
            body["base-location"] = base_location
        self.ensure_namespace(catalog, parts)
        path = {"prefix": catalog, "namespace": "".join(parts)}
        created = self._body(
            self._request("createGenericTable", path, body=body), {200, 201}, "注册通用表"
        )
        table = created.get("table") if isinstance(created, dict) else None
        return {
            "catalog": catalog,
            "namespace": parts,
            "name": target,
            "kind": "generic",
            "format": record["type"],
            "source_datasource": record["id"],
            "source_table": f"{detail['schema']}.{detail['name']}",
            "base_location": base_location,
            "properties": (table or {}).get("properties", properties) if isinstance(table, dict) else properties,
        }

    def unregister(self, catalog: Any, namespace: Any, name: Any) -> None:
        catalog = check_name(catalog or DEFAULT_CATALOG, "Catalog 名称")
        parts = parse_namespace(namespace)
        target = check_name(name, "表名")
        path = {"prefix": catalog, "namespace": "".join(parts), "generic-table": target}
        loaded = self._body(self._request("loadGenericTable", path), {200}, "读取通用表")
        table = loaded.get("table") if isinstance(loaded, dict) else None
        if isinstance(table, dict) and table.get("format") in (MODEL_FORMAT, SEMANTIC_MODEL_FORMAT):
            raise IngestError(400, "该记录是 Lattice 语义模型存储，请在语义模型页面删除。")
        self._body(self._request("dropGenericTable", path), {200, 204}, "取消注册")
