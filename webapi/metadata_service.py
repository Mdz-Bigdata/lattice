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

"""Entity semantics of the 元数据管理 module: the OpenMetadata catalog logic.

Everything an OpenMetadata server does with an entity apart from serving HTTP
lives here — creation under a parent with a computed fully qualified name,
versioned updates with a change description, soft delete and restore with the
children, hard delete with the cleanup of relationships, tag usage and lineage,
tag and glossary-term assignment with mutual-exclusion rules, ownership and
following, domains and data products, search with facets, the activity feed
(change events plus conversations, tasks and announcements), custom-property
definitions and the JSON import/export used to migrate from OpenMetadata.

The service serialises mutations with one re-entrant lock: the platform runs
in a single process, and a lock is a simpler guarantee than optimistic
versioning across the several store calls one mutation needs.
"""

from __future__ import annotations

import datetime as dt
import re
import threading

from .auth import current_actor
from typing import Any, Callable, Iterable

from .metadata_entities import (
    AUTOMATED_CONNECTORS,
    CHANGE_KINDS,
    COLUMN_TYPES,
    CUSTOM_PROPERTY_TYPES,
    DATA_ASSET_TYPES,
    DEFAULT_TEAM,
    DOMAIN_TYPES,
    ENTITY_TYPES,
    EVENT_TYPES,
    GLOSSARY_TERM_STATUSES,
    LATTICE_CONNECTOR_TYPES,
    SEED_CLASSIFICATIONS,
    SEED_TEAMS,
    SEED_USERS,
    SERVICE_CATEGORIES,
    SERVICE_TYPES,
    SYSTEM_USER,
    TABLE_TYPES,
    TEAM_TYPES,
    TIER_CLASSIFICATION,
    TREE_CHILDREN,
    USER_NAME,
    build_fqn,
    change_description,
    check_entity_type,
    check_name,
    check_text,
    child_fqn,
    data_length,
    describe_change,
    is_major_change,
    leaf_name,
    next_version,
    normalize_data_type,
    parent_fqn as fqn_parent,
    parent_types,
    search_text,
    service_type_of,
    split_fqn,
    type_label,
)
from .metadata_store import (
    DEFAULT_PAGE_SIZE,
    MetadataStore,
    MetadataStoreError,
    check_page,
    new_id,
    now_ms,
)

FACET_LIMIT = 2000
MAX_COLUMNS = 2000
MAX_TAGS = 100
MAX_BUNDLE = 20000
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")

#: Document fields a caller may set per entity type (beyond the common ones).
COMMON_FIELDS = {"display_name", "description", "extension", "source_url", "style"}
TYPE_FIELDS: dict[str, set[str]] = {
    "databaseService": {"service_type", "connection", "ingestion", "datasource_id"},
    "messagingService": {"service_type", "connection", "ingestion"},
    "dashboardService": {"service_type", "connection", "ingestion"},
    "pipelineService": {"service_type", "connection", "ingestion"},
    "mlmodelService": {"service_type", "connection", "ingestion"},
    "storageService": {"service_type", "connection", "ingestion"},
    "searchService": {"service_type", "connection", "ingestion"},
    "apiService": {"service_type", "connection", "ingestion"},
    "metadataService": {"service_type", "connection", "ingestion"},
    "database": {"retention_period", "source_name"},
    "databaseSchema": {"retention_period", "source_name"},
    "table": {
        "table_type", "columns", "table_constraints", "partitions", "row_count", "size_in_bytes",
        "view_definition", "schema_definition", "retention_period", "datasource_id", "source_schema",
        "source_table", "file_format", "location", "polaris",
    },
    "storedProcedure": {"code", "language", "procedure_type"},
    "topic": {
        "partitions", "replication_factor", "schema_type", "schema_text", "columns", "cleanup_policies",
        "retention_size", "retention_time", "maximum_message_size", "minimum_in_sync_replicas",
    },
    "dashboard": {"dashboard_type", "project", "charts", "data_models"},
    "chart": {"chart_type", "dashboards"},
    "dashboardDataModel": {"data_model_type", "sql", "columns", "project"},
    "pipeline": {"tasks", "schedule_interval", "concurrency", "pipeline_location", "pipeline_status"},
    "mlmodel": {"algorithm", "ml_features", "ml_hyper_parameters", "ml_store", "server", "target", "dashboard_fqn"},
    "container": {"prefix", "file_formats", "number_of_objects", "size", "columns", "full_path"},
    "searchIndex": {"columns", "index_type", "search_index_settings"},
    "apiCollection": {"endpoint_url"},
    "apiEndpoint": {"endpoint_url", "request_method", "request_schema", "response_schema"},
    "semanticModel": {"catalog", "namespace", "entity_version", "datasets", "metrics", "model_id", "storage", "yaml"},
    "glossary": {"mutually_exclusive"},
    "glossaryTerm": {"synonyms", "related_terms", "references", "status", "mutually_exclusive"},
    "classification": {"mutually_exclusive", "provider", "disabled"},
    "tag": {"mutually_exclusive", "provider", "disabled"},
    "domain": {"domain_type"},
    "dataProduct": set(),
    "team": {"team_type", "email", "is_joinable"},
    "user": {"email", "is_admin", "is_bot"},
    "kpi": {"chart", "metric_type", "target_value", "start_date", "end_date"},
    "eventSubscription": {"enabled", "filters", "destinations"},
}
#: Fields that are relationships or assignments rather than document fields.
ASSIGNMENT_FIELDS = {"owners", "tags", "tier", "domain", "data_products", "experts", "reviewers", "teams", "users", "assets", "columns"}
RELATION_OWNS = "owns"
RELATION_FOLLOWS = "follows"
RELATION_MEMBER = "member"
RELATION_TEAM_PARENT = "parentTeam"
RELATION_EXPERT = "expert"
RELATION_REVIEWER = "reviewer"
RELATION_RELATED = "relatedTerm"
RELATION_PRODUCT = "dataProduct"
LABEL_TYPES = ("manual", "automated", "derived", "propagated")
TAG_STATES = ("confirmed", "suggested")
THREAD_TYPES = ("Conversation", "Task", "Announcement")
TASK_TYPES = ("RequestDescription", "UpdateDescription", "RequestTag", "UpdateTag")
EVENT_LABELS = {
    "entityCreated": "创建",
    "entityUpdated": "更新",
    "entitySoftDeleted": "删除（可恢复）",
    "entityRestored": "恢复",
    "entityDeleted": "永久删除",
}


class MetadataError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _bad(message: str) -> MetadataError:
    return MetadataError(400, message)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class MetadataService:
    """Catalog semantics over :class:`MetadataStore`."""

    @property
    def user(self) -> str:
        """The acting user: the signed-in person when there is one, else the module default."""
        actor = current_actor.get()
        if actor and actor.get("id") != "local" and actor.get("username"):
            return str(actor["username"])
        return self._user

    @user.setter
    def user(self, value: str) -> None:
        self._user = value

    def __init__(self, store: MetadataStore, *, user: str = SYSTEM_USER):
        self.store = store
        self._user = user
        self._lock = threading.RLock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self._event_lock = threading.Lock()
        self._last_event_ts = 0

    # =====================================================================================
    # bootstrap
    # =====================================================================================
    def seed(self) -> dict[str, int]:
        """Create the system classifications, the root team and the platform user once."""
        created = 0
        with self._lock:
            for team in SEED_TEAMS:
                if self.store.get_entity("team", fqn=build_fqn(team["name"])) is None:
                    payload = {k: v for k, v in team.items() if k != "parent"}
                    self.create("team", payload, user=SYSTEM_USER, silent=True)
                    created += 1
                    if team.get("parent"):
                        self._set_team_parent(team["name"], team["parent"])
            for user in SEED_USERS:
                if self.store.get_entity("user", fqn=build_fqn(user["name"])) is None:
                    self.create("user", user, user=SYSTEM_USER, silent=True)
                    created += 1
            for classification in SEED_CLASSIFICATIONS:
                fqn = build_fqn(classification["name"])
                if self.store.get_entity("classification", fqn=fqn) is None:
                    self.create(
                        "classification",
                        {
                            "name": classification["name"],
                            "display_name": classification.get("display_name", ""),
                            "description": classification["description"],
                            "mutually_exclusive": classification["mutually_exclusive"],
                            "provider": "system",
                        },
                        user=SYSTEM_USER,
                        silent=True,
                    )
                    created += 1
                for tag in classification["tags"]:
                    if self.store.get_entity("tag", fqn=child_fqn(fqn, tag["name"])) is None:
                        self.create(
                            "tag",
                            {"name": tag["name"], "description": tag["description"], "parent_fqn": fqn, "provider": "system"},
                            user=SYSTEM_USER,
                            silent=True,
                        )
                        created += 1
        return {"created": created}

    def _set_team_parent(self, child: str, parent: str) -> None:
        child_row = self.store.get_entity("team", fqn=build_fqn(child))
        parent_row = self.store.get_entity("team", fqn=build_fqn(parent))
        if child_row and parent_row:
            self.store.add_relationship(parent_row["id"], "team", child_row["id"], "team", RELATION_TEAM_PARENT)

    # =====================================================================================
    # catalog of types
    # =====================================================================================
    def types(self) -> dict[str, Any]:
        return {
            "entity_types": [
                {
                    "id": name,
                    "label": spec["label"],
                    "category": spec["category"],
                    "service_category": spec.get("service_category"),
                    "parents": parent_types(name),
                    "children": TREE_CHILDREN.get(name, []),
                    "has_columns": bool(spec.get("columns")),
                    "data_asset": bool(spec.get("data_asset")),
                }
                for name, spec in ENTITY_TYPES.items()
            ],
            "service_categories": [
                {
                    "id": key,
                    "label": spec["label"],
                    "description": spec["description"],
                    "entity_type": spec["entity_type"],
                    "children": spec["children"],
                    "connectors": [
                        {"id": name, "automated": name in AUTOMATED_CONNECTORS}
                        for name in spec["connectors"]
                    ],
                }
                for key, spec in SERVICE_CATEGORIES.items()
            ],
            "lattice_connectors": LATTICE_CONNECTOR_TYPES,
            "term_statuses": list(GLOSSARY_TERM_STATUSES),
            "domain_types": list(DOMAIN_TYPES),
            "team_types": list(TEAM_TYPES),
            "table_types": list(TABLE_TYPES),
            "custom_property_types": list(CUSTOM_PROPERTY_TYPES),
            "event_types": [{"id": key, "label": EVENT_LABELS.get(key, key)} for key in EVENT_TYPES],
            "change_kinds": list(CHANGE_KINDS),
            "thread_types": list(THREAD_TYPES),
            "task_types": list(TASK_TYPES),
            "tier_classification": TIER_CLASSIFICATION,
        }

    # =====================================================================================
    # lookup
    # =====================================================================================
    def row(self, entity_type: str | None, ref: str, *, include_deleted: bool = True) -> dict[str, Any]:
        """Find an entity by id or FQN; raise 404 when missing."""
        if entity_type is not None:
            check_entity_type(entity_type)
        if not isinstance(ref, str) or not ref.strip():
            raise _bad("缺少实体标识。")
        ref = ref.strip()
        row = self.store.get_entity(entity_type, id=ref) if _looks_like_id(ref) else None
        if row is None:
            row = self.store.get_entity(entity_type, fqn=ref)
        if row is None and entity_type is None:
            row = self.resolve_asset(ref)
        if row is None or (row["deleted"] and not include_deleted):
            raise MetadataError(404, f"{type_label(entity_type) if entity_type else '实体'}不存在：{ref}")
        return row

    def resolve_asset(self, fqn: str, prefer: Iterable[str] = ("table",)) -> dict[str, Any] | None:
        """An entity of any type by FQN, preferring data assets when several types share it."""
        rows = self.store.get_entities_by_fqn([fqn])
        if not rows:
            return None
        order = list(prefer) + DATA_ASSET_TYPES
        rows.sort(key=lambda row: order.index(row["entity_type"]) if row["entity_type"] in order else 999)
        return rows[0]

    def resolve_column(self, fqn: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Split a column FQN into its owning entity and the column document."""
        parts = split_fqn(fqn)
        for cut in range(len(parts) - 1, 0, -1):
            owner_fqn = build_fqn(*parts[:cut])
            rows = [row for row in self.store.get_entities_by_fqn([owner_fqn]) if row["entity_type"] in COLUMN_TYPES]
            if not rows:
                continue
            column_name = build_fqn(*parts[cut:])
            for row in rows:
                for column in _columns_of(row["json"]):
                    if column.get("name") == column_name:
                        return row, column
        return None

    # =====================================================================================
    # decoration
    # =====================================================================================
    def decorate(self, row: dict[str, Any], *, full: bool = True) -> dict[str, Any]:
        return self.decorate_many([row], full=full)[0]

    def decorate_many(self, rows: list[dict[str, Any]], *, full: bool = False) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        fqns = [row["fqn"] for row in rows]
        owners = self._owners_for(ids)
        tags_by_target = self._group_tags(
            self.store.tags_for_targets(fqns) if not full else
            [tag for row in rows for tag in self.store.tags_for_prefix(row["fqn"])]
        )
        tag_docs = self._tag_documents({tag["tag_fqn"] for tags in tags_by_target.values() for tag in tags})
        domains = self._entities_by_fqn("domain", {row["domain_fqn"] for row in rows if row["domain_fqn"]})
        products = self._products_for(ids) if full else {}
        followers = self._followers_for(ids) if full else {}
        children = self.store.count_children(fqns) if full else {}
        since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        usage = self.store.usage_totals(fqns, since) if full else {}
        services = self._entities_by_fqn(None, {row["service_fqn"] for row in rows if row["service_fqn"]}) if full else {}
        parents = self._entities_by_fqn(None, {row["parent_fqn"] for row in rows if row["parent_fqn"]}) if full else {}
        results = []
        for row in rows:
            results.append(
                self._public(
                    row,
                    owners=owners.get(row["id"], []),
                    tags=tags_by_target,
                    tag_docs=tag_docs,
                    domain=domains.get(row["domain_fqn"]),
                    products=products.get(row["id"], []),
                    followers=followers.get(row["id"], []),
                    children=children,
                    usage=usage.get(row["fqn"]),
                    service=services.get(row["service_fqn"]),
                    parent=parents.get(row["parent_fqn"]),
                    full=full,
                )
            )
        return results

    def _public(
        self, row, *, owners, tags, tag_docs, domain, products, followers, children, usage, service, parent, full,
    ) -> dict[str, Any]:
        document = dict(row["json"])
        entity_type = row["entity_type"]
        fqn = row["fqn"]
        entity_tags = [self._tag_view(tag, tag_docs) for tag in tags.get(fqn, [])]
        out: dict[str, Any] = {
            **{key: value for key, value in document.items() if key not in {"columns"}},
            "id": row["id"],
            "entity_type": entity_type,
            "type_label": type_label(entity_type),
            "name": row["name"],
            "display_name": row["display_name"],
            "fqn": fqn,
            "parent_fqn": row["parent_fqn"],
            "service_fqn": row["service_fqn"],
            "service_type": row["service_type"],
            "description": row["description"],
            "tier": row["tier"] or None,
            "domain": _summary(domain) if domain else None,
            "deleted": row["deleted"],
            "version": row["version"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "owners": owners,
            "tags": [tag for tag in entity_tags if tag["source"] == "classification"],
            "glossary_terms": [tag for tag in entity_tags if tag["source"] == "glossary"],
        }
        if entity_type in COLUMN_TYPES:
            columns = []
            for column in _columns_of(document):
                column_fqn = child_fqn(fqn, column.get("name", ""))
                column_tags = [self._tag_view(tag, tag_docs) for tag in tags.get(column_fqn, [])] if full else []
                columns.append(
                    {
                        **column,
                        "fqn": column_fqn,
                        "tags": [tag for tag in column_tags if tag["source"] == "classification"],
                        "glossary_terms": [tag for tag in column_tags if tag["source"] == "glossary"],
                    }
                )
            out["columns"] = columns
            out["column_count"] = len(columns)
        if entity_type == "eventSubscription":
            out["destinations"] = mask_destinations(document.get("destinations"))
        if full:
            out["data_products"] = products
            out["followers"] = followers
            out["followers_count"] = len(followers)
            out["children_count"] = children.get(fqn, 0)
            out["usage"] = usage or {"queries": 0, "views": 0}
            out["service"] = _summary(service) if service else None
            out["parent"] = _summary(parent) if parent else None
            out["breadcrumb"] = self._breadcrumb(row)
        return out

    def _breadcrumb(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        parts = split_fqn(row["fqn"])
        prefixes = [build_fqn(*parts[: index + 1]) for index in range(len(parts) - 1)]
        rows = {r["fqn"]: r for r in self.store.get_entities_by_fqn(prefixes)}
        chain = []
        for prefix in prefixes:
            candidates = [r for r in rows.values() if r["fqn"] == prefix]
            if not candidates:
                continue
            # A parent shares the child's family: same service, or the governance tree above it.
            match = next((r for r in candidates if r["entity_type"] in parent_types(row["entity_type"]) or r["fqn"] == row["service_fqn"]), None)
            match = match or next((r for r in candidates if r["service_fqn"] == row["service_fqn"]), candidates[0])
            chain.append(_summary(match))
        return chain

    @staticmethod
    def _tag_view(tag: dict[str, Any], tag_docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        doc = tag_docs.get(tag["tag_fqn"])
        parts = split_fqn(tag["tag_fqn"])
        return {
            "tag_fqn": tag["tag_fqn"],
            "source": tag["source"],
            "label_type": tag["label_type"],
            "state": tag["state"],
            "name": doc["name"] if doc else (parts[-1] if parts else tag["tag_fqn"]),
            "display_name": (doc["display_name"] or doc["name"]) if doc else (parts[-1] if parts else tag["tag_fqn"]),
            "root": parts[0] if parts else "",
            "description": doc["description"] if doc else "",
            "style": (doc["json"].get("style") if doc else None) or {},
            "deleted": bool(doc["deleted"]) if doc else True,
        }

    def _tag_documents(self, tag_fqns: set[str]) -> dict[str, dict[str, Any]]:
        if not tag_fqns:
            return {}
        rows = self.store.get_entities_by_fqn(sorted(tag_fqns))
        docs: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["entity_type"] in {"tag", "glossaryTerm"}:
                docs.setdefault(row["fqn"], row)
        return docs

    @staticmethod
    def _group_tags(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tag in rows:
            grouped.setdefault(tag["target_fqn"], []).append(tag)
        for tags in grouped.values():
            tags.sort(key=lambda tag: (tag["source"], tag["tag_fqn"]))
        return grouped

    def _entities_by_fqn(self, entity_type: str | None, fqns: set[str]) -> dict[str, dict[str, Any]]:
        if not fqns:
            return {}
        rows = self.store.get_entities_by_fqn(sorted(fqns), entity_type)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if entity_type is None and row["entity_type"] not in SERVICE_TYPES and row["fqn"] in result:
                continue
            result.setdefault(row["fqn"], row)
        return result

    def _owners_for(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        relations = self.store.relationships_to_many(ids, RELATION_OWNS)
        owner_rows = {row["id"]: row for row in self.store.get_entities([r["from_id"] for r in relations])}
        owners: dict[str, list[dict[str, Any]]] = {}
        for relation in relations:
            owner = owner_rows.get(relation["from_id"])
            if owner and not owner["deleted"]:
                owners.setdefault(relation["to_id"], []).append(_summary(owner))
        return owners

    def _followers_for(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        relations = self.store.relationships_to_many(ids, RELATION_FOLLOWS)
        rows = {row["id"]: row for row in self.store.get_entities([r["from_id"] for r in relations])}
        result: dict[str, list[dict[str, Any]]] = {}
        for relation in relations:
            user = rows.get(relation["from_id"])
            if user:
                result.setdefault(relation["to_id"], []).append(_summary(user))
        return result

    def _products_for(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        relations = self.store.relationships_to_many(ids, RELATION_PRODUCT)
        rows = {row["id"]: row for row in self.store.get_entities([r["from_id"] for r in relations])}
        result: dict[str, list[dict[str, Any]]] = {}
        for relation in relations:
            product = rows.get(relation["from_id"])
            if product and not product["deleted"]:
                result.setdefault(relation["to_id"], []).append(_summary(product))
        return result

    # =====================================================================================
    # create
    # =====================================================================================
    def create(self, entity_type: str, payload: dict[str, Any], *, user: str | None = None, silent: bool = False) -> dict[str, Any]:
        check_entity_type(entity_type)
        if not isinstance(payload, dict):
            raise _bad("请求体必须是对象。")
        user = user or self.user
        with self._lock:
            name = check_name(payload.get("name"), "名称")
            if entity_type == "user" and not USER_NAME.match(name):
                raise _bad("用户名只能包含小写字母、数字、点、下划线和连字符。")
            parent = self._resolve_parent(entity_type, payload)
            fqn = child_fqn(parent["fqn"], name) if parent else build_fqn(name)
            existing = self.store.get_entity(entity_type, fqn=fqn)
            if existing is not None:
                if existing["deleted"]:
                    raise MetadataError(409, f"{type_label(entity_type)} {fqn} 已存在但处于已删除状态，请先恢复或永久删除。")
                raise MetadataError(409, f"{type_label(entity_type)} {fqn} 已存在。")
            document = self._document(entity_type, payload, current=None)
            document["name"] = name
            self._validate_assignments(entity_type, payload)
            stamp = now_ms()
            row = {
                "id": new_id(),
                "entity_type": entity_type,
                "name": name,
                "display_name": document.get("display_name", ""),
                "fqn": fqn,
                "parent_fqn": parent["fqn"] if parent else "",
                "service_type": self._service_type(entity_type, document, parent),
                "service_fqn": self._service_fqn(entity_type, fqn, parent),
                "description": document.get("description", ""),
                "tier": "",
                "domain_fqn": "",
                "deleted": False,
                "version": 0.1,
                "updated_at": stamp,
                "updated_by": user,
                "created_at": stamp,
                "search_text": search_text(document, entity_type, fqn),
                "json": document,
            }
            if entity_type == "domain" and parent is None and payload.get("domain_fqn"):
                pass
            self.store.insert_entity(row)
            self.store.save_version(row["id"], 0.1, document, {"fields_added": [], "fields_updated": [], "fields_deleted": []}, stamp, user)
            self._apply_assignments(row, payload, user, creating=True)
            row = self.store.get_entity(entity_type, id=row["id"]) or row
            if not silent:
                self._emit(
                    {
                        "event_type": "entityCreated",
                        "entity_type": entity_type,
                        "entity_id": row["id"],
                        "entity_fqn": fqn,
                        "entity_name": row["display_name"] or name,
                        "user_name": user,
                        "ts": stamp,
                        "previous_version": None,
                        "current_version": 0.1,
                        "change": {"summary": "创建" + type_label(entity_type)},
                    }
                )
            return self.decorate(row)

    def _resolve_parent(self, entity_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = parent_types(entity_type)
        ref = payload.get("parent_fqn") or payload.get("parent")
        if entity_type == "glossaryTerm":
            ref = ref or payload.get("glossary_fqn") or payload.get("glossary")
        elif entity_type == "tag":
            ref = ref or payload.get("classification_fqn") or payload.get("classification")
        elif entity_type in {"database", "topic", "dashboard", "chart", "dashboardDataModel", "pipeline", "mlmodel", "container", "searchIndex", "apiCollection"}:
            ref = ref or payload.get("service_fqn") or payload.get("service")
        elif entity_type == "databaseSchema":
            ref = ref or payload.get("database_fqn") or payload.get("database")
        elif entity_type in {"table", "storedProcedure"}:
            ref = ref or payload.get("schema_fqn") or payload.get("database_schema")
        elif entity_type == "apiEndpoint":
            ref = ref or payload.get("collection_fqn")
        if not allowed:
            if ref:
                raise _bad(f"{type_label(entity_type)}不能有上级对象。")
            return None
        if not ref:
            if ENTITY_TYPES[entity_type].get("optional_parent"):
                return None
            raise _bad(f"{type_label(entity_type)}必须指定上级对象（{'/'.join(type_label(t) for t in allowed)}）。")
        if not isinstance(ref, str):
            raise _bad("上级对象必须用 FQN 或 ID 指定。")
        for candidate in allowed:
            row = self.store.get_entity(candidate, id=ref) if _looks_like_id(ref) else None
            row = row or self.store.get_entity(candidate, fqn=ref)
            if row is not None:
                if row["deleted"]:
                    raise _bad(f"上级对象 {row['fqn']} 已被删除。")
                return row
        raise MetadataError(404, f"上级对象不存在：{ref}")

    @staticmethod
    def _service_fqn(entity_type: str, fqn: str, parent: dict[str, Any] | None) -> str:
        if entity_type in SERVICE_TYPES:
            return fqn
        if parent is not None and service_type_of(entity_type):
            return parent["service_fqn"] or parent["fqn"]
        if entity_type in {"glossaryTerm", "tag", "domain"} and parent is not None:
            return parent["service_fqn"] or parent["fqn"]
        if entity_type in {"glossary", "classification", "domain"}:
            return fqn
        return ""

    @staticmethod
    def _service_type(entity_type: str, document: dict[str, Any], parent: dict[str, Any] | None) -> str:
        if entity_type in SERVICE_TYPES:
            return str(document.get("service_type") or "")
        if parent is not None:
            return parent["service_type"]
        if entity_type == "semanticModel":
            return "Ossie"
        return ""

    # =====================================================================================
    # documents
    # =====================================================================================
    def _document(self, entity_type: str, payload: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
        """Validate the document fields of ``payload`` onto ``current`` (or a new document)."""
        document = dict(current or {})
        allowed = COMMON_FIELDS | TYPE_FIELDS.get(entity_type, set())
        ignored = {
            "name", "parent_fqn", "parent", "glossary_fqn", "glossary", "classification_fqn", "classification",
            "service_fqn", "service", "database_fqn", "database", "schema_fqn", "database_schema", "collection_fqn",
            "domain_fqn", "domain", *ASSIGNMENT_FIELDS, "id", "fqn", "entity_type", "version", "updated_at",
            "updated_by", "created_at", "deleted", "type_label", "href", "user", "teams", "parent_team",
        }
        for key, value in payload.items():
            if key in ignored:
                continue
            if key not in allowed:
                raise _bad(f"{type_label(entity_type)}不支持字段 {key}。")
            document[key] = self._field(entity_type, key, value, document)
        if "columns" in payload and entity_type in COLUMN_TYPES:
            document["columns"] = self._columns(payload["columns"], existing=_columns_of(document), merge=current is not None)
        elif current is None and entity_type in COLUMN_TYPES:
            document.setdefault("columns", [])
        document.setdefault("display_name", "")
        document.setdefault("description", "")
        document.setdefault("extension", {})
        if entity_type == "glossaryTerm":
            document.setdefault("status", "Draft")
            document.setdefault("synonyms", [])
            document.setdefault("related_terms", [])
            document.setdefault("references", [])
        if entity_type == "domain":
            if not document.get("domain_type"):
                raise _bad("数据域必须选择域类型（Aggregate / Consumer-aligned / Source-aligned）。")
        if entity_type == "team":
            document.setdefault("team_type", "Group")
        if entity_type == "kpi":
            self._check_kpi(document)
        if entity_type == "eventSubscription":
            document.setdefault("enabled", True)
            document.setdefault("filters", {})
            document.setdefault("destinations", [{"type": "in_app"}])
        if entity_type in SERVICE_TYPES:
            document.setdefault("service_type", "CustomDatabase" if entity_type == "databaseService" else "Custom")
            document.setdefault("connection", {})
            document.setdefault("ingestion", {})
        if entity_type == "table":
            document.setdefault("table_type", "Regular")
        return document

    def _field(self, entity_type: str, key: str, value: Any, document: dict[str, Any]) -> Any:
        if key in {"display_name"}:
            return check_text(value, "显示名称", 256)
        if key in {"description", "schema_text", "view_definition", "schema_definition", "code", "sql", "yaml"}:
            return check_text(value, "描述" if key == "description" else key, 200000 if key == "yaml" else 50000)
        if key == "extension":
            return self.validate_extension(entity_type, value)
        if key in {"source_url", "endpoint_url"}:
            text = check_text(value, key, 2000)
            if text and not re.match(r"^(https?://|/)", text):
                raise _bad(f"{key} 必须是 http(s) 地址或站内路径。")
            return text
        if key == "status":
            if value not in GLOSSARY_TERM_STATUSES:
                raise _bad("术语状态无效。")
            return value
        if key == "domain_type":
            if value not in DOMAIN_TYPES:
                raise _bad("域类型无效。")
            return value
        if key == "team_type":
            if value not in TEAM_TYPES:
                raise _bad("团队类型无效。")
            return value
        if key == "table_type":
            if value not in TABLE_TYPES:
                raise _bad("表类型无效。")
            return value
        if key in {"mutually_exclusive", "disabled", "is_admin", "is_bot", "is_joinable", "enabled"}:
            return _bool(value)
        if key in {"synonyms", "related_terms", "cleanup_policies", "file_formats", "charts", "data_models", "dashboards", "partitions"}:
            items = _as_list(value)
            if len(items) > 500:
                raise _bad(f"{key} 最多 500 项。")
            return [check_text(str(item), key, 512) for item in items if str(item).strip()]
        if key == "email":
            text = check_text(value, "邮箱", 256)
            if text and "@" not in text:
                raise _bad("邮箱格式无效。")
            return text
        if key in {"row_count", "size_in_bytes", "number_of_objects", "size", "replication_factor", "retention_size", "retention_time", "maximum_message_size", "minimum_in_sync_replicas", "concurrency"}:
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as error:
                raise _bad(f"{key} 必须是整数。") from error
        if key == "target_value":
            try:
                return float(value)
            except (TypeError, ValueError) as error:
                raise _bad("KPI 目标值必须是数字。") from error
        if key in {"start_date", "end_date"}:
            text = check_text(value, key, 10)
            if text and not ISO_DATE.match(text):
                raise _bad(f"{key} 必须是 YYYY-MM-DD 格式。")
            return text
        if key in {"references", "tasks", "ml_features", "ml_hyper_parameters", "datasets", "metrics", "table_constraints", "destinations"}:
            items = _as_list(value)
            if len(items) > 2000:
                raise _bad(f"{key} 最多 2000 项。")
            if not all(isinstance(item, dict) for item in items):
                raise _bad(f"{key} 的每一项必须是对象。")
            return [_clean(item) for item in items]
        if key in {"connection", "ingestion", "ml_store", "style", "filters", "request_schema", "response_schema", "search_index_settings", "pipeline_status", "storage"}:
            if value is None:
                return {}
            if not isinstance(value, dict):
                raise _bad(f"{key} 必须是对象。")
            if key == "connection":
                reject_secrets(value)
            return _clean(value)
        if isinstance(value, (dict, list)):
            return _clean(value)
        if value is None:
            return ""
        return check_text(str(value), key, 4096)

    def _columns(self, value: Any, *, existing: list[dict[str, Any]], merge: bool) -> list[dict[str, Any]]:
        items = _as_list(value)
        if len(items) > MAX_COLUMNS:
            raise _bad(f"字段最多 {MAX_COLUMNS} 个。")
        current = {column.get("name"): dict(column) for column in existing}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise _bad("字段定义必须是对象。")
            name = check_name(item.get("name"), "字段名")
            if name in seen:
                raise _bad(f"字段 {name} 重复。")
            seen.add(name)
            base = current.get(name, {}) if merge else {}
            column = {**base}
            column["name"] = name
            if "display_name" in item:
                column["display_name"] = check_text(item.get("display_name"), "字段显示名称", 256)
            if "description" in item:
                column["description"] = check_text(item.get("description"), "字段描述", 20000)
            raw_type = item.get("data_type_display") or item.get("type") or item.get("data_type")
            if raw_type is not None:
                column["data_type_display"] = check_text(str(raw_type), "字段类型", 256)
                column["data_type"] = normalize_data_type(item.get("data_type") or raw_type)
                column["data_length"] = data_length(raw_type)
            column.setdefault("data_type_display", "")
            column.setdefault("data_type", normalize_data_type(column.get("data_type_display")))
            column.setdefault("description", "")
            column.setdefault("display_name", "")
            if "constraint" in item:
                column["constraint"] = check_text(item.get("constraint"), "字段约束", 32)
            elif "primary_key" in item:
                column["constraint"] = "PRIMARY_KEY" if _bool(item.get("primary_key")) else ("NULL" if _bool(item.get("nullable", True)) else "NOT_NULL")
            elif "nullable" in item and "constraint" not in column:
                column["constraint"] = "NULL" if _bool(item.get("nullable")) else "NOT_NULL"
            column.setdefault("constraint", "")
            column["ordinal_position"] = index + 1
            if isinstance(item.get("children"), list):
                column["children"] = self._columns(item["children"], existing=base.get("children") or [], merge=merge)
            if "tags" in item and item["tags"] is not None:
                column["_tags"] = item["tags"]
            result.append(column)
        if merge:
            # A patch that names only some columns keeps the others in their positions.
            named = {column["name"] for column in result}
            if named and len(named) < len(current):
                merged = []
                for column in existing:
                    merged.append(next((c for c in result if c["name"] == column["name"]), column))
                for column in result:
                    if column["name"] not in current:
                        merged.append(column)
                result = merged
                for index, column in enumerate(result):
                    column["ordinal_position"] = index + 1
        return result

    def _check_kpi(self, document: dict[str, Any]) -> None:
        chart = document.get("chart") or ""
        if chart not in KPI_CHARTS:
            raise _bad("KPI 图表必须是：" + "、".join(KPI_CHARTS))
        metric = document.get("metric_type") or KPI_CHARTS[chart]["metric_type"]
        if metric not in {"PERCENTAGE", "NUMBER"}:
            raise _bad("KPI 指标类型必须是 PERCENTAGE 或 NUMBER。")
        document["metric_type"] = metric
        if document.get("target_value") is None:
            raise _bad("KPI 必须填写目标值。")
        if metric == "PERCENTAGE" and not 0 <= float(document["target_value"]) <= 100:
            raise _bad("百分比 KPI 的目标值必须在 0–100 之间。")
        if not document.get("start_date") or not document.get("end_date"):
            raise _bad("KPI 必须填写开始日期和结束日期。")
        if document["start_date"] > document["end_date"]:
            raise _bad("KPI 的结束日期不能早于开始日期。")

    # =====================================================================================
    # update
    # =====================================================================================
    def update(self, entity_type: str, ref: str, patch: dict[str, Any], *, user: str | None = None, source: str = "manual") -> dict[str, Any]:
        check_entity_type(entity_type)
        if not isinstance(patch, dict):
            raise _bad("请求体必须是对象。")
        user = user or self.user
        with self._lock:
            row = self.row(entity_type, ref)
            if row["deleted"]:
                raise _bad("已删除的对象不能修改，请先恢复。")
            if "name" in patch and patch["name"] not in (None, row["name"]):
                raise _bad("名称请通过重命名接口修改。")
            previous = dict(row["json"])
            document = self._document(entity_type, patch, current=previous)
            document["name"] = row["name"]
            column_tags = self._pop_column_tags(document)
            self._validate_assignments(entity_type, patch)
            for tags in column_tags.values():
                self._resolve_tags(tags)
            change = change_description(previous, document)
            assignment_changes = self._apply_assignments(row, patch, user, creating=False)
            change["fields_updated"].extend(assignment_changes["updated"])
            change["fields_added"].extend(assignment_changes["added"])
            change["fields_deleted"].extend(assignment_changes["deleted"])
            for column_name, tags in column_tags.items():
                target = child_fqn(row["fqn"], column_name)
                before = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([target]))
                self._write_tags(target, tags, user)
                after = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([target]))
                if before != after:
                    change["fields_updated"].append({"name": f"columns.{column_name}.tags", "old_value": before, "new_value": after})
            if not _has_change(change):
                return self.decorate(self.store.get_entity(entity_type, id=row["id"]) or row)
            self._commit_change(row, document, change, user, previous)
            return self.decorate(self.store.get_entity(entity_type, id=row["id"]) or row)

    @staticmethod
    def _pop_column_tags(document: dict[str, Any]) -> dict[str, Any]:
        tags: dict[str, Any] = {}
        for column in _columns_of(document):
            if "_tags" in column:
                tags[column["name"]] = column.pop("_tags")
        return tags

    def _commit_change(self, row: dict[str, Any], document: dict[str, Any], change: dict[str, Any], user: str, previous: dict[str, Any]) -> None:
        change = scrub_change(change)
        stamp = now_ms()
        previous_version = row["version"]
        version = next_version(previous_version, is_major_change(change))
        change["previous_version"] = previous_version
        latest = self.store.get_entity(row["entity_type"], id=row["id"]) or row
        updated = {
            **latest,
            "display_name": document.get("display_name", ""),
            "description": document.get("description", ""),
            "version": version,
            "updated_at": stamp,
            "updated_by": user,
            "search_text": search_text(document, row["entity_type"], row["fqn"]),
            "json": document,
        }
        self.store.update_entity(updated)
        self.store.save_version(row["id"], version, document, change, stamp, user)
        self._emit(
            {
                "event_type": "entityUpdated",
                "entity_type": row["entity_type"],
                "entity_id": row["id"],
                "entity_fqn": row["fqn"],
                "entity_name": updated["display_name"] or row["name"],
                "user_name": user,
                "ts": stamp,
                "previous_version": previous_version,
                "current_version": version,
                "change": {**change, "summary": describe_change(change), "kinds": _change_kinds(change)},
            }
        )

    def touch(self, row: dict[str, Any], document: dict[str, Any], *, user: str, change: dict[str, Any] | None = None) -> dict[str, Any]:
        """Replace a document wholesale (ingestion), versioning only when something changed."""
        with self._lock:
            latest = self.store.get_entity(row["entity_type"], id=row["id"]) or row
            previous = dict(latest["json"])
            merged = {**previous, **document}
            merged["name"] = latest["name"]
            diff = change or change_description(previous, merged)
            if not _has_change(diff):
                if merged != previous:
                    # Volatile, untracked fields (row counts, crawl details) are kept current
                    # without a new version, as OpenMetadata keeps profiles out of versions.
                    self.store.update_entity({**latest, "json": merged, "search_text": search_text(merged, latest["entity_type"], latest["fqn"])})
                    return self.store.get_entity(row["entity_type"], id=row["id"]) or latest
                return latest
            self._commit_change(latest, merged, diff, user, previous)
            return self.store.get_entity(row["entity_type"], id=row["id"]) or latest

    # ----- assignments ----------------------------------------------------------------------
    def _apply_assignments(self, row: dict[str, Any], payload: dict[str, Any], user: str, *, creating: bool) -> dict[str, list[dict[str, Any]]]:
        changes: dict[str, list[dict[str, Any]]] = {"added": [], "updated": [], "deleted": []}

        def note(name: str, before: Any, after: Any) -> None:
            if before == after:
                return
            if not before:
                changes["added"].append({"name": name, "new_value": after})
            elif not after:
                changes["deleted"].append({"name": name, "old_value": before})
            else:
                changes["updated"].append({"name": name, "old_value": before, "new_value": after})

        entity_type = row["entity_type"]
        if "owners" in payload and payload["owners"] is not None:
            before = [o["fqn"] for o in self._owners_for([row["id"]]).get(row["id"], [])]
            self._set_owners(row, payload["owners"])
            after = [o["fqn"] for o in self._owners_for([row["id"]]).get(row["id"], [])]
            note("owners", before, after)
        if "tags" in payload and payload["tags"] is not None:
            before = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([row["fqn"]]))
            self._write_tags(row["fqn"], payload["tags"], user, entity_row=row)
            after = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([row["fqn"]]))
            note("tags", before, after)
        if "tier" in payload:
            before = row["tier"]
            self._set_tier(row, payload["tier"], user)
            after = (self.store.get_entity(entity_type, id=row["id"]) or row)["tier"]
            note("tier", before, after)
        if "domain" in payload or "domain_fqn" in payload:
            target = payload.get("domain_fqn", payload.get("domain"))
            before = row["domain_fqn"]
            self._set_domain(row, target)
            note("domain", before, (self.store.get_entity(entity_type, id=row["id"]) or row)["domain_fqn"])
        if "data_products" in payload and payload["data_products"] is not None:
            before = sorted(p["fqn"] for p in self._products_for([row["id"]]).get(row["id"], []))
            self._set_products(row, payload["data_products"])
            after = sorted(p["fqn"] for p in self._products_for([row["id"]]).get(row["id"], []))
            note("data_products", before, after)
        if "experts" in payload and payload["experts"] is not None and entity_type in {"domain", "dataProduct"}:
            before = sorted(self._people(row["id"], RELATION_EXPERT))
            self._set_people(row, payload["experts"], RELATION_EXPERT, "专家")
            note("experts", before, sorted(self._people(row["id"], RELATION_EXPERT)))
        if "reviewers" in payload and payload["reviewers"] is not None and entity_type in {"glossary", "glossaryTerm"}:
            before = sorted(self._people(row["id"], RELATION_REVIEWER))
            self._set_people(row, payload["reviewers"], RELATION_REVIEWER, "审核者")
            note("reviewers", before, sorted(self._people(row["id"], RELATION_REVIEWER)))
        if "related_terms" in payload and entity_type == "glossaryTerm":
            self._set_related_terms(row, payload["related_terms"])
        if "teams" in payload and entity_type == "user" and payload["teams"] is not None:
            before = sorted(self._teams_of(row["id"]))
            self._set_user_teams(row, payload["teams"])
            note("teams", before, sorted(self._teams_of(row["id"])))
        if "users" in payload and entity_type == "team" and payload["users"] is not None:
            before = sorted(self._members_of(row["id"]))
            self._set_team_users(row, payload["users"])
            note("users", before, sorted(self._members_of(row["id"])))
        if "parent_team" in payload and entity_type == "team":
            self._set_team_parent_ref(row, payload["parent_team"])
        if "assets" in payload and payload["assets"] is not None and entity_type in {"dataProduct", "domain"}:
            before = len(self.assets_of(row))
            self._set_assets(row, payload["assets"])
            note("assets", before, len(self.assets_of(row)))
        if creating:
            return {"added": [], "updated": [], "deleted": []}
        return changes

    def _validate_assignments(self, entity_type: str, payload: dict[str, Any]) -> None:
        """Resolve every reference an assignment names before anything is written.

        The store commits each statement on its own, so a missing owner or a
        mutually exclusive tag discovered halfway through a mutation would leave
        a created entity, or half an update, behind while the caller is told
        the request failed.
        """
        def find(kind: str, ref: Any, what: str) -> None:
            text = str(ref or "")
            row = (self.store.get_entity(kind, id=text) if _looks_like_id(text) else None) or self.store.get_entity(kind, fqn=text)
            if row is None or row["deleted"]:
                raise MetadataError(404, f"{what}不存在：{ref}")

        if payload.get("owners") is not None:
            self._people_refs(payload["owners"], allow_teams=True, what="所有者")
        if payload.get("tags") is not None:
            self._resolve_tags(payload["tags"])
        if payload.get("tier"):
            tier = str(payload["tier"]).strip()
            find("tag", tier if tier.startswith(TIER_CLASSIFICATION + ".") else child_fqn(TIER_CLASSIFICATION, tier), "分级标签")
        target = payload.get("domain_fqn", payload.get("domain"))
        if target:
            find("domain", target.get("fqn") if isinstance(target, dict) else target, "数据域")
        for item in _as_list(payload.get("data_products"))[:100]:
            find("dataProduct", (item.get("fqn") or item.get("id")) if isinstance(item, dict) else item, "数据产品")
        if payload.get("experts") is not None and entity_type in {"domain", "dataProduct"}:
            self._people_refs(payload["experts"], allow_teams=False, what="专家")
        if payload.get("reviewers") is not None and entity_type in {"glossary", "glossaryTerm"}:
            self._people_refs(payload["reviewers"], allow_teams=False, what="审核者")
        if entity_type == "glossaryTerm":
            for item in _as_list(payload.get("related_terms"))[:100]:
                ref = item.get("fqn") if isinstance(item, dict) else item
                term = self.store.get_entity("glossaryTerm", fqn=str(ref)) if isinstance(ref, str) else None
                if term is None or term["deleted"]:
                    raise MetadataError(404, f"相关术语不存在：{ref}")
        if entity_type == "user":
            for item in _as_list(payload.get("teams"))[:50]:
                find("team", (item.get("fqn") or item.get("name") or item.get("id")) if isinstance(item, dict) else item, "团队")
        if entity_type == "team":
            if payload.get("users") is not None:
                self._people_refs(payload["users"], allow_teams=False, what="成员")
            if payload.get("parent_team"):
                find("team", payload["parent_team"], "上级团队")
        if entity_type in {"dataProduct", "domain"}:
            for item in _as_list(payload.get("assets"))[:2000]:
                kind, ref = (item.get("entity_type") or item.get("type"), item.get("fqn") or item.get("id")) if isinstance(item, dict) else (None, item)
                if (self.row(kind, str(ref)) if kind else self.resolve_asset(str(ref))) is None:
                    raise MetadataError(404, f"资产不存在：{ref}")

    def _set_owners(self, row: dict[str, Any], owners: Any) -> None:
        refs = self._people_refs(owners, allow_teams=True, what="所有者")
        self.store.remove_relationships(to_id=row["id"], relation=RELATION_OWNS)
        for owner in refs:
            self.store.add_relationship(owner["id"], owner["entity_type"], row["id"], row["entity_type"], RELATION_OWNS)

    def _people_refs(self, value: Any, *, allow_teams: bool, what: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in _as_list(value)[:50]:
            if isinstance(item, str):
                kind, ref = None, item
            elif isinstance(item, dict):
                kind = item.get("type") or item.get("entity_type")
                ref = item.get("id") or item.get("fqn") or item.get("name")
            else:
                raise _bad(f"{what}格式无效。")
            if not isinstance(ref, str) or not ref:
                raise _bad(f"{what}缺少名称。")
            candidates = [kind] if kind in {"user", "team"} else (["user", "team"] if allow_teams else ["user"])
            found = None
            for candidate in candidates:
                found = (self.store.get_entity(candidate, id=ref) if _looks_like_id(ref) else None) or self.store.get_entity(candidate, fqn=ref)
                if found:
                    break
            if found is None or found["deleted"]:
                raise MetadataError(404, f"{what}不存在：{ref}")
            if not allow_teams and found["entity_type"] == "team":
                raise _bad(f"{what}必须是用户。")
            if found["id"] not in seen:
                seen.add(found["id"])
                refs.append(found)
        return refs

    def _set_people(self, row: dict[str, Any], value: Any, relation: str, what: str) -> None:
        refs = self._people_refs(value, allow_teams=False, what=what)
        self.store.remove_relationships(to_id=row["id"], relation=relation)
        for person in refs:
            self.store.add_relationship(person["id"], "user", row["id"], row["entity_type"], relation)

    def _people(self, entity_id: str, relation: str) -> list[str]:
        relations = self.store.relationships(to_id=entity_id, relation=relation)
        rows = self.store.get_entities([r["from_id"] for r in relations])
        return [r["fqn"] for r in rows]

    def _people_summaries(self, entity_id: str, relation: str) -> list[dict[str, Any]]:
        relations = self.store.relationships(to_id=entity_id, relation=relation)
        return [_summary(r) for r in self.store.get_entities([r["from_id"] for r in relations]) if not r["deleted"]]

    def _set_related_terms(self, row: dict[str, Any], value: Any) -> None:
        refs = []
        for item in _as_list(value)[:100]:
            ref = item.get("fqn") if isinstance(item, dict) else item
            term = self.store.get_entity("glossaryTerm", fqn=str(ref)) if isinstance(ref, str) else None
            if term is None or term["deleted"]:
                raise MetadataError(404, f"相关术语不存在：{ref}")
            if term["id"] != row["id"]:
                refs.append(term)
        self.store.remove_relationships(from_id=row["id"], relation=RELATION_RELATED)
        for term in refs:
            self.store.add_relationship(row["id"], "glossaryTerm", term["id"], "glossaryTerm", RELATION_RELATED)

    def _set_user_teams(self, row: dict[str, Any], value: Any) -> None:
        teams = []
        for item in _as_list(value)[:50]:
            ref = item.get("fqn") or item.get("name") or item.get("id") if isinstance(item, dict) else item
            team = (self.store.get_entity("team", id=str(ref)) if _looks_like_id(str(ref)) else None) or self.store.get_entity("team", fqn=str(ref))
            if team is None or team["deleted"]:
                raise MetadataError(404, f"团队不存在：{ref}")
            teams.append(team)
        self.store.remove_relationships(to_id=row["id"], relation=RELATION_MEMBER)
        for team in teams:
            self.store.add_relationship(team["id"], "team", row["id"], "user", RELATION_MEMBER)

    def _set_team_users(self, row: dict[str, Any], value: Any) -> None:
        users = self._people_refs(value, allow_teams=False, what="成员")
        self.store.remove_relationships(from_id=row["id"], relation=RELATION_MEMBER)
        for user in users:
            self.store.add_relationship(row["id"], "team", user["id"], "user", RELATION_MEMBER)

    def _set_team_parent_ref(self, row: dict[str, Any], value: Any) -> None:
        self.store.remove_relationships(to_id=row["id"], relation=RELATION_TEAM_PARENT)
        if not value:
            return
        parent = (self.store.get_entity("team", id=str(value)) if _looks_like_id(str(value)) else None) or self.store.get_entity("team", fqn=str(value))
        if parent is None or parent["deleted"]:
            raise MetadataError(404, f"上级团队不存在：{value}")
        if parent["id"] == row["id"]:
            raise _bad("团队不能是自己的上级。")
        self.store.add_relationship(parent["id"], "team", row["id"], "team", RELATION_TEAM_PARENT)

    def _teams_of(self, user_id: str) -> list[str]:
        relations = self.store.relationships(to_id=user_id, relation=RELATION_MEMBER)
        return [r["fqn"] for r in self.store.get_entities([r["from_id"] for r in relations])]

    def _members_of(self, team_id: str) -> list[str]:
        relations = self.store.relationships(from_id=team_id, relation=RELATION_MEMBER)
        return [r["fqn"] for r in self.store.get_entities([r["to_id"] for r in relations])]

    def _set_tier(self, row: dict[str, Any], value: Any, user: str) -> None:
        tier = str(value).strip() if value else ""
        if tier and not tier.startswith(TIER_CLASSIFICATION + "."):
            tier = child_fqn(TIER_CLASSIFICATION, tier)
        current = [t for t in self.store.tags_for_targets([row["fqn"]]) if t["source"] == "classification" and t["tag_fqn"].startswith(TIER_CLASSIFICATION + ".")]
        for tag in current:
            self.store.remove_tag("classification", tag["tag_fqn"], row["fqn"])
        if tier:
            tag_row = self.store.get_entity("tag", fqn=tier)
            if tag_row is None or tag_row["deleted"]:
                raise MetadataError(404, f"分级标签不存在：{tier}")
            self.store.set_tag("classification", tier, row["fqn"], "manual", "confirmed")
        latest = self.store.get_entity(row["entity_type"], id=row["id"]) or row
        self.store.update_entity({**latest, "tier": tier})

    def _set_domain(self, row: dict[str, Any], target: Any) -> None:
        domain_fqn = ""
        if target:
            ref = target.get("fqn") if isinstance(target, dict) else str(target)
            domain = (self.store.get_entity("domain", id=ref) if _looks_like_id(ref) else None) or self.store.get_entity("domain", fqn=ref)
            if domain is None or domain["deleted"]:
                raise MetadataError(404, f"数据域不存在：{ref}")
            domain_fqn = domain["fqn"]
        latest = self.store.get_entity(row["entity_type"], id=row["id"]) or row
        self.store.update_entity({**latest, "domain_fqn": domain_fqn})

    def _set_products(self, row: dict[str, Any], value: Any) -> None:
        products = []
        for item in _as_list(value)[:100]:
            ref = item.get("fqn") or item.get("id") if isinstance(item, dict) else str(item)
            product = (self.store.get_entity("dataProduct", id=str(ref)) if _looks_like_id(str(ref)) else None) or self.store.get_entity("dataProduct", fqn=str(ref))
            if product is None or product["deleted"]:
                raise MetadataError(404, f"数据产品不存在：{ref}")
            products.append(product)
        self.store.remove_relationships(to_id=row["id"], relation=RELATION_PRODUCT)
        for product in products:
            self.store.add_relationship(product["id"], "dataProduct", row["id"], row["entity_type"], RELATION_PRODUCT)

    def _set_assets(self, row: dict[str, Any], value: Any) -> None:
        assets = []
        for item in _as_list(value)[:2000]:
            if isinstance(item, dict):
                entity_type, ref = item.get("entity_type") or item.get("type"), item.get("fqn") or item.get("id")
            else:
                entity_type, ref = None, str(item)
            asset = self.row(entity_type, str(ref)) if entity_type else self.resolve_asset(str(ref))
            if asset is None:
                raise MetadataError(404, f"资产不存在：{ref}")
            assets.append(asset)
        if row["entity_type"] == "dataProduct":
            self.store.remove_relationships(from_id=row["id"], relation=RELATION_PRODUCT)
            for asset in assets:
                self.store.add_relationship(row["id"], "dataProduct", asset["id"], asset["entity_type"], RELATION_PRODUCT)
        else:
            for asset in assets:
                latest = self.store.get_entity(asset["entity_type"], id=asset["id"]) or asset
                self.store.update_entity({**latest, "domain_fqn": row["fqn"]})

    def assets_of(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        if row["entity_type"] == "dataProduct":
            relations = self.store.relationships(from_id=row["id"], relation=RELATION_PRODUCT)
            rows = self.store.get_entities([r["to_id"] for r in relations])
            return [r for r in rows if not r["deleted"]]
        if row["entity_type"] == "domain":
            return self.store.all_entities(None, deleted=False, limit=5000) and [
                r for r in self.store.all_entities(None, deleted=False, limit=20000) if r["domain_fqn"] == row["fqn"] and r["entity_type"] not in {"domain", "dataProduct"}
            ]
        return []

    # ----- tags ---------------------------------------------------------------------------
    def set_tags(self, target_fqn: str, tags: Any, *, user: str | None = None, target_type: str | None = None) -> dict[str, Any]:
        """Replace the tags and glossary terms of an entity or one of its columns."""
        user = user or self.user
        with self._lock:
            entity = None
            if target_type:
                entity = self.row(target_type, target_fqn)
                column = None
            else:
                entity = self.resolve_asset(target_fqn)
                column = None
                if entity is None:
                    resolved = self.resolve_column(target_fqn)
                    if resolved is None:
                        raise MetadataError(404, f"目标不存在：{target_fqn}")
                    entity, column = resolved
            if entity["deleted"]:
                raise _bad("已删除的对象不能打标签。")
            before = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([target_fqn]))
            self._write_tags(target_fqn, tags, user, entity_row=entity if column is None else None)
            after = sorted(t["tag_fqn"] for t in self.store.tags_for_targets([target_fqn]))
            if before != after:
                name = "tags" if column is None else f"columns.{column['name']}.tags"
                change = {"fields_added": [], "fields_updated": [{"name": name, "old_value": before, "new_value": after}], "fields_deleted": []}
                latest = self.store.get_entity(entity["entity_type"], id=entity["id"]) or entity
                self._commit_change(latest, dict(latest["json"]), change, user, dict(latest["json"]))
            return self.decorate(self.store.get_entity(entity["entity_type"], id=entity["id"]) or entity)

    def _write_tags(self, target_fqn: str, tags: Any, user: str, *, entity_row: dict[str, Any] | None = None) -> None:
        wanted = self._resolve_tags(tags)
        self.store.clear_tags(target_fqn)
        tier = ""
        for tag in wanted:
            self.store.set_tag(tag["source"], tag["tag_fqn"], target_fqn, tag["label_type"], tag["state"])
            if tag["source"] == "classification" and tag["tag_fqn"].startswith(TIER_CLASSIFICATION + "."):
                tier = tag["tag_fqn"]
        if entity_row is not None:
            latest = self.store.get_entity(entity_row["entity_type"], id=entity_row["id"]) or entity_row
            if latest["tier"] != tier:
                self.store.update_entity({**latest, "tier": tier})

    def _resolve_tags(self, tags: Any) -> list[dict[str, Any]]:
        """Validate tag and term labels, including mutual exclusion; writes nothing."""
        items = _as_list(tags)
        if len(items) > MAX_TAGS:
            raise _bad(f"标签最多 {MAX_TAGS} 个。")
        wanted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if isinstance(item, str):
                item = {"tag_fqn": item}
            if not isinstance(item, dict):
                raise _bad("标签格式无效。")
            tag_fqn = item.get("tag_fqn") or item.get("fqn") or item.get("tagFQN")
            if not isinstance(tag_fqn, str) or not tag_fqn:
                raise _bad("标签缺少 tag_fqn。")
            if tag_fqn in seen:
                continue
            seen.add(tag_fqn)
            source = item.get("source")
            tag_row = None
            if source in (None, "classification"):
                tag_row = self.store.get_entity("tag", fqn=tag_fqn)
                if tag_row:
                    source = "classification"
            if tag_row is None and source in (None, "glossary"):
                tag_row = self.store.get_entity("glossaryTerm", fqn=tag_fqn)
                if tag_row:
                    source = "glossary"
            if tag_row is None or tag_row["deleted"]:
                raise MetadataError(404, f"标签或术语不存在：{tag_fqn}")
            if tag_row["json"].get("disabled"):
                raise _bad(f"标签 {tag_fqn} 已停用。")
            label_type = item.get("label_type") or "manual"
            state = item.get("state") or "confirmed"
            if label_type not in LABEL_TYPES or state not in TAG_STATES:
                raise _bad("标签的 label_type 或 state 无效。")
            wanted.append({"tag_fqn": tag_fqn, "source": source, "label_type": label_type, "state": state, "row": tag_row})
        self._check_mutual_exclusion(wanted)
        return wanted

    def _check_mutual_exclusion(self, wanted: list[dict[str, Any]]) -> None:
        roots: dict[str, list[str]] = {}
        for tag in wanted:
            root = split_fqn(tag["tag_fqn"])[0]
            roots.setdefault(f"{tag['source']}:{root}", []).append(tag["tag_fqn"])
        for key, fqns in roots.items():
            if len(fqns) < 2:
                continue
            source, root = key.split(":", 1)
            root_row = self.store.get_entity("classification" if source == "classification" else "glossary", fqn=build_fqn(root))
            if root_row and root_row["json"].get("mutually_exclusive"):
                raise _bad(f"{'分类' if source == 'classification' else '术语库'} {root} 是互斥的，同一对象只能使用其中一个{'标签' if source == 'classification' else '术语'}：{'、'.join(fqns)}")
        # A mutually exclusive tag/term also forbids siblings under it.
        for tag in wanted:
            parent = fqn_parent(tag["tag_fqn"])
            if not parent or "." not in tag["tag_fqn"]:
                continue
            parent_row = self.store.get_entity("tag" if tag["source"] == "classification" else "glossaryTerm", fqn=parent)
            if parent_row and parent_row["json"].get("mutually_exclusive"):
                siblings = [t["tag_fqn"] for t in wanted if t is not tag and fqn_parent(t["tag_fqn"]) == parent]
                if siblings:
                    raise _bad(f"{parent} 下的标签互斥，不能同时使用 {tag['tag_fqn']} 与 {'、'.join(siblings)}")

    def tag_usage(self, tag_fqn: str) -> dict[str, Any]:
        usages = self.store.tag_targets(tag_fqn)
        targets = [u["target_fqn"] for u in usages]
        rows = {r["fqn"]: r for r in self.store.get_entities_by_fqn(targets)}
        items = []
        for usage in usages:
            row = rows.get(usage["target_fqn"])
            if row is None:
                resolved = self.resolve_column(usage["target_fqn"])
                if resolved:
                    row = resolved[0]
                    items.append({**usage, "entity": _summary(row), "column": leaf_name(usage["target_fqn"])})
                    continue
                items.append({**usage, "entity": None})
            else:
                items.append({**usage, "entity": _summary(row)})
        return {"tag_fqn": tag_fqn, "items": items, "total": len(items)}

    # ----- followers --------------------------------------------------------------------
    def follow(self, entity_type: str, ref: str, user_name: str, *, follow: bool = True) -> dict[str, Any]:
        with self._lock:
            row = self.row(entity_type, ref)
            user = self.store.get_entity("user", fqn=build_fqn(user_name))
            if user is None:
                raise MetadataError(404, f"用户不存在：{user_name}")
            if follow:
                self.store.add_relationship(user["id"], "user", row["id"], entity_type, RELATION_FOLLOWS)
            else:
                self.store.remove_relationship(user["id"], row["id"], RELATION_FOLLOWS)
            return self.decorate(row)

    # =====================================================================================
    # rename / delete / restore
    # =====================================================================================
    def rename(self, entity_type: str, ref: str, new_name: Any, *, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        with self._lock:
            row = self.row(entity_type, ref)
            name = check_name(new_name, "名称")
            if name == row["name"]:
                return self.decorate(row)
            new_fqn = child_fqn(row["parent_fqn"], name) if row["parent_fqn"] else build_fqn(name)
            if self.store.get_entity(entity_type, fqn=new_fqn) is not None:
                raise MetadataError(409, f"{type_label(entity_type)} {new_fqn} 已存在。")
            old_fqn = row["fqn"]
            descendants = self._descendants(row)
            stamp = now_ms()
            for child in descendants:
                self.store.update_entity(
                    {
                        **child,
                        "fqn": new_fqn + child["fqn"][len(old_fqn):],
                        "parent_fqn": new_fqn + child["parent_fqn"][len(old_fqn):] if child["parent_fqn"].startswith(old_fqn) else child["parent_fqn"],
                        "service_fqn": new_fqn + child["service_fqn"][len(old_fqn):] if child["service_fqn"] == old_fqn or child["service_fqn"].startswith(old_fqn + ".") else child["service_fqn"],
                        "domain_fqn": new_fqn + child["domain_fqn"][len(old_fqn):] if entity_type == "domain" and (child["domain_fqn"] == old_fqn or child["domain_fqn"].startswith(old_fqn + ".")) else child["domain_fqn"],
                    }
                )
            document = dict(row["json"])
            document["name"] = name
            self.store.update_entity(
                {
                    **row,
                    "name": name,
                    "fqn": new_fqn,
                    "service_fqn": new_fqn if row["service_fqn"] == old_fqn else row["service_fqn"],
                    "updated_at": stamp,
                    "updated_by": user,
                    "search_text": search_text(document, entity_type, new_fqn),
                    "json": document,
                }
            )
            self.store.rename_target_prefix(old_fqn, new_fqn)
            if entity_type in {"tag", "classification", "glossaryTerm", "glossary"}:
                self.store.rename_tag_prefix(old_fqn, new_fqn)
                if entity_type in {"tag", "classification"}:
                    self._rename_tier_references(old_fqn, new_fqn)
            if entity_type == "domain":
                for other in self.store.all_entities(None, deleted=None, limit=20000):
                    if other["domain_fqn"] == old_fqn or other["domain_fqn"].startswith(old_fqn + "."):
                        self.store.update_entity({**other, "domain_fqn": new_fqn + other["domain_fqn"][len(old_fqn):]})
            latest = self.store.get_entity(entity_type, id=row["id"]) or row
            self._emit(
                {
                    "event_type": "entityUpdated",
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "entity_fqn": new_fqn,
                    "entity_name": latest["display_name"] or name,
                    "user_name": user,
                    "ts": stamp,
                    "previous_version": row["version"],
                    "current_version": row["version"],
                    "change": {"summary": f"重命名：{old_fqn} → {new_fqn}", "fields_updated": [{"name": "name", "old_value": row["name"], "new_value": name}], "kinds": ["custom"]},
                }
            )
            return self.decorate(latest)

    def _rename_tier_references(self, old: str, new: str) -> None:
        for other in self.store.all_entities(None, deleted=None, limit=20000):
            if other["tier"] and (other["tier"] == old or other["tier"].startswith(old + ".")):
                self.store.update_entity({**other, "tier": new + other["tier"][len(old):]})

    def _descendants(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Entities nested under ``row`` in its own family (service or governance tree)."""
        entity_type = row["entity_type"]
        candidates = self.store.all_entities(None, deleted=None, fqn_prefix=row["fqn"], limit=50000)
        family = FAMILY.get(entity_type)
        result = []
        for candidate in candidates:
            if candidate["id"] == row["id"]:
                continue
            if family is not None:
                if candidate["entity_type"] not in family:
                    continue
            elif candidate["service_fqn"] != row["service_fqn"] and candidate["service_fqn"] != row["fqn"]:
                continue
            result.append(candidate)
        return result

    def delete(self, entity_type: str, ref: str, *, hard: bool = False, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        with self._lock:
            row = self.row(entity_type, ref)
            if entity_type == "team" and row["name"] == DEFAULT_TEAM:
                raise _bad("根团队不能删除。")
            if entity_type == "user" and row["name"] == SYSTEM_USER:
                raise _bad("平台用户不能删除。")
            if entity_type in {"classification", "tag"} and row["json"].get("provider") == "system" and hard:
                raise _bad("系统分类和标签不能永久删除。")
            descendants = self._descendants(row)
            stamp = now_ms()
            if hard:
                for child in [*descendants, row]:
                    self._hard_delete(child)
                self._emit(
                    {
                        "event_type": "entityDeleted",
                        "entity_type": entity_type,
                        "entity_id": row["id"],
                        "entity_fqn": row["fqn"],
                        "entity_name": row["display_name"] or row["name"],
                        "user_name": user,
                        "ts": stamp,
                        "previous_version": row["version"],
                        "current_version": row["version"],
                        "change": {"summary": f"永久删除，含 {len(descendants)} 个下级对象"},
                    }
                )
                return {"deleted": True, "hard": True, "fqn": row["fqn"], "children": len(descendants)}
            self.store.set_deleted([r["id"] for r in [*descendants, row]], True, stamp, user)
            self._emit(
                {
                    "event_type": "entitySoftDeleted",
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "entity_fqn": row["fqn"],
                    "entity_name": row["display_name"] or row["name"],
                    "user_name": user,
                    "ts": stamp,
                    "previous_version": row["version"],
                    "current_version": row["version"],
                    "change": {"summary": f"删除（可恢复），含 {len(descendants)} 个下级对象"},
                }
            )
            return {"deleted": True, "hard": False, "fqn": row["fqn"], "children": len(descendants)}

    def _hard_delete(self, row: dict[str, Any]) -> None:
        entity_type = row["entity_type"]
        self.store.delete_entity(row["id"])
        self.store.delete_target_prefix(row["fqn"])
        self.store.delete_edges_of(row["fqn"])
        if entity_type in {"tag", "classification", "glossaryTerm", "glossary"}:
            self.store.delete_tag_prefix(row["fqn"])
            if entity_type in {"tag", "classification"}:
                for other in self.store.all_entities(None, deleted=None, limit=20000):
                    if other["tier"] and (other["tier"] == row["fqn"] or other["tier"].startswith(row["fqn"] + ".")):
                        self.store.update_entity({**other, "tier": ""})
        if entity_type == "domain":
            for other in self.store.all_entities(None, deleted=None, limit=20000):
                if other["domain_fqn"] == row["fqn"] or other["domain_fqn"].startswith(row["fqn"] + "."):
                    self.store.update_entity({**other, "domain_fqn": ""})

    def restore(self, entity_type: str, ref: str, *, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        with self._lock:
            row = self.row(entity_type, ref)
            if not row["deleted"]:
                return self.decorate(row)
            parent = self.store.get_entity(None, fqn=row["parent_fqn"]) if row["parent_fqn"] else None
            if parent is not None and parent["deleted"]:
                raise _bad(f"上级对象 {parent['fqn']} 仍处于已删除状态，请先恢复它。")
            descendants = self._descendants(row)
            stamp = now_ms()
            self.store.set_deleted([r["id"] for r in [*descendants, row]], False, stamp, user)
            self._emit(
                {
                    "event_type": "entityRestored",
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "entity_fqn": row["fqn"],
                    "entity_name": row["display_name"] or row["name"],
                    "user_name": user,
                    "ts": stamp,
                    "previous_version": row["version"],
                    "current_version": row["version"],
                    "change": {"summary": f"恢复，含 {len(descendants)} 个下级对象"},
                }
            )
            return self.decorate(self.store.get_entity(entity_type, id=row["id"]) or row)

    # =====================================================================================
    # reading collections
    # =====================================================================================
    def get(self, entity_type: str | None, ref: str, *, include_deleted: bool = True) -> dict[str, Any]:
        return self.decorate(self.row(entity_type, ref, include_deleted=include_deleted))

    def list(self, entity_type: str | list[str] | None, **filters: Any) -> dict[str, Any]:
        if isinstance(entity_type, str):
            check_entity_type(entity_type)
        listing = self.store.list_entities(entity_type, **filters)
        listing["items"] = self.decorate_many(listing["items"], full=False)
        return listing

    def children(self, entity_type: str, ref: str, *, deleted: bool | None = False) -> dict[str, Any]:
        row = self.row(entity_type, ref)
        child_types = TREE_CHILDREN.get(entity_type, [])
        groups = []
        for child_type in child_types:
            listing = self.store.list_entities(child_type, parent_fqn=row["fqn"], deleted=deleted, page=1, size=MAX_PAGE)
            items = self.decorate_many(listing["items"], full=False)
            counts = self.store.count_children([item["fqn"] for item in items])
            for item in items:
                item["children_count"] = counts.get(item["fqn"], 0)
            groups.append({"entity_type": child_type, "label": type_label(child_type), "items": items, "total": listing["total"]})
        return {"entity": _summary(row), "groups": groups}

    def tree(self, *, deleted: bool = False) -> dict[str, Any]:
        """Root of the 元数据资产 tree: service categories → services with child counts."""
        counts = self.store.counts_by_type(deleted=False)
        categories = []
        for key, spec in SERVICE_CATEGORIES.items():
            services = self.store.list_entities(spec["entity_type"], deleted=deleted, page=1, size=MAX_PAGE)["items"]
            items = self.decorate_many(services, full=False)
            child_counts = self.store.count_children([item["fqn"] for item in items])
            for item in items:
                item["children_count"] = child_counts.get(item["fqn"], 0)
            categories.append(
                {
                    "id": key,
                    "label": spec["label"],
                    "entity_type": spec["entity_type"],
                    "services": items,
                    # Databases and schemas are containers; the tree counts the assets inside them.
                    "asset_count": sum(counts.get(child, 0) for child in spec["children"] if child in DATA_ASSET_TYPES),
                }
            )
        governance = [
            {"id": "glossary", "label": "术语库", "entity_type": "glossary", "count": counts.get("glossary", 0)},
            {"id": "classification", "label": "分类", "entity_type": "classification", "count": counts.get("classification", 0)},
            {"id": "domain", "label": "数据域", "entity_type": "domain", "count": counts.get("domain", 0)},
            {"id": "dataProduct", "label": "数据产品", "entity_type": "dataProduct", "count": counts.get("dataProduct", 0)},
            {"id": "semanticModel", "label": "语义模型", "entity_type": "semanticModel", "count": counts.get("semanticModel", 0)},
        ]
        return {"categories": categories, "governance": governance, "counts": counts}

    def summary(self) -> dict[str, Any]:
        counts = self.store.counts_by_type(deleted=False)
        deleted = self.store.counts_by_type(deleted=True)
        assets = sum(counts.get(t, 0) for t in DATA_ASSET_TYPES)
        return {
            "counts": counts,
            "deleted_counts": deleted,
            "assets": assets,
            "services": sum(counts.get(t, 0) for t in SERVICE_TYPES),
            "by_category": {
                key: sum(counts.get(child, 0) for child in spec["children"] if child in DATA_ASSET_TYPES)
                for key, spec in SERVICE_CATEGORIES.items()
            },
            "lineage_edges": sum(self.store.edge_counts().values()),
            "tags": counts.get("tag", 0),
            "glossary_terms": counts.get("glossaryTerm", 0),
        }

    def search(
        self, q: str | None = None, *, entity_types: list[str] | None = None, service_type: str | None = None,
        service_fqn: str | None = None, owner: str | None = None, tag: str | None = None, tier: str | None = None,
        domain: str | None = None, deleted: bool = False, sort: str = "name", page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        types = entity_types or DATA_ASSET_TYPES
        for entity_type in types:
            check_entity_type(entity_type)
        ids: list[str] | None = None
        if owner:
            owner_row = self.store.get_entity("user", fqn=owner) or self.store.get_entity("team", fqn=owner)
            if owner_row is None:
                return {"items": [], "total": 0, "page": page, "size": size, "facets": {}}
            ids = [r["to_id"] for r in self.store.relationships(from_id=owner_row["id"], relation=RELATION_OWNS)]
        fqns: list[str] | None = None
        if tag:
            targets = {u["target_fqn"] for u in self.store.tag_targets(tag)}
            fqns = sorted({self._entity_fqn_of_target(t) for t in targets})
        filters = {
            "service_type": service_type, "service_fqn": service_fqn, "deleted": deleted, "q": q, "tier": tier,
            "domain_fqn": domain, "ids": ids, "fqns": fqns, "sort": sort,
        }
        listing = self.store.list_entities(types, **filters, page=page, size=size)
        total = listing["total"]
        # Facets describe the whole result, so they come from a capped sample of it, not from this page.
        sample = listing["items"] if page == 1 and total <= size else self.store.list_entities(types, **filters, page=1, size=MAX_PAGE_FACETS)["items"]
        items = self.decorate_many(listing["items"], full=False)
        return {"items": items, "total": total, "page": page, "size": size, "facets": self._facets(sample), "truncated": total > len(sample)}

    def _entity_fqn_of_target(self, target: str) -> str:
        if self.store.get_entities_by_fqn([target]):
            return target
        resolved = self.resolve_column(target)
        return resolved[0]["fqn"] if resolved else target

    def _facets(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        def bucket(values: Iterable[str]) -> list[dict[str, Any]]:
            counts: dict[str, int] = {}
            for value in values:
                if value:
                    counts[value] = counts.get(value, 0) + 1
            return [{"value": key, "count": count} for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:30]]

        owners = self._owners_for([row["id"] for row in rows[:FACET_LIMIT]])
        tags = self.store.tags_for_targets([row["fqn"] for row in rows[:FACET_LIMIT]])
        return {
            "entity_type": bucket(row["entity_type"] for row in rows),
            "service_type": bucket(row["service_type"] for row in rows),
            "service": bucket(row["service_fqn"] for row in rows if row["entity_type"] not in SERVICE_TYPES),
            "tier": bucket(row["tier"] for row in rows),
            "domain": bucket(row["domain_fqn"] for row in rows),
            "owner": bucket(owner["fqn"] for items in owners.values() for owner in items),
            "tag": bucket(t["tag_fqn"] for t in tags),
        }

    # =====================================================================================
    # versions and feed
    # =====================================================================================
    def versions(self, entity_type: str, ref: str) -> dict[str, Any]:
        row = self.row(entity_type, ref)
        items = []
        for version in self.store.list_versions(row["id"]):
            change = scrub_change(version["change"])
            items.append(
                {
                    "version": version["version"],
                    "updated_at": version["updated_at"],
                    "updated_by": version["updated_by"],
                    "change": change,
                    "summary": describe_change(change) if _has_change(change) else ("创建" if version["version"] == 0.1 else "无字段变更"),
                }
            )
        return {"entity": _summary(row), "versions": items}

    def version(self, entity_type: str, ref: str, version: Any) -> dict[str, Any]:
        row = self.row(entity_type, ref)
        try:
            number = round(float(version), 1)
        except (TypeError, ValueError) as error:
            raise _bad("版本号无效。") from error
        stored = self.store.get_version(row["id"], number)
        if stored is None:
            raise MetadataError(404, f"版本 {number} 不存在。")
        snapshot = {**row, "version": number, "json": stored["json"], "updated_at": stored["updated_at"], "updated_by": stored["updated_by"], "description": stored["json"].get("description", ""), "display_name": stored["json"].get("display_name", "")}
        return {**self.decorate(snapshot, full=False), "change": scrub_change(stored["change"]), "is_version": True}

    def feed(self, *, entity_fqn: str | None = None, entity_type: str | None = None, event_type: str | None = None, user_name: str | None = None, include_children: bool = False, page: int = 1, size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        listing = self.store.list_events(entity_fqn=entity_fqn, entity_type=entity_type, event_type=event_type, user_name=user_name, include_children=include_children, page=page, size=size)
        for item in listing["items"]:
            item["event_label"] = EVENT_LABELS.get(item["event_type"], item["event_type"])
            item["type_label"] = type_label(item["entity_type"])
            change = scrub_change(item.get("change") or {})
            item["change"] = change
            item["summary"] = change.get("summary") or describe_change(change)
        return listing

    # ----- threads --------------------------------------------------------------------------
    def create_thread(self, payload: dict[str, Any], *, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        thread_type = payload.get("thread_type") or "Conversation"
        if thread_type not in THREAD_TYPES:
            raise _bad("会话类型无效。")
        about = payload.get("about_fqn") or ""
        about_row = None
        if about:
            about_row = self.row(payload.get("about_type"), about) if payload.get("about_type") else (self.resolve_asset(about) or (self.resolve_column(about) or [None])[0])
            if about_row is None:
                raise MetadataError(404, f"对象不存在：{about}")
        message = check_text(payload.get("message"), "内容", 20000)
        if not message and thread_type != "Task":
            raise _bad("内容不能为空。")
        stamp = now_ms()
        extra: dict[str, Any] = {"posts": []}
        if thread_type == "Task":
            task_type = payload.get("task_type") or "RequestDescription"
            if task_type not in TASK_TYPES:
                raise _bad("任务类型无效。")
            extra["task"] = {
                "task_type": task_type,
                "status": "Open",
                "assignees": [p["fqn"] for p in self._people_refs(payload.get("assignees"), allow_teams=True, what="处理人")],
                "suggestion": payload.get("suggestion"),
                "column": payload.get("column") or "",
            }
        if thread_type == "Announcement":
            extra["announcement"] = {
                "start_ts": int(payload.get("start_ts") or stamp),
                "end_ts": int(payload.get("end_ts") or stamp + 7 * 86400000),
                "title": check_text(payload.get("title"), "标题", 256),
            }
        thread = {
            "id": new_id(),
            "thread_type": thread_type,
            "about_fqn": about_row["fqn"] if about_row and not about else about,
            "about_type": about_row["entity_type"] if about_row else "",
            "message": message,
            "created_by": user,
            "created_at": stamp,
            "updated_at": stamp,
            "resolved": False,
            "json": extra,
        }
        self.store.insert_thread(thread)
        return self._thread_view(thread)

    def add_post(self, thread_id: str, message: Any, *, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise MetadataError(404, "会话不存在。")
        text = check_text(message, "回复", 20000)
        if not text:
            raise _bad("回复不能为空。")
        posts = thread["json"].setdefault("posts", [])
        posts.append({"id": new_id(), "message": text, "from": user, "ts": now_ms()})
        thread["updated_at"] = now_ms()
        self.store.update_thread(thread)
        return self._thread_view(thread)

    def resolve_task(self, thread_id: str, *, accept: bool, message: Any = None, user: str | None = None) -> dict[str, Any]:
        user = user or self.user
        with self._lock:
            thread = self.store.get_thread(thread_id)
            if thread is None:
                raise MetadataError(404, "会话不存在。")
            task = thread["json"].get("task")
            if thread["thread_type"] != "Task" or not task:
                raise _bad("只有任务可以处理。")
            if task.get("status") != "Open":
                raise _bad("任务已经关闭。")
            if accept and thread["about_fqn"]:
                suggestion = task.get("suggestion")
                target = self.store.get_entity(thread["about_type"], fqn=thread["about_fqn"]) if thread["about_type"] else self.resolve_asset(thread["about_fqn"])
                if target is None:
                    raise MetadataError(404, "任务关联的对象已不存在。")
                if task["task_type"] in {"RequestDescription", "UpdateDescription"}:
                    if task.get("column"):
                        self.update(target["entity_type"], target["id"], {"columns": [{"name": task["column"], "description": str(suggestion or "")}]}, user=user)
                    else:
                        self.update(target["entity_type"], target["id"], {"description": str(suggestion or "")}, user=user)
                else:
                    tags = suggestion if isinstance(suggestion, list) else _as_list(suggestion)
                    target_fqn = child_fqn(target["fqn"], task["column"]) if task.get("column") else target["fqn"]
                    self.set_tags(target_fqn, tags, user=user)
            task["status"] = "Closed" if accept else "Rejected"
            task["resolved_by"] = user
            task["resolved_at"] = now_ms()
            if message:
                thread["json"].setdefault("posts", []).append({"id": new_id(), "message": check_text(message, "回复", 20000), "from": user, "ts": now_ms()})
            thread["resolved"] = True
            thread["updated_at"] = now_ms()
            self.store.update_thread(thread)
            return self._thread_view(thread)

    def close_thread(self, thread_id: str, *, user: str | None = None) -> dict[str, Any]:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise MetadataError(404, "会话不存在。")
        thread["resolved"] = True
        thread["updated_at"] = now_ms()
        if thread["json"].get("task"):
            thread["json"]["task"]["status"] = "Closed"
        self.store.update_thread(thread)
        return self._thread_view(thread)

    def delete_thread(self, thread_id: str) -> dict[str, Any]:
        if self.store.get_thread(thread_id) is None:
            raise MetadataError(404, "会话不存在。")
        self.store.delete_thread(thread_id)
        return {"deleted": True}

    def threads(self, **filters: Any) -> dict[str, Any]:
        listing = self.store.list_threads(**filters)
        listing["items"] = [self._thread_view(t) for t in listing["items"]]
        return listing

    @staticmethod
    def _thread_view(thread: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": thread["id"],
            "thread_type": thread["thread_type"],
            "about_fqn": thread["about_fqn"],
            "about_type": thread["about_type"],
            "message": thread["message"],
            "created_by": thread["created_by"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "resolved": thread["resolved"],
            "posts": thread["json"].get("posts", []),
            "task": thread["json"].get("task"),
            "announcement": thread["json"].get("announcement"),
        }

    # =====================================================================================
    # custom properties
    # =====================================================================================
    def property_definitions(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        if entity_type:
            check_entity_type(entity_type)
        return self.store.properties(entity_type)

    def define_property(self, entity_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        check_entity_type(entity_type)
        name = check_name(payload.get("name"), "属性名")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z", name):
            raise _bad("属性名只能包含字母、数字和下划线，且以字母开头。")
        property_type = payload.get("property_type") or "string"
        if property_type not in CUSTOM_PROPERTY_TYPES:
            raise _bad("属性类型无效。")
        config = payload.get("config") or {}
        if not isinstance(config, dict):
            raise _bad("属性配置必须是对象。")
        if property_type == "enum":
            values = [check_text(str(v), "枚举值", 128) for v in _as_list(config.get("values")) if str(v).strip()]
            if not values:
                raise _bad("枚举属性必须提供取值列表。")
            config = {"values": values, "multi_select": _bool(config.get("multi_select"))}
        elif property_type == "entityReference":
            types = [t for t in _as_list(config.get("entity_types")) if t in ENTITY_TYPES]
            config = {"entity_types": types or DATA_ASSET_TYPES}
        else:
            config = {}
        definition = {
            "entity_type": entity_type,
            "name": name,
            "display_name": check_text(payload.get("display_name"), "显示名称", 256),
            "property_type": property_type,
            "description": check_text(payload.get("description"), "描述", 4000),
            "config": config,
            "created_at": now_ms(),
        }
        self.store.upsert_property(definition)
        return definition

    def remove_property(self, entity_type: str, name: str) -> dict[str, Any]:
        check_entity_type(entity_type)
        if not self.store.delete_property(entity_type, name):
            raise MetadataError(404, "自定义属性不存在。")
        return {"deleted": True}

    def validate_extension(self, entity_type: str, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _bad("自定义属性必须是对象。")
        definitions = {d["name"]: d for d in self.store.properties(entity_type)}
        result: dict[str, Any] = {}
        for key, item in value.items():
            definition = definitions.get(key)
            if definition is None:
                raise _bad(f"{type_label(entity_type)}没有定义自定义属性 {key}。")
            if item is None or item == "":
                continue
            kind = definition["property_type"]
            try:
                if kind in {"string", "markdown"}:
                    result[key] = check_text(str(item), key, 20000)
                elif kind == "integer":
                    result[key] = int(item)
                elif kind == "number":
                    result[key] = float(item)
                elif kind == "boolean":
                    result[key] = _bool(item)
                elif kind == "date":
                    if not ISO_DATE.match(str(item)):
                        raise ValueError
                    result[key] = str(item)
                elif kind == "enum":
                    allowed = definition["config"].get("values", [])
                    chosen = _as_list(item) if definition["config"].get("multi_select") else [item]
                    if any(c not in allowed for c in chosen):
                        raise ValueError
                    result[key] = chosen if definition["config"].get("multi_select") else chosen[0]
                elif kind == "entityReference":
                    ref = item if isinstance(item, dict) else {"fqn": str(item)}
                    found = self.resolve_asset(str(ref.get("fqn") or "")) if not ref.get("entity_type") else self.store.get_entity(ref["entity_type"], fqn=str(ref.get("fqn") or ""))
                    if found is None:
                        raise ValueError
                    result[key] = {"entity_type": found["entity_type"], "fqn": found["fqn"], "name": found["name"]}
            except (TypeError, ValueError) as error:
                raise _bad(f"自定义属性 {key} 的取值不符合类型 {kind}。") from error
        return result

    # =====================================================================================
    # governance helpers for the screens
    # =====================================================================================
    def glossaries(self, *, deleted: bool = False) -> list[dict[str, Any]]:
        rows = self.store.list_entities("glossary", deleted=deleted, page=1, size=MAX_PAGE)["items"]
        counts = self.store.counts_by_type(deleted=False)
        items = self.decorate_many(rows, full=False)
        term_rows = self.store.all_entities("glossaryTerm", deleted=False, limit=50000)
        usage = self.store.tag_usage_counts("glossary")
        for item in items:
            terms = [t for t in term_rows if t["service_fqn"] == item["fqn"]]
            item["term_count"] = len(terms)
            item["usage_count"] = sum(usage.get(t["fqn"], 0) for t in terms)
            item["reviewers"] = self._people_summaries(item["id"], RELATION_REVIEWER)
        return items and sorted(items, key=lambda i: i["name"]) or ([] if counts else [])

    def glossary_terms(self, glossary_fqn: str, *, deleted: bool = False) -> dict[str, Any]:
        glossary = self.row("glossary", glossary_fqn)
        rows = self.store.all_entities("glossaryTerm", deleted=deleted, service_fqn=glossary["fqn"], limit=50000)
        usage = self.store.tag_usage_counts("glossary")
        items = self.decorate_many(rows, full=False)
        related = {r["from_id"]: r for r in []}
        for item in items:
            item["usage_count"] = usage.get(item["fqn"], 0)
            item["reviewers"] = self._people_summaries(item["id"], RELATION_REVIEWER)
            relations = self.store.relationships(from_id=item["id"], relation=RELATION_RELATED)
            item["related_terms"] = [_summary(r) for r in self.store.get_entities([r["to_id"] for r in relations])]
        del related
        return {"glossary": _summary(glossary), "terms": items, "tree": _tree(items, glossary["fqn"])}

    def classifications(self, *, deleted: bool = False) -> list[dict[str, Any]]:
        rows = self.store.list_entities("classification", deleted=deleted, page=1, size=MAX_PAGE)["items"]
        tags = self.store.all_entities("tag", deleted=False, limit=50000)
        usage = self.store.tag_usage_counts("classification")
        items = self.decorate_many(rows, full=False)
        for item in items:
            mine = [t for t in tags if t["service_fqn"] == item["fqn"]]
            item["tag_count"] = len(mine)
            item["usage_count"] = sum(usage.get(t["fqn"], 0) for t in mine)
        return sorted(items, key=lambda i: i["name"])

    def classification_tags(self, classification_fqn: str, *, deleted: bool = False) -> dict[str, Any]:
        classification = self.row("classification", classification_fqn)
        rows = self.store.all_entities("tag", deleted=deleted, service_fqn=classification["fqn"], limit=50000)
        usage = self.store.tag_usage_counts("classification")
        items = self.decorate_many(rows, full=False)
        for item in items:
            item["usage_count"] = usage.get(item["fqn"], 0)
        return {"classification": _summary(classification), "tags": items, "tree": _tree(items, classification["fqn"])}

    def domains(self, *, deleted: bool = False) -> dict[str, Any]:
        rows = self.store.all_entities("domain", deleted=deleted, limit=50000)
        products = self.store.all_entities("dataProduct", deleted=deleted, limit=50000)
        everything = self.store.all_entities(None, deleted=False, limit=50000)
        asset_counts: dict[str, int] = {}
        for row in everything:
            if row["domain_fqn"] and row["entity_type"] not in {"domain", "dataProduct"}:
                asset_counts[row["domain_fqn"]] = asset_counts.get(row["domain_fqn"], 0) + 1
        items = self.decorate_many(rows, full=False)
        product_items = self.decorate_many(products, full=False)
        product_assets = {}
        for product in products:
            product_assets[product["fqn"]] = len(self.store.relationships(from_id=product["id"], relation=RELATION_PRODUCT))
        for item in items:
            item["asset_count"] = asset_counts.get(item["fqn"], 0)
            item["experts"] = self._people_summaries(item["id"], RELATION_EXPERT)
            item["data_products"] = [p for p in product_items if p.get("domain") and p["domain"]["fqn"] == item["fqn"]]
        for product in product_items:
            product["asset_count"] = product_assets.get(product["fqn"], 0)
            product["experts"] = self._people_summaries(product["id"], RELATION_EXPERT)
        return {"domains": items, "tree": _tree(items, ""), "data_products": product_items}

    def domain_assets(self, entity_type: str, ref: str) -> dict[str, Any]:
        row = self.row(entity_type, ref)
        assets = self.assets_of(row)
        return {"entity": _summary(row), "items": self.decorate_many(assets[:MAX_PAGE], full=False), "total": len(assets)}

    def teams(self) -> dict[str, Any]:
        teams = self.store.all_entities("team", deleted=False, limit=5000)
        users = self.store.all_entities("user", deleted=False, limit=20000)
        members = self.store.relationships(relation=RELATION_MEMBER)
        parents = self.store.relationships(relation=RELATION_TEAM_PARENT)
        owns = self.store.relationships(relation=RELATION_OWNS)
        user_rows = {u["id"]: u for u in users}
        team_items = self.decorate_many(teams, full=False)
        user_items = self.decorate_many(users, full=False)
        for team in team_items:
            team["users"] = [_summary(user_rows[m["to_id"]]) for m in members if m["from_id"] == team["id"] and m["to_id"] in user_rows]
            team["user_count"] = len(team["users"])
            team["parent_team"] = next((_summary(t) for t in teams for p in parents if p["to_id"] == team["id"] and p["from_id"] == t["id"]), None)
            team["owns_count"] = sum(1 for o in owns if o["from_id"] == team["id"])
        for user in user_items:
            user["teams"] = [_summary(t) for t in teams for m in members if m["to_id"] == user["id"] and m["from_id"] == t["id"]]
            user["owns_count"] = sum(1 for o in owns if o["from_id"] == user["id"])
        return {"teams": team_items, "users": user_items}

    def owned_by(self, entity_type: str, ref: str) -> dict[str, Any]:
        row = self.row(entity_type, ref)
        relations = self.store.relationships(from_id=row["id"], relation=RELATION_OWNS)
        rows = [r for r in self.store.get_entities([r["to_id"] for r in relations]) if not r["deleted"]]
        follows = self.store.relationships(from_id=row["id"], relation=RELATION_FOLLOWS) if entity_type == "user" else []
        followed = [r for r in self.store.get_entities([r["to_id"] for r in follows]) if not r["deleted"]]
        return {"entity": _summary(row), "owns": self.decorate_many(rows[:MAX_PAGE], full=False), "follows": self.decorate_many(followed[:MAX_PAGE], full=False)}

    # =====================================================================================
    # export / import
    # =====================================================================================
    def export(self, *, entity_types: list[str] | None = None, service_fqn: str | None = None, include_deleted: bool = False) -> dict[str, Any]:
        types = entity_types or [t for t in ENTITY_TYPES if t not in {"kpi", "eventSubscription"}]
        for entity_type in types:
            check_entity_type(entity_type)
        rows = self.store.all_entities(types, deleted=None if include_deleted else False, service_fqn=service_fqn, limit=MAX_BUNDLE)
        ids = [r["id"] for r in rows]
        owners = self._owners_for(ids)
        entities = []
        fqns = [r["fqn"] for r in rows]
        for row in rows:
            document = dict(row["json"])
            if row["entity_type"] == "eventSubscription":
                document["destinations"] = mask_destinations(document.get("destinations"))
            entities.append(
                {
                    **document,
                    "entity_type": row["entity_type"],
                    "name": row["name"],
                    "fqn": row["fqn"],
                    "parent_fqn": row["parent_fqn"],
                    "service_type": row["service_type"],
                    "tier": row["tier"] or None,
                    "domain_fqn": row["domain_fqn"] or None,
                    "deleted": row["deleted"],
                    "version": row["version"],
                    "owners": [{"type": o["entity_type"], "name": o["name"]} for o in owners.get(row["id"], [])],
                }
            )
        tags = []
        for row in rows:
            tags.extend(self.store.tags_for_prefix(row["fqn"]))
        edges = [e for e in self.store.edges() if e["from_fqn"] in set(fqns) or e["to_fqn"] in set(fqns)] if not service_fqn else [
            e for e in self.store.edges() if e["from_fqn"].startswith(service_fqn) or e["to_fqn"].startswith(service_fqn)
        ]
        return {"format": "lattice", "version": 1, "exported_at": now_ms(), "entities": entities, "tags": tags, "lineage": edges}

    def import_bundle(self, bundle: Any, *, fmt: str = "lattice", user: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        user = user or self.user
        if not isinstance(bundle, dict):
            raise _bad("导入内容必须是对象。")
        if fmt == "openmetadata":
            bundle = from_openmetadata(bundle)
        entities = _as_list(bundle.get("entities"))
        if len(entities) > MAX_BUNDLE:
            raise _bad(f"一次最多导入 {MAX_BUNDLE} 个实体。")
        order = list(ENTITY_TYPES)
        entities = [e for e in entities if isinstance(e, dict)]
        entities.sort(key=lambda e: (order.index(e.get("entity_type")) if e.get("entity_type") in order else 999, str(e.get("fqn") or e.get("name") or "")))
        summary = {"created": 0, "updated": 0, "skipped": 0, "tags": 0, "lineage": 0, "errors": []}
        with self._lock:
            for item in entities:
                try:
                    entity_type = check_entity_type(item.get("entity_type"))
                    if entity_type in {"kpi", "eventSubscription"}:
                        summary["skipped"] += 1
                        continue
                    fqn = item.get("fqn")
                    if fqn and not item.get("parent_fqn") and parent_types(entity_type):
                        item["parent_fqn"] = fqn_parent(str(fqn))
                    if not item.get("name") and fqn:
                        item["name"] = leaf_name(str(fqn))
                    payload = {k: v for k, v in item.items() if k not in {"entity_type", "fqn", "deleted", "version", "service_fqn", "tier", "domain_fqn"}}
                    if item.get("domain_fqn"):
                        payload["domain"] = item["domain_fqn"]
                    if item.get("tier"):
                        payload["tier"] = item["tier"]
                    target_fqn = str(fqn) if fqn else (child_fqn(str(item.get("parent_fqn") or ""), str(item["name"])) if item.get("parent_fqn") else build_fqn(str(item["name"])))
                    existing = self.store.get_entity(entity_type, fqn=target_fqn)
                    if dry_run:
                        summary["updated" if existing else "created"] += 1
                        continue
                    if existing:
                        payload.pop("name", None)
                        for key in ("parent_fqn", "parent", "glossary_fqn", "classification_fqn", "service_fqn"):
                            payload.pop(key, None)
                        self.update(entity_type, existing["id"], payload, user=user)
                        summary["updated"] += 1
                    else:
                        self.create(entity_type, payload, user=user)
                        summary["created"] += 1
                except (MetadataError, MetadataStoreError, ValueError) as error:
                    summary["errors"].append(f"{item.get('entity_type')} {item.get('fqn') or item.get('name')}: {error}")
                    if len(summary["errors"]) > 200:
                        break
            for usage in _as_list(bundle.get("tags")):
                if not isinstance(usage, dict) or dry_run:
                    continue
                try:
                    self.store.set_tag(usage.get("source") or "classification", usage["tag_fqn"], usage["target_fqn"], usage.get("label_type") or "manual", usage.get("state") or "confirmed")
                    summary["tags"] += 1
                except (KeyError, MetadataStoreError) as error:
                    summary["errors"].append(f"tag {usage}: {error}")
            for edge in _as_list(bundle.get("lineage")):
                if not isinstance(edge, dict) or dry_run:
                    continue
                if not edge.get("from_fqn") or not edge.get("to_fqn"):
                    continue
                try:
                    self.store.upsert_edge({**edge, "source": edge.get("source") or "import"})
                    summary["lineage"] += 1
                except MetadataStoreError as error:
                    summary["errors"].append(f"lineage {edge.get('from_fqn')}→{edge.get('to_fqn')}: {error}")
        return summary

    # =====================================================================================
    # context for AI and 智能问数
    # =====================================================================================
    def schema_annotations(self, datasource_id: str) -> dict[str, dict[str, Any]]:
        """Descriptions, tags and terms of the tables ingested from one Lattice data source."""
        services = [s for s in self.store.all_entities("databaseService", deleted=False, limit=1000) if s["json"].get("datasource_id") == datasource_id]
        if not services:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for service in services:
            for table in self.store.all_entities("table", deleted=False, service_fqn=service["fqn"], limit=5000):
                document = table["json"]
                key = f"{document.get('source_schema') or split_fqn(table['fqn'])[-2]}.{table['name']}"
                tags = self.store.tags_for_prefix(table["fqn"])
                result[key] = {
                    "fqn": table["fqn"],
                    "description": table["description"],
                    "display_name": table["display_name"],
                    "columns": {c["name"]: c.get("description", "") for c in _columns_of(document) if c.get("description")},
                    "tags": sorted({t["tag_fqn"] for t in tags if t["target_fqn"] == table["fqn"]}),
                    "column_tags": {leaf_name(t["target_fqn"]): t["tag_fqn"] for t in tags if t["target_fqn"] != table["fqn"]},
                }
        return result

    # =====================================================================================
    # public helpers shared with the lineage, ingestion, insight and context modules
    # =====================================================================================
    def owners_for(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return self._owners_for(ids)

    def people_of(self, entity_id: str, relation: str) -> list[dict[str, Any]]:
        return self._people_summaries(entity_id, relation)

    def emit(self, event: dict[str, Any]) -> None:
        """Record a change event and notify the listeners (alerts, insights)."""
        self._emit(event)

    def lock(self) -> threading.RLock:
        return self._lock

    # =====================================================================================
    # events
    # =====================================================================================
    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("id", new_id())
        with self._event_lock:
            # Strictly increasing within the process, so the feed's order is the order of events.
            stamp = int(event.get("ts") or now_ms())
            if stamp <= self._last_event_ts:
                stamp = self._last_event_ts + 1
            self._last_event_ts = stamp
            event["ts"] = stamp
        self.store.add_event(event)
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a broken listener must not fail the mutation
                continue


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
MAX_PAGE = 200
MAX_PAGE_FACETS = 200
FAMILY: dict[str, set[str] | None] = {
    "glossary": {"glossaryTerm"},
    "glossaryTerm": {"glossaryTerm"},
    "classification": {"tag"},
    "tag": {"tag"},
    "domain": {"domain"},
    "dataProduct": set(),
    "team": set(),
    "user": set(),
    "kpi": set(),
    "eventSubscription": set(),
    "semanticModel": set(),
}
KPI_CHARTS: dict[str, dict[str, Any]] = {
    "percentage_of_entities_with_description_by_type": {"label": "带有描述信息的数据资产占比", "metric_type": "PERCENTAGE", "field": "description_percent"},
    "percentage_of_entities_with_owner_by_type": {"label": "带有所有者信息的数据资产占比", "metric_type": "PERCENTAGE", "field": "owner_percent"},
    "percentage_of_entities_with_tier_by_type": {"label": "带有分级信息的数据资产占比", "metric_type": "PERCENTAGE", "field": "tier_percent"},
    "total_entities_by_type": {"label": "数据资产总数", "metric_type": "NUMBER", "field": "assets"},
}


def _looks_like_id(value: str) -> bool:
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", value))


def _columns_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    columns = document.get("columns")
    return [c for c in columns if isinstance(c, dict)] if isinstance(columns, list) else []


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "entity_type": row["entity_type"],
        "type_label": type_label(row["entity_type"]),
        "name": row["name"],
        "display_name": row["display_name"] or row["name"],
        "fqn": row["fqn"],
        "service_type": row["service_type"],
        "deleted": row["deleted"],
    }


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        raise _bad("对象嵌套过深。")
    if isinstance(value, dict):
        if len(value) > 500:
            raise _bad("对象字段过多。")
        return {check_text(str(k), "键", 256): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 5000:
            raise _bad("列表过长。")
        return [_clean(v, depth + 1) for v in value]
    if isinstance(value, str):
        return check_text(value, "取值", 50000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4096]


def _has_change(change: dict[str, Any]) -> bool:
    return bool(change.get("fields_added") or change.get("fields_updated") or change.get("fields_deleted"))


def _change_kinds(change: dict[str, Any]) -> list[str]:
    kinds: set[str] = set()
    for key in ("fields_added", "fields_updated", "fields_deleted"):
        for item in change.get(key, []):
            name = str(item.get("name", ""))
            head = name.split(".")[0]
            if head == "columns":
                kinds.add("columns")
                if name.endswith(".tags"):
                    kinds.add("tags")
                if name.endswith(".description"):
                    kinds.add("description")
            elif head in {"display_name"}:
                kinds.add("displayName")
            elif head in CHANGE_KINDS:
                kinds.add(head)
            elif head == "glossary_terms":
                kinds.add("glossaryTerms")
            else:
                kinds.add("custom")
    return sorted(kinds)


def _tree(items: list[dict[str, Any]], root_fqn: str) -> list[dict[str, Any]]:
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_parent.setdefault(item["parent_fqn"], []).append(item)

    def build(parent: str, depth: int) -> list[dict[str, Any]]:
        nodes = []
        for item in sorted(by_parent.get(parent, []), key=lambda i: i["name"]):
            children = build(item["fqn"], depth + 1) if depth < 12 else []
            nodes.append({**item, "children": children})
        return nodes

    return build(root_fqn, 0)


# ---------------------------------------------------------------------------------------------
# OpenMetadata JSON import
# ---------------------------------------------------------------------------------------------
_OM_TYPE_ALIASES = {
    "databaseservice": "databaseService", "database": "database", "databaseschema": "databaseSchema",
    "table": "table", "storedprocedure": "storedProcedure", "messagingservice": "messagingService",
    "topic": "topic", "dashboardservice": "dashboardService", "dashboard": "dashboard", "chart": "chart",
    "dashboarddatamodel": "dashboardDataModel", "pipelineservice": "pipelineService", "pipeline": "pipeline",
    "mlmodelservice": "mlmodelService", "mlmodel": "mlmodel", "storageservice": "storageService",
    "container": "container", "searchservice": "searchService", "searchindex": "searchIndex",
    "apiservice": "apiService", "apicollection": "apiCollection", "apiendpoint": "apiEndpoint",
    "metadataservice": "metadataService", "glossary": "glossary", "glossaryterm": "glossaryTerm",
    "classification": "classification", "tag": "tag", "domain": "domain", "dataproduct": "dataProduct",
    "team": "team", "user": "user",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def from_openmetadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenMetadata API/export JSON (camelCase) into a Lattice bundle."""
    entities_in = payload.get("entities") if isinstance(payload.get("entities"), list) else payload.get("data")
    if entities_in is None and payload.get("entityType"):
        entities_in = [payload]
    entities: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    for item in _as_list(entities_in):
        if not isinstance(item, dict):
            continue
        entity_type = _OM_TYPE_ALIASES.get(str(item.get("entityType") or item.get("entity_type") or "").lower())
        if not entity_type:
            continue
        fqn = item.get("fullyQualifiedName") or item.get("fqn")
        converted: dict[str, Any] = {
            "entity_type": entity_type,
            "name": item.get("name"),
            "fqn": fqn,
            "display_name": item.get("displayName") or "",
            "description": item.get("description") or "",
        }
        parent_ref = item.get("parent") or item.get("glossary") or item.get("classification") or item.get("databaseSchema") or item.get("database") or item.get("service")
        if isinstance(parent_ref, dict) and parent_ref.get("fullyQualifiedName"):
            converted["parent_fqn"] = parent_ref["fullyQualifiedName"]
        elif fqn and parent_types(entity_type):
            converted["parent_fqn"] = fqn_parent(str(fqn))
        if item.get("serviceType"):
            converted["service_type"] = item["serviceType"]
        if isinstance(item.get("owners"), list):
            converted["owners"] = [{"type": o.get("type"), "name": o.get("name")} for o in item["owners"] if isinstance(o, dict)]
        elif isinstance(item.get("owner"), dict):
            converted["owners"] = [{"type": item["owner"].get("type"), "name": item["owner"].get("name")}]
        if isinstance(item.get("tags"), list) and fqn:
            for tag in item["tags"]:
                if isinstance(tag, dict) and tag.get("tagFQN"):
                    tags.append({"source": "glossary" if str(tag.get("source", "")).lower() == "glossary" else "classification", "tag_fqn": tag["tagFQN"], "target_fqn": fqn, "label_type": str(tag.get("labelType", "manual")).lower(), "state": str(tag.get("state", "confirmed")).lower()})
        if isinstance(item.get("domain"), dict) and item["domain"].get("fullyQualifiedName"):
            converted["domain"] = item["domain"]["fullyQualifiedName"]
        for key in ("tableType", "algorithm", "status", "synonyms", "mutuallyExclusive", "domainType", "teamType", "email", "isAdmin", "provider", "schemaText", "schemaType", "partitions", "replicationFactor", "dashboardType", "sourceUrl", "chartType", "scheduleInterval", "endpointURL", "requestMethod", "prefix", "fileFormats", "indexType", "retentionPeriod", "project"):
            if key in item and item[key] is not None:
                converted[_snake(key) if key != "endpointURL" else "endpoint_url"] = item[key]
        if isinstance(item.get("references"), list):
            converted["references"] = [{"name": r.get("name"), "endpoint": r.get("endpoint")} for r in item["references"] if isinstance(r, dict)]
        if isinstance(item.get("relatedTerms"), list):
            converted["related_terms"] = [r.get("fullyQualifiedName") for r in item["relatedTerms"] if isinstance(r, dict) and r.get("fullyQualifiedName")]
        columns = item.get("columns") or item.get("schemaFields") or item.get("fields") or (item.get("messageSchema") or {}).get("schemaFields") or (item.get("dataModel") or {}).get("columns")
        if isinstance(columns, list):
            converted["columns"] = [_om_column(c, fqn, tags) for c in columns if isinstance(c, dict)]
        if isinstance(item.get("tasks"), list):
            converted["tasks"] = [{"name": t.get("name"), "display_name": t.get("displayName") or "", "description": t.get("description") or "", "task_type": t.get("taskType") or "", "downstream_tasks": t.get("downstreamTasks") or [], "task_sql": t.get("taskSQL") or "", "source_url": t.get("sourceUrl") or ""} for t in item["tasks"] if isinstance(t, dict)]
        if isinstance(item.get("mlFeatures"), list):
            converted["ml_features"] = [
                {
                    "name": f.get("name"), "data_type": f.get("dataType") or "", "description": f.get("description") or "",
                    "feature_algorithm": f.get("featureAlgorithm") or "",
                    "feature_sources": [{"name": s.get("name"), "data_type": s.get("dataType") or "", "data_source_fqn": (s.get("dataSource") or {}).get("fullyQualifiedName") or ""} for s in f.get("featureSources") or [] if isinstance(s, dict)],
                }
                for f in item["mlFeatures"] if isinstance(f, dict)
            ]
        if isinstance(item.get("mlHyperParameters"), list):
            converted["ml_hyper_parameters"] = [{"name": p.get("name"), "value": p.get("value"), "description": p.get("description") or ""} for p in item["mlHyperParameters"] if isinstance(p, dict)]
        if isinstance(item.get("charts"), list):
            converted["charts"] = [c.get("fullyQualifiedName") or c.get("name") for c in item["charts"] if isinstance(c, dict)]
        if isinstance(item.get("extension"), dict):
            converted["extension"] = item["extension"]
        entities.append(converted)
    lineage = []
    for edge in _as_list(payload.get("lineage")):
        if isinstance(edge, dict):
            from_fqn = (edge.get("fromEntity") or {}).get("fullyQualifiedName") if isinstance(edge.get("fromEntity"), dict) else edge.get("from_fqn")
            to_fqn = (edge.get("toEntity") or {}).get("fullyQualifiedName") if isinstance(edge.get("toEntity"), dict) else edge.get("to_fqn")
            if from_fqn and to_fqn:
                details = edge.get("lineageDetails") or {}
                lineage.append(
                    {
                        "from_fqn": from_fqn, "to_fqn": to_fqn, "source": "import", "sql": details.get("sqlQuery") or edge.get("sql") or "",
                        "description": details.get("description") or edge.get("description") or "",
                        "columns": [{"from_columns": c.get("fromColumns") or [], "to_column": c.get("toColumn")} for c in details.get("columnsLineage") or [] if isinstance(c, dict)] or edge.get("columns") or [],
                    }
                )
    return {"format": "lattice", "entities": entities, "tags": tags, "lineage": lineage}


def _om_column(column: dict[str, Any], owner_fqn: Any, tags: list[dict[str, Any]]) -> dict[str, Any]:
    name = column.get("name")
    if isinstance(column.get("tags"), list) and owner_fqn and name:
        for tag in column["tags"]:
            if isinstance(tag, dict) and tag.get("tagFQN"):
                tags.append({"source": "glossary" if str(tag.get("source", "")).lower() == "glossary" else "classification", "tag_fqn": tag["tagFQN"], "target_fqn": child_fqn(str(owner_fqn), str(name)), "label_type": str(tag.get("labelType", "manual")).lower(), "state": str(tag.get("state", "confirmed")).lower()})
    converted = {
        "name": name,
        "display_name": column.get("displayName") or "",
        "description": column.get("description") or "",
        "data_type": column.get("dataType") or "",
        "data_type_display": column.get("dataTypeDisplay") or column.get("dataType") or "",
        "constraint": column.get("constraint") or "",
    }
    if isinstance(column.get("children"), list):
        converted["children"] = [_om_column(c, child_fqn(str(owner_fqn), str(name)) if owner_fqn and name else None, tags) for c in column["children"] if isinstance(c, dict)]
    return converted


summary_of = _summary

#: Stands in for a stored webhook secret in every API response.
SECRET_MASK = "••••••"


def mask_destinations(destinations: Any) -> list[dict[str, Any]]:
    result = []
    for item in destinations if isinstance(destinations, list) else []:
        if isinstance(item, dict):
            result.append({**item, "secret": SECRET_MASK if item.get("secret") else ""})
    return result


#: Keys whose values are credentials; catalog documents only ever hold them masked or empty.
SECRET_FIELD = re.compile(r"(pass|secret|token|credential|private|api[_-]?key|access[_-]?key)", re.IGNORECASE)


def reject_secrets(value: Any, depth: int = 0) -> None:
    """Refuse a connection document that carries a real credential at any depth."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                reject_secrets(item, depth + 1)
            elif SECRET_FIELD.search(str(key)) and item not in (None, "", False, SECRET_MASK):
                raise _bad(f"连接信息不能包含密码、令牌等机密字段（{key}）；需要自动拾取的数据库请绑定平台数据源。")
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item, depth + 1)


def scrub_change(change: Any) -> Any:
    """Mask webhook secrets inside a change description (versions, feed, event payloads)."""
    if not isinstance(change, dict):
        return change
    result = dict(change)
    for key in ("fields_added", "fields_updated", "fields_deleted"):
        if key not in change:
            continue
        items = []
        for item in change.get(key) or []:
            if isinstance(item, dict) and str(item.get("name", "")).split(".")[0] == "destinations":
                item = {**item}
                for side in ("old_value", "new_value"):
                    if side in item:
                        item[side] = mask_destinations(item[side])
            items.append(item)
        result[key] = items
    return result
