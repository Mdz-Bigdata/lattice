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
"""Show a language model only the part of a database a question is about.

Free SQL is written from what the model sees, so what it sees decides most of the outcome.
Every table in the prompt is a chance to pick the wrong one, and without the relationships
it can only guess the join from column names. For a question this module:

* ranks tables by their names, comments, columns and business descriptions;
* adds the tables that connect them, following the join graph;
* lists how the tables join, and the real values of low-cardinality columns.

The join graph is built from what is known, strongest first: relationships a semantic
model declares, foreign keys in the database, and name matches the data confirms (many
warehouses declare no keys at all). A name match alone is never trusted.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SEMANTIC = "语义模型"
DECLARED = "外键"
INFERRED = "数据验证的推断"
#: A database with no more tables than this is shown whole: pruning it could only lose something.
TOP_K = 8
MAX_HOPS = 3
MAX_LISTED = 200
MAX_UNPRUNED = 40
KEY_COLUMN = re.compile(r"(^id$|_(id|sk|key|code|no)$)", re.IGNORECASE)
TEXT_TYPE = re.compile(r"char|text|string|varchar", re.IGNORECASE)
#: Columns whose values never go to an external model, whatever their cardinality.
SENSITIVE_COLUMN = re.compile(r"phone|mobile|tel\b|email|mail|address|id_?card|passw|secret|token|ssn|手机|电话|邮箱|地址|身份证|密码", re.IGNORECASE)


@dataclass
class TableInfo:
    schema: str | None
    name: str
    comment: str = ""
    rows: Any = None
    columns: list[dict[str, Any]] = field(default_factory=list)
    described: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def primary_key(self) -> list[str]:
        return [column["name"] for column in self.columns if column.get("primary_key")]

    def has(self, column: str) -> bool:
        return any(item["name"].lower() == column.lower() for item in self.columns)


@dataclass
class JoinEdge:
    """``many_table.many_columns`` points at the unique ``one_table.one_columns``."""

    many_table: str
    many_columns: list[str]
    one_table: str
    one_columns: list[str]
    source: str

    def touches(self, a: str, b: str) -> bool:
        return {self.many_table.lower(), self.one_table.lower()} == {a.lower(), b.lower()}

    def text(self) -> str:
        return f"{self.many_table}({', '.join(self.many_columns)}) → {self.one_table}({', '.join(self.one_columns)})"


@dataclass
class SchemaLink:
    tables: list[TableInfo]
    edges: list[JoinEdge]
    others: list[str]
    total: int
    seeds: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)


def tokens(text: str) -> dict[str, int]:
    """Latin words and overlapping Chinese pairs; Chinese questions have no spaces to split on."""
    found: dict[str, int] = {}
    lowered = str(text or "").lower()
    for word in re.findall(r"[a-z][a-z0-9]+", lowered):
        for part in word.split("_"):
            if len(part) > 1:
                found[part] = found.get(part, 0) + 1
                if part.endswith("s") and len(part) > 3:
                    found[part[:-1]] = found.get(part[:-1], 0) + 1
    for run in re.findall(r"[一-鿿]+", lowered):
        for index in range(len(run) - 1):
            pair = run[index:index + 2]
            found[pair] = found.get(pair, 0) + 1
    return found


def score_table(question: dict[str, int], table: TableInfo) -> float:
    def overlap(text: str, weight: float) -> float:
        return sum(weight for token in tokens(text) if token in question)

    total = overlap(table.name, 5) + overlap(table.comment, 4) + overlap(table.described, 2)
    for column in table.columns:
        total += overlap(column["name"], 2) + overlap(str(column.get("comment") or ""), 3)
    return total


def _adjacent(edges: Iterable[JoinEdge]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.many_table.lower(), set()).add(edge.one_table.lower())
        graph.setdefault(edge.one_table.lower(), set()).add(edge.many_table.lower())
    return graph


def _route(graph: dict[str, set[str]], start: str, goal: str) -> list[str]:
    queue = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        if len(path) > MAX_HOPS + 1:
            return []
        if path[-1] == goal:
            return path
        for other in sorted(graph.get(path[-1], ())):
            if other not in seen:
                seen.add(other)
                queue.append([*path, other])
    return []


def link_schema(question: str, tables: list[TableInfo], edges: list[JoinEdge], *, top_k: int = TOP_K, picked: list[str] | None = None) -> SchemaLink:
    """Pick the tables for a question, plus whatever connects them.

    ``picked`` are tables a language model chose. Word matching alone cannot tell that 地区
    means a column called region, which is what picking (or a vector index) is for; it
    adds to the matches rather than replacing them, since either can miss a table.
    """
    every = {table.name.lower() for table in tables}
    edges_within = lambda names: [e for e in edges if e.many_table.lower() in names and e.one_table.lower() in names]
    if len(tables) <= top_k:
        return SchemaLink(tables, edges_within(every), [], len(tables))
    wanted = tokens(question)
    ranked = sorted(((score_table(wanted, table), table.name.lower(), table) for table in tables), key=lambda item: (-item[0], item[1]))
    by_name = {table.name.lower(): table for table in tables}
    seeds = [by_name[name.lower()] for name in picked or [] if name.lower() in by_name]
    for score, _, table in ranked:
        if score > 0 and table not in seeds and len(seeds) < top_k:
            seeds.append(table)
    if not seeds:
        # Nothing to go on: an arbitrary few would hide the right table, so nothing is pruned.
        shown = tables[:MAX_UNPRUNED]
        names = {table.name.lower() for table in shown}
        others = [table.qualified for table in tables[MAX_UNPRUNED:MAX_UNPRUNED + MAX_LISTED]]
        return SchemaLink(shown, edges_within(names), others, len(tables))
    chosen = [table.name.lower() for table in seeds]
    graph = _adjacent(edges)
    connectors: list[str] = []
    for index, first in enumerate(list(chosen)):
        for second in list(chosen)[index + 1:]:
            for step in _route(graph, first, second):
                if step not in chosen and step in by_name:
                    chosen.append(step)
                    connectors.append(by_name[step].name)
    # The dimensions a fact table points at, directly or not, are what questions slice by, and
    # they cannot all be found by name: 地区 will not match a column called region.
    frontier = list(chosen)
    while frontier and len(chosen) < top_k * 2:
        current = frontier.pop(0)
        for edge in edges:
            target = edge.one_table.lower()
            if edge.many_table.lower() == current and target not in chosen and target in by_name:
                chosen.append(target)
                connectors.append(by_name[target].name)
                frontier.append(target)
    selected = [by_name[name] for name in chosen]
    names = set(chosen)
    linked = [e for e in edges if e.many_table.lower() in names and e.one_table.lower() in names]
    others = [table.qualified + (f"（{table.comment}）" if table.comment else "") for table in tables if table.name.lower() not in names][:MAX_LISTED]
    return SchemaLink(selected, linked, others, len(tables), [table.name for table in seeds], connectors)


def table_menu(tables: list[TableInfo], limit: int = MAX_LISTED) -> str:
    """One line per table, enough for a model to pick from without seeing every type."""
    lines = []
    for table in tables[:limit]:
        columns = ", ".join(column["name"] for column in table.columns[:30])
        lines.append(f"- {table.name}" + (f"（{table.comment}）" if table.comment else "") + f"：{columns}")
    return "\n".join(lines)


def render_link(link: SchemaLink, values: dict[str, list[str]]) -> str:
    """The schema block of the prompt. Table lines keep the shared ``- 库.表：列`` shape."""
    lines = []
    for table in link.tables:
        head = table.qualified + (f"（{table.comment}）" if table.comment else "")
        rows = f"，约 {table.rows} 行" if table.rows is not None else ""
        columns = ", ".join(
            f"{column['name']} {column.get('type', '')}".strip() + (f"（{column['comment']}）" if column.get("comment") else "")
            for column in table.columns[:60]
        )
        lines.append(f"- {head}{rows}：{columns}")
    if link.edges:
        lines.append("\n已知的表关联（箭头从多的一侧指向一的一侧；关联这些表时必须使用这些字段）：")
        lines.extend(f"- {edge.text()}（来源：{edge.source}）" for edge in link.edges)
    if values:
        lines.append("\n字段的实际取值（筛选时必须原样使用）：")
        lines.extend(f"- {name}：{'、'.join(options)}" for name, options in values.items())
    if link.others:
        lines.append(f"\n其他表（共 {link.total} 张表，以下未展开字段；问题需要时可以使用）：" + "；".join(link.others))
    return "\n".join(lines)


def singular_stem(table: str) -> str:
    word = re.split(r"[_.]", table.lower())[-1]
    if word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def join_candidates(tables: list[TableInfo], known: list[JoinEdge]) -> list[tuple[TableInfo, str, TableInfo, str]]:
    """Name matches worth checking against the data: a key column and the table that owns it.

    Ownership is what separates customers.customer_id from a sibling fact table holding the
    same order_id: joining two facts on a shared key is the fan-out this exists to avoid.
    """
    found = []
    for many in tables:
        for column in many.columns:
            name = column["name"]
            if not KEY_COLUMN.search(name):
                continue
            stem = re.sub(r"_(id|sk|key|code|no)$", "", name.lower())
            for one in tables:
                if one is many or any(edge.touches(many.name, one.name) for edge in known):
                    continue
                if one.has(name) and (stem == singular_stem(one.name) or one.primary_key == [name]):
                    found.append((many, name, one, name))
                elif one.has("id") and stem == singular_stem(one.name):
                    found.append((many, name, one, "id"))
    return found


def edges_from_foreign_keys(rows: list[dict[str, Any]]) -> list[JoinEdge]:
    return [JoinEdge(row["table"], list(row["columns"]), row["referenced_table"], list(row["referenced_columns"]), DECLARED) for row in rows]


class QuestionScope:
    """Which tables and columns 智能问数 may read, per data source.

    This narrows what questions can reach and what is sent to a model. It is not an access
    control on the data source: the SQL workbench, where a person writes the statement, is
    governed by the data source's own account.
    """

    def __init__(self, path: Path):
        self.path = path

    def _all(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, source_id: str) -> dict[str, list[str]]:
        entry = self._all().get(source_id) or {}
        return {key: [str(item) for item in entry.get(key) or []] for key in ("allow_tables", "deny_tables", "deny_columns")}

    def save(self, source_id: str, allow_tables: list[str], deny_tables: list[str], deny_columns: list[str]) -> dict[str, list[str]]:
        def clean(items: list[str]) -> list[str]:
            return sorted({str(item).strip().lower() for item in items if str(item).strip()})

        bad = [item for item in clean(deny_columns) if "." not in item]
        if bad:
            raise ValueError("字段黑名单请写成 表.字段：" + "、".join(bad))
        data = self._all()
        data[source_id] = {"allow_tables": clean(allow_tables), "deny_tables": clean(deny_tables), "deny_columns": clean(deny_columns)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return data[source_id]

    def table_allowed(self, source_id: str, table: str) -> bool:
        scope = self.get(source_id)
        name = table.lower().rsplit(".", 1)[-1]
        if scope["allow_tables"] and not any(fnmatch.fnmatch(name, pattern) for pattern in scope["allow_tables"]):
            return False
        return not any(fnmatch.fnmatch(name, pattern) for pattern in scope["deny_tables"])

    def hides_columns_of(self, source_id: str, table: str) -> bool:
        """Whether a column of this table is excluded, which rules out SELECT * on it."""
        name = table.lower().rsplit(".", 1)[-1]
        return any(fnmatch.fnmatch(name, pattern.rsplit(".", 1)[0]) for pattern in self.get(source_id)["deny_columns"])

    def column_allowed(self, source_id: str, table: str, column: str) -> bool:
        target = f"{table.lower().rsplit('.', 1)[-1]}.{column.lower()}"
        return not any(fnmatch.fnmatch(target, pattern) for pattern in self.get(source_id)["deny_columns"])
