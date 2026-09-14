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

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi.query import QueryStore, MONTHLY
from webapi.polaris import PolarisClient, PolarisError
from webapi.specs import SpecRegistry

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    return QueryStore(tmp_path / "queries")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        monkeypatch.setattr(
            app_module.app.state.polaris,
            "status",
            lambda: {"status": "online", "version": "1.7.0"},
        )
        yield value


def test_sample_query_aggregates_actual_rows_and_persists_history(store):
    result = store.query(question="月度销售额趋势")
    assert len(result["rows"]) == 11
    assert [row[1] for row in result["rows"]] == MONTHLY
    assert result["source"] == "本地示例数据 · DuckDB"
    assert sum(table["rows"] for table in store.metadata()) == 3390
    reopened = QueryStore(store.db.parent)
    assert reopened.history()[0]["id"] == result["id"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t_lattice_orders",
        "SELECT 1; SELECT 2",
        "COPY t_lattice_orders TO '/tmp/leak.csv'",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM information_schema.tables",
        "ATTACH '/tmp/out.db'",
        "SELECT * FROM sqlite_scan('/tmp/private.db', 'secrets')",
        "SELECT * FROM not_a_table",
        "SELECT * INTO other FROM t_lattice_orders",
    ],
)
def test_sql_rejects_mutations_files_extensions_and_other_tables(store, sql):
    with pytest.raises(ValueError):
        store.query(sql=sql)
    assert store.history() == []


def test_query_row_limit_and_ctes(store):
    response = store.query(
        sql="WITH orders AS (SELECT * FROM t_lattice_orders) SELECT * FROM orders"
    )
    assert len(response["rows"]) == 1000
    assert response["truncated"] is True


def test_complex_sql_values_are_json_safe_in_response_and_history(store):
    response = store.query(sql="""SELECT [DATE '2026-01-01'] AS dates,
        {'amount': 1.25} AS detail, uuid() AS id,
        [CAST('NaN' AS DOUBLE)] AS nonfinite, INTERVAL '1 day' AS duration""")
    row = response["rows"][0]
    assert row[0] == ["2026-01-01"]
    assert row[1] == {"amount": 1.25}
    assert isinstance(row[2], str)
    assert row[3] == [None]
    assert isinstance(row[4], str)
    assert store.history()[0]["rows"] == response["rows"]


def test_unknown_question_is_explicit_not_fake_success(store):
    with pytest.raises(ValueError, match="本地规则"):
        store.query(question="告诉我客户的信用卡密码")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_name FROM t_lattice_customers",
        "SELECT customer_name, region FROM t_lattice_customers",
        "SELECT 42 AS answer",
        "SELECT 'x' AS category, NULL AS value",
    ],
)
def test_nonnumeric_or_single_column_results_are_tables(store, sql):
    assert store.query(sql=sql)["chart"] == {
        "dimension": "",
        "metric": "",
        "type": "table",
    }


def test_numeric_metric_can_precede_a_trailing_text_column(store):
    result = store.query(
        sql="SELECT customer_name, customer_id, region FROM t_lattice_customers"
    )
    assert result["chart"]["metric"] == "customer_id"


def test_api_query_and_model_validation(client):
    boot = client.get("/api/bootstrap").json()
    assert len(boot["tables"]) == 6
    assert client.post("/api/validate", json={"yaml": boot["model_yaml"]}).json()[
        "valid"
    ]
    response = client.post("/api/query", json={"question": "商品类别销售额"})
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 4
    assert client.get("/api/history").json()["items"][0]["id"] == response.json()["id"]
    assert (
        client.post(
            "/api/query", json={"question": "月度趋势", "sql": "SELECT 1"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Host": "evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://127.0.0.1:9999"},
    ],
)
def test_browser_boundary_rejects_other_origins(client, headers):
    assert (
        client.post("/api/query", json={"sql": "SELECT 1"}, headers=headers).status_code
        == 403
    )


def test_browser_boundary_requires_json_and_limits_size(client):
    assert (
        client.post(
            "/api/query", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/api/query",
            content=b"x" * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert client.get("/api/not-a-route").status_code == 404


@pytest.mark.parametrize("document", ["null", "[]", "version: 1\nversion: 2", "a: ["])
def test_validation_returns_invalid_without_traceback(client, document):
    response = client.post("/api/validate", json={"yaml": document})
    assert response.status_code == 200
    assert response.json()["valid"] is False


@pytest.mark.parametrize(
    "document",
    [
        'version: 0.2.0.dev0\nsemantic_model: "Warning: should be array"',
        'version: 0.2.0.dev0\nsemantic_model: []\n"Warning: unexpected": 1',
    ],
)
def test_user_warning_text_never_downgrades_schema_errors(client, document):
    result = client.post("/api/validate", json={"yaml": document}).json()
    assert result["valid"] is False
    assert result["errors"]


@pytest.fixture
def polaris(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "client_id": "private-root",
                "client_secret": "private-secret",
                "realm": "LATTICE",
                "base_url": "http://127.0.0.1:8181",
            }
        )
    )
    registry = SpecRegistry(ROOT / "integrations" / "polaris" / "spec")
    service = PolarisClient(credentials, registry)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/oauth/tokens"):
            return httpx.Response(
                200, json={"access_token": "internal-root-token", "expires_in": 300}
            )
        if request.method == "HEAD":
            return httpx.Response(204)
        return httpx.Response(200, json={"catalogs": []})

    monkeypatch.setattr(
        service, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return service, requests


def test_polaris_proxy_uses_fixed_route_and_server_identity(polaris):
    service, requests = polaris
    result = service.request("management", "getCatalog", {"catalogName": "warehouse"})
    assert result["status"] == 200
    assert requests[-1].url.path == "/api/management/v1/catalogs/warehouse"
    assert requests[-1].headers["Authorization"] == "Bearer internal-root-token"
    assert "internal-root-token" not in json.dumps(result)
    assert requests[-1].headers["Polaris-Realm"] == "LATTICE"


@pytest.mark.parametrize(
    "params", [{"catalogName": ".."}, {"catalogName": "."}, {"catalogName": "x\ny"}]
)
def test_polaris_rejects_path_traversal(polaris, params):
    service, requests = polaris
    with pytest.raises(ValueError):
        service.request("management", "getCatalog", params)
    assert requests == []


def test_polaris_unknown_routes_and_headers_fail_before_network(polaris):
    service, requests = polaris
    with pytest.raises(ValueError):
        service.request("management", "../../private")
    with pytest.raises(ValueError):
        service.request(
            "management", "listCatalogs", headers={"Authorization": "Bearer other"}
        )
    assert requests == []


@pytest.mark.parametrize("namespace", ["accounting\u001ftax", "accounting%1Ftax"])
def test_polaris_multipart_namespace_is_encoded_once(polaris, namespace):
    service, requests = polaris
    service.request(
        "catalog", "loadNamespaceMetadata", {"prefix": "lattice", "namespace": namespace}
    )
    assert b"accounting%1Ftax" in requests[-1].url.raw_path
    assert b"%251F" not in requests[-1].url.raw_path


def test_oauth_workbench_never_substitutes_root_credentials(polaris):
    service, requests = polaris
    service.request(
        "catalog",
        "getToken",
        body={
            "grant_type": "client_credentials",
            "client_id": "explicit-user",
            "client_secret": "explicit-secret",
        },
    )
    assert len(requests) == 1
    assert (
        requests[0]
        .headers["Content-Type"]
        .startswith("application/x-www-form-urlencoded")
    )
    assert b"explicit-user" in requests[0].content
    assert b"private-root" not in requests[0].content
    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize("operation", ["rotateCredentials", "resetCredentials"])
def test_gateway_identity_rotation_persists_private_credentials(
    polaris, monkeypatch, operation
):
    service, requests = polaris

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/oauth/tokens"):
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 300}
            )
        return httpx.Response(
            200,
            json={
                "principal": {"name": "root", "clientId": "new-id"},
                "credentials": {"clientId": "new-id", "clientSecret": "new-secret"},
            },
        )

    monkeypatch.setattr(
        service, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = service.request(
        "management", operation, {"principalName": "root"}, body={}
    )
    stored = json.loads(service.credentials_path.read_text())
    assert stored["client_id"] == "new-id" and stored["client_secret"] == "new-secret"
    assert stored["realm"] == "LATTICE"
    assert service.credentials_path.stat().st_mode & 0o777 == 0o600
    assert "new-secret" not in json.dumps(result)
    assert not service._token


def test_other_principal_reset_does_not_replace_gateway_identity(polaris, monkeypatch):
    service, requests = polaris
    original = service.credentials_path.read_bytes()

    def handler(request):
        if request.url.path.endswith("/oauth/tokens"):
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 300}
            )
        return httpx.Response(
            200,
            json={
                "principal": {"name": "other", "clientId": "new-id"},
                "credentials": {"clientId": "new-id", "clientSecret": "new-secret"},
            },
        )

    monkeypatch.setattr(
        service, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = service.request(
        "management", "resetCredentials", {"principalName": "other"}, body={}
    )
    assert result["body"]["credentials"]["clientSecret"] == "new-secret"
    assert service.credentials_path.read_bytes() == original


def test_failed_rotation_publication_keeps_private_recovery_copy(polaris, monkeypatch):
    service, _ = polaris
    original = service.credentials_path.read_bytes()
    monkeypatch.setattr(
        Path, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("denied"))
    )
    with pytest.raises(PolarisError, match="恢复文件"):
        service._save_credentials(
            json.loads(original),
            {"credentials": {"clientId": "new-id", "clientSecret": "new-secret"}},
        )
    recoveries = list(service.credentials_path.parent.glob(".credentials-*"))
    assert len(recoveries) == 1
    assert json.loads(recoveries[0].read_text())["client_secret"] == "new-secret"
    assert recoveries[0].stat().st_mode & 0o777 == 0o600
    assert service.credentials_path.read_bytes() == original


def test_validation_rejects_yaml_aliases_without_amplifying_errors(client):
    document = 'a: &a ["x", "x"]\nb: &b [*a, *a]\nc: [*b, *b]'
    response = client.post("/api/validate", json={"yaml": document})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert len(response.content) < 2000
