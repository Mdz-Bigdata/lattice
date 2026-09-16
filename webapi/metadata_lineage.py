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

"""Data lineage of the 元数据管理 module, after OpenMetadata's lineage API.

An edge joins two catalog assets (table → table, table → dashboard, pipeline
→ table, table → semantic model …) and may carry the SQL that produced it,
a description, the pipeline that runs it and column-level mappings. Edges
come from four places:

* **manual** — drawn on the 血缘关系 screen or posted to the API;
* **query** / **view** — parsed out of SQL with sqlglot: a ``CREATE TABLE … AS``,
  ``CREATE VIEW`` or ``INSERT … SELECT`` names its target, a plain ``SELECT``
  is attached to the target the caller names, and views ingested from a
  data source are re-parsed from their definition;
* **polaris** / **semantic** — written by the ingestor when a Generic Table
  registered in Polaris points at an origin table, or a semantic-model
  dataset names its source table;
* **import** — bundles exported from Lattice or OpenMetadata.

The graph API walks the edges in both directions to a bounded depth and
returns nodes with the little the screen needs (type, service, tier, column
names) plus the edges with their column mappings. Usage is the second half of
OpenMetadata's lineage story: every query the platform runs on a data source
is attributed to the catalog tables it reads, which feeds the 使用率 figures
and the 查询 tab of an asset.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from .metadata_entities import DATA_ASSET_TYPES, build_fqn, child_fqn, split_fqn, type_label
from .metadata_service import MetadataError, MetadataService, summary_of
from .metadata_store import MetadataStore, MetadataStoreError, day_of, now_ms, today

MAX_DEPTH = 6
DEFAULT_DEPTH = 3
MAX_NODES = 400
MAX_SQL = 20000
MAX_COLUMN_MAPPINGS = 500
EDGE_SOURCES = ("manual", "query", "view", "pipeline", "semantic", "polaris", "import", "dbt")
INDEX_TTL = 60.0
#: OpenMetadata service type → sqlglot dialect used to parse its SQL.
DIALECTS = {
    "Mysql": "mysql",
    "MariaDB": "mysql",
    "Postgres": "postgres",
    "Greenplum": "postgres",
    "Redshift": "redshift",
    "Clickhouse": "clickhouse",
    "StarRocks": "starrocks",
    "Doris": "doris",
    "Hive": "hive",
    "Impala": "hive",
    "DuckDB": "duckdb",
    "Iceberg": "duckdb",
    "Paimon": "duckdb",
    "Polaris": "duckdb",
    "Trino": "trino",
    "Presto": "presto",
    "Snowflake": "snowflake",
    "BigQuery": "bigquery",
    "Databricks": "databricks",
    "Oracle": "oracle",
    "Mssql": "tsql",
    "SQLite": "sqlite",
    "Athena": "athena",
    "Spark": "spark",
}
_KNOWN_DIALECTS = set(DIALECTS.values())


class LineageService:
    """Edges, graphs, SQL-derived lineage and query usage over :class:`MetadataStore`."""

    def __init__(self, store: MetadataStore, service: MetadataService):
        self.store = store
        self.service = service
        self._index_lock = threading.Lock()
        self._index_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # =====================================================================================
    # resolution
    # =====================================================================================
    def resolve(self, ref: Any, entity_type: str | None = None, *, what: str = "对象") -> dict[str, Any]:
        """A data asset by id or FQN; a governance object is refused as a lineage end."""
        if isinstance(ref, dict):
            entity_type = entity_type or ref.get("entity_type") or ref.get("type")
            ref = ref.get("fqn") or ref.get("id") or ref.get("name")
        if not isinstance(ref, str) or not ref.strip():
            raise MetadataError(400, f"缺少{what}。")
        row = self.service.row(entity_type, ref.strip())
        if row["entity_type"] not in DATA_ASSET_TYPES:
            raise MetadataError(400, f"{what}必须是数据资产，{type_label(row['entity_type'])}不能出现在血缘中。")
        return row

    def invalidate(self) -> None:
        with self._index_lock:
            self._index_cache.clear()

    # =====================================================================================
    # edges
    # =====================================================================================
    def add_edge(self, payload: dict[str, Any], *, user: str | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MetadataError(400, "请求体必须是对象。")
        user = user or self.service.user
        source = payload.get("source") or "manual"
        if source not in EDGE_SOURCES:
            raise MetadataError(400, "血缘来源无效。")
        with self.service.lock():
            upstream = self.resolve(payload.get("from_fqn") or payload.get("from"), payload.get("from_type"), what="上游对象")
            downstream = self.resolve(payload.get("to_fqn") or payload.get("to"), payload.get("to_type"), what="下游对象")
            if upstream["id"] == downstream["id"]:
                raise MetadataError(400, "上游和下游不能是同一个对象。")
            if upstream["deleted"] or downstream["deleted"]:
                raise MetadataError(400, "已删除的对象不能建立血缘。")
            pipeline_fqn = ""
            if payload.get("pipeline_fqn") or payload.get("pipeline"):
                pipeline = self.service.row("pipeline", str(payload.get("pipeline_fqn") or payload.get("pipeline")))
                pipeline_fqn = pipeline["fqn"]
            columns = self._column_mappings(payload.get("columns"), upstream, downstream)
            sql = payload.get("sql") or ""
            if not isinstance(sql, str):
                raise MetadataError(400, "sql 必须是文本。")
            description = payload.get("description") or ""
            if not isinstance(description, str):
                raise MetadataError(400, "描述必须是文本。")
            existed = bool(self.store.edges(from_fqn=upstream["fqn"], to_fqn=downstream["fqn"]))
            edge = self.store.upsert_edge(
                {
                    "from_fqn": upstream["fqn"],
                    "from_type": upstream["entity_type"],
                    "to_fqn": downstream["fqn"],
                    "to_type": downstream["entity_type"],
                    "source": source,
                    "sql": sql.strip()[:MAX_SQL],
                    "description": description.strip()[:4000],
                    "columns": columns,
                    "pipeline_fqn": pipeline_fqn,
                }
            )
            self.service.emit(
                {
                    "event_type": "entityUpdated",
                    "entity_type": downstream["entity_type"],
                    "entity_id": downstream["id"],
                    "entity_fqn": downstream["fqn"],
                    "entity_name": downstream["display_name"] or downstream["name"],
                    "user_name": user,
                    "ts": now_ms(),
                    "previous_version": downstream["version"],
                    "current_version": downstream["version"],
                    "change": {
                        "summary": ("更新" if existed else "新增") + f"血缘：{upstream['fqn']} → {downstream['fqn']}",
                        "fields_added": [] if existed else [{"name": "lineage", "new_value": upstream["fqn"]}],
                        "fields_updated": [{"name": "lineage", "old_value": upstream["fqn"], "new_value": upstream["fqn"]}] if existed else [],
                        "fields_deleted": [],
                        "kinds": ["lineage"],
                    },
                }
            )
            return self._edge_view(edge)

    def _column_mappings(self, value: Any, upstream: dict[str, Any], downstream: dict[str, Any]) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise MetadataError(400, "字段血缘必须是数组。")
        if len(value) > MAX_COLUMN_MAPPINGS:
            raise MetadataError(400, f"字段血缘最多 {MAX_COLUMN_MAPPINGS} 条。")
        upstream_columns = _column_names(upstream)
        downstream_columns = _column_names(downstream)
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise MetadataError(400, "字段血缘的每一项必须是对象。")
            to_column = _column_fqn(item.get("to_column") or item.get("to"), downstream, downstream_columns, "下游字段")
            sources = item.get("from_columns") if item.get("from_columns") is not None else item.get("from")
            if isinstance(sources, str):
                sources = [sources]
            if not isinstance(sources, list) or not sources:
                raise MetadataError(400, f"字段 {to_column} 缺少上游字段。")
            from_columns = []
            for source in sources[:50]:
                fqn = _column_fqn(source, upstream, upstream_columns, "上游字段")
                if fqn not in from_columns:
                    from_columns.append(fqn)
            function = item.get("function") or ""
            result.append({"from_columns": from_columns, "to_column": to_column, "function": str(function)[:500]})
        return result

    def remove_edge(self, from_ref: Any, to_ref: Any, *, user: str | None = None) -> dict[str, Any]:
        user = user or self.service.user
        with self.service.lock():
            upstream = self.resolve(from_ref, what="上游对象")
            downstream = self.resolve(to_ref, what="下游对象")
            if not self.store.delete_edge(upstream["fqn"], downstream["fqn"]):
                raise MetadataError(404, "血缘关系不存在。")
            self.service.emit(
                {
                    "event_type": "entityUpdated",
                    "entity_type": downstream["entity_type"],
                    "entity_id": downstream["id"],
                    "entity_fqn": downstream["fqn"],
                    "entity_name": downstream["display_name"] or downstream["name"],
                    "user_name": user,
                    "ts": now_ms(),
                    "previous_version": downstream["version"],
                    "current_version": downstream["version"],
                    "change": {
                        "summary": f"删除血缘：{upstream['fqn']} → {downstream['fqn']}",
                        "fields_added": [],
                        "fields_updated": [],
                        "fields_deleted": [{"name": "lineage", "old_value": upstream["fqn"]}],
                        "kinds": ["lineage"],
                    },
                }
            )
            return {"deleted": True, "from_fqn": upstream["fqn"], "to_fqn": downstream["fqn"]}

    def edge(self, from_ref: Any, to_ref: Any) -> dict[str, Any]:
        upstream = self.resolve(from_ref, what="上游对象")
        downstream = self.resolve(to_ref, what="下游对象")
        edges = self.store.edges(from_fqn=upstream["fqn"], to_fqn=downstream["fqn"])
        if not edges:
            raise MetadataError(404, "血缘关系不存在。")
        return self._edge_view(edges[0])

    @staticmethod
    def _edge_view(edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": edge["id"],
            "from_fqn": edge["from_fqn"],
            "from_type": edge["from_type"],
            "to_fqn": edge["to_fqn"],
            "to_type": edge["to_type"],
            "source": edge["source"],
            "sql": edge.get("sql") or "",
            "description": edge.get("description") or "",
            "columns": edge.get("columns") or [],
            "pipeline_fqn": edge.get("pipeline_fqn") or "",
            "created_at": edge.get("created_at"),
            "updated_at": edge.get("updated_at"),
        }

    # =====================================================================================
    # graphs
    # =====================================================================================
    def graph(
        self, entity_type: str | None, ref: str, *, upstream_depth: Any = DEFAULT_DEPTH,
        downstream_depth: Any = DEFAULT_DEPTH, include_deleted: bool = False,
    ) -> dict[str, Any]:
        root = self.resolve(ref, entity_type)
        up = _depth(upstream_depth)
        down = _depth(downstream_depth)
        depths: dict[str, int] = {root["fqn"]: 0}
        edges: dict[str, dict[str, Any]] = {}
        truncated = False
        for direction, limit in (("upstream", up), ("downstream", down)):
            frontier = [root["fqn"]]
            level = 0
            while frontier and level < limit:
                level += 1
                found = self.store.edges_for_many(frontier, direction)
                next_frontier: list[str] = []
                for edge in found:
                    edges.setdefault(edge["id"], edge)
                    other = edge["from_fqn"] if direction == "upstream" else edge["to_fqn"]
                    if other in depths:
                        continue
                    if len(depths) >= MAX_NODES:
                        truncated = True
                        break
                    depths[other] = -level if direction == "upstream" else level
                    next_frontier.append(other)
                frontier = next_frontier
                if truncated:
                    break
        rows = {r["fqn"]: r for r in self._asset_rows(list(depths))}
        nodes = []
        for fqn, depth in depths.items():
            row = rows.get(fqn)
            if row is None:
                nodes.append({"fqn": fqn, "entity_type": "", "type_label": "未登记", "name": split_fqn(fqn)[-1], "display_name": split_fqn(fqn)[-1], "depth": depth, "missing": True, "deleted": False, "columns": []})
                continue
            if row["deleted"] and not include_deleted and fqn != root["fqn"]:
                continue
            nodes.append(self._node(row, depth))
        present = {node["fqn"] for node in nodes}
        edge_views = [self._edge_view(e) for e in edges.values() if e["from_fqn"] in present and e["to_fqn"] in present]
        edge_views.sort(key=lambda e: (e["from_fqn"], e["to_fqn"]))
        nodes.sort(key=lambda n: (n["depth"], n["fqn"]))
        return {
            "entity": summary_of(root),
            "nodes": nodes,
            "edges": edge_views,
            "upstream_depth": up,
            "downstream_depth": down,
            "truncated": truncated,
            "upstream_count": sum(1 for n in nodes if n["depth"] < 0),
            "downstream_count": sum(1 for n in nodes if n["depth"] > 0),
        }

    def _asset_rows(self, fqns: list[str]) -> list[dict[str, Any]]:
        """Rows for the FQNs, preferring data assets when a governance object shares one."""
        rows = self.store.get_entities_by_fqn(fqns)
        order = {name: index for index, name in enumerate(DATA_ASSET_TYPES)}
        best: dict[str, dict[str, Any]] = {}
        for row in rows:
            rank = order.get(row["entity_type"], 999)
            current = best.get(row["fqn"])
            if current is None or rank < order.get(current["entity_type"], 999):
                best[row["fqn"]] = row
        return list(best.values())

    def _node(self, row: dict[str, Any], depth: int) -> dict[str, Any]:
        document = row["json"]
        columns = [c.get("name") for c in document.get("columns") or [] if isinstance(c, dict) and c.get("name")][:200]
        return {
            "id": row["id"],
            "fqn": row["fqn"],
            "entity_type": row["entity_type"],
            "type_label": type_label(row["entity_type"]),
            "name": row["name"],
            "display_name": row["display_name"] or row["name"],
            "service_type": row["service_type"],
            "service_fqn": row["service_fqn"],
            "tier": row["tier"] or None,
            "deleted": row["deleted"],
            "description": (row["description"] or "")[:300],
            "depth": depth,
            "columns": columns,
            "missing": False,
        }

    def impact(self, entity_type: str | None, ref: str, *, depth: Any = MAX_DEPTH) -> dict[str, Any]:
        """Everything downstream of an asset, grouped by type: what a change would touch."""
        graph = self.graph(entity_type, ref, upstream_depth=0, downstream_depth=depth)
        affected = [n for n in graph["nodes"] if n["depth"] > 0]
        by_type: dict[str, int] = {}
        for node in affected:
            by_type[node["entity_type"] or "unknown"] = by_type.get(node["entity_type"] or "unknown", 0) + 1
        return {
            "entity": graph["entity"],
            "total": len(affected),
            "by_type": [{"entity_type": key, "label": type_label(key) if key != "unknown" else "未登记", "count": count} for key, count in sorted(by_type.items(), key=lambda i: -i[1])],
            "items": affected[:200],
            "truncated": graph["truncated"],
            "depth": graph["downstream_depth"],
        }

    def edges_of(self, entity_type: str | None, ref: str) -> dict[str, Any]:
        row = self.resolve(ref, entity_type)
        return {
            "entity": summary_of(row),
            "upstream": [self._edge_view(e) for e in self.store.edges(to_fqn=row["fqn"])],
            "downstream": [self._edge_view(e) for e in self.store.edges(from_fqn=row["fqn"])],
        }

    def summary(self) -> dict[str, Any]:
        counts = self.store.edge_counts()
        return {"total": sum(counts.values()), "by_source": counts}

    # =====================================================================================
    # SQL-derived lineage
    # =====================================================================================
    def from_sql(
        self, sql: Any, *, dialect: str | None = None, datasource_id: str | None = None,
        service_fqn: str | None = None, target: Any = None, apply: bool = False,
        source: str = "query", user: str | None = None,
    ) -> dict[str, Any]:
        """Lineage edges implied by one SQL statement, optionally written to the store."""
        if not isinstance(sql, str) or not sql.strip():
            raise MetadataError(400, "SQL 不能为空。")
        if len(sql) > MAX_SQL:
            raise MetadataError(400, f"SQL 不能超过 {MAX_SQL} 个字符。")
        if source not in EDGE_SOURCES:
            raise MetadataError(400, "血缘来源无效。")
        service_fqns = self._service_fqns(datasource_id, service_fqn)
        read = _dialect(dialect) or (self._dialect_of_services(service_fqns) if service_fqns else None)
        try:
            statements = sqlglot.parse(sql, read=read)
        except sqlglot.errors.SqlglotError as error:
            raise MetadataError(400, f"SQL 无法解析：{str(error)[:300]}") from error
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            raise MetadataError(400, "请提供一条 SQL 语句。")
        tree = statements[0]
        target_ref, select, kind = _split_statement(tree)
        index = self._table_index(service_fqns)
        target_row = None
        if target:
            target_row = self.resolve(target, what="目标对象")
        elif target_ref is not None:
            target_row = self._lookup(index, target_ref)
        if kind == "view":
            source = "view"
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        references: list[tuple[str | None, str]] = []
        aliases: dict[str, tuple[str | None, str]] = {}
        for table in select.find_all(exp.Table) if select is not None else []:
            if not isinstance(table.this, exp.Identifier):
                continue
            pair = (table.db or None, table.name)
            if not table.db and table.name.lower() in ctes:
                continue
            if target_ref is not None and pair == target_ref and kind != "view":
                continue
            if pair not in references:
                references.append(pair)
            aliases[(table.alias or table.name).lower()] = pair
            aliases.setdefault(table.name.lower(), pair)
        sources: list[dict[str, Any]] = []
        unresolved: list[str] = []
        resolved: dict[tuple[str | None, str], dict[str, Any]] = {}
        for pair in references:
            row = self._lookup(index, pair)
            label = f"{pair[0]}.{pair[1]}" if pair[0] else pair[1]
            if row is None:
                unresolved.append(label)
                continue
            if target_row is not None and row["id"] == target_row["id"]:
                continue
            resolved[pair] = row
            sources.append({**summary_of(row), "reference": label})
        columns = self._column_lineage(select, aliases, resolved, target_row) if target_row is not None and select is not None else []
        edges: list[dict[str, Any]] = []
        if target_row is not None:
            for pair, row in resolved.items():
                edge_columns = [c for c in columns if any(f.startswith(row["fqn"] + ".") for f in c["from_columns"])]
                edge_columns = [{"from_columns": [f for f in c["from_columns"] if f.startswith(row["fqn"] + ".")], "to_column": c["to_column"], "function": c.get("function", "")} for c in edge_columns]
                edges.append(
                    {
                        "from_fqn": row["fqn"],
                        "from_type": row["entity_type"],
                        "to_fqn": target_row["fqn"],
                        "to_type": target_row["entity_type"],
                        "source": source,
                        "sql": sql.strip()[:MAX_SQL],
                        "columns": edge_columns,
                    }
                )
        applied = 0
        if apply and edges:
            with self.service.lock():
                for edge in edges:
                    self.store.upsert_edge(edge)
                    applied += 1
                if applied:
                    self.service.emit(
                        {
                            "event_type": "entityUpdated",
                            "entity_type": target_row["entity_type"],
                            "entity_id": target_row["id"],
                            "entity_fqn": target_row["fqn"],
                            "entity_name": target_row["display_name"] or target_row["name"],
                            "user_name": user or self.service.user,
                            "ts": now_ms(),
                            "previous_version": target_row["version"],
                            "current_version": target_row["version"],
                            "change": {"summary": f"从 SQL 解析出 {applied} 条血缘", "fields_added": [{"name": "lineage", "new_value": [e["from_fqn"] for e in edges]}], "fields_updated": [], "fields_deleted": [], "kinds": ["lineage"]},
                        }
                    )
        return {
            "kind": kind,
            "dialect": read or "",
            "target": summary_of(target_row) if target_row else None,
            "target_reference": (f"{target_ref[0]}.{target_ref[1]}" if target_ref and target_ref[0] else (target_ref[1] if target_ref else "")),
            "sources": sources,
            "unresolved": unresolved,
            "columns": columns,
            "edges": [{k: v for k, v in e.items() if k != "sql"} for e in edges],
            "applied": applied,
        }

    def _column_lineage(self, select: exp.Expression, aliases: dict[str, tuple[str | None, str]], resolved: dict[tuple[str | None, str], dict[str, Any]], target_row: dict[str, Any]) -> list[dict[str, Any]]:
        projections = _projections(select)
        if projections is None:
            return []
        target_columns = _column_names(target_row)
        result: list[dict[str, Any]] = []
        single = next(iter(resolved.values())) if len(resolved) == 1 else None
        for projection in projections:
            if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
                owner = single
                if isinstance(projection, exp.Column) and projection.table:
                    owner = resolved.get(aliases.get(projection.table.lower(), (None, "")))
                if owner is None:
                    continue
                for name in _column_names(owner):
                    if target_columns and name not in target_columns:
                        continue
                    result.append({"from_columns": [child_fqn(owner["fqn"], name)], "to_column": child_fqn(target_row["fqn"], name), "function": ""})
                continue
            out_name = projection.alias_or_name
            if not out_name:
                continue
            if target_columns and out_name not in target_columns:
                match = next((c for c in target_columns if c.lower() == out_name.lower()), None)
                if match is None:
                    continue
                out_name = match
            from_columns: list[str] = []
            for column in projection.find_all(exp.Column):
                if isinstance(column.this, exp.Star):
                    continue
                owner = None
                if column.table:
                    owner = resolved.get(aliases.get(column.table.lower(), (None, "")))
                elif single is not None:
                    owner = single
                if owner is None:
                    continue
                fqn = child_fqn(owner["fqn"], column.name)
                if fqn not in from_columns:
                    from_columns.append(fqn)
            if not from_columns:
                continue
            function = ""
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(inner, exp.Column):
                function = inner.sql()[:200]
            result.append({"from_columns": from_columns, "to_column": child_fqn(target_row["fqn"], out_name), "function": function})
            if len(result) >= MAX_COLUMN_MAPPINGS:
                break
        return result

    def sync_views(self, service_fqn: str | None = None, *, user: str | None = None) -> dict[str, Any]:
        """Re-derive view lineage from the stored view definitions of a service (or all)."""
        rows = self.store.all_entities("table", deleted=False, service_fqn=service_fqn or None, limit=50000)
        views = [r for r in rows if str(r["json"].get("table_type") or "") in {"View", "MaterializedView", "SecureView"} and r["json"].get("view_definition")]
        summary = {"views": len(views), "edges": 0, "unresolved": 0, "errors": []}
        for view in views:
            try:
                result = self.from_sql(
                    view["json"]["view_definition"], service_fqn=view["service_fqn"], target=view["fqn"],
                    apply=True, source="view", user=user,
                )
                summary["edges"] += result["applied"]
                summary["unresolved"] += len(result["unresolved"])
            except (MetadataError, MetadataStoreError) as error:
                if len(summary["errors"]) < 50:
                    summary["errors"].append(f"{view['fqn']}: {error}")
        return summary

    # =====================================================================================
    # table lookup
    # =====================================================================================
    def _service_fqns(self, datasource_id: str | None, service_fqn: str | None) -> list[str]:
        if service_fqn:
            row = self.service.row(None, service_fqn)
            return [row["service_fqn"] or row["fqn"]]
        if datasource_id:
            return [s["fqn"] for s in self.services_of_datasource(datasource_id)]
        return []

    def services_of_datasource(self, datasource_id: str) -> list[dict[str, Any]]:
        return [
            row for row in self.store.all_entities("databaseService", deleted=False, limit=2000)
            if row["json"].get("datasource_id") == datasource_id
        ]

    def _dialect_of_services(self, service_fqns: list[str]) -> str | None:
        for fqn in service_fqns:
            row = self.store.get_entity("databaseService", fqn=fqn)
            if row is not None:
                return DIALECTS.get(row["service_type"])
        return None

    def _table_index(self, service_fqns: list[str]) -> dict[str, Any]:
        """Catalog tables keyed by ``schema.table`` and ``table`` (lower case)."""
        key = "|".join(sorted(service_fqns)) or "*"
        now = time.monotonic()
        with self._index_lock:
            cached = self._index_cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        rows: list[dict[str, Any]] = []
        if service_fqns:
            for fqn in service_fqns:
                rows.extend(self.store.all_entities("table", deleted=False, service_fqn=fqn, limit=20000))
        else:
            rows = self.store.all_entities("table", deleted=False, limit=50000)
        qualified: dict[str, list[dict[str, Any]]] = {}
        plain: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            parts = split_fqn(row["fqn"])
            schema = str(row["json"].get("source_schema") or (parts[-2] if len(parts) > 1 else "")).lower()
            database = str(parts[-3]).lower() if len(parts) > 2 else ""
            name = row["name"].lower()
            qualified.setdefault(f"{schema}.{name}", []).append(row)
            if database:
                qualified.setdefault(f"{database}.{name}", []).append(row)
            plain.setdefault(name, []).append(row)
        index = {"qualified": qualified, "plain": plain}
        with self._index_lock:
            self._index_cache[key] = (now + INDEX_TTL, index)
        return index

    @staticmethod
    def _lookup(index: dict[str, Any], pair: tuple[str | None, str]) -> dict[str, Any] | None:
        schema, name = pair
        if schema:
            # The same schema.table in several services (or databases) is ambiguous, not "the first one".
            rows = list({row["id"]: row for row in index["qualified"].get(f"{schema.lower()}.{name.lower()}") or []}.values())
            return rows[0] if len(rows) == 1 else None
        rows = list({row["id"]: row for row in index["plain"].get(name.lower()) or []}.values())
        if len(rows) == 1:
            return rows[0]
        if rows:
            # Prefer a table whose schema is the engine's default over an ambiguous match.
            defaults = [r for r in rows if str(r["json"].get("source_schema") or "").lower() in {"main", "public", "default", "lattice_demo"}]
            if len(defaults) == 1:
                return defaults[0]
        return None

    def find_table_anywhere(self, schema: str | None, name: str) -> dict[str, Any] | None:
        """A table by ``schema.name`` across every service; None when ambiguous."""
        return self._lookup(self._table_index([]), (schema or None, name))

    def find_table(self, datasource_id: str, schema: str | None, name: str) -> dict[str, Any] | None:
        services = self._service_fqns(datasource_id, None)
        if not services:
            return None
        return self._lookup(self._table_index(services), (schema or None, name))

    # =====================================================================================
    # usage
    # =====================================================================================
    def record_query(self, payload: dict[str, Any]) -> int:
        """Attribute an executed query to the catalog tables it read. Never raises."""
        try:
            sql = str(payload.get("sql") or "")
            datasource_id = str(payload.get("datasource_id") or "")
            if not sql.strip() or not datasource_id:
                return 0
            services = self._service_fqns(datasource_id, None)
            if not services:
                return 0
            read = _dialect(payload.get("dialect")) or self._dialect_of_services(services)
            try:
                tree = sqlglot.parse_one(sql, read=read)
            except sqlglot.errors.SqlglotError:
                return 0
            ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
            index = self._table_index(services)
            seen: set[str] = set()
            stamp = now_ms()
            day = day_of(stamp)
            query_id = str(payload.get("id") or stamp)
            for table in tree.find_all(exp.Table):
                if not isinstance(table.this, exp.Identifier):
                    continue
                if not table.db and table.name.lower() in ctes:
                    continue
                row = self._lookup(index, (table.db or None, table.name))
                if row is None or row["fqn"] in seen:
                    continue
                seen.add(row["fqn"])
                if self.store.upsert_query_ref(query_id, row["fqn"], sql, datasource_id, stamp):
                    self.store.add_usage(row["fqn"], day, queries=1)
            return len(seen)
        except Exception:  # noqa: BLE001 - usage must never break a query
            return 0

    def queries_for(self, entity_type: str | None, ref: str, *, limit: int = 50) -> dict[str, Any]:
        row = self.resolve(ref, entity_type)
        items = self.store.queries_for(row["fqn"], limit=limit)
        return {"entity": summary_of(row), "items": items, "total": len(items)}

    def usage(self, entity_type: str | None, ref: str, *, days: int = 30) -> dict[str, Any]:
        row = self.resolve(ref, entity_type)
        span = max(1, min(int(days), 365))
        since = day_of(now_ms() - span * 86400000)
        series = self.store.usage(row["fqn"], since)
        return {
            "entity": summary_of(row),
            "days": span,
            "series": series,
            "queries": sum(int(item["queries"]) for item in series),
            "views": sum(int(item["views"]) for item in series),
            "today": today(),
        }


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _depth(value: Any) -> int:
    try:
        depth = int(value) if value is not None else DEFAULT_DEPTH
    except (TypeError, ValueError) as error:
        raise MetadataError(400, "血缘深度必须是整数。") from error
    if depth < 0 or depth > MAX_DEPTH:
        raise MetadataError(400, f"血缘深度必须在 0–{MAX_DEPTH} 之间。")
    return depth


def _dialect(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if text in _KNOWN_DIALECTS:
        return text
    mapped = DIALECTS.get(str(value).strip())
    if mapped:
        return mapped
    if re.match(r"^[a-z][a-z0-9_]{1,30}\Z", text):
        try:
            sqlglot.Dialect.get_or_raise(text)
            return text
        except Exception:  # noqa: BLE001
            return None
    return None


def _column_names(row: dict[str, Any]) -> list[str]:
    return [str(c.get("name")) for c in row["json"].get("columns") or [] if isinstance(c, dict) and c.get("name")]


def _column_fqn(value: Any, row: dict[str, Any], names: list[str], what: str) -> str:
    if isinstance(value, dict):
        value = value.get("fqn") or value.get("name")
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(400, f"{what}不能为空。")
    text = value.strip()
    if text.startswith(row["fqn"] + "."):
        name = text[len(row["fqn"]) + 1 :]
        parts = split_fqn(name)
        name = parts[0] if parts else name
    else:
        name = text
    if names and name not in names:
        match = next((c for c in names if c.lower() == name.lower()), None)
        if match is None:
            raise MetadataError(404, f"{what}不存在：{row['fqn']} 没有字段 {name}")
        name = match
    return child_fqn(row["fqn"], name)


def _table_pair(node: exp.Expression | None) -> tuple[str | None, str] | None:
    if isinstance(node, exp.Schema):
        node = node.this
    if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
        return (node.db or None, node.name)
    return None


def _split_statement(tree: exp.Expression) -> tuple[tuple[str | None, str] | None, exp.Expression | None, str]:
    """(target table, the SELECT that feeds it, kind) for CTAS / CREATE VIEW / INSERT / SELECT."""
    if isinstance(tree, exp.Create):
        kind = str(tree.args.get("kind") or "").upper()
        select = tree.expression
        if isinstance(select, exp.Subquery):
            select = select.this
        return _table_pair(tree.this), select, ("view" if kind == "VIEW" else "query")
    if isinstance(tree, exp.Insert):
        select = tree.expression
        if isinstance(select, exp.Subquery):
            select = select.this
        return _table_pair(tree.this), select, "query"
    if isinstance(tree, exp.Merge):
        return _table_pair(tree.this), tree.args.get("using"), "query"
    return None, tree, "select"


def _projections(select: exp.Expression | None) -> list[exp.Expression] | None:
    if select is None:
        return None
    while isinstance(select, (exp.Union, exp.Except, exp.Intersect)):
        select = select.left
    if isinstance(select, exp.Subquery):
        select = select.this
    if isinstance(select, exp.Select):
        return list(select.expressions)
    return None


def parse_dialect_of(service_type: str) -> str | None:
    """Public alias used by the ingestor when it stores view definitions."""
    return DIALECTS.get(service_type)


__all__ = ["LineageService", "DIALECTS", "EDGE_SOURCES", "MAX_DEPTH", "parse_dialect_of", "build_fqn"]


def _unused(*_: Iterable[Any]) -> None:  # pragma: no cover - keeps the import list honest
    return None
