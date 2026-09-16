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

"""SQL repository of the 元数据管理 module (an OpenMetadata-style catalog).

The catalog follows OpenMetadata's storage design: every entity is one row
holding its JSON document next to the handful of columns the screens filter on
(type, name, fully qualified name, parent, service, tier, domain, deleted),
plus side tables for versions, relationships, tag usage, lineage edges, change
events, activity threads, usage counters, ingestion runs, insight snapshots,
notifications and custom-property definitions.

Two backends share one SQL text:

* **PostgreSQL** — the platform's business database, in an additive schema of
  its own (``lattice_metadata``); this is the default, exactly like the quality
  module's ``lattice_quality``;
* **SQLite** — a file under ``.runtime/webui`` used by the tests and as the
  fallback when PostgreSQL cannot be reached at startup, so the catalog keeps
  working on a machine without the business database.  :meth:`healthy` says
  which backend is live and why.

The SQL is written with ``?`` placeholders and translated for psycopg; the
schema uses only types both engines understand (text, integer, real).  Values
never reach a statement except as bound parameters — the only identifiers ever
formatted in are this module's own table names.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import env
from .quality_store import DEFAULT_DSN as QUALITY_DEFAULT_DSN
from .quality_store import QualityStoreError, detail, parse_dsn as parse_postgres_dsn

try:  # Optional, as in the quality store.
    import psycopg
except ImportError:  # pragma: no cover - the platform environment always has it
    psycopg = None  # type: ignore[assignment]

SCHEMA = "lattice_metadata"
DSN_SETTING = "METADATA_DSN"
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT = 30
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20
MAX_SEARCH_TERMS = 6

TABLES = (
    "entity",
    "entity_version",
    "relationship",
    "tag_usage",
    "lineage_edge",
    "change_event",
    "thread",
    "insight_snapshot",
    "ingestion_run",
    "usage_daily",
    "query_ref",
    "notification",
    "custom_property",
    "setting",
)

DDL = (
    """CREATE TABLE IF NOT EXISTS {entity} (
  id           text PRIMARY KEY,
  entity_type  text NOT NULL,
  name         text NOT NULL,
  display_name text NOT NULL DEFAULT '',
  fqn          text NOT NULL,
  parent_fqn   text NOT NULL DEFAULT '',
  service_type text NOT NULL DEFAULT '',
  service_fqn  text NOT NULL DEFAULT '',
  description  text NOT NULL DEFAULT '',
  tier         text NOT NULL DEFAULT '',
  domain_fqn   text NOT NULL DEFAULT '',
  deleted      integer NOT NULL DEFAULT 0,
  version      real NOT NULL DEFAULT 0.1,
  updated_at   bigint NOT NULL,
  updated_by   text NOT NULL DEFAULT '',
  created_at   bigint NOT NULL,
  search_text  text NOT NULL DEFAULT '',
  json         text NOT NULL,
  UNIQUE (entity_type, fqn)
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_entity_type ON {entity} (entity_type, deleted)",
    "CREATE INDEX IF NOT EXISTS idx_md_entity_parent ON {entity} (parent_fqn)",
    "CREATE INDEX IF NOT EXISTS idx_md_entity_service ON {entity} (service_fqn)",
    "CREATE INDEX IF NOT EXISTS idx_md_entity_fqn ON {entity} (fqn)",
    """CREATE TABLE IF NOT EXISTS {entity_version} (
  entity_id  text NOT NULL,
  version    real NOT NULL,
  json       text NOT NULL,
  change     text NOT NULL DEFAULT '{{}}',
  updated_at bigint NOT NULL,
  updated_by text NOT NULL DEFAULT '',
  PRIMARY KEY (entity_id, version)
)""",
    """CREATE TABLE IF NOT EXISTS {relationship} (
  from_id   text NOT NULL,
  from_type text NOT NULL,
  to_id     text NOT NULL,
  to_type   text NOT NULL,
  relation  text NOT NULL,
  json      text NOT NULL DEFAULT '{{}}',
  PRIMARY KEY (from_id, to_id, relation)
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_rel_to ON {relationship} (to_id, relation)",
    "CREATE INDEX IF NOT EXISTS idx_md_rel_from ON {relationship} (from_id, relation)",
    """CREATE TABLE IF NOT EXISTS {tag_usage} (
  source     text NOT NULL,
  tag_fqn    text NOT NULL,
  target_fqn text NOT NULL,
  label_type text NOT NULL DEFAULT 'manual',
  state      text NOT NULL DEFAULT 'confirmed',
  PRIMARY KEY (source, tag_fqn, target_fqn)
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_tag_target ON {tag_usage} (target_fqn)",
    "CREATE INDEX IF NOT EXISTS idx_md_tag_fqn ON {tag_usage} (tag_fqn)",
    """CREATE TABLE IF NOT EXISTS {lineage_edge} (
  id           text PRIMARY KEY,
  from_fqn     text NOT NULL,
  from_type    text NOT NULL,
  to_fqn       text NOT NULL,
  to_type      text NOT NULL,
  source       text NOT NULL DEFAULT 'manual',
  sql          text NOT NULL DEFAULT '',
  description  text NOT NULL DEFAULT '',
  columns      text NOT NULL DEFAULT '[]',
  pipeline_fqn text NOT NULL DEFAULT '',
  created_at   bigint NOT NULL,
  updated_at   bigint NOT NULL,
  UNIQUE (from_fqn, to_fqn)
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_edge_from ON {lineage_edge} (from_fqn)",
    "CREATE INDEX IF NOT EXISTS idx_md_edge_to ON {lineage_edge} (to_fqn)",
    """CREATE TABLE IF NOT EXISTS {change_event} (
  id               text PRIMARY KEY,
  event_type       text NOT NULL,
  entity_type      text NOT NULL,
  entity_id        text NOT NULL,
  entity_fqn       text NOT NULL,
  entity_name      text NOT NULL DEFAULT '',
  user_name        text NOT NULL DEFAULT '',
  ts               bigint NOT NULL,
  previous_version real,
  current_version  real,
  change           text NOT NULL DEFAULT '{{}}'
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_event_ts ON {change_event} (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_md_event_fqn ON {change_event} (entity_fqn)",
    """CREATE TABLE IF NOT EXISTS {thread} (
  id          text PRIMARY KEY,
  thread_type text NOT NULL,
  about_fqn   text NOT NULL DEFAULT '',
  about_type  text NOT NULL DEFAULT '',
  message     text NOT NULL DEFAULT '',
  created_by  text NOT NULL DEFAULT '',
  created_at  bigint NOT NULL,
  updated_at  bigint NOT NULL,
  resolved    integer NOT NULL DEFAULT 0,
  json        text NOT NULL DEFAULT '{{}}'
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_thread_about ON {thread} (about_fqn)",
    """CREATE TABLE IF NOT EXISTS {insight_snapshot} (
  day  text PRIMARY KEY,
  ts   bigint NOT NULL,
  json text NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS {ingestion_run} (
  id          text PRIMARY KEY,
  service_fqn text NOT NULL DEFAULT '',
  run_type    text NOT NULL DEFAULT 'metadata',
  status      text NOT NULL,
  trigger     text NOT NULL DEFAULT 'manual',
  started_at  bigint NOT NULL,
  finished_at bigint,
  summary     text NOT NULL DEFAULT '{{}}',
  message     text NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_run_started ON {ingestion_run} (started_at DESC)",
    """CREATE TABLE IF NOT EXISTS {usage_daily} (
  target_fqn text NOT NULL,
  day        text NOT NULL,
  queries    integer NOT NULL DEFAULT 0,
  views      integer NOT NULL DEFAULT 0,
  PRIMARY KEY (target_fqn, day)
)""",
    """CREATE TABLE IF NOT EXISTS {query_ref} (
  query_id      text NOT NULL,
  target_fqn    text NOT NULL,
  sql           text NOT NULL DEFAULT '',
  datasource_id text NOT NULL DEFAULT '',
  ts            bigint NOT NULL,
  PRIMARY KEY (query_id, target_fqn)
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_query_target ON {query_ref} (target_fqn, ts DESC)",
    """CREATE TABLE IF NOT EXISTS {notification} (
  id              text PRIMARY KEY,
  subscription_id text NOT NULL,
  event_id        text NOT NULL DEFAULT '',
  ts              bigint NOT NULL,
  status          text NOT NULL DEFAULT 'unread',
  detail          text NOT NULL DEFAULT '',
  json            text NOT NULL DEFAULT '{{}}'
)""",
    "CREATE INDEX IF NOT EXISTS idx_md_notification_ts ON {notification} (ts DESC)",
    """CREATE TABLE IF NOT EXISTS {custom_property} (
  entity_type   text NOT NULL,
  name          text NOT NULL,
  display_name  text NOT NULL DEFAULT '',
  property_type text NOT NULL DEFAULT 'string',
  description   text NOT NULL DEFAULT '',
  config        text NOT NULL DEFAULT '{{}}',
  created_at    bigint NOT NULL,
  PRIMARY KEY (entity_type, name)
)""",
    """CREATE TABLE IF NOT EXISTS {setting} (
  key  text PRIMARY KEY,
  json text NOT NULL
)""",
)

ENTITY_COLUMNS = (
    "id, entity_type, name, display_name, fqn, parent_fqn, service_type, service_fqn, "
    "description, tier, domain_fqn, deleted, version, updated_at, updated_by, created_at, "
    "search_text, json"
)
EDGE_COLUMNS = (
    "id, from_fqn, from_type, to_fqn, to_type, source, sql, description, columns, "
    "pipeline_fqn, created_at, updated_at"
)
EVENT_COLUMNS = (
    "id, event_type, entity_type, entity_id, entity_fqn, entity_name, user_name, ts, "
    "previous_version, current_version, change"
)
THREAD_COLUMNS = (
    "id, thread_type, about_fqn, about_type, message, created_by, created_at, updated_at, "
    "resolved, json"
)
RUN_COLUMNS = (
    "id, service_fqn, run_type, status, trigger, started_at, finished_at, summary, message"
)
NOTIFICATION_COLUMNS = "id, subscription_id, event_id, ts, status, detail, json"
PROPERTY_COLUMNS = (
    "entity_type, name, display_name, property_type, description, config, created_at"
)
JSON_FIELDS = {"json", "change", "columns", "summary", "config"}
#: Millisecond timestamps; an earlier revision declared them ``integer`` (int4 on
#: PostgreSQL), so bootstrap widens them in place.
BIGINT_COLUMNS = (
    ("entity", "updated_at"), ("entity", "created_at"), ("entity_version", "updated_at"),
    ("lineage_edge", "created_at"), ("lineage_edge", "updated_at"), ("change_event", "ts"),
    ("thread", "created_at"), ("thread", "updated_at"), ("insight_snapshot", "ts"),
    ("ingestion_run", "started_at"), ("ingestion_run", "finished_at"), ("query_ref", "ts"),
    ("notification", "ts"), ("custom_property", "created_at"),
)


class MetadataStoreError(ValueError):
    """A user-facing store error; the message is Chinese and safe to display."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return str(uuid.uuid4())


def today() -> str:
    return dt.date.today().isoformat()


def day_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).date().isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def check_page(page: Any, size: Any) -> tuple[int, int]:
    try:
        page_number = int(page) if page is not None else 1
        page_size = int(size) if size is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError) as error:
        raise MetadataStoreError(400, "分页参数必须是整数。") from error
    if page_number < 1:
        raise MetadataStoreError(400, "页码必须从 1 开始。")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise MetadataStoreError(400, f"每页数量必须在 1–{MAX_PAGE_SIZE} 之间。")
    return page_number, page_size


def resolve_dsn(value: str | None = None) -> str:
    """The configured DSN: ``LATTICE_METADATA_DSN``, else the quality one, else the default."""
    if value:
        return value
    return env(DSN_SETTING) or env("QUALITY_DSN") or QUALITY_DEFAULT_DSN


def split_dsn(url: str) -> tuple[str, str]:
    """Return ``("sqlite", path)`` or ``("postgres", conninfo)`` for a DSN."""
    if not isinstance(url, str) or not url.strip():
        raise MetadataStoreError(400, "元数据库连接串不能为空。")
    text = url.strip()
    if text.lower().startswith("sqlite:"):
        # SQLAlchemy convention: sqlite:///relative.db, sqlite:////absolute.db
        rest = text.split(":", 1)[1]
        path = rest[2:] if rest.startswith("//") else rest
        if path.startswith("/"):
            path = path[1:]
        if not path or path == ":memory:":
            return "sqlite", ":memory:"
        return "sqlite", path
    try:
        return "postgres", parse_postgres_dsn(text)
    except QualityStoreError as error:
        raise MetadataStoreError(error.status_code, str(error).replace("质量元数据库", "元数据库")) from error


class MetadataStore:
    """Repository over PostgreSQL (``lattice_metadata`` schema) or SQLite."""

    def __init__(self, dsn: str | None = None, fallback_path: Path | None = None):
        self.dsn = resolve_dsn(dsn)
        self.fallback_path = fallback_path
        self.backend = ""
        self.location = ""
        self.conninfo = ""
        self.fallback_reason = ""
        self._ready = False
        self._error = ""
        self._lock = threading.RLock()
        self._sqlite: sqlite3.Connection | None = None
        self._pg: Any = None

    # ----- lifecycle ----------------------------------------------------------------
    def bootstrap(self) -> dict[str, Any]:
        """Create the schema, choosing the backend; never raises out of the lifespan."""
        with self._lock:
            self._ready = False
            self._error = ""
            self.fallback_reason = ""
            try:
                backend, target = split_dsn(self.dsn)
            except MetadataStoreError as error:
                backend, target = "", ""
                self._error = str(error)
            if backend == "postgres":
                try:
                    self._open_postgres(target)
                    self._ready = True
                except Exception as error:  # noqa: BLE001 - reported through healthy()
                    self._error = f"PostgreSQL 不可达：{detail(error)}"
            elif backend == "sqlite":
                try:
                    self._open_sqlite(target)
                    self._ready = True
                except Exception as error:  # noqa: BLE001
                    self._error = f"SQLite 无法打开：{detail(error)}"
            if not self._ready and self.fallback_path is not None:
                try:
                    self._open_sqlite(str(self.fallback_path))
                    self.fallback_reason = self._error or "未配置可用的 PostgreSQL 连接"
                    self._ready = True
                    self._error = ""
                except Exception as error:  # noqa: BLE001
                    self._error = (self._error + "；" if self._error else "") + f"本地 SQLite 也无法打开：{detail(error)}"
            return self.healthy()

    def _open_postgres(self, conninfo: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg 未安装")
        self.backend = "postgres"
        self.conninfo = conninfo
        try:
            params = psycopg.conninfo.conninfo_to_dict(conninfo)
        except Exception:  # noqa: BLE001
            params = {}
        self.location = f"{params.get('host', '')}:{params.get('port', '5432')}/{params.get('dbname', '')}".strip("/")
        self._drop_postgres()
        connection = self._connect_postgres()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
                for statement in DDL:
                    cursor.execute(self._render(statement))
                for table, column in BIGINT_COLUMNS:
                    cursor.execute(f"ALTER TABLE {SCHEMA}.{table} ALTER COLUMN {column} TYPE bigint")
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._pg = connection

    def _open_sqlite(self, path: str) -> None:
        if self._sqlite is not None:
            try:
                self._sqlite.close()
            except sqlite3.Error:
                pass
            self._sqlite = None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False, timeout=STATEMENT_TIMEOUT)
        connection.row_factory = sqlite3.Row
        if path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=OFF")
        for statement in DDL:
            connection.execute(self._render(statement, backend="sqlite"))
        connection.commit()
        self._sqlite = connection
        self.backend = "sqlite"
        self.location = path

    def close(self) -> None:
        with self._lock:
            self._drop_postgres()
            if self._sqlite is not None:
                try:
                    self._sqlite.close()
                except sqlite3.Error:
                    pass
                self._sqlite = None
            self._ready = False

    def healthy(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "ok": self._ready,
            "backend": self.backend,
            "location": self.location,
            "schema": SCHEMA if self.backend == "postgres" else "",
            "fallback": bool(self.fallback_reason),
            "fallback_reason": self.fallback_reason,
            "detail": self._error,
        }
        if self._ready:
            try:
                status["entities"] = self.count_entities(None, deleted=None)
                status["detail"] = status["detail"] or (
                    f"元数据存储已连接（{'PostgreSQL ' + SCHEMA if self.backend == 'postgres' else 'SQLite 本地文件'}）"
                )
            except MetadataStoreError as error:
                status["ok"] = False
                status["detail"] = str(error)
        return status

    # ----- SQL plumbing -------------------------------------------------------------
    def _render(self, statement: str, backend: str | None = None) -> str:
        backend = backend or self.backend
        names = {name: (f"{SCHEMA}.{name}" if backend == "postgres" else name) for name in TABLES}
        return statement.format(**names)

    def _table(self, name: str) -> str:
        return f"{SCHEMA}.{name}" if self.backend == "postgres" else name

    def _connect_postgres(self):
        return psycopg.connect(
            self.conninfo,
            connect_timeout=CONNECT_TIMEOUT,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT * 1000}",
            autocommit=False,
        )

    def _postgres_connection(self):
        """One connection kept open under the store lock; reopened after a failure."""
        connection = self._pg
        if connection is not None and not getattr(connection, "closed", False):
            return connection
        try:
            connection = self._connect_postgres()
        except Exception as error:  # noqa: BLE001
            raise MetadataStoreError(503, f"元数据库连接失败：{detail(error)}") from error
        self._pg = connection
        return connection

    def _drop_postgres(self) -> None:
        connection, self._pg = self._pg, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """A cursor on the live backend; commits on success, rolls back on error."""
        if not self._ready:
            raise MetadataStoreError(503, "元数据存储尚未就绪：" + (self._error or "未初始化"))
        with self._lock:
            if self.backend == "sqlite":
                connection = self._sqlite
                assert connection is not None
                cursor = connection.cursor()
                try:
                    yield _SqliteCursor(cursor)
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise MetadataStoreError(500, f"元数据存储操作失败：{detail(error)}") from error
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    cursor.close()
                return
            connection = self._postgres_connection()
            try:
                with connection.cursor() as cursor:
                    yield _PostgresCursor(cursor)
                connection.commit()
            except psycopg.Error as error:
                self._rollback(connection)
                if isinstance(error, psycopg.OperationalError) or getattr(connection, "closed", False):
                    self._drop_postgres()
                raise MetadataStoreError(500, f"元数据存储操作失败：{detail(error)}") from error
            except Exception:
                self._rollback(connection)
                raise

    def _rollback(self, connection: Any) -> None:
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001 - a dead connection is dropped on the next call
            self._drop_postgres()

    # ----- entities -------------------------------------------------------------------
    @staticmethod
    def _entity_row(record: dict[str, Any]) -> dict[str, Any]:
        row = dict(record)
        row["deleted"] = bool(row.get("deleted"))
        row["version"] = round(float(row.get("version") or 0.1), 1)
        row["json"] = loads(row.get("json"), {})
        return row

    def insert_entity(self, row: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('entity')} ({ENTITY_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._entity_params(row),
            )

    def update_entity(self, row: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._table('entity')} SET entity_type = ?, name = ?, display_name = ?, "
                "fqn = ?, parent_fqn = ?, service_type = ?, service_fqn = ?, description = ?, "
                "tier = ?, domain_fqn = ?, deleted = ?, version = ?, updated_at = ?, updated_by = ?, "
                "created_at = ?, search_text = ?, json = ? WHERE id = ?",
                self._entity_params(row)[1:] + [row["id"]],
            )

    @staticmethod
    def _entity_params(row: dict[str, Any]) -> list[Any]:
        return [
            row["id"],
            row["entity_type"],
            row["name"],
            row.get("display_name") or "",
            row["fqn"],
            row.get("parent_fqn") or "",
            row.get("service_type") or "",
            row.get("service_fqn") or "",
            row.get("description") or "",
            row.get("tier") or "",
            row.get("domain_fqn") or "",
            1 if row.get("deleted") else 0,
            float(row.get("version") or 0.1),
            int(row.get("updated_at") or now_ms()),
            row.get("updated_by") or "",
            int(row.get("created_at") or now_ms()),
            row.get("search_text") or "",
            dumps(row.get("json") or {}),
        ]

    def get_entity(
        self, entity_type: str | None, *, id: str | None = None, fqn: str | None = None
    ) -> dict[str, Any] | None:
        clauses, params = [], []
        if id is not None:
            clauses.append("id = ?")
            params.append(id)
        elif fqn is not None:
            clauses.append("fqn = ?")
            params.append(fqn)
        else:
            return None
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {ENTITY_COLUMNS} FROM {self._table('entity')} WHERE " + " AND ".join(clauses),
                params,
            )
            record = cursor.fetchone()
        return self._entity_row(record) if record else None

    def get_entities(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        with self._cursor() as cursor:
            for start in range(0, len(ids), 200):
                chunk = ids[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT {ENTITY_COLUMNS} FROM {self._table('entity')} WHERE id IN ({marks})", chunk
                )
                rows.extend(self._entity_row(record) for record in cursor.fetchall())
        return rows

    def get_entities_by_fqn(self, fqns: list[str], entity_type: str | None = None) -> list[dict[str, Any]]:
        if not fqns:
            return []
        rows: list[dict[str, Any]] = []
        with self._cursor() as cursor:
            for start in range(0, len(fqns), 200):
                chunk = fqns[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                sql = f"SELECT {ENTITY_COLUMNS} FROM {self._table('entity')} WHERE fqn IN ({marks})"
                params: list[Any] = list(chunk)
                if entity_type:
                    sql += " AND entity_type = ?"
                    params.append(entity_type)
                cursor.execute(sql, params)
                rows.extend(self._entity_row(record) for record in cursor.fetchall())
        return rows

    def list_entities(
        self,
        entity_type: str | list[str] | None = None,
        *,
        parent_fqn: str | None = None,
        service_fqn: str | None = None,
        service_type: str | None = None,
        deleted: bool | None = False,
        q: str | None = None,
        fqn_prefix: str | None = None,
        tier: str | None = None,
        domain_fqn: str | None = None,
        ids: list[str] | None = None,
        fqns: list[str] | None = None,
        sort: str = "name",
        page: int = 1,
        size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        where, params = self._entity_filter(
            entity_type, parent_fqn, service_fqn, service_type, deleted, q, fqn_prefix, tier,
            domain_fqn, ids, fqns,
        )
        order = {
            "name": "name ASC, fqn ASC",
            "-name": "name DESC",
            "updated": "updated_at DESC",
            "-updated": "updated_at DESC",
            "created": "created_at DESC",
            "fqn": "fqn ASC",
            "type": "entity_type ASC, fqn ASC",
        }.get(sort or "name", "name ASC, fqn ASC")
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('entity')}{where}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT {ENTITY_COLUMNS} FROM {self._table('entity')}{where} ORDER BY {order} "
                "LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            )
            items = [self._entity_row(record) for record in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "size": size}

    def _entity_filter(
        self, entity_type, parent_fqn, service_fqn, service_type, deleted, q, fqn_prefix, tier,
        domain_fqn, ids, fqns,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if isinstance(entity_type, list):
            if not entity_type:
                clauses.append("1 = 0")
            else:
                clauses.append("entity_type IN (" + ", ".join("?" for _ in entity_type) + ")")
                params.extend(entity_type)
        elif entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if parent_fqn is not None:
            clauses.append("parent_fqn = ?")
            params.append(parent_fqn)
        if service_fqn:
            clauses.append("service_fqn = ?")
            params.append(service_fqn)
        if service_type:
            clauses.append("service_type = ?")
            params.append(service_type)
        if deleted is not None:
            clauses.append("deleted = ?")
            params.append(1 if deleted else 0)
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if domain_fqn:
            clauses.append("domain_fqn = ?")
            params.append(domain_fqn)
        if fqn_prefix:
            clauses.append("(fqn = ? OR fqn LIKE ? ESCAPE '\\')")
            params.extend([fqn_prefix, _like_prefix(fqn_prefix + ".")])
        if ids is not None:
            if not ids:
                clauses.append("1 = 0")
            else:
                clauses.append("id IN (" + ", ".join("?" for _ in ids) + ")")
                params.extend(ids)
        if fqns is not None:
            if not fqns:
                clauses.append("1 = 0")
            else:
                clauses.append("fqn IN (" + ", ".join("?" for _ in fqns) + ")")
                params.extend(fqns)
        for term in search_terms(q):
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            params.append("%" + _escape_like(term) + "%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def all_entities(
        self, entity_type: str | list[str] | None = None, *, deleted: bool | None = False,
        service_fqn: str | None = None, fqn_prefix: str | None = None, limit: int = 20000,
    ) -> list[dict[str, Any]]:
        where, params = self._entity_filter(
            entity_type, None, service_fqn, None, deleted, None, fqn_prefix, None, None, None, None
        )
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {ENTITY_COLUMNS} FROM {self._table('entity')}{where} ORDER BY fqn LIMIT ?",
                params + [int(limit)],
            )
            return [self._entity_row(record) for record in cursor.fetchall()]

    def search_rows(self, entity_types: list[str], limit: int = 20000) -> list[dict[str, Any]]:
        """Id, type, name and search text of live entities, for ranking in memory."""
        if not entity_types:
            return []
        marks = ", ".join("?" for _ in entity_types)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT id, entity_type, fqn, name, search_text FROM {self._table('entity')} "
                f"WHERE deleted = 0 AND entity_type IN ({marks}) LIMIT ?",
                list(entity_types) + [int(limit)],
            )
            return cursor.fetchall()

    def count_entities(self, entity_type: str | None, deleted: bool | None = False) -> int:
        where, params = self._entity_filter(
            entity_type, None, None, None, deleted, None, None, None, None, None, None
        )
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('entity')}{where}", params)
            return int(cursor.fetchone()["total"])

    def counts_by_type(self, deleted: bool | None = False) -> dict[str, int]:
        where, params = self._entity_filter(None, None, None, None, deleted, None, None, None, None, None, None)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT entity_type, count(*) AS total FROM {self._table('entity')}{where} GROUP BY entity_type",
                params,
            )
            return {str(row["entity_type"]): int(row["total"]) for row in cursor.fetchall()}

    def count_children(self, parent_fqns: list[str], deleted: bool | None = False) -> dict[str, int]:
        if not parent_fqns:
            return {}
        counts: dict[str, int] = {}
        with self._cursor() as cursor:
            for start in range(0, len(parent_fqns), 200):
                chunk = parent_fqns[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                sql = (
                    f"SELECT parent_fqn, count(*) AS total FROM {self._table('entity')} "
                    f"WHERE parent_fqn IN ({marks})"
                )
                params: list[Any] = list(chunk)
                if deleted is not None:
                    sql += " AND deleted = ?"
                    params.append(1 if deleted else 0)
                cursor.execute(sql + " GROUP BY parent_fqn", params)
                for row in cursor.fetchall():
                    counts[str(row["parent_fqn"])] = int(row["total"])
        return counts

    def delete_entity(self, entity_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(f"DELETE FROM {self._table('entity')} WHERE id = ?", [entity_id])
            cursor.execute(f"DELETE FROM {self._table('entity_version')} WHERE entity_id = ?", [entity_id])
            cursor.execute(
                f"DELETE FROM {self._table('relationship')} WHERE from_id = ? OR to_id = ?",
                [entity_id, entity_id],
            )

    def set_deleted(self, ids: list[str], deleted: bool, updated_at: int, updated_by: str) -> None:
        if not ids:
            return
        with self._cursor() as cursor:
            for start in range(0, len(ids), 200):
                chunk = ids[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"UPDATE {self._table('entity')} SET deleted = ?, updated_at = ?, updated_by = ? "
                    f"WHERE id IN ({marks})",
                    [1 if deleted else 0, updated_at, updated_by] + chunk,
                )

    # ----- versions -------------------------------------------------------------------
    def save_version(
        self, entity_id: str, version: float, document: dict[str, Any], change: dict[str, Any],
        updated_at: int, updated_by: str,
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('entity_version')} WHERE entity_id = ? AND version = CAST(? AS real)",
                [entity_id, float(version)],
            )
            cursor.execute(
                f"INSERT INTO {self._table('entity_version')} "
                "(entity_id, version, json, change, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                [entity_id, float(version), dumps(document), dumps(change), updated_at, updated_by],
            )

    def list_versions(self, entity_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT entity_id, version, json, change, updated_at, updated_by "
                f"FROM {self._table('entity_version')} WHERE entity_id = ? ORDER BY version DESC",
                [entity_id],
            )
            return [self._version_row(row) for row in cursor.fetchall()]

    def get_version(self, entity_id: str, version: float) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT entity_id, version, json, change, updated_at, updated_by "
                f"FROM {self._table('entity_version')} WHERE entity_id = ? AND version = CAST(? AS real)",
                [entity_id, float(version)],
            )
            row = cursor.fetchone()
        return self._version_row(row) if row else None

    @staticmethod
    def _version_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity_id": row["entity_id"],
            "version": round(float(row["version"]), 1),
            "json": loads(row["json"], {}),
            "change": loads(row["change"], {}),
            "updated_at": int(row["updated_at"]),
            "updated_by": row["updated_by"] or "",
        }

    # ----- relationships ----------------------------------------------------------------
    def add_relationship(
        self, from_id: str, from_type: str, to_id: str, to_type: str, relation: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('relationship')} (from_id, from_type, to_id, to_type, relation, json) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (from_id, to_id, relation) DO UPDATE SET json = ?",
                [from_id, from_type, to_id, to_type, relation, dumps(extra or {}), dumps(extra or {})],
            )

    def remove_relationship(self, from_id: str, to_id: str, relation: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('relationship')} WHERE from_id = ? AND to_id = ? AND relation = ?",
                [from_id, to_id, relation],
            )

    def remove_relationships(
        self, *, from_id: str | None = None, to_id: str | None = None, relation: str | None = None,
        to_type: str | None = None, from_type: str | None = None,
    ) -> None:
        clauses, params = self._relationship_filter(from_id, to_id, relation, to_type, from_type)
        if not clauses:
            return
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('relationship')} WHERE " + " AND ".join(clauses), params
            )

    def relationships(
        self, *, from_id: str | None = None, to_id: str | None = None, relation: str | None = None,
        to_type: str | None = None, from_type: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses, params = self._relationship_filter(from_id, to_id, relation, to_type, from_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT from_id, from_type, to_id, to_type, relation, json FROM {self._table('relationship')}"
                f"{where} LIMIT ?",
                params + [int(limit)],
            )
            return [
                {
                    "from_id": row["from_id"],
                    "from_type": row["from_type"],
                    "to_id": row["to_id"],
                    "to_type": row["to_type"],
                    "relation": row["relation"],
                    "extra": loads(row["json"], {}),
                }
                for row in cursor.fetchall()
            ]

    def relationships_to_many(self, to_ids: list[str], relation: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not to_ids:
            return rows
        with self._cursor() as cursor:
            for start in range(0, len(to_ids), 200):
                chunk = to_ids[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT from_id, from_type, to_id, to_type, relation, json FROM {self._table('relationship')} "
                    f"WHERE relation = ? AND to_id IN ({marks})",
                    [relation] + chunk,
                )
                rows.extend(
                    {
                        "from_id": row["from_id"],
                        "from_type": row["from_type"],
                        "to_id": row["to_id"],
                        "to_type": row["to_type"],
                        "relation": row["relation"],
                        "extra": loads(row["json"], {}),
                    }
                    for row in cursor.fetchall()
                )
        return rows

    @staticmethod
    def _relationship_filter(from_id, to_id, relation, to_type, from_type) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if from_id is not None:
            clauses.append("from_id = ?")
            params.append(from_id)
        if to_id is not None:
            clauses.append("to_id = ?")
            params.append(to_id)
        if relation is not None:
            clauses.append("relation = ?")
            params.append(relation)
        if to_type is not None:
            clauses.append("to_type = ?")
            params.append(to_type)
        if from_type is not None:
            clauses.append("from_type = ?")
            params.append(from_type)
        return clauses, params

    # ----- tags -------------------------------------------------------------------------
    def set_tag(self, source: str, tag_fqn: str, target_fqn: str, label_type: str, state: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('tag_usage')} (source, tag_fqn, target_fqn, label_type, state) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, tag_fqn, target_fqn) "
                "DO UPDATE SET label_type = ?, state = ?",
                [source, tag_fqn, target_fqn, label_type, state, label_type, state],
            )

    def remove_tag(self, source: str, tag_fqn: str, target_fqn: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('tag_usage')} WHERE source = ? AND tag_fqn = ? AND target_fqn = ?",
                [source, tag_fqn, target_fqn],
            )

    def clear_tags(self, target_fqn: str, source: str | None = None) -> None:
        with self._cursor() as cursor:
            if source is None:
                cursor.execute(f"DELETE FROM {self._table('tag_usage')} WHERE target_fqn = ?", [target_fqn])
            else:
                cursor.execute(
                    f"DELETE FROM {self._table('tag_usage')} WHERE target_fqn = ? AND source = ?",
                    [target_fqn, source],
                )

    def tags_for_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Tags of an entity and everything nested under its FQN (columns, terms…)."""
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT source, tag_fqn, target_fqn, label_type, state FROM {self._table('tag_usage')} "
                "WHERE target_fqn = ? OR target_fqn LIKE ? ESCAPE '\\'",
                [prefix, _like_prefix(prefix + ".")],
            )
            return [dict(row) for row in cursor.fetchall()]

    def tags_for_targets(self, targets: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not targets:
            return rows
        with self._cursor() as cursor:
            for start in range(0, len(targets), 200):
                chunk = targets[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT source, tag_fqn, target_fqn, label_type, state FROM {self._table('tag_usage')} "
                    f"WHERE target_fqn IN ({marks})",
                    chunk,
                )
                rows.extend(dict(row) for row in cursor.fetchall())
        return rows

    def tag_targets(self, tag_fqn: str, limit: int = 5000) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT source, tag_fqn, target_fqn, label_type, state FROM {self._table('tag_usage')} "
                "WHERE tag_fqn = ? LIMIT ?",
                [tag_fqn, int(limit)],
            )
            return [dict(row) for row in cursor.fetchall()]

    def tag_usage_counts(self, source: str | None = None) -> dict[str, int]:
        with self._cursor() as cursor:
            if source is None:
                cursor.execute(
                    f"SELECT tag_fqn, count(*) AS total FROM {self._table('tag_usage')} GROUP BY tag_fqn"
                )
            else:
                cursor.execute(
                    f"SELECT tag_fqn, count(*) AS total FROM {self._table('tag_usage')} WHERE source = ? "
                    "GROUP BY tag_fqn",
                    [source],
                )
            return {str(row["tag_fqn"]): int(row["total"]) for row in cursor.fetchall()}

    def delete_tag_prefix(self, tag_fqn: str) -> None:
        """Remove every usage of a tag/term and of the tags/terms nested under it."""
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('tag_usage')} WHERE tag_fqn = ? OR tag_fqn LIKE ? ESCAPE '\\'",
                [tag_fqn, _like_prefix(tag_fqn + ".")],
            )

    def delete_target_prefix(self, target_fqn: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('tag_usage')} WHERE target_fqn = ? OR target_fqn LIKE ? ESCAPE '\\'",
                [target_fqn, _like_prefix(target_fqn + ".")],
            )

    def rename_tag_prefix(self, old: str, new: str) -> None:
        self._rename_prefix("tag_usage", "tag_fqn", old, new)

    def rename_target_prefix(self, old: str, new: str) -> None:
        self._rename_prefix("tag_usage", "target_fqn", old, new)
        self._rename_prefix("lineage_edge", "from_fqn", old, new)
        self._rename_prefix("lineage_edge", "to_fqn", old, new)
        self._rename_prefix("usage_daily", "target_fqn", old, new)
        self._rename_prefix("query_ref", "target_fqn", old, new)
        self._rename_prefix("thread", "about_fqn", old, new)

    def _rename_prefix(self, table: str, column: str, old: str, new: str) -> None:
        if old == new:
            return
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._table(table)} SET {column} = ? WHERE {column} = ?", [new, old]
            )
            cursor.execute(
                f"SELECT DISTINCT {column} AS value FROM {self._table(table)} WHERE {column} LIKE ? ESCAPE '\\'",
                [_like_prefix(old + ".")],
            )
            values = [str(row["value"]) for row in cursor.fetchall()]
            for value in values:
                cursor.execute(
                    f"UPDATE {self._table(table)} SET {column} = ? WHERE {column} = ?",
                    [new + value[len(old):], value],
                )

    # ----- lineage --------------------------------------------------------------------
    def upsert_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        stamp = now_ms()
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {EDGE_COLUMNS} FROM {self._table('lineage_edge')} WHERE from_fqn = ? AND to_fqn = ?",
                [edge["from_fqn"], edge["to_fqn"]],
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    f"UPDATE {self._table('lineage_edge')} SET from_type = ?, to_type = ?, source = ?, sql = ?, "
                    "description = ?, columns = ?, pipeline_fqn = ?, updated_at = ? WHERE id = ?",
                    [
                        edge.get("from_type") or existing["from_type"],
                        edge.get("to_type") or existing["to_type"],
                        edge.get("source") or existing["source"],
                        edge.get("sql") if edge.get("sql") is not None else existing["sql"],
                        edge.get("description") if edge.get("description") is not None else existing["description"],
                        dumps(edge.get("columns") if edge.get("columns") is not None else loads(existing["columns"], [])),
                        edge.get("pipeline_fqn") if edge.get("pipeline_fqn") is not None else existing["pipeline_fqn"],
                        stamp,
                        existing["id"],
                    ],
                )
                edge_id = existing["id"]
            else:
                edge_id = edge.get("id") or new_id()
                cursor.execute(
                    f"INSERT INTO {self._table('lineage_edge')} ({EDGE_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        edge_id,
                        edge["from_fqn"],
                        edge.get("from_type") or "",
                        edge["to_fqn"],
                        edge.get("to_type") or "",
                        edge.get("source") or "manual",
                        edge.get("sql") or "",
                        edge.get("description") or "",
                        dumps(edge.get("columns") or []),
                        edge.get("pipeline_fqn") or "",
                        stamp,
                        stamp,
                    ],
                )
            cursor.execute(f"SELECT {EDGE_COLUMNS} FROM {self._table('lineage_edge')} WHERE id = ?", [edge_id])
            return self._edge_row(cursor.fetchone())

    @staticmethod
    def _edge_row(row: dict[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["columns"] = loads(record.get("columns"), [])
        return record

    def delete_edge(self, from_fqn: str, to_fqn: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('lineage_edge')} WHERE from_fqn = ? AND to_fqn = ?",
                [from_fqn, to_fqn],
            )
            return cursor.rowcount > 0

    def delete_edges_of(self, fqn: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('lineage_edge')} WHERE from_fqn = ? OR to_fqn = ? "
                "OR from_fqn LIKE ? ESCAPE '\\' OR to_fqn LIKE ? ESCAPE '\\'",
                [fqn, fqn, _like_prefix(fqn + "."), _like_prefix(fqn + ".")],
            )

    def delete_edges_by_source(self, source: str, fqn_prefix: str | None = None) -> None:
        with self._cursor() as cursor:
            if fqn_prefix:
                cursor.execute(
                    f"DELETE FROM {self._table('lineage_edge')} WHERE source = ? AND "
                    "(to_fqn = ? OR to_fqn LIKE ? ESCAPE '\\')",
                    [source, fqn_prefix, _like_prefix(fqn_prefix + ".")],
                )
            else:
                cursor.execute(f"DELETE FROM {self._table('lineage_edge')} WHERE source = ?", [source])

    def edges(self, *, from_fqn: str | None = None, to_fqn: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if from_fqn is not None:
            clauses.append("from_fqn = ?")
            params.append(from_fqn)
        if to_fqn is not None:
            clauses.append("to_fqn = ?")
            params.append(to_fqn)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {EDGE_COLUMNS} FROM {self._table('lineage_edge')}{where} ORDER BY from_fqn, to_fqn LIMIT 5000",
                params,
            )
            return [self._edge_row(row) for row in cursor.fetchall()]

    def edges_for_many(self, fqns: list[str], direction: str) -> list[dict[str, Any]]:
        column = "to_fqn" if direction == "upstream" else "from_fqn"
        rows: list[dict[str, Any]] = []
        if not fqns:
            return rows
        with self._cursor() as cursor:
            for start in range(0, len(fqns), 200):
                chunk = fqns[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT {EDGE_COLUMNS} FROM {self._table('lineage_edge')} WHERE {column} IN ({marks})", chunk
                )
                rows.extend(self._edge_row(row) for row in cursor.fetchall())
        return rows

    def edge_counts(self) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT source, count(*) AS total FROM {self._table('lineage_edge')} GROUP BY source")
            return {str(row["source"]): int(row["total"]) for row in cursor.fetchall()}

    # ----- change events ------------------------------------------------------------------
    def add_event(self, event: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('change_event')} ({EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    event.get("id") or new_id(),
                    event["event_type"],
                    event["entity_type"],
                    event.get("entity_id") or "",
                    event.get("entity_fqn") or "",
                    event.get("entity_name") or "",
                    event.get("user_name") or "",
                    int(event.get("ts") or now_ms()),
                    event.get("previous_version"),
                    event.get("current_version"),
                    dumps(event.get("change") or {}),
                ],
            )

    def list_events(
        self, *, entity_fqn: str | None = None, entity_type: str | None = None,
        event_type: str | None = None, user_name: str | None = None, since: int | None = None,
        include_children: bool = False, page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        clauses: list[str] = []
        params: list[Any] = []
        if entity_fqn:
            if include_children:
                clauses.append("(entity_fqn = ? OR entity_fqn LIKE ? ESCAPE '\\')")
                params.extend([entity_fqn, _like_prefix(entity_fqn + ".")])
            else:
                clauses.append("entity_fqn = ?")
                params.append(entity_fqn)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if user_name:
            clauses.append("user_name = ?")
            params.append(user_name)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(int(since))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('change_event')}{where}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT {EVENT_COLUMNS} FROM {self._table('change_event')}{where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            )
            items = []
            for row in cursor.fetchall():
                record = dict(row)
                record["change"] = loads(record.get("change"), {})
                items.append(record)
        return {"items": items, "total": total, "page": page, "size": size}

    def events_by_user(self, since: int) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT user_name, count(*) AS total, max(ts) AS last_ts FROM {self._table('change_event')} "
                "WHERE ts >= ? AND user_name <> '' GROUP BY user_name ORDER BY total DESC LIMIT 50",
                [int(since)],
            )
            return [
                {"user_name": row["user_name"], "total": int(row["total"]), "last_ts": int(row["last_ts"])}
                for row in cursor.fetchall()
            ]

    def event_counts(self, since: int) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT event_type, count(*) AS total FROM {self._table('change_event')} WHERE ts >= ? "
                "GROUP BY event_type",
                [int(since)],
            )
            return {str(row["event_type"]): int(row["total"]) for row in cursor.fetchall()}

    # ----- activity threads --------------------------------------------------------------
    def insert_thread(self, thread: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('thread')} ({THREAD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    thread["id"],
                    thread["thread_type"],
                    thread.get("about_fqn") or "",
                    thread.get("about_type") or "",
                    thread.get("message") or "",
                    thread.get("created_by") or "",
                    int(thread.get("created_at") or now_ms()),
                    int(thread.get("updated_at") or now_ms()),
                    1 if thread.get("resolved") else 0,
                    dumps(thread.get("json") or {}),
                ],
            )

    def update_thread(self, thread: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._table('thread')} SET message = ?, updated_at = ?, resolved = ?, json = ? WHERE id = ?",
                [
                    thread.get("message") or "",
                    int(thread.get("updated_at") or now_ms()),
                    1 if thread.get("resolved") else 0,
                    dumps(thread.get("json") or {}),
                    thread["id"],
                ],
            )

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT {THREAD_COLUMNS} FROM {self._table('thread')} WHERE id = ?", [thread_id])
            row = cursor.fetchone()
        return self._thread_row(row) if row else None

    def delete_thread(self, thread_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(f"DELETE FROM {self._table('thread')} WHERE id = ?", [thread_id])

    def list_threads(
        self, *, about_fqn: str | None = None, thread_type: str | None = None,
        resolved: bool | None = None, include_children: bool = False,
        page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        clauses: list[str] = []
        params: list[Any] = []
        if about_fqn:
            if include_children:
                clauses.append("(about_fqn = ? OR about_fqn LIKE ? ESCAPE '\\')")
                params.extend([about_fqn, _like_prefix(about_fqn + ".")])
            else:
                clauses.append("about_fqn = ?")
                params.append(about_fqn)
        if thread_type:
            clauses.append("thread_type = ?")
            params.append(thread_type)
        if resolved is not None:
            clauses.append("resolved = ?")
            params.append(1 if resolved else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('thread')}{where}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT {THREAD_COLUMNS} FROM {self._table('thread')}{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            )
            items = [self._thread_row(row) for row in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "size": size}

    @staticmethod
    def _thread_row(row: dict[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["resolved"] = bool(record.get("resolved"))
        record["json"] = loads(record.get("json"), {})
        return record

    # ----- insight snapshots ------------------------------------------------------------
    def save_snapshot(self, day: str, snapshot: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('insight_snapshot')} (day, ts, json) VALUES (?, ?, ?) "
                "ON CONFLICT (day) DO UPDATE SET ts = ?, json = ?",
                [day, now_ms(), dumps(snapshot), now_ms(), dumps(snapshot)],
            )

    def list_snapshots(self, since_day: str | None = None) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            if since_day:
                cursor.execute(
                    f"SELECT day, ts, json FROM {self._table('insight_snapshot')} WHERE day >= ? ORDER BY day",
                    [since_day],
                )
            else:
                cursor.execute(f"SELECT day, ts, json FROM {self._table('insight_snapshot')} ORDER BY day")
            return [
                {"day": row["day"], "ts": int(row["ts"]), "data": loads(row["json"], {})}
                for row in cursor.fetchall()
            ]

    def get_snapshot(self, day: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT day, ts, json FROM {self._table('insight_snapshot')} WHERE day = ?", [day])
            row = cursor.fetchone()
        return {"day": row["day"], "ts": int(row["ts"]), "data": loads(row["json"], {})} if row else None

    # ----- ingestion runs ---------------------------------------------------------------
    def insert_run(self, run: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('ingestion_run')} ({RUN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run["id"],
                    run.get("service_fqn") or "",
                    run.get("run_type") or "metadata",
                    run.get("status") or "running",
                    run.get("trigger") or "manual",
                    int(run.get("started_at") or now_ms()),
                    run.get("finished_at"),
                    dumps(run.get("summary") or {}),
                    run.get("message") or "",
                ],
            )

    def update_run(self, run: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._table('ingestion_run')} SET status = ?, finished_at = ?, summary = ?, message = ? WHERE id = ?",
                [
                    run.get("status") or "running",
                    run.get("finished_at"),
                    dumps(run.get("summary") or {}),
                    (run.get("message") or "")[:4000],
                    run["id"],
                ],
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT {RUN_COLUMNS} FROM {self._table('ingestion_run')} WHERE id = ?", [run_id])
            row = cursor.fetchone()
        return self._run_row(row) if row else None

    def list_runs(
        self, *, service_fqn: str | None = None, run_type: str | None = None,
        page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        clauses: list[str] = []
        params: list[Any] = []
        if service_fqn:
            clauses.append("service_fqn = ?")
            params.append(service_fqn)
        if run_type:
            clauses.append("run_type = ?")
            params.append(run_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('ingestion_run')}{where}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT {RUN_COLUMNS} FROM {self._table('ingestion_run')}{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            )
            items = [self._run_row(row) for row in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "size": size}

    def latest_runs(self) -> dict[str, dict[str, Any]]:
        """The most recent run of every service, keyed by service FQN."""
        latest: dict[str, dict[str, Any]] = {}
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {RUN_COLUMNS} FROM {self._table('ingestion_run')} ORDER BY started_at DESC LIMIT 2000"
            )
            for row in cursor.fetchall():
                record = self._run_row(row)
                latest.setdefault(record["service_fqn"], record)
        return latest

    @staticmethod
    def _run_row(row: dict[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["summary"] = loads(record.get("summary"), {})
        return record

    def prune_runs(self, keep: int = 500) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {self._table('ingestion_run')} ORDER BY started_at DESC LIMIT 100000 OFFSET ?",
                [int(keep)],
            )
            stale = [row["id"] for row in cursor.fetchall()]
            for start in range(0, len(stale), 200):
                chunk = stale[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(f"DELETE FROM {self._table('ingestion_run')} WHERE id IN ({marks})", chunk)

    # ----- usage --------------------------------------------------------------------------
    def add_usage(self, target_fqn: str, day: str, *, queries: int = 0, views: int = 0) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('usage_daily')} (target_fqn, day, queries, views) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (target_fqn, day) DO UPDATE SET queries = "
                f"{self._table('usage_daily')}.queries + ?, views = {self._table('usage_daily')}.views + ?",
                [target_fqn, day, int(queries), int(views), int(queries), int(views)],
            )

    def set_usage(self, target_fqn: str, day: str, *, queries: int) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('usage_daily')} (target_fqn, day, queries, views) VALUES (?, ?, ?, 0) "
                "ON CONFLICT (target_fqn, day) DO UPDATE SET queries = ?",
                [target_fqn, day, int(queries), int(queries)],
            )

    def usage(self, target_fqn: str, since_day: str) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT day, queries, views FROM {self._table('usage_daily')} WHERE target_fqn = ? AND day >= ? ORDER BY day",
                [target_fqn, since_day],
            )
            return [dict(row) for row in cursor.fetchall()]

    def usage_totals(self, targets: list[str], since_day: str) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        if not targets:
            return totals
        with self._cursor() as cursor:
            for start in range(0, len(targets), 200):
                chunk = targets[start : start + 200]
                marks = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT target_fqn, sum(queries) AS queries, sum(views) AS views FROM {self._table('usage_daily')} "
                    f"WHERE day >= ? AND target_fqn IN ({marks}) GROUP BY target_fqn",
                    [since_day] + chunk,
                )
                for row in cursor.fetchall():
                    totals[str(row["target_fqn"])] = {
                        "queries": int(row["queries"] or 0),
                        "views": int(row["views"] or 0),
                    }
        return totals

    def top_usage(self, since_day: str, kind: str = "views", limit: int = 10) -> list[dict[str, Any]]:
        column = "views" if kind == "views" else "queries"
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT target_fqn, sum({column}) AS total FROM {self._table('usage_daily')} WHERE day >= ? "
                f"GROUP BY target_fqn HAVING sum({column}) > 0 ORDER BY total DESC LIMIT ?",
                [since_day, int(limit)],
            )
            return [{"target_fqn": row["target_fqn"], "total": int(row["total"])} for row in cursor.fetchall()]

    def used_targets(self, since_day: str) -> set[str]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT target_fqn FROM {self._table('usage_daily')} WHERE day >= ? AND (queries > 0 OR views > 0)",
                [since_day],
            )
            return {str(row["target_fqn"]) for row in cursor.fetchall()}

    def upsert_query_ref(self, query_id: str, target_fqn: str, sql: str, datasource_id: str, ts: int) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT 1 AS found FROM {self._table('query_ref')} WHERE query_id = ? AND target_fqn = ?",
                [query_id, target_fqn],
            )
            if cursor.fetchone():
                return False
            cursor.execute(
                f"INSERT INTO {self._table('query_ref')} (query_id, target_fqn, sql, datasource_id, ts) VALUES (?, ?, ?, ?, ?)",
                [query_id, target_fqn, sql[:20000], datasource_id, int(ts)],
            )
            return True

    def queries_for(self, target_fqn: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT query_id, target_fqn, sql, datasource_id, ts FROM {self._table('query_ref')} "
                "WHERE target_fqn = ? ORDER BY ts DESC LIMIT ?",
                [target_fqn, int(limit)],
            )
            return [dict(row) for row in cursor.fetchall()]

    def query_counts(self) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT target_fqn, count(*) AS total FROM {self._table('query_ref')} GROUP BY target_fqn")
            return {str(row["target_fqn"]): int(row["total"]) for row in cursor.fetchall()}

    # ----- notifications ---------------------------------------------------------------
    def insert_notification(self, notification: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('notification')} ({NOTIFICATION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    notification.get("id") or new_id(),
                    notification["subscription_id"],
                    notification.get("event_id") or "",
                    int(notification.get("ts") or now_ms()),
                    notification.get("status") or "unread",
                    (notification.get("detail") or "")[:2000],
                    dumps(notification.get("json") or {}),
                ],
            )

    def update_notification(self, notification_id: str, *, status: str, detail: str | None = None) -> None:
        with self._cursor() as cursor:
            if detail is None:
                cursor.execute(
                    f"UPDATE {self._table('notification')} SET status = ? WHERE id = ?", [status, notification_id]
                )
            else:
                cursor.execute(
                    f"UPDATE {self._table('notification')} SET status = ?, detail = ? WHERE id = ?",
                    [status, detail[:2000], notification_id],
                )

    def list_notifications(
        self, *, subscription_id: str | None = None, status: str | None = None,
        page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        page, size = check_page(page, size)
        clauses: list[str] = []
        params: list[Any] = []
        if subscription_id:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM {self._table('notification')}{where}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT {NOTIFICATION_COLUMNS} FROM {self._table('notification')}{where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            )
            items = []
            for row in cursor.fetchall():
                record = dict(row)
                record["json"] = loads(record.get("json"), {})
                items.append(record)
        return {"items": items, "total": total, "page": page, "size": size}

    def notification_counts(self) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT status, count(*) AS total FROM {self._table('notification')} GROUP BY status")
            return {str(row["status"]): int(row["total"]) for row in cursor.fetchall()}

    def delete_notifications(self, subscription_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(f"DELETE FROM {self._table('notification')} WHERE subscription_id = ?", [subscription_id])

    # ----- custom properties ---------------------------------------------------------------
    def upsert_property(self, definition: dict[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('custom_property')} ({PROPERTY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (entity_type, name) DO UPDATE SET display_name = ?, property_type = ?, description = ?, config = ?",
                [
                    definition["entity_type"],
                    definition["name"],
                    definition.get("display_name") or "",
                    definition.get("property_type") or "string",
                    definition.get("description") or "",
                    dumps(definition.get("config") or {}),
                    int(definition.get("created_at") or now_ms()),
                    definition.get("display_name") or "",
                    definition.get("property_type") or "string",
                    definition.get("description") or "",
                    dumps(definition.get("config") or {}),
                ],
            )

    def delete_property(self, entity_type: str, name: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self._table('custom_property')} WHERE entity_type = ? AND name = ?",
                [entity_type, name],
            )
            return cursor.rowcount > 0

    def properties(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            if entity_type:
                cursor.execute(
                    f"SELECT {PROPERTY_COLUMNS} FROM {self._table('custom_property')} WHERE entity_type = ? ORDER BY name",
                    [entity_type],
                )
            else:
                cursor.execute(f"SELECT {PROPERTY_COLUMNS} FROM {self._table('custom_property')} ORDER BY entity_type, name")
            items = []
            for row in cursor.fetchall():
                record = dict(row)
                record["config"] = loads(record.get("config"), {})
                items.append(record)
            return items

    # ----- settings ------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._cursor() as cursor:
            cursor.execute(f"SELECT json FROM {self._table('setting')} WHERE key = ?", [key])
            row = cursor.fetchone()
        return loads(row["json"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self._table('setting')} (key, json) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET json = ?",
                [key, dumps(value), dumps(value)],
            )


# ---------------------------------------------------------------------------
# Cursor adapters: one SQL text, two placeholder styles and row types
# ---------------------------------------------------------------------------
class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self.cursor = cursor

    def execute(self, sql: str, params: Any = None) -> None:
        self.cursor.execute(sql, list(params or []))

    def fetchone(self) -> dict[str, Any] | None:
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount


class _PostgresCursor:
    def __init__(self, cursor: Any):
        self.cursor = cursor

    def execute(self, sql: str, params: Any = None) -> None:
        self.cursor.execute(sql.replace("?", "%s"), list(params or []))

    def _columns(self) -> list[str]:
        return [column.name for column in self.cursor.description or []]

    def fetchone(self) -> dict[str, Any] | None:
        row = self.cursor.fetchone()
        return dict(zip(self._columns(), row)) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        columns = self._columns()
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(prefix: str) -> str:
    return _escape_like(prefix) + "%"


def search_terms(q: Any) -> list[str]:
    if not isinstance(q, str):
        return []
    terms = [term.strip().lower() for term in q.replace("　", " ").split() if term.strip()]
    return terms[:MAX_SEARCH_TERMS]
