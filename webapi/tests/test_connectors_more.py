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

"""Oracle, SQL Server, MongoDB, Elasticsearch and Kafka connectors, driven by fakes.

The drivers are optional and no server is available in CI, so every test injects
a stand-in module through ``sys.modules`` (or an ``httpx.MockTransport`` for the
REST-based Elasticsearch connector) and checks what the connector asks of it and
what it makes of the answer.
"""

import datetime as dt
import json
import sys
import types

import httpx
import pytest

from webapi.connectors import (
    ConnectorError,
    ElasticsearchConnector,
    KafkaConnector,
    MongoDBConnector,
    OracleConnector,
    SqlServerConnector,
    flatten_document,
    infer_columns,
    rows_to_arrow,
)
from webapi.quality_metrics import MetricError, build, make_context


# ----- shared helpers ------------------------------------------------------------------
def test_snapshot_rows_keep_types_that_agree_and_stringify_the_rest():
    rows = [
        {"n": 1, "f": 1.5, "b": True, "s": "a", "t": dt.datetime(2026, 1, 1, 12, 0), "mixed": 1, "obj": {"x": 1}},
        {"n": 2, "f": 2, "b": None, "s": None, "t": None, "mixed": "two"},
    ]
    table = rows_to_arrow([flatten_document(row) for row in rows])
    types_by_name = {name: str(table.schema.field(name).type) for name in table.column_names}
    assert types_by_name == {"n": "int64", "f": "double", "b": "bool", "s": "string", "t": "timestamp[us]", "mixed": "string", "obj.x": "int64"}
    assert table.column("mixed").to_pylist() == ["1", "two"]
    columns = {item["name"]: item for item in infer_columns([flatten_document(row) for row in rows])}
    assert columns["mixed"]["type"] == "integer | string" and columns["mixed"]["nullable"] is False
    assert columns["obj.x"]["nullable"] is True and columns["b"]["nullable"] is True
    assert flatten_document({"a": {"b": {"c": 1}}, "l": [1, 2]}) == {"a.b": '{"c": 1}', "l": "[1, 2]"}


def test_missing_driver_is_reported_as_configuration(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", None)
    with pytest.raises(ConnectorError, match="缺少驱动 oracledb"):
        OracleConnector({"host": "h", "port": 1521, "user": "u", "service_name": "orcl"}).connect()


# ----- Oracle and SQL Server ----------------------------------------------------------
class FakeCursor:
    def __init__(self, answers):
        self.answers = answers
        self.executed = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle, (columns, rows) in self.answers:
            if needle in sql:
                self.description = [(name,) for name in columns]
                self._rows = list(rows)
                return
        self.description = [("value",)]
        self._rows = [[1]]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, limit):
        return list(self._rows[:limit])

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True

    def cancel(self):
        pass


def install_dbapi(monkeypatch, name, cursor, calls):
    module = types.ModuleType(name)

    def connect(**kwargs):
        calls.append(kwargs)
        return FakeConnection(cursor)

    module.connect = connect
    monkeypatch.setitem(sys.modules, name, module)


def test_oracle_connector_reads_the_dictionary_views_and_bounds_rows(monkeypatch):
    cursor = FakeCursor([
        ("v$version", (["banner"], [["Oracle Database 19c"]])),
        ("all_tables WHERE owner", (["n"], [[3]])),
        ("all_tab_columns", (["column_name", "data_type", "nullable"], [["ID", "NUMBER", "N"], ["NAME", "VARCHAR2", "Y"]])),
        ("all_cons_columns", (["column_name"], [["ID"]])),
        ("count(*) FROM", (["n"], [[42]])),
        ("FETCH FIRST", (["ID", "NAME"], [[1, "a"], [2, "b"], [3, "c"]])),
    ])
    calls = []
    install_dbapi(monkeypatch, "oracledb", cursor, calls)
    connector = OracleConnector({"host": "db", "port": 1521, "user": "app", "password": "pw", "service_name": "ORCLPDB1"})
    assert connector.default_schema() == "APP" and connector.summary() == "db:1521 / ORCLPDB1"
    probe = connector.test()
    assert probe["ok"] and probe["server_version"] == "Oracle Database 19c" and probe["table_count"] == 3
    assert calls[0]["dsn"] == "db:1521/ORCLPDB1" and calls[0]["user"] == "app"
    detail = connector.table("APP", "ORDERS")
    assert detail["rows"] == 42 and detail["columns"][0] == {"name": "ID", "type": "NUMBER", "nullable": False, "primary_key": True}
    result = connector.query("SELECT id, name FROM app.orders", 2)
    assert result["rows"] == [[1, "a"], [2, "b"]] and result["truncated"] is True
    assert cursor.executed[-1][0] == 'SELECT * FROM (\nSELECT id, name FROM app.orders\n) lattice_q FETCH FIRST 3 ROWS ONLY'
    with pytest.raises(ConnectorError):
        connector.query("DELETE FROM app.orders")


def test_sql_server_connector_uses_top_and_information_schema(monkeypatch):
    cursor = FakeCursor([
        ("@@VERSION", (["v"], [["Microsoft SQL Server 2022\n\tCopyright"]])),
        ("INFORMATION_SCHEMA.TABLES", (["n"], [[5]])),
        ("INFORMATION_SCHEMA.COLUMNS", (["c", "t", "n"], [["id", "int", "NO"], ["note", "nvarchar", "YES"]])),
        ("KEY_COLUMN_USAGE", (["c"], [["id"]])),
        ("count(*) FROM", (["n"], [[7]])),
        ("TOP (", (["id"], [[1]])),
        ("sys.sql_modules", (["definition"], [["CREATE VIEW dbo.v AS SELECT id FROM dbo.t"]])),
    ])
    calls = []
    install_dbapi(monkeypatch, "pymssql", cursor, calls)
    connector = SqlServerConnector({"host": "db", "port": 1433, "user": "sa", "password": "pw", "database": "shop"})
    probe = connector.test()
    assert probe["server_version"] == "Microsoft SQL Server 2022" and probe["table_count"] == 5
    assert calls[0]["database"] == "shop" and calls[0]["port"] == "1433"
    detail = connector.table("dbo", "t")
    assert detail["rows"] == 7 and detail["columns"][1]["nullable"] is True and detail["columns"][0]["primary_key"] is True
    assert connector.query("SELECT id FROM dbo.t", 5)["rows"] == [[1]]
    assert cursor.executed[-1][0] == "SELECT TOP (6) * FROM (\nSELECT id FROM dbo.t\n) AS lattice_q"
    assert connector.view_definition("dbo", "v").startswith("CREATE VIEW")


def test_oracle_and_tsql_quality_sql_use_their_own_functions():
    oracle = build("table_freshness", make_context("oracle", "APP", "ORDERS", "UPDATED_AT", {"interval_value": 2, "interval_unit": "hour"}))
    assert "SYSTIMESTAMP - INTERVAL '2' HOUR" in oracle.invalidate_sql
    tsql = build("table_freshness", make_context("tsql", "dbo", "orders", "updated_at", {"interval_value": 1, "interval_unit": "day"}))
    assert 'DATEADD(DAY, -1, SYSDATETIME())' in tsql.invalidate_sql
    assert "NOT REGEXP_LIKE" in build("column_match_regex", make_context("oracle", "APP", "T", "C", {"regexp": "^a"})).invalidate_sql
    with pytest.raises(MetricError, match="正则表达式"):
        build("column_match_regex", make_context("tsql", "dbo", "t", "c", {"regexp": "^a"}))


# ----- MongoDB --------------------------------------------------------------------------
class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query, limit=0):
        return iter(self.documents[:limit] if limit else self.documents)

    def estimated_document_count(self):
        return len(self.documents)


class FakeDatabase:
    def __init__(self, collections):
        self.collections = collections

    def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        return self.collections.get(name, FakeCollection([]))


class FakeMongoClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.databases = {
            "shop": FakeDatabase({
                "orders": FakeCollection([
                    {"_id": "o1", "amount": 10, "customer": {"id": 1, "name": "A"}, "tags": ["x"]},
                    {"_id": "o2", "amount": 5.5, "customer": {"id": 2, "name": "B"}},
                    {"_id": "o3", "amount": None, "customer": {"id": 3}},
                ]),
            }),
            "admin": FakeDatabase({}),
        }
        FakeMongoClient.instances.append(self)

    def server_info(self):
        return {"version": "7.0.5"}

    def list_database_names(self):
        return list(self.databases)

    def __getitem__(self, name):
        return self.databases[name]

    def close(self):
        self.closed = True


def test_mongodb_connector_infers_fields_and_queries_a_document_snapshot(monkeypatch):
    module = types.ModuleType("pymongo")
    module.MongoClient = FakeMongoClient
    monkeypatch.setitem(sys.modules, "pymongo", module)
    connector = MongoDBConnector({"host": "m", "port": 27017, "user": "app", "password": "pw", "database": "shop", "sample_rows": 2})
    probe = connector.test()
    assert probe["server_version"] == "7.0.5" and probe["table_count"] == 1
    assert FakeMongoClient.instances[-1].kwargs["authSource"] == "admin" and FakeMongoClient.instances[-1].closed
    assert [item["name"] for item in connector.schemas()] == ["shop"]
    assert connector.tables("shop") == [{"schema": "shop", "name": "orders", "kind": "collection", "rows": 3, "comment": ""}]
    detail = connector.table("shop", "orders")
    names = {item["name"]: item for item in detail["columns"]}
    assert detail["rows"] == 3 and names["amount"]["type"] == "double | integer" and names["customer.name"]["nullable"] is True
    result = connector.query('SELECT "customer.name" AS name, amount FROM shop.orders ORDER BY amount DESC')
    assert result["columns"] == ["name", "amount"] and result["rows"] == [["A", 10.0], ["B", 5.5]]
    total = connector.query("SELECT count(*) AS n FROM orders")
    assert total["rows"] == [[2]]  # the snapshot holds sample_rows documents
    with pytest.raises(ConnectorError, match="未找到数据库"):
        connector.query("SELECT * FROM other.orders")


# ----- Elasticsearch ----------------------------------------------------------------------
def elastic_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/":
        return httpx.Response(200, json={"cluster_name": "lattice", "version": {"number": "8.12.0"}})
    if path == "/_cat/indices":
        return httpx.Response(200, json=[
            {"index": "orders", "docs.count": "2", "store.size": "10kb"},
            {"index": ".kibana", "docs.count": "1", "store.size": "1kb"},
        ])
    if path == "/orders/_mapping":
        return httpx.Response(200, json={"orders": {"mappings": {"properties": {"amount": {"type": "float"}, "customer": {"properties": {"name": {"type": "keyword"}}}}}}})
    if path == "/orders/_count":
        return httpx.Response(200, json={"count": 2})
    if path == "/orders/_search":
        body = json.loads(request.content)
        assert body["size"] == 2 and request.headers.get("Authorization") == "ApiKey k3y"
        return httpx.Response(200, json={"hits": {"hits": [
            {"_id": "1", "_source": {"amount": 3.5, "customer": {"name": "A"}}},
            {"_id": "2", "_source": {"amount": 1.0, "customer": {"name": "B"}}},
        ]}})
    if path.startswith("/missing/"):
        return httpx.Response(404, json={"error": {"reason": "no such index [missing]"}})
    return httpx.Response(404, json={})


def test_elasticsearch_connector_maps_indices_to_tables(monkeypatch):
    monkeypatch.setattr(ElasticsearchConnector, "_transport", httpx.MockTransport(elastic_handler))
    connector = ElasticsearchConnector({"host": "es", "port": 9200, "api_key": "k3y", "sample_rows": 2})
    probe = connector.test()
    assert probe["server_version"] == "8.12.0" and probe["table_count"] == 1 and "lattice" in probe["detail"]
    assert connector.schemas() == [{"name": "indices", "table_count": 1}]
    assert [item["name"] for item in connector.tables("indices")] == ["orders"]
    detail = connector.table("indices", "orders")
    assert [item["name"] for item in detail["columns"]] == ["_id", "amount", "customer.name"] and detail["rows"] == 2
    result = connector.query('SELECT "customer.name", amount FROM orders WHERE amount > 2')
    assert result["rows"] == [["A", 3.5]]
    with pytest.raises(ConnectorError, match="no such index"):
        connector.table("indices", "missing")


# ----- Kafka -------------------------------------------------------------------------------
class Record:
    def __init__(self, partition, offset, timestamp, key, value):
        self.partition, self.offset, self.timestamp, self.key, self.value = partition, offset, timestamp, key, value


class FakeTopicPartition:
    def __init__(self, topic, partition):
        self.topic, self.partition = topic, partition

    def __hash__(self):
        return hash((self.topic, self.partition))

    def __eq__(self, other):
        return (self.topic, self.partition) == (other.topic, other.partition)


class FakeConsumer:
    messages = {
        "orders": [Record(0, i, 1_700_000_000_000 + i, b"k%d" % i, json.dumps({"amount": i, "partition": "clash"}).encode()) for i in range(5)],
        "__consumer_offsets": [],
    }

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.assigned = []
        self.positions = {}

    def topics(self):
        return set(self.messages)

    def partitions_for_topic(self, topic):
        return {0} if topic in self.messages else None

    def beginning_offsets(self, partitions):
        return {tp: 0 for tp in partitions}

    def end_offsets(self, partitions):
        return {tp: len(self.messages[tp.topic]) for tp in partitions}

    def assign(self, partitions):
        self.assigned = partitions

    def seek(self, tp, offset):
        self.positions[tp] = offset

    def __iter__(self):
        for tp in self.assigned:
            start = self.positions.get(tp, 0)
            yield from self.messages[tp.topic][start:]

    def close(self):
        pass


class FakeAdmin:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def list_topics(self):
        return ["orders", "__consumer_offsets"]

    def close(self):
        pass


def test_kafka_connector_reads_the_newest_messages_and_expands_json(monkeypatch):
    module = types.ModuleType("kafka")
    module.KafkaConsumer = FakeConsumer
    module.KafkaAdminClient = FakeAdmin
    module.TopicPartition = FakeTopicPartition
    monkeypatch.setitem(sys.modules, "kafka", module)
    connector = KafkaConnector({"bootstrap_servers": "k1:9092, k2:9092", "security_protocol": "SASL_PLAINTEXT", "sasl_username": "u", "sasl_password": "p", "sample_rows": 3})
    probe = connector.test()
    assert probe["table_count"] == 1 and "1 个主题" in probe["detail"]
    assert connector.summary() == "k1:9092, k2:9092"
    assert connector.tables("topics") == [{"schema": "topics", "name": "orders", "kind": "topic", "rows": 5, "comment": "1 个分区"}]
    detail = connector.table("topics", "orders")
    assert detail["rows"] == 5 and {item["name"] for item in detail["columns"]} >= {"partition", "offset", "timestamp", "key", "value", "amount", "value.partition"}
    result = connector.query('SELECT "offset", amount, "key" FROM orders ORDER BY "offset"')
    assert result["rows"] == [[2, 2, "k2"], [3, 3, "k3"], [4, 4, "k4"]]  # the newest sample_rows messages
    with pytest.raises(ConnectorError, match="未找到主题"):
        connector.query("SELECT * FROM nope")
