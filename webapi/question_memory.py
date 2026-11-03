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
"""What people confirmed, and what 智能问数 was asked.

A confirmed answer pins a question to its plan, so asking it again gives the same number,
and similar questions see it as an example. Only a person's confirmation counts: an answer
that merely ran is not evidence that its definition was right, and teaching the model from
unconfirmed history would make a past mistake look like a convention.

The log keeps every question including refusals, which is how a team finds out which
definitions its users are missing.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .semantic_planner import normalize

MAX_LOG = 2000
MAX_EXAMPLES = 3
SIMILAR_ENOUGH = 0.45


def _pairs(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(max(len(text) - 1, 1))}


def similarity(a: str, b: str) -> float:
    first, second = _pairs(normalize(a)), _pairs(normalize(b))
    return len(first & second) / len(first | second) if first | second else 0.0


class QuestionMemory:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS verified_question(
                id TEXT PRIMARY KEY, created_at TEXT, datasource_id TEXT, question TEXT,
                normalized TEXT, kind TEXT, title TEXT, definition TEXT, result_id TEXT, created_by TEXT,
                UNIQUE(datasource_id, normalized))"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS question_log(
                id TEXT PRIMARY KEY, created_at TEXT, datasource_id TEXT, question TEXT,
                outcome TEXT, provider TEXT, planner TEXT, sql TEXT, detail TEXT, result_id TEXT)"""
            )

    # ----- confirmed answers -----------------------------------------------------------
    def confirm(self, payload: dict[str, Any], created_by: str = "") -> dict[str, Any]:
        question = str(payload.get("question") or "").strip()
        if not question:
            raise ValueError("这条结果不是由提问产生的，无法确认。")
        semantic = payload.get("semantic") or {}
        if payload.get("provider") == "semantic" and semantic.get("request"):
            kind, definition = "semantic", {"request": semantic["request"], "model_id": semantic.get("model_id"), "fingerprint": semantic.get("fingerprint")}
        elif payload.get("provider") == "llm":
            kind, definition = "sql", {"sql": payload.get("sql")}
        else:
            raise ValueError("只有智能问数生成的结果可以确认。")
        record = {
            "id": str(uuid.uuid4()),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "datasource_id": payload.get("datasource_id"),
            "question": question,
            "normalized": normalize(question),
            "kind": kind,
            "title": payload.get("title") or question,
            "definition": json.dumps(definition, ensure_ascii=False),
            "result_id": payload.get("id"),
            "created_by": created_by,
        }
        with sqlite3.connect(self.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO verified_question(id, created_at, datasource_id, question, normalized, kind, title, definition, result_id, created_by)"
                " VALUES (:id, :created_at, :datasource_id, :question, :normalized, :kind, :title, :definition, :result_id, :created_by)",
                record,
            )
        return self._view(record)

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {key: (json.loads(row[key]) if key == "definition" else row[key]) for key in ("id", "created_at", "datasource_id", "question", "kind", "title", "definition", "result_id", "created_by")}

    def _rows(self, where: str = "", parameters: tuple = ()) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(f"SELECT * FROM verified_question {where} ORDER BY created_at DESC", parameters)]

    def confirmed(self, datasource_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._rows("WHERE datasource_id = ?", (datasource_id,)) if datasource_id else self._rows()
        return [self._view(row) for row in rows]

    def exact(self, datasource_id: str, question: str) -> dict[str, Any] | None:
        rows = self._rows("WHERE datasource_id = ? AND normalized = ?", (datasource_id, normalize(question)))
        return self._view(rows[0]) if rows else None

    def examples(self, datasource_id: str, question: str, kind: str, limit: int = MAX_EXAMPLES) -> list[dict[str, Any]]:
        """Confirmed answers to questions like this one, closest first."""
        rows = self._rows("WHERE datasource_id = ? AND kind = ?", (datasource_id, kind))
        scored = sorted(((similarity(question, row["question"]), row) for row in rows), key=lambda item: -item[0])
        return [self._view(row) for score, row in scored[:limit] if score >= SIMILAR_ENOUGH]

    def delete(self, item_id: str) -> bool:
        with sqlite3.connect(self.path) as con:
            return con.execute("DELETE FROM verified_question WHERE id = ?", (item_id,)).rowcount > 0

    # ----- the log -----------------------------------------------------------------------
    def log(self, datasource_id: str, question: str, outcome: str, *, provider: str = "", planner: str = "", sql: str = "", detail: str = "", result_id: str = "") -> None:
        try:
            with sqlite3.connect(self.path) as con:
                con.execute(
                    "INSERT INTO question_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), dt.datetime.now(dt.timezone.utc).isoformat(), datasource_id, question, outcome, provider, planner or "", sql[:4000], detail[:1000], result_id),
                )
                con.execute(f"DELETE FROM question_log WHERE id NOT IN (SELECT id FROM question_log ORDER BY created_at DESC LIMIT {MAX_LOG})")
        except sqlite3.Error:
            return  # Keeping a record must never fail the answer.

    def entries(self, limit: int = 100, outcome: str | None = None) -> list[dict[str, Any]]:
        size = max(1, min(int(limit), MAX_LOG))
        where, parameters = ("WHERE outcome = ?", (outcome,)) if outcome else ("", ())
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(f"SELECT * FROM question_log {where} ORDER BY created_at DESC LIMIT {size}", parameters)]
