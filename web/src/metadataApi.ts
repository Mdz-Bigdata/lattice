// SPDX-License-Identifier: Apache-2.0
/**
 * Types and helpers of the 元数据管理 module (/api/metadata).
 *
 * The backend is an OpenMetadata-style catalog: every asset is an entity with a fully qualified
 * name (FQN), a type, owners, tags, glossary terms, a tier, a domain and a version history. The
 * screens address entities by type plus FQN, and keep their own route in the URL hash after
 * `#metadata/` so a reload, a tab switch or a shared link lands on the same screen.
 */
import { request } from "./api";

export interface EntityRef {
  id: string;
  entity_type: string;
  type_label: string;
  name: string;
  display_name: string;
  fqn: string;
  service_type: string;
  deleted: boolean;
}
export interface TagLabel {
  tag_fqn: string;
  source: "classification" | "glossary";
  label_type: string;
  state: string;
  name: string;
  display_name: string;
  root: string;
  description: string;
  style: { color?: string; icon_url?: string };
  deleted: boolean;
}
export interface Column {
  name: string;
  display_name: string;
  description: string;
  data_type: string;
  data_type_display: string;
  data_length?: number | null;
  constraint: string;
  ordinal_position: number;
  fqn: string;
  tags: TagLabel[];
  glossary_terms: TagLabel[];
  children?: Column[];
}
export interface Entity {
  id: string;
  entity_type: string;
  type_label: string;
  name: string;
  display_name: string;
  fqn: string;
  parent_fqn: string;
  service_fqn: string;
  service_type: string;
  description: string;
  tier: string | null;
  domain: EntityRef | null;
  deleted: boolean;
  version: number;
  updated_at: number;
  updated_by: string;
  created_at: number;
  owners: EntityRef[];
  tags: TagLabel[];
  glossary_terms: TagLabel[];
  columns?: Column[];
  column_count?: number;
  data_products?: EntityRef[];
  followers?: EntityRef[];
  followers_count?: number;
  children_count?: number;
  usage?: { queries: number; views: number };
  service?: EntityRef | null;
  parent?: EntityRef | null;
  breadcrumb?: EntityRef[];
  extension?: Record<string, unknown>;
  style?: { color?: string; icon_url?: string };
  [key: string]: unknown;
}
export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}
export interface Facet {
  value: string;
  count: number;
}
export interface SearchResult extends Paged<Entity> {
  facets: Record<string, Facet[]>;
  truncated?: boolean;
}
export interface TreeCategory {
  id: string;
  label: string;
  entity_type: string;
  services: Entity[];
  asset_count: number;
}
export interface TreeResponse {
  categories: TreeCategory[];
  governance: { id: string; label: string; entity_type: string; count: number }[];
  counts: Record<string, number>;
}
export interface ChildGroup {
  entity_type: string;
  label: string;
  items: Entity[];
  total: number;
}
export interface ChildrenResponse {
  entity: EntityRef;
  groups: ChildGroup[];
}
export interface FieldChange {
  name: string;
  old_value?: unknown;
  new_value?: unknown;
}
export interface ChangeDescription {
  fields_added?: FieldChange[];
  fields_updated?: FieldChange[];
  fields_deleted?: FieldChange[];
  summary?: string;
  kinds?: string[];
}
export interface VersionItem {
  version: number;
  updated_at: number;
  updated_by: string;
  change: ChangeDescription;
  summary: string;
}
export interface FeedEvent {
  id: string;
  event_type: string;
  event_label: string;
  entity_type: string;
  type_label: string;
  entity_id: string;
  entity_fqn: string;
  entity_name: string;
  user_name: string;
  ts: number;
  previous_version: number | null;
  current_version: number | null;
  change: ChangeDescription;
  summary: string;
}
export interface Thread {
  id: string;
  thread_type: "Conversation" | "Task" | "Announcement";
  about_fqn: string;
  about_type: string;
  message: string;
  created_by: string;
  created_at: number;
  updated_at: number;
  resolved: boolean;
  posts: { id: string; message: string; from: string; ts: number }[];
  task: {
    task_type: string;
    status: string;
    assignees: string[];
    suggestion: unknown;
    column: string;
    resolved_by?: string;
  } | null;
  announcement: { start_ts: number; end_ts: number; title: string } | null;
}
export interface ColumnLineage {
  from_columns: string[];
  to_column: string;
  function: string;
}
export interface LineageNode {
  id?: string;
  fqn: string;
  entity_type: string;
  type_label: string;
  name: string;
  display_name: string;
  service_type?: string;
  service_fqn?: string;
  tier?: string | null;
  deleted: boolean;
  description?: string;
  depth: number;
  columns: string[];
  missing: boolean;
}
export interface LineageEdge {
  id: string;
  from_fqn: string;
  from_type: string;
  to_fqn: string;
  to_type: string;
  source: string;
  sql: string;
  description: string;
  columns: ColumnLineage[];
  pipeline_fqn: string;
  created_at?: number;
  updated_at?: number;
}
export interface LineageGraph {
  entity: EntityRef;
  nodes: LineageNode[];
  edges: LineageEdge[];
  upstream_depth: number;
  downstream_depth: number;
  truncated: boolean;
  upstream_count: number;
  downstream_count: number;
}
export interface SqlLineage {
  kind: string;
  dialect: string;
  target: EntityRef | null;
  target_reference: string;
  sources: (EntityRef & { reference: string })[];
  unresolved: string[];
  columns: ColumnLineage[];
  applied: number;
}
export interface Impact {
  entity: EntityRef;
  total: number;
  by_type: { entity_type: string; label: string; count: number }[];
  items: LineageNode[];
  truncated: boolean;
  depth: number;
}
export interface ServiceCategory {
  id: string;
  label: string;
  description: string;
  entity_type: string;
  children: string[];
  connectors: { id: string; automated: boolean }[];
}
export interface TypesResponse {
  entity_types: {
    id: string;
    label: string;
    category: string;
    service_category: string | null;
    parents: string[];
    children: string[];
    has_columns: boolean;
    data_asset: boolean;
  }[];
  service_categories: ServiceCategory[];
  lattice_connectors: Record<string, string>;
  term_statuses: string[];
  domain_types: string[];
  team_types: string[];
  table_types: string[];
  custom_property_types: string[];
  event_types: { id: string; label: string }[];
  change_kinds: string[];
  thread_types: string[];
  task_types: string[];
  kpi_charts: { id: string; label: string; metric_type: string; field: string }[];
  tier_options: string[];
  edge_sources: string[];
  destination_types: string[];
  max_lineage_depth: number;
}
export interface RunSummary {
  databases: number;
  schemas: number;
  tables: number;
  semantic_models: number;
  columns: number;
  created: number;
  updated: number;
  unchanged: number;
  restored: number;
  deleted: number;
  lineage_edges: number;
  view_edges: number;
  errors: string[];
}
export interface IngestionRun {
  id: string;
  service_fqn: string;
  run_type: string;
  status: "running" | "success" | "partial" | "failed";
  trigger: string;
  started_at: number;
  finished_at: number | null;
  summary: Partial<RunSummary>;
  message: string;
  service?: EntityRef | null;
  elapsed_ms?: number | null;
}
export interface IngestionSettings {
  enabled: boolean;
  interval_minutes: number;
  last_run_at: number | null;
  last_status: string;
  next_run_at: number | null;
  options: Record<string, unknown>;
}
export interface IngestionService extends Entity {
  ingestion: IngestionSettings;
  last_run: IngestionRun | null;
  running: boolean;
  datasource: { id: string; name: string; type_label: string; status: string } | null;
  automated: boolean;
  asset_count: number;
}
export interface InsightTypeRow {
  entity_type: string;
  label: string;
  total: number;
  with_description: number;
  with_owner: number;
  with_tier: number;
  description_percent: number;
  owner_percent: number;
  tier_percent: number;
}
export interface InsightFigures {
  day?: string;
  assets: number;
  with_description: number;
  with_owner: number;
  with_tier: number;
  with_tags: number;
  with_glossary_terms: number;
  with_lineage: number;
  used_30d: number;
  unused_30d: number;
  description_percent: number;
  owner_percent: number;
  tier_percent: number;
  tag_percent: number;
  lineage_percent: number;
  by_type: InsightTypeRow[];
  by_service_type?: { service_type: string; count: number }[];
  governance?: Record<string, number>;
}
export interface Kpi extends Entity {
  chart_label: string;
  field: string;
  current_value: number;
  target_value: number;
  progress: number;
  status: "pending" | "achieved" | "expired" | "on_track" | "at_risk";
  status_label: string;
  days_left: number;
  history: { day: string; value: number }[];
}
export interface UsageItem {
  target_fqn: string;
  total: number;
  entity: EntityRef | null;
}
export interface Insights {
  period: { days: number; from: string; to: string };
  /** True when a team, tier or domain filter is applied: no history or deltas then. */
  filtered?: boolean;
  current: InsightFigures;
  series: InsightFigures[];
  change: Record<string, number>;
  kpis: Kpi[];
  top_viewed: UsageItem[];
  top_queried: UsageItem[];
  active_users: { user_name: string; total: number; last_ts: number }[];
  events: Record<string, number>;
}
export interface TagOption {
  fqn: string;
  name: string;
  display_name: string;
  description: string;
  source: "classification" | "glossary";
  root: string;
  status: string;
  style: { color?: string; icon_url?: string };
}
export interface PersonOption extends EntityRef {
  is_bot: boolean;
}
export interface Notification {
  id: string;
  subscription_id: string;
  event_id: string;
  ts: number;
  status: string;
  detail: string;
  json: {
    subscription_name?: string;
    destination?: string;
    event_type?: string;
    entity_type?: string;
    entity_fqn?: string;
    entity_name?: string;
    user_name?: string;
    summary?: string;
  };
  subscription: EntityRef | null;
}
export interface TermView {
  fqn: string;
  name: string;
  display_name: string;
  description: string;
  glossary: string;
  status: string;
  synonyms: string[];
}
export interface ContextHit {
  fqn: string;
  entity_type: string;
  type_label: string;
  name: string;
  display_name: string;
  description: string;
  owners: string[];
  tags: string[];
  glossary_terms: string[];
  columns: string[];
}
export interface ContextCard {
  entity: Entity;
  glossary: TermView[];
  lineage: {
    upstream: (EntityRef & { source: string; description: string })[];
    downstream: (EntityRef & { source: string; description: string })[];
  };
  queries: { sql: string; datasource_id: string; ts: number }[];
  usage: { queries: number; views: number };
  markdown: string;
}
export interface AskResult {
  question: string;
  answer: string;
  configured: boolean;
  provider: string;
  model: string;
  context: { markdown: string; entities: ContextHit[]; terms: TermView[] };
  detail: string;
}
export interface Suggestion {
  entity: { fqn: string; entity_type: string; display_name: string };
  description: string;
  current_description: string;
  columns: { name: string; description: string; current: string }[];
  provider: string;
  model: string;
}
export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: {
    properties?: Record<string, { type?: string; description?: string }>;
    required?: string[];
  };
}
export interface McpStatus {
  name: string;
  version: string;
  protocol_versions: string[];
  sessions: number;
  calls: number;
  endpoint: string;
  user: string;
  url: string;
  clients: Record<string, unknown>;
  tool_definitions: ToolDefinition[];
  resources: { uri: string; name: string; description: string }[];
  resource_templates: { uriTemplate: string; name: string; description: string }[];
  prompts: { name: string; description: string }[];
}
export interface MetadataHealth {
  ok: boolean;
  backend: string;
  location: string;
  schema: string;
  fallback: boolean;
  fallback_reason: string;
  detail: string;
  entities?: number;
  boot_error?: string;
  scheduler?: { running: boolean; last_error: string } | null;
  mcp?: { sessions: number; calls: number } | null;
}

/* ------------------------------------------------------------------ requests */

type Params = Record<string, string | number | boolean | null | undefined>;

export function metadataPath(path: string, params: Params = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const text = query.toString();
  return `/api/metadata${path}${text ? `?${text}` : ""}`;
}
export function mdGet<T>(path: string, params: Params = {}): Promise<T> {
  return request<T>(metadataPath(path, params));
}
export function mdPost<T>(path: string, body: unknown = {}): Promise<T> {
  return request<T>(`/api/metadata${path}`, body);
}

/* ------------------------------------------------------------------ labels */

export const ASSET_TYPES = [
  "table",
  "storedProcedure",
  "topic",
  "dashboard",
  "chart",
  "dashboardDataModel",
  "pipeline",
  "mlmodel",
  "container",
  "searchIndex",
  "apiCollection",
  "apiEndpoint",
  "semanticModel",
];
export const SERVICE_ENTITY_TYPES = [
  "databaseService",
  "messagingService",
  "dashboardService",
  "pipelineService",
  "mlmodelService",
  "storageService",
  "searchService",
  "apiService",
  "metadataService",
];
export const COLUMN_TYPES = new Set([
  "table",
  "topic",
  "dashboardDataModel",
  "container",
  "searchIndex",
]);
export const TYPE_LABELS: Record<string, string> = {
  databaseService: "数据库服务",
  database: "数据库",
  databaseSchema: "数据库模式",
  table: "数据表",
  storedProcedure: "存储过程",
  messagingService: "消息服务",
  topic: "消息主题",
  dashboardService: "仪表板服务",
  dashboard: "仪表板",
  chart: "图表",
  dashboardDataModel: "仪表板数据模型",
  pipelineService: "工作流服务",
  pipeline: "工作流",
  mlmodelService: "机器学习模型服务",
  mlmodel: "机器学习模型",
  storageService: "存储服务",
  container: "存储容器",
  searchService: "搜索服务",
  searchIndex: "搜索索引",
  apiService: "API 服务",
  apiCollection: "API 集合",
  apiEndpoint: "API 端点",
  metadataService: "元数据服务",
  semanticModel: "语义模型",
  glossary: "术语库",
  glossaryTerm: "术语",
  classification: "分类",
  tag: "标签",
  domain: "数据域",
  dataProduct: "数据产品",
  team: "团队",
  user: "用户",
  kpi: "KPI",
  eventSubscription: "告警订阅",
};
export const CHILD_TYPES: Record<string, string[]> = {
  databaseService: ["database"],
  database: ["databaseSchema"],
  databaseSchema: ["table", "storedProcedure"],
  messagingService: ["topic"],
  dashboardService: ["dashboard", "chart", "dashboardDataModel"],
  pipelineService: ["pipeline"],
  mlmodelService: ["mlmodel"],
  storageService: ["container"],
  container: ["container"],
  searchService: ["searchIndex"],
  apiService: ["apiCollection"],
  apiCollection: ["apiEndpoint"],
};
export const RUN_STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  success: "成功",
  partial: "部分成功",
  failed: "失败",
  skipped: "已跳过",
};
export const TERM_STATUS_LABELS: Record<string, string> = {
  Draft: "草稿",
  "In Review": "审核中",
  Approved: "已批准",
  Deprecated: "已弃用",
  Rejected: "已拒绝",
};

export function typeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type;
}
export function entityName(entity: { display_name?: string; name: string }): string {
  return entity.display_name || entity.name;
}
export function tierLabel(tier?: string | null): string {
  if (!tier) return "";
  const parts = tier.split(".");
  return parts[parts.length - 1] ?? tier;
}
export function isAsset(type: string): boolean {
  return ASSET_TYPES.includes(type);
}
export function pad(value: number): string {
  return String(value).padStart(2, "0");
}
export function formatTime(ms?: number | null): string {
  if (!ms) return "—";
  const date = new Date(ms);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
export function timeAgo(ms?: number | null): string {
  if (!ms) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 30 * 86400) return `${Math.floor(seconds / 86400)} 天前`;
  return formatTime(ms);
}
export function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`;
}
/** Last FQN part, honouring OpenMetadata's quoting of names that contain dots. */
export function leafName(fqn: string): string {
  const parts = splitFqn(fqn);
  return parts[parts.length - 1] ?? fqn;
}
export function splitFqn(fqn: string): string[] {
  const parts: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < fqn.length; index += 1) {
    const char = fqn[index];
    if (quoted) {
      if (char === "\\" && fqn[index + 1] === '"') {
        current += '"';
        index += 1;
      } else if (char === '"') quoted = false;
      else current += char;
    } else if (char === '"') quoted = true;
    else if (char === ".") {
      parts.push(current);
      current = "";
    } else current += char;
  }
  parts.push(current);
  return parts;
}
export function quoteFqnPart(part: string): string {
  return part.includes(".") || part.includes('"')
    ? `"${part.replaceAll('"', '\\"')}"`
    : part;
}
export function childFqn(parent: string, name: string): string {
  return parent ? `${parent}.${quoteFqnPart(name)}` : quoteFqnPart(name);
}

/* ------------------------------------------------------------------ routes */

export interface MetadataRoute {
  view: string;
  parts: string[];
}
export const METADATA_PAGE = "metadata";

export function parseRoute(hash: string = window.location.hash): MetadataRoute {
  const raw = hash.replace(/^#/, "");
  const segments = raw.split("/");
  if (segments[0] !== METADATA_PAGE) return { view: "explore", parts: [] };
  const decoded = segments.slice(1).map((segment) => {
    try {
      return decodeURIComponent(segment);
    } catch {
      return segment;
    }
  });
  return { view: decoded[0] || "explore", parts: decoded.slice(1) };
}
export function routeHash(view: string, ...parts: (string | undefined)[]): string {
  const clean = parts.filter((part): part is string => !!part);
  return [METADATA_PAGE, view, ...clean].map(encodeURIComponent).join("/");
}
export function navigate(view: string, ...parts: (string | undefined)[]): void {
  window.location.hash = routeHash(view, ...parts);
}
export function openEntity(entityType: string, fqn: string, tab?: string): void {
  navigate("entity", entityType, fqn, tab);
}
