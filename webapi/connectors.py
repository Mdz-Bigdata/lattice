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

"""Read-only connectors for the supported query engines and lake formats.

Every connector exposes the same small surface: ``test``, ``schemas``,
``tables``, ``table`` (columns), ``preview`` and ``query``. User SQL is parsed
with sqlglot in the engine's dialect and rejected unless it is a single
read-only ``SELECT``/``WITH`` statement; results are bounded by a row limit and a
wall-clock timeout. Lake formats (Iceberg, Paimon) are read into Arrow with their
official Python SDKs and queried through an in-memory DuckDB with external file
access disabled.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import decimal
import importlib
import json
import math
import re
import ssl as ssl_module
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import httpx
import sqlglot
from sqlglot import exp

SECRET_MASK = "••••••"
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
QUERY_TIMEOUT = 30
CONNECT_TIMEOUT = 5
LAKE_SCAN_ROWS = 500_000
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\Z")
LOOSE_IDENTIFIER = re.compile(r"^[^\x00-\x1f\"`'\\;]{1,255}\Z")
HEADER_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9-]*)\s*:\s*(.*?)\s*$")


class ConnectorError(ValueError):
    """A user-facing error; the message is safe to display."""


def field(
    name: str,
    label: str,
    type: str = "text",
    *,
    required: bool = False,
    default: Any = None,
    placeholder: str | None = None,
    help: str | None = None,
    options: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "name": name,
        "label": label,
        "type": type,
        "required": required,
    }
    if default is not None:
        descriptor["default"] = default
    if placeholder:
        descriptor["placeholder"] = placeholder
    if help:
        descriptor["help"] = help
    if options:
        descriptor["options"] = [
            {"value": value, "label": label} for value, label in options
        ]
    return descriptor


def json_value(value: Any) -> Any:
    """Convert driver-specific values into JSON-serializable equivalents."""
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (uuid.UUID, dt.timedelta)):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "as_py"):
        return json_value(value.as_py())
    return str(value)


def chart_hint(columns: list[str], rows: list[list[Any]]) -> dict[str, str]:
    """Pick a dimension/metric pair only when the data genuinely supports it."""
    metric = next(
        (
            index
            for index in range(len(columns) - 1, 0, -1)
            if any(
                isinstance(row[index], (int, float)) and not isinstance(row[index], bool)
                for row in rows
            )
            and all(
                value is None
                or (isinstance(value, (int, float)) and not isinstance(value, bool))
                for value in (row[index] for row in rows)
            )
        ),
        None,
    )
    if metric is None:
        return {"dimension": "", "metric": "", "type": "table"}
    return {"dimension": columns[0], "metric": columns[metric], "type": "bar"}


def guard_sql(sql: str, dialect: str) -> exp.Expression:
    """Accept exactly one read-only query in the given dialect."""
    if not isinstance(sql, str) or not sql.strip():
        raise ConnectorError("SQL 不能为空。")
    if "\x00" in sql:
        raise ConnectorError("SQL 包含非法字符。")
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as error:
        raise ConnectorError("SQL 无法解析：" + str(error)[:300]) from error
    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        raise ConnectorError("仅支持一条只读 SELECT / WITH 查询。")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise ConnectorError("仅支持只读 SELECT / WITH 查询，不允许修改数据或结构。")
    if tree.find(exp.Into):
        raise ConnectorError("不允许导出或写入数据。")
    for node in tree.walk():
        if isinstance(node, (exp.Command, exp.DDL, exp.DML, exp.Transaction)):
            raise ConnectorError("仅支持只读查询。")
        if isinstance(node, exp.Lock):
            raise ConnectorError("不允许锁定数据。")
    return tree


def strip_statement(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def wrap_limit(sql: str, limit: int) -> str:
    """Bound the result of an already-validated query without rewriting it."""
    return f"SELECT * FROM (\n{strip_statement(sql)}\n) AS lattice_q LIMIT {int(limit)}"


def clamp_limit(limit: Any) -> int:
    try:
        value = int(limit) if limit is not None else DEFAULT_LIMIT
    except (TypeError, ValueError) as error:
        raise ConnectorError("limit 必须是整数。") from error
    return max(1, min(value, MAX_LIMIT))


def referenced_tables(tree: exp.Expression) -> list[tuple[str | None, str]]:
    """Return (schema, table) pairs referenced by a parsed query, excluding CTEs."""
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    references: list[tuple[str | None, str]] = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        if table.catalog:
            raise ConnectorError("不支持三段式表名，请使用 库.表 的形式。")
        pair = (table.db or None, table.name)
        if not table.db and table.name in ctes:
            continue
        if pair not in references:
            references.append(pair)
    return references


def run_with_timeout(
    action: Callable[[], Any], timeout: float, cancel: Callable[[], Any] | None = None
) -> Any:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(action)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as error:
        if cancel is not None:
            try:
                cancel()
            except Exception:  # noqa: BLE001 - best-effort cancellation
                pass
        raise ConnectorError(f"操作超过 {int(timeout)} 秒未完成，已取消。") from error
    finally:
        executor.shutdown(wait=False)


def check_identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not LOOSE_IDENTIFIER.match(value):
        raise ConnectorError(f"{what}无效。")
    return value


def parse_header_lines(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = HEADER_LINE.match(line)
        if not match:
            raise ConnectorError("附加请求头格式应为每行一个 `名称: 值`。")
        headers[match.group(1)] = match.group(2)
    return headers


class Connector:
    """Base class; subclasses declare metadata and implement the engine calls."""

    type_id = ""
    label = ""
    category = ""
    description = ""
    driver = ""
    dialect = ""
    default_port: int | None = None
    fields: list[dict[str, Any]] = []
    identifier_quote = '"'
    supports_schemas = True

    def __init__(self, config: dict[str, Any]):
        self.config = self.normalize(config)

    # ----- metadata -----------------------------------------------------
    @classmethod
    def descriptor(cls) -> dict[str, Any]:
        return {
            "id": cls.type_id,
            "label": cls.label,
            "category": cls.category,
            "description": cls.description,
            "driver": cls.driver,
            "dialect": cls.dialect,
            "default_port": cls.default_port,
            "fields": cls.fields,
        }

    @classmethod
    def secret_fields(cls) -> set[str]:
        return {item["name"] for item in cls.fields if item["type"] == "password"}

    @classmethod
    def normalize(cls, config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ConnectorError("连接配置必须是对象。")
        unknown = set(config) - {item["name"] for item in cls.fields}
        if unknown:
            raise ConnectorError("未知的连接参数：" + "、".join(sorted(unknown)))
        normalized: dict[str, Any] = {}
        for item in cls.fields:
            name = item["name"]
            value = config.get(name, item.get("default"))
            if item["type"] == "checkbox":
                if isinstance(value, str):
                    value = value.strip().lower() in {"1", "true", "yes", "on"}
                normalized[name] = bool(value)
                continue
            if item["type"] == "number":
                if value in (None, ""):
                    if item["required"]:
                        raise ConnectorError(f"请填写 {item['label']}。")
                    normalized[name] = None
                    continue
                try:
                    value = int(value)
                except (TypeError, ValueError) as error:
                    raise ConnectorError(f"{item['label']} 必须是整数。") from error
                if name == "port" and not 1 <= value <= 65535:
                    raise ConnectorError("端口必须在 1–65535 之间。")
                normalized[name] = value
                continue
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ConnectorError(f"{item['label']} 必须是文本。")
            if "\x00" in value or len(value) > 4096:
                raise ConnectorError(f"{item['label']} 包含非法内容或过长。")
            if item["type"] != "textarea":
                value = value.strip()
            if item["type"] == "select" and value:
                allowed = {option["value"] for option in item.get("options", [])}
                if value not in allowed:
                    raise ConnectorError(f"{item['label']} 的取值无效。")
            if item["required"] and not value:
                raise ConnectorError(f"请填写 {item['label']}。")
            normalized[name] = value
        return normalized

    def summary(self) -> str:
        host = self.config.get("host")
        port = self.config.get("port")
        database = self.config.get("database") or ""
        if host:
            return f"{host}:{port}" + (f" / {database}" if database else "")
        return database

    def quote(self, name: str) -> str:
        quote = self.identifier_quote
        return quote + name.replace(quote, quote + quote) + quote

    def qualified(self, schema: str | None, name: str) -> str:
        if schema:
            return f"{self.quote(schema)}.{self.quote(name)}"
        return self.quote(name)

    # ----- engine surface (implemented by subclasses) --------------------
    def test(self) -> dict[str, Any]:
        raise NotImplementedError

    def schemas(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def tables(self, schema: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def table(self, schema: str, name: str) -> dict[str, Any]:
        raise NotImplementedError

    def view_definition(self, schema: str, name: str) -> str | None:
        """The query behind a view when the engine exposes it (the catalog derives lineage from it)."""
        return None

    def execute(self, sql: str, limit: int) -> tuple[list[str], list[list[Any]]]:
        """Run validated SQL and return up to ``limit`` raw rows."""
        raise NotImplementedError

    def default_schema(self) -> str | None:
        return self.config.get("database") or None

    # ----- shared behaviour -----------------------------------------------
    def query(self, sql: str, limit: Any = None) -> dict[str, Any]:
        limit = clamp_limit(limit)
        tree = guard_sql(sql, self.dialect)
        started = time.monotonic()
        columns, rows = self.execute(self.prepare(tree, sql, limit + 1), limit + 1)
        truncated = len(rows) > limit
        rows = [[json_value(value) for value in row] for row in rows[:limit]]
        return {
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
            "sql": strip_statement(sql),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }

    def prepare(self, tree: exp.Expression, sql: str, limit: int) -> str:
        return wrap_limit(sql, limit)

    def preview(self, schema: str | None, name: str, limit: Any = None) -> dict[str, Any]:
        check_identifier(name, "表名")
        if schema:
            check_identifier(schema, "模式名")
        return self.query(f"SELECT * FROM {self.qualified(schema, name)}", limit)

    def timed(self, action: Callable[[], Any], cancel: Callable[[], Any] | None = None):
        return run_with_timeout(action, QUERY_TIMEOUT + CONNECT_TIMEOUT, cancel)


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------
DUCKDB_CONFIG = {
    "enable_external_access": "false",
    "allow_unsigned_extensions": "false",
    "memory_limit": "256MB",
    "threads": "2",
}


class DuckDBConnector(Connector):
    type_id = "duckdb"
    label = "DuckDB"
    category = "本地文件"
    description = "本机 DuckDB 数据库文件；以只读方式打开，禁止访问外部文件。"
    driver = "duckdb " + duckdb.__version__
    dialect = "duckdb"
    fields = [
        field(
            "path",
            "数据库文件路径",
            required=True,
            placeholder="/绝对路径/analytics.duckdb 或 :memory:",
            help="必须是本机上的绝对路径；:memory: 表示临时内存库。",
        ),
        field("read_only", "只读打开", "checkbox", default=True),
    ]

    @classmethod
    def normalize(cls, config):
        normalized = super().normalize(config)
        path = normalized["path"]
        if path != ":memory:":
            candidate = Path(path)
            if not candidate.is_absolute():
                raise ConnectorError("数据库文件路径必须是绝对路径。")
            if candidate.exists() and not candidate.is_file():
                raise ConnectorError("数据库文件路径不是文件。")
            normalized["path"] = str(candidate)
        return normalized

    def summary(self):
        return self.config["path"]

    def connect(self):
        path = self.config["path"]
        if path == ":memory:":
            return duckdb.connect(":memory:", config=DUCKDB_CONFIG)
        if not Path(path).is_file():
            raise ConnectorError("数据库文件不存在：" + path)
        try:
            return duckdb.connect(
                path, read_only=bool(self.config["read_only"]), config=DUCKDB_CONFIG
            )
        except duckdb.Error as error:
            raise ConnectorError("无法打开 DuckDB 文件：" + str(error)[:300]) from error

    def _run(self, action):
        def call():
            with self.connect() as con:
                return action(con)

        try:
            return self.timed(call)
        except duckdb.Error as error:
            raise ConnectorError(str(error)[:600]) from error

    def test(self):
        started = time.monotonic()
        version, count = self._run(
            lambda con: (
                con.execute("SELECT version()").fetchone()[0],
                con.execute(
                    "SELECT count(*) FROM duckdb_tables() WHERE NOT internal"
                ).fetchone()[0],
            )
        )
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": "DuckDB " + str(version),
            "detail": "本地 DuckDB 文件可读。",
            "table_count": int(count),
        }

    def schemas(self):
        rows = self._run(
            lambda con: con.execute(
                "SELECT schema_name, count(table_name) FROM duckdb_tables() "
                "WHERE NOT internal AND database_name = current_database() "
                "GROUP BY schema_name ORDER BY schema_name"
            ).fetchall()
        )
        names = {row[0]: row[1] for row in rows}
        names.setdefault("main", 0)
        return [
            {"name": name, "table_count": count} for name, count in sorted(names.items())
        ]

    def tables(self, schema):
        check_identifier(schema, "模式名")
        rows = self._run(
            lambda con: con.execute(
                "SELECT table_name, 'table', estimated_size, comment FROM duckdb_tables() "
                "WHERE NOT internal AND schema_name = ? "
                "UNION ALL SELECT view_name, 'view', NULL, comment FROM duckdb_views() "
                "WHERE NOT internal AND schema_name = ? ORDER BY 1",
                [schema, schema],
            ).fetchall()
        )
        return [
            {
                "schema": schema,
                "name": row[0],
                "kind": row[1],
                "rows": row[2],
                "comment": row[3] or "",
            }
            for row in rows
        ]

    def table(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(con):
            columns = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [schema, name],
            ).fetchall()
            if not columns:
                raise ConnectorError(f"未找到表 {schema}.{name}。")
            count = con.execute(
                f"SELECT count(*) FROM {self.qualified(schema, name)}"
            ).fetchone()[0]
            return columns, count

        columns, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count),
            "columns": [
                {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
                for row in columns
            ],
        }

    def view_definition(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")
        row = self._run(
            lambda con: con.execute(
                "SELECT sql FROM duckdb_views() WHERE NOT internal AND schema_name = ? AND view_name = ?",
                [schema, name],
            ).fetchone()
        )
        return str(row[0]) if row and row[0] else None

    def default_schema(self):
        return "main"

    def execute(self, sql, limit):
        def call(con):
            result = con.execute(sql)
            return [c[0] for c in result.description], result.fetchmany(limit)

        return self._run(call)


# ---------------------------------------------------------------------------
# MySQL protocol family: MySQL, StarRocks, Doris
# ---------------------------------------------------------------------------
def mysql_fields(default_port: int, *, catalog: bool = False) -> list[dict[str, Any]]:
    items = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=default_port),
        field("user", "用户名", required=True),
        field("password", "密码", "password"),
        field("database", "默认数据库", placeholder="可选"),
    ]
    if catalog:
        items.append(
            field("catalog", "Catalog", placeholder="default_catalog", help="外部 Catalog 名称，可选。")
        )
    items.append(field("ssl", "使用 TLS 连接", "checkbox", default=False))
    return items


class MySQLFamilyConnector(Connector):
    identifier_quote = "`"
    system_schemas = {"information_schema", "performance_schema", "mysql", "sys"}
    version_sql = "SELECT VERSION()"

    def connect(self):
        import pymysql

        kwargs: dict[str, Any] = {
            "host": self.config["host"],
            "port": self.config["port"],
            "user": self.config["user"],
            "password": self.config.get("password") or "",
            "database": self.config.get("database") or None,
            "charset": "utf8mb4",
            "connect_timeout": CONNECT_TIMEOUT,
            "read_timeout": QUERY_TIMEOUT + 5,
            "write_timeout": 10,
            "autocommit": True,
        }
        if self.config.get("ssl"):
            context = ssl_module.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl_module.CERT_NONE
            kwargs["ssl"] = context
        try:
            connection = pymysql.connect(**kwargs)
        except pymysql.MySQLError as error:
            raise ConnectorError(self.describe_error(error)) from error
        self.after_connect(connection)
        return connection

    def after_connect(self, connection):
        return None

    @staticmethod
    def describe_error(error) -> str:
        args = getattr(error, "args", ())
        if len(args) >= 2 and isinstance(args[0], int):
            return f"[{args[0]}] {args[1]}"[:600]
        return str(error)[:600]

    def _run(self, action):
        import pymysql

        state: dict[str, Any] = {}

        def call():
            connection = self.connect()
            state["connection"] = connection
            try:
                with connection.cursor() as cursor:
                    return action(cursor)
            finally:
                connection.close()

        def cancel():
            connection = state.get("connection")
            if connection is not None:
                connection.close()

        try:
            return self.timed(call, cancel)
        except pymysql.MySQLError as error:
            raise ConnectorError(self.describe_error(error)) from error

    def server_version(self, cursor) -> str:
        cursor.execute(self.version_sql)
        return str(cursor.fetchone()[0])

    def test(self):
        started = time.monotonic()

        def probe(cursor):
            version = self.server_version(cursor)
            cursor.execute("SHOW DATABASES")
            databases = [row[0] for row in cursor.fetchall()]
            return version, databases

        version, databases = self._run(probe)
        user_databases = [
            name for name in databases if name.lower() not in self.system_schemas
        ]
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": version,
            "detail": f"{self.label} 连接成功，可见 {len(user_databases)} 个数据库。",
        }

    def schemas(self):
        def load(cursor):
            cursor.execute("SHOW DATABASES")
            return [row[0] for row in cursor.fetchall()]

        names = self._run(load)
        return [
            {"name": name, "table_count": None}
            for name in names
            if name.lower() not in self.system_schemas
        ]

    def tables(self, schema):
        check_identifier(schema, "数据库名")

        def load(cursor):
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (schema,),
            )
            return cursor.fetchall()

        rows = self._run(load)
        return [
            {
                "schema": schema,
                "name": row[0],
                "kind": "view" if "VIEW" in str(row[1]).upper() else "table",
                "rows": int(row[2]) if row[2] is not None else None,
                "comment": row[3] or "",
            }
            for row in rows
        ]

    def table(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(f"SHOW FULL COLUMNS FROM {self.qualified(schema, name)}")
            columns = cursor.fetchall()
            cursor.execute(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (schema, name),
            )
            found = cursor.fetchone()
            return columns, found[0] if found else None

        columns, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count) if count is not None else None,
            "columns": [
                {
                    "name": row[0],
                    "type": row[1],
                    "nullable": str(row[3]).upper() == "YES",
                    "primary_key": str(row[4]).upper() == "PRI",
                    "comment": row[8] if len(row) > 8 and row[8] else "",
                }
                for row in columns
            ],
        }

    def view_definition(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (schema, name),
            )
            return cursor.fetchone()

        row = self._run(load)
        return str(row[0]) if row and row[0] else None

    def timeout_statements(self) -> list[str]:
        return [f"SET SESSION max_execution_time = {QUERY_TIMEOUT * 1000}"]

    def execute(self, sql, limit):
        def call(cursor):
            for statement in self.timeout_statements():
                try:
                    cursor.execute(statement)
                except Exception:  # noqa: BLE001 - optional server feature
                    pass
            cursor.execute(sql)
            columns = [c[0] for c in cursor.description or []]
            return columns, [list(row) for row in cursor.fetchmany(limit)]

        return self._run(call)


class MySQLConnector(MySQLFamilyConnector):
    type_id = "mysql"
    label = "MySQL"
    category = "关系数据库"
    description = "MySQL 5.7 / 8.x / 9.x，也兼容 MariaDB 等 MySQL 协议服务。"
    driver = "PyMySQL"
    dialect = "mysql"
    default_port = 3306
    fields = mysql_fields(3306)


class StarRocksConnector(MySQLFamilyConnector):
    type_id = "starrocks"
    label = "StarRocks"
    category = "OLAP 引擎"
    description = "通过 FE 的 MySQL 协议端口（默认 9030）连接 StarRocks。"
    driver = "PyMySQL（MySQL 协议）"
    dialect = "starrocks"
    default_port = 9030
    fields = mysql_fields(9030, catalog=True)
    system_schemas = {"information_schema", "_statistics_", "sys"}

    def after_connect(self, connection):
        catalog = self.config.get("catalog")
        if catalog and catalog != "default_catalog":
            with connection.cursor() as cursor:
                cursor.execute(f"SET CATALOG {self.quote(catalog)}")

    def server_version(self, cursor):
        cursor.execute("SELECT VERSION()")
        version = str(cursor.fetchone()[0])
        try:
            cursor.execute("SELECT current_version()")
            version = "StarRocks " + str(cursor.fetchone()[0])
        except Exception:  # noqa: BLE001 - older releases
            pass
        return version

    def timeout_statements(self):
        return [f"SET query_timeout = {QUERY_TIMEOUT}"]


class DorisConnector(MySQLFamilyConnector):
    type_id = "doris"
    label = "Apache Doris"
    category = "OLAP 引擎"
    description = "通过 FE 的 MySQL 协议端口（默认 9030）连接 Apache Doris。"
    driver = "PyMySQL（MySQL 协议）"
    dialect = "doris"
    default_port = 9030
    fields = mysql_fields(9030)
    system_schemas = {"information_schema", "__internal_schema", "mysql"}

    def server_version(self, cursor):
        cursor.execute("SELECT @@version_comment")
        comment = str(cursor.fetchone()[0])
        cursor.execute("SELECT VERSION()")
        version = str(cursor.fetchone()[0])
        return comment if "doris" in comment.lower() else f"{comment} ({version})"

    def timeout_statements(self):
        return [f"SET query_timeout = {QUERY_TIMEOUT}"]


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
class PostgresConnector(Connector):
    type_id = "postgresql"
    label = "PostgreSQL"
    category = "关系数据库"
    description = "PostgreSQL 12+ 及兼容协议的数据库。"
    driver = "psycopg 3"
    dialect = "postgres"
    default_port = 5432
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=5432),
        field("user", "用户名", required=True),
        field("password", "密码", "password"),
        field("database", "数据库", required=True),
        field("schema", "默认模式", default="public"),
        field(
            "sslmode",
            "SSL 模式",
            "select",
            default="prefer",
            options=[("disable", "disable"), ("prefer", "prefer"), ("require", "require")],
        ),
    ]

    def connect(self):
        import psycopg

        try:
            return psycopg.connect(
                host=self.config["host"],
                port=self.config["port"],
                user=self.config["user"],
                password=self.config.get("password") or "",
                dbname=self.config["database"],
                sslmode=self.config.get("sslmode") or "prefer",
                connect_timeout=CONNECT_TIMEOUT,
                options=f"-c statement_timeout={QUERY_TIMEOUT * 1000}",
                autocommit=True,
            )
        except psycopg.Error as error:
            raise ConnectorError(str(error).strip()[:600]) from error

    def _run(self, action):
        import psycopg

        state: dict[str, Any] = {}

        def call():
            connection = self.connect()
            state["connection"] = connection
            try:
                with connection.cursor() as cursor:
                    return action(cursor)
            finally:
                connection.close()

        def cancel():
            connection = state.get("connection")
            if connection is not None:
                connection.cancel()
                connection.close()

        try:
            return self.timed(call, cancel)
        except psycopg.Error as error:
            raise ConnectorError(str(error).strip()[:600]) from error

    def default_schema(self):
        return self.config.get("schema") or "public"

    def test(self):
        started = time.monotonic()

        def probe(cursor):
            cursor.execute("SELECT version()")
            version = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (self.default_schema(),),
            )
            return version, cursor.fetchone()[0]

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": str(version).split(" on ")[0],
            "detail": f"连接成功，模式 {self.default_schema()} 中有 {count} 张表。",
            "table_count": int(count),
        }

    def schemas(self):
        def load(cursor):
            cursor.execute(
                "SELECT n.nspname, count(c.oid) FROM pg_namespace n "
                "LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','v','m','p','f') "
                "WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' "
                "GROUP BY n.nspname ORDER BY n.nspname"
            )
            return cursor.fetchall()

        return [
            {"name": row[0], "table_count": int(row[1])} for row in self._run(load)
        ]

    def tables(self, schema):
        check_identifier(schema, "模式名")

        def load(cursor):
            cursor.execute(
                "SELECT c.relname, c.relkind, c.reltuples, obj_description(c.oid, 'pg_class') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f') ORDER BY c.relname",
                (schema,),
            )
            return cursor.fetchall()

        kinds = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}
        return [
            {
                "schema": schema,
                "name": row[0],
                "kind": kinds.get(row[1], "table"),
                "rows": int(row[2]) if row[2] is not None and row[2] >= 0 else None,
                "comment": row[3] or "",
            }
            for row in self._run(load)
        ]

    def table(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, name),
            )
            columns = cursor.fetchall()
            if not columns:
                raise ConnectorError(f"未找到表 {schema}.{name}。")
            cursor.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                (self.qualified(schema, name),),
            )
            keys = {row[0] for row in cursor.fetchall()}
            cursor.execute(f"SELECT count(*) FROM {self.qualified(schema, name)}")
            return columns, keys, cursor.fetchone()[0]

        columns, keys, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count),
            "columns": [
                {
                    "name": row[0],
                    "type": row[1],
                    "nullable": row[2] == "YES",
                    "primary_key": row[0] in keys,
                }
                for row in columns
            ],
        }

    def view_definition(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT pg_get_viewdef(c.oid, true) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('v', 'm')",
                (schema, name),
            )
            return cursor.fetchone()

        row = self._run(load)
        return str(row[0]) if row and row[0] else None

    def execute(self, sql, limit):
        def call(cursor):
            cursor.execute(sql)
            columns = [c.name for c in cursor.description or []]
            return columns, [list(row) for row in cursor.fetchmany(limit)]

        return self._run(call)


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------
class ClickHouseConnector(Connector):
    type_id = "clickhouse"
    label = "ClickHouse"
    category = "OLAP 引擎"
    description = "通过 HTTP 接口（默认 8123）连接 ClickHouse。"
    driver = "clickhouse-connect"
    dialect = "clickhouse"
    default_port = 8123
    identifier_quote = "`"
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "HTTP 端口", "number", required=True, default=8123),
        field("user", "用户名", required=True, default="default"),
        field("password", "密码", "password"),
        field("database", "默认数据库", default="default"),
        field("secure", "使用 HTTPS", "checkbox", default=False),
    ]
    system_schemas = {"system", "information_schema"}

    def client(self):
        import clickhouse_connect
        from clickhouse_connect.driver.exceptions import ClickHouseError

        try:
            return clickhouse_connect.get_client(
                host=self.config["host"],
                port=self.config["port"],
                username=self.config["user"],
                password=self.config.get("password") or "",
                database=self.config.get("database") or "default",
                secure=bool(self.config.get("secure")),
                connect_timeout=CONNECT_TIMEOUT,
                send_receive_timeout=QUERY_TIMEOUT + 5,
                settings={"max_execution_time": QUERY_TIMEOUT},
            )
        except ClickHouseError as error:
            raise ConnectorError(str(error)[:600]) from error
        except OSError as error:
            raise ConnectorError("无法连接 ClickHouse：" + str(error)[:300]) from error

    def _run(self, action):
        from clickhouse_connect.driver.exceptions import ClickHouseError

        def call():
            client = self.client()
            try:
                return action(client)
            finally:
                client.close()

        try:
            return self.timed(call)
        except ClickHouseError as error:
            raise ConnectorError(str(error)[:600]) from error

    def test(self):
        started = time.monotonic()

        def probe(client):
            version = client.query("SELECT version()").result_rows[0][0]
            count = client.query(
                "SELECT count() FROM system.tables WHERE database = {db:String}",
                parameters={"db": self.config.get("database") or "default"},
            ).result_rows[0][0]
            return version, count

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": "ClickHouse " + str(version),
            "detail": f"连接成功，数据库 {self.config.get('database') or 'default'} 中有 {count} 张表。",
            "table_count": int(count),
        }

    def schemas(self):
        rows = self._run(
            lambda client: client.query(
                "SELECT d.name, count(t.name) FROM system.databases d "
                "LEFT JOIN system.tables t ON t.database = d.name "
                "GROUP BY d.name ORDER BY d.name"
            ).result_rows
        )
        return [
            {"name": row[0], "table_count": int(row[1])}
            for row in rows
            if row[0].lower() not in self.system_schemas
        ]

    def tables(self, schema):
        check_identifier(schema, "数据库名")
        rows = self._run(
            lambda client: client.query(
                "SELECT name, engine, total_rows, comment FROM system.tables "
                "WHERE database = {db:String} ORDER BY name",
                parameters={"db": schema},
            ).result_rows
        )
        return [
            {
                "schema": schema,
                "name": row[0],
                "kind": "view" if "View" in str(row[1]) else "table",
                "rows": int(row[2]) if row[2] is not None else None,
                "comment": row[3] or "",
            }
            for row in rows
        ]

    def view_definition(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")
        rows = self._run(
            lambda client: client.query(
                "SELECT as_select FROM system.tables WHERE database = {db:String} AND name = {name:String}",
                parameters={"db": schema, "name": name},
            ).result_rows
        )
        return str(rows[0][0]) if rows and rows[0][0] else None

    def table(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")

        def load(client):
            columns = client.query(
                "SELECT name, type, comment, is_in_primary_key FROM system.columns "
                "WHERE database = {db:String} AND table = {t:String} ORDER BY position",
                parameters={"db": schema, "t": name},
            ).result_rows
            if not columns:
                raise ConnectorError(f"未找到表 {schema}.{name}。")
            count = client.query(
                f"SELECT count() FROM {self.qualified(schema, name)}"
            ).result_rows[0][0]
            return columns, count

        columns, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count),
            "columns": [
                {
                    "name": row[0],
                    "type": row[1],
                    "nullable": str(row[1]).startswith("Nullable("),
                    "comment": row[2] or "",
                    "primary_key": bool(row[3]),
                }
                for row in columns
            ],
        }

    def execute(self, sql, limit):
        def call(client):
            result = client.query(sql)
            return list(result.column_names), [
                list(row) for row in result.result_rows[:limit]
            ]

        return self._run(call)


# ---------------------------------------------------------------------------
# Apache Hive (HiveServer2)
# ---------------------------------------------------------------------------
class HiveConnector(Connector):
    type_id = "hive"
    label = "Apache Hive"
    category = "数据湖 / 数仓"
    description = "通过 HiveServer2 Thrift 接口（默认 10000）连接 Hive。"
    driver = "PyHive（Thrift + pure-sasl）"
    dialect = "hive"
    default_port = 10000
    identifier_quote = "`"
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=10000),
        field("user", "用户名", default="hive"),
        field("password", "密码", "password"),
        field("database", "默认数据库", default="default"),
        field(
            "auth",
            "认证方式",
            "select",
            default="NONE",
            options=[("NONE", "NONE（默认）"), ("NOSASL", "NOSASL"), ("LDAP", "LDAP"), ("CUSTOM", "CUSTOM")],
            help="与 hive.server2.authentication 一致；Kerberos 需要单独配置，当前未支持。",
        ),
    ]

    def connect(self):
        from pyhive import hive
        from pyhive.exc import Error as HiveError
        from thrift.transport.TTransport import TTransportException

        auth = self.config.get("auth") or "NONE"
        kwargs: dict[str, Any] = {
            "host": self.config["host"],
            "port": self.config["port"],
            "username": self.config.get("user") or "hive",
            "database": self.config.get("database") or "default",
            "auth": auth,
        }
        if auth in {"LDAP", "CUSTOM"}:
            kwargs["password"] = self.config.get("password") or ""
        try:
            return hive.connect(**kwargs)
        except (HiveError, TTransportException, OSError, EOFError) as error:
            raise ConnectorError("无法连接 HiveServer2：" + str(error)[:400]) from error

    def _run(self, action):
        from pyhive.exc import Error as HiveError
        from thrift.transport.TTransport import TTransportException

        state: dict[str, Any] = {}

        def call():
            connection = self.connect()
            state["connection"] = connection
            cursor = connection.cursor()
            state["cursor"] = cursor
            try:
                return action(cursor)
            finally:
                cursor.close()
                connection.close()

        def cancel():
            cursor = state.get("cursor")
            if cursor is not None:
                cursor.cancel()

        try:
            return self.timed(call, cancel)
        except (HiveError, TTransportException, EOFError) as error:
            raise ConnectorError(str(error)[:600]) from error

    def test(self):
        started = time.monotonic()

        def probe(cursor):
            cursor.execute("SELECT version()")
            version = cursor.fetchone()[0]
            cursor.execute("SHOW DATABASES")
            return version, len(cursor.fetchall())

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": "Hive " + str(version).split(" ")[0],
            "detail": f"HiveServer2 连接成功，可见 {count} 个数据库。",
        }

    def schemas(self):
        def load(cursor):
            cursor.execute("SHOW DATABASES")
            return [row[0] for row in cursor.fetchall()]

        return [{"name": name, "table_count": None} for name in self._run(load)]

    def tables(self, schema):
        check_identifier(schema, "数据库名")

        def load(cursor):
            cursor.execute(f"SHOW TABLES IN {self.quote(schema)}")
            return [row[0] for row in cursor.fetchall()]

        return [
            {"schema": schema, "name": name, "kind": "table", "rows": None, "comment": ""}
            for name in self._run(load)
        ]

    def table(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(f"DESCRIBE {self.qualified(schema, name)}")
            return cursor.fetchall()

        rows = self._run(load)
        columns: list[dict[str, Any]] = []
        partition = False
        for row in rows:
            column = str(row[0] or "").strip()
            if not column:
                continue
            if column.startswith("#"):
                partition = "Partition" in column
                continue
            if partition and any(item["name"] == column for item in columns):
                continue
            columns.append(
                {
                    "name": column,
                    "type": str(row[1] or "").strip(),
                    "nullable": True,
                    "comment": str(row[2] or "").strip() if len(row) > 2 else "",
                }
            )
        if not columns:
            raise ConnectorError(f"未找到表 {schema}.{name}。")
        return {"schema": schema, "name": name, "kind": "table", "rows": None, "columns": columns}

    def execute(self, sql, limit):
        def call(cursor):
            cursor.execute(sql)
            columns = [c[0].split(".")[-1] for c in cursor.description or []]
            return columns, [list(row) for row in cursor.fetchmany(limit)]

        return self._run(call)


# ---------------------------------------------------------------------------
# Lake formats executed through an in-memory DuckDB over Arrow
# ---------------------------------------------------------------------------
class ArrowLakeConnector(Connector):
    """Load referenced tables into DuckDB views and run the SQL there."""

    dialect = "duckdb"
    supports_schemas = True

    def load_arrow(self, schema: str, name: str, limit: int | None):
        raise NotImplementedError

    def resolve_schema(self, schema: str | None) -> str:
        resolved = schema or self.default_schema()
        if not resolved:
            raise ConnectorError("请使用 库.表 的形式指定表，或在数据源中设置默认库。")
        return resolved

    def prepare(self, tree, sql, limit):
        self._references = referenced_tables(tree)
        return wrap_limit(sql, limit)

    def execute(self, sql, limit):
        references = getattr(self, "_references", [])
        if not references:
            raise ConnectorError("查询没有引用任何数据湖表。")

        def call():
            con = duckdb.connect(":memory:", config=DUCKDB_CONFIG)
            try:
                for index, (schema, name) in enumerate(references):
                    resolved = self.resolve_schema(schema)
                    arrow = self.load_arrow(resolved, name, LAKE_SCAN_ROWS)
                    handle = f"__lattice_arrow_{index}"
                    con.register(handle, arrow)
                    con.execute(f"CREATE SCHEMA IF NOT EXISTS {self.quote(resolved)}")
                    con.execute(
                        f"CREATE OR REPLACE VIEW {self.qualified(resolved, name)} "
                        f"AS SELECT * FROM {self.quote(handle)}"
                    )
                    if schema is None:
                        con.execute(
                            f"CREATE OR REPLACE VIEW {self.quote(name)} AS SELECT * FROM {self.quote(handle)}"
                        )
                result = con.execute(sql)
                return [c[0] for c in result.description], result.fetchmany(limit)
            finally:
                con.close()

        try:
            return self.timed(call)
        except duckdb.Error as error:
            raise ConnectorError(str(error)[:600]) from error


def arrow_columns(arrow_schema) -> list[dict[str, Any]]:
    return [
        {"name": item.name, "type": str(item.type), "nullable": item.nullable}
        for item in arrow_schema
    ]


class IcebergConnector(ArrowLakeConnector):
    type_id = "iceberg"
    label = "Apache Iceberg"
    category = "数据湖表格式"
    description = "通过 Iceberg REST Catalog（如 Apache Polaris）或 Hive Metastore 读取 Iceberg 表。"
    driver = "pyiceberg + DuckDB"
    fields = [
        field(
            "catalog_type",
            "Catalog 类型",
            "select",
            default="rest",
            options=[("rest", "REST Catalog"), ("hive", "Hive Metastore")],
        ),
        field("uri", "Catalog 地址", required=True, placeholder="http://127.0.0.1:8181/api/catalog"),
        field("warehouse", "Warehouse / Catalog 名称", placeholder="lattice"),
        field("credential", "OAuth 凭据（client_id:client_secret）", "password"),
        field("token", "Bearer Token", "password"),
        field("scope", "OAuth Scope", default="PRINCIPAL_ROLE:ALL"),
        field(
            "extra_headers",
            "附加请求头",
            "textarea",
            placeholder="Polaris-Realm: LATTICE\nX-Iceberg-Access-Delegation: vended-credentials",
            help="每行一个 `名称: 值`。",
        ),
        field("s3_endpoint", "S3 Endpoint", placeholder="http://127.0.0.1:19000"),
        field("s3_access_key", "S3 Access Key"),
        field("s3_secret_key", "S3 Secret Key", "password"),
        field("s3_region", "S3 Region", default="us-east-1"),
        field("namespace", "默认命名空间", placeholder="demo"),
    ]

    def summary(self):
        return f"{self.config['uri']} / {self.config.get('warehouse') or ''}".rstrip(" /")

    def default_schema(self):
        return self.config.get("namespace") or None

    def properties(self) -> dict[str, str]:
        config = self.config
        properties: dict[str, str] = {"type": config.get("catalog_type") or "rest", "uri": config["uri"]}
        if config.get("warehouse"):
            properties["warehouse"] = config["warehouse"]
        if config.get("credential"):
            properties["credential"] = config["credential"]
        if config.get("token"):
            properties["token"] = config["token"]
        if config.get("scope") and config.get("credential"):
            properties["scope"] = config["scope"]
        for key, value in parse_header_lines(config.get("extra_headers") or "").items():
            properties["header." + key] = value
        if config.get("s3_endpoint"):
            properties["s3.endpoint"] = config["s3_endpoint"]
            properties["s3.path-style-access"] = "true"
        if config.get("s3_access_key"):
            properties["s3.access-key-id"] = config["s3_access_key"]
        if config.get("s3_secret_key"):
            properties["s3.secret-access-key"] = config["s3_secret_key"]
        if config.get("s3_region"):
            properties["s3.region"] = config["s3_region"]
        return properties

    def catalog(self):
        from pyiceberg.catalog import load_catalog

        try:
            return load_catalog("lattice", **self.properties())
        except Exception as error:  # noqa: BLE001 - pyiceberg raises many types
            raise ConnectorError("无法连接 Iceberg Catalog：" + str(error)[:400]) from error

    def _run(self, action):
        def call():
            return action(self.catalog())

        try:
            return self.timed(call)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError("Iceberg 操作失败：" + str(error)[:500]) from error

    @staticmethod
    def split_namespace(schema: str) -> tuple[str, ...]:
        return tuple(part for part in schema.split(".") if part)

    def test(self):
        started = time.monotonic()
        namespaces = self._run(lambda catalog: catalog.list_namespaces())
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": f"Iceberg {self.config.get('catalog_type') or 'rest'} catalog",
            "detail": f"Catalog 连接成功，可见 {len(namespaces)} 个命名空间。",
        }

    def schemas(self):
        namespaces = self._run(lambda catalog: catalog.list_namespaces())
        return [{"name": ".".join(item), "table_count": None} for item in namespaces]

    def tables(self, schema):
        check_identifier(schema, "命名空间")
        namespace = self.split_namespace(schema)
        identifiers = self._run(lambda catalog: catalog.list_tables(namespace))
        return [
            {"schema": schema, "name": item[-1], "kind": "iceberg", "rows": None, "comment": ""}
            for item in identifiers
        ]

    def table(self, schema, name):
        check_identifier(schema, "命名空间")
        check_identifier(name, "表名")
        namespace = self.split_namespace(schema)

        def load(catalog):
            table = catalog.load_table((*namespace, name))
            snapshot = table.current_snapshot()
            # pyiceberg's Summary is a Mapping whose __iter__ yields (key, value)
            # pairs, so dict() feeds tuples back into __getitem__ and raises.
            # Reading the key directly is the supported access path.
            summary = snapshot.summary if snapshot else None
            rows = summary.get("total-records") if summary else None
            return (
                [
                    {
                        "name": item.name,
                        "type": str(item.field_type),
                        "nullable": not item.required,
                        "comment": item.doc or "",
                    }
                    for item in table.schema().fields
                ],
                int(rows) if rows is not None else None,
                {"location": table.location(), "format-version": str(table.format_version)},
            )

        columns, rows, properties = self._run(load)
        return {"schema": schema, "name": name, "kind": "iceberg", "rows": rows, "columns": columns, "properties": properties}

    def load_arrow(self, schema, name, limit):
        namespace = self.split_namespace(schema)
        catalog = self.catalog()
        try:
            table = catalog.load_table((*namespace, name))
            return table.scan(limit=limit).to_arrow()
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(f"读取 Iceberg 表 {schema}.{name} 失败：" + str(error)[:400]) from error


class PaimonConnector(ArrowLakeConnector):
    type_id = "paimon"
    label = "Apache Paimon"
    category = "数据湖表格式"
    description = "通过 Paimon 官方 Python SDK 读取文件系统 Catalog 或 REST Catalog 中的表。"
    driver = "pypaimon + DuckDB"
    fields = [
        field(
            "catalog_type",
            "Catalog 类型",
            "select",
            default="filesystem",
            options=[("filesystem", "文件系统 Catalog"), ("rest", "REST Catalog")],
        ),
        field("warehouse", "Warehouse 路径", required=True, placeholder="file:///data/paimon 或 s3://bucket/warehouse"),
        field("uri", "REST Catalog 地址", placeholder="仅 REST Catalog 需要"),
        field("token", "REST Token", "password"),
        field("s3_endpoint", "S3 Endpoint", placeholder="http://127.0.0.1:19000"),
        field("s3_access_key", "S3 Access Key"),
        field("s3_secret_key", "S3 Secret Key", "password"),
        field("s3_region", "S3 Region"),
        field("database", "默认数据库", placeholder="default"),
    ]

    @classmethod
    def normalize(cls, config):
        normalized = super().normalize(config)
        warehouse = normalized["warehouse"]
        if warehouse.startswith("/"):
            normalized["warehouse"] = "file://" + warehouse
        elif not re.match(r"^[a-z][a-z0-9+.-]*://", warehouse):
            raise ConnectorError("Warehouse 路径必须是绝对路径或带协议的 URI（file://、s3://）。")
        if normalized.get("catalog_type") == "rest" and not normalized.get("uri"):
            raise ConnectorError("REST Catalog 需要填写地址。")
        return normalized

    def summary(self):
        return self.config["warehouse"]

    def options(self) -> dict[str, str]:
        config = self.config
        options: dict[str, str] = {"warehouse": config["warehouse"]}
        if config.get("catalog_type") == "rest":
            options["metastore"] = "rest"
            options["uri"] = config["uri"]
            if config.get("token"):
                options["token"] = config["token"]
        if config.get("s3_endpoint"):
            options["fs.s3.endpoint"] = config["s3_endpoint"]
        if config.get("s3_access_key"):
            options["fs.s3.accessKeyId"] = config["s3_access_key"]
        if config.get("s3_secret_key"):
            options["fs.s3.accessKeySecret"] = config["s3_secret_key"]
        if config.get("s3_region"):
            options["fs.s3.region"] = config["s3_region"]
        return options

    def catalog(self):
        from pypaimon import CatalogFactory

        try:
            return CatalogFactory.create(self.options())
        except Exception as error:  # noqa: BLE001
            raise ConnectorError("无法打开 Paimon Catalog：" + str(error)[:400]) from error

    def _run(self, action):
        def call():
            return action(self.catalog())

        try:
            return self.timed(call)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError("Paimon 操作失败：" + str(error)[:500]) from error

    def test(self):
        started = time.monotonic()
        databases = self._run(lambda catalog: catalog.list_databases())
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": "Paimon " + (self.config.get("catalog_type") or "filesystem") + " catalog",
            "detail": f"Catalog 可访问，可见 {len(databases)} 个数据库。",
        }

    def schemas(self):
        databases = self._run(lambda catalog: catalog.list_databases())
        return [{"name": name, "table_count": None} for name in databases]

    def tables(self, schema):
        check_identifier(schema, "数据库名")
        names = self._run(lambda catalog: catalog.list_tables(schema))
        return [
            {"schema": schema, "name": name, "kind": "paimon", "rows": None, "comment": ""}
            for name in names
        ]

    def table(self, schema, name):
        check_identifier(schema, "数据库名")
        check_identifier(name, "表名")

        def load(catalog):
            table = catalog.get_table(f"{schema}.{name}")
            keys = set(getattr(table, "primary_keys", None) or [])
            rows = None
            try:
                snapshot = table.snapshot_manager().get_latest_snapshot()
                rows = getattr(snapshot, "total_record_count", None) if snapshot else None
            except Exception:  # noqa: BLE001 - snapshot metadata is optional
                rows = None
            return (
                [
                    {
                        "name": item.name,
                        "type": str(item.type),
                        "nullable": True,
                        "primary_key": item.name in keys,
                        "comment": getattr(item, "description", None) or "",
                    }
                    for item in table.fields
                ],
                int(rows) if rows is not None else None,
                {"partition_keys": ",".join(getattr(table, "partition_keys", None) or [])},
            )

        columns, rows, properties = self._run(load)
        return {"schema": schema, "name": name, "kind": "paimon", "rows": rows, "columns": columns, "properties": properties}

    def load_arrow(self, schema, name, limit):
        catalog = self.catalog()
        try:
            table = catalog.get_table(f"{schema}.{name}")
            builder = table.new_read_builder()
            if limit:
                builder = builder.with_limit(limit)
            plan = builder.new_scan().plan()
            return builder.new_read().to_arrow(plan.splits())
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(f"读取 Paimon 表 {schema}.{name} 失败：" + str(error)[:400]) from error


# ---------------------------------------------------------------------------
# Optional drivers and document snapshots (Oracle, SQL Server, MongoDB,
# Elasticsearch, Kafka)
# ---------------------------------------------------------------------------
#: Import name -> pip distribution, for the message shown when a driver is absent.
OPTIONAL_DRIVERS = {"oracledb": "oracledb", "pymssql": "pymssql", "pymongo": "pymongo", "kafka": "kafka-python"}
#: Snapshot connectors never pull more than this many documents or messages per table.
SNAPSHOT_MAX_ROWS = 200_000
SNAPSHOT_DEFAULT_ROWS = 10_000
#: Elasticsearch refuses ``from + size`` beyond ``index.max_result_window`` (10 000 by default).
ELASTIC_PAGE = 10_000
SYSTEM_DATABASES_MONGO = {"admin", "local", "config"}


def optional_driver(module: str):
    """Import a driver lazily; a missing one is a configuration problem, not a crash."""
    try:
        return importlib.import_module(module)
    except ImportError as error:
        package = OPTIONAL_DRIVERS.get(module, module)
        raise ConnectorError(
            f"缺少驱动 {package}：请在运行环境中安装后重试（例如 uv pip install {package}）。"
        ) from error


def flatten_document(document: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """One level of dotted keys for nested objects; deeper values become JSON text."""
    flat: dict[str, Any] = {}
    if not isinstance(document, dict):
        return {"value": scalar_value(document)}
    for key, value in document.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 1:
            flat.update(flatten_document(value, f"{name}.", depth + 1))
        else:
            flat[name] = scalar_value(value)
    return flat


def scalar_value(value: Any) -> Any:
    """A JSON-safe scalar for one cell of a snapshot; containers become JSON text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def rows_to_arrow(rows: list[dict[str, Any]], columns: list[str] | None = None):
    """An Arrow table whose column types survive mixed documents.

    Arrow infers one type per column from the first values it sees and fails on
    the first document that disagrees; a column that is not consistently
    numeric, boolean or temporal is therefore stored as text.
    """
    import pyarrow as pa

    names: list[str] = list(columns or [])
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    if not names:
        names = ["value"]
    arrays = {}
    for name in names:
        values = [row.get(name) for row in rows]
        present = [value for value in values if value is not None]
        if all(isinstance(value, bool) for value in present):
            arrays[name] = pa.array(values, type=pa.bool_())
        elif all(isinstance(value, int) and not isinstance(value, bool) for value in present):
            arrays[name] = pa.array(values, type=pa.int64())
        elif all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
            arrays[name] = pa.array([float(value) if value is not None else None for value in values], type=pa.float64())
        elif all(isinstance(value, dt.datetime) for value in present):
            arrays[name] = pa.array(
                [None if value is None else (value.replace(tzinfo=None) if value.tzinfo else value) for value in values],
                type=pa.timestamp("us"),
            )
        else:
            arrays[name] = pa.array([None if value is None else (value if isinstance(value, str) else str(value)) for value in values], type=pa.string())
    return pa.table(arrays)


def infer_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Column descriptors from sampled documents: name, union of types, nullable."""
    seen: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    nulls: set[str] = set()
    for row in rows:
        for key, value in row.items():
            counts[key] = counts.get(key, 0) + 1
            if value is None:
                nulls.add(key)
                continue
            kind = {bool: "boolean", int: "integer", float: "double", str: "string"}.get(type(value))
            if kind is None:
                kind = "timestamp" if isinstance(value, (dt.datetime, dt.date)) else "string"
            seen.setdefault(key, set()).add(kind)
    return [
        {
            "name": key,
            "type": " | ".join(sorted(seen.get(key, {"null"}))),
            "nullable": counts.get(key, 0) < len(rows) or key in nulls,
        }
        for key in counts
    ]


def sample_rows_field(default: int = SNAPSHOT_DEFAULT_ROWS) -> dict[str, Any]:
    return field(
        "sample_rows",
        "快照行数",
        "number",
        default=default,
        help=f"每张表最多读取多少条记录用于查询与核查（上限 {SNAPSHOT_MAX_ROWS}）。",
    )


class SnapshotConnector(ArrowLakeConnector):
    """A source without SQL: rows are pulled into DuckDB and queried there."""

    dialect = "duckdb"
    schema_name = "default"
    schema_label = ""

    def sample_limit(self, limit: int | None = None) -> int:
        configured = self.config.get("sample_rows") or SNAPSHOT_DEFAULT_ROWS
        bounded = max(1, min(int(configured), SNAPSHOT_MAX_ROWS))
        return min(bounded, int(limit)) if limit else bounded

    def default_schema(self):
        return self.schema_name

    def load_rows(self, schema: str, name: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def load_arrow(self, schema, name, limit):
        if schema != self.schema_name:
            raise ConnectorError(f"该数据源只有一个库 {self.schema_name}，没有 {schema}。")
        return rows_to_arrow(self.load_rows(schema, name, self.sample_limit(limit)))


# ----- Oracle ----------------------------------------------------------------------
class OracleConnector(Connector):
    type_id = "oracle"
    label = "Oracle"
    category = "关系数据库"
    description = "Oracle Database 12c+（python-oracledb thin 模式，无需安装客户端库）。"
    driver = "python-oracledb"
    dialect = "oracle"
    default_port = 1521
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=1521),
        field("user", "用户名", required=True),
        field("password", "密码", "password"),
        field("service_name", "服务名", required=True, placeholder="ORCLPDB1", help="连接串使用 主机:端口/服务名。"),
        field("schema", "默认模式", help="留空时使用登录用户名（大写）。"),
    ]

    def summary(self):
        return f"{self.config['host']}:{self.config['port']} / {self.config['service_name']}"

    def connect(self):
        oracledb = optional_driver("oracledb")
        try:
            return oracledb.connect(
                user=self.config["user"],
                password=self.config.get("password") or "",
                dsn=f"{self.config['host']}:{self.config['port']}/{self.config['service_name']}",
                tcp_connect_timeout=CONNECT_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001 - the driver's error hierarchy is its own
            raise ConnectorError(str(error).strip()[:600]) from error

    def _run(self, action):
        state: dict[str, Any] = {}

        def call():
            connection = self.connect()
            state["connection"] = connection
            try:
                cursor = connection.cursor()
                try:
                    return action(cursor)
                finally:
                    cursor.close()
            finally:
                connection.close()

        def cancel():
            connection = state.get("connection")
            if connection is not None:
                try:
                    connection.cancel()
                finally:
                    connection.close()

        try:
            return self.timed(call, cancel)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    def default_schema(self):
        return (self.config.get("schema") or self.config["user"]).upper()

    def test(self):
        started = time.monotonic()

        def probe(cursor):
            cursor.execute("SELECT banner FROM v$version")
            row = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM all_tables WHERE owner = :1", (self.default_schema(),))
            return (row[0] if row else "Oracle"), cursor.fetchone()[0]

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": str(version)[:120],
            "detail": f"连接成功，模式 {self.default_schema()} 中有 {count} 张表。",
            "table_count": int(count),
        }

    def schemas(self):
        def load(cursor):
            cursor.execute("SELECT owner, count(*) FROM all_tables GROUP BY owner ORDER BY owner")
            return cursor.fetchall()

        return [{"name": row[0], "table_count": int(row[1])} for row in self._run(load)]

    def tables(self, schema):
        check_identifier(schema, "模式名")

        def load(cursor):
            cursor.execute(
                "SELECT t.table_name, 'table', t.num_rows, c.comments FROM all_tables t "
                "LEFT JOIN all_tab_comments c ON c.owner = t.owner AND c.table_name = t.table_name "
                "WHERE t.owner = :1 "
                "UNION ALL SELECT v.view_name, 'view', NULL, c.comments FROM all_views v "
                "LEFT JOIN all_tab_comments c ON c.owner = v.owner AND c.table_name = v.view_name "
                "WHERE v.owner = :2 ORDER BY 1",
                (schema, schema),
            )
            return cursor.fetchall()

        return [
            {"schema": schema, "name": row[0], "kind": row[1], "rows": int(row[2]) if row[2] is not None else None, "comment": row[3] or ""}
            for row in self._run(load)
        ]

    def table(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT column_name, data_type, nullable FROM all_tab_columns "
                "WHERE owner = :1 AND table_name = :2 ORDER BY column_id",
                (schema, name),
            )
            columns = cursor.fetchall()
            if not columns:
                raise ConnectorError(f"未找到表 {schema}.{name}。")
            cursor.execute(
                "SELECT cc.column_name FROM all_constraints c JOIN all_cons_columns cc "
                "ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
                "WHERE c.owner = :1 AND c.table_name = :2 AND c.constraint_type = 'P'",
                (schema, name),
            )
            keys = {row[0] for row in cursor.fetchall()}
            cursor.execute(f"SELECT count(*) FROM {self.qualified(schema, name)}")
            return columns, keys, cursor.fetchone()[0]

        columns, keys, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count),
            "columns": [
                {"name": row[0], "type": row[1], "nullable": row[2] == "Y", "primary_key": row[0] in keys}
                for row in columns
            ],
        }

    def view_definition(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute("SELECT text FROM all_views WHERE owner = :1 AND view_name = :2", (schema, name))
            return cursor.fetchone()

        row = self._run(load)
        return str(row[0]) if row and row[0] else None

    def prepare(self, tree, sql, limit):
        return f"SELECT * FROM (\n{strip_statement(sql)}\n) lattice_q FETCH FIRST {int(limit)} ROWS ONLY"

    def execute(self, sql, limit):
        def call(cursor):
            cursor.execute(sql)
            columns = [item[0] for item in cursor.description or []]
            return columns, [list(row) for row in cursor.fetchmany(limit)]

        return self._run(call)


# ----- SQL Server -------------------------------------------------------------------
class SqlServerConnector(Connector):
    type_id = "mssql"
    label = "SQL Server"
    category = "关系数据库"
    description = "Microsoft SQL Server 2016+ 与 Azure SQL Database。"
    driver = "pymssql"
    dialect = "tsql"
    default_port = 1433
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=1433),
        field("user", "用户名", required=True),
        field("password", "密码", "password"),
        field("database", "数据库", required=True),
        field("schema", "默认模式", default="dbo"),
    ]

    def connect(self):
        pymssql = optional_driver("pymssql")
        try:
            return pymssql.connect(
                server=self.config["host"],
                port=str(self.config["port"]),
                user=self.config["user"],
                password=self.config.get("password") or "",
                database=self.config["database"],
                login_timeout=CONNECT_TIMEOUT,
                timeout=QUERY_TIMEOUT,
                autocommit=True,
            )
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    def _run(self, action):
        state: dict[str, Any] = {}

        def call():
            connection = self.connect()
            state["connection"] = connection
            try:
                cursor = connection.cursor()
                try:
                    return action(cursor)
                finally:
                    cursor.close()
            finally:
                connection.close()

        def cancel():
            connection = state.get("connection")
            if connection is not None:
                connection.close()

        try:
            return self.timed(call, cancel)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    def default_schema(self):
        return self.config.get("schema") or "dbo"

    def test(self):
        started = time.monotonic()

        def probe(cursor):
            cursor.execute("SELECT @@VERSION")
            version = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s", (self.default_schema(),)
            )
            return version, cursor.fetchone()[0]

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": str(version).split("\n")[0][:120],
            "detail": f"连接成功，模式 {self.default_schema()} 中有 {count} 张表。",
            "table_count": int(count),
        }

    def schemas(self):
        def load(cursor):
            cursor.execute(
                "SELECT s.name, count(t.object_id) FROM sys.schemas s "
                "LEFT JOIN sys.tables t ON t.schema_id = s.schema_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') AND s.name NOT LIKE 'db[_]%' "
                "GROUP BY s.name ORDER BY s.name"
            )
            return cursor.fetchall()

        return [{"name": row[0], "table_count": int(row[1])} for row in self._run(load)]

    def tables(self, schema):
        check_identifier(schema, "模式名")

        def load(cursor):
            cursor.execute(
                "SELECT t.name, 'table', SUM(p.rows), CAST(ep.value AS nvarchar(4000)) FROM sys.tables t "
                "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "LEFT JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
                "LEFT JOIN sys.extended_properties ep ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
                "WHERE s.name = %s GROUP BY t.name, CAST(ep.value AS nvarchar(4000)) "
                "UNION ALL SELECT v.name, 'view', NULL, NULL FROM sys.views v "
                "JOIN sys.schemas s ON s.schema_id = v.schema_id WHERE s.name = %s ORDER BY 1",
                (schema, schema),
            )
            return cursor.fetchall()

        return [
            {"schema": schema, "name": row[0], "kind": row[1], "rows": int(row[2]) if row[2] is not None else None, "comment": row[3] or ""}
            for row in self._run(load)
        ]

    def table(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema, name),
            )
            columns = cursor.fetchall()
            if not columns:
                raise ConnectorError(f"未找到表 {schema}.{name}。")
            cursor.execute(
                "SELECT k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS c "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON k.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
                "AND k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME "
                "WHERE c.CONSTRAINT_TYPE = 'PRIMARY KEY' AND c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s",
                (schema, name),
            )
            keys = {row[0] for row in cursor.fetchall()}
            cursor.execute(f"SELECT count(*) FROM {self.qualified(schema, name)}")
            return columns, keys, cursor.fetchone()[0]

        columns, keys, count = self._run(load)
        return {
            "schema": schema,
            "name": name,
            "kind": "table",
            "rows": int(count),
            "columns": [
                {"name": row[0], "type": row[1], "nullable": row[2] == "YES", "primary_key": row[0] in keys}
                for row in columns
            ],
        }

    def view_definition(self, schema, name):
        check_identifier(schema, "模式名")
        check_identifier(name, "表名")

        def load(cursor):
            cursor.execute(
                "SELECT m.definition FROM sys.sql_modules m JOIN sys.views v ON v.object_id = m.object_id "
                "JOIN sys.schemas s ON s.schema_id = v.schema_id WHERE s.name = %s AND v.name = %s",
                (schema, name),
            )
            return cursor.fetchone()

        row = self._run(load)
        return str(row[0]) if row and row[0] else None

    def prepare(self, tree, sql, limit):
        return f"SELECT TOP ({int(limit)}) * FROM (\n{strip_statement(sql)}\n) AS lattice_q"

    def execute(self, sql, limit):
        def call(cursor):
            cursor.execute(sql)
            columns = [item[0] for item in cursor.description or []]
            return columns, [list(row) for row in cursor.fetchmany(limit)]

        return self._run(call)


# ----- MongoDB ------------------------------------------------------------------------
class MongoDBConnector(SnapshotConnector):
    type_id = "mongodb"
    label = "MongoDB"
    category = "文档数据库"
    description = "MongoDB 4.4+：数据库作为库、集合作为表，字段按抽样文档推断；查询在文档快照上以 DuckDB SQL 执行。"
    driver = "pymongo"
    default_port = 27017
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=27017),
        field("user", "用户名"),
        field("password", "密码", "password"),
        field("database", "默认数据库", required=True),
        field("auth_source", "认证数据库", default="admin"),
        field("tls", "启用 TLS", "checkbox", default=False),
        sample_rows_field(),
    ]

    def default_schema(self):
        return self.config.get("database") or None

    def client(self):
        pymongo = optional_driver("pymongo")
        options: dict[str, Any] = {
            "host": self.config["host"],
            "port": self.config["port"],
            "serverSelectionTimeoutMS": CONNECT_TIMEOUT * 1000,
            "connectTimeoutMS": CONNECT_TIMEOUT * 1000,
            "socketTimeoutMS": QUERY_TIMEOUT * 1000,
            "tls": bool(self.config.get("tls")),
        }
        if self.config.get("user"):
            options.update(
                username=self.config["user"],
                password=self.config.get("password") or "",
                authSource=self.config.get("auth_source") or "admin",
            )
        try:
            return pymongo.MongoClient(**options)
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    def _run(self, action):
        def call():
            client = self.client()
            try:
                return action(client)
            finally:
                client.close()

        try:
            return self.timed(call)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    def load_arrow(self, schema, name, limit):
        check_identifier(schema, "库名")
        return rows_to_arrow(self.load_rows(schema, name, self.sample_limit(limit)))

    def test(self):
        started = time.monotonic()

        def probe(client):
            info = client.server_info()
            names = client[self.config["database"]].list_collection_names()
            return info.get("version", ""), len(names)

        version, count = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": str(version),
            "detail": f"连接成功，数据库 {self.config['database']} 中有 {count} 个集合。",
            "table_count": int(count),
        }

    def schemas(self):
        def load(client):
            return [
                {"name": name, "table_count": len(client[name].list_collection_names())}
                for name in client.list_database_names()
                if name not in SYSTEM_DATABASES_MONGO
            ]

        return self._run(load)

    def tables(self, schema):
        check_identifier(schema, "库名")

        def load(client):
            database = client[schema]
            return [
                {"schema": schema, "name": name, "kind": "collection", "rows": int(database[name].estimated_document_count()), "comment": ""}
                for name in sorted(database.list_collection_names())
            ]

        return self._run(load)

    def table(self, schema, name):
        check_identifier(schema, "库名")
        check_identifier(name, "表名")

        def load(client):
            collection = client[schema][name]
            sample = [flatten_document(document) for document in collection.find({}, limit=200)]
            return sample, int(collection.estimated_document_count())

        sample, count = self._run(load)
        if not sample and count == 0:
            raise ConnectorError(f"未找到集合 {schema}.{name}，或集合为空。")
        return {"schema": schema, "name": name, "kind": "collection", "rows": count, "columns": infer_columns(sample)}

    def load_rows(self, schema, name, limit):
        check_identifier(name, "表名")

        def load(client):
            if schema not in client.list_database_names():
                raise ConnectorError(f"未找到数据库 {schema}。")
            return [flatten_document(document) for document in client[schema][name].find({}, limit=limit)]

        return self._run(load)


# ----- Elasticsearch ------------------------------------------------------------------
class ElasticsearchConnector(SnapshotConnector):
    type_id = "elasticsearch"
    label = "Elasticsearch"
    category = "搜索引擎"
    description = "Elasticsearch 7/8 与 OpenSearch：索引作为表、映射作为字段；查询在文档快照上以 DuckDB SQL 执行。"
    driver = "REST API（httpx）"
    default_port = 9200
    schema_name = "indices"
    fields = [
        field("host", "主机", required=True, placeholder="127.0.0.1"),
        field("port", "端口", "number", required=True, default=9200),
        field("scheme", "协议", "select", default="http", options=[("http", "http"), ("https", "https")]),
        field("user", "用户名"),
        field("password", "密码", "password"),
        field("api_key", "API Key", "password", help="填写后优先于用户名密码。"),
        field("verify_ssl", "校验证书", "checkbox", default=True),
        sample_rows_field(),
    ]
    #: Tests inject an ``httpx.MockTransport`` here.
    _transport: Any = None

    def summary(self):
        return f"{self.config.get('scheme') or 'http'}://{self.config['host']}:{self.config['port']}"

    def _client(self):
        headers = {"Accept": "application/json"}
        auth = None
        if self.config.get("api_key"):
            headers["Authorization"] = f"ApiKey {self.config['api_key']}"
        elif self.config.get("user"):
            auth = (self.config["user"], self.config.get("password") or "")
        return httpx.Client(
            base_url=self.summary(),
            headers=headers,
            auth=auth,
            verify=bool(self.config.get("verify_ssl", True)),
            timeout=httpx.Timeout(QUERY_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=self._transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        def call():
            with self._client() as client:
                response = client.request(method, path, **kwargs)
            if response.status_code >= 400:
                detail = response.text[:300]
                try:
                    detail = response.json().get("error", {}).get("reason") or detail
                except (ValueError, AttributeError):
                    pass
                raise ConnectorError(f"Elasticsearch 返回 HTTP {response.status_code}：{detail}")
            return response.json()

        try:
            return self.timed(call)
        except ConnectorError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectorError(str(error).strip()[:600]) from error

    def _indices(self) -> list[dict[str, Any]]:
        listing = self._request("GET", "/_cat/indices", params={"format": "json", "h": "index,docs.count,store.size,status"})
        rows = []
        for item in listing if isinstance(listing, list) else []:
            name = str(item.get("index") or "")
            if not name or name.startswith("."):
                continue
            count = item.get("docs.count")
            rows.append({"schema": self.schema_name, "name": name, "kind": "index", "rows": int(count) if str(count or "").isdigit() else None, "comment": str(item.get("store.size") or "")})
        return sorted(rows, key=lambda row: row["name"])

    def test(self):
        started = time.monotonic()
        root = self._request("GET", "/")
        indices = self._indices()
        version = ((root.get("version") or {}).get("number")) if isinstance(root, dict) else ""
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": str(version or ""),
            "detail": f"连接成功，集群 {root.get('cluster_name', '') if isinstance(root, dict) else ''} 有 {len(indices)} 个索引。",
            "table_count": len(indices),
        }

    def schemas(self):
        return [{"name": self.schema_name, "table_count": len(self._indices())}]

    def tables(self, schema):
        if schema != self.schema_name:
            raise ConnectorError(f"Elasticsearch 数据源只有 {self.schema_name} 一个库。")
        return self._indices()

    def table(self, schema, name):
        check_identifier(name, "索引名")
        mapping = self._request("GET", f"/{name}/_mapping")
        properties = (((mapping or {}).get(name) or {}).get("mappings") or {}).get("properties") or {}
        columns: list[dict[str, Any]] = [{"name": "_id", "type": "keyword", "nullable": False}]

        def walk(items: dict[str, Any], prefix: str, depth: int) -> None:
            for key, spec in items.items():
                kind = str(spec.get("type") or ("object" if spec.get("properties") else "object"))
                if spec.get("properties") and depth < 2:
                    walk(spec["properties"], f"{prefix}{key}.", depth + 1)
                else:
                    columns.append({"name": f"{prefix}{key}", "type": kind, "nullable": True})

        walk(properties, "", 0)
        count = self._request("GET", f"/{name}/_count")
        return {"schema": self.schema_name, "name": name, "kind": "index", "rows": int((count or {}).get("count") or 0), "columns": columns}

    def load_rows(self, schema, name, limit):
        check_identifier(name, "索引名")
        size = min(limit, ELASTIC_PAGE)
        answer = self._request("POST", f"/{name}/_search", json={"size": size, "query": {"match_all": {}}, "track_total_hits": False})
        hits = ((answer or {}).get("hits") or {}).get("hits") or []
        return [{"_id": hit.get("_id"), **flatten_document(hit.get("_source") or {})} for hit in hits]


# ----- Kafka ------------------------------------------------------------------------------
class KafkaConnector(SnapshotConnector):
    type_id = "kafka"
    label = "Apache Kafka"
    category = "消息队列"
    description = "Kafka 2.x+：主题作为表，读取最近的消息（JSON 消息展开为字段）后以 DuckDB SQL 查询；partition / offset / key / value 列需用双引号引用。"
    driver = "kafka-python"
    default_port = 9092
    schema_name = "topics"
    fields = [
        field("bootstrap_servers", "Bootstrap 服务器", required=True, placeholder="127.0.0.1:9092,127.0.0.1:9093"),
        field(
            "security_protocol",
            "安全协议",
            "select",
            default="PLAINTEXT",
            options=[("PLAINTEXT", "PLAINTEXT"), ("SSL", "SSL"), ("SASL_PLAINTEXT", "SASL_PLAINTEXT"), ("SASL_SSL", "SASL_SSL")],
        ),
        field(
            "sasl_mechanism",
            "SASL 机制",
            "select",
            default="PLAIN",
            options=[("PLAIN", "PLAIN"), ("SCRAM-SHA-256", "SCRAM-SHA-256"), ("SCRAM-SHA-512", "SCRAM-SHA-512")],
        ),
        field("sasl_username", "SASL 用户名"),
        field("sasl_password", "SASL 密码", "password"),
        sample_rows_field(1000),
        field("poll_seconds", "读取等待（秒）", "number", default=5, help="消费消息时最多等待多久。"),
    ]

    def summary(self):
        return self.config["bootstrap_servers"]

    def _servers(self) -> list[str]:
        servers = [item.strip() for item in str(self.config["bootstrap_servers"]).replace(";", ",").split(",") if item.strip()]
        if not servers:
            raise ConnectorError("请填写 Bootstrap 服务器。")
        return servers

    def _kwargs(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "bootstrap_servers": self._servers(),
            "security_protocol": self.config.get("security_protocol") or "PLAINTEXT",
            "request_timeout_ms": QUERY_TIMEOUT * 1000,
            "api_version_auto_timeout_ms": CONNECT_TIMEOUT * 1000,
        }
        if str(options["security_protocol"]).startswith("SASL"):
            options.update(
                sasl_mechanism=self.config.get("sasl_mechanism") or "PLAIN",
                sasl_plain_username=self.config.get("sasl_username") or "",
                sasl_plain_password=self.config.get("sasl_password") or "",
            )
        return options

    def _consumer(self, kafka):
        return kafka.KafkaConsumer(
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            consumer_timeout_ms=max(1, int(self.config.get("poll_seconds") or 5)) * 1000,
            **self._kwargs(),
        )

    def _run(self, action):
        def call():
            kafka = optional_driver("kafka")
            return action(kafka)

        try:
            return self.timed(call)
        except ConnectorError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ConnectorError(str(error).strip()[:600]) from error

    @staticmethod
    def _user_topics(names: Iterable[str]) -> list[str]:
        return sorted(name for name in names if name and not name.startswith("__"))

    def test(self):
        started = time.monotonic()

        def probe(kafka):
            admin = kafka.KafkaAdminClient(**self._kwargs())
            try:
                return self._user_topics(admin.list_topics())
            finally:
                admin.close()

        topics = self._run(probe)
        return {
            "ok": True,
            "status": "online",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "server_version": "",
            "detail": f"连接成功，共 {len(topics)} 个主题。",
            "table_count": len(topics),
        }

    def schemas(self):
        def load(kafka):
            consumer = self._consumer(kafka)
            try:
                return len(self._user_topics(consumer.topics()))
            finally:
                consumer.close()

        return [{"name": self.schema_name, "table_count": self._run(load)}]

    def _offsets(self, kafka, consumer, topic: str):
        partitions = consumer.partitions_for_topic(topic) or set()
        assigned = [kafka.TopicPartition(topic, partition) for partition in sorted(partitions)]
        if not assigned:
            raise ConnectorError(f"未找到主题 {topic}。")
        beginning = consumer.beginning_offsets(assigned)
        end = consumer.end_offsets(assigned)
        return assigned, beginning, end

    def tables(self, schema):
        if schema != self.schema_name:
            raise ConnectorError(f"Kafka 数据源只有 {self.schema_name} 一个库。")

        def load(kafka):
            consumer = self._consumer(kafka)
            try:
                rows = []
                for topic in self._user_topics(consumer.topics()):
                    assigned, beginning, end = self._offsets(kafka, consumer, topic)
                    total = sum(int(end.get(tp, 0)) - int(beginning.get(tp, 0)) for tp in assigned)
                    rows.append({"schema": schema, "name": topic, "kind": "topic", "rows": total, "comment": f"{len(assigned)} 个分区"})
                return rows
            finally:
                consumer.close()

        return self._run(load)

    def table(self, schema, name):
        check_identifier(name, "主题名")
        sample = self.load_rows(schema, name, 100)
        total = self._message_count(name)
        return {"schema": self.schema_name, "name": name, "kind": "topic", "rows": total, "columns": infer_columns(sample) if sample else infer_columns([self._record_row(None)])}

    def _message_count(self, topic: str) -> int:
        def load(kafka):
            consumer = self._consumer(kafka)
            try:
                assigned, beginning, end = self._offsets(kafka, consumer, topic)
                return sum(int(end.get(tp, 0)) - int(beginning.get(tp, 0)) for tp in assigned)
            finally:
                consumer.close()

        return self._run(load)

    @staticmethod
    def _decode(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.hex()
        return str(value)

    def _record_row(self, record: Any) -> dict[str, Any]:
        if record is None:
            return {"partition": 0, "offset": 0, "timestamp": None, "key": None, "value": None}
        value = self._decode(getattr(record, "value", None))
        row: dict[str, Any] = {
            "partition": int(getattr(record, "partition", 0)),
            "offset": int(getattr(record, "offset", 0)),
            "timestamp": dt.datetime.fromtimestamp(int(getattr(record, "timestamp", 0) or 0) / 1000, tz=dt.timezone.utc).replace(tzinfo=None) if getattr(record, "timestamp", None) else None,
            "key": self._decode(getattr(record, "key", None)),
            "value": value,
        }
        if isinstance(value, str) and value[:1] in "{[":
            try:
                payload = json.loads(value)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                for key, item in flatten_document(payload).items():
                    row[key if key not in row else f"value.{key}"] = item
        return row

    def load_rows(self, schema, name, limit):
        check_identifier(name, "主题名")

        def load(kafka):
            consumer = self._consumer(kafka)
            try:
                assigned, beginning, end = self._offsets(kafka, consumer, name)
                consumer.assign(assigned)
                share = max(1, limit // len(assigned))
                for tp in assigned:
                    consumer.seek(tp, max(int(beginning.get(tp, 0)), int(end.get(tp, 0)) - share))
                rows = []
                for record in consumer:
                    rows.append(self._record_row(record))
                    if len(rows) >= limit:
                        break
                return rows
            finally:
                consumer.close()

        return self._run(load)


CONNECTORS: dict[str, type[Connector]] = {
    connector.type_id: connector
    for connector in (
        DuckDBConnector,
        MySQLConnector,
        PostgresConnector,
        ClickHouseConnector,
        StarRocksConnector,
        DorisConnector,
        HiveConnector,
        IcebergConnector,
        PaimonConnector,
        OracleConnector,
        SqlServerConnector,
        MongoDBConnector,
        ElasticsearchConnector,
        KafkaConnector,
    )
}
DIALECT_LABELS = {
    "duckdb": "DuckDB SQL",
    "mysql": "MySQL SQL",
    "postgres": "PostgreSQL SQL",
    "clickhouse": "ClickHouse SQL",
    "starrocks": "StarRocks SQL（MySQL 兼容）",
    "doris": "Apache Doris SQL（MySQL 兼容）",
    "hive": "HiveQL",
    "oracle": "Oracle SQL",
    "tsql": "SQL Server T-SQL",
}


def connector_types() -> list[dict[str, Any]]:
    return [connector.descriptor() for connector in CONNECTORS.values()]


def make_connector(type_id: str, config: dict[str, Any]) -> Connector:
    connector = CONNECTORS.get(type_id)
    if connector is None:
        raise ConnectorError("不支持的数据源类型：" + str(type_id)[:40])
    return connector(config)


def mask_config(type_id: str, config: dict[str, Any]) -> dict[str, Any]:
    connector = CONNECTORS.get(type_id)
    secrets = connector.secret_fields() if connector else set()
    return {
        key: (SECRET_MASK if key in secrets and value else value)
        for key, value in config.items()
    }


def merge_secrets(type_id: str, incoming: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """Replace mask placeholders (or omitted secrets) with the stored secret."""
    connector = CONNECTORS.get(type_id)
    secrets = connector.secret_fields() if connector else set()
    merged = dict(incoming)
    for key in secrets:
        if merged.get(key) == SECRET_MASK or (key not in merged and stored):
            if stored and stored.get(key):
                merged[key] = stored[key]
            elif merged.get(key) == SECRET_MASK:
                merged[key] = ""
    return merged


def iter_dialects() -> Iterable[str]:
    return sorted({connector.dialect for connector in CONNECTORS.values()})
