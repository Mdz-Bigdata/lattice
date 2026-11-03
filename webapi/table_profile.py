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
"""What the data says about the grain of a wide table.

No schema says that an order's amount sits on each of its items, yet SUM over it is only right
if that is known. It shows in the data: within one order the column never changes, across
orders it does. Evidence is graded rather than treated as yes or no, because a key that repeats
only a couple of times cannot tell a repeated measure from a coincidence, and in that case the
reader has to be told instead of the question being answered as if it had been settled.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import sqlglot
from sqlglot import exp

#: Columns that may identify something: order_id, sku_no, 订单号, 客户编码.
KEY_LIKE = re.compile(r"(^id$|_(id|sk|key|code|no|num|number)$|(编号|编码|单号|号|代码)$)", re.IGNORECASE)
#: Numeric names that describe a quantity, never an identifier.
MEASURE_NAME = re.compile(r"amount|amt|price|qty|quantity|value|total|sum|cost|fee|tax|paid|金额|价|额|费|数量", re.IGNORECASE)
KEY_TYPE = re.compile(r"int|char|text|string", re.IGNORECASE)
NOT_KEY_TYPE = re.compile(r"point|date|time|decimal|numeric|float|double", re.IGNORECASE)
MAX_KEYS = 4
MAX_KEY_PAIRS = 3
MAX_UNNAMED_KEYS = 2
#: A dimension this empty suggests a join that failed while the table was built.
NULL_SHARE_NOTABLE = 0.05
#: An identifier has many values: this share of the rows, so 地区 is not mistaken for one.
UNNAMED_KEY_MIN_RATIO = 0.02
MAX_COLUMNS = 60
#: Reads a sample: enough to see keys repeat, and bounded on a huge table.
SAMPLE_ROWS = 200_000
#: Keys spanning several rows needed before a constant column counts as settled.
MIN_REPEATED_KEYS = 5


@dataclass
class TableProfile:
    #: Keys that actually repeat, with how many distinct values each has; fewer means coarser.
    keys: dict[tuple[str, ...], int] = field(default_factory=dict)
    #: column -> keys it is constant within, coarsest first. Safe to act on.
    constant: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    #: The same on evidence too thin to act on: too few repeats, or a column that may not be a key.
    uncertain: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    #: The table holds more rows than were read, so the findings need checking in full.
    sampled: bool = False
    #: Reading failed or timed out: no finding here means nothing was learned, not nothing wrong.
    failed: bool = False

    def key_of(self, column: str) -> tuple[str, ...]:
        """The one key a column belongs to, or () if none or if the data cannot say which.

        Constant within two keys is not settled by picking one: a limit per customer and a fee
        charged monthly look alike, and reducing either by the other's key gives a wrong total.
        """
        options = self.constant.get(column) or []
        return options[0] if len(options) == 1 else ()


def _source(table: exp.Expression) -> str:
    """A table reference as DuckDB text, the dialect the statements below are written in."""
    return table.sql(dialect="duckdb")


def _quoted(columns: tuple[str, ...]) -> str:
    return ", ".join(f'"{name}"' for name in columns)


def per_key_statement(table: exp.Expression, key: tuple[str, ...], columns: list[str], dialect: str) -> str:
    distinct = ", ".join(f'COUNT(DISTINCT "{name}") AS "d{index}"' for index, name in enumerate(columns))
    largest = ", ".join(f'MAX("d{index}") AS "d{index}"' for index in range(len(columns)))
    present = " AND ".join(f'"{name}" IS NOT NULL' for name in key)
    statement = (
        f'SELECT SUM(CASE WHEN "lattice_n" > 1 THEN 1 ELSE 0 END) AS "lattice_multi", COUNT(*) AS "lattice_keys", {largest} FROM ('
        f'SELECT {_quoted(key)}, COUNT(*) AS "lattice_n", {distinct} FROM (SELECT * FROM {_source(table)} LIMIT {SAMPLE_ROWS}) AS "lattice_s" '
        f'WHERE {present} GROUP BY {_quoted(key)}) AS "lattice_groups"'
    )
    return sqlglot.transpile(statement, read="duckdb", write=dialect)[0]


def spread_statement(table: exp.Expression, columns: list[str], dialect: str) -> str:
    statement = (
        "SELECT " + ", ".join(f'COUNT(DISTINCT "{name}") AS "c{index}"' for index, name in enumerate(columns))
        + f', COUNT(*) AS "lattice_rows" FROM (SELECT * FROM {_source(table)} LIMIT {SAMPLE_ROWS + 1}) AS "lattice_s"'
    )
    return sqlglot.transpile(statement, read="duckdb", write=dialect)[0]


def _could_be_key(column: str, kind: str, distinct: float, rows: float) -> bool:
    """A column shaped like an identifier though not named like one (单据, ord_ref)."""
    if MEASURE_NAME.search(column) or not KEY_TYPE.search(kind) or NOT_KEY_TYPE.search(kind):
        return False
    return MIN_REPEATED_KEYS <= distinct < rows and distinct / max(rows, 1) >= UNNAMED_KEY_MIN_RATIO


def profile_table(
    run: Callable[[str], list[list[Any]]], table: exp.Expression, columns: list[str], dialect: str,
    *, keys: list[str] | None = None, kinds: dict[str, str] | None = None,
) -> TableProfile:
    """Find which columns are constant within which keys. ``run`` returns a statement's rows.

    Keys named like keys are trusted, alone and in pairs, since a tenant and an order number
    can identify an order together. A column merely shaped like a key is not: tax is constant
    within amount, and reducing by amount would merge different orders, which loses money
    silently. What only such a column explains is reported as uncertain and never acted on.
    """
    profile = TableProfile()
    columns = columns[:MAX_COLUMNS]
    try:
        totals = run(spread_statement(table, columns, dialect))[0]
    except Exception:  # noqa: BLE001 - reported through ``failed``, never mistaken for a clean table
        profile.failed = True
        return profile
    spread = {name: float(totals[index] or 0) for index, name in enumerate(columns)}
    rows = float(totals[len(columns)] or 0)
    profile.sampled = rows > SAMPLE_ROWS
    named = [name for name in (keys if keys is not None else columns) if KEY_LIKE.search(name) or keys is not None][:MAX_KEYS]
    unnamed = sorted(
        (name for name in columns if name not in named and _could_be_key(name, (kinds or {}).get(name, ""), spread[name], rows)),
        key=lambda name: -spread[name],
    )[:MAX_UNNAMED_KEYS]

    found: dict[str, list[tuple[int, tuple[str, ...]]]] = {}
    weak: dict[str, list[tuple[int, tuple[str, ...]]]] = {}

    def examine(key: tuple[str, ...], trusted: bool, only: list[str] | None = None) -> None:
        others = [name for name in (only if only is not None else columns) if name not in key]
        if not others:
            return
        try:
            row = run(per_key_statement(table, key, others, dialect))[0]
        except Exception:  # noqa: BLE001 - reported through ``failed``
            profile.failed = True
            return
        repeated, cardinality = float(row[0] or 0), int(float(row[1] or 0))
        if repeated <= 0:
            return
        profile.keys[key] = cardinality
        target = found if trusted and repeated >= MIN_REPEATED_KEYS else weak
        for index, column in enumerate(others):
            # A column constant everywhere (a quantity of 1 on every row) says nothing about keys.
            if float(row[index + 2] or 0) <= 1 and spread[column] > 1:
                target.setdefault(column, []).append((cardinality, key))

    for name in named:
        examine((name,), True)
    # Columns no single key explains may still belong to two keys together.
    unexplained = [name for name in columns if name not in found]
    for pair in list(itertools.combinations(named, 2))[:MAX_KEY_PAIRS]:
        if unexplained:
            examine(pair, True, unexplained)
    unexplained = [name for name in columns if name not in found]
    for name in unnamed:
        if unexplained:
            examine((name,), False, unexplained)
    profile.constant = {column: [key for _, key in sorted(options)] for column, options in found.items()}
    profile.uncertain = {column: [key for _, key in sorted(options)] for column, options in weak.items() if column not in found}
    return profile


def self_check(run: Callable[[str], list[list[Any]]], table: exp.Expression, columns: list[tuple[str, str]], dialect: str, *, primary_key: list[str] | None = None) -> list[dict[str, str]]:
    """What a wide table can say about itself when nothing else can be compared with it.

    None of this proves the numbers right: a table consistently wrong passes every check. It does
    catch what breaking a wide table usually leaves behind: rows written twice by a rerun or a bad
    join, a key that repeats, dimensions emptied by a failed join, and data that stopped arriving.
    """
    checks: list[dict[str, str]] = []
    source = _source(table)
    plain = [name for name, kind in columns if not re.search(r"json|array|map|struct|blob|binary|bytea", kind, re.IGNORECASE)]

    def transpiled(statement: str) -> str:
        return sqlglot.transpile(statement, read="duckdb", write=dialect)[0]

    sample = f'(SELECT * FROM {source} LIMIT {SAMPLE_ROWS}) AS "lattice_s"'
    try:
        repeated = run(transpiled(f'SELECT 1 AS "lattice_dup" FROM {sample} GROUP BY {_quoted(tuple(plain))} HAVING COUNT(*) > 1 LIMIT 1'))
        if repeated:
            checks.append({"name": "宽表自检", "status": "skipped", "detail": "存在完全相同的重复行，汇总结果可能偏大。常见原因是构建任务重复写入或关联放大，请先检查宽表的构建过程。"})
    except Exception as error:  # noqa: BLE001 - a check that cannot run is reported as such
        checks.append({"name": "宽表自检", "status": "info", "detail": f"未能检查重复行：{str(error)[:120]}"})
    if primary_key:
        try:
            if run(transpiled(f'SELECT 1 AS "lattice_dup" FROM {sample} GROUP BY {_quoted(tuple(primary_key))} HAVING COUNT(*) > 1 LIMIT 1')):
                checks.append({"name": "宽表自检", "status": "skipped", "detail": f"声明的主键（{'、'.join(primary_key)}）存在重复，这张表的粒度和声明不符，汇总结果可能偏大。"})
        except Exception:  # noqa: BLE001 - the duplicate-row check already reports an unreadable table
            pass
    text = [name for name, kind in columns if re.search(r"char|text|string", kind, re.IGNORECASE)][:MAX_COLUMNS]
    if text:
        shares = ", ".join(f'SUM(CASE WHEN "{name}" IS NULL THEN 1 ELSE 0 END) AS "n{index}"' for index, name in enumerate(text))
        try:
            row = run(transpiled(f'SELECT COUNT(*) AS "lattice_rows", {shares} FROM {sample}'))[0]
            rows = float(row[0] or 0)
            empty = [f"{name}（{float(row[index + 1] or 0) / rows:.0%}）" for index, name in enumerate(text) if rows and float(row[index + 1] or 0) / rows >= NULL_SHARE_NOTABLE]
            if empty:
                checks.append({"name": "宽表自检", "status": "info", "detail": "这些维度有较多空值，可能是构建时关联失败，相关行会落入空分组：" + "、".join(empty[:6]) + "。"})
        except Exception:  # noqa: BLE001 - the duplicate-row check already reports an unreadable table
            pass
    dates = [name for name, kind in columns if re.search(r"date|time", kind, re.IGNORECASE)][:4]
    if dates:
        latest = ", ".join(f'MAX("{name}") AS "m{index}"' for index, name in enumerate(dates))
        try:
            row = run(transpiled(f"SELECT {latest} FROM {source}"))[0]
            shown = [f"{name} 最新到 {str(value)[:10]}" for name, value in zip(dates, row) if value is not None]
            if shown:
                checks.append({"name": "数据新鲜度", "status": "info", "detail": "；".join(shown) + "。请确认是否符合预期。"})
        except Exception:  # noqa: BLE001 - freshness is a courtesy, its absence misleads no one
            pass
    return checks
