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

"""Verify source-spec coverage and the reference trust boundary."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from webapi.specs import SpecRegistry

SPEC_DIR = Path(__file__).resolve().parents[2] / "integrations/polaris/spec"


@pytest.fixture(scope="module")
def registry():
    return SpecRegistry(SPEC_DIR)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def resolve_pointer(document, reference):
    assert reference.startswith("#/")
    result = document
    for token in reference[2:].split("/"):
        result = result[token.replace("~1", "/").replace("~0", "~")]
    return result


def write_specs(directory, management):
    directory.mkdir(exist_ok=True)
    (directory / "polaris-management-service.yml").write_text(
        yaml.safe_dump(management)
    )
    (directory / "polaris-catalog-service.yaml").write_text(
        yaml.safe_dump({"openapi": "3.0.3", "info": {"title": "Catalog"}, "paths": {}})
    )


def test_exact_pinned_public_operation_coverage(registry):
    specs = {spec["id"]: spec for spec in registry.public_specs()["specs"]}
    assert len(specs["management"]["operations"]) == 33
    assert len(specs["catalog"]["operations"]) == 45
    assert all(spec["version"] == "1.7.0" for spec in specs.values())
    operations = specs["catalog"]["operations"]
    assert len([op for op in operations if "semantic-models" in op["path"]]) == 5
    assert not any("/plan" in op["path"] or "/tasks" in op["path"] for op in operations)
    assert len({(op["method"], op["path"]) for op in operations}) == 45


def test_oauth_form_and_semantic_model_examples(registry):
    oauth = registry.operation("catalog", "getToken")
    assert oauth["method"] == "POST"
    assert oauth["media_type"] == "application/x-www-form-urlencoded"
    assert oauth["request_required"] is True
    assert oauth["request_example"]["grant_type"] == "client_credentials"
    assert {"client_id", "client_secret"} <= oauth["request_example"].keys()
    model = registry.operation("catalog", "createSemanticModel")
    assert {p["name"] for p in model["parameters"]} == {"prefix", "namespace"}
    document = model["request_example"]["document"]
    assert document["version"] == "0.1.1"
    assert json.loads(document["semantic_model"])["name"]
    assert (
        "entity-version"
        in registry.operation("catalog", "updateSemanticModel")["request_schema"][
            "required"
        ]
    )
    table = registry.operation("catalog", "createTable")["request_example"]
    assert table["schema"]["fields"][0]["type"] == "long"


def test_bundles_are_json_serializable_and_all_references_resolve(registry):
    for spec_id in ("management", "catalog"):
        document = json.loads(json.dumps(registry.bundled(spec_id)))
        for node in walk(document):
            if "$ref" in node:
                resolve_pointer(document, node["$ref"])
            for reference in node.get("discriminator", {}).get("mapping", {}).values():
                if reference.startswith("#"):
                    resolve_pointer(document, reference)
        assert document["info"]["version"] == "0.0.1"
    assert (
        "/polaris/v1/{prefix}/namespaces/{namespace}/semantic-models"
        in registry.bundled("catalog")["paths"]
    )


def test_generated_request_examples_match_schema(registry):
    for specification in registry.public_specs()["specs"]:
        bundle = registry.bundled(specification["id"])
        for operation in specification["operations"]:
            if operation["request_example"] is None:
                continue
            schema = {
                **operation["request_schema"],
                "x-lattice-references": bundle.get("x-lattice-references", {}),
            }
            errors = list(
                Draft202012Validator(schema).iter_errors(operation["request_example"])
            )
            assert not errors, f"{operation['id']}: {errors[0].message}"


def test_unknown_operations_and_specs_fail_closed(registry):
    with pytest.raises(ValueError):
        registry.operation("management", "notAnOperation")
    with pytest.raises(ValueError):
        registry.operation("external", "getToken")
    with pytest.raises(ValueError):
        registry.bundled("external")


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.yaml#/item",
        "/etc/passwd",
        "https://example.com/spec.yaml#/item",
        "file:///etc/passwd",
        "%2e%2e/outside.yaml#/item",
        "//example.com/spec.yaml",
    ],
)
def test_unsafe_references_are_rejected_before_fetch(tmp_path, reference):
    write_specs(
        tmp_path,
        {"info": {"title": "Management"}, "paths": {"/bad": {"$ref": reference}}},
    )
    with pytest.raises(ValueError):
        SpecRegistry(tmp_path)


def test_symlink_reference_cannot_escape_spec_root(tmp_path):
    directory = tmp_path / "spec"
    outside = tmp_path / "outside.yaml"
    outside.write_text("item: {}")
    write_specs(
        directory,
        {"info": {"title": "M"}, "paths": {"/bad": {"$ref": "link.yaml#/item"}}},
    )
    (directory / "link.yaml").symlink_to(outside)
    with pytest.raises(ValueError):
        SpecRegistry(directory)


def test_recursive_refs_compositions_and_operation_parameter_override(tmp_path):
    write_specs(
        tmp_path,
        {
            "openapi": "3.0.3",
            "info": {"title": "M"},
            "paths": {
                "/items/{id}": {"$ref": "parts/routes.yaml#/paths/~1items~1{id}"},
            },
        },
    )
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts/routes.yaml").write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "/items/{id}": {
                        "parameters": [
                            {
                                "name": "id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "post": {
                            "operationId": "createItem",
                            "parameters": [
                                {
                                    "name": "id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "integer"},
                                }
                            ],
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "../models.yaml#/Node"}
                                    }
                                },
                            },
                            "responses": {
                                200: {
                                    "description": "OK",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "../models.yaml#/Node"}
                                        }
                                    },
                                }
                            },
                        },
                    }
                }
            }
        )
    )
    (tmp_path / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "Node": {
                    "allOf": [
                        {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string", "example": "sample"}
                            },
                        },
                        {
                            "type": "object",
                            "required": ["mode"],
                            "properties": {
                                "mode": {
                                    "oneOf": [{"enum": ["active"]}, {"type": "integer"}]
                                }
                            },
                        },
                    ],
                    "type": "object",
                    "properties": {
                        "children": {"type": "array", "items": {"$ref": "#/Node"}}
                    },
                }
            }
        )
    )
    local = SpecRegistry(tmp_path)
    operation = local.operation("management", "createItem")
    assert operation["parameters"] == [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ]
    assert operation["request_example"]["name"] == "sample"
    assert operation["request_example"]["mode"] == "active"
    bundle = json.loads(json.dumps(local.bundled("management")))
    assert (
        "200"
        in resolve_pointer(bundle, bundle["paths"]["/items/{id}"]["$ref"])["post"][
            "responses"
        ]
    )
    for node in walk(bundle):
        if "$ref" in node:
            resolve_pointer(bundle, node["$ref"])


def test_returned_metadata_cannot_mutate_registry(registry):
    operation = registry.operation("management", "listCatalogs")
    operation["method"] = "DELETE"
    assert registry.operation("management", "listCatalogs")["method"] == "GET"
