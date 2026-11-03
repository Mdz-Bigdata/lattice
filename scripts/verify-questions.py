#!/usr/bin/env python3
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
"""Check 智能问数 against answers computed without its joins, on every data source.

Run against a started service. Expected values come from the sample tables one row at a time
in Python, so a join mistake in the semantic layer or a wrong date function on an engine shows
up as a difference rather than repeating itself on both sides. No language model is called:
these are questions the semantic layer must answer on its own. Exits non-zero on any difference,
and writes the report to .runtime/webui/question-verification.json.
"""

from __future__ import annotations

import collections
import datetime as dt
import decimal
import json
import os
import sys
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime" / "webui"
GRAINS = ("day", "week", "month", "quarter", "year")
TOLERANCE = 0.01


def expected_answers(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    def rows(table: str, columns: str) -> list[tuple]:
        return con.execute(f"SELECT {columns} FROM {table}").fetchall()

    orders = {row[0]: row[1:] for row in rows("t_lattice_orders", "order_id, customer_id, order_date")}
    regions = dict(rows("t_lattice_customers", "customer_id, region"))
    categories = dict(rows("t_lattice_products", "product_id, category"))
    sellers = dict(rows("t_lattice_sellers", "seller_id, seller_name"))
    items = rows("t_lattice_order_items", "order_id, product_id, seller_id, price")
    payments = rows("t_lattice_payments", "order_id, payment_value")

    def region(order: int) -> str:
        return regions[orders[order][0]]

    def total(pairs) -> dict[str, float]:
        result: dict[str, decimal.Decimal] = collections.defaultdict(decimal.Decimal)
        for key, value in pairs:
            result[key] += value
        return {key: float(value) for key, value in result.items()}

    def distinct(pairs) -> dict[str, float]:
        result: dict[str, set] = collections.defaultdict(set)
        for key, value in pairs:
            result[key].add(value)
        return {key: float(len(value)) for key, value in result.items()}

    south = total((sellers[i[2]], i[3]) for i in items if region(i[0]) == "华南")
    top = sorted(south.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    return {
        "各地区的销售额和支付额": {"key": "region", "total_sales": total((region(i[0]), i[3]) for i in items), "total_payment": total((region(p[0]), p[1]) for p in payments)},
        "华东地区各商品类别的销售额": {"key": "category", "total_sales": total((categories[i[1]], i[3]) for i in items if region(i[0]) == "华东")},
        "各地区客户数": {"key": "region", "customer_count": distinct((region(order), detail[0]) for order, detail in orders.items())},
        "2018年各类别的订单量": {"key": "category", "order_count": distinct((categories[i[1]], i[0]) for i in items if orders[i[0]][1].year == 2018)},
        "华南地区销售额最高的前3个商家": {"key": "seller_name", "total_sales": dict(top), "order": [name for name, _ in top]},
        "商品类别销售额": {"key": "category", "total_sales": total((categories[i[1]], i[3]) for i in items)},
    }


def expected_by_grain(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, float]]:
    """Period totals; these use the sample's own date_trunc, compared with every other engine's."""
    return {
        grain: {
            str(period)[:10]: float(value)
            for period, value in con.execute(
                f"SELECT date_trunc('{grain}', o.order_date), sum(i.price) FROM t_lattice_order_items i JOIN t_lattice_orders o USING (order_id) GROUP BY 1"
            ).fetchall()
        }
        for grain in GRAINS
    }


def differs(expected: dict[str, float], actual: dict[str, float]) -> str:
    expected = {str(key): value for key, value in expected.items() if value}
    actual = {key: value for key, value in actual.items() if value}
    if set(expected) != set(actual):
        return f"分组不一致：缺 {sorted(set(expected) - set(actual))[:3]}，多 {sorted(set(actual) - set(expected))[:3]}"
    wrong = [key for key in expected if abs(expected[key] - actual[key]) > TOLERANCE]
    return f"数值不一致：{wrong[0]} 期望 {expected[wrong[0]]}，实际 {actual[wrong[0]]}" if wrong else ""


def check_question(client: httpx.Client, source: str, question: str, expected: dict) -> str:
    response = client.post("/api/query", json={"question": question, "datasource_id": source, "refresh": True})
    if response.status_code != 200:
        return f"HTTP {response.status_code}：{str(response.json().get('detail'))[:200]}"
    payload = response.json()
    if payload["provider"] != "semantic":
        return f"没有走语义层：{payload['provider']}"
    if payload.get("verification", {}).get("status") != "passed":
        return f"校验未通过：{payload.get('verification')}"
    columns = payload["columns"]
    key = expected["key"]
    for metric, values in expected.items():
        if metric in {"key", "order"}:
            continue
        actual = {str(row[columns.index(key)]): float(row[columns.index(metric)] or 0) for row in payload["rows"]}
        difference = differs(values, actual)
        if difference:
            return f"{metric} {difference}"
    if "order" in expected and [str(row[columns.index(key)]) for row in payload["rows"]] != expected["order"]:
        return "排名顺序不一致"
    return ""


def check_grain(client: httpx.Client, source: str, grain: str, expected: dict[str, float]) -> str:
    response = client.post("/api/metrics/query", json={"model": "builtin", "metrics": ["total_sales"], "dimensions": [{"field": "t_lattice_orders.order_date", "grain": grain}], "datasource_id": source, "refresh": True, "limit": 1000})
    if response.status_code != 200:
        return f"HTTP {response.status_code}：{str(response.json().get('detail'))[:200]}"
    return differs(expected, {str(row[0])[:10]: float(row[1]) for row in response.json()["rows"]})


def main() -> int:
    base = f"http://127.0.0.1:{os.environ.get('LATTICE_WEB_PORT', '8787')}"
    client = httpx.Client(base_url=base, timeout=240, trust_env=False)
    with duckdb.connect(str(RUNTIME / "sample.duckdb"), read_only=True) as con:
        answers, grains = expected_answers(con), expected_by_grain(con)
    report: dict[str, dict] = {}
    failures = 0
    for source in client.get("/api/datasources").json()["items"]:
        results: dict[str, str] = {}
        for question, expected in answers.items():
            results[question] = check_question(client, source["id"], question, expected)
        for grain, expected in grains.items():
            results[f"按 {grain} 汇总"] = check_grain(client, source["id"], grain, expected)
        bad = {name: text for name, text in results.items() if text}
        failures += len(bad)
        report[source["id"]] = {"type": source["type"], "checked": len(results), "failures": bad}
        print(f"{source['id']}（{source['type']}）：{'全部一致' if not bad else ''}")
        for name, text in bad.items():
            print(f"  ✗ {name}：{text}")
    checked = sum(item["checked"] for item in report.values())
    (RUNTIME / "question-verification.json").write_text(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "checked": checked, "failures": failures, "sources": report}, ensure_ascii=False, indent=1))
    print(f"共 {checked} 项，{failures} 项不一致。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
