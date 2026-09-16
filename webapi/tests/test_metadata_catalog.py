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

"""Entity semantics of the OpenMetadata-style catalog, on the SQLite backend."""

import json

import pytest

from webapi import metadata_service

from webapi.metadata_entities import build_fqn, normalize_data_type, split_fqn
from webapi.metadata_service import SECRET_MASK, MetadataError, MetadataService
from webapi.metadata_store import MetadataStore, MetadataStoreError, split_dsn

SCHEMA_FQN = "mysql-local.default.lattice_demo"


@pytest.fixture
def store():
    value = MetadataStore("sqlite:///:memory:")
    assert value.bootstrap()["ok"]
    yield value
    value.close()


@pytest.fixture
def catalog(store):
    service = MetadataService(store)
    service.seed()
    return service


def make_table(catalog, name="orders", columns=None, schema_fqn=SCHEMA_FQN):
    service, database, schema = split_fqn(schema_fqn)
    if catalog.store.get_entity("databaseService", fqn=service) is None:
        catalog.create("databaseService", {"name": service, "service_type": "Mysql", "datasource_id": service})
    if catalog.store.get_entity("database", fqn=build_fqn(service, database)) is None:
        catalog.create("database", {"name": database, "parent_fqn": service})
    if catalog.store.get_entity("databaseSchema", fqn=schema_fqn) is None:
        catalog.create("databaseSchema", {"name": schema, "parent_fqn": build_fqn(service, database)})
    return catalog.create(
        "table",
        {
            "name": name,
            "parent_fqn": schema_fqn,
            "source_schema": schema,
            "columns": columns or [{"name": "id", "type": "int"}, {"name": "amount", "type": "decimal(10,2)"}],
        },
    )


def expect_error(status, operation, *args, **kwargs):
    with pytest.raises(MetadataError) as caught:
        operation(*args, **kwargs)
    assert caught.value.status_code == status
    return str(caught.value)


# ----- names, types and DSNs ---------------------------------------------------------------
def test_fqn_quoting_round_trips_names_with_dots_and_quotes():
    fqn = build_fqn("svc", "db.prod", 'we"ird')
    assert fqn == 'svc."db.prod"."we\\"ird"'
    assert split_fqn(fqn) == ["svc", "db.prod", 'we"ird']


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("varchar(255)", "VARCHAR"),
        ("Nullable(Int32)", "INT"),
        ("bigint", "BIGINT"),
        ("timestamp with time zone", "TIMESTAMP"),
        ("decimal(10,2)", "DECIMAL"),
        ("list<int>", "ARRAY"),
        ("", "UNKNOWN"),
    ],
)
def test_engine_types_normalise_to_openmetadata_names(raw, expected):
    assert normalize_data_type(raw) == expected


def test_dsn_selects_backend():
    assert split_dsn("sqlite:///:memory:") == ("sqlite", ":memory:")
    assert split_dsn("sqlite:///catalog.db") == ("sqlite", "catalog.db")
    backend, conninfo = split_dsn("postgresql+asyncpg://u:p@127.0.0.1:5432/blog_converter")
    assert backend == "postgres" and "dbname=blog_converter" in conninfo
    with pytest.raises(MetadataStoreError):
        split_dsn("mysql://root@localhost/db")


def test_unreachable_postgres_falls_back_to_sqlite(tmp_path):
    store = MetadataStore("postgresql://nobody:x@127.0.0.1:1/none", fallback_path=tmp_path / "catalog.sqlite")
    health = store.bootstrap()
    assert health["ok"] and health["backend"] == "sqlite" and health["fallback"]
    assert "PostgreSQL" in health["fallback_reason"]
    store.close()


# ----- seeding and hierarchy ------------------------------------------------------------------
def test_seed_is_idempotent_and_creates_system_classifications(catalog):
    assert catalog.seed() == {"created": 0}
    names = {item["name"]: item for item in catalog.classifications()}
    assert {"PII", "PersonalData", "Tier"} <= set(names)
    assert names["Tier"]["tag_count"] == 5
    expect_error(400, catalog.delete, "classification", "Tier", hard=True)


def test_create_computes_fqn_service_and_rejects_duplicates(catalog):
    table = make_table(catalog)
    assert table["fqn"] == f"{SCHEMA_FQN}.orders"
    assert table["service_fqn"] == "mysql-local" and table["service_type"] == "Mysql"
    assert [crumb["entity_type"] for crumb in table["breadcrumb"]] == ["databaseService", "database", "databaseSchema"]
    assert table["columns"][1]["data_type"] == "DECIMAL" and table["columns"][1]["fqn"] == f"{SCHEMA_FQN}.orders.amount"
    expect_error(409, make_table, catalog)
    expect_error(404, catalog.create, "table", {"name": "x", "parent_fqn": "nope.nope.nope"})
    expect_error(400, catalog.create, "table", {"name": "x"})
    assert "不支持字段 row_count" in expect_error(400, catalog.create, "glossary", {"name": "g", "row_count": 5})


def test_updates_are_versioned_and_schema_changes_bump_the_major_version(catalog):
    table = make_table(catalog)
    updated = catalog.update("table", table["fqn"], {"description": "订单事实表"})
    assert updated["version"] == 0.2
    row = catalog.row("table", table["fqn"])
    touched = catalog.touch(row, {"columns": [{"name": "id", "data_type_display": "int", "data_type": "INT"}]}, user="bot")
    assert touched["version"] == 1.0
    history = catalog.versions("table", table["fqn"])["versions"]
    assert [item["version"] for item in history] == [1.0, 0.2, 0.1]
    assert "删除字段 amount" in history[0]["summary"]
    assert catalog.version("table", table["fqn"], "0.2")["description"] == "订单事实表"


def test_touch_keeps_volatile_fields_current_without_a_new_version(catalog):
    table = make_table(catalog)
    row = catalog.row("table", table["fqn"])
    latest = catalog.touch(row, {"row_count": 1100}, user="bot")
    assert latest["version"] == 0.1
    assert catalog.store.get_entity("table", id=table["id"])["json"]["row_count"] == 1100


def test_soft_delete_cascades_and_restore_requires_the_parent(catalog):
    table = make_table(catalog)
    result = catalog.delete("databaseSchema", SCHEMA_FQN)
    assert result == {"deleted": True, "hard": False, "fqn": SCHEMA_FQN, "children": 1}
    assert catalog.get("table", table["fqn"])["deleted"]
    assert catalog.list("table")["total"] == 0
    expect_error(400, catalog.restore, "table", table["fqn"])
    catalog.restore("databaseSchema", SCHEMA_FQN)
    assert not catalog.get("table", table["fqn"])["deleted"]


def test_hard_delete_cleans_tags_lineage_and_versions(catalog):
    orders = make_table(catalog)
    daily = make_table(catalog, "orders_daily")
    catalog.set_tags(orders["fqn"], ["PII.Sensitive"])
    catalog.store.upsert_edge({"from_fqn": orders["fqn"], "to_fqn": daily["fqn"], "from_type": "table", "to_type": "table"})
    catalog.delete("table", orders["fqn"], hard=True)
    store = catalog.store
    assert store.get_entity("table", id=orders["id"]) is None
    assert store.tags_for_prefix(orders["fqn"]) == []
    assert store.edges(to_fqn=daily["fqn"]) == []
    assert store.list_versions(orders["id"]) == []


# ----- tags, owners, domains ------------------------------------------------------------------
def test_mutually_exclusive_classifications_and_tier(catalog):
    table = make_table(catalog)
    assert "互斥" in expect_error(400, catalog.set_tags, table["fqn"], ["PII.Sensitive", "PII.NonSensitive"])
    tagged = catalog.set_tags(table["fqn"], ["PII.Sensitive", "Tier.Tier2"])
    assert tagged["tier"] == "Tier.Tier2"
    assert {tag["tag_fqn"] for tag in tagged["tags"]} == {"PII.Sensitive", "Tier.Tier2"}
    cleared = catalog.update("table", table["fqn"], {"tier": None})
    assert cleared["tier"] is None


def test_column_tags_and_glossary_terms(catalog):
    table = make_table(catalog)
    catalog.create("glossary", {"name": "Sales", "description": "销售术语"})
    catalog.create("glossaryTerm", {"name": "GMV", "parent_fqn": "Sales", "description": "成交总额", "synonyms": ["成交额"]})
    catalog.set_tags(f"{table['fqn']}.amount", [{"tag_fqn": "Sales.GMV", "source": "glossary"}, "PII.None"])
    entity = catalog.get("table", table["fqn"])
    amount = next(column for column in entity["columns"] if column["name"] == "amount")
    assert [term["tag_fqn"] for term in amount["glossary_terms"]] == ["Sales.GMV"]
    assert [tag["tag_fqn"] for tag in amount["tags"]] == ["PII.None"]
    assert entity["version"] == 0.2
    assert catalog.tag_usage("Sales.GMV")["items"][0]["column"] == "amount"
    assert catalog.glossary_terms("Sales")["terms"][0]["usage_count"] == 1


def test_owners_teams_followers(catalog):
    catalog.create("team", {"name": "Finance", "team_type": "Department", "display_name": "财务部"})
    catalog.create("user", {"name": "alice", "display_name": "Alice", "teams": ["Finance"]})
    expect_error(400, catalog.create, "user", {"name": "Bob Smith"})
    table = make_table(catalog)
    owned = catalog.update("table", table["fqn"], {"owners": ["alice", "Finance"]})
    assert sorted(owner["name"] for owner in owned["owners"]) == ["Finance", "alice"]
    followed = catalog.follow("table", table["fqn"], "alice")
    assert followed["followers_count"] == 1
    teams = catalog.teams()
    finance = next(team for team in teams["teams"] if team["name"] == "Finance")
    assert [user["name"] for user in finance["users"]] == ["alice"] and finance["owns_count"] == 1
    assert catalog.owned_by("user", "alice")["owns"][0]["fqn"] == table["fqn"]


def test_domains_and_data_products_hold_assets(catalog):
    table = make_table(catalog)
    expect_error(400, catalog.create, "domain", {"name": "Sales"})
    catalog.create("domain", {"name": "Sales", "domain_type": "Source-aligned"})
    product = catalog.create("dataProduct", {"name": "orders-360", "domain": "Sales", "assets": [{"entity_type": "table", "fqn": table["fqn"]}]})
    assert product["domain"]["fqn"] == "Sales"
    catalog.update("table", table["fqn"], {"domain": "Sales"})
    overview = catalog.domains()
    assert overview["domains"][0]["asset_count"] == 1
    assert overview["data_products"][0]["asset_count"] == 1
    assert catalog.domain_assets("dataProduct", "orders-360")["items"][0]["fqn"] == table["fqn"]


def test_rename_moves_children_tags_and_lineage(catalog):
    orders = make_table(catalog)
    daily = make_table(catalog, "orders_daily")
    catalog.set_tags(orders["fqn"], ["PII.Sensitive"])
    catalog.store.upsert_edge({"from_fqn": orders["fqn"], "to_fqn": daily["fqn"], "from_type": "table", "to_type": "table"})
    catalog.rename("databaseSchema", SCHEMA_FQN, "sales_mart")
    moved = "mysql-local.default.sales_mart.orders"
    assert catalog.get("table", moved)["tags"][0]["tag_fqn"] == "PII.Sensitive"
    assert catalog.store.edges(from_fqn=moved)[0]["to_fqn"] == "mysql-local.default.sales_mart.orders_daily"
    expect_error(404, catalog.get, "table", orders["fqn"])


# ----- search, properties, import ------------------------------------------------------------
def test_search_matches_columns_and_returns_facets(catalog):
    make_table(catalog, "orders", [{"name": "customer_mobile", "type": "varchar(20)"}])
    make_table(catalog, "sellers")
    result = catalog.search("mobile")
    assert result["total"] == 1 and result["items"][0]["name"] == "orders"
    assert result["facets"]["service_type"] == [{"value": "Mysql", "count": 1}]
    assert catalog.search(None, entity_types=["table"])["total"] == 2


def test_custom_properties_validate_the_extension(catalog):
    table = make_table(catalog)
    catalog.define_property("table", {"name": "sensitivity", "property_type": "enum", "config": {"values": ["low", "high"]}})
    expect_error(400, catalog.update, "table", table["fqn"], {"extension": {"sensitivity": "medium"}})
    expect_error(400, catalog.update, "table", table["fqn"], {"extension": {"undefined": 1}})
    assert catalog.update("table", table["fqn"], {"extension": {"sensitivity": "high"}})["extension"] == {"sensitivity": "high"}
    assert catalog.remove_property("table", "sensitivity") == {"deleted": True}


def test_openmetadata_export_imports_entities_tags_and_lineage(catalog):
    payload = {
        "entities": [
            {"entityType": "databaseService", "name": "om", "fullyQualifiedName": "om", "serviceType": "Snowflake"},
            {"entityType": "database", "name": "prod", "fullyQualifiedName": "om.prod"},
            {"entityType": "databaseSchema", "name": "sales", "fullyQualifiedName": "om.prod.sales"},
            {
                "entityType": "table", "name": "orders", "fullyQualifiedName": "om.prod.sales.orders", "description": "订单",
                "tableType": "Regular",
                "columns": [{"name": "id", "dataType": "INT", "dataTypeDisplay": "int", "tags": [{"tagFQN": "PII.None", "source": "Classification", "labelType": "Manual", "state": "Confirmed"}]}],
                "tags": [{"tagFQN": "Tier.Tier2"}],
            },
            {"entityType": "table", "name": "orders_daily", "fullyQualifiedName": "om.prod.sales.orders_daily"},
        ],
        "lineage": [
            {
                "fromEntity": {"fullyQualifiedName": "om.prod.sales.orders"},
                "toEntity": {"fullyQualifiedName": "om.prod.sales.orders_daily"},
                "lineageDetails": {"sqlQuery": "INSERT INTO orders_daily SELECT id FROM orders", "columnsLineage": [{"fromColumns": ["om.prod.sales.orders.id"], "toColumn": "om.prod.sales.orders_daily.id"}]},
            }
        ],
    }
    summary = catalog.import_bundle(payload, fmt="openmetadata")
    assert summary["created"] == 5 and summary["errors"] == []
    assert summary["tags"] == 2 and summary["lineage"] == 1
    orders = catalog.get("table", "om.prod.sales.orders")
    assert orders["description"] == "订单" and orders["service_type"] == "Snowflake"
    assert orders["columns"][0]["tags"][0]["tag_fqn"] == "PII.None"
    edge = catalog.store.edges(to_fqn="om.prod.sales.orders_daily")[0]
    assert edge["source"] == "import" and edge["columns"][0]["to_column"] == "om.prod.sales.orders_daily.id"
    again = catalog.import_bundle(catalog.export(), dry_run=True)
    assert again["created"] == 0 and again["updated"] >= 5


def test_webhook_secrets_never_leave_the_catalog(catalog):
    created = catalog.create("eventSubscription", {"name": "hook", "destinations": [{"type": "webhook", "url": "http://127.0.0.1:9/h", "secret": "s3cret"}]})
    assert created["destinations"][0]["secret"] == SECRET_MASK
    assert catalog.store.get_entity("eventSubscription", id=created["id"])["json"]["destinations"][0]["secret"] == "s3cret"
    exported = catalog.export(entity_types=["eventSubscription"])
    assert exported["entities"][0]["destinations"][0]["secret"] == SECRET_MASK


# ----- review fixes -----------------------------------------------------------------------------
def test_failed_assignments_leave_nothing_behind(catalog):
    expect_error(404, catalog.create, "glossary", {"name": "g1", "owners": ["nobody"]})
    assert catalog.store.get_entity("glossary", fqn="g1") is None
    table = make_table(catalog)
    catalog.create("user", {"name": "alice"})
    expect_error(400, catalog.update, "table", table["fqn"], {"owners": ["alice"], "tags": ["PII.Sensitive", "PII.NonSensitive"]})
    entity = catalog.get("table", table["fqn"])
    assert entity["owners"] == [] and entity["version"] == 0.1
    expect_error(404, catalog.update, "table", table["fqn"], {"description": "x", "columns": [{"name": "id", "tags": ["Nope.Tag"]}]})
    assert catalog.get("table", table["fqn"])["description"] == ""


def test_versions_and_feed_never_show_webhook_secrets(catalog):
    hook = {"type": "webhook", "url": "http://127.0.0.1:9/h", "secret": "s3cret"}
    created = catalog.create("eventSubscription", {"name": "hook", "destinations": [hook]})
    catalog.update("eventSubscription", created["fqn"], {"destinations": [{"type": "in_app"}, hook]})
    stored = [item["change"] for item in catalog.store.list_versions(created["id"])]
    shown = [
        catalog.versions("eventSubscription", created["fqn"]),
        catalog.feed(entity_fqn=created["fqn"]),
        catalog.version("eventSubscription", created["fqn"], "0.2"),
        stored,
    ]
    text = json.dumps(shown, ensure_ascii=False)
    assert "s3cret" not in text and SECRET_MASK in text


def test_search_pages_beyond_the_facet_sample(catalog, monkeypatch):
    monkeypatch.setattr(metadata_service, "MAX_PAGE_FACETS", 3)
    for index in range(7):
        make_table(catalog, f"t{index}")
    page = catalog.search(None, entity_types=["table"], page=3, size=3)
    assert page["total"] == 7 and [item["name"] for item in page["items"]] == ["t6"]
    assert page["truncated"] and page["facets"]["entity_type"] == [{"value": "table", "count": 3}]


def test_connection_documents_refuse_credentials(catalog):
    catalog.create("databaseService", {"name": "pg1", "service_type": "Postgres"})
    assert "机密字段" in expect_error(400, catalog.update, "databaseService", "pg1", {"connection": {"host": "db", "password": "hunter2"}})
    assert "机密字段" in expect_error(400, catalog.update, "databaseService", "pg1", {"connection": {"config": {"api_key": "k"}}})
    kept = catalog.update("databaseService", "pg1", {"connection": {"host": "db", "password": SECRET_MASK, "token": ""}})
    assert kept["connection"]["host"] == "db"
