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

"""The query result cache: keys, expiry, eviction, invalidation and its routes."""

import json

import pytest

from webapi.query_cache import MAX_ROWS, QueryCache, QueryCacheError, cache_key, normalize_sql
from webapi.tasks import TaskRegistry, register_platform_tasks


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def result(rows=1):
    return {"columns": ["n"], "rows": [[i] for i in range(rows)], "truncated": False, "sql": "SELECT 1", "elapsed_ms": 3.0}


def test_keys_ignore_whitespace_outside_literals_and_a_trailing_semicolon():
    assert normalize_sql("  SELECT   a,\n b FROM t ; ") == "SELECT a, b FROM t"
    assert normalize_sql("SELECT 'a   b'  FROM t") == "SELECT 'a   b' FROM t"
    assert cache_key("pg", "SELECT 1;", 100) == cache_key("pg", "select 1".upper(), "100")
    assert cache_key("pg", "SELECT 1", 100) != cache_key("pg", "SELECT 1", 200)
    assert cache_key("pg", "SELECT 1", None) != cache_key("mysql", "SELECT 1", None)
    assert cache_key("pg", "SELECT 'a b'", None) != cache_key("pg", "SELECT 'a  b'", None)


def test_hits_expire_after_the_ttl_and_carry_their_age():
    clock = Clock()
    cache = QueryCache(ttl_seconds=60, clock=clock)
    assert cache.get("pg", "SELECT 1") is None
    assert cache.put("pg", "SELECT 1", None, result(), [("public", "orders")])
    clock.now += 25
    hit = cache.get("pg", "select 1")
    assert hit is None  # case matters: literals and identifiers may be case-sensitive
    hit = cache.get("pg", "SELECT 1")
    assert hit["cached"] is True and hit["cache_age_seconds"] == 25 and hit["rows"] == [[0]]
    hit["rows"].append([9])
    assert cache.get("pg", "SELECT 1")["rows"] == [[0]]  # callers get copies
    clock.now += 40
    assert cache.get("pg", "SELECT 1") is None
    stats = cache.stats()
    assert (stats["hits"], stats["misses"], stats["entries"]) == (2, 3, 0)


def test_least_recently_used_entries_are_evicted_and_big_results_are_skipped():
    cache = QueryCache(max_entries=10, clock=Clock())
    for index in range(12):
        cache.put("pg", f"SELECT {index}", None, result())
        if index == 9:
            cache.get("pg", "SELECT 0")  # touched last: survives the eviction
    stats = cache.stats()
    assert stats["entries"] == 10 and stats["evictions"] == 2
    assert cache.get("pg", "SELECT 0") is not None and cache.get("pg", "SELECT 1") is None
    assert cache.put("pg", "SELECT big", None, result(MAX_ROWS + 1)) is False
    cache.enabled = False
    assert cache.put("pg", "SELECT off", None, result()) is False and cache.get("pg", "SELECT 0") is None


def test_invalidation_targets_a_source_a_table_or_everything():
    cache = QueryCache(clock=Clock())
    cache.put("pg", "SELECT * FROM public.orders", None, result(), [("public", "orders")])
    cache.put("pg", "SELECT * FROM customers", None, result(), [(None, "customers")])
    cache.put("mysql", "SELECT * FROM orders", None, result(), [(None, "orders")])
    assert cache.invalidate(table="ORDERS") == 2
    assert cache.get("pg", "SELECT * FROM customers") is not None
    assert cache.invalidate("pg") == 1 and cache.stats()["entries"] == 0
    cache.put("pg", "SELECT 1", None, result())
    cache.put("mysql", "SELECT 2", None, result())
    assert cache.invalidate() == 2 and cache.stats()["last_cleared"] == 1000.0
    assert cache.stats()["invalidations"] == 5


def test_sweep_settings_persist_and_entries_list_the_newest_first(tmp_path):
    clock = Clock()
    path = tmp_path / "query-cache.json"
    cache = QueryCache(ttl_seconds=30, clock=clock, path=path)
    cache.put("pg", "SELECT 1", None, result(), [("public", "a")])
    clock.now += 20
    cache.put("pg", "SELECT 2", 50, result(2), [("public", "b")])
    clock.now += 15
    assert cache.sweep() == {"removed": 1, "entries": 1}
    items = cache.entries()
    assert [(item["sql"], item["rows"], item["tables"], item["expires_in_seconds"]) for item in items] == [("SELECT 2", 2, ["public.b"], 15)]
    assert cache.drop(items[0]["key"]) is True and cache.drop("nope") is False
    with pytest.raises(QueryCacheError, match="缓存有效期"):
        cache.configure(ttl_seconds=5)
    stats = cache.configure(enabled=False, ttl_seconds=120, max_entries=20)
    assert stats["enabled"] is False and stats["ttl_seconds"] == 120
    assert json.loads(path.read_text()) == {"enabled": False, "ttl_seconds": 120, "max_entries": 20}
    again = QueryCache(clock=clock, path=path)
    assert (again.enabled, again.ttl_seconds, again.max_entries) == (False, 120, 20)


def test_environment_configures_the_cache(monkeypatch):
    monkeypatch.setenv("LATTICE_QUERY_CACHE", "off")
    monkeypatch.setenv("LATTICE_QUERY_CACHE_TTL", "90")
    monkeypatch.setenv("LATTICE_QUERY_CACHE_ENTRIES", "not-a-number")
    cache = QueryCache.from_env()
    assert (cache.enabled, cache.ttl_seconds, cache.max_entries) == (False, 90, 500)


def test_cache_tasks_sweep_and_clear():
    clock = Clock()
    cache = QueryCache(ttl_seconds=30, clock=clock)
    cache.put("pg", "SELECT 1", None, result())
    cache.put("mysql", "SELECT 1", None, result())
    registry = register_platform_tasks(TaskRegistry(), cache=cache)
    assert registry.keys() == ["QueryCacheTask.clear", "QueryCacheTask.sweep"]
    assert registry.run("QueryCacheTask", "clear", "pg", {})["removed"] == 1
    clock.now += 60
    assert registry.run("QueryCacheTask", "sweep", "", {}) == {"removed": 1, "entries": 0}
