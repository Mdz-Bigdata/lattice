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

"""Entity model of the 元数据管理 module, after OpenMetadata's.

OpenMetadata organises every asset under a *service* (a database service
contains databases, which contain schemas, which contain tables, and so on for
messaging, dashboard, pipeline, ML-model, storage, search and API services), and
governs those assets with glossaries, classifications, domains, data products,
teams and users. This module is the static description of that model: the
entity types, the containment hierarchy each follows, the fully qualified name
(FQN) rules, the connector catalog of every service category, the seed
classifications and the helpers that turn a stored document into search text
or a change description.

Nothing here touches storage; :mod:`webapi.metadata_service` does.
"""

from __future__ import annotations

import re
from typing import Any

NAME_MAX = 256
NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,256}\Z")
USER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
FQN_SEPARATOR = "."
FQN_QUOTE = '"'
SYSTEM_USER = "lattice"
DEFAULT_TEAM = "Organization"
TIER_CLASSIFICATION = "Tier"

# ----- entity types -----------------------------------------------------------------------
#: type id -> definition. ``parent`` is the type of the entity an instance is nested under
#: (a list when several are possible), ``category`` groups types on the 元数据资产 tree.
ENTITY_TYPES: dict[str, dict[str, Any]] = {
    # data services and their assets
    "databaseService": {"label": "数据库服务", "category": "service", "service_category": "database"},
    "database": {"label": "数据库", "parent": "databaseService", "category": "database", "service_category": "database"},
    "databaseSchema": {"label": "数据库模式", "parent": "database", "category": "database", "service_category": "database"},
    "table": {"label": "数据表", "parent": "databaseSchema", "category": "database", "service_category": "database", "columns": True, "data_asset": True},
    "storedProcedure": {"label": "存储过程", "parent": "databaseSchema", "category": "database", "service_category": "database", "data_asset": True},
    "messagingService": {"label": "消息服务", "category": "service", "service_category": "messaging"},
    "topic": {"label": "消息主题", "parent": "messagingService", "category": "messaging", "service_category": "messaging", "columns": True, "data_asset": True},
    "dashboardService": {"label": "仪表板服务", "category": "service", "service_category": "dashboard"},
    "dashboard": {"label": "仪表板", "parent": "dashboardService", "category": "dashboard", "service_category": "dashboard", "data_asset": True},
    "chart": {"label": "图表", "parent": "dashboardService", "category": "dashboard", "service_category": "dashboard", "data_asset": True},
    "dashboardDataModel": {"label": "仪表板数据模型", "parent": "dashboardService", "category": "dashboard", "service_category": "dashboard", "columns": True, "data_asset": True},
    "pipelineService": {"label": "工作流服务", "category": "service", "service_category": "pipeline"},
    "pipeline": {"label": "工作流", "parent": "pipelineService", "category": "pipeline", "service_category": "pipeline", "data_asset": True},
    "mlmodelService": {"label": "机器学习模型服务", "category": "service", "service_category": "mlmodel"},
    "mlmodel": {"label": "机器学习模型", "parent": "mlmodelService", "category": "mlmodel", "service_category": "mlmodel", "data_asset": True},
    "storageService": {"label": "存储服务", "category": "service", "service_category": "storage"},
    "container": {"label": "存储容器", "parent": ["storageService", "container"], "category": "storage", "service_category": "storage", "columns": True, "data_asset": True},
    "searchService": {"label": "搜索服务", "category": "service", "service_category": "search"},
    "searchIndex": {"label": "搜索索引", "parent": "searchService", "category": "search", "service_category": "search", "columns": True, "data_asset": True},
    "apiService": {"label": "API 服务", "category": "service", "service_category": "api"},
    "apiCollection": {"label": "API 集合", "parent": "apiService", "category": "api", "service_category": "api", "data_asset": True},
    "apiEndpoint": {"label": "API 端点", "parent": "apiCollection", "category": "api", "service_category": "api", "data_asset": True},
    "metadataService": {"label": "元数据服务", "category": "service", "service_category": "metadata"},
    "semanticModel": {"label": "语义模型", "category": "semantic", "data_asset": True},
    # governance
    "glossary": {"label": "术语库", "category": "governance"},
    "glossaryTerm": {"label": "术语", "parent": ["glossary", "glossaryTerm"], "category": "governance"},
    "classification": {"label": "分类", "category": "governance"},
    "tag": {"label": "标签", "parent": ["classification", "tag"], "category": "governance"},
    "domain": {"label": "数据域", "parent": ["domain"], "optional_parent": True, "category": "governance"},
    "dataProduct": {"label": "数据产品", "category": "governance"},
    # people and system objects
    "team": {"label": "团队", "category": "people"},
    "user": {"label": "用户", "category": "people"},
    "kpi": {"label": "KPI", "category": "system"},
    "eventSubscription": {"label": "告警订阅", "category": "system"},
}

SERVICE_TYPES = {name for name, spec in ENTITY_TYPES.items() if spec["category"] == "service"}
DATA_ASSET_TYPES = [name for name, spec in ENTITY_TYPES.items() if spec.get("data_asset")]
COLUMN_TYPES = {name for name, spec in ENTITY_TYPES.items() if spec.get("columns")}
GOVERNANCE_TYPES = {"glossary", "glossaryTerm", "classification", "tag", "domain", "dataProduct"}
#: Types whose children are found by parent FQN on the 元数据资产 tree.
TREE_CHILDREN: dict[str, list[str]] = {
    "databaseService": ["database"],
    "database": ["databaseSchema"],
    "databaseSchema": ["table", "storedProcedure"],
    "messagingService": ["topic"],
    "dashboardService": ["dashboard", "chart", "dashboardDataModel"],
    "pipelineService": ["pipeline"],
    "mlmodelService": ["mlmodel"],
    "storageService": ["container"],
    "container": ["container"],
    "searchService": ["searchIndex"],
    "apiService": ["apiCollection"],
    "apiCollection": ["apiEndpoint"],
    "glossary": ["glossaryTerm"],
    "glossaryTerm": ["glossaryTerm"],
    "classification": ["tag"],
    "tag": ["tag"],
    "domain": ["domain"],
}

# ----- service categories and their connectors (OpenMetadata's catalog) ------------------------
#: Lattice data-source connector type -> the OpenMetadata-style service type name.
LATTICE_CONNECTOR_TYPES: dict[str, str] = {
    "duckdb": "DuckDB",
    "mysql": "Mysql",
    "postgresql": "Postgres",
    "clickhouse": "Clickhouse",
    "starrocks": "StarRocks",
    "doris": "Doris",
    "hive": "Hive",
    "iceberg": "Iceberg",
    "paimon": "Paimon",
    "oracle": "Oracle",
    "mssql": "Mssql",
    "mongodb": "MongoDB",
    "elasticsearch": "ElasticSearch",
    "kafka": "Kafka",
}
POLARIS_SERVICE_TYPE = "Polaris"
SEMANTIC_SERVICE_TYPE = "Ossie"

SERVICE_CATEGORIES: dict[str, dict[str, Any]] = {
    "database": {
        "label": "数据库",
        "description": "从最流行的数据库类型服务中提取元数据",
        "entity_type": "databaseService",
        "children": ["database", "databaseSchema", "table"],
        "connectors": [
            "Athena", "AzureSQL", "BigQuery", "BigTable", "Clickhouse", "Couchbase", "Databricks",
            "Datalake", "Db2", "DeltaLake", "DomoDatabase", "Doris", "Druid", "DuckDB", "DynamoDB",
            "Glue", "Greenplum", "Hive", "Iceberg", "Impala", "MariaDB", "MongoDB", "Mssql", "Mysql",
            "Oracle", "Paimon", "PinotDB", "Polaris", "Postgres", "Presto", "Redshift", "Salesforce",
            "SapErp", "SapHana", "SAS", "SingleStore", "Snowflake", "SQLite", "StarRocks", "Teradata",
            "Trino", "UnityCatalog", "Vertica", "CustomDatabase",
        ],
    },
    "messaging": {
        "label": "消息队列",
        "description": "从最常用的消息队列类型服务中提取元数据",
        "entity_type": "messagingService",
        "children": ["topic"],
        "connectors": ["Kafka", "Kinesis", "Redpanda", "CustomMessaging"],
    },
    "dashboard": {
        "label": "仪表板",
        "description": "从最流行的仪表板类型服务中提取元数据",
        "entity_type": "dashboardService",
        "children": ["dashboard", "chart", "dashboardDataModel"],
        "connectors": [
            "DomoDashboard", "Lightdash", "Looker", "Metabase", "Mode", "Mstr", "PowerBI", "QlikCloud",
            "QlikSense", "QuickSight", "Redash", "Superset", "Tableau", "CustomDashboard",
        ],
    },
    "pipeline": {
        "label": "工作流",
        "description": "从最常用的工作流类型服务中提取元数据",
        "entity_type": "pipelineService",
        "children": ["pipeline"],
        "connectors": [
            "Airbyte", "Airflow", "Dagster", "DatabricksPipeline", "DBTCloud", "DomoPipeline", "Fivetran",
            "Flink", "GluePipeline", "KafkaConnect", "Nifi", "OpenLineage", "Spark", "Spline",
            "CustomPipeline",
        ],
    },
    "mlmodel": {
        "label": "机器学习模型",
        "description": "通过 UI 界面，从机器学习模型类型服务中提取元数据",
        "entity_type": "mlmodelService",
        "children": ["mlmodel"],
        "connectors": ["Mlflow", "SageMaker", "CustomMlModel"],
    },
    "storage": {
        "label": "存储",
        "description": "从最流行的存储类型服务中提取元数据",
        "entity_type": "storageService",
        "children": ["container"],
        "connectors": ["S3", "GCS", "ADLS", "CustomStorage"],
    },
    "search": {
        "label": "搜索",
        "description": "从最流行的搜索服务中提取元数据",
        "entity_type": "searchService",
        "children": ["searchIndex"],
        "connectors": ["ElasticSearch", "OpenSearch", "CustomSearch"],
    },
    "metadata": {
        "label": "元数据",
        "description": "通过 UI 界面，从元数据类型服务中提取元数据",
        "entity_type": "metadataService",
        "children": [],
        "connectors": ["OpenMetadata", "Amundsen", "Atlas", "Alation", "AlationSink"],
    },
    "api": {
        "label": "APIs",
        "description": "从最常用的 API 服务中提取元数据",
        "entity_type": "apiService",
        "children": ["apiCollection", "apiEndpoint"],
        "connectors": ["Rest"],
    },
}
AUTOMATED_CONNECTORS = set(LATTICE_CONNECTOR_TYPES.values()) | {POLARIS_SERVICE_TYPE}

# ----- seed governance objects ----------------------------------------------------------------
SEED_CLASSIFICATIONS: list[dict[str, Any]] = [
    {
        "name": "PII",
        "display_name": "PII",
        "description": "个人身份信息（Personally Identifiable Information）。",
        "mutually_exclusive": True,
        "tags": [
            {"name": "Sensitive", "description": "敏感的个人身份信息，例如身份证号、手机号、邮箱。"},
            {"name": "NonSensitive", "description": "非敏感的个人身份信息。"},
            {"name": "None", "description": "不含个人身份信息。"},
        ],
    },
    {
        "name": "PersonalData",
        "display_name": "PersonalData",
        "description": "GDPR 意义上的个人数据。",
        "mutually_exclusive": True,
        "tags": [
            {"name": "Personal", "description": "可直接或间接识别自然人的数据。"},
            {"name": "SpecialCategory", "description": "特殊类别的个人数据，如健康、宗教、生物特征。"},
        ],
    },
    {
        "name": TIER_CLASSIFICATION,
        "display_name": "Tier",
        "description": "数据资产的重要性分级。",
        "mutually_exclusive": True,
        "tags": [
            {"name": "Tier1", "description": "关键业务资产：影响收入、合规或核心决策。"},
            {"name": "Tier2", "description": "重要资产：被多个团队或产品依赖。"},
            {"name": "Tier3", "description": "部门级资产：在单个团队内使用。"},
            {"name": "Tier4", "description": "个人或临时资产。"},
            {"name": "Tier5", "description": "可废弃的资产。"},
        ],
    },
]
SEED_TEAMS: list[dict[str, Any]] = [
    {"name": DEFAULT_TEAM, "display_name": "组织", "team_type": "Organization", "description": "组织根团队。"},
    {"name": "DataPlatform", "display_name": "数据平台组", "team_type": "Group", "description": "负责本地数据平台的团队。", "parent": DEFAULT_TEAM},
]
SEED_USERS: list[dict[str, Any]] = [
    {"name": SYSTEM_USER, "display_name": "Lattice", "email": "lattice@localhost", "is_admin": True, "teams": ["DataPlatform"]},
]
GLOSSARY_TERM_STATUSES = ("Draft", "In Review", "Approved", "Deprecated", "Rejected")
DOMAIN_TYPES = ("Aggregate", "Consumer-aligned", "Source-aligned")
TEAM_TYPES = ("Organization", "BusinessUnit", "Division", "Department", "Group")
TABLE_TYPES = ("Regular", "External", "View", "SecureView", "MaterializedView", "Iceberg", "Paimon", "Local", "Partitioned", "Foreign")
CUSTOM_PROPERTY_TYPES = ("string", "markdown", "integer", "number", "boolean", "date", "enum", "entityReference")
EVENT_TYPES = ("entityCreated", "entityUpdated", "entitySoftDeleted", "entityRestored", "entityDeleted")
CHANGE_KINDS = ("description", "displayName", "owners", "tags", "tier", "domain", "columns", "extension", "lineage", "glossaryTerms", "custom")

# ----- FQN helpers ----------------------------------------------------------------------------
def quote_part(part: str) -> str:
    if FQN_SEPARATOR in part or FQN_QUOTE in part:
        return FQN_QUOTE + part.replace(FQN_QUOTE, "\\" + FQN_QUOTE) + FQN_QUOTE
    return part


def build_fqn(*parts: str) -> str:
    return FQN_SEPARATOR.join(quote_part(str(part)) for part in parts if part is not None and part != "")


def split_fqn(fqn: str) -> list[str]:
    """Split ``a.b."c.d"`` into ``["a", "b", "c.d"]`` (OpenMetadata quoting)."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    text = fqn or ""
    while index < len(text):
        char = text[index]
        if quoted:
            if char == "\\" and index + 1 < len(text) and text[index + 1] == FQN_QUOTE:
                current.append(FQN_QUOTE)
                index += 2
                continue
            if char == FQN_QUOTE:
                quoted = False
            else:
                current.append(char)
        elif char == FQN_QUOTE:
            quoted = True
        elif char == FQN_SEPARATOR:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def parent_fqn(fqn: str) -> str:
    parts = split_fqn(fqn)
    return build_fqn(*parts[:-1]) if len(parts) > 1 else ""


def leaf_name(fqn: str) -> str:
    parts = split_fqn(fqn)
    return parts[-1] if parts else ""


def child_fqn(parent: str, name: str) -> str:
    return build_fqn(*split_fqn(parent), name) if parent else build_fqn(name)


# ----- validation ------------------------------------------------------------------------------
def check_name(value: Any, what: str = "名称") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what}必须是文本。")
    name = value.strip()
    if not name or not NAME.match(name):
        raise ValueError(f"{what}须为 1–{NAME_MAX} 个字符，不能包含控制字符。")
    return name


def check_text(value: Any, what: str, limit: int = 20000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what}必须是文本。")
    if len(value) > limit:
        raise ValueError(f"{what}不能超过 {limit} 个字符。")
    if "\x00" in value:
        raise ValueError(f"{what}包含非法字符。")
    return value.strip()


def check_entity_type(value: Any) -> str:
    if not isinstance(value, str) or value not in ENTITY_TYPES:
        raise ValueError(f"未知的实体类型：{value!r}。")
    return value


def type_label(entity_type: str) -> str:
    return ENTITY_TYPES.get(entity_type, {}).get("label", entity_type)


def parent_types(entity_type: str) -> list[str]:
    parent = ENTITY_TYPES[entity_type].get("parent")
    if parent is None:
        return []
    return list(parent) if isinstance(parent, list) else [parent]


def service_type_of(entity_type: str) -> str | None:
    """The service entity type that roots ``entity_type``'s hierarchy, if any."""
    category = ENTITY_TYPES[entity_type].get("service_category")
    if not category:
        return None
    return SERVICE_CATEGORIES[category]["entity_type"]


def category_of_service(service_entity_type: str) -> str | None:
    for key, spec in SERVICE_CATEGORIES.items():
        if spec["entity_type"] == service_entity_type:
            return key
    return None


# ----- column data types ------------------------------------------------------------------------
_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("BOOLEAN", ("bool",)),
    ("TINYINT", ("tinyint", "int8", "uint8")),
    ("SMALLINT", ("smallint", "int16", "uint16")),
    ("BIGINT", ("bigint", "int64", "uint64", "hugeint", "long")),
    ("INT", ("int", "integer", "int32", "uint32", "serial")),
    ("DECIMAL", ("decimal", "numeric", "money")),
    ("DOUBLE", ("double", "float8", "float64", "real")),
    ("FLOAT", ("float", "float32")),
    ("TIMESTAMP", ("timestamp", "datetime")),
    ("DATE", ("date",)),
    ("TIME", ("time",)),
    ("INTERVAL", ("interval",)),
    ("JSON", ("json", "jsonb", "variant")),
    ("UUID", ("uuid",)),
    ("BINARY", ("blob", "bytea", "binary", "bytes", "varbinary")),
    ("ARRAY", ("array", "list", "[]")),
    ("MAP", ("map",)),
    ("STRUCT", ("struct", "row", "record", "tuple")),
    ("ENUM", ("enum",)),
    ("TEXT", ("text", "clob", "longtext", "mediumtext")),
    ("CHAR", ("char", "bpchar", "fixedstring")),
    ("VARCHAR", ("varchar", "string", "character varying", "nvarchar", "str")),
]


def normalize_data_type(raw: Any) -> str:
    """Map an engine-specific type such as ``varchar(255)`` or ``Nullable(Int32)`` onto OpenMetadata's names."""
    text = str(raw or "").strip().lower()
    if not text:
        return "UNKNOWN"
    inner = text
    for wrapper in ("nullable(", "lowcardinality("):
        if inner.startswith(wrapper) and inner.endswith(")"):
            inner = inner[len(wrapper) : -1]
    base = re.split(r"[(<\[ ]", inner, 1)[0].strip() or inner
    if "[]" in inner or inner.startswith(("array", "list")):
        return "ARRAY"
    for canonical, keys in _TYPE_RULES:
        if base in keys:
            return canonical
    for canonical, keys in _TYPE_RULES:
        if any(base.startswith(key) for key in keys if len(key) > 3):
            return canonical
    return base.upper()[:32] or "UNKNOWN"


def data_length(raw: Any) -> int | None:
    match = re.search(r"\((\d+)", str(raw or ""))
    return int(match.group(1)) if match else None


# ----- documents ------------------------------------------------------------------------------
def search_text(document: dict[str, Any], entity_type: str, fqn: str) -> str:
    """Lower-cased text the store's LIKE search runs over."""
    parts = [
        entity_type,
        fqn,
        str(document.get("name") or ""),
        str(document.get("display_name") or ""),
        str(document.get("description") or "")[:2000],
        str(document.get("service_type") or ""),
        " ".join(str(item) for item in document.get("synonyms") or []),
    ]
    for column in (document.get("columns") or [])[:400]:
        if isinstance(column, dict):
            parts.append(str(column.get("name") or ""))
            parts.append(str(column.get("display_name") or ""))
            parts.append(str(column.get("description") or "")[:200])
    for field in ("ml_features", "tasks", "charts", "fields", "schema_fields"):
        for item in document.get(field) or []:
            if isinstance(item, dict):
                parts.append(str(item.get("name") or ""))
            elif isinstance(item, str):
                parts.append(item)
    return " ".join(part.lower() for part in parts if part)[:20000]


TRACKED_FIELDS = (
    "description", "display_name", "owners", "tags", "tier", "domain", "table_type",
    "extension", "status", "synonyms", "related_terms", "references", "glossary_terms",
    "algorithm", "ml_features", "ml_hyper_parameters", "tasks", "charts", "partitions",
    "schema_text", "endpoint_url", "request_method", "experts", "reviewers", "data_products",
    "domain_type", "mutually_exclusive", "style", "target_value", "metric_type", "chart",
    "start_date", "end_date", "enabled", "filters", "destinations", "team_type", "email",
    "is_admin", "source_url", "retention_period", "ingestion", "connection", "properties",
    "fields", "data_model", "file_formats", "index_type",
    "partition_keys", "columns_summary",
)


def change_description(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """OpenMetadata-style diff: fields added, updated and deleted between two documents."""
    added: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    previous = previous or {}
    for field in TRACKED_FIELDS:
        before = previous.get(field)
        after = current.get(field)
        if _empty(before) and _empty(after):
            continue
        if _empty(before):
            added.append({"name": field, "new_value": after})
        elif _empty(after):
            deleted.append({"name": field, "old_value": before})
        elif before != after:
            updated.append({"name": field, "old_value": before, "new_value": after})
    before_columns = {c.get("name"): c for c in previous.get("columns") or [] if isinstance(c, dict)}
    after_columns = {c.get("name"): c for c in current.get("columns") or [] if isinstance(c, dict)}
    for name, column in after_columns.items():
        if name not in before_columns:
            added.append({"name": f"columns.{name}", "new_value": _column_summary(column)})
            continue
        old = before_columns[name]
        for key in ("description", "data_type_display", "display_name", "tags", "constraint"):
            if _empty(old.get(key)) and _empty(column.get(key)):
                continue
            if old.get(key) != column.get(key):
                updated.append(
                    {"name": f"columns.{name}.{key}", "old_value": old.get(key), "new_value": column.get(key)}
                )
    for name, column in before_columns.items():
        if name not in after_columns:
            deleted.append({"name": f"columns.{name}", "old_value": _column_summary(column)})
    return {"fields_added": added, "fields_updated": updated, "fields_deleted": deleted}


def _column_summary(column: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": column.get("name"),
        "data_type": column.get("data_type"),
        "data_type_display": column.get("data_type_display"),
    }


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def is_major_change(change: dict[str, Any]) -> bool:
    """Schema changes (a column removed or retyped) bump the major version, as in OpenMetadata."""
    if any(str(item.get("name", "")).startswith("columns.") for item in change.get("fields_deleted", [])):
        return True
    return any(
        str(item.get("name", "")).startswith("columns.") and str(item.get("name", "")).endswith("data_type_display")
        for item in change.get("fields_updated", [])
    )


def next_version(current: float, major: bool) -> float:
    if major:
        return float(int(current) + 1)
    return round(current + 0.1, 1)


def describe_change(change: dict[str, Any]) -> str:
    """A short Chinese sentence for the activity feed."""
    pieces: list[str] = []
    labels = {
        "description": "描述", "display_name": "显示名称", "owners": "所有者", "tags": "标签", "tier": "分级",
        "domain": "数据域", "extension": "自定义属性", "status": "状态", "glossary_terms": "术语",
        "columns": "字段", "synonyms": "同义词", "related_terms": "相关术语", "data_products": "数据产品",
        "experts": "专家", "reviewers": "审核者", "ingestion": "拾取配置", "connection": "连接",
    }

    def label(name: str) -> str:
        head = name.split(".")[0]
        text = labels.get(head, head)
        if head == "columns" and name.count(".") >= 1:
            parts = name.split(".")
            text = f"字段 {parts[1]}" + (f" 的{labels.get(parts[2], parts[2])}" if len(parts) > 2 else "")
        return text

    for item in change.get("fields_added", [])[:4]:
        pieces.append(f"新增{label(item['name'])}")
    for item in change.get("fields_updated", [])[:4]:
        pieces.append(f"更新{label(item['name'])}")
    for item in change.get("fields_deleted", [])[:4]:
        pieces.append(f"删除{label(item['name'])}")
    total = sum(len(change.get(key, [])) for key in ("fields_added", "fields_updated", "fields_deleted"))
    if total > len(pieces):
        pieces.append(f"等共 {total} 项变更")
    return "，".join(pieces) if pieces else "无字段变更"
