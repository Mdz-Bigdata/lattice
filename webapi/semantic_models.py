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

"""Gateway implementation of the five Polaris semantic-model operations.

Apache Polaris 1.7.0 ships the OpenAPI definition of the semantic-model
(Apache Ossie) API but its ``SemanticModelCatalogAdapter`` answers every call
with HTTP 501.  This module implements the same contract in the Lattice gateway:

* identical request / response shapes and error envelopes as the source spec
  ``polaris-catalog-apis/semantic-models-api.yaml`` (``LoadSemanticModelResponse``,
  ``ListSemanticModelsResponse``, ``IcebergErrorResponse``);
* the target namespace must exist in the real Polaris catalog (404 otherwise);
* documents are validated against the bundled Apache Ossie JSON schema (400 on failure);
* optimistic concurrency through opaque ``entity-version`` values (409 on
  mismatch);
* persistence as Polaris **Generic Table** records inside the requested
  catalog and namespace, so the data lives in the real Polaris metastore and
  survives restarts.

Only the public Polaris REST API is used; nothing is written to files or S3.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from validation import validate as validator

from .polaris import PolarisError

OPERATIONS = {
    "createSemanticModel",
    "listSemanticModels",
    "loadSemanticModel",
    "updateSemanticModel",
    "dropSemanticModel",
}
IMPLEMENTATION = "lattice-gateway"
FORMAT = "lattice-semantic-model"
RECORD_PREFIX = "lattice_semantic_model__"
OWNER = "lattice-webui"
STORAGE_VERSION = "v1"
NAME = re.compile(r"[A-Za-z0-9\-_]+\Z")
MAX_NAME = 200
MAX_ENTITY_VERSION = 255
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_PAGE_SIZE = 500
MAX_LIST_PAGES = 50
ENTITY_VERSION = re.compile(r"[A-Za-z0-9._:@+~-]{1,255}\Z")
UNIT_SEPARATOR = "\x1f"
SCHEMA = Path(__file__).resolve().parent.parent / "core-spec" / "ossie-schema.json"


class SemanticModelError(Exception):
    """An error that maps onto the spec's ``IcebergErrorResponse`` envelope."""

    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(message)
        self.status = status
        self.error_type = error_type


def error_response(status: int, error_type: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "body": {"error": {"message": message, "type": error_type, "code": status}},
        "headers": {"content-type": "application/json"},
    }


def _bad_request(message: str) -> SemanticModelError:
    return SemanticModelError(400, "BadRequestException", message)


def check_name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value) or len(value) > MAX_NAME:
        raise _bad_request(
            "semantic model name must match ^[A-Za-z0-9\\-_]+$ and be at most 200 characters"
        )
    return value


def check_prefix(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or any(ord(char) < 32 or char in "/\\" for char in value)
    ):
        raise _bad_request("invalid catalog prefix")
    return value


def namespace_parts(value: Any) -> list[str]:
    """Split the Iceberg REST namespace path parameter into its levels."""
    if not isinstance(value, str) or not value:
        raise _bad_request("namespace is required")
    raw = re.sub(r"%1f", UNIT_SEPARATOR, value, flags=re.IGNORECASE)
    parts = raw.split(UNIT_SEPARATOR)
    if any(
        not part or part in {".", ".."} or any(ord(char) < 32 for char in part)
        for part in parts
    ):
        raise _bad_request("invalid namespace")
    if sum(len(part) for part in parts) > 1000:
        raise _bad_request("namespace is too long")
    return parts


def namespace_param(parts: list[str]) -> str:
    return UNIT_SEPARATOR.join(parts)


def check_entity_version(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ENTITY_VERSION:
        raise _bad_request("entity-version must be a non-empty string of at most 255 characters")
    return value


def parse_document(document: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate a ``SemanticModelDocument`` and return it with the parsed model."""
    if not isinstance(document, dict):
        raise _bad_request("document must be an object with version and semantic_model")
    version = document.get("version")
    raw_model = document.get("semantic_model")
    if not isinstance(version, str) or not version.strip() or len(version) > 64:
        raise _bad_request("document.version must be a non-empty string")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise _bad_request("document.semantic_model must be a JSON string")
    unknown = set(document) - {"version", "semantic_model"}
    if unknown:
        raise _bad_request("document has unsupported fields: " + ", ".join(sorted(unknown)))
    try:
        encoded = raw_model.encode("utf-8")
    except UnicodeError as error:
        raise _bad_request("document.semantic_model contains invalid Unicode") from error
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise _bad_request("document.semantic_model must be at most 256 KiB")
    try:
        parsed = json.loads(raw_model)
    except ValueError as error:
        raise _bad_request("document.semantic_model is not valid JSON: " + str(error)[:200]) from error
    models = parsed if isinstance(parsed, list) else [parsed]
    if not models or not all(isinstance(item, dict) for item in models):
        raise _bad_request("document.semantic_model must be a JSON object (or array of objects)")
    data = {"version": version, "semantic_model": models}
    try:
        schema = json.loads(SCHEMA.read_text())
        failures = validator.validate_schema(data, schema)
        if not failures:
            messages = (
                validator.validate_unique_names(data)
                + validator.validate_references(data)
                + validator.validate_sql(data)
            )
            failures = [
                message
                for message in messages
                if not str(message).startswith(("[Reference] Warning:", "[SQL] Warning:"))
            ]
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError, TypeError) as error:
        raise _bad_request("semantic model validation failed: " + str(error)[:300]) from error
    if failures:
        raise _bad_request(
            "semantic model validation failed: "
            + "; ".join(str(message)[:300] for message in failures[:8])
        )
    return {"version": version, "semantic_model": raw_model}, data


def document_yaml(document: dict[str, str]) -> str:
    """Render a stored document as the YAML shape used by the Apache Ossie tooling."""
    parsed = json.loads(document["semantic_model"])
    models = parsed if isinstance(parsed, list) else [parsed]
    return yaml.safe_dump(
        {"version": document["version"], "semantic_model": models},
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def document_from_yaml(text: Any) -> dict[str, str]:
    """Convert an Apache Ossie YAML document into the API's JSON-string document."""
    if not isinstance(text, str) or not text.strip():
        raise _bad_request("YAML 内容不能为空。")
    if len(text.encode("utf-8", errors="replace")) > MAX_DOCUMENT_BYTES:
        raise _bad_request("YAML 内容不能超过 256 KiB。")
    try:
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(text)):
            raise _bad_request("YAML 不支持别名引用。")
        data = yaml.load(text, Loader=validator.UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise _bad_request("YAML 无效：" + str(error)[:300]) from error
    if not isinstance(data, dict) or "semantic_model" not in data:
        raise _bad_request("YAML 根节点必须包含 version 和 semantic_model。")
    version = data.get("version")
    if not isinstance(version, str):
        raise _bad_request("YAML 缺少文本类型的 version 字段。")
    model = data["semantic_model"]
    if isinstance(model, list) and len(model) == 1:
        model = model[0]
    try:
        serialized = json.dumps(model, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as error:
        raise _bad_request("YAML 无法转换为 JSON：" + str(error)[:200]) from error
    return {"version": version, "semantic_model": serialized}


def new_entity_version() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(8)


class SemanticModelService:
    """Serve the semantic-model operations on top of real Polaris generic tables."""

    def __init__(self, client):
        self.client = client

    # ----- upstream helpers ---------------------------------------------------------
    def _call(self, operation: str, path: dict[str, str], *, body=None, query=None,
              token: str | None = None) -> dict:
        """One documented catalog call, as the gateway or as ``token``'s own principal."""
        try:
            return self.client.request(
                "catalog", operation, path_params=path, body=body, query=query, token=token
            )
        except (PolarisError, httpx.HTTPError, OSError) as error:
            raise SemanticModelError(
                503, "ServiceUnavailableException", "Polaris 服务不可用：" + str(error)[:300]
            ) from error
        except ValueError as error:
            raise _bad_request(str(error)[:300]) from error

    @staticmethod
    def _passthrough(response: dict, action: str) -> None:
        """Turn an unexpected upstream status into the matching error envelope."""
        status = response.get("status")
        body = response.get("body")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message, error_type = error["message"], str(error.get("type") or "PolarisException")
        else:
            message, error_type = f"{action}：Polaris 返回 HTTP {status}", "PolarisException"
        if status in {401, 403}:
            raise SemanticModelError(status, error_type, message)
        raise SemanticModelError(502, "BadGatewayException", message)

    def _ensure_namespace(self, prefix: str, namespace: list[str], token=None) -> None:
        response = self._call(
            "loadNamespaceMetadata",
            {"prefix": prefix, "namespace": namespace_param(namespace)},
            token=token,
        )
        if response.get("status") == 200:
            return
        if response.get("status") == 404:
            raise SemanticModelError(
                404,
                "NoSuchNamespaceException",
                "Namespace does not exist: " + ".".join(namespace),
            )
        self._passthrough(response, "读取命名空间")

    @staticmethod
    def _record_name(name: str) -> str:
        return RECORD_PREFIX + name

    def _load_record(self, prefix: str, namespace: list[str], name: str, token=None) -> dict[str, Any] | None:
        response = self._call(
            "loadGenericTable",
            {"prefix": prefix, "namespace": namespace_param(namespace), "generic-table": self._record_name(name)},
            token=token,
        )
        if response.get("status") == 404:
            return None
        if response.get("status") != 200:
            self._passthrough(response, "读取语义模型")
        body = response.get("body")
        table = body.get("table") if isinstance(body, dict) else None
        properties = table.get("properties") if isinstance(table, dict) else None
        if (
            not isinstance(table, dict)
            or table.get("format") != FORMAT
            or not isinstance(properties, dict)
            or properties.get("lattice.owner") != OWNER
            or properties.get("lattice.semantic-model") != STORAGE_VERSION
        ):
            # A foreign generic table with a colliding name is not one of our models.
            return None
        document = {
            "version": str(properties.get("lattice.document-version") or ""),
            "semantic_model": str(properties.get("lattice.document-json") or ""),
        }
        entity_version = properties.get("lattice.entity-version")
        if (
            not document["version"]
            or not document["semantic_model"]
            or not isinstance(entity_version, str)
            or not ENTITY_VERSION.fullmatch(entity_version)
        ):
            raise SemanticModelError(
                502, "BadGatewayException", f"stored semantic model {name} is incomplete"
            )
        return {
            "name": name,
            "document": document,
            "entity_version": entity_version,
            "created_at": str(properties.get("lattice.created-at") or ""),
            "updated_at": str(properties.get("lattice.updated-at") or ""),
        }

    def _write_record(
        self,
        prefix: str,
        namespace: list[str],
        name: str,
        document: dict[str, str],
        entity_version: str,
        created_at: str,
        updated_at: str,
        token=None,
    ) -> dict:
        response = self._call(
            "createGenericTable",
            {"prefix": prefix, "namespace": namespace_param(namespace)},
            token=token,
            body={
                "name": self._record_name(name),
                "format": FORMAT,
                "doc": f"Apache Ossie semantic model '{name}' · stored by the Lattice gateway implementation of the Polaris semantic-model API",
                "properties": {
                    "lattice.owner": OWNER,
                    "lattice.semantic-model": STORAGE_VERSION,
                    "lattice.model-name": name,
                    "lattice.entity-version": entity_version,
                    "lattice.created-at": created_at,
                    "lattice.updated-at": updated_at,
                    "lattice.document-version": document["version"],
                    "lattice.document-json": document["semantic_model"],
                },
            },
        )
        return response

    def _drop_record(self, prefix: str, namespace: list[str], name: str, token=None) -> dict:
        return self._call(
            "dropGenericTable",
            {"prefix": prefix, "namespace": namespace_param(namespace), "generic-table": self._record_name(name)},
            token=token,
        )

    @staticmethod
    def _response(record: dict[str, Any], status: int = 200) -> dict[str, Any]:
        return {
            "status": status,
            "body": {"document": dict(record["document"]), "entity-version": record["entity_version"]},
            "headers": {"content-type": "application/json", "etag": f'"{record["entity_version"]}"'},
        }

    # ----- operations ---------------------------------------------------------------
    def create(self, prefix: str, namespace: list[str], body: Any, token=None) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise _bad_request("request body must be a CreateSemanticModelRequest object")
        name = check_name(body.get("name"))
        document, _ = parse_document(body.get("document"))
        self._ensure_namespace(prefix, namespace, token)
        if self._load_record(prefix, namespace, name, token) is not None:
            raise SemanticModelError(
                409, "AlreadyExistsException", "The given semantic model already exists"
            )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        entity_version = new_entity_version()
        response = self._write_record(
            prefix, namespace, name, document, entity_version, now, now, token
        )
        if response.get("status") == 409:
            raise SemanticModelError(
                409, "AlreadyExistsException", "The given semantic model already exists"
            )
        if response.get("status") not in {200, 201}:
            self._passthrough(response, "保存语义模型")
        saved = self._load_record(prefix, namespace, name, token)
        if saved is None or saved["entity_version"] != entity_version:
            raise SemanticModelError(
                502, "BadGatewayException", "semantic model was written but could not be read back"
            )
        return self._response(saved)

    def load(self, prefix: str, namespace: list[str], name: str, token=None) -> dict[str, Any]:
        name = check_name(name)
        record = self._load_record(prefix, namespace, name, token)
        if record is None:
            self._ensure_namespace(prefix, namespace, token)
            raise SemanticModelError(
                404, "NoSuchSemanticModelException", "The given semantic model does not exist"
            )
        return self._response(record)

    @staticmethod
    def _encode_cursor(skip: int, upstream: str | None) -> str:
        raw = json.dumps({"s": skip, "t": upstream or ""}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: Any) -> tuple[int, str | None]:
        if value in (None, ""):
            return 0, None
        if not isinstance(value, str) or len(value) > 4096:
            raise _bad_request("invalid pageToken")
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            skip = int(data["s"])
            upstream = data["t"]
        except (ValueError, KeyError, TypeError) as error:
            raise _bad_request("invalid pageToken") from error
        if skip < 0 or not isinstance(upstream, str) or len(upstream) > 4096:
            raise _bad_request("invalid pageToken")
        return skip, upstream or None

    def list(self, prefix: str, namespace: list[str], query: dict[str, Any] | None,
             token=None) -> dict[str, Any]:
        """List model identifiers, one page at a time.

        ``pageSize`` counts semantic models, but an upstream page counts *every*
        generic table in the namespace, so a page holding only foreign tables would
        otherwise look like an empty result. Upstream pages are consumed until the
        page is filled or the namespace is exhausted, and ``next-page-token`` is this
        service's own opaque cursor over that walk.
        """
        query = query or {}
        skip, cursor = self._decode_cursor(query.get("pageToken"))
        wanted = MAX_PAGE_SIZE
        page_size = query.get("pageSize")
        if page_size not in (None, ""):
            try:
                wanted = int(page_size)
            except (TypeError, ValueError) as error:
                raise _bad_request("pageSize must be an integer") from error
            if wanted < 1 or wanted > MAX_PAGE_SIZE:
                raise _bad_request(f"pageSize must be between 1 and {MAX_PAGE_SIZE}")
        self._ensure_namespace(prefix, namespace, token)
        identifiers: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        next_cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            upstream_query: dict[str, Any] = {"pageSize": MAX_PAGE_SIZE}
            if cursor:
                upstream_query["pageToken"] = cursor
            response = self._call(
                "listGenericTables",
                {"prefix": prefix, "namespace": namespace_param(namespace)},
                query=upstream_query,
                token=token,
            )
            if response.get("status") == 404:
                raise SemanticModelError(
                    404, "NoSuchNamespaceException", "Namespace does not exist: " + ".".join(namespace)
                )
            if response.get("status") != 200:
                self._passthrough(response, "列出语义模型")
            body = response.get("body") if isinstance(response.get("body"), dict) else {}
            names = []
            for item in body.get("identifiers", []) or []:
                table_name = item.get("name") if isinstance(item, dict) else None
                if isinstance(table_name, str) and table_name.startswith(RECORD_PREFIX):
                    model_name = table_name[len(RECORD_PREFIX) :]
                    if NAME.fullmatch(model_name):
                        names.append(model_name)
            consumed = min(skip, len(names))
            names = names[consumed:]
            skip -= consumed
            room = wanted - len(identifiers)
            if len(names) > room:
                identifiers.extend(
                    {"namespace": list(namespace), "name": name} for name in names[:room]
                )
                next_cursor = self._encode_cursor(consumed + room, cursor)
                break
            identifiers.extend({"namespace": list(namespace), "name": name} for name in names)
            upstream_next = body.get("next-page-token")
            if not isinstance(upstream_next, str) or not upstream_next:
                break
            if upstream_next in seen_tokens:
                raise SemanticModelError(
                    502, "BadGatewayException", "Polaris returned a repeated page token"
                )
            seen_tokens.add(upstream_next)
            cursor = upstream_next
            if len(identifiers) >= wanted:
                next_cursor = self._encode_cursor(0, cursor)
                break
        else:
            next_cursor = self._encode_cursor(0, cursor) if cursor else None
        result: dict[str, Any] = {"identifiers": identifiers}
        if next_cursor:
            result["next-page-token"] = next_cursor
        return {"status": 200, "body": result, "headers": {"content-type": "application/json"}}

    def update(self, prefix: str, namespace: list[str], name: str, body: Any,
               token=None) -> dict[str, Any]:
        """Replace a model's document.

        Polaris 1.7.0 generic tables have no update operation, so the record is
        dropped and written again. Any failure after the drop - a returned error,
        a dropped connection or a timeout - restores the previous document before
        reporting, so an interrupted update never silently loses the model.
        """
        name = check_name(name)
        if not isinstance(body, dict):
            raise _bad_request("request body must be an UpdateSemanticModelRequest object")
        expected = check_entity_version(body.get("entity-version"))
        document, _ = parse_document(body.get("document"))
        current = self._load_record(prefix, namespace, name, token)
        if current is None:
            self._ensure_namespace(prefix, namespace, token)
            raise SemanticModelError(
                404, "NoSuchSemanticModelException", "The given semantic model does not exist"
            )
        if current["entity_version"] != expected:
            raise SemanticModelError(
                409,
                "SemanticModelVersionMismatchException",
                "The semantic model version doesn't match the supplied entity-version",
            )
        entity_version = new_entity_version()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        dropped = self._drop_record(prefix, namespace, name, token)
        if dropped.get("status") not in {200, 204}:
            self._passthrough(dropped, "替换语义模型")
        try:
            written = self._write_record(
                prefix, namespace, name, document, entity_version,
                current["created_at"] or now, now, token,
            )
            if written.get("status") not in {200, 201}:
                raise SemanticModelError(
                    502,
                    "BadGatewayException",
                    f"更新语义模型失败：Polaris 返回 HTTP {written.get('status')}",
                )
            saved = self._load_record(prefix, namespace, name, token)
            if saved is None or saved["entity_version"] != entity_version:
                raise SemanticModelError(
                    502, "BadGatewayException", "semantic model was updated but could not be read back"
                )
            return self._response(saved)
        except BaseException as error:
            self._restore(prefix, namespace, name, current, now, token, error)
            raise

    def _restore(self, prefix, namespace, name, current, now, token, error) -> None:
        """Put the previous document back after a failed replacement."""
        try:
            restored = self._write_record(
                prefix, namespace, name, current["document"], current["entity_version"],
                current["created_at"] or now, current["updated_at"] or now, token,
            )
            if restored.get("status") in {200, 201, 409}:
                return
            detail = f"Polaris 返回 HTTP {restored.get('status')}"
        except (SemanticModelError, PolarisError, httpx.HTTPError, OSError, ValueError) as failure:
            detail = str(failure)[:200]
        message = (
            f"更新语义模型 {name} 失败后无法恢复原内容（{detail}），"
            f"该模型当前已从 Polaris 中删除，请使用 createSemanticModel 重新创建。"
        )
        if isinstance(error, SemanticModelError):
            error.args = (str(error) + "；" + message,)
        raise SemanticModelError(500, "InternalServerErrorException", message) from error

    def drop(self, prefix: str, namespace: list[str], name: str, token=None,
             entity_version: str | None = None) -> dict[str, Any]:
        name = check_name(name)
        record = self._load_record(prefix, namespace, name, token)
        if record is None:
            self._ensure_namespace(prefix, namespace, token)
            raise SemanticModelError(
                404, "NoSuchSemanticModelException", "The given semantic model does not exist"
            )
        if entity_version is not None and record["entity_version"] != entity_version:
            raise SemanticModelError(
                409,
                "SemanticModelVersionMismatchException",
                "The semantic model version doesn't match the supplied entity-version",
            )
        response = self._drop_record(prefix, namespace, name, token)
        if response.get("status") not in {200, 204}:
            self._passthrough(response, "删除语义模型")
        return {"status": 204, "body": None, "headers": {}}

    # ----- gateway dispatch ---------------------------------------------------------
    def handle(
        self,
        operation_id: str,
        path_params: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Serve one operation with the same envelope as ``PolarisClient.request``.

        ``token`` makes every upstream call act as that caller's own Polaris
        principal, so the caller's grants - not the gateway's - decide what the
        operation may read or change.
        """
        path_params = path_params or {}
        try:
            if operation_id not in OPERATIONS:
                raise _bad_request(f"unsupported operation: {operation_id}")
            prefix = check_prefix(path_params.get("prefix"))
            namespace = namespace_parts(path_params.get("namespace"))
            if operation_id == "createSemanticModel":
                return self.create(prefix, namespace, body, token)
            if operation_id == "listSemanticModels":
                return self.list(prefix, namespace, query, token)
            name = path_params.get("semantic-model-name")
            if operation_id == "loadSemanticModel":
                return self.load(prefix, namespace, name, token)
            if operation_id == "updateSemanticModel":
                return self.update(prefix, namespace, name, body, token)
            return self.drop(prefix, namespace, name, token)
        except SemanticModelError as error:
            return error_response(error.status, error.error_type, str(error))
        except (PolarisError, httpx.HTTPError, OSError) as error:
            return error_response(
                503, "ServiceUnavailableException", "Polaris 服务不可用：" + str(error)[:300]
            )

    # ----- direct REST access (external clients) ------------------------------------
    def authorize(self, authorization: str | None, prefix: str, namespace: list[str]) -> str:
        """Check the caller's own Polaris token against the target namespace.

        This only proves the caller can see the namespace; every operation that
        follows is executed with the same token, so Polaris itself enforces
        whether that principal may create, replace or drop the record.
        """
        if not authorization or not authorization.lower().startswith("bearer "):
            raise SemanticModelError(401, "NotAuthorizedException", "Bearer token required")
        token = authorization[7:].strip()
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise SemanticModelError(401, "NotAuthorizedException", "invalid bearer token")
        path = (
            "/api/catalog/v1/"
            + quote(prefix, safe="")
            + "/namespaces/"
            + quote(namespace_param(namespace), safe="")
        )
        try:
            response = self.client.caller_request("GET", path, token)
        except (PolarisError, httpx.HTTPError, OSError) as error:
            raise SemanticModelError(
                503, "ServiceUnavailableException", "Polaris 服务不可用：" + str(error)[:300]
            ) from error
        if response.status_code in {401, 403}:
            raise SemanticModelError(
                response.status_code, "NotAuthorizedException", "Polaris rejected the bearer token"
            )
        if response.status_code == 404:
            raise SemanticModelError(
                404, "NoSuchNamespaceException", "Namespace does not exist: " + ".".join(namespace)
            )
        if response.status_code != 200:
            raise SemanticModelError(
                502, "BadGatewayException", f"Polaris 返回 HTTP {response.status_code}"
            )
        return token

    # ----- WebUI conveniences -------------------------------------------------------
    def catalog_listing(self, prefix: str, namespace: list[str]) -> dict[str, Any]:
        """List models with their versions for the 语义模型 page."""
        items = []
        listed = self.list(prefix, namespace, {"pageSize": MAX_PAGE_SIZE})
        truncated = bool(listed["body"].get("next-page-token"))
        for identifier in listed["body"]["identifiers"]:
            record = self._load_record(prefix, namespace, identifier["name"])
            if record is None:
                continue
            try:
                parsed = json.loads(record["document"]["semantic_model"])
            except ValueError:
                parsed = {}
            model = parsed[0] if isinstance(parsed, list) and parsed else parsed
            items.append(
                {
                    "name": record["name"],
                    "entity_version": record["entity_version"],
                    "document_version": record["document"]["version"],
                    "model_name": str(model.get("name", "")) if isinstance(model, dict) else "",
                    "created_at": record["created_at"],
                    "updated_at": record["updated_at"],
                    "size_bytes": len(record["document"]["semantic_model"].encode("utf-8")),
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "catalog": prefix,
            "namespace": namespace,
            "items": items,
            "truncated": truncated,
            "implementation": IMPLEMENTATION,
            "storage": "Polaris Generic Table",
        }
