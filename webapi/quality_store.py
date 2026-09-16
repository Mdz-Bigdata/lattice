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

"""PostgreSQL repository for the 数据质量 module's own metadata.

The quality module keeps its rules, schedules, executions and results in the
user's real PostgreSQL instance, in an **additive** schema of its own
(``lattice_quality``). Nothing outside that schema is ever read for control data
or modified: the application tables in ``public`` / ``warehouse`` are targets of
quality rules, never storage for them.

Design decisions this file embodies:

* the configured DSN is a SQLAlchemy-style URL (``postgresql+asyncpg://…``)
  while this project speaks **psycopg 3 (sync)**, so :func:`parse_dsn` strips the
  driver suffix and produces a libpq conninfo string;
* every value reaching SQL is a bound parameter — only identifiers of this
  module's own schema are ever formatted into a statement, and those are
  module constants;
* ``psycopg_pool`` is optional. When the package is installed a small pool is
  used, otherwise each call opens a short-lived connection under a lock; no new
  dependency is introduced for this;
* :meth:`QualityStore.bootstrap` is idempotent and must never fail the app
  lifespan — an unreachable database is reported by :meth:`QualityStore.healthy`
  and turned into a Chinese error by the API layer instead;
* rows leave the store JSON-safe through one helper (:func:`json_safe`), so
  ``Decimal`` counts render as integers and timestamps as ISO-8601 text.

Chinese labels that belong to the metric catalog (metric names) are *not*
duplicated here; the API layer attaches them. The rule-level and dimension
section titles used by 质量报告分析 are stable enumerations of this schema, so
they are resolved locally and the store stays free of catalog imports.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from . import env

try:  # Optional: a pool is nicer under the scheduler thread, but not required.
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - exercised by the deployment without it
    ConnectionPool = None

DEFAULT_DSN = "postgresql+asyncpg://postgres:postgres@localhost:5432/blog_converter"
# Read through ``env`` as ``LATTICE_QUALITY_DSN``.
DSN_SETTING = "QUALITY_DSN"
SCHEMA = "lattice_quality"
SCHEMES = ("postgres", "postgresql")
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT = 30
POOL_MAX_SIZE = 4
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
TABLES = ("dv_rule", "dv_job_schedule", "dv_job_execution", "dv_job_execution_result", "dv_task_run", "dv_rule_template")
LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低"}
DIMENSION_LABELS = {
    "uniqueness": "唯一性校验",
    "completeness": "完整性校验",
    "accuracy": "准确性校验",
    "standard": "数据标准校验",
    "relation": "关联性校验",
    "timeliness": "及时性校验",
    "consistency": "一致性校验",
    "custom": "自定义校验",
}
QUALITY_BANDS = ((80, "优秀"), (60, "良好"), (40, "中等"), (20, "合格"))

RULE_TABLE = f"{SCHEMA}.dv_rule"
SCHEDULE_TABLE = f"{SCHEMA}.dv_job_schedule"
EXECUTION_TABLE = f"{SCHEMA}.dv_job_execution"
RESULT_TABLE = f"{SCHEMA}.dv_job_execution_result"
TASK_RUN_TABLE = f"{SCHEMA}.dv_task_run"
TEMPLATE_TABLE = f"{SCHEMA}.dv_rule_template"

RULE_COLUMNS = (
    "id, name, metric, dimension, level, datasource_id, datasource_name, schema_name, "
    "table_name, column_name, config, expected_type, result_formula, operator, threshold, "
    "state, comment, create_time, update_time"
)
SCHEDULE_COLUMNS = (
    "id, name, bean_name, method_name, method_params, cron_expression, state, "
    "retry_limit, retry_delay_seconds, misfire_policy, max_backfill, last_status, last_message, "
    "last_fire_time, next_fire_time, create_time, update_time"
)
#: How a schedule treats firings missed while the gateway was down.
MISFIRE_POLICIES = ("skip", "once", "all")
MISFIRE_LABELS = {"skip": "跳过", "once": "补跑一次", "all": "逐次补跑"}
MAX_RETRY_LIMIT = 10
MAX_RETRY_DELAY = 3600
MAX_BACKFILL = 50
TASK_RUN_STATUSES = ("running", "success", "failed", "retrying", "skipped")
TASK_RUN_STATUS_LABELS = {
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "retrying": "等待重试",
    "skipped": "已跳过",
}
TASK_RUN_COLUMNS = (
    "id, schedule_id, schedule_name, task, task_label, trigger_type, attempt, status, "
    "planned_time, start_time, end_time, elapsed_ms, message, detail, create_time"
)
TEMPLATE_COLUMNS = (
    "id, name, description, metric, dimension, level, config, expected_type, result_formula, "
    "operator, threshold, builtin, create_time, update_time"
)
EXECUTION_COLUMNS = (
    "id, rule_id, rule_name, schedule_id, trigger_type, status, start_time, end_time, "
    "elapsed_ms, message, create_time"
)
RESULT_COLUMNS = (
    "id, job_execution_id, rule_id, rule_name, metric_name, metric_dimension, datasource_id, "
    "datasource_name, database_name, table_name, column_name, rule_level, checked_count, "
    "actual_value, expected_value, expected_type, result_formula, operator, threshold, score, "
    "state, invalidate_sql, check_time"
)

DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {RULE_TABLE} (
  id              bigserial PRIMARY KEY,
  name            varchar(200) NOT NULL,
  metric          varchar(64)  NOT NULL,
  dimension       varchar(32)  NOT NULL,
  level           varchar(8)   NOT NULL DEFAULT 'medium',
  datasource_id   varchar(64)  NOT NULL,
  datasource_name varchar(200) NOT NULL DEFAULT '',
  schema_name     varchar(200),
  table_name      varchar(200) NOT NULL,
  column_name     varchar(200),
  config          jsonb        NOT NULL DEFAULT '{{}}'::jsonb,
  expected_type   varchar(32)  NOT NULL DEFAULT 'fix_value',
  result_formula  varchar(32)  NOT NULL DEFAULT 'actual',
  operator        varchar(8)   NOT NULL DEFAULT 'lte',
  threshold       numeric(20,4) NOT NULL DEFAULT 0,
  state           smallint     NOT NULL DEFAULT 1,
  comment         text         NOT NULL DEFAULT '',
  create_time     timestamptz  NOT NULL DEFAULT now(),
  update_time     timestamptz  NOT NULL DEFAULT now()
)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEDULE_TABLE} (
  id              bigserial PRIMARY KEY,
  name            varchar(200) NOT NULL,
  bean_name       varchar(120) NOT NULL DEFAULT 'QualityTask',
  method_name     varchar(120) NOT NULL DEFAULT 'run',
  method_params   varchar(500) NOT NULL DEFAULT '',
  cron_expression varchar(120) NOT NULL,
  state           smallint     NOT NULL DEFAULT 0,
  retry_limit     smallint     NOT NULL DEFAULT 0,
  retry_delay_seconds integer  NOT NULL DEFAULT 60,
  misfire_policy  varchar(16)  NOT NULL DEFAULT 'skip',
  max_backfill    smallint     NOT NULL DEFAULT 10,
  last_status     varchar(16)  NOT NULL DEFAULT '',
  last_message    text         NOT NULL DEFAULT '',
  last_fire_time  timestamptz,
  next_fire_time  timestamptz,
  create_time     timestamptz  NOT NULL DEFAULT now(),
  update_time     timestamptz  NOT NULL DEFAULT now()
)""",
    # Schedules created before the task registry existed gain the new columns in place.
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS retry_limit smallint NOT NULL DEFAULT 0",
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS retry_delay_seconds integer NOT NULL DEFAULT 60",
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS misfire_policy varchar(16) NOT NULL DEFAULT 'skip'",
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS max_backfill smallint NOT NULL DEFAULT 10",
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS last_status varchar(16) NOT NULL DEFAULT ''",
    f"ALTER TABLE {SCHEDULE_TABLE} ADD COLUMN IF NOT EXISTS last_message text NOT NULL DEFAULT ''",
    f"""CREATE TABLE IF NOT EXISTS {EXECUTION_TABLE} (
  id           bigserial PRIMARY KEY,
  rule_id      bigint,
  rule_name    varchar(200) NOT NULL DEFAULT '',
  schedule_id  bigint,
  trigger_type varchar(16)  NOT NULL,
  status       smallint     NOT NULL,
  start_time   timestamptz  NOT NULL DEFAULT now(),
  end_time     timestamptz,
  elapsed_ms   integer,
  message      text NOT NULL DEFAULT '',
  create_time  timestamptz  NOT NULL DEFAULT now()
)""",
    f"""CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
  id               bigserial PRIMARY KEY,
  job_execution_id bigint NOT NULL,
  rule_id          bigint,
  rule_name        varchar(200) NOT NULL DEFAULT '',
  metric_name      varchar(64),
  metric_dimension varchar(32),
  datasource_id    varchar(64),
  datasource_name  varchar(200),
  database_name    varchar(200),
  table_name       varchar(200),
  column_name      varchar(200),
  rule_level       varchar(8),
  checked_count    numeric(20,0) NOT NULL DEFAULT 0,
  actual_value     numeric(20,4) NOT NULL DEFAULT 0,
  expected_value   numeric(20,4),
  expected_type    varchar(32),
  result_formula   varchar(32),
  operator         varchar(8),
  threshold        numeric(20,4),
  score            numeric(20,4),
  state            smallint NOT NULL,
  invalidate_sql   text NOT NULL DEFAULT '',
  check_time       timestamptz NOT NULL DEFAULT now()
)""",
    f"CREATE INDEX IF NOT EXISTS idx_dv_result_check_time ON {RESULT_TABLE} (check_time DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_dv_result_rule ON {RESULT_TABLE} (rule_id)",
    f"CREATE INDEX IF NOT EXISTS idx_dv_execution_start ON {EXECUTION_TABLE} (start_time DESC)",
    f"""CREATE TABLE IF NOT EXISTS {TASK_RUN_TABLE} (
  id            bigserial PRIMARY KEY,
  schedule_id   bigint,
  schedule_name varchar(200) NOT NULL DEFAULT '',
  task          varchar(240) NOT NULL DEFAULT '',
  task_label    varchar(200) NOT NULL DEFAULT '',
  trigger_type  varchar(16)  NOT NULL DEFAULT 'schedule',
  attempt       smallint     NOT NULL DEFAULT 1,
  status        varchar(16)  NOT NULL DEFAULT 'running',
  planned_time  timestamptz,
  start_time    timestamptz  NOT NULL DEFAULT now(),
  end_time      timestamptz,
  elapsed_ms    integer,
  message       text         NOT NULL DEFAULT '',
  detail        jsonb        NOT NULL DEFAULT '{{}}'::jsonb,
  create_time   timestamptz  NOT NULL DEFAULT now()
)""",
    f"CREATE INDEX IF NOT EXISTS idx_dv_task_run_start ON {TASK_RUN_TABLE} (start_time DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_dv_task_run_schedule ON {TASK_RUN_TABLE} (schedule_id)",
    f"""CREATE TABLE IF NOT EXISTS {TEMPLATE_TABLE} (
  id              bigserial PRIMARY KEY,
  name            varchar(200) NOT NULL,
  description     text         NOT NULL DEFAULT '',
  metric          varchar(64)  NOT NULL,
  dimension       varchar(32)  NOT NULL,
  level           varchar(8)   NOT NULL DEFAULT 'medium',
  config          jsonb        NOT NULL DEFAULT '{{}}'::jsonb,
  expected_type   varchar(32)  NOT NULL DEFAULT 'fix_value',
  result_formula  varchar(32)  NOT NULL DEFAULT 'actual',
  operator        varchar(8)   NOT NULL DEFAULT 'lte',
  threshold       numeric(20,4) NOT NULL DEFAULT 0,
  builtin         smallint     NOT NULL DEFAULT 0,
  create_time     timestamptz  NOT NULL DEFAULT now(),
  update_time     timestamptz  NOT NULL DEFAULT now()
)""",
)


class QualityStoreError(ValueError):
    """A user-facing store error; the message is Chinese and safe to display."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def parse_dsn(url: str) -> str:
    """Turn a SQLAlchemy-style URL into a libpq conninfo string.

    Accepts ``postgresql://``, ``postgres://`` and any ``+driver`` suffix
    (``asyncpg``, ``psycopg2``, …), which is stripped because this project talks
    to PostgreSQL through psycopg 3. The user info is percent-decoded, a missing
    port or password is simply omitted, and query parameters are passed through
    as libpq options. A non-PostgreSQL scheme raises ``QualityStoreError(400)``.
    """
    if not isinstance(url, str) or not url.strip():
        raise QualityStoreError(400, "质量元数据库连接串不能为空。")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower().split("+", 1)[0]
    if scheme not in SCHEMES:
        raise QualityStoreError(400, "质量元数据库仅支持 PostgreSQL 连接串。")
    try:
        port = parts.port
    except ValueError as error:
        raise QualityStoreError(400, "质量元数据库连接串的端口无效。") from error
    params: dict[str, Any] = {}
    if parts.hostname:
        params["host"] = unquote(parts.hostname)
    if port is not None:
        params["port"] = port
    if parts.username:
        params["user"] = unquote(parts.username)
    if parts.password is not None:
        params["password"] = unquote(parts.password)
    database = unquote(parts.path.lstrip("/"))
    if database:
        params["dbname"] = database
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        params[key.lower()] = value
    if not params:
        raise QualityStoreError(400, "质量元数据库连接串无效。")
    try:
        return make_conninfo(**params)
    except psycopg.Error as error:
        raise QualityStoreError(400, f"质量元数据库连接串无效：{detail(error)}。") from error


def detail(error: BaseException) -> str:
    """Shorten a driver error so it can be embedded in a user-facing message."""
    return str(error).strip().splitlines()[0][:300] if str(error).strip() else type(error).__name__


def json_safe(value: Any) -> Any:
    """Convert a psycopg value into something ``json.dumps`` accepts.

    ``connectors.json_value`` turns every ``Decimal`` into a float; quality
    counts are whole numbers stored as ``numeric``, and the screens show them as
    counts, so an integral ``Decimal`` becomes an ``int`` here instead.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int)):
        return value
    return str(value)


def plain_number(value: float) -> int | float:
    """Totals are summed as floats; whole numbers render as counts, not ``1.0``."""
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)


def quality_score(checked: float, errors: float) -> float:
    """One score definition for every category: higher is better, 0 checked → 100."""
    if checked <= 0:
        return 100.0
    return round((checked - errors) / checked * 100, 2)


def quality_band(score: float | None) -> str:
    """Map a score onto the 优秀/良好/中等/合格/不合格 bands of 质量报告分析."""
    if score is None:
        return "不合格"
    for floor, label in QUALITY_BANDS:
        if score >= floor:
            return label
    return "不合格"


def like_pattern(value: str) -> str:
    """Wrap a search term for ``ILIKE``; wildcards typed by the user are literal."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _text(
    value: Any,
    limit: int,
    label: str,
    *,
    required: bool = False,
    default: str = "",
    multiline: bool = False,
) -> str:
    if value is None:
        value = default
    if not isinstance(value, str):
        raise QualityStoreError(400, f"{label}必须是文本。")
    value = value.strip() if not multiline else value.strip("\r\n\t ")
    if required and not value:
        raise QualityStoreError(400, f"{label}不能为空。")
    allowed = {10, 13, 9} if multiline else set()
    if len(value) > limit or any(ord(char) < 32 and ord(char) not in allowed for char in value):
        raise QualityStoreError(400, f"{label}须为不超过 {limit} 个字符且不含控制字符的文本。")
    return value


def _optional_text(value: Any, limit: int, label: str) -> str | None:
    if value is None:
        return None
    text = _text(value, limit, label)
    return text or None


def _number(value: Any, label: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal, str)):
        raise QualityStoreError(400, f"{label}必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise QualityStoreError(400, f"{label}必须是数字。") from error
    if not math.isfinite(number):
        raise QualityStoreError(400, f"{label}必须是有限数字。")
    return number


def _optional_number(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    return _number(value, label)


def _integer(value: Any, label: str, *, default: int = 0, minimum: int | None = None) -> int:
    if value is None or value == "":
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal, str)):
        raise QualityStoreError(400, f"{label}必须是整数。")
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise QualityStoreError(400, f"{label}必须是整数。") from error
    if minimum is not None and number < minimum:
        raise QualityStoreError(400, f"{label}不能小于 {minimum}。")
    return number


def _optional_integer(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    return _integer(value, label)


def _record_id(value: Any, label: str) -> int:
    identifier = _integer(value, label)
    if identifier <= 0:
        raise QualityStoreError(400, f"{label}无效。")
    return identifier


def _bounded(value: Any, label: str, *, default: int, minimum: int = 0, maximum: int) -> int:
    number = _integer(value, label, default=default)
    if number < minimum or number > maximum:
        raise QualityStoreError(400, f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return number


def _policy(value: Any) -> str:
    policy = _text(value, 16, "补跑策略", default="skip") or "skip"
    if policy not in MISFIRE_POLICIES:
        raise QualityStoreError(400, "补跑策略只能是 skip（跳过）、once（补跑一次）或 all（逐次补跑）。")
    return policy


def _run_status(value: Any, *, terminal: bool = False) -> str:
    status = _text(value, 16, "执行状态", required=True)
    allowed = tuple(item for item in TASK_RUN_STATUSES if not terminal or item != "running")
    if status not in allowed:
        raise QualityStoreError(400, f"执行状态只能是 {'、'.join(allowed)}。")
    return status


def _state(value: Any, label: str, *, allowed: tuple[int, ...], default: int) -> int:
    state = _integer(value, label, default=default)
    if state not in allowed:
        raise QualityStoreError(400, f"{label}只能是 {'、'.join(str(item) for item in allowed)}。")
    return state


def _config(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise QualityStoreError(400, "核查配置必须是对象。")
    return {str(key): item for key, item in value.items()}


def _timestamp(value: Any, label: str) -> dt.datetime | None:
    """Coerce a value for a ``timestamptz`` column, always as a known instant.

    A naive datetime written to ``timestamptz`` is interpreted in the *server's*
    TimeZone, not the application's. This database runs with Asia/Shanghai while
    the service may run anywhere, so a naive value would be stored hours away
    from the moment that was meant — a scheduled job would then fire late by the
    difference. Every naive value therefore gets the local offset attached here,
    at the single point where times enter the database.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError as error:
            raise QualityStoreError(400, f"{label}须为 ISO-8601 时间。") from error
    if not isinstance(value, dt.datetime):
        raise QualityStoreError(400, f"{label}须为 ISO-8601 时间。")
    return value if value.tzinfo else value.astimezone()


def _page(page: Any, size: Any) -> tuple[int, int]:
    """Clamp paging defensively; the API layer validates the same bounds first."""
    number = max(1, _integer(page, "页码", default=1))
    limit = _integer(size, "每页条数", default=DEFAULT_PAGE_SIZE)
    limit = min(MAX_PAGE_SIZE, max(1, limit))
    return limit, (number - 1) * limit


def _day(value: Any) -> dt.date:
    if value is None or value == "":
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            parsed = dt.date.fromisoformat(value.strip())
        except ValueError as error:
            raise QualityStoreError(400, "日期格式须为 YYYY-MM-DD。") from error
        # The report needs the day after the requested one, which does not exist
        # for date.max; refusing here keeps that an explicit Chinese 400 instead
        # of an OverflowError that escapes as a plain-text 500.
        if parsed >= dt.date.max:
            raise QualityStoreError(400, "报告日期超出支持范围。")
        return parsed
    raise QualityStoreError(400, "日期格式须为 YYYY-MM-DD。")


class QualityStore:
    """Every statement issued against the ``lattice_quality`` schema lives here."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or env(DSN_SETTING) or DEFAULT_DSN
        self.schema = SCHEMA
        self._lock = threading.Lock()
        self._pool: Any = None
        self._ready = False
        self._error = ""
        self._invalid: QualityStoreError | None = None
        try:
            self.conninfo = parse_dsn(self.dsn)
        except QualityStoreError as error:
            # A misconfigured DSN is a configuration problem, not a reason for the
            # gateway to refuse to start; healthy() reports it on every call.
            self.conninfo = ""
            self._invalid = error
            self._error = str(error)
        self.database = str(conninfo_to_dict(self.conninfo).get("dbname") or "")

    # ----- connections ----------------------------------------------------------
    def _connect_kwargs(self) -> dict[str, Any]:
        # A runaway metadata query must not outlive a request; mirror the
        # connector layer's timeouts so both halves behave the same.
        return {
            "connect_timeout": CONNECT_TIMEOUT,
            "autocommit": True,
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT * 1000}",
        }

    def _ensure_pool(self) -> Any:
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    self.conninfo,
                    min_size=1,
                    max_size=POOL_MAX_SIZE,
                    kwargs=self._connect_kwargs(),
                    timeout=CONNECT_TIMEOUT,
                    name="lattice-quality",
                    open=True,
                )
            return self._pool

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._invalid is not None:
            raise self._invalid
        if ConnectionPool is not None:
            with self._ensure_pool().connection() as connection:
                yield connection
            return
        # Without psycopg_pool a short-lived connection per call is enough; the
        # lock keeps the scheduler thread and request threads off each other.
        with self._lock:
            connection = psycopg.connect(self.conninfo, **self._connect_kwargs())
            try:
                yield connection
            finally:
                connection.close()

    def close(self) -> None:
        """Release the pool, if one was created; safe to call more than once."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    # ----- execution helpers ----------------------------------------------------
    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        columns = [column.name for column in cursor.description or ()]
        return [
            {name: json_safe(value) for name, value in zip(columns, row)}
            for row in cursor.fetchall()
        ]

    def _run(self, sql: str, params: Sequence[Any] = (), *, fetch: str = "none") -> Any:
        """Execute one statement; ``params`` are always bound, never formatted."""
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, tuple(params))
                    if fetch == "all":
                        return self._rows(cursor)
                    if fetch == "one":
                        rows = self._rows(cursor)
                        return rows[0] if rows else None
                    if fetch == "value":
                        row = cursor.fetchone()
                        return json_safe(row[0]) if row else None
                    return None
        except psycopg.Error as error:
            raise QualityStoreError(500, f"质量元数据库操作失败：{detail(error)}。") from error
        except OSError as error:
            raise QualityStoreError(503, f"质量元数据库连接失败：{detail(error)}。") from error

    def _paged(
        self,
        columns: str,
        table: str,
        conditions: list[str],
        params: list[Any],
        order: str,
        page: Any,
        size: Any,
    ) -> dict[str, Any]:
        # The interpolated fragments are module constants and static condition
        # text; every user value travels in ``params``.
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        limit, offset = _page(page, size)
        total = self._run(f"SELECT count(1) FROM {table}{where}", params, fetch="value")
        items = self._run(
            f"SELECT {columns} FROM {table}{where} ORDER BY {order} LIMIT %s OFFSET %s",
            [*params, limit, offset],
            fetch="all",
        )
        return {"items": items or [], "total": int(total or 0)}

    # ----- lifecycle ------------------------------------------------------------
    def bootstrap(self) -> None:
        """Create schema, tables and indexes. Idempotent, and never raises.

        The app lifespan calls this on every startup; a database that is down at
        that moment must not stop the gateway from serving everything else, so
        the failure is remembered and reported by :meth:`healthy`.
        """
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    for statement in DDL:
                        cursor.execute(statement)
        except (psycopg.Error, QualityStoreError, OSError) as error:
            self._ready = False
            self._error = detail(error)
            return
        self._ready = True
        self._error = ""

    def healthy(self) -> dict[str, Any]:
        """Report whether the metadata schema is reachable and complete."""
        base = {"database": self.database, "schema": SCHEMA}
        try:
            names = self._run(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                [SCHEMA],
                fetch="all",
            )
        except QualityStoreError as error:
            return {"ok": False, "detail": str(error), **base}
        present = {str(row["table_name"]) for row in names or ()}
        missing = [name for name in TABLES if name not in present]
        if missing:
            joined = "、".join(missing)
            return {"ok": False, "detail": f"质量元数据表缺失：{joined}。", **base}
        if not self._ready and self._error:
            return {"ok": False, "detail": f"质量元数据库初始化失败：{self._error}。", **base}
        return {"ok": True, "detail": "质量元数据库连接正常。", **base}

    # ----- rules ----------------------------------------------------------------
    def _rule_values(self, data: dict[str, Any]) -> list[Any]:
        return [
            _text(data.get("name"), 200, "规则名称", required=True),
            _text(data.get("metric"), 64, "核查类型", required=True),
            _text(data.get("dimension"), 32, "规则分类", required=True),
            _text(data.get("level"), 8, "规则级别", default="medium"),
            _text(data.get("datasource_id"), 64, "数据源", required=True),
            _text(data.get("datasource_name"), 200, "数据源名称"),
            _optional_text(data.get("schema_name"), 200, "模式名"),
            _text(data.get("table_name"), 200, "数据表", required=True),
            _optional_text(data.get("column_name"), 200, "核查字段"),
            Jsonb(_config(data.get("config"))),
            _text(data.get("expected_type"), 32, "期望值类型", default="fix_value"),
            _text(data.get("result_formula"), 32, "结果计算方式", default="actual"),
            _text(data.get("operator"), 8, "比较方式", default="lte"),
            _number(data.get("threshold"), "阈值"),
            _state(data.get("state"), "规则状态", allowed=(0, 1), default=1),
            _text(data.get("comment"), 2000, "备注", multiline=True),
        ]

    def list_rules(
        self,
        dimension: str | None = None,
        name: str | None = None,
        page: Any = 1,
        size: Any = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Page over rules, optionally filtered by category and name fragment."""
        conditions: list[str] = []
        params: list[Any] = []
        if dimension:
            conditions.append("dimension = %s")
            params.append(_text(dimension, 32, "规则分类"))
        if name:
            conditions.append("name ILIKE %s")
            params.append(like_pattern(_text(name, 200, "规则名称")))
        return self._paged(
            RULE_COLUMNS, RULE_TABLE, conditions, params, "id DESC", page, size
        )

    def get_rule(self, rule_id: Any) -> dict[str, Any]:
        """Return one rule, or raise ``QualityStoreError(404)`` when it is gone."""
        row = self._run(
            f"SELECT {RULE_COLUMNS} FROM {RULE_TABLE} WHERE id = %s",
            [_record_id(rule_id, "规则编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "核查规则不存在。")
        return row

    def create_rule(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a rule and return the stored row."""
        return self._run(
            f"""INSERT INTO {RULE_TABLE}
                (name, metric, dimension, level, datasource_id, datasource_name, schema_name,
                 table_name, column_name, config, expected_type, result_formula, operator,
                 threshold, state, comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {RULE_COLUMNS}""",
            self._rule_values(data),
            fetch="one",
        )

    def update_rule(self, rule_id: Any, data: dict[str, Any]) -> dict[str, Any]:
        """Update a rule; keys absent from ``data`` keep their stored value."""
        identifier = _record_id(rule_id, "规则编号")
        merged = {**self.get_rule(identifier), **data}
        row = self._run(
            f"""UPDATE {RULE_TABLE} SET
                name = %s, metric = %s, dimension = %s, level = %s, datasource_id = %s,
                datasource_name = %s, schema_name = %s, table_name = %s, column_name = %s,
                config = %s, expected_type = %s, result_formula = %s, operator = %s,
                threshold = %s, state = %s, comment = %s, update_time = now()
                WHERE id = %s RETURNING {RULE_COLUMNS}""",
            [*self._rule_values(merged), identifier],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "核查规则不存在。")
        return row

    def delete_rule(self, rule_id: Any) -> dict[str, Any]:
        """Delete a rule; executions already recorded keep their rule name."""
        row = self._run(
            f"DELETE FROM {RULE_TABLE} WHERE id = %s RETURNING id",
            [_record_id(rule_id, "规则编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "核查规则不存在。")
        return {"id": row["id"], "deleted": True}

    def enabled_rules(self) -> list[dict[str, Any]]:
        """Every rule the scheduler and 一键执行 are allowed to run."""
        return self._run(
            f"SELECT {RULE_COLUMNS} FROM {RULE_TABLE} WHERE state = %s ORDER BY id",
            [1],
            fetch="all",
        )

    # ----- schedules ------------------------------------------------------------
    def _schedule_values(self, data: dict[str, Any]) -> list[Any]:
        return [
            _text(data.get("name"), 200, "任务名称", required=True),
            _text(data.get("bean_name"), 120, "bean 名称", default="QualityTask"),
            _text(data.get("method_name"), 120, "方法名称", default="run"),
            _text(data.get("method_params"), 500, "方法参数"),
            _text(data.get("cron_expression"), 120, "cron 表达式", required=True),
            _state(data.get("state"), "调度状态", allowed=(0, 1), default=0),
            _bounded(data.get("retry_limit"), "失败重试次数", default=0, maximum=MAX_RETRY_LIMIT),
            _bounded(data.get("retry_delay_seconds"), "重试间隔", default=60, minimum=1, maximum=MAX_RETRY_DELAY),
            _policy(data.get("misfire_policy")),
            _bounded(data.get("max_backfill"), "补跑上限", default=10, minimum=1, maximum=MAX_BACKFILL),
        ]

    def list_schedules(
        self, name: str | None = None, page: Any = 1, size: Any = DEFAULT_PAGE_SIZE
    ) -> dict[str, Any]:
        """Page over schedules, optionally filtered by name fragment."""
        conditions: list[str] = []
        params: list[Any] = []
        if name:
            conditions.append("name ILIKE %s")
            params.append(like_pattern(_text(name, 200, "任务名称")))
        return self._paged(
            SCHEDULE_COLUMNS, SCHEDULE_TABLE, conditions, params, "id DESC", page, size
        )

    def get_schedule(self, schedule_id: Any) -> dict[str, Any]:
        """Return one schedule, or raise ``QualityStoreError(404)``."""
        row = self._run(
            f"SELECT {SCHEDULE_COLUMNS} FROM {SCHEDULE_TABLE} WHERE id = %s",
            [_record_id(schedule_id, "调度编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return row

    def create_schedule(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a schedule and return the stored row."""
        return self._run(
            f"""INSERT INTO {SCHEDULE_TABLE}
                (name, bean_name, method_name, method_params, cron_expression, state,
                 retry_limit, retry_delay_seconds, misfire_policy, max_backfill)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {SCHEDULE_COLUMNS}""",
            self._schedule_values(data),
            fetch="one",
        )

    def update_schedule(self, schedule_id: Any, data: dict[str, Any]) -> dict[str, Any]:
        """Update a schedule; keys absent from ``data`` keep their stored value."""
        identifier = _record_id(schedule_id, "调度编号")
        merged = {**self.get_schedule(identifier), **data}
        row = self._run(
            f"""UPDATE {SCHEDULE_TABLE} SET
                name = %s, bean_name = %s, method_name = %s, method_params = %s,
                cron_expression = %s, state = %s, retry_limit = %s, retry_delay_seconds = %s,
                misfire_policy = %s, max_backfill = %s, update_time = now()
                WHERE id = %s RETURNING {SCHEDULE_COLUMNS}""",
            [*self._schedule_values(merged), identifier],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return row

    def delete_schedule(self, schedule_id: Any) -> dict[str, Any]:
        """Delete a schedule."""
        row = self._run(
            f"DELETE FROM {SCHEDULE_TABLE} WHERE id = %s RETURNING id",
            [_record_id(schedule_id, "调度编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return {"id": row["id"], "deleted": True}

    def set_schedule_state(
        self, schedule_id: Any, state: Any, next_fire_time: Any = None
    ) -> dict[str, Any]:
        """Start or stop a schedule, recording the next firing when it starts."""
        row = self._run(
            f"""UPDATE {SCHEDULE_TABLE} SET state = %s, next_fire_time = %s, update_time = now()
                WHERE id = %s RETURNING {SCHEDULE_COLUMNS}""",
            [
                _state(state, "调度状态", allowed=(0, 1), default=0),
                _timestamp(next_fire_time, "下次执行时间"),
                _record_id(schedule_id, "调度编号"),
            ],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return row

    def mark_fired(self, schedule_id: Any, last: Any, next: Any) -> dict[str, Any]:
        """Record a firing; the scheduler skips misfires rather than catching up."""
        row = self._run(
            f"""UPDATE {SCHEDULE_TABLE}
                SET last_fire_time = %s, next_fire_time = %s, update_time = now()
                WHERE id = %s RETURNING {SCHEDULE_COLUMNS}""",
            [
                _timestamp(last, "上次执行时间"),
                _timestamp(next, "下次执行时间"),
                _record_id(schedule_id, "调度编号"),
            ],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return row

    def running_schedules(self) -> list[dict[str, Any]]:
        """Every schedule in state 运行, for the scheduler status panel."""
        return self._run(
            f"SELECT {SCHEDULE_COLUMNS} FROM {SCHEDULE_TABLE} WHERE state = %s ORDER BY id",
            [1],
            fetch="all",
        )

    def record_schedule_outcome(self, schedule_id: Any, status: str, message: str = "") -> dict[str, Any]:
        """Remember how the last firing ended, for the 调度 list."""
        row = self._run(
            f"""UPDATE {SCHEDULE_TABLE} SET last_status = %s, last_message = %s, update_time = now()
                WHERE id = %s RETURNING {SCHEDULE_COLUMNS}""",
            [
                _run_status(status),
                _text(message, 2000, "执行说明", multiline=True),
                _record_id(schedule_id, "调度编号"),
            ],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "调度任务不存在。")
        return row

    # ----- task runs ------------------------------------------------------------
    def start_task_run(
        self,
        *,
        schedule_id: Any = None,
        schedule_name: str = "",
        task: str = "",
        task_label: str = "",
        trigger_type: str = "schedule",
        attempt: Any = 1,
        planned_time: Any = None,
        message: str = "",
    ) -> int:
        """Open a task run in status ``running`` and return its id."""
        row = self._run(
            f"""INSERT INTO {TASK_RUN_TABLE}
                (schedule_id, schedule_name, task, task_label, trigger_type, attempt, status,
                 planned_time, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            [
                _optional_integer(schedule_id, "调度编号"),
                _text(schedule_name, 200, "任务名称"),
                _text(task, 240, "任务标识"),
                _text(task_label, 200, "任务说明"),
                _text(trigger_type, 16, "触发方式", required=True),
                _bounded(attempt, "执行次序", default=1, minimum=1, maximum=MAX_RETRY_LIMIT + 1),
                "running",
                _timestamp(planned_time, "计划执行时间"),
                _text(message, 2000, "执行说明", multiline=True),
            ],
            fetch="one",
        )
        return int(row["id"])

    def finish_task_run(
        self,
        run_id: Any,
        status: str,
        elapsed_ms: Any = None,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close a task run; no row is ever left ``running``."""
        row = self._run(
            f"""UPDATE {TASK_RUN_TABLE}
                SET status = %s, end_time = now(), elapsed_ms = %s, message = %s, detail = %s
                WHERE id = %s RETURNING {TASK_RUN_COLUMNS}""",
            [
                _run_status(status, terminal=True),
                _optional_integer(elapsed_ms, "耗时"),
                _text(message, 4000, "执行说明", multiline=True),
                Jsonb(detail if isinstance(detail, dict) else {}),
                _record_id(run_id, "执行编号"),
            ],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "任务执行记录不存在。")
        return row

    def list_task_runs(
        self,
        schedule_id: Any = None,
        status: str | None = None,
        task: str | None = None,
        page: Any = 1,
        size: Any = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Page over task runs, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if schedule_id not in (None, ""):
            conditions.append("schedule_id = %s")
            params.append(_record_id(schedule_id, "调度编号"))
        if status:
            conditions.append("status = %s")
            params.append(_run_status(status))
        if task:
            conditions.append("task = %s")
            params.append(_text(task, 240, "任务标识"))
        return self._paged(TASK_RUN_COLUMNS, TASK_RUN_TABLE, conditions, params, "id DESC", page, size)

    def get_task_run(self, run_id: Any) -> dict[str, Any]:
        row = self._run(
            f"SELECT {TASK_RUN_COLUMNS} FROM {TASK_RUN_TABLE} WHERE id = %s",
            [_record_id(run_id, "执行编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "任务执行记录不存在。")
        return row

    # ----- rule templates -------------------------------------------------------
    def _template_values(self, data: dict[str, Any]) -> list[Any]:
        return [
            _text(data.get("name"), 200, "模板名称", required=True),
            _text(data.get("description"), 2000, "模板说明", multiline=True),
            _text(data.get("metric"), 64, "核查类型", required=True),
            _text(data.get("dimension"), 32, "规则分类", required=True),
            _text(data.get("level"), 8, "规则级别", default="medium"),
            Jsonb(_config(data.get("config"))),
            _text(data.get("expected_type"), 32, "期望值类型", default="fix_value"),
            _text(data.get("result_formula"), 32, "结果计算方式", default="actual"),
            _text(data.get("operator"), 8, "比较方式", default="lte"),
            _number(data.get("threshold"), "阈值"),
            _state(data.get("builtin"), "内置标记", allowed=(0, 1), default=0),
        ]

    def list_templates(
        self, name: str | None = None, metric: str | None = None, page: Any = 1, size: Any = DEFAULT_PAGE_SIZE
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if name:
            conditions.append("name ILIKE %s")
            params.append(like_pattern(_text(name, 200, "模板名称")))
        if metric:
            conditions.append("metric = %s")
            params.append(_text(metric, 64, "核查类型"))
        return self._paged(TEMPLATE_COLUMNS, TEMPLATE_TABLE, conditions, params, "builtin DESC, id", page, size)

    def count_templates(self) -> int:
        return int(self._run(f"SELECT count(1) FROM {TEMPLATE_TABLE}", [], fetch="value") or 0)

    def get_template(self, template_id: Any) -> dict[str, Any]:
        row = self._run(
            f"SELECT {TEMPLATE_COLUMNS} FROM {TEMPLATE_TABLE} WHERE id = %s",
            [_record_id(template_id, "模板编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "规则模板不存在。")
        return row

    def create_template(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            f"""INSERT INTO {TEMPLATE_TABLE}
                (name, description, metric, dimension, level, config, expected_type,
                 result_formula, operator, threshold, builtin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {TEMPLATE_COLUMNS}""",
            self._template_values(data),
            fetch="one",
        )

    def update_template(self, template_id: Any, data: dict[str, Any]) -> dict[str, Any]:
        identifier = _record_id(template_id, "模板编号")
        merged = {**self.get_template(identifier), **data}
        row = self._run(
            f"""UPDATE {TEMPLATE_TABLE} SET
                name = %s, description = %s, metric = %s, dimension = %s, level = %s, config = %s,
                expected_type = %s, result_formula = %s, operator = %s, threshold = %s, builtin = %s,
                update_time = now()
                WHERE id = %s RETURNING {TEMPLATE_COLUMNS}""",
            [*self._template_values(merged), identifier],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "规则模板不存在。")
        return row

    def delete_template(self, template_id: Any) -> dict[str, Any]:
        row = self._run(
            f"DELETE FROM {TEMPLATE_TABLE} WHERE id = %s RETURNING id",
            [_record_id(template_id, "模板编号")],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "规则模板不存在。")
        return {"id": row["id"], "deleted": True}

    def task_run_counts(self) -> dict[str, int]:
        """Runs by status over the last day, for 质量健康 and the 调度 header."""
        rows = self._run(
            f"""SELECT status, count(1) AS total FROM {TASK_RUN_TABLE}
                WHERE start_time >= now() - interval '1 day' GROUP BY status""",
            [],
            fetch="all",
        )
        return {str(row["status"]): int(row["total"]) for row in rows or ()}

    # ----- executions -----------------------------------------------------------
    def start_execution(
        self,
        rule_id: Any = None,
        rule_name: str = "",
        trigger_type: str = "manual",
        schedule_id: Any = None,
        message: str = "",
    ) -> int:
        """Open an execution row in status 0 (运行中) and return its id."""
        row = self._run(
            f"""INSERT INTO {EXECUTION_TABLE}
                (rule_id, rule_name, schedule_id, trigger_type, status, message)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            [
                _optional_integer(rule_id, "规则编号"),
                _text(rule_name, 200, "规则名称"),
                _optional_integer(schedule_id, "调度编号"),
                _text(trigger_type, 16, "触发方式", required=True),
                0,
                _text(message, 2000, "执行说明", multiline=True),
            ],
            fetch="one",
        )
        return int(row["id"])

    def finish_execution(
        self, execution_id: Any, status: Any, elapsed_ms: Any = None, message: str = ""
    ) -> dict[str, Any]:
        """Close an execution; no row is ever left in status 0."""
        row = self._run(
            f"""UPDATE {EXECUTION_TABLE}
                SET status = %s, end_time = now(), elapsed_ms = %s, message = %s
                WHERE id = %s RETURNING {EXECUTION_COLUMNS}""",
            [
                _state(status, "执行状态", allowed=(0, 1, 2), default=1),
                _optional_integer(elapsed_ms, "耗时"),
                _text(message, 4000, "执行说明", multiline=True),
                _record_id(execution_id, "执行编号"),
            ],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "执行记录不存在。")
        return row

    def record_result(self, execution_id: Any, result_row: dict[str, Any]) -> dict[str, Any]:
        """Store one check result and return it as the frontend will see it."""
        data = result_row or {}
        return self._run(
            f"""INSERT INTO {RESULT_TABLE}
                (job_execution_id, rule_id, rule_name, metric_name, metric_dimension,
                 datasource_id, datasource_name, database_name, table_name, column_name,
                 rule_level, checked_count, actual_value, expected_value, expected_type,
                 result_formula, operator, threshold, score, state, invalidate_sql)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                RETURNING {RESULT_COLUMNS}""",
            [
                _record_id(execution_id, "执行编号"),
                _optional_integer(data.get("rule_id"), "规则编号"),
                _text(data.get("rule_name"), 200, "规则名称"),
                _optional_text(data.get("metric_name"), 64, "核查类型"),
                _optional_text(data.get("metric_dimension"), 32, "规则分类"),
                _optional_text(data.get("datasource_id"), 64, "数据源"),
                _optional_text(data.get("datasource_name"), 200, "数据源名称"),
                _optional_text(data.get("database_name"), 200, "数据库"),
                _optional_text(data.get("table_name"), 200, "数据表"),
                _optional_text(data.get("column_name"), 200, "核查字段"),
                _optional_text(data.get("rule_level"), 8, "规则级别"),
                _integer(data.get("checked_count"), "核查数量", minimum=0),
                _number(data.get("actual_value"), "不合规数量"),
                _optional_number(data.get("expected_value"), "期望值"),
                _optional_text(data.get("expected_type"), 32, "期望值类型"),
                _optional_text(data.get("result_formula"), 32, "结果计算方式"),
                _optional_text(data.get("operator"), 8, "比较方式"),
                _optional_number(data.get("threshold"), "阈值"),
                _optional_number(data.get("score"), "得分"),
                _state(data.get("state"), "校验结果", allowed=(1, 2), default=1),
                _text(data.get("invalidate_sql"), 20000, "错误数据 SQL", multiline=True),
            ],
            fetch="one",
        )

    def list_executions(
        self, status: Any = None, page: Any = 1, size: Any = DEFAULT_PAGE_SIZE
    ) -> dict[str, Any]:
        """Page over the execution log, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None and status != "":
            conditions.append("status = %s")
            params.append(_state(status, "执行状态", allowed=(0, 1, 2), default=1))
        return self._paged(
            EXECUTION_COLUMNS, EXECUTION_TABLE, conditions, params, "id DESC", page, size
        )

    def get_execution(self, execution_id: Any) -> dict[str, Any]:
        """One execution together with the results it produced."""
        identifier = _record_id(execution_id, "执行编号")
        row = self._run(
            f"SELECT {EXECUTION_COLUMNS} FROM {EXECUTION_TABLE} WHERE id = %s",
            [identifier],
            fetch="one",
        )
        if row is None:
            raise QualityStoreError(404, "执行记录不存在。")
        row["results"] = self.results_for_execution(identifier)
        return row

    def results_for_execution(self, execution_id: Any) -> list[dict[str, Any]]:
        """Results of one execution, for the 执行日志 drawer."""
        return self._run(
            f"SELECT {RESULT_COLUMNS} FROM {RESULT_TABLE} WHERE job_execution_id = %s ORDER BY id",
            [_record_id(execution_id, "执行编号")],
            fetch="all",
        )

    # ----- analysis -------------------------------------------------------------
    def latest_results(self, name: str | None = None) -> list[dict[str, Any]]:
        """The most recent result of every rule, for 质量统计分析.

        ``DISTINCT ON`` keeps one row per rule; a result without a rule id comes
        from a deleted rule and is left out of the per-rule view.
        """
        conditions = ["rule_id IS NOT NULL"]
        params: list[Any] = []
        if name:
            conditions.append("rule_name ILIKE %s")
            params.append(like_pattern(_text(name, 200, "规则名称")))
        where = " AND ".join(conditions)
        return self._run(
            f"""SELECT * FROM (
                    SELECT DISTINCT ON (rule_id) {RESULT_COLUMNS} FROM {RESULT_TABLE}
                    WHERE {where}
                    ORDER BY rule_id, check_time DESC, id DESC
                ) latest ORDER BY latest.check_time DESC, latest.id DESC""",
            params,
            fetch="all",
        )

    def recent_results(self, table_names: list[str], days: Any = 7, limit: Any = 200) -> list[dict[str, Any]]:
        """Results recorded for these tables within the last ``days`` days, newest first."""
        names = [_text(name, 200, "数据表") for name in table_names if str(name or "").strip()][:50]
        if not names:
            return []
        placeholders = ", ".join(["%s"] * len(names))
        return self._run(
            f"""SELECT {RESULT_COLUMNS} FROM {RESULT_TABLE}
                WHERE table_name IN ({placeholders}) AND check_time >= now() - (%s * interval '1 day')
                ORDER BY check_time DESC, id DESC LIMIT %s""",
            [*names, _bounded(days, "天数", default=7, minimum=1, maximum=365), _bounded(limit, "条数", default=200, minimum=1, maximum=1000)],
            fetch="all",
        )

    def dimension_error_counts(self) -> dict[str, int]:
        """Error totals per category for the 质量统计分析 sidebar tree."""
        rows = self._run(
            f"""SELECT metric_dimension, sum(actual_value) AS error_count FROM (
                    SELECT DISTINCT ON (rule_id) rule_id, metric_dimension, actual_value, check_time
                    FROM {RESULT_TABLE} WHERE rule_id IS NOT NULL
                    ORDER BY rule_id, check_time DESC, id DESC
                ) latest
                WHERE latest.metric_dimension IS NOT NULL
                GROUP BY metric_dimension""",
            [],
            fetch="all",
        )
        counts = {dimension: 0 for dimension in DIMENSION_LABELS}
        for row in rows or ():
            counts[str(row["metric_dimension"])] = int(row["error_count"] or 0)
        return counts

    def report(self, date: Any = None) -> dict[str, Any]:
        """Build 质量报告分析 for one local day.

        One query returns the day's latest result per rule; the grouping is done
        in Python because the three sections of the report are three different
        views over the very same handful of rows.
        """
        day = _day(date)
        # The requested day is a local day. A naive bound would be resolved in
        # the server's own TimeZone (Asia/Shanghai on this instance), shifting
        # the 24-hour window by the offset difference and moving results into
        # the wrong report, so both ends carry the local offset explicitly.
        start = _timestamp(dt.datetime.combine(day, dt.time.min), "报告起始时间")
        end = _timestamp(dt.datetime.combine(day, dt.time.min) + dt.timedelta(days=1), "报告结束时间")
        rows = self._run(
            f"""SELECT * FROM (
                    SELECT DISTINCT ON (rule_id, rule_name) {RESULT_COLUMNS} FROM {RESULT_TABLE}
                    WHERE check_time >= %s AND check_time < %s
                    ORDER BY rule_id, rule_name, check_time DESC, id DESC
                ) latest ORDER BY latest.check_time DESC, latest.id DESC""",
            [start, end],
            fetch="all",
        ) or []

        datasource_errors: dict[tuple[str, str], float] = {}
        rule_errors: dict[tuple[str, str, str], float] = {}
        sections: dict[str, list[dict[str, Any]]] = {}
        total_checked = 0.0
        total_errors = 0.0
        for row in rows:
            level = str(row.get("rule_level") or "")
            dimension = str(row.get("metric_dimension") or "")
            datasource = str(row.get("datasource_name") or "")
            errors = float(row.get("actual_value") or 0)
            checked = float(row.get("checked_count") or 0)
            total_checked += checked
            total_errors += errors
            datasource_errors[(datasource, level)] = (
                datasource_errors.get((datasource, level), 0.0) + errors
            )
            rule_errors[(dimension, str(row.get("rule_name") or ""), level)] = (
                rule_errors.get((dimension, str(row.get("rule_name") or ""), level), 0.0) + errors
            )
            sections.setdefault(dimension, []).append(
                {
                    "rule_name": row.get("rule_name") or "",
                    "datasource_name": datasource,
                    "table_name": row.get("table_name") or "",
                    "column_name": row.get("column_name"),
                    "checked_count": plain_number(checked),
                    "actual_value": plain_number(errors),
                    "score": row.get("score"),
                }
            )
        ordered = [key for key in DIMENSION_LABELS if key in sections]
        ordered += [key for key in sections if key not in DIMENSION_LABELS]
        score = quality_score(total_checked, total_errors)
        return {
            "date": day.isoformat(),
            "datasource_errors": [
                {
                    "datasource_name": datasource,
                    "level": level,
                    "level_label": LEVEL_LABELS.get(level, level),
                    "error_count": plain_number(count),
                }
                for (datasource, level), count in sorted(
                    datasource_errors.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "rule_errors": [
                {
                    "dimension": dimension,
                    "dimension_label": DIMENSION_LABELS.get(dimension, dimension),
                    "rule_name": rule_name,
                    "level": level,
                    "level_label": LEVEL_LABELS.get(level, level),
                    "error_count": plain_number(count),
                }
                for (dimension, rule_name, level), count in sorted(
                    rule_errors.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "dimension_sections": [
                {
                    "dimension": dimension,
                    "dimension_label": DIMENSION_LABELS.get(dimension, dimension),
                    "rows": sections[dimension],
                }
                for dimension in ordered
            ],
            "total_checked": plain_number(total_checked),
            "total_errors": plain_number(total_errors),
            "score": score,
            "level_label": quality_band(score),
        }
