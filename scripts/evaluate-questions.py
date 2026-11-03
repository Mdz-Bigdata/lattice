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
"""Measure 智能问数 on many questions whose answers are known, and count the wrong ones.

Questions are generated from metrics, dimensions, filters and ways of asking, over data with the
traps real warehouses have: orders with several items and payments, orders with no items, and
orders whose customer is missing. Expected answers come from the rows in plain Python, never
through SQL. What matters is the wrong count: a refusal or a question back costs the asker a
moment, and a wrong number costs them a decision.

Runs in-process on a throwaway copy, so it touches no real data. Without --model only the
platform's own parsing is exercised; with it, questions that need interpreting go to the
configured model (synthetic data only). Exits non-zero if any answer was wrong.
"""

from __future__ import annotations

import argparse
import collections
import decimal
import json
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TOLERANCE = 0.011

METRICS = {
    "total_sales": ["销售额", "成交额", "GMV", "营业额", "销售金额", "销售总额"],
    "order_count": ["订单量", "订单数", "单量", "订单数量"],
    "avg_order_value": ["客单价", "平均订单金额", "笔单价"],
    "total_payment": ["支付额", "支付金额", "实付金额", "付款金额"],
    "customer_count": ["客户数", "买家数", "下单客户数"],
    "item_count": ["明细数", "销售件数", "商品件数"],
}
DIMENSIONS = {
    "region": ["地区", "区域", "大区"],
    "category": ["类别", "品类", "商品类别", "分类"],
    "seller": ["商家", "卖家", "店铺"],
    "status": ["订单状态", "状态"],
    "payment_type": ["支付方式", "付款方式"],
    "month": ["每月", "按月", "月度"],
    "quarter": ["每季度", "按季度"],
    "year": ["每年", "按年"],
}
FILTERS = {
    "none": ("", {}),
    "east": ("华东地区", {"region": "华东"}),
    "south": ("华南", {"region": "华南"}),
    "digital": ("数码家电类别", {"category": "数码家电"}),
    "done": ("已完成", {"status": "已完成"}),
    "y2018": ("2018年", {"from": "2018-01-01", "to": "2018-12-31"}),
    "m201803": ("2018年3月", {"from": "2018-03-01", "to": "2018-03-31"}),
    "q1": ("2018年第一季度", {"from": "2018-01-01", "to": "2018-03-31"}),
}
GRAINS = {"month", "quarter", "year"}
#: Ways of asking. The platform parses the first group itself; the rest need interpreting.
PLAIN = ["{f}各{d}的{m}", "{f}按{d}统计{m}", "帮我看一下{f}各{d}的{m}", "{f}{d}{m}", "查询{f}各{d}{m}分别是多少"]
PLAIN_TIME = ["{f}{d}的{m}", "{d}{f}{m}", "统计{f}{d}{m}"]
NO_DIMENSION = ["{f}{m}是多少", "{f}的{m}", "统计一下{f}{m}", "{f}{m}有多少"]
RANKED = ["{f}{m}最高的前3个{d}", "{f}{m}排名前3的{d}"]
INTERPRET = ["{f}不同{d}的{m}对比", "{m}按{d}拆一下{f}", "想了解{f}每个{d}贡献的{m}", "把{f}{m}按{d}列出来给我"]
INTERPRET_NONE = ["{f}一共有多少{m}", "{f}整体{m}水平"]


def seed_traps(con) -> None:
    from webapi.query import WIDE_SALES_SQL

    con.execute("INSERT INTO t_lattice_customers VALUES (61, '客户 61', '华东')")
    # Several items and payments, across categories and sellers.
    con.execute("INSERT INTO t_lattice_orders VALUES (9001, 1, DATE '2018-03-05', '已完成'), (9002, 2, DATE '2018-03-20', '已取消'), (9003, 3, DATE '2018-04-02', '已完成')")
    con.execute("INSERT INTO t_lattice_order_items VALUES (9001, 1, 1, 100.00, DATE '2018-03-05'), (9001, 2, 2, 50.00, DATE '2018-03-05'), (9001, 4, 3, 25.50, DATE '2018-03-05'), (9002, 4, 1, 30.00, DATE '2018-03-20'), (9002, 8, 2, 70.00, DATE '2018-03-20')")
    con.execute("INSERT INTO t_lattice_payments VALUES (9001, 90.00, '在线支付'), (9001, 85.50, '银行转账'), (9003, 44.00, '银行转账')")
    # An order with no items is on orders but cannot be in a table built from items.
    # An order whose customer is missing keeps its sales in a group of its own.
    con.execute("INSERT INTO t_lattice_orders VALUES (9004, 999, DATE '2018-05-06', '已完成')")
    con.execute("INSERT INTO t_lattice_order_items VALUES (9004, 3, 3, 40.00, DATE '2018-05-06')")
    con.execute("INSERT INTO t_lattice_payments VALUES (9004, 40.00, '在线支付')")
    con.execute(WIDE_SALES_SQL)


class Truth:
    """Answers computed one row at a time from the base tables."""

    def __init__(self, con):
        fetch = lambda sql: con.execute(sql).fetchall()  # noqa: E731
        self.orders = {row[0]: {"customer": row[1], "date": row[2], "status": row[3]} for row in fetch("SELECT order_id, customer_id, order_date, status FROM t_lattice_orders")}
        self.regions = dict(fetch("SELECT customer_id, region FROM t_lattice_customers"))
        self.categories = dict(fetch("SELECT product_id, category FROM t_lattice_products"))
        self.sellers = dict(fetch("SELECT seller_id, seller_name FROM t_lattice_sellers"))
        self.items = fetch("SELECT order_id, product_id, seller_id, price FROM t_lattice_order_items")
        self.payments = fetch("SELECT order_id, payment_value, payment_type FROM t_lattice_payments")

    def region(self, order):
        return str(self.regions.get(self.orders[order]["customer"]))

    def period(self, order, grain):
        date = self.orders[order]["date"]
        if grain == "month":
            return date.strftime("%Y-%m")
        if grain == "year":
            return date.strftime("%Y")
        return f"{date.year}-{(date.month - 1) // 3 * 3 + 1:02d}"

    def order_key(self, order, dim):
        if dim == "region":
            return self.region(order)
        if dim == "status":
            return self.orders[order]["status"]
        if dim in GRAINS:
            return self.period(order, dim)
        return ""

    def compute(self, metric: str, dim: str | None, filters: dict):
        """{group: value}, or None when no correct number exists and refusal is the right answer."""
        def order_passes(order):
            info = self.orders[order]
            if "region" in filters and self.region(order) != filters["region"]:
                return False
            if "status" in filters and info["status"] != filters["status"]:
                return False
            if "from" in filters and not filters["from"] <= info["date"].isoformat() <= filters["to"]:
                return False
            return True

        def item_passes(item):
            return ("category" not in filters or self.categories[item[1]] == filters["category"]) and order_passes(item[0])

        items = [item for item in self.items if item_passes(item)]
        orders = {order for order in self.orders if order_passes(order)}
        if "category" in filters:
            orders = {item[0] for item in items}
        item_level = dim in {"category", "seller"}

        def item_key(item):
            if dim == "category":
                return self.categories[item[1]]
            if dim == "seller":
                return self.sellers[item[2]]
            return self.order_key(item[0], dim)

        result: dict = collections.defaultdict(decimal.Decimal)
        if dim == "payment_type":
            if metric == "total_payment":
                for order, value, kind in self.payments:
                    if order in orders:
                        result[kind] += value
                return {key: float(value) for key, value in result.items()}
            if metric in {"order_count", "customer_count"}:
                groups: dict = collections.defaultdict(set)
                for order, _, kind in self.payments:
                    if order in orders:
                        groups[kind].add(order if metric == "order_count" else self.orders[order]["customer"])
                return {key: float(len(value)) for key, value in groups.items()}
            return None  # Sales repeat over an order's payments: no correct split exists.
        if metric == "total_sales" or metric == "item_count":
            for item in items:
                result[item_key(item)] += item[3] if metric == "total_sales" else 1
        elif metric == "avg_order_value":
            sums: dict = collections.defaultdict(decimal.Decimal)
            seen: dict = collections.defaultdict(set)
            for item in items:
                sums[item_key(item)] += item[3]
                seen[item_key(item)].add(item[0])
            return {key: round(float(sums[key]) / len(seen[key]), 2) for key in sums}
        elif metric in {"order_count", "customer_count"}:
            groups = collections.defaultdict(set)
            source = [(item_key(item), item[0]) for item in items] if item_level else [(self.order_key(order, dim), order) for order in orders]
            for key, order in source:
                groups[key].add(order if metric == "order_count" else self.orders[order]["customer"])
            return {key: float(len(value)) for key, value in groups.items()}
        elif metric == "total_payment":
            if item_level:
                return None  # One payment, several categories: any split counts it twice.
            for order, value, _ in self.payments:
                if order in orders:
                    result[self.order_key(order, dim)] += value
        return {key: float(value) for key, value in result.items()}


def generate(count: int, rng: random.Random, interpret: bool) -> list[dict]:
    cases: list[dict] = []
    seen: set[str] = set()
    attempts = 0
    while len(cases) < count and attempts < count * 50:
        attempts += 1
        metric = rng.choice(list(METRICS))
        dim = rng.choice([None, *DIMENSIONS])
        filter_key = rng.choice(list(FILTERS))
        phrase, filters = FILTERS[filter_key]
        if dim and dim in filters:
            continue
        if dim == "payment_type" and "category" in filters:
            continue
        ranked = False
        if dim is None:
            template = rng.choice(INTERPRET_NONE if interpret else NO_DIMENSION)
        elif interpret:
            template = rng.choice(INTERPRET)
        elif dim in GRAINS:
            template = rng.choice(PLAIN_TIME)
        else:
            ranked = rng.random() < 0.2
            template = rng.choice(RANKED if ranked else PLAIN)
        question = template.format(f=phrase, d=rng.choice(DIMENSIONS[dim]) if dim else "", m=rng.choice(METRICS[metric]))
        if question in seen:
            continue
        seen.add(question)
        cases.append({"question": question, "metric": metric, "dim": dim, "filters": filters, "ranked": ranked})
    return cases


def normalize_key(value, dim) -> str:
    text = str(value)
    if dim in {"month", "quarter"}:
        return text[:7]
    if dim == "year":
        return text[:4]
    return text


def judge(case: dict, payload: dict, status: int, truth: Truth) -> tuple[str, str]:
    expected = truth.compute(case["metric"], case["dim"], case["filters"])
    if status != 200:
        detail = str(payload.get("detail"))[:160]
        return ("refused_correctly", detail) if expected is None else ("refused", detail)
    if payload.get("needs_clarification"):
        return "asked", str(payload["clarification"]["terms"][0]["term"])
    if expected is None:
        return "wrong", f"应当拒绝，却返回了 {payload.get('rows')[:3]} SQL={payload.get('sql', '')[:200]}"
    columns = payload["columns"]
    numeric = [
        index for index in range(len(columns))
        if payload["rows"] and all(isinstance(row[index], (int, float)) or row[index] is None for row in payload["rows"])
    ]
    labels = [index for index in range(len(columns)) if index not in numeric]
    if case["dim"] is None:
        # A single number, possibly with extra columns only a model would add.
        wanted = next(iter(expected.values()), 0.0) if expected else 0.0
        values = [float(row[index] or 0) for row in payload["rows"] for index in numeric]
        if not values and not wanted:
            return "correct", ""
        if len(payload["rows"]) == 1 and any(abs(value - wanted) <= TOLERANCE for value in values):
            return "correct", ""
        return "wrong", f"期望 {wanted}，实际 {payload['rows'][:3]} SQL={payload.get('sql', '')[:220]}"
    semantic = payload.get("semantic") or {}
    alias = next((column["name"] for column in semantic.get("columns", []) if column.get("metric") == case["metric"]), None)
    metric_index = columns.index(alias) if alias in columns else (numeric[-1] if numeric else None)
    if metric_index is None:
        return ("correct", "") if not expected else ("wrong", f"没有返回数值：{payload['rows'][:3]}")
    if case["dim"] in GRAINS and semantic:
        labels = [0]
    if case["dim"] in GRAINS and not semantic:
        labels = [0]
    actual = {}
    for row in payload["rows"]:
        key = normalize_key(row[labels[0]] if labels else "", case["dim"])
        if key in actual:
            return "wrong", f"分组 {key} 出现多次：{payload['rows'][:4]} SQL={payload.get('sql', '')[:200]}"
        actual[key] = float(row[metric_index] or 0)
    wanted = {str(key): value for key, value in expected.items() if value}
    actual = {key: value for key, value in actual.items() if value}
    if case["ranked"]:
        top = sorted(wanted.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
        wanted_values = [round(value, 2) for _, value in top]
        got = sorted(round(value, 2) for value in actual.values())
        if got != sorted(wanted_values):
            return "wrong", f"排名期望 {top}，实际 {actual} SQL={payload.get('sql', '')[:220]}"
        return "correct", ""
    if set(wanted) != set(actual):
        return "wrong", f"分组不一致：缺 {sorted(set(wanted) - set(actual))[:4]} 多 {sorted(set(actual) - set(wanted))[:4]} SQL={payload.get('sql', '')[:220]}"
    bad = [key for key in wanted if abs(wanted[key] - actual[key]) > TOLERANCE]
    if bad:
        return "wrong", f"{bad[0]} 期望 {wanted[bad[0]]}，实际 {actual[bad[0]]} SQL={payload.get('sql', '')[:220]}"
    return "correct", ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000, help="questions the platform parses itself")
    parser.add_argument("--model", type=int, default=0, help="questions that need interpreting, sent to the configured model")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--report", default=str(ROOT / ".runtime" / "webui" / "question-evaluation.json"))
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="lattice-eval-"))
    os.environ.update({"LATTICE_METADATA_DSN": "sqlite:///:memory:", "LATTICE_METADATA_SCHEDULER": "0", "LATTICE_AUTH": "0", "LATTICE_AUTH_DB": str(workspace / "auth.sqlite")})
    (workspace / "web").mkdir()
    settings = ROOT / ".runtime" / "webui" / "llm.json"
    if args.model and settings.is_file():
        shutil.copy(settings, workspace / "web" / "llm.json")

    import duckdb
    from fastapi.testclient import TestClient

    from webapi import app as app_module

    app_module.RUNTIME = workspace / "web"
    app_module.POLARIS_RUNTIME = workspace / "polaris"
    app_module.ENGINES_RUNTIME = workspace / "engines"
    rng = random.Random(args.seed)
    tallies: dict[str, collections.Counter] = {}
    examples: dict[str, list] = collections.defaultdict(list)
    started = time.monotonic()
    try:
        with TestClient(app_module.app, base_url="http://127.0.0.1:8787") as client:
            store = client.app.state.store
            with duckdb.connect(str(store.db)) as con:
                seed_traps(con)
            with duckdb.connect(str(store.db), read_only=True) as con:
                truth = Truth(con)
            for name, cases in (("parsed", generate(args.count, rng, False)), ("interpreted", generate(args.model, rng, True))):
                if not cases:
                    continue
                tally: collections.Counter = collections.Counter()
                for case in cases:
                    response = client.post("/api/query", json={"question": case["question"], "datasource_id": "local-sample"})
                    outcome, detail = judge(case, response.json(), response.status_code, truth)
                    route = (response.json().get("semantic") or {}).get("route", response.json().get("provider", "")) if response.status_code == 200 else ""
                    tally[outcome] += 1
                    tally[f"route:{route}"] += 1 if outcome == "correct" else 0
                    if outcome in {"wrong", "refused"} and len(examples[f"{name}:{outcome}"]) < 40:
                        examples[f"{name}:{outcome}"].append({"question": case["question"], "metric": case["metric"], "dim": case["dim"], "filters": case["filters"], "detail": detail})
                tallies[name] = tally
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    wrong = 0
    for name, tally in tallies.items():
        total = sum(count for key, count in tally.items() if not key.startswith("route:"))
        wrong += tally["wrong"]
        routes = {key[6:]: count for key, count in tally.items() if key.startswith("route:") and count}
        print(f"[{name}] 共 {total} 题：正确 {tally['correct']}，正确拒绝 {tally['refused_correctly']}，先提问 {tally['asked']}，未能回答 {tally['refused']}，答错 {tally['wrong']}；正确答案的路径 {routes}")
    for key, items in examples.items():
        print(f"--- {key}（前 {min(len(items), 12)} 条）")
        for item in items[:12]:
            print(f"  {item['question']} | {item['metric']} / {item['dim']} / {item['filters']} | {item['detail']}")
    print(f"用时 {time.monotonic() - started:.1f} 秒。答错 {wrong} 题。")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps({"tallies": {key: dict(value) for key, value in tallies.items()}, "examples": examples}, ensure_ascii=False, indent=1))
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
