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
import math
import re
import ssl as ssl_module
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
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
