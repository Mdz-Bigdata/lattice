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

"""The gateway implementation of the five Polaris semantic-model operations."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from webapi import app as app_module
from webapi import ossie_document
from webapi import semantic_models as sm
from webapi.semantic_models import SemanticModelError, SemanticModelService
from webapi.specs import SpecRegistry

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "tpcds_semantic_model.yaml"


class FakeCatalog:
    """Stateful stand-in for PolarisClient.request against generic tables."""

    def __init__(self):
        self.tables: dict[tuple[str, str, str], dict] = {}
        self.namespaces = {("lattice", "demo"), ("lattice", "sales\x1fnorth")}
        self.calls = []
        self.fail_create_once = False
        self.page_size = None

    def request(self, spec, operation, path_params=None, query=None, body=None, headers=None,
                token=None):
        self.calls.append((operation, copy.deepcopy(path_params), copy.deepcopy(query), token))
        assert spec == "catalog"
        key = (path_params["prefix"], path_params["namespace"])
        if operation == "loadNamespaceMetadata":
            return {"status": 200 if key in self.namespaces else 404, "body": {}}
        if key not in self.namespaces:
            return {"status": 404, "body": {"error": {"message": "no namespace", "type": "NoSuchNamespaceException", "code": 404}}}
        if operation == "createGenericTable":
            if self.fail_create_once:
                self.fail_create_once = False
                return {"status": 500, "body": {"error": {"message": "boom", "type": "X", "code": 500}}}
            table_key = (*key, body["name"])
            if table_key in self.tables:
                return {"status": 409, "body": {}}
            self.tables[table_key] = copy.deepcopy(body)
            return {"status": 200, "body": {"table": body}}
        if operation == "loadGenericTable":
            table = self.tables.get((*key, path_params["generic-table"]))
            return {"status": 200 if table else 404, "body": {"table": copy.deepcopy(table)} if table else {}}
        if operation == "dropGenericTable":
            return {"status": 204 if self.tables.pop((*key, path_params["generic-table"]), None) else 404, "body": None}
        if operation == "listGenericTables":
            names = sorted(name for (p, n, name) in self.tables if (p, n) == key)
            body = {}
            if self.page_size:
                start = int((query or {}).get("pageToken") or 0)
                window = names[start:start + self.page_size]
                if start + self.page_size < len(names):
                    body["next-page-token"] = str(start + self.page_size)
                names = window
            body["identifiers"] = [{"namespace": key[1].split("\x1f"), "name": name} for name in names]
            return {"status": 200, "body": body}
        raise AssertionError(operation)


@pytest.fixture
def document():
    return sm.document_from_yaml(EXAMPLE.read_text())


@pytest.fixture
def service():
    catalog = FakeCatalog()
    return SemanticModelService(catalog), catalog


def test_registry_marks_the_five_operations_as_gateway_implemented():
    registry = SpecRegistry(ROOT / "integrations" / "polaris" / "spec")
    catalog = {op["id"]: op for op in registry.public_specs()["specs"][1]["operations"]}
    gateway = {name for name, op in catalog.items() if op["implementation"] == "lattice-gateway"}
    assert gateway == sm.OPERATIONS
    assert catalog["createGenericTable"]["implementation"] == "upstream"


def test_full_lifecycle_matches_the_spec_contract(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "demo"}
    created = svc.handle("createSemanticModel", path, None, {"name": "tpcds_retail_model", "document": document})
    assert created["status"] == 200
    assert created["body"]["document"] == document
    version = created["body"]["entity-version"]
    assert isinstance(version, str) and 1 <= len(version) <= 255

    # The record is stored inside the real catalog namespace as a generic table.
    stored = catalog.tables[("lattice", "demo", sm.RECORD_PREFIX + "tpcds_retail_model")]
    assert stored["format"] == sm.FORMAT
    assert stored["properties"]["lattice.entity-version"] == version

    listed = svc.handle("listSemanticModels", path, {"pageSize": "50"}, None)
    assert listed["status"] == 200
    assert listed["body"] == {"identifiers": [{"namespace": ["demo"], "name": "tpcds_retail_model"}]}

    loaded = svc.handle("loadSemanticModel", {**path, "semantic-model-name": "tpcds_retail_model"}, None, None)
    assert loaded["status"] == 200 and loaded["body"]["entity-version"] == version

    duplicate = svc.handle("createSemanticModel", path, None, {"name": "tpcds_retail_model", "document": document})
    assert duplicate["status"] == 409
    assert duplicate["body"]["error"]["type"] == "AlreadyExistsException"

    stale = svc.handle(
        "updateSemanticModel",
        {**path, "semantic-model-name": "tpcds_retail_model"},
        None,
        {"document": document, "entity-version": "stale"},
    )
    assert stale["status"] == 409
    assert stale["body"]["error"]["type"] == "SemanticModelVersionMismatchException"

    changed = json.loads(document["semantic_model"])
    changed["description"] = "updated by test"
    new_document = {"version": document["version"], "semantic_model": json.dumps(changed)}
    updated = svc.handle(
        "updateSemanticModel",
        {**path, "semantic-model-name": "tpcds_retail_model"},
        None,
        {"document": new_document, "entity-version": version},
    )
    assert updated["status"] == 200
    assert updated["body"]["entity-version"] != version
    assert json.loads(updated["body"]["document"]["semantic_model"])["description"] == "updated by test"

    dropped = svc.handle("dropSemanticModel", {**path, "semantic-model-name": "tpcds_retail_model"}, None, None)
    assert dropped["status"] == 204 and dropped["body"] is None
    missing = svc.handle("loadSemanticModel", {**path, "semantic-model-name": "tpcds_retail_model"}, None, None)
    assert missing["status"] == 404
    assert missing["body"]["error"]["type"] == "NoSuchSemanticModelException"


def test_polaris_contract_example_with_released_ossie_version_is_accepted(service):
    """The console's default request body is the Polaris 1.7.0 contract example.

    That example declares the released Apache Ossie ``0.1.1`` while the bundled
    schema is pinned to ``0.2.0.dev0``; the gateway must accept it unchanged and
    store the document exactly as written.
    """
    svc, catalog = service
    registry = SpecRegistry(ROOT / "integrations" / "polaris" / "spec")
    example = registry.operation("catalog", "createSemanticModel")["request_example"]
    assert example["document"]["version"] == "0.1.1"
    assert ossie_document.accepted_versions() == (ossie_document.schema_version(), "0.1.1")
    assert ossie_document.schema_version() != "0.1.1"

    path = {"prefix": "lattice", "namespace": "demo"}
    created = svc.handle("createSemanticModel", path, None, example)
    assert created["status"] == 200, created["body"]
    assert created["body"]["document"] == example["document"]
    loaded = svc.handle(
        "loadSemanticModel", {**path, "semantic-model-name": example["name"]}, None, None
    )
    assert loaded["status"] == 200
    assert loaded["body"]["document"] == example["document"]

    unknown = svc.handle(
        "createSemanticModel", path, None,
        {"name": "other", "document": {**example["document"], "version": "0.1.0"}},
    )
    assert unknown["status"] == 400
    message = unknown["body"]["error"]["message"]
    assert "0.1.0" in message and "0.1.1" in message and ossie_document.schema_version() in message
    assert set(catalog.tables) == {("lattice", "demo", sm.RECORD_PREFIX + example["name"])}


def test_missing_namespace_and_invalid_documents_are_rejected(service, document):
    svc, _ = service
    absent = svc.handle("createSemanticModel", {"prefix": "lattice", "namespace": "nowhere"}, None, {"name": "m", "document": document})
    assert absent["status"] == 404 and absent["body"]["error"]["type"] == "NoSuchNamespaceException"
    listed = svc.handle("listSemanticModels", {"prefix": "lattice", "namespace": "nowhere"}, None, None)
    assert listed["status"] == 404

    bad_name = svc.handle("createSemanticModel", {"prefix": "lattice", "namespace": "demo"}, None, {"name": "bad name!", "document": document})
    assert bad_name["status"] == 400
    not_json = svc.handle(
        "createSemanticModel", {"prefix": "lattice", "namespace": "demo"}, None,
        {"name": "m", "document": {"version": "0.1.1", "semantic_model": "{not json"}},
    )
    assert not_json["status"] == 400 and "JSON" in not_json["body"]["error"]["message"]
    invalid_model = svc.handle(
        "createSemanticModel", {"prefix": "lattice", "namespace": "demo"}, None,
        {"name": "m", "document": {"version": "0.1.1", "semantic_model": json.dumps({"name": "x", "datasets": [{"name": "d", "fields": [{"name": "f", "expression": "unknown.col"}]}]})}},
    )
    assert invalid_model["status"] == 400
    assert "validation failed" in invalid_model["body"]["error"]["message"]


def test_multipart_namespaces_and_yaml_round_trip(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "sales%1Fnorth"}
    created = svc.handle("createSemanticModel", path, None, {"name": "north_model", "document": document})
    assert created["status"] == 200
    assert ("lattice", "sales\x1fnorth", sm.RECORD_PREFIX + "north_model") in catalog.tables
    listed = svc.handle("listSemanticModels", path, None, None)
    assert listed["body"]["identifiers"] == [{"namespace": ["sales", "north"], "name": "north_model"}]
    rendered = sm.document_yaml(created["body"]["document"])
    assert sm.document_from_yaml(rendered)["semantic_model"] == document["semantic_model"]


def test_failed_replacement_restores_the_previous_document(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "demo"}
    created = svc.handle("createSemanticModel", path, None, {"name": "keep", "document": document})
    version = created["body"]["entity-version"]
    catalog.fail_create_once = True
    changed = json.loads(document["semantic_model"])
    changed["description"] = "should not persist"
    failed = svc.handle(
        "updateSemanticModel", {**path, "semantic-model-name": "keep"}, None,
        {"document": {"version": document["version"], "semantic_model": json.dumps(changed)}, "entity-version": version},
    )
    assert failed["status"] == 502
    restored = svc.handle("loadSemanticModel", {**path, "semantic-model-name": "keep"}, None, None)
    assert restored["status"] == 200
    assert restored["body"]["entity-version"] == version
    assert restored["body"]["document"] == document


def test_foreign_generic_tables_are_never_treated_as_models(service, document):
    svc, catalog = service
    catalog.tables[("lattice", "demo", sm.RECORD_PREFIX + "ghost")] = {
        "name": sm.RECORD_PREFIX + "ghost", "format": "delta", "properties": {}
    }
    loaded = svc.handle("loadSemanticModel", {"prefix": "lattice", "namespace": "demo", "semantic-model-name": "ghost"}, None, None)
    assert loaded["status"] == 404
    dropped = svc.handle("dropSemanticModel", {"prefix": "lattice", "namespace": "demo", "semantic-model-name": "ghost"}, None, None)
    assert dropped["status"] == 404
    assert ("lattice", "demo", sm.RECORD_PREFIX + "ghost") in catalog.tables


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "RUNTIME", tmp_path / "web")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as value:
        catalog = FakeCatalog()
        app_module.app.state.semantic = SemanticModelService(catalog)
        yield value, catalog


def test_gateway_request_serves_the_operations_instead_of_501(client, document):
    http, _ = client
    response = http.post(
        "/api/polaris/request",
        json={
            "spec": "catalog",
            "operation_id": "createSemanticModel",
            "path_params": {"prefix": "lattice", "namespace": "demo"},
            "body": {"name": "via_console", "document": document},
        },
    )
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["status"] == 200
    assert envelope["body"]["document"] == document
    listing = http.get("/api/semantic-models?catalog=lattice&namespace=demo").json()
    assert [item["name"] for item in listing["items"]] == ["via_console"]
    assert listing["items"][0]["model_name"] == json.loads(document["semantic_model"])["name"]
    loaded = http.get("/api/semantic-models/load?catalog=lattice&namespace=demo&name=via_console").json()
    assert loaded["yaml"].startswith("version:")
    published = http.post(
        "/api/semantic-models/publish",
        json={"catalog": "lattice", "namespace": "demo", "name": "via_console", "yaml": loaded["yaml"], "entity_version": loaded["entity_version"]},
    )
    assert published.status_code == 200 and published.json()["action"] == "updated"
    conflict = http.post(
        "/api/semantic-models/delete",
        json={"catalog": "lattice", "namespace": "demo", "name": "via_console", "entity_version": loaded["entity_version"]},
    )
    assert conflict.status_code == 409
    deleted = http.post(
        "/api/semantic-models/delete",
        json={"catalog": "lattice", "namespace": "demo", "name": "via_console", "entity_version": published.json()["entity_version"]},
    )
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True


def test_rest_paths_require_a_valid_polaris_bearer_token(client, document, monkeypatch):
    http, catalog = client
    service = app_module.app.state.semantic
    seen = {}

    def authorize(authorization, prefix, namespace):
        seen["authorization"] = authorization
        if authorization != "Bearer caller-token":
            raise SemanticModelError(401, "NotAuthorizedException", "Polaris rejected the bearer token")
        return "caller-token"

    monkeypatch.setattr(service, "authorize", authorize)
    base = "/polaris/v1/lattice/namespaces/demo/semantic-models"
    anonymous = http.post(base, json={"name": "rest_model", "document": document})
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["type"] == "NotAuthorizedException"
    headers = {"Authorization": "Bearer caller-token"}
    created = http.post(base, json={"name": "rest_model", "document": document}, headers=headers)
    assert created.status_code == 200, created.text
    version = created.json()["entity-version"]
    assert created.headers["etag"] == f'"{version}"'
    assert http.get(base, headers=headers).json()["identifiers"] == [{"namespace": ["demo"], "name": "rest_model"}]
    assert http.get(base + "/rest_model", headers=headers).json()["entity-version"] == version
    updated = http.put(base + "/rest_model", json={"document": document, "entity-version": version}, headers=headers)
    assert updated.status_code == 200 and updated.json()["entity-version"] != version
    assert http.delete(base + "/rest_model", headers=headers).status_code == 204
    assert http.get(base + "/rest_model", headers=headers).status_code == 404
    assert http.post(base, content="{}", headers={**headers, "Content-Type": "text/plain"}).status_code == 415
    assert seen["authorization"] == "Bearer caller-token"
    # Every upstream call must carry the caller's own token, never the gateway's root identity.
    mutations = [call for call in catalog.calls if call[0] != "loadNamespaceMetadata"]
    assert mutations and all(call[3] == "caller-token" for call in mutations)
    assert created.headers["cache-control"] == "no-store"


def test_authorize_checks_the_caller_token_against_polaris(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"client_id": "root", "client_secret": "s", "realm": "LATTICE", "base_url": "http://127.0.0.1:8181"}))
    from webapi.polaris import PolarisClient

    registry = SpecRegistry(ROOT / "integrations" / "polaris" / "spec")
    client = PolarisClient(credentials, registry)
    requests = []

    def handler(request):
        requests.append(request)
        status = 200 if request.headers.get("Authorization") == "Bearer good" else 401
        return httpx.Response(status, json={})

    monkeypatch.setattr(client, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    service = SemanticModelService(client)
    service.authorize("Bearer good", "lattice", ["sales", "north"])
    assert requests[-1].url.raw_path == b"/api/catalog/v1/lattice/namespaces/sales%1Fnorth"
    assert requests[-1].headers["Polaris-Realm"] == "LATTICE"
    with pytest.raises(SemanticModelError) as info:
        service.authorize("Bearer bad", "lattice", ["demo"])
    assert info.value.status == 401
    with pytest.raises(SemanticModelError):
        service.authorize(None, "lattice", ["demo"])


def test_rest_routes_reject_an_oversized_body_before_parsing(client, monkeypatch):
    http, _ = client
    service = app_module.app.state.semantic
    monkeypatch.setattr(service, "authorize", lambda *args: "caller-token")
    base = "/polaris/v1/lattice/namespaces/demo/semantic-models"
    headers = {"Authorization": "Bearer caller-token", "Content-Type": "application/json"}
    huge = json.dumps({"name": "big", "document": {"version": "0.1.1", "semantic_model": "x" * (2 * 1024 * 1024)}})
    assert http.post(base, content=huge, headers=headers).status_code == 413


def test_rest_routes_authenticate_before_reading_the_body(client, monkeypatch):
    http, catalog = client
    service = app_module.app.state.semantic

    def refuse(*args):
        raise SemanticModelError(403, "ForbiddenException", "no access")

    monkeypatch.setattr(service, "authorize", refuse)
    response = http.post(
        "/polaris/v1/lattice/namespaces/demo/semantic-models",
        content="{not json",
        headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ForbiddenException"
    assert catalog.calls == []


def test_update_restores_the_document_when_the_rewrite_connection_fails(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "demo"}
    created = svc.handle("createSemanticModel", path, None, {"name": "keep", "document": document})
    version = created["body"]["entity-version"]
    calls = {"writes": 0}
    original = catalog.request

    def flaky(spec, operation, path_params=None, query=None, body=None, headers=None, token=None):
        if operation == "createGenericTable":
            calls["writes"] += 1
            if calls["writes"] == 1:  # the replacement write, after the drop succeeded
                raise httpx.ConnectError("connection reset")
        return original(spec, operation, path_params, query, body, headers, token)

    catalog.request = flaky
    changed = json.loads(document["semantic_model"])
    changed["description"] = "must not persist"
    failed = svc.handle(
        "updateSemanticModel", {**path, "semantic-model-name": "keep"}, None,
        {"document": {"version": document["version"], "semantic_model": json.dumps(changed)}, "entity-version": version},
    )
    assert failed["status"] == 503
    restored = svc.handle("loadSemanticModel", {**path, "semantic-model-name": "keep"}, None, None)
    assert restored["status"] == 200
    assert restored["body"]["entity-version"] == version
    assert restored["body"]["document"] == document


def test_update_reports_the_loss_when_the_document_cannot_be_restored(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "demo"}
    created = svc.handle("createSemanticModel", path, None, {"name": "doomed", "document": document})
    version = created["body"]["entity-version"]
    original = catalog.request

    def always_fail_writes(spec, operation, path_params=None, query=None, body=None, headers=None, token=None):
        if operation == "createGenericTable" and path_params.get("prefix") == "lattice" and body:
            if body["name"].endswith("doomed"):
                return {"status": 500, "body": {"error": {"message": "down", "type": "X", "code": 500}}}
        return original(spec, operation, path_params, query, body, headers, token)

    catalog.request = always_fail_writes
    failed = svc.handle(
        "updateSemanticModel", {**path, "semantic-model-name": "doomed"}, None,
        {"document": document, "entity-version": version},
    )
    assert failed["status"] == 500
    assert "无法恢复" in failed["body"]["error"]["message"]
    assert "createSemanticModel" in failed["body"]["error"]["message"]


def test_listing_pages_past_foreign_generic_tables(service, document):
    svc, catalog = service
    path = {"prefix": "lattice", "namespace": "demo"}
    for index in range(3):
        svc.handle("createSemanticModel", path, None, {"name": f"model_{index}", "document": document})
    # A namespace where plain generic tables outnumber the models and sort first.
    for index in range(5):
        catalog.tables[("lattice", "demo", f"aaa_other_{index}")] = {"name": f"aaa_other_{index}", "format": "delta", "properties": {}}
    catalog.page_size = 4  # force several upstream pages
    first = svc.handle("listSemanticModels", path, {"pageSize": "2"}, None)
    assert first["status"] == 200
    assert [i["name"] for i in first["body"]["identifiers"]] == ["model_0", "model_1"]
    cursor = first["body"]["next-page-token"]
    assert isinstance(cursor, str) and cursor
    second = svc.handle("listSemanticModels", path, {"pageToken": cursor, "pageSize": "2"}, None)
    assert [i["name"] for i in second["body"]["identifiers"]] == ["model_2"]
    assert "next-page-token" not in second["body"]
    assert svc.handle("listSemanticModels", path, {"pageToken": "not-a-cursor"}, None)["status"] == 400


def test_drop_refuses_a_stale_entity_version(service, document):
    svc, _ = service
    created = svc.create("lattice", ["demo"], {"name": "guarded", "document": document})
    version = created["body"]["entity-version"]
    with pytest.raises(SemanticModelError) as info:
        svc.drop("lattice", ["demo"], "guarded", entity_version="old")
    assert info.value.status == 409
    assert svc.drop("lattice", ["demo"], "guarded", entity_version=version)["status"] == 204


def test_stored_record_with_an_unusable_entity_version_is_not_served(service, document):
    svc, catalog = service
    svc.create("lattice", ["demo"], {"name": "tampered", "document": document})
    record = catalog.tables[("lattice", "demo", sm.RECORD_PREFIX + "tampered")]
    record["properties"]["lattice.entity-version"] = "bad\r\nInjected: header"
    loaded = svc.handle("loadSemanticModel", {"prefix": "lattice", "namespace": "demo", "semantic-model-name": "tampered"}, None, None)
    assert loaded["status"] == 502


def test_upstream_outage_is_reported_as_service_unavailable(service):
    svc, catalog = service

    def down(*args, **kwargs):
        raise httpx.ConnectError("polaris is down")

    catalog.request = down
    result = svc.handle("listSemanticModels", {"prefix": "lattice", "namespace": "demo"}, None, None)
    assert result["status"] == 503
    assert result["body"]["error"]["type"] == "ServiceUnavailableException"
