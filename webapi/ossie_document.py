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

"""One Apache Ossie document check shared by every place Lattice validates a model.

The bundled ``core-spec/ossie-schema.json`` pins ``version`` to the in-development
spec (``0.2.0.dev0``).  The Polaris 1.7.0 semantic-model contract, however, was
written against the released Apache Ossie ``0.1.1``: its only request example
carries that version, and so do external clients that follow the Polaris
documentation.  ``0.2.0.dev0`` only adds to ``0.1.1`` (``datatype``, root
``dialects`` / ``vendors``, more dialect names, free-form vendor names; every
``required`` list is unchanged), so a released-version document is validated
against the current schema with only the version constant relaxed, and the
document itself is kept exactly as the caller wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validation import validate as validator

SCHEMA = Path(__file__).resolve().parent.parent / "core-spec" / "ossie-schema.json"
# Released Apache Ossie versions whose documents the bundled schema still accepts.
RELEASED_VERSIONS = ("0.1.1",)
WARNING_PREFIXES = ("[Reference] Warning:", "[SQL] Warning:")


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def schema_version(schema: dict[str, Any] | None = None) -> str:
    """The ``version`` constant the bundled schema is pinned to."""
    schema = load_schema() if schema is None else schema
    version = schema.get("properties", {}).get("version", {}).get("const")
    if not isinstance(version, str) or not version:
        raise ValueError("core-spec/ossie-schema.json does not pin a version constant")
    return version


def accepted_versions(schema: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Document versions a Lattice service accepts, current schema version first."""
    current = schema_version(schema)
    return (current, *(version for version in RELEASED_VERSIONS if version != current))


def check_document(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Run the Apache Ossie validator over a parsed document.

    Returns ``(failures, warnings)``.  A document declaring a released version is
    checked against the current schema with the version constant relaxed and gets
    a warning saying so; any other version fails with the accepted list.  Parsing
    and I/O errors propagate to the caller exactly as the validator raises them.
    """
    schema = load_schema()
    current = schema_version(schema)
    accepted = accepted_versions(schema)
    version = data.get("version")
    if isinstance(version, str) and version not in accepted:
        return (
            [
                "[Schema] version: expected one of "
                + ", ".join(accepted)
                + f" (got {version!r})"
            ],
            [],
        )
    checked = data if version == current else {**data, "version": current}
    failures = validator.validate_schema(checked, schema)
    warnings: list[str] = []
    if not failures:
        messages = (
            validator.validate_unique_names(checked)
            + validator.validate_references(checked)
            + validator.validate_sql(checked)
        )
        warnings = [str(m) for m in messages if str(m).startswith(WARNING_PREFIXES)]
        failures = [str(m) for m in messages if not str(m).startswith(WARNING_PREFIXES)]
    if not failures and version != current:
        warnings.append(
            f"[Schema] Warning: version {version} is the released Apache Ossie version; "
            f"validated under the current {current} schema, which is backward compatible"
        )
    return failures, warnings
