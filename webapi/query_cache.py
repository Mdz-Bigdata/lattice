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

"""Result cache for 智能问数, the SQL 工作台 and report queries.

Every query the platform runs is read-only, so a result is only ever stale,
never wrong, and staleness is bounded by three explicit policies:

* **time**: an entry expires ``ttl_seconds`` after it was stored;
* **space**: at most ``max_entries`` results are kept, least recently used
  first out, and a result larger than ``MAX_ROWS`` is never kept;
* **invalidation**: entries can be dropped for one data source (its
  configuration changed or it was removed), for one table (an ingestion or a
  load just finished), or all at once, and every request may ask to bypass the
  cache with ``refresh``.

The cache is in memory: a restart empties it, which is the safe default for a
cache. Only its settings persist, in a small JSON file under ``.runtime``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import env

DEFAULT_TTL = 600
DEFAULT_ENTRIES = 500
MIN_TTL = 10
MAX_TTL = 7 * 86400
MIN_ENTRIES = 10
MAX_ENTRIES = 5000
#: A result with more rows than this is not worth the memory it would keep.
MAX_ROWS = 5000


class QueryCacheError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def normalize_sql(sql: Any) -> str:
    """Collapse whitespace outside string literals and drop a trailing semicolon.

    Two spellings of the same statement share a cache entry; a literal keeps
    its exact spacing because it is part of the query's meaning.
    """
    text = str(sql or "").strip().rstrip(";").strip()
    out: list[str] = []
    quoted = False
    pending_space = False
    for char in text:
        if char == "'":
            quoted = not quoted
        if not quoted and char.isspace():
            pending_space = True
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
        out.append(char)
    return "".join(out)


def cache_key(datasource_id: Any, sql: Any, limit: Any = None) -> str:
    try:
        bound = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        bound = None
    raw = json.dumps([str(datasource_id or ""), normalize_sql(sql), bound], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def table_names(tables: Any) -> set[str]:
    """Lower-case ``name`` and ``schema.name`` forms of every referenced table."""
    names: set[str] = set()
    for item in tables or ():
        schema, name = (item if isinstance(item, (tuple, list)) and len(item) == 2 else (None, item))
        if not name:
            continue
        names.add(str(name).lower())
        if schema:
            names.add(f"{str(schema).lower()}.{str(name).lower()}")
    return names


class QueryCache:
    """Thread-safe LRU of query results with expiry and targeted invalidation."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL,
        max_entries: int = DEFAULT_ENTRIES,
        enabled: bool = True,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.ttl_seconds = _bounded(ttl_seconds, "缓存有效期", MIN_TTL, MAX_TTL)
        self.max_entries = _bounded(max_entries, "缓存条目上限", MIN_ENTRIES, MAX_ENTRIES)
        self.enabled = bool(enabled)
        self.path = path
        self._clock = clock
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.invalidations = 0
        self.last_cleared: float | None = None
        self._load()

    @classmethod
    def from_env(cls, path: Path | None = None) -> "QueryCache":
        """Settings from ``LATTICE_QUERY_CACHE*``; the settings file, if any, wins."""
        enabled = env("QUERY_CACHE", "1").strip().lower() not in {"0", "false", "off", "no"}
        ttl = _int_env("QUERY_CACHE_TTL", DEFAULT_TTL, MIN_TTL, MAX_TTL)
        entries = _int_env("QUERY_CACHE_ENTRIES", DEFAULT_ENTRIES, MIN_ENTRIES, MAX_ENTRIES)
        return cls(ttl_seconds=ttl, max_entries=entries, enabled=enabled, path=path)

    # ----- reads and writes ---------------------------------------------------------
    def get(self, datasource_id: Any, sql: Any, limit: Any = None) -> dict[str, Any] | None:
        """A copy of the cached result, marked ``cached`` with its age; None on a miss."""
        if not self.enabled:
            return None
        key = cache_key(datasource_id, sql, limit)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry["expires"] <= now:
                self._entries.pop(key, None)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            entry["hits"] += 1
            self.hits += 1
            result = dict(entry["result"])
            result["rows"] = list(entry["result"].get("rows") or [])
            result["cached"] = True
            result["cache_age_seconds"] = int(now - entry["created"])
            result["cache_key"] = key
            return result

    def put(self, datasource_id: Any, sql: Any, limit: Any, result: dict[str, Any], tables: Any = ()) -> bool:
        """Keep a result; False when the cache is off or the result is too large."""
        if not self.enabled or not isinstance(result, dict):
            return False
        rows = result.get("rows") or []
        if len(rows) > MAX_ROWS:
            return False
        key = cache_key(datasource_id, sql, limit)
        now = self._clock()
        stored = {k: v for k, v in result.items() if k not in {"cached", "cache_age_seconds", "cache_key"}}
        stored["rows"] = list(rows)
        with self._lock:
            self._entries[key] = {
                "datasource_id": str(datasource_id or ""),
                "sql": normalize_sql(sql)[:2000],
                "tables": table_names(tables),
                "result": stored,
                "rows": len(rows),
                "created": now,
                "expires": now + self.ttl_seconds,
                "hits": 0,
            }
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1
        return True

    # ----- invalidation ---------------------------------------------------------------
    def invalidate(self, datasource_id: Any = None, table: Any = None) -> int:
        """Drop entries of one data source, one table (any source), or everything."""
        source = str(datasource_id or "")
        name = str(table or "").strip().lower()
        with self._lock:
            doomed = [
                key
                for key, entry in self._entries.items()
                if (not source or entry["datasource_id"] == source) and (not name or name in entry["tables"])
            ]
            for key in doomed:
                self._entries.pop(key, None)
            self.invalidations += len(doomed)
            if not source and not name:
                self.last_cleared = self._clock()
        return len(doomed)

    def sweep(self) -> dict[str, Any]:
        """Remove expired entries; the scheduler runs this as ``QueryCacheTask.sweep``."""
        now = self._clock()
        with self._lock:
            expired = [key for key, entry in self._entries.items() if entry["expires"] <= now]
            for key in expired:
                self._entries.pop(key, None)
            return {"removed": len(expired), "entries": len(self._entries)}

    # ----- settings and reporting -----------------------------------------------------
    def configure(self, *, enabled: Any = None, ttl_seconds: Any = None, max_entries: Any = None) -> dict[str, Any]:
        with self._lock:
            if ttl_seconds is not None:
                self.ttl_seconds = _bounded(ttl_seconds, "缓存有效期", MIN_TTL, MAX_TTL)
            if max_entries is not None:
                self.max_entries = _bounded(max_entries, "缓存条目上限", MIN_ENTRIES, MAX_ENTRIES)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
                    self.evictions += 1
            if enabled is not None:
                self.enabled = bool(enabled)
                if not self.enabled:
                    self.invalidate()
            self._save()
            return self.stats()

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            live = [entry for entry in self._entries.values() if entry["expires"] > now]
            by_source: dict[str, dict[str, Any]] = {}
            for entry in live:
                bucket = by_source.setdefault(entry["datasource_id"], {"datasource_id": entry["datasource_id"], "entries": 0, "rows": 0, "hits": 0})
                bucket["entries"] += 1
                bucket["rows"] += entry["rows"]
                bucket["hits"] += entry["hits"]
            oldest = min((entry["created"] for entry in live), default=None)
            lookups = self.hits + self.misses
            return {
                "enabled": self.enabled,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "max_rows": MAX_ROWS,
                "entries": len(live),
                "rows": sum(entry["rows"] for entry in live),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / lookups, 4) if lookups else 0.0,
                "evictions": self.evictions,
                "invalidations": self.invalidations,
                "oldest_age_seconds": int(now - oldest) if oldest is not None else None,
                "last_cleared": self.last_cleared,
                "by_datasource": sorted(by_source.values(), key=lambda item: -item["entries"]),
            }

    def entries(self, datasource_id: Any = None, limit: int = 50) -> list[dict[str, Any]]:
        """The newest live entries, for the cache panel; SQL is truncated, rows are not shown."""
        now = self._clock()
        source = str(datasource_id or "")
        with self._lock:
            items = [
                {
                    "key": key,
                    "datasource_id": entry["datasource_id"],
                    "sql": entry["sql"][:300],
                    "tables": sorted(name for name in entry["tables"] if "." in name) or sorted(entry["tables"]),
                    "rows": entry["rows"],
                    "hits": entry["hits"],
                    "age_seconds": int(now - entry["created"]),
                    "expires_in_seconds": int(entry["expires"] - now),
                }
                for key, entry in reversed(self._entries.items())
                if entry["expires"] > now and (not source or entry["datasource_id"] == source)
            ]
        return items[: max(1, min(int(limit or 50), 500))]

    def drop(self, key: Any) -> bool:
        with self._lock:
            removed = self._entries.pop(str(key or ""), None) is not None
            if removed:
                self.invalidations += 1
            return removed

    # ----- persistence of settings ----------------------------------------------------
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            if "ttl_seconds" in data:
                self.ttl_seconds = _bounded(data["ttl_seconds"], "缓存有效期", MIN_TTL, MAX_TTL)
            if "max_entries" in data:
                self.max_entries = _bounded(data["max_entries"], "缓存条目上限", MIN_ENTRIES, MAX_ENTRIES)
        except QueryCacheError:
            pass
        if "enabled" in data:
            self.enabled = bool(data["enabled"])

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"enabled": self.enabled, "ttl_seconds": self.ttl_seconds, "max_entries": self.max_entries}, ensure_ascii=False, indent=2),
                "utf-8",
            )
        except OSError:
            pass


def _bounded(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise QueryCacheError(400, f"{label}必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise QueryCacheError(400, f"{label}必须是整数。") from error
    if number < minimum or number > maximum:
        raise QueryCacheError(400, f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return number


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env(name, "").strip()
    if not raw:
        return default
    try:
        return _bounded(raw, name, minimum, maximum)
    except QueryCacheError:
        return default
