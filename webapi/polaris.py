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

"""Authenticated, fixed-target gateway to the actual local Apache Polaris."""

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

BASES = {"management": "/api/management/v1", "catalog": "/api/catalog"}
RESERVED_HEADERS = {
    "authorization",
    "host",
    "cookie",
    "polaris-realm",
    "content-length",
    "connection",
    "transfer-encoding",
    "content-type",
    "proxy-authorization",
}


class PolarisError(Exception):
    pass


def check_token(token: object) -> str:
    """Reject anything that cannot be a bearer token before it reaches a header."""
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise ValueError("无效的访问令牌。")
    return token


class PolarisClient:
    def __init__(self, credentials: Path, registry):
        self.credentials_path = credentials
        self.registry = registry
        self._token = ""
        self._expires = 0
        self._lock = threading.Lock()
        self._credential_mutation_lock = threading.Lock()

    def _credentials(self):
        if not self.credentials_path.is_file():
            raise PolarisError("Polaris 尚未启动，请运行 ./start-web.sh。")
        data = json.loads(self.credentials_path.read_text())
        parsed = urlsplit(data["base_url"])
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise PolarisError("Polaris 地址必须是配置中的本机服务地址。")
        return data

    @staticmethod
    def _client():
        return httpx.Client(
            timeout=httpx.Timeout(30, connect=4),
            trust_env=False,
            follow_redirects=False,
        )

    def _authorization(self, credentials):
        with self._lock:
            if self._token and self._expires > time.monotonic():
                return self._token
            with self._client() as client:
                response = client.post(
                    credentials["base_url"] + "/api/catalog/v1/oauth/tokens",
                    headers={"Polaris-Realm": credentials["realm"]},
                    data={
                        "grant_type": "client_credentials",
                        "client_id": credentials["client_id"],
                        "client_secret": credentials["client_secret"],
                        "scope": "PRINCIPAL_ROLE:ALL",
                    },
                )
            if response.status_code != 200:
                raise PolarisError(
                    f"Polaris 服务身份认证失败（HTTP {response.status_code}），请检查服务配置。"
                )
            body = response.json()
            self._token = body["access_token"]
            self._expires = time.monotonic() + max(
                0, int(body.get("expires_in", 300)) - 15
            )
            return self._token

    def service(self):
        """Base URL and realm of the configured local service, without the secrets."""
        credentials = self._credentials()
        return credentials["base_url"], credentials["realm"]

    def caller_request(self, method: str, path: str, token: str):
        """Send one request with a caller-supplied bearer token, never the gateway's."""
        base_url, realm = self.service()
        with self._client() as client:
            return client.request(
                method,
                base_url + path,
                headers={
                    "Authorization": "Bearer " + check_token(token),
                    "Polaris-Realm": realm,
                },
            )

    def status(self):
        try:
            response = self.request("management", "listCatalogs")
            if response["status"] != 200:
                return {
                    "status": "error",
                    "version": "1.7.0",
                    "detail": f"Polaris 返回 HTTP {response['status']}",
                }
            return {
                "status": "online",
                "version": "1.7.0",
                "detail": "本地持久化服务已连接",
                "catalog_count": len(response["body"].get("catalogs", [])),
            }
        except (PolarisError, httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            return {"status": "offline", "version": "1.7.0", "detail": str(exc)}

    @staticmethod
    def _value(value, schema):
        if schema.get("type") == "array":
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value.split(",")
            if not isinstance(value, list):
                raise ValueError("数组参数请使用 JSON 数组。")
            return [
                str(item).lower() if isinstance(item, bool) else str(item)
                for item in value
            ]
        if isinstance(value, (dict, list)):
            raise ValueError("该参数必须是标量。")
        return str(value).lower() if isinstance(value, bool) else str(value)

    def request(
        self,
        spec,
        operation_id,
        path_params=None,
        query=None,
        body=None,
        headers=None,
        token=None,
    ):
        """Send one documented operation. ``token`` acts as that caller instead of root."""
        if token is not None:
            return self._request(
                spec, operation_id, path_params, query, body, headers, token
            )
        if spec == "management" and operation_id in {
            "rotateCredentials",
            "resetCredentials",
        }:
            # Credential changes are not idempotent. Serialize them and persist
            # successful changes before another operation can reuse the old pair.
            with self._credential_mutation_lock:
                return self._request(
                    spec, operation_id, path_params, query, body, headers
                )
        return self._request(spec, operation_id, path_params, query, body, headers)

    def _request(
        self,
        spec,
        operation_id,
        path_params=None,
        query=None,
        body=None,
        headers=None,
        token=None,
    ):
        operation = self.registry.operation(spec, operation_id)
        path_params, query, headers = path_params or {}, query or {}, headers or {}
        route = operation["path"]
        parameters = operation.get("parameters", [])
        allowed = {
            location: {p["name"]: p for p in parameters if p["in"] == location}
            for location in ("path", "query", "header")
        }
        for name in path_params:
            if name not in allowed["path"]:
                raise ValueError(f"未声明的路径参数：{name}")
        for name, parameter in allowed["path"].items():
            value = path_params.get(name)
            if value is None or str(value) == "":
                raise ValueError(f"缺少路径参数：{name}")
            value = str(value)
            if name == "namespace":
                # Iceberg uses a unit separator for multipart namespaces;
                # examples in the upstream spec use its percent-encoded form.
                value = re.sub(r"%1f", "\u001f", value, flags=re.IGNORECASE)
            if value in {".", ".."} or any(
                ord(char) < 32 and not (name == "namespace" and char == "\u001f")
                for char in value
            ):
                raise ValueError("路径参数包含非法字符。")
            route = route.replace("{" + name + "}", quote(value, safe=""))
        if "{" in route:
            raise ValueError("路径参数不完整。")
        params = {}
        for name, value in query.items():
            if name not in allowed["query"]:
                raise ValueError(f"未声明的查询参数：{name}")
            if value is not None and value != "":
                params[name] = self._value(
                    value, allowed["query"][name].get("schema", {})
                )
        for name, parameter in allowed["query"].items():
            if parameter.get("required") and name not in params:
                raise ValueError(f"缺少查询参数：{name}")
        request_headers = {}
        header_names = {name.lower(): p for name, p in allowed["header"].items()}
        for name, value in headers.items():
            if name.lower() in RESERVED_HEADERS or name.lower() not in header_names:
                raise ValueError(f"不允许覆盖请求头：{name}")
            if "\r" in str(value) or "\n" in str(value):
                raise ValueError("请求头包含非法换行。")
            request_headers[name] = str(value)
        for name, parameter in header_names.items():
            if parameter.get("required") and name not in {
                key.lower() for key in request_headers
            }:
                raise ValueError(f"缺少请求头：{name}")
        if operation.get("request_required") and body is None:
            raise ValueError("该操作需要请求体。")
        credentials = self._credentials()
        request_headers["Polaris-Realm"] = credentials["realm"]
        is_token_request = spec == "catalog" and route == "/v1/oauth/tokens"
        if token is not None:
            # A caller-supplied identity is never combined with the gateway's own.
            request_headers["Authorization"] = "Bearer " + check_token(token)
        elif not is_token_request:
            request_headers["Authorization"] = "Bearer " + self._authorization(
                credentials
            )
        changes_gateway_identity = (
            spec == "management"
            and operation_id in {"rotateCredentials", "resetCredentials"}
            and path_params.get("principalName")
            == credentials.get("principal_name", "root")
        )
        if changes_gateway_identity:
            # Fail before the non-idempotent upstream action when the directory
            # cannot hold a replacement credential record.
            with tempfile.TemporaryFile(
                mode="w", dir=self.credentials_path.parent
            ) as probe:
                probe.write(" " * 8192)
                probe.flush()
                os.fsync(probe.fileno())
        # The OAuth workbench uses only caller-supplied credentials. Never inject
        # the gateway's root client credentials into its visible response.
        kwargs = {"params": params, "headers": request_headers}
        if body is not None:
            if operation.get("media_type") == "application/x-www-form-urlencoded":
                if not isinstance(body, dict):
                    raise ValueError("OAuth 表单请求体必须是 JSON 对象。")
                kwargs["data"] = body
            else:
                kwargs["json"] = body
        with self._client() as client:
            response = client.request(
                operation["method"],
                credentials["base_url"] + BASES[spec] + route,
                **kwargs,
            )
        if response.status_code == 401 and not is_token_request and token is None:
            with self._lock:
                self._token = ""
                self._expires = 0
        try:
            response_body = response.json() if response.content else None
        except ValueError:
            response_body = response.text
        if changes_gateway_identity and response.is_success:
            self._save_credentials(credentials, response_body)
            response_body = dict(response_body)
            response_body["credentials"] = {
                "clientId": response_body["credentials"]["clientId"],
                "clientSecret": "[已保存到本机私有配置]",
            }
        return {
            "status": response.status_code,
            "body": response_body,
            "headers": {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                in {"content-type", "etag", "location", "retry-after", "x-request-id"}
            },
        }

    def _save_credentials(self, previous, response_body):
        pair = (
            response_body.get("credentials", {})
            if isinstance(response_body, dict)
            else {}
        )
        if not all(
            isinstance(pair.get(key), str) and pair[key]
            for key in ("clientId", "clientSecret")
        ):
            raise PolarisError(
                "Polaris 已更改服务凭据，但未返回有效的新凭据。请检查本地服务配置。"
            )
        updated = {
            **previous,
            "client_id": pair["clientId"],
            "client_secret": pair["clientSecret"],
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.credentials_path.parent,
                prefix=".credentials-",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(updated, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.credentials_path)
        except OSError as exc:
            # Upstream already changed the identity: retain the private new pair
            # for recovery if atomic publication fails, never delete the last copy.
            recovery = f"新凭据恢复文件：{temporary}。" if temporary is not None else ""
            raise PolarisError(
                "Polaris 已更改服务凭据，但本机配置更新失败。" + recovery
            ) from exc
        finally:
            with self._lock:
                self._token = ""
                self._expires = 0

    def overview(self):
        output = {
            "catalogs": [],
            "principals": [],
            "principal_roles": [],
            "status": "online",
            "errors": [],
        }
        for operation, field, upstream_key in (
            ("listCatalogs", "catalogs", "catalogs"),
            ("listPrincipals", "principals", "principals"),
            ("listPrincipalRoles", "principal_roles", "roles"),
        ):
            try:
                response = self.request("management", operation)
                if response["status"] == 200 and isinstance(response["body"], dict):
                    output[field] = response["body"].get(
                        upstream_key, response["body"].get(field, [])
                    )
                else:
                    output["errors"].append(f"{operation}: HTTP {response['status']}")
            except (
                PolarisError,
                httpx.HTTPError,
                OSError,
                ValueError,
                KeyError,
            ) as exc:
                output["errors"].append(str(exc))
        if output["errors"]:
            output["status"] = "degraded"
        return output
