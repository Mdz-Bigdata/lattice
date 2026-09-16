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

"""DSN parsing, JSON-safe rows and parameterised SQL of the quality store.

Every test here runs against a fake connection/cursor: the store's contract is
"no value is ever formatted into a statement", and that is a property of the SQL
text plus the bound parameters, not of a live server. The real database is
exercised separately by the bootstrap check documented in the module.
"""

import datetime as dt
import decimal

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from webapi import quality_store
from webapi.quality_store import (
    DDL,
    DIMENSION_LABELS,
    RESULT_COLUMNS,
    RULE_COLUMNS,
    SCHEMA,
    TABLES,
    QualityStore,
    QualityStoreError,
    json_safe,
    parse_dsn,
    quality_band,
    quality_score,
)

DSN = "postgresql+asyncpg://postgres:postgres@localhost:5432/blog_converter"
RULE_FIELDS = RULE_COLUMNS.split(", ")
RESULT_FIELDS = RESULT_COLUMNS.split(", ")
INJECTION = "a'; DROP TABLE public.orders; --"


class FakeColumn:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, database):
        self.database = database
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.database.calls.append((sql, params))
        if self.database.failure is not None:
            raise self.database.failure
        columns, rows = self.database.take()
        self.description = [FakeColumn(name) for name in columns] if columns else None
        self.rows = [tuple(row) for row in rows]

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, database):
        self.database = database
        self.closed = False

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        self.closed = True


class FakeDatabase:
    """Records every statement and hands back scripted result sets."""

    def __init__(self):
        self.calls = []
        self.results = []
        self.connections = []
        self.kwargs = {}
        self.conninfo = ""
        self.failure = None
        self.connect_error = None

    def queue(self, columns, rows):
        self.results.append((columns, rows))

    def take(self):
        return self.results.pop(0) if self.results else ((), [])

    def connect(self, conninfo, **kwargs):
        if self.connect_error is not None:
            raise self.connect_error
        self.conninfo = conninfo
        self.kwargs = kwargs
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    @property
    def statements(self):
        return [sql for sql, _ in self.calls]


class FakePool:
    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.database = FakeDatabase()
        self.closed = False
        self.handed_out = 0

    def connection(self):
        pool = self

        class Borrow:
            def __enter__(self):
                pool.handed_out += 1
                return FakeConnection(pool.database)

            def __exit__(self, *exc_info):
                return False

        return Borrow()

    def close(self):
        self.closed = True


@pytest.fixture
def database(monkeypatch):
    fake = FakeDatabase()
    # Force the lock path so the test is identical whether or not psycopg_pool
    # happens to be installed in the environment running it.
    monkeypatch.setattr(quality_store, "ConnectionPool", None)
    monkeypatch.setattr(quality_store.psycopg, "connect", fake.connect)
    return fake


@pytest.fixture
def store(database):
    return QualityStore(DSN)


def assert_parameterised(database):
    """No statement may carry a value; every ``%s`` must have one parameter."""
    for sql, params in database.calls:
        values = tuple(params or ())
        assert sql.count("%s") == len(values), sql
        # A statement built by this store never contains a literal at all; a
        # short bound value may legitimately read as part of a column name.
        assert "'" not in sql, sql
        for value in values:
            if isinstance(value, str) and len(value) > 8:
                assert value not in sql, sql


def rule_row(**overrides):
    values = {
        "id": 7,
        "name": "订单号唯一",
        "metric": "column_duplicate",
        "dimension": "uniqueness",
        "level": "high",
        "datasource_id": "quality-postgres",
        "datasource_name": "业务库 PostgreSQL",
        "schema_name": "public",
        "table_name": "orders",
        "column_name": "order_id",
        "config": {"filter": ""},
        "expected_type": "fix_value",
        "result_formula": "actual",
        "operator": "lte",
        "threshold": decimal.Decimal("0.0000"),
        "state": 1,
        "comment": "",
        "create_time": dt.datetime(2026, 9, 12, 8, 30),
        "update_time": dt.datetime(2026, 9, 12, 8, 30),
    }
    values.update(overrides)
    return tuple(values[name] for name in RULE_FIELDS)


def result_row(**overrides):
    values = {
        "id": 1,
        "job_execution_id": 11,
        "rule_id": 7,
        "rule_name": "订单号唯一",
        "metric_name": "column_duplicate",
        "metric_dimension": "uniqueness",
        "datasource_id": "quality-postgres",
        "datasource_name": "业务库 PostgreSQL",
        "database_name": "blog_converter",
        "table_name": "orders",
        "column_name": "order_id",
        "rule_level": "high",
        "checked_count": decimal.Decimal("100"),
        "actual_value": decimal.Decimal("5"),
        "expected_value": decimal.Decimal("0"),
        "expected_type": "fix_value",
        "result_formula": "actual",
        "operator": "lte",
        "threshold": decimal.Decimal("0"),
        "score": decimal.Decimal("95.00"),
        "state": 2,
        "invalidate_sql": "SELECT 1",
        "check_time": dt.datetime(2026, 9, 12, 9, 0),
    }
    values.update(overrides)
    return tuple(values[name] for name in RESULT_FIELDS)


# ----- parse_dsn ---------------------------------------------------------------
def test_parse_dsn_strips_the_asyncpg_driver_suffix():
    assert conninfo_to_dict(parse_dsn(DSN)) == {
        "host": "localhost",
        "port": "5432",
        "user": "postgres",
        "password": "postgres",
        "dbname": "blog_converter",
    }


@pytest.mark.parametrize(
    "url",
    [
        "postgres://postgres:postgres@localhost:5432/blog_converter",
        "postgresql://postgres:postgres@localhost:5432/blog_converter",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/blog_converter",
        "POSTGRESQL+ASYNCPG://postgres:postgres@localhost:5432/blog_converter",
    ],
)
def test_parse_dsn_accepts_every_postgres_spelling(url):
    assert conninfo_to_dict(parse_dsn(url))["dbname"] == "blog_converter"


def test_parse_dsn_decodes_a_percent_encoded_password():
    parsed = conninfo_to_dict(parse_dsn("postgresql+asyncpg://us%40er:p%40ss%2Fw%3Ard@h/db"))
    assert parsed["user"] == "us@er"
    assert parsed["password"] == "p@ss/w:rd"


def test_parse_dsn_without_port_or_password():
    parsed = conninfo_to_dict(parse_dsn("postgresql+asyncpg://postgres@localhost/blog_converter"))
    assert "port" not in parsed
    assert "password" not in parsed
    assert parsed["host"] == "localhost"


def test_parse_dsn_keeps_query_options():
    parsed = conninfo_to_dict(parse_dsn(DSN + "?sslmode=require&application_name=lattice"))
    assert parsed["sslmode"] == "require"
    assert parsed["application_name"] == "lattice"


@pytest.mark.parametrize(
    "url",
    [
        "mysql+pymysql://root@localhost:3306/db",
        "sqlite:///quality.db",
        "clickhouse://localhost:8123/default",
        "",
        "   ",
        None,
        "postgresql://localhost:notaport/db",
        "postgresql://localhost/db?bogus_option=1",
    ],
)
def test_parse_dsn_rejects_what_psycopg_cannot_use(url):
    with pytest.raises(QualityStoreError) as raised:
        parse_dsn(url)
    assert raised.value.status_code == 400
    assert str(raised.value).endswith("。")


def test_store_reads_the_dsn_from_the_environment(monkeypatch):
    monkeypatch.setenv("LATTICE_QUALITY_DSN", "postgresql+asyncpg://u:p@127.0.0.1:6543/lattice")
    store = QualityStore()
    assert store.database == "lattice"
    assert "host=127.0.0.1" in store.conninfo
    assert store.schema == SCHEMA


def test_store_falls_back_to_the_default_dsn_without_the_variable(monkeypatch):
    """Only ``LATTICE_QUALITY_DSN`` is read; an unset variable uses the default."""
    monkeypatch.delenv("LATTICE_QUALITY_DSN", raising=False)
    assert QualityStore().database == "blog_converter"
    monkeypatch.setenv("LATTICE_QUALITY_DSN", "postgresql+asyncpg://u:p@127.0.0.1:6543/current")
    assert QualityStore().database == "current"


# ----- JSON-safe rows ----------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (decimal.Decimal("12"), 12),
        (decimal.Decimal("1318635.0000"), 1318635),
        (decimal.Decimal("95.25"), 95.25),
        (decimal.Decimal("NaN"), None),
        (dt.datetime(2026, 9, 12, 9, 30), "2026-09-12T09:30:00"),
        (dt.date(2026, 9, 12), "2026-09-12"),
        (float("inf"), None),
        (None, None),
        (True, True),
        ("已启用", "已启用"),
        (3, 3),
    ],
)
def test_json_safe_values(value, expected):
    assert json_safe(value) == expected
    assert type(json_safe(value)) is type(expected)


def test_json_safe_walks_containers():
    value = {"counts": [decimal.Decimal("2"), decimal.Decimal("2.5")], "at": dt.date(2026, 1, 1)}
    assert json_safe(value) == {"counts": [2, 2.5], "at": "2026-01-01"}


def test_rows_come_back_json_safe(store, database):
    database.queue(RULE_FIELDS, [rule_row()])
    rule = store.get_rule(7)
    assert rule["threshold"] == 0 and isinstance(rule["threshold"], int)
    assert rule["create_time"] == "2026-09-12T08:30:00"
    assert rule["config"] == {"filter": ""}
    assert rule["column_name"] == "order_id"


def test_score_and_band():
    assert quality_score(100, 5) == 95.0
    assert quality_score(0, 0) == 100.0
    assert quality_score(3, 1) == 66.67
    assert [quality_band(score) for score in (100, 80, 79, 60, 40, 20, 19)] == [
        "优秀",
        "优秀",
        "良好",
        "良好",
        "中等",
        "合格",
        "不合格",
    ]


# ----- bootstrap and health ----------------------------------------------------
def test_bootstrap_is_idempotent_ddl(store, database):
    store.bootstrap()
    store.bootstrap()
    assert len(database.calls) == 2 * len(DDL)
    for sql, params in database.calls:
        assert not params
        assert "IF NOT EXISTS" in sql
    created = " ".join(database.statements)
    for table in TABLES:
        assert f"{SCHEMA}.{table}" in created
    assert created.count("CREATE INDEX IF NOT EXISTS") == 2 * 5
    assert created.count("ADD COLUMN IF NOT EXISTS") == 2 * 6


def test_bootstrap_never_raises_when_the_database_is_down(store, database):
    database.connect_error = psycopg.OperationalError("connection refused")
    store.bootstrap()
    health = store.healthy()
    assert health["ok"] is False
    assert health["database"] == "blog_converter"
    assert health["schema"] == SCHEMA
    assert health["detail"].endswith("。")


def test_healthy_reports_missing_tables(store, database):
    database.queue(["table_name"], [("dv_rule",), ("dv_job_schedule",)])
    health = store.healthy()
    assert health["ok"] is False
    assert "dv_job_execution" in health["detail"]
    assert database.calls[0][1] == (SCHEMA,)


def test_healthy_ok_when_every_table_is_present(store, database):
    database.queue(["table_name"], [(name,) for name in TABLES])
    assert store.healthy() == {
        "ok": True,
        "detail": "质量元数据库连接正常。",
        "database": "blog_converter",
        "schema": SCHEMA,
    }
    assert "information_schema.tables" in database.statements[0]


def test_driver_failures_become_chinese_store_errors(store, database):
    database.failure = psycopg.errors.UndefinedTable("relation does not exist")
    with pytest.raises(QualityStoreError) as raised:
        store.enabled_rules()
    assert raised.value.status_code == 500
    assert str(raised.value).startswith("质量元数据库操作失败：")


def test_connection_uses_bounded_timeouts(store, database):
    database.queue(RULE_FIELDS, [])
    store.enabled_rules()
    assert database.conninfo == store.conninfo
    assert database.kwargs["connect_timeout"] == quality_store.CONNECT_TIMEOUT
    assert database.kwargs["autocommit"] is True
    assert "statement_timeout=30000" in database.kwargs["options"]
    assert database.connections[0].closed is True


def test_pool_is_used_when_psycopg_pool_is_installed(monkeypatch):
    monkeypatch.setattr(quality_store, "ConnectionPool", FakePool)

    def refuse(*args, **kwargs):
        raise AssertionError("the pool must be used instead of a direct connection")

    monkeypatch.setattr(quality_store.psycopg, "connect", refuse)
    store = QualityStore(DSN)
    store.bootstrap()
    pool = store._pool
    assert isinstance(pool, FakePool)
    assert pool.kwargs["max_size"] == quality_store.POOL_MAX_SIZE
    assert pool.handed_out == 1
    assert len(pool.database.calls) == len(DDL)
    store.close()
    assert pool.closed is True


# ----- rules -------------------------------------------------------------------
def test_list_rules_binds_every_filter(store, database):
    database.queue(["count"], [(3,)])
    database.queue(RULE_FIELDS, [rule_row()])
    page = store.list_rules(dimension="uniqueness", name=INJECTION, page=2, size=5)
    assert page["total"] == 3
    assert page["items"][0]["name"] == "订单号唯一"
    count_sql, count_params = database.calls[0]
    page_sql, page_params = database.calls[1]
    assert "dimension = %s" in count_sql and "name ILIKE %s" in count_sql
    assert count_params == ("uniqueness", f"%{INJECTION}%")
    assert page_params == ("uniqueness", f"%{INJECTION}%", 5, 5)
    assert page_sql.rstrip().endswith("LIMIT %s OFFSET %s")
    assert_parameterised(database)


def test_list_rules_escapes_like_wildcards(store, database):
    database.queue(["count"], [(0,)])
    database.queue(RULE_FIELDS, [])
    store.list_rules(name="100%_rate")
    assert database.calls[0][1] == ("%100\\%\\_rate%",)


def test_list_rules_clamps_paging(store, database):
    database.queue(["count"], [(0,)])
    database.queue(RULE_FIELDS, [])
    store.list_rules(page=0, size=5000)
    assert database.calls[1][1] == (quality_store.MAX_PAGE_SIZE, 0)


def test_create_rule_binds_values_and_returns_the_row(store, database):
    database.queue(RULE_FIELDS, [rule_row(name=INJECTION)])
    created = store.create_rule(
        {
            "name": INJECTION,
            "metric": "column_duplicate",
            "dimension": "uniqueness",
            "level": "high",
            "datasource_id": "quality-postgres",
            "datasource_name": "业务库 PostgreSQL",
            "schema_name": "public",
            "table_name": "orders",
            "column_name": "order_id",
            "config": {"filter": "status = '已完成'"},
            "threshold": 0,
            "state": 1,
            "comment": "上线前必须为 0。",
        }
    )
    sql, params = database.calls[0]
    assert sql.count("%s") == 16
    assert params[0] == INJECTION
    assert params[9].obj == {"filter": "status = '已完成'"}
    assert created["id"] == 7
    assert_parameterised(database)


def test_create_rule_rejects_an_empty_name(store, database):
    with pytest.raises(QualityStoreError) as raised:
        store.create_rule({"name": "  ", "metric": "column_null", "dimension": "completeness"})
    assert raised.value.status_code == 400
    assert str(raised.value) == "规则名称不能为空。"
    assert database.calls == []


def test_update_rule_keeps_untouched_columns(store, database):
    database.queue(RULE_FIELDS, [rule_row()])
    database.queue(RULE_FIELDS, [rule_row(state=0)])
    updated = store.update_rule(7, {"state": 0})
    _, params = database.calls[1]
    assert params[0] == "订单号唯一"
    assert params[1] == "column_duplicate"
    assert params[14] == 0
    assert params[-1] == 7
    assert updated["state"] == 0
    assert_parameterised(database)


def test_missing_rows_raise_404(store, database):
    database.queue(RULE_FIELDS, [])
    with pytest.raises(QualityStoreError) as raised:
        store.get_rule(404)
    assert raised.value.status_code == 404
    assert str(raised.value) == "核查规则不存在。"


def test_delete_rule_reports_a_missing_rule(store, database):
    database.queue(["id"], [])
    with pytest.raises(QualityStoreError) as raised:
        store.delete_rule(9)
    assert raised.value.status_code == 404


def test_delete_rule_returns_the_deleted_id(store, database):
    database.queue(["id"], [(9,)])
    assert store.delete_rule(9) == {"id": 9, "deleted": True}
    assert database.calls[0][1] == (9,)


@pytest.mark.parametrize("bad", ["", None, 0, -1, "abc", 1.5e400])
def test_invalid_identifiers_are_rejected_before_sql(store, database, bad):
    with pytest.raises(QualityStoreError) as raised:
        store.get_rule(bad)
    assert raised.value.status_code == 400
    assert database.calls == []


def test_enabled_rules_binds_the_state(store, database):
    database.queue(RULE_FIELDS, [rule_row()])
    rules = store.enabled_rules()
    assert [rule["id"] for rule in rules] == [7]
    assert database.calls[0] == (
        f"SELECT {RULE_COLUMNS} FROM {SCHEMA}.dv_rule WHERE state = %s ORDER BY id",
        (1,),
    )


# ----- schedules ---------------------------------------------------------------
def test_create_schedule_applies_the_documented_defaults(store, database):
    database.queue(["id"], [(1,)])
    store.create_schedule({"name": "每日质量巡检", "cron_expression": "0 0 12 * * ?"})
    sql, params = database.calls[0]
    assert params == ("每日质量巡检", "QualityTask", "run", "", "0 0 12 * * ?", 0, 0, 60, "skip", 10)
    assert sql.count("%s") == 10
    assert_parameterised(database)


def test_set_schedule_state_binds_the_next_fire_time(store, database):
    database.queue(["id"], [(1,)])
    store.set_schedule_state(1, 1, "2026-09-12T12:00:00")
    _, params = database.calls[0]
    # The naive input keeps its wall-clock reading but is pinned to the local
    # offset, so the timestamptz column stores the instant that was meant.
    assert params == (1, dt.datetime(2026, 9, 12, 12, 0).astimezone(), 1)


def test_mark_fired_binds_both_timestamps(store, database):
    database.queue(["id"], [(1,)])
    fired = dt.datetime(2026, 9, 12, 12, 0)
    store.mark_fired(1, fired, fired + dt.timedelta(days=1))
    _, params = database.calls[0]
    assert params == (
        fired.astimezone(),
        (fired + dt.timedelta(days=1)).astimezone(),
        1,
    )


def test_running_schedules_filters_on_state(store, database):
    database.queue(["id", "name"], [(1, "每日质量巡检")])
    assert store.running_schedules() == [{"id": 1, "name": "每日质量巡检"}]
    assert database.calls[0][1] == (1,)


def test_schedule_state_must_be_zero_or_one(store, database):
    with pytest.raises(QualityStoreError) as raised:
        store.set_schedule_state(1, 7)
    assert raised.value.status_code == 400
    assert str(raised.value).endswith("。")


# ----- executions --------------------------------------------------------------
def test_start_execution_opens_a_running_row(store, database):
    database.queue(["id"], [(11,)])
    assert store.start_execution(7, "订单号唯一", "manual") == 11
    sql, params = database.calls[0]
    assert params == (7, "订单号唯一", None, "manual", 0, "")
    assert "INSERT INTO lattice_quality.dv_job_execution" in sql
    assert_parameterised(database)


def test_finish_execution_binds_status_and_message(store, database):
    database.queue(["id", "status"], [(11, 2)])
    row = store.finish_execution(11, 2, 1234, "连接数据源失败。")
    assert row == {"id": 11, "status": 2}
    _, params = database.calls[0]
    assert params == (2, 1234, "连接数据源失败。", 11)


def test_record_result_binds_every_column(store, database):
    database.queue(RESULT_FIELDS, [result_row()])
    stored = store.record_result(
        11,
        {
            "rule_id": 7,
            "rule_name": "订单号唯一",
            "metric_name": "column_duplicate",
            "metric_dimension": "uniqueness",
            "datasource_id": "quality-postgres",
            "datasource_name": "业务库 PostgreSQL",
            "database_name": "blog_converter",
            "table_name": "orders",
            "column_name": "order_id",
            "rule_level": "high",
            "checked_count": 100,
            "actual_value": 5,
            "expected_value": 0,
            "expected_type": "fix_value",
            "result_formula": "actual",
            "operator": "lte",
            "threshold": 0,
            "score": 95.0,
            "state": 2,
            "invalidate_sql": "SELECT * FROM public.orders WHERE order_id IS NULL",
        },
    )
    sql, params = database.calls[0]
    assert sql.count("%s") == 21
    assert params[0] == 11 and params[11] == 100 and params[19] == 2
    assert stored["checked_count"] == 100
    assert stored["score"] == 95.0
    assert stored["check_time"] == "2026-09-12T09:00:00"
    assert_parameterised(database)


def test_list_executions_filters_on_status_zero(store, database):
    database.queue(["count"], [(1,)])
    database.queue(["id", "status"], [(11, 0)])
    page = store.list_executions(status=0, page=1, size=20)
    assert page == {"items": [{"id": 11, "status": 0}], "total": 1}
    assert database.calls[0][1] == (0,)
    assert database.calls[1][1] == (0, 20, 0)


def test_get_execution_attaches_its_results(store, database):
    database.queue(["id", "rule_name"], [(11, "订单号唯一")])
    database.queue(RESULT_FIELDS, [result_row()])
    execution = store.get_execution(11)
    assert execution["id"] == 11
    assert execution["results"][0]["actual_value"] == 5
    assert database.calls[0][1] == (11,)
    assert database.calls[1][1] == (11,)


# ----- analysis ----------------------------------------------------------------
def test_latest_results_keeps_one_row_per_rule(store, database):
    database.queue(RESULT_FIELDS, [result_row()])
    rows = store.latest_results(name=INJECTION)
    sql, params = database.calls[0]
    assert "DISTINCT ON (rule_id)" in sql
    assert "rule_name ILIKE %s" in sql
    assert params == (f"%{INJECTION}%",)
    assert rows[0]["rule_name"] == "订单号唯一"
    assert_parameterised(database)


def test_dimension_error_counts_cover_all_six_categories(store, database):
    database.queue(
        ["metric_dimension", "error_count"],
        [("uniqueness", decimal.Decimal("1318635")), ("timeliness", decimal.Decimal("12"))],
    )
    counts = store.dimension_error_counts()
    assert set(counts) == set(DIMENSION_LABELS)
    assert counts["uniqueness"] == 1318635
    assert counts["timeliness"] == 12
    assert counts["accuracy"] == 0


def test_report_aggregates_the_day(store, database):
    database.queue(
        RESULT_FIELDS,
        [
            result_row(),
            result_row(
                id=2,
                rule_id=8,
                rule_name="下单时间新鲜度",
                metric_name="table_freshness",
                metric_dimension="timeliness",
                rule_level="low",
                datasource_name="业务库 PostgreSQL",
                checked_count=decimal.Decimal("100"),
                actual_value=decimal.Decimal("15"),
            ),
        ],
    )
    report = store.report("2026-09-12")
    sql, params = database.calls[0]
    assert "check_time >= %s AND check_time < %s" in sql
    # Both ends carry the local offset so the window is the requested LOCAL day,
    # not the same wall-clock reading in the server's Asia/Shanghai zone.
    assert params == (
        dt.datetime(2026, 9, 12).astimezone(),
        dt.datetime(2026, 9, 13).astimezone(),
    )
    assert report["date"] == "2026-09-12"
    assert report["total_checked"] == 200
    assert report["total_errors"] == 20
    assert report["score"] == 90.0
    assert report["level_label"] == "优秀"
    assert report["datasource_errors"] == [
        {
            "datasource_name": "业务库 PostgreSQL",
            "level": "low",
            "level_label": "低",
            "error_count": 15,
        },
        {
            "datasource_name": "业务库 PostgreSQL",
            "level": "high",
            "level_label": "高",
            "error_count": 5,
        },
    ]
    assert [item["rule_name"] for item in report["rule_errors"]] == ["下单时间新鲜度", "订单号唯一"]
    assert [section["dimension"] for section in report["dimension_sections"]] == [
        "uniqueness",
        "timeliness",
    ]
    assert report["dimension_sections"][0]["dimension_label"] == "唯一性校验"
    assert report["dimension_sections"][0]["rows"] == [
        {
            "rule_name": "订单号唯一",
            "datasource_name": "业务库 PostgreSQL",
            "table_name": "orders",
            "column_name": "order_id",
            "checked_count": 100,
            "actual_value": 5,
            "score": 95.0,
        }
    ]


def test_report_of_an_empty_day_scores_full_marks(store, database):
    database.queue(RESULT_FIELDS, [])
    report = store.report(dt.date(2026, 9, 12))
    assert report["total_checked"] == 0
    assert report["score"] == 100.0
    assert report["level_label"] == "优秀"
    assert report["dimension_sections"] == []


def test_report_rejects_a_malformed_date(store, database):
    with pytest.raises(QualityStoreError) as raised:
        store.report("2026/09/12")
    assert raised.value.status_code == 400
    assert str(raised.value) == "日期格式须为 YYYY-MM-DD。"
    assert database.calls == []


def test_no_statement_carries_a_value(store, database):
    """One sweep over the whole surface: SQL text never contains user input."""
    for columns, rows in [
        (["count"], [(1,)]),                                    # list_rules total
        (RULE_FIELDS, [rule_row()]),                            # list_rules page
        (RULE_FIELDS, [rule_row()]),                            # enabled_rules
        (["count"], [(1,)]),                                    # list_schedules total
        (["id", "name"], [(1, "每日质量巡检")]),                  # list_schedules page
        (["id"], [(1,)]),                                       # create_schedule
        (["id"], [(11,)]),                                      # start_execution
        (RESULT_FIELDS, [result_row()]),                        # record_result
        (RESULT_FIELDS, [result_row()]),                        # latest_results
        (["metric_dimension", "error_count"], []),              # dimension_error_counts
        (RESULT_FIELDS, []),                                    # report
    ]:
        database.queue(columns, rows)
    store.list_rules(dimension=INJECTION, name=INJECTION)
    store.enabled_rules()
    store.list_schedules(name=INJECTION)
    store.create_schedule({"name": INJECTION, "cron_expression": "0 0 12 * * ?"})
    store.start_execution(1, INJECTION, "schedule", 3)
    store.record_result(11, {"rule_name": INJECTION, "invalidate_sql": INJECTION, "state": 2})
    store.latest_results(name=INJECTION)
    store.dimension_error_counts()
    store.report("2026-09-12")
    assert_parameterised(database)
    assert "DROP TABLE" not in " ".join(database.statements)


def test_a_misconfigured_dsn_does_not_stop_the_app(database):
    store = QualityStore("mysql+pymysql://root@localhost:3306/db")
    store.bootstrap()
    health = store.healthy()
    assert health["ok"] is False
    assert health["detail"] == "质量元数据库仅支持 PostgreSQL 连接串。"
    assert health["database"] == ""
    assert database.calls == []
    with pytest.raises(QualityStoreError) as raised:
        store.enabled_rules()
    assert raised.value.status_code == 400


def test_naive_times_are_stored_as_the_intended_instant():
    """A naive datetime in a timestamptz column would otherwise be read in the
    server's timezone (this database runs Asia/Shanghai), silently shifting a
    scheduled firing by the offset difference."""
    import datetime as dt

    from webapi.quality_store import _timestamp

    naive = dt.datetime(2026, 9, 12, 0, 23, 0)
    stored = _timestamp(naive, "下次执行时间")
    assert stored is not None and stored.tzinfo is not None
    assert stored.utcoffset() == naive.astimezone().utcoffset()
    assert stored.replace(tzinfo=None) == naive

    aware = dt.datetime(2026, 9, 12, 0, 23, 0, tzinfo=dt.timezone.utc)
    assert _timestamp(aware, "下次执行时间") == aware
    assert _timestamp("2026-09-12T00:23:00", "下次执行时间").tzinfo is not None
    assert _timestamp(None, "下次执行时间") is None


def test_report_date_bounds_are_rejected_as_chinese_errors():
    """date.max has no following day, so building the window raised OverflowError,
    which guarded() does not catch and Starlette turned into a plain-text 500."""
    import datetime as dt

    from webapi.quality_store import QualityStoreError, _day

    assert _day("2026-09-12") == dt.date(2026, 9, 12)
    assert _day("0001-01-01") == dt.date.min
    for bad in ("9999-12-31", "notadate", "2026-02-31", ""):
        if bad == "":
            continue
        with pytest.raises(QualityStoreError) as info:
            _day(bad)
        assert info.value.status_code == 400
        assert str(info.value).endswith("。")


def test_task_runs_are_parameterised_and_bounded(store, database):
    database.queue(["id"], [(7,)])
    run_id = store.start_task_run(
        schedule_id=3, schedule_name="每日巡检", task="QualityTask.run", task_label="质量核查全量执行",
        trigger_type="retry", attempt=2, planned_time="2026-09-12T12:00:00", message=INJECTION,
    )
    assert run_id == 7
    sql, params = database.calls[0]
    assert params[:7] == (3, "每日巡检", "QualityTask.run", "质量核查全量执行", "retry", 2, "running")
    assert params[8] == INJECTION and INJECTION not in sql
    database.queue(["id"], [(7,)])
    store.finish_task_run(7, "success", 120, "共 3 条规则", {"total": 3})
    sql, params = database.calls[1]
    assert params[0] == "success" and params[1] == 120 and params[4] == 7
    with pytest.raises(QualityStoreError, match="执行状态"):
        store.finish_task_run(7, "running")
    with pytest.raises(QualityStoreError, match="补跑策略"):
        store.create_schedule({"name": "x", "cron_expression": "0 0 12 * * ?", "misfire_policy": "later"})
    with pytest.raises(QualityStoreError, match="失败重试次数"):
        store.create_schedule({"name": "x", "cron_expression": "0 0 12 * * ?", "retry_limit": 99})
    assert_parameterised(database)


def test_task_run_listing_filters_by_schedule_status_and_task(store, database):
    database.queue(["count"], [(1,)])
    database.queue(["id"], [(1,)])
    store.list_task_runs(schedule_id=3, status="failed", task="MetadataTask.ingest", page=2, size=10)
    count_sql, count_params = database.calls[0]
    assert count_params == (3, "failed", "MetadataTask.ingest")
    assert "schedule_id = %s AND status = %s AND task = %s" in count_sql
    _, params = database.calls[1]
    assert params[-2:] == (10, 10)
    assert_parameterised(database)
