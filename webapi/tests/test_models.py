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

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from webapi import models
from webapi.models import ModelStoreError, PolarisModelStore


class FakeCatalog:
    """Stateful upstream adapter; exercises persistence across store instances."""

    def __init__(self):
        self.tables = {}
        self.calls = []
        self.namespace_exists = True
        self.pages = None
        self.fail_load = False

    def request(self, spec, operation, path_params=None, query=None, body=None):
        self.calls.append(
            (
                spec,
                operation,
                copy.deepcopy(path_params),
                copy.deepcopy(body),
                copy.deepcopy(query),
            )
        )
        assert spec == "catalog"
        assert path_params["prefix"] == "lattice"
        if operation == "loadNamespaceMetadata":
            return {"status": 200 if self.namespace_exists else 404, "body": {}}
        if operation == "createNamespace":
            assert body == {"namespace": ["demo"]}
            self.namespace_exists = True
            return {"status": 200, "body": body}
        assert path_params["namespace"] == "demo"
        if operation == "createGenericTable":
            if body["name"] in self.tables:
                return {"status": 409, "body": {}}
            self.tables[body["name"]] = copy.deepcopy(body)
            return {"status": 200, "body": {"table": body}}
        if operation == "loadGenericTable":
            if self.fail_load:
                raise httpx.ConnectError("readback unavailable")
            table = self.tables.get(path_params["generic-table"])
            return {
                "status": 200 if table else 404,
                "body": {"table": copy.deepcopy(table)},
            }
        if operation == "listGenericTables":
            if not self.namespace_exists:
                return {"status": 404, "body": {}}
            if self.pages is not None:
                return {"status": 200, "body": self.pages.pop(0)}
            return {
                "status": 200,
                "body": {
                    "identifiers": [
                        {"name": name, "namespace": ["demo"]} for name in self.tables
                    ]
                },
            }
        if operation == "dropGenericTable":
            del self.tables[path_params["generic-table"]]
            return {"status": 204, "body": None}
        raise AssertionError(operation)


@pytest.fixture
def document():
    return (
        Path(__file__).resolve().parents[2] / "examples" / "tpcds_semantic_model.yaml"
    ).read_text()


@pytest.fixture
def catalog():
    return FakeCatalog()


def test_create_roundtrip_list_delete_through_actual_generic_api_contract(
    catalog, document
):
    store = PolarisModelStore(catalog)
    saved = store.create("销售语义模型", document)
    assert saved["yaml"] == document
    assert saved["sha256"] == hashlib.sha256(document.encode()).hexdigest()
    assert saved["size_bytes"] == len(document.encode())
    assert saved["storage"] == "Polaris Generic Table"
    assert models.MODEL_ID.fullmatch(saved["id"])
    assert catalog.tables[saved["id"]]["format"] == "lattice"
    assert catalog.tables[saved["id"]]["properties"]["lattice.document-yaml"] == document
    reopened = PolarisModelStore(catalog)
    assert reopened.get(saved["id"]) == saved
    listed = reopened.list()
    assert listed["items"][0]["id"] == saved["id"]
    assert "yaml" not in listed["items"][0]
    assert reopened.delete(saved["id"], saved["sha256"])["deleted"]
    assert reopened.list()["items"] == []
    with pytest.raises(ModelStoreError) as missing:
        reopened.get(saved["id"])
    assert missing.value.status_code == 404
    assert not any("SemanticModel" in call[1] for call in catalog.calls)


def test_repeat_save_creates_immutable_versions_instead_of_overwriting(
    catalog, document
):
    store = PolarisModelStore(catalog)
    first, second = store.create("same name", document), store.create(
        "same name", document
    )
    assert first["id"] != second["id"]
    assert len(catalog.tables) == 2
    assert first["sha256"] == second["sha256"]


def test_namespace_created_only_on_explicit_save(catalog, document):
    catalog.namespace_exists = False
    store = PolarisModelStore(catalog)
    assert store.list()["items"] == []
    assert not any(call[1] == "createNamespace" for call in catalog.calls)
    store.create("new", document)
    assert catalog.namespace_exists


@pytest.mark.parametrize(
    "name", ["", "  ", "x" * 121, "bad\nname", "bad\x7fname", 1, None]
)
def test_invalid_names_fail_before_network(catalog, document, name):
    with pytest.raises(ModelStoreError) as failure:
        PolarisModelStore(catalog).create(name, document)
    assert failure.value.status_code == 400
    assert catalog.calls == []


@pytest.mark.parametrize(
    "document",
    [
        "null",
        "[]",
        "a: [",
        "version: a\nversion: b",
        "version: wrong\nsemantic_model: []",
        "&a [*a]",
    ],
)
def test_invalid_yaml_or_schema_never_reaches_polaris(catalog, document):
    with pytest.raises(ModelStoreError) as failure:
        PolarisModelStore(catalog).create("invalid", document)
    assert failure.value.status_code == 422
    assert catalog.calls == []


@pytest.mark.parametrize(
    "document", ["#" + "a" * models.MAX_DOCUMENT_BYTES, "#" + "中" * 100000]
)
def test_size_cap_is_utf8_bytes_not_character_count(catalog, document):
    with pytest.raises(ModelStoreError) as failure:
        PolarisModelStore(catalog).create("oversized", document)
    assert failure.value.status_code == 413
    assert catalog.calls == []


@pytest.mark.parametrize(
    "model_id",
    ["..", "foreign_table", "lattice_model_../../x", "lattice_model_" + "g" * 32, None],
)
def test_invalid_record_ids_fail_before_any_network(catalog, model_id):
    with pytest.raises(ModelStoreError):
        PolarisModelStore(catalog).get(model_id)
    with pytest.raises(ModelStoreError):
        PolarisModelStore(catalog).delete(model_id, "a" * 64)
    assert catalog.calls == []


@pytest.mark.parametrize("change", ["format", "owner", "storage", "name"])
def test_foreign_or_mismatched_records_cannot_be_read_or_deleted(
    catalog, document, change
):
    store = PolarisModelStore(catalog)
    saved = store.create("owned", document)
    table = catalog.tables[saved["id"]]
    if change in {"format", "name"}:
        table[change] = "foreign"
    else:
        table["properties"][
            "lattice.owner" if change == "owner" else "lattice.model-storage"
        ] = "foreign"
    before = len(catalog.calls)
    with pytest.raises(ModelStoreError) as failure:
        store.delete(saved["id"], saved["sha256"])
    assert failure.value.status_code == 409
    assert not any(call[1] == "dropGenericTable" for call in catalog.calls[before:])
    assert saved["id"] in catalog.tables
    listed = store.list()
    assert listed["items"] == [] and listed["warnings"]


def test_changed_payload_and_wrong_delete_digest_are_rejected(catalog, document):
    store = PolarisModelStore(catalog)
    saved = store.create("owned", document)
    with pytest.raises(ModelStoreError) as failure:
        store.delete(saved["id"], "a" * 64)
    assert failure.value.status_code == 409
    catalog.tables[saved["id"]]["properties"]["lattice.document-yaml"] += "\n# changed"
    with pytest.raises(ModelStoreError) as failure:
        store.delete(saved["id"], saved["sha256"])
    assert failure.value.status_code == 409
    assert not any(call[1] == "dropGenericTable" for call in catalog.calls)


def test_create_conflict_does_not_touch_existing_record(catalog, document, monkeypatch):
    monkeypatch.setattr(models.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    model_id = "lattice_model_" + "a" * 32
    original = {
        "name": model_id,
        "format": "foreign",
        "properties": {"private": "keep"},
    }
    catalog.tables[model_id] = copy.deepcopy(original)
    with pytest.raises(ModelStoreError) as failure:
        PolarisModelStore(catalog).create("new", document)
    assert failure.value.status_code == 409
    assert catalog.tables[model_id] == original
    assert not any(call[1] == "dropGenericTable" for call in catalog.calls)


def test_successful_create_followed_by_readback_error_is_not_reported_as_unsaved(
    catalog, document
):
    catalog.fail_load = True
    with pytest.raises(ModelStoreError, match="已创建模型记录") as failure:
        PolarisModelStore(catalog).create("saved but unreadable", document)
    assert failure.value.status_code == 502
    assert len(catalog.tables) == 1


def test_pagination_deduplicates_and_follows_server_token(catalog, document):
    store = PolarisModelStore(catalog)
    first, second = store.create("one", document), store.create("two", document)
    catalog.pages = [
        {"identifiers": [{"name": first["id"]}], "next-page-token": "next"},
        {
            "identifiers": [{"name": first["id"]}, {"name": second["id"]}],
            "next-page-token": None,
        },
    ]
    listed = store.list()
    assert len(listed["items"]) == 2
    pages = [call for call in catalog.calls if call[1] == "listGenericTables"]
    assert pages[-1][4]["pageToken"] == "next"


def test_repeating_page_token_is_explicit_error(catalog):
    catalog.pages = [
        {"identifiers": [], "next-page-token": "repeat"},
        {"identifiers": [], "next-page-token": "repeat"},
    ]
    with pytest.raises(ModelStoreError, match="分页令牌"):
        PolarisModelStore(catalog).list()


def test_list_cap_is_visible_and_does_not_load_unrelated_tables(
    catalog, document, monkeypatch
):
    store = PolarisModelStore(catalog)
    store.create("one", document)
    store.create("two", document)
    monkeypatch.setattr(models, "MAX_LIST_MODELS", 1)
    listed = store.list()
    assert len(listed["items"]) == 1 and listed["truncated"]


@pytest.mark.parametrize(
    "document",
    ["invalid: 2026-13-01", "invalid: " + "9" * 5000, "x: " + "1:" * 175 + "1.0"],
)
def test_yaml_scalar_constructor_failures_are_validation_errors(catalog, document):
    with pytest.raises(ModelStoreError) as failure:
        PolarisModelStore(catalog).create("invalid", document)
    assert failure.value.status_code == 422
    assert catalog.calls == []


def test_ambiguous_create_transport_failure_identifies_record_and_avoids_retry(
    catalog, document, monkeypatch
):
    original = catalog.request

    def request(spec, operation, **kwargs):
        response = original(spec, operation, **kwargs)
        if operation == "createGenericTable":
            raise httpx.ReadTimeout("response lost after upstream commit")
        return response

    monkeypatch.setattr(catalog, "request", request)
    with pytest.raises(ModelStoreError, match="结果尚未确认") as failure:
        PolarisModelStore(catalog).create("ambiguous", document)
    assert failure.value.status_code == 502
    assert len(catalog.tables) == 1
    assert next(iter(catalog.tables)) in str(failure.value)


def test_compact_alias_amplification_rejected_before_expansion(catalog):
    document = "version: 0.2.0.dev0\na0: &a0 [leaf]\n"
    document += "\n".join(
        f"a{n}: &a{n} [" + ", ".join([f"*a{n - 1}"] * 6) + "]" for n in range(1, 10)
    )
    document += "\nsemantic_model: [*a9]\n"
    with pytest.raises(ModelStoreError, match="别名") as failure:
        PolarisModelStore(catalog).create("aliases", document)
    assert failure.value.status_code == 422
    assert len(str(failure.value)) < 200
    assert catalog.calls == []


@pytest.mark.parametrize("token", [0, False, [], {}])
def test_invalid_falsy_pagination_tokens_are_rejected(catalog, token):
    catalog.pages = [{"identifiers": [], "next-page-token": token}]
    with pytest.raises(ModelStoreError, match="分页令牌"):
        PolarisModelStore(catalog).list()


def test_surrogate_name_rejected_before_network(catalog, document):
    with pytest.raises(ModelStoreError, match="Unicode"):
        PolarisModelStore(catalog).create("\ud800", document)
    assert catalog.calls == []


def test_deep_or_excessive_node_documents_are_bounded(catalog):
    for document in [
        "x: " + "[" * 60 + "x" + "]" * 60,
        "x: [" + ",".join(["0"] * 21000) + "]",
    ]:
        with pytest.raises(ModelStoreError, match="结构"):
            PolarisModelStore(catalog).create("large structure", document)
    assert catalog.calls == []
