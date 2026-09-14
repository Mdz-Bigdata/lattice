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

"""Load the pinned upstream API contracts without network or external file access."""

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator

VERSION = "1.7.0"
SPEC_FILES = {
    "management": "polaris-management-service.yml",
    "catalog": "polaris-catalog-service.yaml",
}
METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
REFERENCE_PREFIX = "#/x-lattice-references/"


def _json_values(value: Any) -> Any:
    """YAML permits numeric response keys; JSON and OpenAPI require strings."""
    if isinstance(value, dict):
        return {str(key): _json_values(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_values(child) for child in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"Unsupported value in API specification: {type(value).__name__}")


def _pointer(document: Any, fragment: str) -> Any:
    if not fragment:
        return document
    if not fragment.startswith("/"):
        raise ValueError("API references must use JSON Pointer fragments")
    current = document
    try:
        for token in fragment[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            current = (
                current[int(token)] if isinstance(current, list) else current[token]
            )
        return current
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(f"Unresolved API reference fragment: {fragment}") from error


class _Bundler:
    """Rewrite each referenced object once; local references preserve schema cycles."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.documents: dict[Path, dict] = {}
        self.references: dict[str, Any] = {}

    def _read(self, filename: Path) -> dict:
        filename = filename.resolve()
        if not filename.is_relative_to(self.directory):
            raise ValueError(
                "API references must remain inside the specification directory"
            )
        if filename not in self.documents:
            try:
                document = yaml.safe_load(filename.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as error:
                raise ValueError(
                    f"Cannot read API specification: {filename.name}"
                ) from error
            if not isinstance(document, dict):
                raise ValueError("An API specification must be a mapping")
            self.documents[filename] = _json_values(document)
        return self.documents[filename]

    def _reference(self, reference: str, filename: Path) -> str:
        if not isinstance(reference, str):
            raise ValueError("API references must be strings")
        parsed = urlsplit(reference)
        relative = unquote(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or Path(relative).is_absolute()
        ):
            raise ValueError("Only local relative API references are allowed")
        target_file = (filename.parent / relative).resolve() if relative else filename
        if not target_file.is_relative_to(self.directory):
            raise ValueError(
                "API references must remain inside the specification directory"
            )
        fragment = unquote(parsed.fragment)
        identity = f"{target_file.relative_to(self.directory).as_posix()}#{fragment}"
        key = "ref_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if key not in self.references:
            target = _pointer(self._read(target_file), fragment)
            # Register before descent so recursive schemas terminate with a local ref.
            self.references[key] = None
            self.references[key] = self._rewrite(target, target_file)
        return REFERENCE_PREFIX + key

    def _rewrite(self, value: Any, filename: Path) -> Any:
        if isinstance(value, list):
            return [self._rewrite(child, filename) for child in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key == "$ref":
                result[key] = self._reference(child, filename)
            elif key == "discriminator" and isinstance(child, dict):
                result[key] = deepcopy(child)
                mapping = child.get("mapping", {})
                result[key]["mapping"] = {
                    name: (
                        self._reference(reference, filename)
                        if isinstance(reference, str)
                        and ("#" in reference or "/" in reference)
                        else reference
                    )
                    for name, reference in mapping.items()
                }
            else:
                result[key] = self._rewrite(child, filename)
        return result

    def bundle(self, filename: str) -> dict:
        source = (self.directory / filename).resolve()
        result = self._rewrite(self._read(source), source)
        if self.references:
            result["x-lattice-references"] = self.references
        return result


def _follow(value: Any, bundle: dict) -> Any:
    """Dereference the current object, leaving child objects for the caller."""
    seen = set()
    while isinstance(value, dict) and "$ref" in value:
        reference = value["$ref"]
        if reference in seen:
            return value
        seen.add(reference)
        target = _pointer(bundle, reference[1:])
        if not isinstance(target, dict):
            return target
        value = {
            **target,
            **{key: item for key, item in value.items() if key != "$ref"},
        }
    return value


def _expand(
    value: Any, bundle: dict, seen: frozenset = frozenset(), depth: int = 0
) -> Any:
    """Expand editor metadata while retaining references at recursive edges."""
    if depth > 40:
        return deepcopy(value)
    if isinstance(value, list):
        return [_expand(child, bundle, seen, depth + 1) for child in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if reference in seen:
            return deepcopy(value)
        return _expand(_follow(value, bundle), bundle, seen | {reference}, depth + 1)
    return {
        key: _expand(child, bundle, seen, depth + 1) for key, child in value.items()
    }


def _merge_example(base: Any, other: Any) -> Any:
    if isinstance(base, dict) and isinstance(other, dict):
        result = deepcopy(base)
        for key, value in other.items():
            result[key] = _merge_example(result[key], value) if key in result else value
        return result
    return other if other is not None else base


def _sample(
    schema: Any, bundle: dict, seen: frozenset = frozenset(), depth: int = 0
) -> Any:
    """Build a small request example from examples, required fields and alternatives."""
    if not isinstance(schema, dict) or depth > 24:
        return None
    if "$ref" in schema:
        reference = schema["$ref"]
        if reference in seen:
            return None
        return _sample(_follow(schema, bundle), bundle, seen | {reference}, depth + 1)
    for key in ("example", "default", "const"):
        if key in schema:
            candidate = schema[key]
            # Upstream PrimitiveType uses an example array for a scalar schema.
            if schema.get("type") in (
                "string",
                "integer",
                "number",
                "boolean",
            ) and isinstance(candidate, list):
                candidate = candidate[0] if candidate else None
            example_schema = {
                **schema,
                "x-lattice-references": bundle.get("x-lattice-references", {}),
            }
            if Draft202012Validator(example_schema).is_valid(candidate):
                return deepcopy(candidate)
    if schema.get("enum"):
        return deepcopy(schema["enum"][0])
    if schema.get("examples") and isinstance(schema["examples"], list):
        return deepcopy(schema["examples"][0])

    value = None
    for variant in schema.get("allOf", []):
        value = _merge_example(value, _sample(variant, bundle, seen, depth + 1))
    alternatives = schema.get("oneOf") or schema.get("anyOf") or []
    if alternatives:
        value = _merge_example(value, _sample(alternatives[0], bundle, seen, depth + 1))

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        own = {}
        properties = schema.get("properties", {})
        names = list(schema.get("required", list(properties)))
        # A required field can come from allOf while its constraint is local.
        for name in properties:
            if isinstance(value, dict) and name in value and name not in names:
                names.append(name)
        for name in names:
            child = properties.get(name, {})
            resolved_child = _follow(child, bundle)
            if isinstance(resolved_child, dict) and resolved_child.get(
                "readOnly", False
            ):
                continue
            own[name] = _sample(child, bundle, seen, depth + 1)
        value = _merge_example(value, own)
        discriminator = schema.get("discriminator", {})
        property_name = discriminator.get("propertyName")
        mapping = discriminator.get("mapping", {})
        selected_value = str(value.get(property_name)) if property_name else None
        if property_name and mapping and selected_value not in mapping:
            selected_value = next(iter(mapping))
            value[property_name] = selected_value
        selected = mapping.get(selected_value) if selected_value is not None else None
        if selected and selected.startswith("#/"):
            value = _merge_example(
                value, _sample({"$ref": selected}, bundle, seen, depth + 1)
            )
        return value
    if value is not None:
        return value
    if kind == "array":
        count = min(max(schema.get("minItems", 1), 1), 3)
        return [
            _sample(schema.get("items", {}), bundle, seen, depth + 1)
            for _ in range(count)
        ]
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        minimum = schema.get("minimum", 0)
        return minimum + 1 if schema.get("exclusiveMinimum") is True else minimum
    if kind == "string":
        return {
            "date": "2026-01-01",
            "date-time": "2026-01-01T00:00:00Z",
            "uri": "https://example.com",
        }.get(schema.get("format", ""), "example")
    return None


def _request_example(media: dict, bundle: dict) -> Any:
    if "example" in media:
        return deepcopy(media["example"])
    for example in media.get("examples", {}).values():
        example = _follow(example, bundle)
        if "value" in example:
            return deepcopy(example["value"])
    return _sample(media.get("schema", {}), bundle)


# Polaris 1.7.0 documents these operations but its adapter returns 501; the
# Lattice gateway serves them itself (webapi/semantic_models.py).
GATEWAY_OPERATIONS = frozenset(
    {
        "createSemanticModel",
        "listSemanticModels",
        "loadSemanticModel",
        "updateSemanticModel",
        "dropSemanticModel",
    }
)
GATEWAY_IMPLEMENTATION = "lattice-gateway"


def _default_tag(path: str) -> str:
    for marker, tag in (
        ("semantic-models", "Semantic Model API"),
        ("generic-tables", "Generic Table API"),
        ("policies", "Policy API"),
        ("principal-roles", "Principal Roles"),
        ("principals", "Principals"),
        ("catalog-roles", "Catalog Roles"),
        ("catalogs", "Catalogs"),
        ("namespaces", "Catalog API"),
    ):
        if marker in path:
            return tag
    return "Catalog API"


class SpecRegistry:
    """One inventory powers the API editor and the server's operation allowlist."""

    def __init__(self, spec_dir: Path | str):
        directory = Path(spec_dir).resolve()
        self._bundles = {}
        self._operations = {}
        self._specs = []
        for spec_id, filename in SPEC_FILES.items():
            bundle = _Bundler(directory).bundle(filename)
            self._bundles[spec_id] = bundle
            operations = self._collect_operations(bundle)
            self._operations[spec_id] = {
                operation["id"]: operation for operation in operations
            }
            self._specs.append(
                {
                    "id": spec_id,
                    "title": bundle.get("info", {}).get("title", spec_id),
                    "version": VERSION,
                    "operations": operations,
                }
            )

    @staticmethod
    def _collect_operations(bundle: dict) -> list[dict]:
        operations = []
        identifiers = set()
        for path, raw_item in bundle.get("paths", {}).items():
            item = _follow(raw_item, bundle)
            for method, raw_operation in item.items():
                if method not in METHODS:
                    continue
                source = _follow(raw_operation, bundle)
                operation_id = source.get("operationId")
                if not operation_id or operation_id in identifiers:
                    raise ValueError(
                        f"Missing or duplicate API operation ID: {operation_id}"
                    )
                identifiers.add(operation_id)
                parameters = {}
                for raw_parameter in item.get("parameters", []) + source.get(
                    "parameters", []
                ):
                    parameter = _expand(raw_parameter, bundle)
                    parameters[(parameter["name"], parameter["in"])] = parameter
                body = _follow(source.get("requestBody", {}), bundle)
                content = body.get("content", {})
                media_type = next(
                    (
                        name
                        for name in (
                            "application/json",
                            "application/x-www-form-urlencoded",
                        )
                        if name in content
                    ),
                    next(iter(content), "application/json"),
                )
                media = content.get(media_type, {})
                description = source.get("description", "").strip()
                operations.append(
                    {
                        "id": operation_id,
                        "method": method.upper(),
                        "path": path,
                        "summary": source.get("summary")
                        or description.split("\n")[0]
                        or operation_id,
                        "description": description,
                        "tag": (source.get("tags") or [_default_tag(path)])[0],
                        "parameters": list(parameters.values()),
                        "request_example": (
                            _request_example(media, bundle) if content else None
                        ),
                        "request_schema": _expand(media.get("schema", {}), bundle),
                        "request_required": bool(body.get("required", False)),
                        "media_type": media_type,
                        "deprecated": bool(source.get("deprecated", False)),
                        "implementation": (
                            GATEWAY_IMPLEMENTATION
                            if operation_id in GATEWAY_OPERATIONS
                            else "upstream"
                        ),
                    }
                )
        return operations

    def public_specs(self) -> dict:
        return {"specs": deepcopy(self._specs)}

    def operation(self, spec_id: str, operation_id: str) -> dict:
        try:
            return deepcopy(self._operations[spec_id][operation_id])
        except KeyError as error:
            raise ValueError(
                f"Unknown API operation: {spec_id}/{operation_id}"
            ) from error

    def bundled(self, spec_id: str) -> dict:
        try:
            return deepcopy(self._bundles[spec_id])
        except KeyError as error:
            raise ValueError(f"Unknown API specification: {spec_id}") from error
