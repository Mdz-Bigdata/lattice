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

"""Lattice extension: immutable YAML model records in real Polaris generic tables.

This is deliberately separate from Polaris 1.7.0's unimplemented native semantic
model routes. Documents are stored as generic-table properties in PostgreSQL via
the public Polaris API; this module never writes files or S3 objects.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import uuid

import yaml
import httpx

from validation import validate as validator

from .ossie_document import check_document
from .polaris import PolarisClient, PolarisError

CATALOG = "lattice"
NAMESPACE = "demo"
STORAGE = "Polaris Generic Table"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_LIST_MODELS = 200
MAX_LIST_RECORDS = 1000
MODEL_ID = re.compile(r"lattice_model_[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
OWNER = "lattice-webui"


class ModelStoreError(ValueError):
    """A user-readable error that the HTTP layer can map without losing status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def document_bytes(document: object) -> bytes:
    if not isinstance(document, str) or not document.strip():
        raise ModelStoreError(400, "模型 YAML 不能为空。")
    try:
        content = document.encode("utf-8")
    except UnicodeError as error:
        raise ModelStoreError(400, "模型 YAML 包含无效的 Unicode 字符。") from error
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ModelStoreError(413, "模型 YAML 不能超过 256 KiB（UTF-8）。")
    return content


def parse_document(document: str) -> dict:
    """Parse bounded, alias-free YAML for both validation and persistence APIs."""
    document_bytes(document)
    try:
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(document)):
            raise ModelStoreError(422, "模型 YAML 不支持别名引用，请展开内容后再保存。")
        data = yaml.load(document, Loader=validator.UniqueKeyLoader)
        if not isinstance(data, dict):
            raise ModelStoreError(422, "模型 YAML 根节点必须是对象。")
        pending = [(data, 0)]
        node_count = 0
        while pending:
            value, depth = pending.pop()
            node_count += 1
            if depth > 48 or node_count > 20000:
                raise ModelStoreError(422, "模型 YAML 结构过深或节点过多。")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return data
    except ModelStoreError:
        raise
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as error:
        raise ModelStoreError(422, "模型 YAML 无效：" + str(error)[:400]) from error


def validate_document(document: str) -> bytes:
    """Validate on the server even when the browser previously validated YAML."""
    data = parse_document(document)
    try:
        failures, _ = check_document(data)
        if failures:
            raise ModelStoreError(
                422,
                "模型校验未通过："
                + "；".join(str(message)[:400] for message in failures[:8]),
            )
    except ModelStoreError:
        raise
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as error:
        raise ModelStoreError(422, "模型 YAML 无效：" + str(error)[:400]) from error
    return document.encode("utf-8")


def validate_name(name: object) -> str:
    if not isinstance(name, str):
        raise ModelStoreError(400, "模型名称必须是文本。")
    name = name.strip()
    try:
        name.encode("utf-8")
    except UnicodeError as error:
        raise ModelStoreError(400, "模型名称包含无效的 Unicode 字符。") from error
    if (
        not name
        or len(name) > 120
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ModelStoreError(400, "模型名称须为 1–120 个字符，不能包含控制字符。")
    return name


def validate_id(model_id: str) -> None:
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ModelStoreError(400, "模型记录 ID 无效。")


class PolarisModelStore:
    """Create/list/load/delete immutable application-owned generic-table records."""

    def __init__(self, client: PolarisClient):
        self.client = client
        self._lock = threading.RLock()

    def _request(self, operation: str, model_id=None, *, body=None, query=None):
        path = {"prefix": CATALOG, "namespace": NAMESPACE}
        if operation == "createNamespace":
            path.pop("namespace")
        if model_id is not None:
            path["generic-table"] = model_id
        return self.client.request(
            "catalog", operation, path_params=path, body=body, query=query
        )

    @staticmethod
    def _check(response: dict, allowed: set[int], action: str):
        status = response.get("status")
        if status in allowed:
            return response.get("body")
        if status == 404:
            raise ModelStoreError(
                404, f"{action}失败：目标记录或 lattice/demo 命名空间不存在。"
            )
        if status == 409:
            raise ModelStoreError(409, f"{action}失败：记录冲突；现有内容未被覆盖。")
        raise ModelStoreError(
            502, f"{action}失败：真实 Polaris 服务返回 HTTP {status}。"
        )

    def _ensure_namespace(self) -> None:
        response = self._request("loadNamespaceMetadata")
        if response.get("status") == 404:
            self._check(
                self._request("createNamespace", body={"namespace": [NAMESPACE]}),
                {200, 201, 409},
                "创建模型命名空间",
            )
        else:
            self._check(response, {200}, "读取模型命名空间")

    @staticmethod
    def _decode(model_id: str, response: dict, *, include_yaml: bool) -> dict:
        body = PolarisModelStore._check(response, {200}, "读取模型")
        table = body.get("table") if isinstance(body, dict) else None
        props = table.get("properties") if isinstance(table, dict) else None
        if (
            not isinstance(table, dict)
            or table.get("name") != model_id
            or table.get("format") != "lattice"
            or not isinstance(props, dict)
            or props.get("lattice.owner") != OWNER
            or props.get("lattice.model-storage") != "v1"
        ):
            raise ModelStoreError(
                409, "该 Generic Table 不属于 Lattice 模型扩展，拒绝读取或删除。"
            )
        document = props.get("lattice.document-yaml")
        content = document_bytes(document)
        digest = hashlib.sha256(content).hexdigest()
        if digest != props.get("lattice.document-sha256"):
            raise ModelStoreError(409, "模型内容与保存时的 SHA-256 不符，拒绝操作。")
        name = validate_name(props.get("lattice.model-name"))
        created_at = props.get("lattice.created-at")
        try:
            if not isinstance(created_at, str):
                raise ValueError("missing timestamp")
            parsed_time = dt.datetime.fromisoformat(created_at)
            if parsed_time.tzinfo is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError) as error:
            raise ModelStoreError(409, "模型记录的创建时间无效。") from error
        model: dict[str, object] = {
            "id": model_id,
            "name": name,
            "created_at": created_at,
            "sha256": digest,
            "size_bytes": len(content),
            "catalog": CATALOG,
            "namespace": NAMESPACE,
            "storage": STORAGE,
        }
        if include_yaml:
            model["yaml"] = document
        return model

    def create(self, name: str, yaml: str) -> dict:
        name = validate_name(name)
        content = validate_document(yaml)
        model_id = "lattice_model_" + uuid.uuid4().hex
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        properties = {
            "lattice.owner": OWNER,
            "lattice.model-storage": "v1",
            "lattice.model-name": name,
            "lattice.created-at": created_at,
            "lattice.document-sha256": hashlib.sha256(content).hexdigest(),
            "lattice.document-yaml": yaml,
        }
        with self._lock:
            self._ensure_namespace()
            try:
                created = self._request(
                    "createGenericTable",
                    body={
                        "name": model_id,
                        "format": "lattice",
                        "doc": "Lattice model extension · immutable Apache Ossie YAML record",
                        "properties": properties,
                    },
                )
            except (PolarisError, httpx.HTTPError, OSError) as error:
                raise ModelStoreError(
                    502,
                    f"模型创建请求的结果尚未确认（记录 ID：{model_id}）；请刷新已保存模型列表确认后再重试。",
                ) from error
            self._check(created, {200, 201}, "保存模型")
            # Read the actual upstream record back before claiming persistence.
            try:
                saved = self.get(model_id)
            except (ModelStoreError, PolarisError, httpx.HTTPError, OSError) as error:
                raise ModelStoreError(
                    502,
                    f"Polaris 已创建模型记录 {model_id}，但读回验证失败；请刷新已保存模型列表，避免重复保存。",
                ) from error
            if saved["sha256"] != properties["lattice.document-sha256"]:
                raise ModelStoreError(
                    502, f"模型 {model_id} 已创建，但实际读回内容不一致。"
                )
            return saved

    def get(self, model_id: str) -> dict:
        validate_id(model_id)
        with self._lock:
            return self._decode(
                model_id, self._request("loadGenericTable", model_id), include_yaml=True
            )

    def list(self) -> dict:
        items: list[dict] = []
        warnings: list[str] = []
        seen_ids, seen_tokens = set(), set()
        token = None
        scanned = 0
        truncated = False
        with self._lock:
            for _ in range(100):
                query: dict[str, object] = {"pageSize": 100}
                if token:
                    query["pageToken"] = token
                response = self._request("listGenericTables", query=query)
                if response.get("status") == 404:
                    break
                body = self._check(response, {200}, "列出模型")
                identifiers = (
                    body.get("identifiers") if isinstance(body, dict) else None
                )
                if not isinstance(identifiers, list):
                    raise ModelStoreError(502, "Polaris 模型列表响应格式无效。")
                for identifier in identifiers:
                    scanned += 1
                    if scanned > MAX_LIST_RECORDS or len(items) >= MAX_LIST_MODELS:
                        truncated = True
                        break
                    model_id = (
                        identifier.get("name") if isinstance(identifier, dict) else None
                    )
                    if (
                        not isinstance(model_id, str)
                        or not MODEL_ID.fullmatch(model_id)
                        or model_id in seen_ids
                    ):
                        continue
                    seen_ids.add(model_id)
                    loaded = self._request("loadGenericTable", model_id)
                    if loaded.get("status") == 404:
                        continue  # A concurrently removed record is not a list failure.
                    try:
                        items.append(self._decode(model_id, loaded, include_yaml=False))
                    except ModelStoreError as error:
                        if error.status_code >= 500:
                            raise
                        warnings.append(f"跳过记录 {model_id}：{error}")
                if truncated:
                    break
                token = body.get("next-page-token")
                if token is None or token == "":
                    break
                if not isinstance(token, str) or token in seen_tokens:
                    raise ModelStoreError(502, "Polaris 返回了重复或无效的分页令牌。")
                seen_tokens.add(token)
            else:
                truncated = True
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {
            "items": items,
            "catalog": CATALOG,
            "namespace": NAMESPACE,
            "storage": STORAGE,
            "warnings": warnings,
            "truncated": truncated,
        }

    def delete(self, model_id: str, sha256: str) -> dict:
        validate_id(model_id)
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            raise ModelStoreError(400, "删除模型须提供完整 SHA-256。")
        with self._lock:
            model = self.get(model_id)
            if model["sha256"] != sha256:
                raise ModelStoreError(
                    409, "模型内容与确认删除的版本不一致，未执行删除。"
                )
            self._check(
                self._request("dropGenericTable", model_id), {200, 204}, "删除模型"
            )
        return {"deleted": True, "id": model_id}
