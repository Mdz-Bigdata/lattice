#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# See the License for the specific language governing permissions
# and limitations under the License.

"""Exercise the running WebUI gateway and real, local Polaris lifecycle APIs.

Run with .runtime/envs/core/bin/python scripts/verify-webui.py. Disposable
objects use a unique prefix and are removed even after a failed check.
Reports contain operation names/statuses, never credentials or response bodies.
"""

from __future__ import annotations

import argparse
import json
import secrets
from urllib.parse import quote
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


class VerificationError(RuntimeError):
    """A real service response did not meet the expected contract."""


class Verification:
    def __init__(self, base_url: str):
        self.client = httpx.Client(
            base_url=base_url, timeout=45, trust_env=False, follow_redirects=False
        )
        self.name = "verify_" + secrets.token_hex(6)
        self.calls: list[dict[str, Any]] = []
        self.direct_calls: list[dict[str, Any]] = []
        self.assertions: list[str] = []
        self.cleanups: list[tuple[str, str, dict[str, Any]]] = []
        self.cleanup_errors: list[str] = []
        self.advertised: set[tuple[str, str]] = set()

    def check(self, condition: Any, description: str) -> None:
        if not condition:
            raise VerificationError(description)
        self.assertions.append(description)

    def request(
        self,
        spec: str,
        operation: str,
        *,
        expect: tuple[int, ...] = (200, 201, 204),
        cleanup: bool = False,
        **kwargs: Any,
    ) -> Any:
        response = self.client.post(
            "/api/polaris/request",
            json={"spec": spec, "operation_id": operation, **kwargs},
        )
        if response.status_code != 200:
            raise VerificationError(f"Gateway {operation}: HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise VerificationError(
                f"Gateway {operation}: malformed JSON response"
            ) from exc
        if not isinstance(result, dict) or type(result.get("status")) is not int:
            raise VerificationError(f"Gateway {operation}: malformed response envelope")
        status = result["status"]
        self.calls.append(
            {
                "spec": spec,
                "operation": operation,
                "status": status,
                "expected": list(expect),
                "cleanup": cleanup,
            }
        )
        if status not in expect:
            response_body = result.get("body")
            error_body = (
                response_body.get("error") if isinstance(response_body, dict) else None
            )
            error_type = (
                error_body.get("type", "") if isinstance(error_body, dict) else ""
            )
            raise VerificationError(f"{operation}: HTTP {status} {error_type}")
        return result.get("body")

    def later(self, spec: str, operation: str, **kwargs: Any) -> None:
        self.cleanups.append((spec, operation, kwargs))

    def cleanup(self) -> None:
        for spec, operation, kwargs in reversed(self.cleanups):
            try:
                self.request(
                    spec, operation, cleanup=True, expect=(200, 204, 404), **kwargs
                )
            except (
                VerificationError,
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                self.cleanup_errors.append(str(exc))

    def bootstrap(self) -> dict[str, Any]:
        health = self.client.get("/api/health")
        health.raise_for_status()
        self.check(
            health.json().get("application") == "lattice-webui", "WebUI service identity"
        )
        specs = self.client.get("/api/polaris/specs")
        specs.raise_for_status()
        self.advertised = {
            (spec["id"], op["id"])
            for spec in specs.json()["specs"]
            for op in spec["operations"]
        }
        self.check(
            len(self.advertised) == 78, "Pinned Polaris exposes all 78 operations"
        )
        response = self.client.get("/api/bootstrap")
        response.raise_for_status()
        bootstrap = response.json()
        self.check(
            bootstrap["polaris"]["status"] == "online",
            "Polaris authenticated connectivity",
        )
        query = self.client.post("/api/query", json={"question": "月度销售额趋势"})
        query.raise_for_status()
        self.check(
            len(query.json()["rows"]) == 11,
            "Monthly sales query returns 11 real DuckDB aggregates",
        )
        validate = self.client.post(
            "/api/validate", json={"yaml": bootstrap["model_yaml"]}
        )
        validate.raise_for_status()
        self.check(
            validate.json()["valid"], "Generated Apache Ossie sample passes existing validator"
        )
        return bootstrap

    def management(self) -> None:
        def m(operation: str, **kwargs: Any) -> Any:
            return self.request("management", operation, **kwargs)

        catalog = self.name
        cp = {"catalogName": catalog}
        storage = {
            "storageType": "S3",
            "allowedLocations": [f"s3://lattice-warehouse/{catalog}"],
            "endpoint": "http://127.0.0.1:19000",
            "endpointInternal": "http://127.0.0.1:19000",
            "pathStyleAccess": True,
            "region": "us-east-1",
        }
        m(
            "createCatalog",
            body={
                "catalog": {
                    "name": catalog,
                    "type": "INTERNAL",
                    "properties": {
                        "default-base-location": f"s3://lattice-warehouse/{catalog}",
                        "polaris.config.drop-with-purge.enabled": "true",
                        "polaris.config.purge-view-metadata-on-drop": "false",
                    },
                    "storageConfigInfo": storage,
                }
            },
        )
        self.later("management", "deleteCatalog", path_params=cp)
        loaded = m("getCatalog", path_params=cp)
        self.check(loaded["name"] == catalog, "Catalog create/load")
        m(
            "updateCatalog",
            path_params=cp,
            body={
                "currentEntityVersion": loaded["entityVersion"],
                "properties": {**loaded["properties"], "verification": "updated"},
            },
        )
        self.check(
            m("getCatalog", path_params=cp)["properties"]["verification"] == "updated",
            "Catalog update persists",
        )
        m("listCatalogs")
        m(
            "addGrantToCatalogRole",
            path_params={**cp, "catalogRoleName": "catalog_admin"},
            body={"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}},
        )

        pp = {"principalName": self.name}
        m(
            "createPrincipal",
            body={
                "principal": {"name": self.name, "properties": {}},
                "credentialRotationRequired": False,
            },
        )
        self.later("management", "deletePrincipal", path_params=pp)
        principal = m("getPrincipal", path_params=pp)
        m(
            "updatePrincipal",
            path_params=pp,
            body={
                "currentEntityVersion": principal["entityVersion"],
                "properties": {"verification": "updated"},
            },
        )
        m("listPrincipals")
        m("rotateCredentials", path_params=pp, expect=(403,))
        self.check(
            True, "Polaris prevents root from rotating another principal identity"
        )
        credential = m("resetCredentials", path_params=pp, body={})["credentials"]
        token = self.request(
            "catalog",
            "getToken",
            body={
                "grant_type": "client_credentials",
                "client_id": credential["clientId"],
                "client_secret": credential["clientSecret"],
                "scope": "PRINCIPAL_ROLE:ALL",
            },
        )
        self.check(
            bool(token.get("access_token")),
            "OAuth authenticates disposable principal credentials",
        )
        rotated = self.client.post(
            f"http://127.0.0.1:8181/api/management/v1/principals/{self.name}/rotate",
            headers={
                "Polaris-Realm": "LATTICE",
                "Authorization": "Bearer " + token["access_token"],
            },
            json={},
        )
        self.direct_calls.append(
            {
                "operation": "rotateCredentials",
                "status": rotated.status_code,
                "identity": "disposable principal",
            }
        )
        self.check(
            rotated.status_code == 200,
            "Direct Polaris self-rotation with disposable principal identity",
        )
        self.check(
            rotated.json()["credentials"]["clientSecret"] != credential["clientSecret"],
            "Self-rotation changes secret",
        )

        prp = {"principalRoleName": self.name}
        m("createPrincipalRole", body={"principalRole": {"name": self.name}})
        self.later("management", "deletePrincipalRole", path_params=prp)
        role = m("getPrincipalRole", path_params=prp)
        m(
            "updatePrincipalRole",
            path_params=prp,
            body={
                "currentEntityVersion": role["entityVersion"],
                "properties": {"verification": "updated"},
            },
        )
        m("listPrincipalRoles")
        m(
            "assignPrincipalRole",
            path_params=pp,
            body={"principalRole": {"name": self.name}},
        )
        self.later("management", "revokePrincipalRole", path_params={**pp, **prp})
        self.check(
            bool(m("listPrincipalRolesAssigned", path_params=pp)["roles"]),
            "Principal role assignment",
        )
        m("listAssigneePrincipalsForPrincipalRole", path_params=prp)

        crp = {**cp, "catalogRoleName": self.name}
        m(
            "createCatalogRole",
            path_params=cp,
            body={"catalogRole": {"name": self.name}},
        )
        self.later("management", "deleteCatalogRole", path_params=crp)
        role = m("getCatalogRole", path_params=crp)
        m(
            "updateCatalogRole",
            path_params=crp,
            body={
                "currentEntityVersion": role["entityVersion"],
                "properties": {"verification": "updated"},
            },
        )
        m("listCatalogRoles", path_params=cp)
        assignment = {**prp, **cp}
        m(
            "assignCatalogRoleToPrincipalRole",
            path_params=assignment,
            body={"catalogRole": {"name": self.name}},
        )
        self.later(
            "management",
            "revokeCatalogRoleFromPrincipalRole",
            path_params={**assignment, **crp},
        )
        m("listCatalogRolesForPrincipalRole", path_params=assignment)
        m("listAssigneePrincipalRolesForCatalogRole", path_params=crp)
        grant = {"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_ACCESS"}}
        m("addGrantToCatalogRole", path_params=crp, body=grant)
        m("listGrantsForCatalogRole", path_params=crp)
        m("revokeGrantFromCatalogRole", path_params=crp, body=grant)

    def notifications(self) -> None:
        name = self.name + "_external"
        catalog_path = {"catalogName": name}
        self.request(
            "management",
            "createCatalog",
            body={
                "catalog": {
                    "name": name,
                    "type": "EXTERNAL",
                    "properties": {
                        "default-base-location": f"s3://lattice-warehouse/{name}"
                    },
                    "storageConfigInfo": {
                        "storageType": "S3",
                        "allowedLocations": [f"s3://lattice-warehouse/{name}"],
                        "endpoint": "http://127.0.0.1:19000",
                        "endpointInternal": "http://127.0.0.1:19000",
                        "pathStyleAccess": True,
                        "region": "us-east-1",
                    },
                }
            },
        )
        self.later("management", "deleteCatalog", path_params=catalog_path)
        self.request(
            "management",
            "addGrantToCatalogRole",
            path_params={**catalog_path, "catalogRoleName": "catalog_admin"},
            body={"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}},
        )
        prefix = {"prefix": name}
        path = {**prefix, "namespace": "events"}
        self.request(
            "catalog",
            "createNamespace",
            path_params=prefix,
            body={"namespace": ["events"]},
        )
        self.later("catalog", "dropNamespace", path_params=path)
        payload = {
            "table-name": "received_records",
            "timestamp": int(time.time() * 1000),
            "table-uuid": "00000000-0000-4000-8000-000000000001",
            "metadata-location": f"s3://lattice-warehouse/{name}/events/records",
        }
        self.request(
            "catalog",
            "sendNotification",
            path_params={**path, "table": "received_records"},
            body={"notification-type": "VALIDATE", "payload": payload},
        )
        self.check(
            True,
            "External catalog VALIDATE notification passes location and authorization checks",
        )

    def catalog(self, bootstrap: dict[str, Any]) -> None:
        prefix = {"prefix": self.name}
        ns = [self.name, "nested"]
        path = {**prefix, "namespace": "\x1f".join(ns)}

        def c(operation: str, **kwargs: Any) -> Any:
            return self.request("catalog", operation, **kwargs)

        c("getConfig", query={"warehouse": self.name})
        for namespace in [[self.name], ns]:
            c("createNamespace", path_params=prefix, body={"namespace": namespace})
            self.later(
                "catalog",
                "dropNamespace",
                path_params={**prefix, "namespace": "\x1f".join(namespace)},
            )
        c("listNamespaces", path_params=prefix, query={"parent": self.name})
        self.check(
            c("loadNamespaceMetadata", path_params=path)["namespace"] == ns,
            "Multipart namespace round trip",
        )
        c("namespaceExists", path_params=path)
        c(
            "updateProperties",
            path_params=path,
            body={"updates": {"verification": "updated"}, "removals": []},
        )
        self.check(
            c("loadNamespaceMetadata", path_params=path)["properties"]["verification"]
            == "updated",
            "Namespace properties update",
        )

        schema = {
            "type": "struct",
            "schema-id": 0,
            "fields": [{"id": 1, "name": "id", "type": "long", "required": True}],
        }
        table = {**path, "table": "records"}
        created = c(
            "createTable", path_params=path, body={"name": "records", "schema": schema}
        )
        self.later(
            "catalog", "dropTable", path_params=table, query={"purgeRequested": True}
        )
        self.check(
            created["metadata-location"].startswith("s3://lattice-warehouse/"),
            "Iceberg metadata written to MinIO S3",
        )
        c("tableExists", path_params=table)
        c("listTables", path_params=path)
        c("loadTable", path_params=table)
        credentials = c("loadCredentials", path_params=table)
        self.check(
            bool(credentials.get("storage-credentials")),
            "Iceberg temporary storage credentials vending",
        )
        c(
            "updateTable",
            path_params=table,
            body={
                "requirements": [],
                "updates": [
                    {"action": "set-properties", "updates": {"verification": "updated"}}
                ],
            },
        )
        self.check(
            c("loadTable", path_params=table)["metadata"]["properties"]["verification"]
            == "updated",
            "Iceberg table update persists",
        )
        c(
            "commitTransaction",
            path_params=prefix,
            body={
                "table-changes": [
                    {
                        "identifier": {"namespace": ns, "name": "records"},
                        "requirements": [],
                        "updates": [
                            {
                                "action": "set-properties",
                                "updates": {"transaction": "committed"},
                            }
                        ],
                    }
                ]
            },
        )
        self.check(
            c("loadTable", path_params=table)["metadata"]["properties"]["transaction"]
            == "committed",
            "Iceberg transaction commit",
        )
        c(
            "reportMetrics",
            path_params=table,
            body={
                "report-type": "scan-report",
                "table-name": "records",
                "snapshot-id": 0,
                "schema-id": 0,
                "filter": True,
                "projected-field-ids": [1],
                "projected-field-names": ["id"],
                "metrics": {},
            },
        )

        self.notifications()

        view = {**path, "view": "record_view"}
        c(
            "createView",
            path_params=path,
            body={
                "name": "record_view",
                "schema": schema,
                "view-version": {
                    "version-id": 1,
                    "timestamp-ms": int(time.time() * 1000),
                    "schema-id": 0,
                    "summary": {},
                    "representations": [
                        {
                            "type": "sql",
                            "sql": "SELECT id FROM records",
                            "dialect": "spark",
                        }
                    ],
                    "default-namespace": ns,
                },
                "properties": {},
            },
        )
        self.later("catalog", "dropView", path_params=view)
        c("viewExists", path_params=view)
        c("listViews", path_params=path)
        c(
            "replaceView",
            path_params=view,
            body={
                "updates": [
                    {"action": "set-properties", "updates": {"verification": "updated"}}
                ]
            },
        )
        view_data = c("loadView", path_params=view)
        self.check(
            view_data["metadata"]["properties"]["verification"] == "updated",
            "Iceberg view update persists",
        )

        generic = {**path, "generic-table": "delta_records"}
        c(
            "createGenericTable",
            path_params=path,
            body={
                "name": "delta_records",
                "format": "delta",
                "base-location": f"s3://lattice-warehouse/{self.name}/delta_records",
            },
        )
        self.later("catalog", "dropGenericTable", path_params=generic)
        c("listGenericTables", path_params=path)
        self.check(
            c("loadGenericTable", path_params=generic)["table"]["format"] == "delta",
            "Generic Delta table registration",
        )

        policy = {**path, "policy-name": "compact_records"}
        c(
            "createPolicy",
            path_params=path,
            body={
                "name": "compact_records",
                "type": "system.data-compaction",
                "content": '{"enable":true}',
            },
        )
        self.later(
            "catalog", "dropPolicy", path_params=policy, query={"detach-all": True}
        )
        c("listPolicies", path_params=path)
        loaded_policy = c("loadPolicy", path_params=policy)["policy"]
        c(
            "updatePolicy",
            path_params=policy,
            body={
                "current-policy-version": loaded_policy["version"],
                "description": "verification policy",
                "content": '{"enable":false}',
            },
        )
        target = {"target": {"type": "table-like", "path": [*ns, "records"]}}
        c("attachPolicy", path_params=policy, body=target)
        applied = c(
            "getApplicablePolicies",
            path_params=prefix,
            query={"namespace": "\x1f".join(ns), "target-name": "records"},
        )
        self.check(
            bool(applied.get("applicable-policies")),
            "Policy mapping applies to real Iceberg table",
        )
        c("detachPolicy", path_params=policy, body=target)

        model_yaml = yaml.safe_load(bootstrap["model_yaml"])
        model = model_yaml["semantic_model"]
        if isinstance(model, list):
            model = model[0]
        document = {
            "version": model_yaml["version"],
            "semantic_model": json.dumps(model, ensure_ascii=False),
        }
        semantic = {**path, "semantic-model-name": "sales_model"}
        # Polaris 1.7.0's own adapter returns 501 for these five operations; the
        # Lattice gateway implements the documented contract on top of real Polaris
        # generic tables, so the lifecycle is verified end to end here.
        created = c(
            "createSemanticModel",
            path_params=path,
            body={"name": "sales_model", "document": document},
        )
        self.later("catalog", "dropSemanticModel", path_params=semantic)
        version = created.get("entity-version") if isinstance(created, dict) else None
        self.check(
            isinstance(version, str) and bool(version),
            "createSemanticModel returns an entity-version (gateway implementation)",
        )
        self.check(
            created.get("document") == document,
            "createSemanticModel stores the submitted Apache Ossie document",
        )
        listed = c("listSemanticModels", path_params=path)
        self.check(
            {"namespace": ns, "name": "sales_model"}
            in listed.get("identifiers", []),
            "listSemanticModels lists the created model identifier",
        )
        loaded = c("loadSemanticModel", path_params=semantic)
        self.check(
            loaded.get("entity-version") == version
            and loaded.get("document") == document,
            "loadSemanticModel returns the stored document and version",
        )
        c(
            "createSemanticModel",
            path_params=path,
            expect=(409,),
            body={"name": "sales_model", "document": document},
        )
        c(
            "updateSemanticModel",
            path_params=semantic,
            expect=(409,),
            body={"entity-version": "stale-version", "document": document},
        )
        updated_model = dict(model)
        updated_model["description"] = "updated by verify-webui"
        updated_document = {
            "version": model_yaml["version"],
            "semantic_model": json.dumps(updated_model, ensure_ascii=False),
        }
        updated = c(
            "updateSemanticModel",
            path_params=semantic,
            body={"entity-version": version, "document": updated_document},
        )
        self.check(
            isinstance(updated.get("entity-version"), str)
            and updated["entity-version"] != version
            and updated.get("document") == updated_document,
            "updateSemanticModel applies optimistic concurrency and replaces the document",
        )
        c(
            "createSemanticModel",
            path_params=path,
            expect=(400,),
            body={
                "name": "broken_model",
                "document": {"version": "0.1.1", "semantic_model": "{not json"},
            },
        )
        self.semantic_rest_identity(prefix["prefix"], path["namespace"], document)
        c("dropSemanticModel", path_params=semantic, expect=(204,))
        c("loadSemanticModel", path_params=semantic, expect=(404,))
        self.check(
            True,
            "All five semantic-model operations are served by the Lattice gateway with real persistence",
        )

        # Own catalog temporarily retains view metadata for re-registration.
        c(
            "renameView",
            path_params=prefix,
            body={
                "source": {"namespace": ns, "name": "record_view"},
                "destination": {"namespace": ns, "name": "renamed_view"},
            },
        )
        renamed_view = {**path, "view": "renamed_view"}
        self.later("catalog", "dropView", path_params=renamed_view)
        view_data = c("loadView", path_params=renamed_view)
        c("dropView", path_params=renamed_view)
        c(
            "registerView",
            path_params=path,
            body={
                "name": "registered_view",
                "metadata-location": view_data["metadata-location"],
            },
        )
        self.later(
            "catalog", "dropView", path_params={**path, "view": "registered_view"}
        )
        current_catalog = self.request(
            "management", "getCatalog", path_params={"catalogName": self.name}
        )
        self.request(
            "management",
            "updateCatalog",
            path_params={"catalogName": self.name},
            body={
                "currentEntityVersion": current_catalog["entityVersion"],
                "properties": {
                    **current_catalog["properties"],
                    "polaris.config.purge-view-metadata-on-drop": "true",
                },
            },
        )

        c(
            "renameTable",
            path_params=prefix,
            body={
                "source": {"namespace": ns, "name": "records"},
                "destination": {"namespace": ns, "name": "renamed_records"},
            },
        )
        renamed_table = {**path, "table": "renamed_records"}
        self.later(
            "catalog",
            "dropTable",
            path_params=renamed_table,
            query={"purgeRequested": True},
        )
        table_data = c("loadTable", path_params=renamed_table)
        c("dropTable", path_params=renamed_table, query={"purgeRequested": False})
        c(
            "registerTable",
            path_params=path,
            body={
                "name": "registered_records",
                "metadata-location": table_data["metadata-location"],
            },
        )
        self.later(
            "catalog",
            "dropTable",
            path_params={**path, "table": "registered_records"},
            query={"purgeRequested": True},
        )

    def semantic_rest_identity(self, catalog: str, namespace: str, document: dict) -> None:
        """The same-path REST routes must act as the caller, not as the gateway's root."""
        reader = self.name + "_reader"
        role, catalog_role = reader + "_role", reader + "_catalog_role"
        m = lambda op, **kw: self.request("management", op, **kw)  # noqa: E731
        credential = m(
            "createPrincipal", body={"principal": {"name": reader, "type": "PRINCIPAL"}}
        )["credentials"]
        self.later("management", "deletePrincipal", path_params={"principalName": reader})
        m("createPrincipalRole", body={"principalRole": {"name": role}})
        self.later("management", "deletePrincipalRole", path_params={"principalRoleName": role})
        m(
            "assignPrincipalRole",
            path_params={"principalName": reader},
            body={"principalRole": {"name": role}},
        )
        m(
            "createCatalogRole",
            path_params={"catalogName": catalog},
            body={"catalogRole": {"name": catalog_role}},
        )
        self.later(
            "management",
            "deleteCatalogRole",
            path_params={"catalogName": catalog, "catalogRoleName": catalog_role},
        )
        m(
            "assignCatalogRoleToPrincipalRole",
            path_params={"principalRoleName": role, "catalogName": catalog},
            body={"catalogRole": {"name": catalog_role}},
        )
        for privilege in (
            "NAMESPACE_LIST",
            "NAMESPACE_READ_PROPERTIES",
            "TABLE_LIST",
            "TABLE_READ_PROPERTIES",
        ):
            m(
                "addGrantToCatalogRole",
                path_params={"catalogName": catalog, "catalogRoleName": catalog_role},
                body={"grant": {"type": "catalog", "privilege": privilege}},
            )
        token = self.request(
            "catalog",
            "getToken",
            body={
                "grant_type": "client_credentials",
                "client_id": credential["clientId"],
                "client_secret": credential["clientSecret"],
                "scope": "PRINCIPAL_ROLE:ALL",
            },
        )["access_token"]
        headers = {"Authorization": "Bearer " + token}
        # The unit separator between namespace levels must be percent-encoded in a URL.
        route = (
            f"/polaris/v1/{quote(catalog, safe='')}"
            f"/namespaces/{quote(namespace, safe='')}/semantic-models"
        )
        anonymous = self.client.get(route)
        listed = self.client.get(route, headers=headers)
        created = self.client.post(
            route,
            headers={**headers, "Content-Type": "application/json"},
            json={"name": "denied_model", "document": document},
        )
        dropped = self.client.request("DELETE", route + "/sales_model", headers=headers)
        for label, response in (
            ("unauthenticated", anonymous),
            ("read-only list", listed),
            ("read-only create", created),
            ("read-only drop", dropped),
        ):
            self.direct_calls.append(
                {
                    "operation": "semantic-models REST",
                    "status": response.status_code,
                    "identity": label,
                }
            )
        self.check(anonymous.status_code == 401, "REST semantic-model routes reject an anonymous caller")
        self.check(listed.status_code == 200, "A read-only principal can list semantic models")
        self.check(
            created.status_code == 403,
            "A read-only principal cannot create a semantic model (caller identity, not gateway root)",
        )
        self.check(
            dropped.status_code == 403,
            "A read-only principal cannot drop a semantic model",
        )

    def report(self, error: str | None) -> dict[str, Any]:
        covered = {
            (c["spec"], c["operation"])
            for c in self.calls
            if c["status"] in c["expected"]
        }
        successful = {
            (c["spec"], c["operation"]) for c in self.calls if 200 <= c["status"] < 300
        }
        upstream_stubs = sorted(
            {c["operation"] for c in self.calls if c["status"] == 501}
        )
        return {
            "status": (
                "passed"
                if not error
                and not self.cleanup_errors
                and not (self.advertised - covered)
                else "failed"
            ),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "run_prefix": self.name,
            "backend": "Apache Polaris 1.7.0 + PostgreSQL + MinIO (real local services)",
            "operation_count": len(self.advertised),
            "covered_operation_count": len(covered),
            "successful_gateway_operation_count": len(successful),
            "upstream_unimplemented_operations": upstream_stubs,
            "direct_identity_checks": self.direct_calls,
            "uncovered_operations": [
                f"{s}.{o}" for s, o in sorted(self.advertised - covered)
            ],
            "assertions": self.assertions,
            "calls": self.calls,
            "error": error,
            "cleanup_errors": self.cleanup_errors,
            "limitations": [
                "Five semantic-model operations return 501 in upstream Polaris 1.7.0; they are served by the Lattice gateway (webapi/semantic_models.py) with persistence in Polaris generic tables, executed as the calling principal.",
                "Cloud provider identities and remote catalog federation require external configuration.",
                "External notification validation is tested; remote CREATE/UPDATE delivery is not configured.",
                "DuckDB demonstration queries do not use an external LLM or Trino cluster.",
                "This is local API lifecycle verification, not an exhaustive upstream test suite.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--report", type=Path, default=ROOT / ".runtime/webui/verification.json"
    )
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        parser.error("--url must be a plain local HTTP origin")
    verify = Verification(args.url)
    error = None
    try:
        bootstrap = verify.bootstrap()
        verify.management()
        verify.catalog(bootstrap)
    except (VerificationError, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        error = str(exc)
    finally:
        verify.cleanup()
        verify.client.close()
    report = verify.report(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"{report['status']}: {len(report['assertions'])} assertions, "
        f"{report['covered_operation_count']}/{report['operation_count']} routes checked "
        f"({report['successful_gateway_operation_count']} successful, "
        f"{len(report['upstream_unimplemented_operations'])} upstream stubs); report: {args.report}"
    )
    if error:
        print(error, file=sys.stderr)
    if verify.cleanup_errors:
        print("Cleanup failed: " + "; ".join(verify.cleanup_errors), file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
