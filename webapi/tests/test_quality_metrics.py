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

"""The metric catalog and the per-dialect SQL it generates."""

import json

import pytest

from webapi.connectors import CONNECTORS, guard_sql, iter_dialects
from webapi.quality_metrics import (
    DIALECTS,
    DIMENSIONS,
    METRICS,
    SCRIPTS,
    MetricError,
    build,
    catalog,
    make_context,
    validate_config,
)

# One valid configuration per metric; `day` keeps the freshness case legal on Hive.
SAMPLE_CONFIG = {
    "column_duplicate": {},
    "column_null": {},
    "column_blank": {},
    "column_match_regex": {"regexp": "^[0-9]{6}$"},
    "column_length": {"comparator": "<=", "length": 10},
    "column_value_between": {"min": 1, "max": 100},
    "column_not_in_enums": {"enum_list": "男,女"},
    "column_not_in_reference": {
        "reference_schema": "dim",
        "reference_table": "city",
        "reference_column": "code",
    },
    "table_freshness": {"interval_value": 2, "interval_unit": "day"},
}
METRIC_IDS = tuple(METRICS)
INJECTIONS = (
    'a"; DROP TABLE x; --',
    "a`; DROP TABLE x; --",
    "a'; DROP TABLE x",
    "orders; SELECT 1",
    "col\nname",
    "col\x00name",
)


def sql_for(dialect, metric_id, *, config=None, schema="shop", table="orders",
            column="email", source_type=""):
    merged = dict(SAMPLE_CONFIG[metric_id])
    merged.update(config or {})
    return build(
        metric_id,
        make_context(dialect, schema, table, column, merged, source_type=source_type),
    )


# ----- generation over every metric and dialect ---------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("metric_id", METRIC_IDS)
def test_every_metric_and_dialect_generates_guarded_read_only_sql(dialect, metric_id):
    result = sql_for(dialect, metric_id)
    quote = SCRIPTS[dialect].quote
    table = f"{quote}shop{quote}.{quote}orders{quote}"
    for statement in (result.actual_sql, result.total_sql, result.invalidate_sql):
        guard_sql(statement, dialect)
        assert table in statement
    assert result.actual_sql.startswith("SELECT count(1) AS actual_value FROM (")
    assert result.actual_sql.endswith(") t")
    assert result.invalidate_sql in result.actual_sql
    assert result.total_sql == f"SELECT count(1) AS checked_count FROM {table}"
    assert result.predicate
    assert result.scan_limited is False


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("metric_id", METRIC_IDS)
def test_filter_is_guarded_and_anded_last(dialect, metric_id):
    result = sql_for(dialect, metric_id, config={"filter": "amount > 0"})
    for statement in (result.actual_sql, result.total_sql, result.invalidate_sql):
        guard_sql(statement, dialect)
    assert result.total_sql.endswith(" WHERE (amount > 0)")
    if metric_id == "column_duplicate":
        # The aggregate has no WHERE left to extend, so the filter precedes GROUP BY.
        assert " WHERE (amount > 0) GROUP BY " in result.invalidate_sql
    else:
        assert result.invalidate_sql.endswith(" AND (amount > 0)")
        assert result.invalidate_sql.index(result.predicate) < result.invalidate_sql.index(
            "(amount > 0)"
        )


def test_dialect_scripts_cover_every_connector_and_quote_like_it():
    assert set(DIALECTS) == set(iter_dialects())
    for connector in CONNECTORS.values():
        assert SCRIPTS[connector.dialect].quote == connector.identifier_quote


@pytest.mark.parametrize(
    "dialect, table, column",
    [
        ("postgres", '"shop"."orders"', '"email"'),
        ("duckdb", '"shop"."orders"', '"email"'),
        ("mysql", "`shop`.`orders`", "`email`"),
        ("starrocks", "`shop`.`orders`", "`email`"),
        ("doris", "`shop`.`orders`", "`email`"),
        ("clickhouse", "`shop`.`orders`", "`email`"),
        ("hive", "`shop`.`orders`", "`email`"),
    ],
)
def test_identifiers_are_quoted_per_dialect(dialect, table, column):
    result = sql_for(dialect, "column_null")
    assert result.invalidate_sql == f"SELECT * FROM {table} WHERE ({column} IS NULL)"
    without_schema = sql_for(dialect, "column_null", schema=None)
    assert f"FROM {table.split('.')[1]} " in without_schema.invalidate_sql


# ----- per-metric semantics -----------------------------------------------------------
def test_duplicate_counts_repeated_values_not_rows():
    result = sql_for("postgres", "column_duplicate")
    assert result.invalidate_sql == (
        'SELECT "email" FROM "shop"."orders" GROUP BY "email" HAVING count("email") > 1'
    )
    assert result.actual_sql == (
        "SELECT count(1) AS actual_value FROM (\n" + result.invalidate_sql + "\n) t"
    )


def test_blank_and_null_predicates():
    assert sql_for("mysql", "column_null").predicate == "(`email` IS NULL)"
    assert sql_for("mysql", "column_blank").predicate == "(`email` IS NULL OR `email` = '')"


@pytest.mark.parametrize(
    "dialect, fragment",
    [
        ("postgres", "\"email\" !~ '^[0-9]{6}$'"),
        ("mysql", "`email` NOT REGEXP '^[0-9]{6}$'"),
        ("starrocks", "`email` NOT REGEXP '^[0-9]{6}$'"),
        ("doris", "`email` NOT REGEXP '^[0-9]{6}$'"),
        ("clickhouse", "NOT match(`email`, '^[0-9]{6}$')"),
        ("hive", "NOT (`email` RLIKE '^[0-9]{6}$')"),
        ("duckdb", "NOT regexp_matches(\"email\", '^[0-9]{6}$')"),
    ],
)
def test_not_match_regex_uses_the_dialect_operator(dialect, fragment):
    predicate = sql_for(dialect, "column_match_regex").predicate
    assert predicate.startswith("(")
    assert "IS NOT NULL AND " in predicate
    assert fragment in predicate


@pytest.mark.parametrize(
    "dialect, fragment",
    [
        ("postgres", 'length("email"::text)'),
        ("mysql", "length(`email`)"),
        ("starrocks", "length(`email`)"),
        ("doris", "length(`email`)"),
        ("clickhouse", "length(toString(`email`))"),
        ("hive", "length(`email`)"),
        ("duckdb", 'length(CAST("email" AS VARCHAR))'),
    ],
)
def test_length_check_negates_the_conforming_comparison(dialect, fragment):
    config = {"comparator": ">", "length": 3}
    predicate = sql_for(dialect, "column_length", config=config).predicate
    assert f"NOT ({fragment} > 3)" in predicate
    assert "IS NOT NULL AND " in predicate


def test_value_between_negates_the_conforming_range_and_allows_one_bound():
    both = sql_for("postgres", "column_value_between", config={"min": 1, "max": 100}).predicate
    assert both == '("email" IS NOT NULL AND NOT ("email" >= 1 AND "email" <= 100))'
    lower = sql_for("postgres", "column_value_between", config={"min": 0.5, "max": None}).predicate
    assert lower == '("email" IS NOT NULL AND NOT ("email" >= 0.5))'
    upper = sql_for("postgres", "column_value_between", config={"min": None, "max": 9}).predicate
    assert upper == '("email" IS NOT NULL AND NOT ("email" <= 9))'


def test_enum_values_are_quoted_string_literals_with_doubled_quotes():
    result = sql_for(
        "postgres", "column_not_in_enums", config={"enum_list": "O'Brien\n李, O'Brien"}
    )
    assert result.predicate == (
        "(\"email\" IS NOT NULL AND \"email\" NOT IN ('O''Brien', '李'))"
    )
    guard_sql(result.invalidate_sql, "postgres")


def test_reference_check_correlates_the_aliased_source_table():
    result = sql_for("postgres", "column_not_in_reference")
    assert result.invalidate_sql == (
        'SELECT * FROM "shop"."orders" lattice_src WHERE (lattice_src."email" IS NOT NULL '
        'AND NOT EXISTS (SELECT 1 FROM "dim"."city" lattice_ref '
        'WHERE lattice_ref."code" = lattice_src."email"))'
    )
    without_schema = sql_for(
        "mysql", "column_not_in_reference", config={"reference_schema": ""}
    )
    assert "FROM `city` lattice_ref" in without_schema.invalidate_sql


@pytest.mark.parametrize(
    "dialect, unit, fragment",
    [
        ("postgres", "day", "(\"email\" < now() - interval '3 day')"),
        ("postgres", "minute", "(\"email\" < now() - interval '3 minute')"),
        ("mysql", "hour", "(`email` < DATE_SUB(now(), INTERVAL 3 HOUR))"),
        ("starrocks", "day", "(`email` < DATE_SUB(now(), INTERVAL 3 DAY))"),
        ("doris", "minute", "(`email` < DATE_SUB(now(), INTERVAL 3 MINUTE))"),
        ("clickhouse", "day", "(`email` < now() - INTERVAL 3 DAY)"),
        ("duckdb", "hour", "(\"email\" < now() - INTERVAL 3 hour)"),
        ("hive", "day", "(`email` < date_sub(current_timestamp(), 3))"),
    ],
)
def test_freshness_compares_timestamps_without_date_format(dialect, unit, fragment):
    result = sql_for(
        dialect, "table_freshness", config={"interval_value": 3, "interval_unit": unit}
    )
    assert result.predicate == fragment
    assert "DATE_FORMAT" not in result.invalidate_sql.upper()
    guard_sql(result.invalidate_sql, dialect)


@pytest.mark.parametrize("unit", ["minute", "hour"])
def test_hive_freshness_rejects_sub_day_units(unit):
    with pytest.raises(MetricError) as error:
        sql_for("hive", "table_freshness", config={"interval_value": 1, "interval_unit": unit})
    assert "Hive" in str(error.value)
    assert str(error.value).endswith("。")


@pytest.mark.parametrize("source_type, limited", [("iceberg", True), ("paimon", True),
                                                  ("duckdb", False), ("", False)])
def test_scan_limited_is_exposed_for_the_lake_formats(source_type, limited):
    assert sql_for("duckdb", "column_null", source_type=source_type).scan_limited is limited


# ----- safety -------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("value", INJECTIONS)
def test_identifier_injection_is_rejected_everywhere(dialect, value):
    for kwargs in ({"column": value}, {"table": value}, {"schema": value}):
        with pytest.raises(MetricError):
            sql_for(dialect, "column_null", **kwargs)
    for name in ("reference_schema", "reference_table", "reference_column"):
        with pytest.raises(MetricError):
            sql_for(dialect, "column_not_in_reference", config={name: value})


@pytest.mark.parametrize(
    "regexp",
    ["a' OR '1'='1", "^x$'; DROP TABLE x; --", "a;b", "a\nb", "it's"],
)
def test_regex_with_a_quote_or_semicolon_is_rejected_not_escaped(regexp):
    with pytest.raises(MetricError):
        validate_config("column_match_regex", {"regexp": regexp})
    with pytest.raises(MetricError):
        sql_for("postgres", "column_match_regex", config={"regexp": regexp})


@pytest.mark.parametrize(
    "filter_sql",
    [
        "id IN (SELECT id FROM secrets)",
        "EXISTS (SELECT 1 FROM users)",
        "1 = 1; DROP TABLE orders",
        "1 = 1 --",
        "1 = 1 /* x */",
        "((",
        "amount >",
        "DROP TABLE orders",
    ],
)
def test_unsafe_filters_are_rejected(filter_sql):
    with pytest.raises(MetricError):
        sql_for("postgres", "column_null", config={"filter": filter_sql})


def test_enum_and_filter_values_cannot_carry_control_characters():
    with pytest.raises(MetricError):
        validate_config("column_not_in_enums", {"enum_list": "a\x01b"})
    with pytest.raises(MetricError):
        validate_config("column_null", {"filter": "a = 'b\x00'"})


def test_metric_error_reports_a_chinese_message_and_status():
    with pytest.raises(MetricError) as error:
        build("no_such_metric", make_context("postgres", "shop", "orders", "email", {}))
    assert error.value.status_code == 400
    assert str(error.value).endswith("。")
    with pytest.raises(MetricError):
        build("column_null", make_context("oracle", "shop", "orders", "email", {}))
    with pytest.raises(MetricError):
        sql_for("postgres", "table_freshness", column=None)


# ----- configuration ------------------------------------------------------------------
def test_validate_config_normalises_and_is_idempotent():
    once = validate_config(
        "column_length", {"comparator": "<=", "length": "12", "filter": " amount > 0 "}
    )
    assert once == {"comparator": "<=", "length": 12, "filter": "amount > 0"}
    assert validate_config("column_length", once) == once
    enums = validate_config("column_not_in_enums", {"enum_list": " 男 , 女 \n男 "})
    assert enums["enum_list"] == "男,女"
    assert validate_config("column_not_in_enums", enums) == enums
    defaults = validate_config("table_freshness", {})
    assert defaults == {"interval_value": 1, "interval_unit": "day", "filter": ""}
    assert validate_config("column_value_between", {"min": "2.5"})["min"] == 2.5


@pytest.mark.parametrize(
    "metric_id, config",
    [
        ("column_null", {"nope": 1}),
        ("column_match_regex", {}),
        ("column_match_regex", {"regexp": ""}),
        ("column_match_regex", {"regexp": 7}),
        ("column_length", {"length": 10, "comparator": "LIKE"}),
        ("column_length", {"comparator": "<="}),
        ("column_length", {"comparator": "<=", "length": -1}),
        ("column_length", {"comparator": "<=", "length": 1.5}),
        ("column_length", {"comparator": "<=", "length": "ten"}),
        ("column_value_between", {}),
        ("column_value_between", {"min": 10, "max": 1}),
        ("column_not_in_enums", {"enum_list": " , "}),
        ("column_not_in_reference", {"reference_table": "city"}),
        ("table_freshness", {"interval_value": 0}),
        ("table_freshness", {"interval_value": 1, "interval_unit": "week"}),
        ("table_freshness", {"interval_value": float("inf")}),
    ],
)
def test_validate_config_rejects_bad_input(metric_id, config):
    with pytest.raises(MetricError) as error:
        validate_config(metric_id, config)
    assert str(error.value).endswith("。")


def test_validate_config_requires_an_object():
    with pytest.raises(MetricError):
        validate_config("column_null", "filter=1")


# ----- catalog ------------------------------------------------------------------------
def test_catalog_lists_the_six_categories_in_product_order():
    payload = catalog()
    assert [item["id"] for item in payload["dimensions"]] == [
        "uniqueness",
        "completeness",
        "accuracy",
        "standard",
        "relation",
        "timeliness",
    ]
    assert [item["label"] for item in payload["dimensions"]] == [
        "唯一性校验",
        "完整性校验",
        "准确性校验",
        "数据标准校验",
        "关联性校验",
        "及时性校验",
    ]
    listed = [metric["id"] for item in payload["dimensions"] for metric in item["metrics"]]
    assert listed == [metric_id for item in DIMENSIONS for metric_id in item.metrics]
    assert sorted(listed) == sorted(METRICS)
    json.dumps(payload)


def test_catalog_metrics_describe_their_form():
    metrics = {
        metric["id"]: metric
        for item in catalog()["dimensions"]
        for metric in item["metrics"]
    }
    for metric_id, metric in metrics.items():
        assert metric["label"] == METRICS[metric_id].label
        assert metric["needs_column"] is True
        assert metric["fields"][-1]["name"] == "filter"
        assert all(field["label"] for field in metric["fields"])
    assert metrics["table_freshness"]["level"] == "table"
    assert metrics["column_null"]["level"] == "column"
    comparator = metrics["column_length"]["fields"][0]
    assert comparator["required"] is True
    assert comparator["default"] == "<="
    assert [option["value"] for option in comparator["options"]] == [
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
    ]
