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

"""Data insights, KPIs and alerts of the 元数据管理 module.

OpenMetadata's *Data Insights* answer one question — how healthy is the
catalog — with a handful of figures: how many assets exist, how many carry a
description, an owner and a tier, per asset type and over time. Here those
figures are computed from the catalog rows on demand, frozen once a day into
``insight_snapshot`` so the 7/30-day charts have history, and compared with
the KPIs a team sets (a target percentage or count between two dates).

Alerts are OpenMetadata's event subscriptions: a subscription names the
entity types, event types, change kinds and FQN prefix it cares about and
where to deliver — the in-app notification list and/or a webhook. Delivery
runs on a worker thread so a slow webhook never holds the catalog lock.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import queue
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx

from .metadata_entities import DATA_ASSET_TYPES, ENTITY_TYPES, EVENT_TYPES, SERVICE_TYPES, TIER_CLASSIFICATION, split_fqn, type_label
from .metadata_service import KPI_CHARTS, SECRET_MASK, MetadataError, MetadataService, summary_of
from .metadata_store import MetadataStore, MetadataStoreError, day_of, now_ms, today

MAX_DAYS = 90
DEFAULT_DAYS = 7
WEBHOOK_TIMEOUT = 5.0
MAX_NOTIFICATIONS_PER_EVENT = 50
DESTINATION_TYPES = ("in_app", "webhook")


class InsightsService:
    """Health figures, snapshots and KPI progress over the catalog."""

    def __init__(self, store: MetadataStore, service: MetadataService):
        self.store = store
        self.service = service
        self._snapshot_day = ""

    # =====================================================================================
    # figures
    # =====================================================================================
    def compute(self, *, team: str | None = None, tier: str | None = None, domain: str | None = None) -> dict[str, Any]:
        rows = self.store.all_entities(DATA_ASSET_TYPES, deleted=False, limit=50000)
        owners = self.service.owners_for([r["id"] for r in rows]) if rows else {}
        if team:
            allowed = self._team_member_ids(team)
            rows = [r for r in rows if any(o["id"] in allowed for o in owners.get(r["id"], []))]
        if tier:
            wanted = tier if tier.startswith(TIER_CLASSIFICATION + ".") else f"{TIER_CLASSIFICATION}.{tier}"
            rows = [r for r in rows if r["tier"] == wanted]
        if domain:
            rows = [r for r in rows if r["domain_fqn"] == domain or r["domain_fqn"].startswith(domain + ".")]
        by_type: dict[str, dict[str, int]] = {}
        described = owned = tiered = 0
        by_service_type: dict[str, int] = {}
        by_service: dict[str, int] = {}
        for row in rows:
            bucket = by_type.setdefault(row["entity_type"], {"total": 0, "with_description": 0, "with_owner": 0, "with_tier": 0})
            bucket["total"] += 1
            has_description = bool((row["description"] or "").strip())
            has_owner = bool(owners.get(row["id"]))
            has_tier = bool(row["tier"])
            bucket["with_description"] += has_description
            bucket["with_owner"] += has_owner
            bucket["with_tier"] += has_tier
            described += has_description
            owned += has_owner
            tiered += has_tier
            if row["service_type"]:
                by_service_type[row["service_type"]] = by_service_type.get(row["service_type"], 0) + 1
            if row["service_fqn"] and row["entity_type"] not in SERVICE_TYPES:
                by_service[row["service_fqn"]] = by_service.get(row["service_fqn"], 0) + 1
        total = len(rows)
        fqns = [r["fqn"] for r in rows]
        tagged = with_terms = 0
        if fqns:
            usage = self.store.tags_for_targets(fqns)
            tagged_targets = {t["target_fqn"] for t in usage if t["source"] == "classification"}
            term_targets = {t["target_fqn"] for t in usage if t["source"] == "glossary"}
            tagged = sum(1 for f in fqns if f in tagged_targets)
            with_terms = sum(1 for f in fqns if f in term_targets)
        edges = self.store.edges()
        linked = {e["from_fqn"] for e in edges} | {e["to_fqn"] for e in edges}
        with_lineage = sum(1 for f in fqns if f in linked)
        counts = self.store.counts_by_type(deleted=False)
        since_30 = day_of(now_ms() - 30 * 86400000)
        used = self.store.used_targets(since_30)
        return {
            "computed_at": now_ms(),
            "day": today(),
            "assets": total,
            "with_description": described,
            "with_owner": owned,
            "with_tier": tiered,
            "with_tags": tagged,
            "with_glossary_terms": with_terms,
            "with_lineage": with_lineage,
            "used_30d": sum(1 for f in fqns if f in used),
            "unused_30d": sum(1 for f in fqns if f not in used),
            "description_percent": _percent(described, total),
            "owner_percent": _percent(owned, total),
            "tier_percent": _percent(tiered, total),
            "tag_percent": _percent(tagged, total),
            "lineage_percent": _percent(with_lineage, total),
            "by_type": [
                {
                    "entity_type": key,
                    "label": type_label(key),
                    **value,
                    "description_percent": _percent(value["with_description"], value["total"]),
                    "owner_percent": _percent(value["with_owner"], value["total"]),
                    "tier_percent": _percent(value["with_tier"], value["total"]),
                }
                for key, value in sorted(by_type.items(), key=lambda item: -item[1]["total"])
            ],
            "by_service_type": [{"service_type": k, "count": v} for k, v in sorted(by_service_type.items(), key=lambda i: -i[1])],
            "by_service": [{"service_fqn": k, "count": v} for k, v in sorted(by_service.items(), key=lambda i: -i[1])[:30]],
            "governance": {
                "glossaries": counts.get("glossary", 0),
                "glossary_terms": counts.get("glossaryTerm", 0),
                "classifications": counts.get("classification", 0),
                "tags": counts.get("tag", 0),
                "domains": counts.get("domain", 0),
                "data_products": counts.get("dataProduct", 0),
                "teams": counts.get("team", 0),
                "users": counts.get("user", 0),
                "services": sum(counts.get(t, 0) for t in SERVICE_TYPES),
                "lineage_edges": len(edges),
            },
            "filters": {"team": team or "", "tier": tier or "", "domain": domain or ""},
        }

    def _team_member_ids(self, team_ref: str) -> set[str]:
        team = self.service.row("team", team_ref)
        ids = {team["id"]}
        for relation in self.store.relationships(from_id=team["id"], relation="member"):
            ids.add(relation["to_id"])
        # Sub-teams count as part of their parent, as in OpenMetadata's team hierarchy.
        for relation in self.store.relationships(from_id=team["id"], relation="parentTeam"):
            child = self.store.get_entity("team", id=relation["to_id"])
            if child and not child["deleted"]:
                ids |= self._team_member_ids(child["id"])
        return ids

    # =====================================================================================
    # snapshots and series
    # =====================================================================================
    def snapshot(self, day: str | None = None) -> dict[str, Any]:
        day = day or today()
        data = self.compute()
        self.store.save_snapshot(day, _slim(data))
        self._snapshot_day = day
        return {"day": day, "data": data}

    def ensure_snapshot(self) -> bool:
        """Freeze today's figures once; the scheduler calls this every tick."""
        day = today()
        if self._snapshot_day == day:
            return False
        try:
            if self.store.get_snapshot(day) is None:
                self.snapshot(day)
            self._snapshot_day = day
            return True
        except (MetadataStoreError, MetadataError):
            return False

    def series(self, days: Any = DEFAULT_DAYS) -> list[dict[str, Any]]:
        span = _days(days)
        since = (dt.date.today() - dt.timedelta(days=span - 1)).isoformat()
        stored = {item["day"]: item["data"] for item in self.store.list_snapshots(since)}
        current = _slim(self.compute())
        stored[today()] = current
        return [{"day": day, **data} for day, data in sorted(stored.items()) if day >= since]

    def overview(self, days: Any = DEFAULT_DAYS, *, team: str | None = None, tier: str | None = None, domain: str | None = None) -> dict[str, Any]:
        span = _days(days)
        filtered = bool(team or tier or domain)
        current = self.compute(team=team, tier=tier, domain=domain)
        # Snapshots are frozen unfiltered, so a filtered view has no comparable history.
        series = [{**_slim(current), "day": today()}] if filtered else self.series(span)
        first = series[0] if series else None
        since_ms = now_ms() - span * 86400000
        since_day = day_of(since_ms)
        top_viewed = self._decorate_usage(self.store.top_usage(since_day, "views", 8))
        top_queried = self._decorate_usage(self.store.top_usage(since_day, "queries", 8))
        return {
            "period": {"days": span, "from": series[0]["day"] if series else today(), "to": today()},
            "current": current,
            "series": series,
            "change": {} if filtered else {
                key: round(current[key] - (first[key] if first and key in first else current[key]), 2)
                for key in ("assets", "description_percent", "owner_percent", "tier_percent")
            },
            "filtered": filtered,
            "kpis": self.kpis(),
            "top_viewed": top_viewed,
            "top_queried": top_queried,
            "active_users": self.store.events_by_user(since_ms)[:10],
            "events": self.store.event_counts(since_ms),
        }

    def _decorate_usage(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = {r["fqn"]: r for r in self.store.get_entities_by_fqn([i["target_fqn"] for i in items]) if r["entity_type"] in DATA_ASSET_TYPES}
        result = []
        for item in items:
            row = rows.get(item["target_fqn"])
            result.append({"target_fqn": item["target_fqn"], "total": item["total"], "entity": summary_of(row) if row else None})
        return result

    # =====================================================================================
    # KPIs
    # =====================================================================================
    def kpis(self, *, current: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = self.store.all_entities("kpi", deleted=False, limit=200)
        if not rows:
            return []
        current = current or self.compute()
        series = self.series(30)
        result = []
        for row in rows:
            result.append(self._kpi_view(row, current, series))
        return result

    def kpi(self, ref: str) -> dict[str, Any]:
        row = self.service.row("kpi", ref)
        return self._kpi_view(row, self.compute(), self.series(30))

    def _kpi_view(self, row: dict[str, Any], current: dict[str, Any], series: list[dict[str, Any]]) -> dict[str, Any]:
        document = row["json"]
        chart = KPI_CHARTS.get(document.get("chart") or "", {})
        field = chart.get("field", "assets")
        value = float(current.get(field) or 0)
        target = float(document.get("target_value") or 0)
        start = str(document.get("start_date") or "")
        end = str(document.get("end_date") or "")
        day = today()
        if target <= 0:
            progress = 100.0 if value > 0 else 0.0
        else:
            progress = round(min(100.0, value / target * 100), 2)
        if day < start:
            status, status_label = "pending", "未开始"
        elif value >= target:
            status, status_label = "achieved", "已达成"
        elif day > end:
            status, status_label = "expired", "已到期未达成"
        else:
            elapsed = _elapsed_fraction(start, end, day)
            on_track = target <= 0 or (value / target) >= elapsed * 0.9
            status, status_label = ("on_track", "进展正常") if on_track else ("at_risk", "存在风险")
        history = [{"day": item["day"], "value": item.get(field)} for item in series if field in item]
        return {
            **self.service.decorate(row, full=False),
            "chart_label": chart.get("label", document.get("chart")),
            "field": field,
            "current_value": value,
            "target_value": target,
            "progress": progress,
            "status": status,
            "status_label": status_label,
            "days_left": max(0, (_date(end) - dt.date.today()).days) if end else 0,
            "history": history,
        }

    # =====================================================================================
    # views
    # =====================================================================================
    def record_view(self, fqn: str) -> None:
        try:
            self.store.add_usage(fqn, today(), views=1)
        except MetadataStoreError:
            return


class AlertService:
    """Event subscriptions: match change events and deliver notifications."""

    def __init__(self, store: MetadataStore, service: MetadataService, *, synchronous: bool = False):
        self.store = store
        self.service = service
        self.synchronous = synchronous
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""
        self.delivered = 0

    # ----- lifecycle --------------------------------------------------------------------------
    def attach(self) -> None:
        if self.handle not in self.service.listeners:
            self.service.listeners.append(self.handle)

    def start(self) -> None:
        if self.synchronous or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lattice-metadata-alerts", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                continue
            self._deliver(item)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.synchronous or (self._thread is not None and self._thread.is_alive()),
            "pending": self._queue.qsize(),
            "delivered": self.delivered,
            "last_error": self.last_error,
            "notifications": self.store.notification_counts(),
        }

    # ----- matching ----------------------------------------------------------------------------
    def handle(self, event: dict[str, Any]) -> int:
        """Listener on the service: queue a delivery for every matching subscription."""
        try:
            subscriptions = self.store.all_entities("eventSubscription", deleted=False, limit=500)
        except MetadataStoreError as error:
            self.last_error = str(error)
            return 0
        queued = 0
        for subscription in subscriptions:
            document = subscription["json"]
            if not document.get("enabled", True):
                continue
            if not self.matches(document.get("filters") or {}, event):
                continue
            item = {"subscription": subscription, "event": event}
            if self.synchronous:
                self._deliver(item)
            else:
                self._queue.put(item)
            queued += 1
            if queued >= MAX_NOTIFICATIONS_PER_EVENT:
                break
        return queued

    @staticmethod
    def matches(filters: dict[str, Any], event: dict[str, Any]) -> bool:
        entity_types = filters.get("entity_types") or []
        if entity_types and event.get("entity_type") not in entity_types:
            return False
        event_types = filters.get("event_types") or []
        if event_types and event.get("event_type") not in event_types:
            return False
        prefix = str(filters.get("fqn_prefix") or "")
        fqn = str(event.get("entity_fqn") or "")
        if prefix and not (fqn == prefix or fqn.startswith(prefix + ".")):
            return False
        kinds = filters.get("change_kinds") or []
        if kinds:
            change = event.get("change") or {}
            event_kinds = set(change.get("kinds") or [])
            if event.get("event_type") == "entityCreated":
                event_kinds.add("created")
            if not event_kinds & set(kinds):
                return False
        users = filters.get("users") or []
        if users and event.get("user_name") not in users:
            return False
        return True

    # ----- delivery ---------------------------------------------------------------------------
    def _deliver(self, item: dict[str, Any]) -> None:
        subscription = item["subscription"]
        event = item["event"]
        destinations = subscription["json"].get("destinations") or [{"type": "in_app"}]
        for destination in destinations:
            if not isinstance(destination, dict):
                continue
            kind = destination.get("type") or "in_app"
            record = {
                "id": None,
                "subscription_id": subscription["id"],
                "event_id": event.get("id") or "",
                "ts": now_ms(),
                "status": "unread",
                "detail": "",
                "json": {
                    "subscription_name": subscription["display_name"] or subscription["name"],
                    "destination": kind,
                    "event_type": event.get("event_type"),
                    "entity_type": event.get("entity_type"),
                    "entity_fqn": event.get("entity_fqn"),
                    "entity_name": event.get("entity_name"),
                    "user_name": event.get("user_name"),
                    "summary": (event.get("change") or {}).get("summary") or "",
                    "ts": event.get("ts"),
                },
            }
            if kind == "in_app":
                try:
                    self.store.insert_notification(record)
                    self.delivered += 1
                except MetadataStoreError as error:
                    self.last_error = str(error)
                continue
            if kind == "webhook":
                status, detail = self.post_webhook(destination, event, subscription)
                record["status"] = status
                record["detail"] = detail
                try:
                    self.store.insert_notification(record)
                    self.delivered += status == "delivered"
                except MetadataStoreError as error:
                    self.last_error = str(error)

    @staticmethod
    def post_webhook(destination: dict[str, Any], event: dict[str, Any], subscription: dict[str, Any]) -> tuple[str, str]:
        url = str(destination.get("url") or "")
        body = json.dumps(
            {
                "subscription": subscription["name"],
                "event": {k: v for k, v in event.items() if k != "change"},
                "change": event.get("change") or {},
            },
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "lattice-metadata-alerts/1"}
        secret = str(destination.get("secret") or "")
        if secret:
            headers["X-Lattice-Signature"] = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        try:
            with httpx.Client(timeout=httpx.Timeout(WEBHOOK_TIMEOUT, connect=3), trust_env=False, follow_redirects=False) as client:
                response = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as error:
            return "failed", f"Webhook 请求失败：{str(error)[:300]}"
        if response.status_code >= 400:
            return "failed", f"Webhook 返回 HTTP {response.status_code}"
        return "delivered", f"Webhook 返回 HTTP {response.status_code}"

    # ----- subscriptions -----------------------------------------------------------------------
    def validate(self, payload: dict[str, Any], existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Normalise the filters and destinations of a subscription document.

        A webhook secret sent back as the mask keeps the secret stored for the
        same URL, or for the webhook in the same position when only its URL was
        edited; a masked secret with nothing to keep is refused rather than
        silently dropped.
        """
        stored_hooks = [d for d in existing or [] if isinstance(d, dict) and d.get("type") == "webhook"]
        stored = {str(d.get("url")): str(d.get("secret") or "") for d in stored_hooks}
        hook_index = 0
        filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
        clean: dict[str, Any] = {}
        types = [t for t in _as_list(filters.get("entity_types")) if isinstance(t, str)]
        for entity_type in types:
            if entity_type not in ENTITY_TYPES:
                raise MetadataError(400, f"未知的实体类型：{entity_type}")
        clean["entity_types"] = types
        events = [t for t in _as_list(filters.get("event_types")) if isinstance(t, str)]
        for event_type in events:
            if event_type not in EVENT_TYPES:
                raise MetadataError(400, f"未知的事件类型：{event_type}")
        clean["event_types"] = events
        clean["change_kinds"] = [str(k)[:32] for k in _as_list(filters.get("change_kinds"))][:20]
        clean["fqn_prefix"] = str(filters.get("fqn_prefix") or "")[:1024]
        clean["users"] = [str(u)[:128] for u in _as_list(filters.get("users"))][:50]
        destinations = []
        for item in _as_list(payload.get("destinations")) or [{"type": "in_app"}]:
            if not isinstance(item, dict):
                raise MetadataError(400, "通知目标必须是对象。")
            kind = item.get("type") or "in_app"
            if kind not in DESTINATION_TYPES:
                raise MetadataError(400, "通知目标类型必须是 in_app 或 webhook。")
            if kind == "webhook":
                url = str(item.get("url") or "").strip()
                parts = urlsplit(url)
                if parts.scheme not in {"http", "https"} or not parts.netloc:
                    raise MetadataError(400, "Webhook 地址必须是 http(s) URL。")
                secret = str(item.get("secret") or "")
                if secret == SECRET_MASK:
                    if stored.get(url):
                        secret = stored[url]
                    elif hook_index < len(stored_hooks) and stored_hooks[hook_index].get("secret"):
                        secret = str(stored_hooks[hook_index]["secret"])
                    else:
                        raise MetadataError(400, f"请重新填写 Webhook {url} 的签名密钥。")
                hook_index += 1
                destinations.append({"type": "webhook", "url": url[:2000], "secret": secret[:256]})
            else:
                destinations.append({"type": "in_app"})
        return {**payload, "filters": clean, "destinations": destinations, "enabled": _bool(payload.get("enabled", True))}

    def test(self, ref: str, *, user: str | None = None) -> dict[str, Any]:
        subscription = self.service.row("eventSubscription", ref)
        event = {
            "id": "test",
            "event_type": "entityUpdated",
            "entity_type": "eventSubscription",
            "entity_id": subscription["id"],
            "entity_fqn": subscription["fqn"],
            "entity_name": subscription["display_name"] or subscription["name"],
            "user_name": user or self.service.user,
            "ts": now_ms(),
            "change": {"summary": "这是一条测试通知。", "kinds": ["custom"]},
        }
        self._deliver({"subscription": subscription, "event": event})
        return {"sent": True, "destinations": len(subscription["json"].get("destinations") or [])}

    def notifications(self, **filters: Any) -> dict[str, Any]:
        listing = self.store.list_notifications(**filters)
        subscriptions = {s["id"]: s for s in self.store.all_entities("eventSubscription", deleted=None, limit=500)}
        for item in listing["items"]:
            subscription = subscriptions.get(item["subscription_id"])
            item["subscription"] = summary_of(subscription) if subscription else None
        listing["counts"] = self.store.notification_counts()
        return listing

    def mark_read(self, ids: Any = None, *, all_unread: bool = False) -> dict[str, Any]:
        updated = 0
        if all_unread:
            page = 1
            while True:
                listing = self.store.list_notifications(status="unread", page=page, size=200)
                if not listing["items"]:
                    break
                for item in listing["items"]:
                    self.store.update_notification(item["id"], status="read")
                    updated += 1
                if len(listing["items"]) < 200:
                    break
        for notification_id in _as_list(ids)[:500]:
            self.store.update_notification(str(notification_id), status="read")
            updated += 1
        return {"updated": updated}


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
SLIM_FIELDS = (
    "day", "assets", "with_description", "with_owner", "with_tier", "with_tags", "with_glossary_terms",
    "with_lineage", "used_30d", "unused_30d", "description_percent", "owner_percent", "tier_percent",
    "tag_percent", "lineage_percent", "by_type", "by_service_type", "governance", "computed_at",
)


def _slim(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in SLIM_FIELDS if key in data}


def _percent(part: int, total: int) -> float:
    return round(part / total * 100, 2) if total else 0.0


def _days(value: Any) -> int:
    try:
        span = int(value) if value is not None else DEFAULT_DAYS
    except (TypeError, ValueError) as error:
        raise MetadataError(400, "天数必须是整数。") from error
    if span < 1 or span > MAX_DAYS:
        raise MetadataError(400, f"天数必须在 1–{MAX_DAYS} 之间。")
    return span


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return dt.date.today()


def _elapsed_fraction(start: str, end: str, day: str) -> float:
    begin, finish, current = _date(start), _date(end), _date(day)
    span = (finish - begin).days
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (current - begin).days / span))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def kpi_charts() -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in KPI_CHARTS.items()]


def tier_options() -> list[str]:
    return [f"{TIER_CLASSIFICATION}.Tier{index}" for index in range(1, 6)]


__all__ = ["InsightsService", "AlertService", "kpi_charts", "tier_options", "split_fqn"]
