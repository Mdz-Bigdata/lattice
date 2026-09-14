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

"""Real, bounded SQL queries over explicitly labeled sample data plus history."""

import datetime as dt
import decimal
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from .connectors import chart_hint, json_value

TABLES = {
    "t_lattice_order_items": "订单明细",
    "t_lattice_orders": "订单",
    "t_lattice_customers": "客户",
    "t_lattice_products": "商品",
    "t_lattice_sellers": "商家",
    "t_lattice_payments": "支付",
}
QUESTIONS = [
    "月度销售额趋势",
    "商品类别销售额",
    "商家销售额排名",
    "每月订单量",
    "平均订单金额",
]
SOURCE = "本地示例数据 · DuckDB"
MONTHLY = [
    1108542.12,
    1226917.44,
    1308249.36,
    1766703.26,
    1596821.17,
    1680547.11,
    1632740.95,
    2057918.34,
    1913886.72,
    2164812.51,
    1732028.65,
]
MONTH_SQL = """SELECT strftime(shipping_limit_date, '%Y-%m') AS sales_month,
       round(sum(price), 2) AS monthly_sales
FROM t_lattice_order_items
GROUP BY strftime(shipping_limit_date, '%Y-%m')
ORDER BY sales_month NULLS FIRST"""
HISTORY_LIMIT = 50


class QueryStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "sample.duckdb"
        self.history_db = directory / "history.sqlite"
        self._initialize()

    def _initialize(self):
        with duckdb.connect(str(self.db)) as con:
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='t_lattice_order_items'"
            ).fetchone()[0]
            if not exists:
                con.execute("BEGIN")
                con.execute("""CREATE TABLE t_lattice_order_items(
                    order_id INTEGER, product_id INTEGER, seller_id INTEGER,
                    price DECIMAL(16,2), shipping_limit_date DATE)""")
                rows = []
                for month, total in enumerate(MONTHLY):
                    date = dt.date(2017 + (7 + month) // 12, (7 + month) % 12 + 1, 1)
                    cents = round(total * 100)
                    for i in range(100):
                        price = decimal.Decimal(cents // 100 + (i < cents % 100)) / 100
                        rows.append(
                            (
                                month * 100 + i + 1,
                                i % 20 + 1,
                                i % 10 + 1,
                                price,
                                date + dt.timedelta(days=i % 27),
                            )
                        )
                con.executemany(
                    "INSERT INTO t_lattice_order_items VALUES (?, ?, ?, ?, ?)", rows
                )
                con.execute("""CREATE TABLE t_lattice_orders AS SELECT order_id,
                    (order_id % 60 + 1)::INTEGER AS customer_id,
                    shipping_limit_date AS order_date, '已完成' AS status
                    FROM t_lattice_order_items""")
                con.execute("""CREATE TABLE t_lattice_customers AS SELECT
                    i::INTEGER AS customer_id, '客户 ' || i AS customer_name,
                    CASE WHEN i % 3 = 0 THEN '华东' WHEN i % 3 = 1 THEN '华南' ELSE '华北' END AS region
                    FROM range(1, 61) t(i)""")
                con.execute("""CREATE TABLE t_lattice_products AS SELECT
                    i::INTEGER AS product_id, '商品 ' || i AS product_name,
                    CASE i % 4 WHEN 0 THEN '数码家电' WHEN 1 THEN '家居生活'
                    WHEN 2 THEN '服饰鞋包' ELSE '食品饮料' END AS category
                    FROM range(1, 21) t(i)""")
                con.execute("""CREATE TABLE t_lattice_sellers AS SELECT
                    i::INTEGER AS seller_id, '商家 ' || i AS seller_name
                    FROM range(1, 11) t(i)""")
                con.execute(
                    """CREATE TABLE t_lattice_payments AS SELECT order_id,
                    price AS payment_value, '在线支付' AS payment_type FROM t_lattice_order_items"""
                )
                con.execute("COMMIT")
        with sqlite3.connect(self.history_db) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS query_history(id TEXT PRIMARY KEY, created_at TEXT, result TEXT)"
            )

    def connect(self):
        return duckdb.connect(
            str(self.db),
            read_only=True,
            config={
                "enable_external_access": "false",
                "allow_unsigned_extensions": "false",
                "memory_limit": "256MB",
                "threads": "2",
            },
        )

    def metadata(self):
        with self.connect() as con:
            return [
                {
                    "name": name,
                    "label": label,
                    "columns": [
                        {"name": row[0], "type": row[1]}
                        for row in con.execute(f"DESCRIBE {name}").fetchall()
                    ],
                    "rows": con.execute(f"SELECT count(*) FROM {name}").fetchone()[0],
                }
                for name, label in TABLES.items()
            ]

    @staticmethod
    def translate(question: str) -> tuple[str, str]:
        q = question.lower()
        if any(k in q for k in ("每月订单", "月订单", "订单量")):
            return (
                "每月订单量",
                """SELECT strftime(order_date, '%Y-%m') AS order_month,
                count(*) AS order_count FROM t_lattice_orders
                GROUP BY strftime(order_date, '%Y-%m')
                ORDER BY order_month NULLS FIRST""",
            )
        if any(k in q for k in ("类别", "分类", "category")):
            return (
                "商品类别销售额",
                """SELECT p.category AS category, round(sum(i.price), 2) AS sales
                FROM t_lattice_order_items i
                JOIN t_lattice_products p ON p.product_id = i.product_id
                GROUP BY p.category ORDER BY sales DESC NULLS FIRST""",
            )
        if any(k in q for k in ("商家", "seller")):
            return (
                "商家销售额排名",
                """SELECT s.seller_name AS seller_name, round(sum(i.price), 2) AS sales
                FROM t_lattice_order_items i
                JOIN t_lattice_sellers s ON s.seller_id = i.seller_id
                GROUP BY s.seller_name ORDER BY sales DESC NULLS FIRST""",
            )
        if any(k in q for k in ("平均", "客单", "average")):
            return (
                "平均订单金额",
                "SELECT round(avg(price), 2) AS average_order_value FROM t_lattice_order_items",
            )
        if any(k in q for k in ("月", "趋势", "monthly", "trend")):
            return "月度销售额趋势", MONTH_SQL
        raise ValueError(
            "当前为本地规则问数，支持："
            + "、".join(QUESTIONS)
            + "。其他分析请使用 SQL 工作台，或配置模型后再提问。"
        )

    @staticmethod
    def safe_sql(sql: str) -> str:
        statements = sqlglot.parse(sql, read="duckdb")
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("仅支持一条只读 SELECT / WITH 查询。")
        tree = statements[0]
        if tree.find(exp.Into):
            raise ValueError("不允许导出或写入数据。")
        ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier) or table.db or table.catalog:
                raise ValueError(
                    "只允许读取本地示例业务表，不允许文件、外部数据或跨库访问。"
                )
            if table.name not in TABLES and table.name not in ctes:
                raise ValueError(f"未知或禁止访问的数据表：{table.name}")
        # Table-valued functions may occur outside a Table node (e.g. UNNEST).
        for node in tree.walk():
            if isinstance(node, (exp.Command, exp.DDL, exp.DML)):
                raise ValueError("只允许只读查询。")
        return tree.sql(dialect="duckdb")

    def execute_sql(self, sql: str) -> dict:
        """Run one guarded query over the sample tables; rows are JSON-safe."""
        started = time.monotonic()
        safe = self.safe_sql(sql)
        with self.connect() as con:
            timer = threading.Timer(8, con.interrupt)
            timer.start()
            try:
                result = con.execute(f"SELECT * FROM ({safe}) AS result LIMIT 1001")
                columns = [c[0] for c in result.description]
                rows = result.fetchall()
            finally:
                timer.cancel()
        truncated = len(rows) > 1000
        return {
            "columns": columns,
            "rows": [[json_value(value) for value in row] for row in rows[:1000]],
            "truncated": truncated,
            "sql": safe,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }

    def query(self, question: str | None = None, sql: str | None = None):
        """Rules-based or SQL query over the sample data (recorded in history)."""
        started = time.monotonic()
        title, proposed = (
            ("SQL 查询结果", sql) if sql else self.translate(question or "")
        )
        result = self.execute_sql(proposed)
        payload = {
            "id": str(uuid.uuid4()),
            "title": title,
            "sql": result["sql"],
            "columns": result["columns"],
            "rows": result["rows"],
            "chart": chart_hint(result["columns"], result["rows"]),
            "steps": [
                "识别问题中的维度与指标，匹配本地示例表。",
                "生成只读 SQL 并执行安全检查。",
                f"在 DuckDB 中执行，返回 {len(result['rows'])} 行"
                + ("（已截断至 1000 行）。" if result["truncated"] else "。"),
            ],
            "source": SOURCE,
            "provider": "rules" if not sql else "sql",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "truncated": result["truncated"],
            "datasource_id": "local-sample",
            "datasource_name": SOURCE,
            "dialect": "duckdb",
        }
        self.record(payload)
        return payload

    def record(self, payload: dict) -> None:
        with sqlite3.connect(self.history_db) as con:
            con.execute(
                "INSERT INTO query_history VALUES (?, ?, ?)",
                (
                    payload["id"],
                    payload["created_at"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            con.execute(
                "DELETE FROM query_history WHERE id NOT IN "
                f"(SELECT id FROM query_history ORDER BY created_at DESC LIMIT {HISTORY_LIMIT})"
            )

    @staticmethod
    def _json_value(value):
        return json_value(value)

    def history(self):
        with sqlite3.connect(self.history_db) as con:
            return [
                json.loads(row[0])
                for row in con.execute(
                    f"SELECT result FROM query_history ORDER BY created_at DESC LIMIT {HISTORY_LIMIT}"
                )
            ]
