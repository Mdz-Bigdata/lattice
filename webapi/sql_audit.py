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
"""Static and data checks on SQL a model wrote without the semantic layer.

When a question is outside what a semantic model defines, the statement comes from
the language model and nothing vouches for its joins. These checks catch what makes
such a statement wrong while still looking right:

* tables and columns the source does not have;
* joins with no condition, which multiply every row by every row;
* fan-outs: SUM, AVG or plain COUNT over rows that a join repeats. Whether a join
  repeats rows depends on the data, so the join key is probed for duplicates;
* joins on the wrong column, when the relationship between two tables is known;
* filter values the data does not contain (status = 'refunded' on a column that only
  ever says 已退款), which run fine and return a confident zero;
* tables and columns the question scope excludes;
* measures a wide table repeats per key, which SUM counts once per row with no join in sight.

A pre-aggregated subquery or CTE is the correct way to combine facts, and passes
because grouping by the join key makes that key unique.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

#: (schema or None, table, key columns) -> True, False when it has duplicates, None when unknown.
UniquenessProbe = Callable[[str | None, str, list[str]], "bool | None"]
#: (schema or None, table, column, value, is a LIKE pattern) -> whether any row matches, None when unknown.
ValueProbe = Callable[[str | None, str, str, str, bool], "bool | None"]
MAX_PROBES = 12
MAX_VALUE_PROBES = 8
DATE_LIKE = re.compile(r"^\d{4}([-/]\d{1,2}){0,2}")


@dataclass
class AuditReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    #: Filters on values the data does not hold. Worth one correction, but the value may
    #: simply not have occurred yet, so a model that insists gets a warning, not a refusal.
    value_issues: list[str] = field(default_factory=list)
    #: The aggregates the statement computes over physical columns, for a person to review.
    measures: list[str] = field(default_factory=list)
    #: Names of the columns summed or averaged anywhere in the statement, inner queries included.
    measure_names: set[str] = field(default_factory=set)
    #: Every ``table.column`` the statement filters on by a string value.
    filtered_columns: set[str] = field(default_factory=set)
    #: The columns behind ``value_issues``, so a correction can be held to keeping the condition.
    value_columns: set[str] = field(default_factory=set)


def parse_schema_context(text: str) -> dict[str, set[str]]:
    """Tables and columns from the ``- schema.table，约 N 行：col TYPE, col TYPE`` listing."""
    tables: dict[str, set[str]] = {}
    for line in text.splitlines():
        match = re.match(r"-\s+([^\s，（:：]+)[^：]*：(.*)$", line)
        if not match:
            continue
        columns = {
            part.strip().split(" ")[0].strip('"`').lower()
            for part in re.split(r",\s+(?![^()]*\))", match.group(2))
            if part.strip()
        }
        tables[match.group(1).rsplit(".", 1)[-1].lower()] = columns
    return tables


def _physical(scope: Scope, alias: str) -> exp.Table | None:
    source = scope.sources.get(alias)
    return source if isinstance(source, exp.Table) else None


def _grouped_by(scope: Scope, alias: str, columns: list[str]) -> bool:
    """A derived table grouped by exactly these columns has one row per key."""
    source = scope.sources.get(alias)
    if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
        return False
    group = source.expression.args.get("group")
    if group is None:
        return False
    outputs: dict[str, str] = {}
    for item in source.expression.selects:
        outputs[item.alias_or_name.lower()] = item.unalias().sql().lower()
    keys = {item.sql().lower() for item in group.expressions}
    names = {key.rsplit(".", 1)[-1].strip('"`') for key in keys}
    return all(outputs.get(name.lower(), "") in keys or name.lower() in names for name in columns) and len(keys) <= len(columns)


def _conjuncts(condition: exp.Expression | None) -> list[exp.Expression]:
    if condition is None:
        return []
    if isinstance(condition, exp.And):
        return [*_conjuncts(condition.left), *_conjuncts(condition.right)]
    if isinstance(condition, exp.Paren):
        return _conjuncts(condition.this)
    return [condition]


def _own_aggregates(select: exp.Select) -> list[exp.AggFunc]:
    """Aggregates evaluated by this SELECT, not by a nested query or a window."""
    found = []
    for node in select.find_all(exp.AggFunc):
        owner = node.find_ancestor(exp.Select)
        if owner is select and not node.find_ancestor(exp.Window):
            found.append(node)
    return found


def _repeat_safe(aggregate: exp.AggFunc) -> bool:
    if isinstance(aggregate, (exp.Min, exp.Max, exp.ApproxDistinct)):
        return True
    return isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct)


class _Join:
    def __init__(self, left: str, right: str):
        self.left, self.right = left, right
        self.left_columns: list[str] = []
        self.right_columns: list[str] = []


def _join_pairs(select: exp.Select, scope: Scope) -> tuple[list[_Join], list[str]]:
    """The equality joins of one SELECT, plus a description of any join without a condition."""
    aliases = list(scope.selected_sources)
    pairs: dict[frozenset[str], _Join] = {}
    uses_where = []
    bare = []

    def add(condition: exp.Expression) -> bool:
        linked = False
        for term in _conjuncts(condition):
            if not isinstance(term, exp.EQ) or not isinstance(term.left, exp.Column) or not isinstance(term.right, exp.Column):
                continue
            left, right = term.left.table, term.right.table
            if not left or not right or left == right:
                continue
            key = frozenset((left, right))
            pair = pairs.setdefault(key, _Join(left, right))
            if pair.left == left:
                pair.left_columns.append(term.left.name)
                pair.right_columns.append(term.right.name)
            else:
                pair.left_columns.append(term.right.name)
                pair.right_columns.append(term.left.name)
            linked = True
        return linked

    for join in select.args.get("joins") or []:
        alias = join.alias_or_name
        using = join.args.get("using")
        on = join.args.get("on")
        if isinstance(join.this, (exp.Unnest, exp.Lateral)):
            continue
        if using:
            left = next((name for name in aliases if name != alias), "")
            pair = pairs.setdefault(frozenset((left, alias)), _Join(left, alias))
            names = [item.name for item in using]
            forward = pair.left == left
            pair.left_columns.extend(names)
            pair.right_columns.extend(names)
            if not forward:
                pair.left, pair.right = left, alias
            continue
        if on is not None:
            add(on)
            continue
        uses_where.append(alias)
    where = select.args.get("where")
    linked_by_where = add(where.this) if where is not None else False
    for alias in uses_where:
        if not (linked_by_where and any(alias in key for key in pairs)):
            bare.append(alias)
    return list(pairs.values()), bare


def _column_owner(column: exp.Column, scope: Scope) -> str:
    if column.table:
        return column.table
    sources = list(scope.selected_sources)
    return sources[0] if len(sources) == 1 else ""


def _filter_values(select: exp.Select, scope: Scope) -> list[tuple[exp.Table, str, str, bool]]:
    """(table, column, value, is LIKE) for string filters on physical columns of this SELECT."""
    where = select.args.get("where")
    if where is None:
        return []
    found = []

    def add(left: exp.Expression, value: exp.Expression, like: bool) -> None:
        while isinstance(left, (exp.Lower, exp.Upper, exp.Trim, exp.Paren)):
            left = left.this
        if not isinstance(left, exp.Column) or not isinstance(value, exp.Literal) or not value.is_string:
            return
        table = _physical(scope, _column_owner(left, scope))
        text = value.this
        if table is not None and text and not DATE_LIKE.match(text):
            found.append((table, left.name, text, like))

    for node in where.find_all(exp.EQ, exp.In, exp.Like, exp.ILike):
        if node.find_ancestor(exp.Select) is not select:
            continue
        if isinstance(node, exp.In):
            for item in node.expressions:
                add(node.this, item, False)
        else:
            add(node.left, node.right, not isinstance(node, exp.EQ))
    return found


def audit_sql(
    sql: str,
    dialect: str,
    known: dict[str, set[str]] | None = None,
    probe: UniquenessProbe | None = None,
    *,
    edges: list[Any] | None = None,
    table_allowed: Callable[[str], bool] | None = None,
    column_allowed: Callable[[str, str], bool] | None = None,
    hides_columns: Callable[[str], bool] | None = None,
    repeated_measures: Callable[[str], dict[str, str]] | None = None,
    uncertain_measures: Callable[[str], dict[str, str]] | None = None,
    unprofiled: Callable[[str], bool] | None = None,
    value_probe: ValueProbe | None = None,
) -> AuditReport:
    report = AuditReport()
    tree = None
    for candidate in (dialect, "duckdb"):
        try:
            statements = [item for item in sqlglot.parse(sql, read=candidate) if item is not None]
        except sqlglot.errors.SqlglotError:
            continue
        if len(statements) == 1:
            tree = statements[0]
            break
    if tree is None:
        report.warnings.append("语句无法静态解析，未做关联校验；引擎仍会按只读规则执行或拒绝。")
        return report
    try:
        scopes = traverse_scope(tree)
    except (sqlglot.errors.SqlglotError, ValueError, KeyError) as error:
        report.warnings.append(f"语句的作用域无法分析，未做关联校验：{str(error)[:120]}")
        return report

    probes: dict[tuple[str | None, str, tuple[str, ...]], bool | None] = {}

    def unique(scope: Scope, alias: str, columns: list[str]) -> bool | None:
        table = _physical(scope, alias)
        if table is None:
            return True if _grouped_by(scope, alias, columns) else None
        if probe is None:
            return None
        key = (table.db or None, table.name, tuple(sorted(column.lower() for column in columns)))
        if key not in probes:
            if len(probes) >= MAX_PROBES:
                return None
            probes[key] = probe(table.db or None, table.name, list(columns))
        return probes[key]

    fan_checked = False
    value_checks = 0
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        # ----- names that do not exist
        if known is not None:
            for alias in scope.selected_sources:
                table = _physical(scope, alias)
                if table is None:
                    continue
                columns = known.get(table.name.lower())
                if columns is None:
                    report.errors.append(f"表 {table.name} 不存在。")
                    continue
                for column in scope.columns:
                    if column.table == alias and column.name.lower() not in columns:
                        report.errors.append(f"表 {table.name} 中没有列 {column.name}。")
            if all(_physical(scope, alias) is not None for alias in scope.selected_sources):
                everything = set()
                for alias in scope.selected_sources:
                    everything |= known.get(_physical(scope, alias).name.lower(), set())
                outputs = {item.alias.lower() for item in select.selects if item.alias}
                for column in scope.columns:
                    if not column.table and column.name.lower() not in everything and column.name.lower() not in outputs and everything:
                        report.errors.append(f"列 {column.name} 不在所查询的表中。")
        # ----- what the question scope excludes
        for alias in scope.selected_sources:
            table = _physical(scope, alias)
            if table is None:
                continue
            if table_allowed is not None and not table_allowed(table.name):
                report.errors.append(f"表 {table.name} 不在智能问数允许访问的范围内。")
            if column_allowed is not None:
                # An unqualified column could belong to any source of this SELECT.
                for column in scope.columns:
                    if (column.table == alias or not column.table) and not column_allowed(table.name, column.name):
                        report.errors.append(f"字段 {table.name}.{column.name} 不在智能问数允许访问的范围内。")
                if select.is_star and hides_columns is not None and hides_columns(table.name):
                    report.errors.append(f"表 {table.name} 含有不允许访问的字段，请明确列出需要的字段，不要使用 SELECT *。")
        # ----- filter values
        for table, column, value, like in _filter_values(select, scope):
            target = f"{table.name}.{column}".lower()
            report.filtered_columns.add(target)
            if value_probe is None or value_checks >= MAX_VALUE_PROBES:
                continue
            value_checks += 1
            if value_probe(table.db or None, table.name, column, value, like) is False:
                report.value_columns.add(target)
                report.value_issues.append(
                    f"{table.name}.{column} 中没有匹配 '{value}' 的数据。如果是取值写法不对，请改用该字段的实际取值；"
                    "如果问题问的就是这个值，请保留这个筛选条件，结果为空也是正确答案。不要去掉条件，也不要改成回答别的问题。"
                )
        for aggregate in _own_aggregates(select):
            columns = list(aggregate.find_all(exp.Column))
            if not isinstance(aggregate, exp.Count):
                report.measure_names |= {column.name.lower() for column in columns}
            # A derived table's column is named after the real one or hides it; either way the
            # physical choice is what is worth showing, so report where a real table is read.
            if any(_physical(scope, _column_owner(column, scope)) is not None for column in columns):
                text = aggregate.sql(dialect=dialect)
                if text not in report.measures:
                    report.measures.append(text)
        for name, source in scope.sources.items():
            # SUM over a pre-reduced subquery: the measure is chosen one level down, by a column
            # the aggregate there may not touch, so name the outer aggregate too.
            if isinstance(source, Scope):
                for aggregate in _own_aggregates(select):
                    text = aggregate.sql(dialect=dialect)
                    if text not in report.measures:
                        report.measures.append(text)
                break
        # ----- measures a wide table already repeats
        if repeated_measures is not None:
            for aggregate in _own_aggregates(select):
                if _repeat_safe(aggregate):
                    continue
                for column in aggregate.find_all(exp.Column):
                    table = _physical(scope, _column_owner(column, scope))
                    if table is not None and unprofiled is not None and unprofiled(table.name):
                        report.warnings.append(f"未能探测 {table.name} 的粒度（查询失败或超时），无法排除 {column.name} 在多行上重复，{aggregate.sql(dialect=dialect)} 可能偏大，请核对。")
                    key = repeated_measures(table.name).get(column.name.lower()) if table is not None else None
                    if key:
                        report.errors.append(
                            f"{aggregate.sql(dialect=dialect)} 存在重复累计：{table.name}.{column.name} 在同一个 {key} 的多行上取值相同。"
                            f"请先用子查询按 {key} 去重（SELECT DISTINCT 键, {column.name}, 需要的维度），再汇总。"
                        )
                    weak = uncertain_measures(table.name).get(column.name.lower()) if table is not None and uncertain_measures is not None else None
                    if weak:
                        report.warnings.append(f"{table.name}.{column.name} 在同一个 {weak} 内取值相同，但无法确定它属于哪个键（证据太少，或同时符合多个键）。{aggregate.sql(dialect=dialect)} 的去重方式会影响结果，请核对。")
        # ----- joins
        joins = select.args.get("joins") or []
        if not joins:
            continue
        pairs, bare = _join_pairs(select, scope)
        for alias in bare:
            report.errors.append(f"表 {alias} 的关联没有条件，会产生笛卡尔积。")
        # ----- joins that contradict a known relationship
        for pair in pairs:
            left, right = _physical(scope, pair.left), _physical(scope, pair.right)
            if left is None or right is None or not edges:
                continue
            related = [edge for edge in edges if edge.touches(left.name, right.name)]
            if not related:
                report.warnings.append(f"{left.name} 与 {right.name} 之间没有已知的关联关系，请核对关联字段（{', '.join(pair.left_columns)} = {', '.join(pair.right_columns)}）。")
                continue
            used = {(a.lower(), b.lower()) for a, b in zip(pair.left_columns, pair.right_columns)}
            matches = False
            for edge in related:
                forward = edge.many_table.lower() == left.name.lower()
                near, far = (edge.many_columns, edge.one_columns) if forward else (edge.one_columns, edge.many_columns)
                if {(a.lower(), b.lower()) for a, b in zip(near, far)} <= used:
                    matches = True
            if matches:
                continue
            described = "；".join(edge.text() for edge in related)
            message = f"{left.name} 与 {right.name} 的关联字段不对：用的是 {', '.join(pair.left_columns)} = {', '.join(pair.right_columns)}，已知关系是 {described}。"
            if any(edge.source != "数据验证的推断" for edge in related):
                report.errors.append(message)
            else:
                report.warnings.append(message)
        aggregates = _own_aggregates(select)
        sensitive = [item for item in aggregates if not _repeat_safe(item)]
        if not sensitive:
            continue
        fan_checked = True
        # Which sources a join repeats: b's rows repeat when a's key has duplicates.
        repeats: dict[str, list[tuple[str, list[str]]]] = {}
        unverifiable: list[str] = []
        for pair in pairs:
            left_unique = unique(scope, pair.left, pair.left_columns)
            right_unique = unique(scope, pair.right, pair.right_columns)
            if right_unique is False:
                repeats.setdefault(pair.left, []).append((pair.right, pair.right_columns))
            if left_unique is False:
                repeats.setdefault(pair.right, []).append((pair.left, pair.left_columns))
            if right_unique is None or left_unique is None:
                unverifiable.append(f"{pair.left} 与 {pair.right}")
        neighbours: dict[str, set[str]] = {}
        for pair in pairs:
            neighbours.setdefault(pair.left, set()).add(pair.right)
            neighbours.setdefault(pair.right, set()).add(pair.left)
        for aggregate in sensitive:
            owners = {column.table for column in aggregate.find_all(exp.Column) if column.table}
            if not owners:
                # COUNT(*) counts joined rows: that may be what was meant, so it cannot be
                # called wrong, but the reader should know what the number counts.
                if repeats:
                    report.warnings.append(f"{aggregate.sql(dialect=dialect)} 统计的是关联后的行数，存在一对多关联时不等于任何一张表的行数。")
                continue
            for owner in owners:
                # A join elsewhere repeats this source's rows too, so walk the whole join graph.
                reached, frontier = {owner}, [owner]
                culprit = None
                while frontier and culprit is None:
                    current = frontier.pop()
                    for other, columns in repeats.get(current, []):
                        # Only a join leading away from the measured rows can repeat them.
                        # Looking back along the way we came is not a repeat: customers
                        # repeat per order, but that says nothing about the order's rows.
                        if other not in owners and other not in reached:
                            culprit = (current, other, columns)
                            break
                    for other in neighbours.get(current, ()):
                        if other not in reached:
                            reached.add(other)
                            frontier.append(other)
                if culprit:
                    near, far, columns = culprit
                    report.errors.append(
                        f"{aggregate.sql(dialect=dialect)} 存在重复累计：{far} 的关联键（{', '.join(columns)}）有重复值，"
                        f"关联后 {near} 的一行会对应多行。请先把 {far} 按关联键聚合成子查询后再关联，或分别聚合后合并。"
                    )
                    break
        for text in unverifiable:
            report.warnings.append(f"未能确认 {text} 的关联键是否唯一，无法排除重复累计。")
    if fan_checked and not report.errors:
        report.passed.append("关联键唯一性探测未发现重复累计。")
    report.errors = list(dict.fromkeys(report.errors))
    report.warnings = list(dict.fromkeys(report.warnings))
    report.value_issues = list(dict.fromkeys(report.value_issues))
    return report


def value_probe_sql(schema: str | None, table: str, column: str, value: str, like: bool, dialect: str) -> str:
    """One row exists exactly when some row holds this value, compared without regard to case."""
    target = exp.Table(this=exp.to_identifier(table, quoted=True), db=exp.to_identifier(schema, quoted=True) if schema else None)
    subject = exp.Lower(this=exp.column(exp.to_identifier(column, quoted=True)))
    literal = exp.Literal.string(value.lower())
    condition = exp.Like(this=subject, expression=literal) if like else exp.EQ(this=subject, expression=literal)
    return exp.Select(expressions=[exp.alias_(exp.Literal.number(1), "lattice_hit")]).from_(target).where(condition).limit(1).sql(dialect=dialect)


def duplicate_key_probe_sql(schema: str | None, table: str, columns: list[str], dialect: str) -> str:
    """One row exists exactly when the key has a duplicate; NULL keys never join."""
    keys = [exp.column(exp.to_identifier(name, quoted=True)) for name in columns]
    target = exp.Table(this=exp.to_identifier(table, quoted=True), db=exp.to_identifier(schema, quoted=True) if schema else None)
    conditions = [exp.Not(this=exp.Is(this=key.copy(), expression=exp.Null())) for key in keys]
    select = (
        exp.Select(expressions=[exp.alias_(exp.Literal.number(1), "lattice_dup")])
        .from_(target)
        .where(exp.and_(*conditions))
        .group_by(*[key.copy() for key in keys])
        .having(exp.GT(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(1)))
        .limit(1)
    )
    return select.sql(dialect=dialect)
