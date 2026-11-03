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
up as a difference rather than repeating itself on both sides. The demo tables hold one item
per order, so the one-to-many cases run on verification tables that hold several. No language model is called:
these are questions the semantic layer must answer on its own, from the wide table when it can
and from the joined tables when it cannot. Exits non-zero on any difference,
and writes the report to .runtime/webui/question-verification.json.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import decimal
import json
import os
import sys
from pathlib import Path

import duckdb
import httpx
import yaml

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
    payments = rows("t_lattice_payments", "order_id, payment_value, payment_type")

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
        "各地区的销售额和支付额": {"key": "region", "route": "wide", "total_sales": total((region(i[0]), i[3]) for i in items), "total_payment": total((region(p[0]), p[1]) for p in payments)},
        "华东地区各商品类别的销售额": {"key": "category", "route": "wide", "total_sales": total((categories[i[1]], i[3]) for i in items if region(i[0]) == "华东")},
        "各地区客户数": {"key": "region", "route": "wide", "customer_count": distinct((region(order), detail[0]) for order, detail in orders.items())},
        "2018年各类别的订单量": {"key": "category", "route": "wide", "order_count": distinct((categories[i[1]], i[0]) for i in items if orders[i[0]][1].year == 2018)},
        "华南地区销售额最高的前3个商家": {"key": "seller_name", "route": "wide", "total_sales": dict(top), "order": [name for name, _ in top]},
        "商品类别销售额": {"key": "category", "route": "wide", "total_sales": total((categories[i[1]], i[3]) for i in items)},
        # The wide table has no payment type, so these must find their way to the base tables.
        "华南地区各支付方式的支付额": {"key": "payment_type", "route": "join", "total_payment": total((p[2], p[1]) for p in payments if region(p[0]) == "华南")},
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


def one_to_many_cases(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Requests over tables with several items and payments per order, and their true answers.

    Computed row by row from the base tables. The wide model declares no grains, so these also
    check that each engine's data is enough to find them. ``naive`` is what a plain SUM over the
    wide table returns, kept to prove that the case can actually catch the mistake.
    """
    regions = dict(con.execute("SELECT customer_id, region FROM lattice_verify_customers").fetchall())
    orders = {row[0]: row[1:] for row in con.execute("SELECT order_id, customer_id, status, order_amount, order_date FROM lattice_verify_orders").fetchall()}
    items = con.execute("SELECT order_id, category, item_price FROM lattice_verify_items").fetchall()
    payments = con.execute("SELECT order_id, paid, payment_type FROM lattice_verify_payments").fetchall()

    def region(order: int) -> str:
        return regions[orders[order][0]]

    def total(pairs) -> dict[str, float]:
        result: dict[str, decimal.Decimal] = collections.defaultdict(decimal.Decimal)
        for key, value in pairs:
            result[key] += value
        return {key: float(value) for key, value in result.items()}

    amount = total((region(order), detail[2]) for order, detail in orders.items())
    sales = total((region(i[0]), i[2]) for i in items)
    paid = total((region(p[0]), p[1]) for p in payments)
    orders_by_category: dict[str, set] = collections.defaultdict(set)
    for order, category, _ in items:
        orders_by_category[category].add(order)
    per_category = {key: float(len(value)) for key, value in orders_by_category.items()}
    naive = dict(con.execute("SELECT region, sum(order_amount)::DOUBLE FROM lattice_verify_wide GROUP BY 1").fetchall())
    if all(abs(naive[key] - amount[key]) < TOLERANCE for key in amount):
        raise SystemExit("校验数据没有一对多的重复，这项核对失去了意义。")
    month = lambda order: orders[order][3].strftime("%Y-%m")  # noqa: E731
    monthly = total((month(order), detail[2]) for order, detail in orders.items())
    done = total((region(order), detail[2]) for order, detail in orders.items() if detail[1] == "已完成")
    wide, star = "builtin:verify-wide", "builtin:verify-star"
    return [
        {"name": "宽表：订单级度量按地区去重汇总", "model": wide, "metrics": ["amount"], "dimensions": [{"field": "w.region"}], "expect": {"amount": amount}},
        {"name": "宽表：明细级度量按地区汇总", "model": wide, "metrics": ["sales"], "dimensions": [{"field": "w.region"}], "expect": {"sales": sales}},
        {"name": "宽表：两种粒度合并", "model": wide, "metrics": ["sales", "amount"], "dimensions": [{"field": "w.region"}], "expect": {"sales": sales, "amount": amount}},
        {"name": "宽表：订单级度量带筛选", "model": wide, "metrics": ["amount"], "dimensions": [{"field": "w.region"}], "filters": [{"field": "w.status", "op": "=", "value": "已完成"}], "expect": {"amount": done}},
        {"name": "宽表：订单级度量按月", "model": wide, "metrics": ["amount"], "dimensions": [{"field": "w.order_date", "grain": "month"}], "expect": {"amount": monthly}, "key_length": 7},
        {"name": "宽表：去重计数按类别", "model": wide, "metrics": ["orders"], "dimensions": [{"field": "w.category"}], "expect": {"orders": per_category}},
        {"name": "宽表：订单级度量按类别必须拒绝", "model": wide, "metrics": ["amount"], "dimensions": [{"field": "w.category"}], "refused": True},
        {"name": "多表：明细与订单金额分别聚合", "model": star, "metrics": ["sales", "amount"], "dimensions": [{"field": "customers.region"}], "expect": {"sales": sales, "amount": amount}},
        {"name": "多表：三张事实表分别聚合", "model": star, "metrics": ["sales", "amount", "paid"], "dimensions": [{"field": "customers.region"}], "expect": {"sales": sales, "amount": amount, "paid": paid}},
        {"name": "多表：去重计数跨一对多", "model": star, "metrics": ["orders"], "dimensions": [{"field": "items.category"}], "expect": {"orders": per_category}},
        {"name": "多表：支付按类别必须拒绝", "model": star, "metrics": ["paid"], "dimensions": [{"field": "items.category"}], "refused": True},
    ]


def check_case(client: httpx.Client, source: str, case: dict) -> str:
    body = {"model": case["model"], "metrics": case["metrics"], "dimensions": case["dimensions"], "filters": case.get("filters", []), "datasource_id": source, "refresh": True, "limit": 1000}
    response = client.post("/api/metrics/query", json=body)
    if case.get("refused"):
        return "" if response.status_code == 400 and "重复累计" in str(response.json().get("detail")) else f"应当拒绝，实际 HTTP {response.status_code}"
    if response.status_code != 200:
        return f"HTTP {response.status_code}：{str(response.json().get('detail'))[:220]}"
    payload = response.json()
    if payload.get("verification", {}).get("status") != "passed":
        return f"校验未通过：{str(payload.get('verification'))[:220]}"
    columns = payload["columns"]
    length = case.get("key_length", 10)
    for metric, values in case["expect"].items():
        actual = {str(row[0])[:length]: float(row[columns.index(metric)] or 0) for row in payload["rows"]}
        difference = differs(values, actual)
        if difference:
            return f"{metric} {difference}"
    return ""


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
    route = (payload.get("semantic") or {}).get("route")
    if route != expected["route"]:
        return f"查询路径应为 {expected['route']}，实际 {route}"
    columns = payload["columns"]
    key = expected["key"]
    for metric, values in expected.items():
        if metric in {"key", "order", "route"}:
            continue
        actual = {str(row[columns.index(key)]): float(row[columns.index(metric)] or 0) for row in payload["rows"]}
        difference = differs(values, actual)
        if difference:
            return f"{metric} {difference}"
    if "order" in expected and [str(row[columns.index(key)]) for row in payload["rows"]] != expected["order"]:
        return "排名顺序不一致"
    return ""


def check_grain(client: httpx.Client, source: str, grain: str, expected: dict[str, float], model: str = "builtin", field: str = "t_lattice_orders.order_date") -> str:
    response = client.post("/api/metrics/query", json={"model": model, "metrics": ["total_sales"], "dimensions": [{"field": field, "grain": grain}], "datasource_id": source, "refresh": True, "limit": 1000})
    if response.status_code != 200:
        return f"HTTP {response.status_code}：{str(response.json().get('detail'))[:200]}"
    return differs(expected, {str(row[0])[:10]: float(row[1]) for row in response.json()["rows"]})


def check_golden(client: httpx.Client, path: Path) -> int:
    """Questions with answers a person vouches for: each runs against a statement they wrote.

    This is the only check here that can say a definition is the business's own. A question sent
    back is not counted as wrong, since no number was given, but the run says so: a settled
    word belongs in the dictionary before these results mean anything.
    """
    sys.path.insert(0, str(ROOT))
    from webapi.questions import compare_results

    cases = yaml.safe_load(path.read_text()) or []
    tally: collections.Counter = collections.Counter()
    for case in cases:
        source, question = case["datasource"], case["question"]
        expected = client.post(f"/api/datasources/{source}/query", json={"sql": case["expected_sql"], "refresh": True})
        if expected.status_code != 200:
            tally["invalid"] += 1
            print(f"  ✗ {question}：标准答案的 SQL 无法执行：{str(expected.json().get('detail'))[:200]}")
            continue
        answer = client.post("/api/query", json={"question": question, "datasource_id": source, "refresh": True})
        payload = answer.json()
        if answer.status_code != 200:
            tally["refused"] += 1
            print(f"  - {question}：未回答：{str(payload.get('detail'))[:200]}")
        elif payload.get("needs_clarification"):
            tally["asked"] += 1
            print(f"  ? {question}：需要先确认“{payload['clarification']['terms'][0]['term']}”的口径，请在页面确认后重跑")
        else:
            difference = compare_results(expected.json(), payload)
            if difference is None:
                tally["incomparable"] += 1
                print(f"  ? {question}：结果结构与标准答案不同，无法比较。SQL：{payload.get('sql', '')[:200]}")
            elif difference:
                tally["wrong"] += 1
                print(f"  ✗ {question}：与标准答案不一致（{difference}）。SQL：{payload.get('sql', '')[:240]}")
            else:
                tally["correct"] += 1
                print(f"  ✓ {question}")
    print(f"黄金题集共 {len(cases)} 题：正确 {tally['correct']}，答错 {tally['wrong']}，先提问 {tally['asked']}，未回答 {tally['refused']}，无法比较 {tally['incomparable']}，标准答案无效 {tally['invalid']}。")
    return 1 if tally["wrong"] or tally["invalid"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, help="a YAML file of real questions with the SQL that answers each correctly")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{os.environ.get('LATTICE_WEB_PORT', '8787')}"
    client = httpx.Client(base_url=base, timeout=240, trust_env=False)
    if args.golden:
        return check_golden(client, args.golden)
    with duckdb.connect(str(RUNTIME / "sample.duckdb"), read_only=True) as con:
        answers, grains, cases = expected_answers(con), expected_by_grain(con), one_to_many_cases(con)
    report: dict[str, dict] = {}
    failures = 0
    for source in client.get("/api/datasources").json()["items"]:
        results: dict[str, str] = {}
        for question, expected in answers.items():
            results[question] = check_question(client, source["id"], question, expected)
        for grain, expected in grains.items():
            results[f"明细按 {grain} 汇总"] = check_grain(client, source["id"], grain, expected)
            results[f"宽表按 {grain} 汇总"] = check_grain(client, source["id"], grain, expected, "builtin:wide", "sales_wide.order_date")
        for case in cases:
            results[f"一对多｜{case['name']}"] = check_case(client, source["id"], case)
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
