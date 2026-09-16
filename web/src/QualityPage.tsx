// SPDX-License-Identifier: Apache-2.0
/**
 * The 数据质量 console: five internal tabs over the /api/quality routes.
 *
 * All five screens live in one component tree because they share the metric catalog, the
 * registered data sources and the selected tab; sibling pages would refetch the catalog on every
 * switch and lose the filters. Each tab owns its request state and guards it with a monotonic
 * request counter, so a slow reply from an abandoned filter can never overwrite newer rows.
 *
 * Every endpoint is optional at runtime. The quality metadata database may be unreachable and the
 * routes may not be mounted yet, so the catalog, the dimension tree and the option lists all have
 * built-in fallbacks and every failure surfaces as an ErrorBanner instead of an empty screen.
 *
 * One field is added to the 核查规则编辑 form beyond the product screens: Schema, between 数据源
 * and 数据表. A rule stores schema_name, and a table name alone is ambiguous on every dialect that
 * has schemas.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Code2,
  Eye,
  FileBarChart,
  History,
  ListChecks,
  ListTree,
  Pencil,
  PieChart,
  Play,
  Plus,
  Power,
  RefreshCw,
  RotateCcw,
  ScrollText,
  Search,
  ShieldCheck,
  Table2,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  request,
  errorMessage,
  datasourcePath,
  type ColumnInfo,
  type DataSource,
  type TableDetail,
} from "./api";
import {
  DataGrid,
  Drawer,
  Empty,
  ErrorBanner,
  Loading,
  PageTitle,
} from "./components";
import { AiRulesDrawer } from "./AiPanels";
import {
  preferredSchema,
  sourceLabel,
  SourceSelect,
  type GoTo,
  useApi,
  useSchemas,
  useTables,
} from "./SourceBrowser";
import type { DataPageProps } from "./DataPages";

/* ------------------------------------------------------------------ API shapes */

interface Paged<T> {
  items: T[];
  total: number;
}
interface Option {
  value: string;
  label: string;
}
/** The catalog may serialise an option either as an object or as a (value, label) pair. */
type RawOption = Option | [string, string];

interface ConfigField {
  name: string;
  label: string;
  type: string;
  required?: boolean;
  default?: unknown;
  placeholder?: string;
  help?: string;
  options?: RawOption[];
}
interface CatalogMetric {
  id: string;
  label: string;
  level?: string;
  fields: ConfigField[];
  needs_column?: boolean;
}
interface CatalogDimension {
  id: string;
  label: string;
  metrics: CatalogMetric[];
}
interface MetricCatalog {
  dimensions: CatalogDimension[];
  operators?: RawOption[];
  expected_types?: RawOption[];
  result_formulas?: RawOption[];
  levels?: RawOption[];
}
interface QualityHealth {
  ok?: boolean;
  detail?: string;
  database?: string;
  schema?: string;
  scheduler?: {
    running?: boolean;
    jobs?: { id: number; name: string; next_fire_time: string | null }[];
  };
}
interface QualityRule {
  id: number;
  name: string;
  metric: string;
  metric_label: string;
  dimension: string;
  dimension_label: string;
  level: string;
  level_label: string;
  datasource_id: string;
  datasource_name: string;
  schema_name: string | null;
  table_name: string;
  column_name: string | null;
  config: Record<string, unknown>;
  expected_type: string;
  result_formula: string;
  operator: string;
  threshold: number;
  state: 0 | 1;
  state_label: string;
  comment: string;
  create_time: string;
  update_time: string;
}
interface QualitySchedule {
  id: number;
  name: string;
  bean_name: string;
  method_name: string;
  method_params: string;
  cron_expression: string;
  state: 0 | 1;
  state_label: string;
  last_fire_time: string | null;
  next_fire_time: string | null;
  create_time: string;
  task?: string;
  task_label?: string;
  retry_limit?: number;
  retry_delay_seconds?: number;
  misfire_policy?: string;
  misfire_policy_label?: string;
  max_backfill?: number;
  last_status?: string;
  last_status_label?: string;
  last_message?: string;
}
/** A job the scheduler can dispatch; the 调度 form offers these instead of free-text bean names. */
interface QualityTaskOption {
  key: string;
  bean_name: string;
  method_name: string;
  label: string;
  description: string;
  params_label: string;
  params_hint: string;
  params_required: boolean;
  category: string;
}
interface QualityTasks {
  items: QualityTaskOption[];
  misfire_policies: Option[];
  run_statuses: Option[];
}
interface QualityTemplate {
  id: number;
  name: string;
  description: string;
  metric: string;
  metric_label: string;
  dimension: string;
  dimension_label: string;
  level: string;
  level_label: string;
  config: Record<string, unknown>;
  expected_type: string;
  result_formula: string;
  operator: string;
  threshold: number;
  builtin: boolean;
  needs_column: boolean;
  create_time: string;
  update_time: string;
}
interface BatchOutcome {
  total: number;
  created: QualityRule[];
  errors: {
    datasource_id: string;
    schema_name: string | null;
    table_name: string;
    column_name: string | null;
    message: string;
  }[];
}
interface QualityTaskRun {
  id: number;
  schedule_id: number | null;
  schedule_name: string;
  task: string;
  task_label: string;
  trigger_type: string;
  trigger_label: string;
  attempt: number;
  status: string;
  status_label: string;
  planned_time: string | null;
  start_time: string;
  end_time: string | null;
  elapsed_ms: number | null;
  message: string;
  detail: Record<string, unknown>;
}
interface QualityResult {
  id: number;
  job_execution_id: number;
  rule_id: number | null;
  rule_name: string;
  metric_name: string;
  metric_label: string;
  metric_dimension: string;
  dimension_label: string;
  datasource_name: string;
  database_name: string;
  table_name: string;
  column_name: string | null;
  rule_level: string;
  level_label: string;
  checked_count: number;
  actual_value: number;
  expected_value: number | null;
  expected_type: string;
  result_formula: string;
  operator: string;
  threshold: number | null;
  score: number | null;
  state: 1 | 2;
  state_label: string;
  invalidate_sql: string;
  check_time: string;
}
interface QualityExecution {
  id: number;
  rule_id: number | null;
  rule_name: string;
  schedule_id: number | null;
  trigger_type: string;
  trigger_label: string;
  status: 0 | 1 | 2;
  status_label: string;
  start_time: string;
  end_time: string | null;
  elapsed_ms: number | null;
  message: string;
  results?: QualityResult[];
}
interface ReportRow {
  rule_name: string;
  datasource_name: string;
  table_name: string;
  column_name: string | null;
  /** Optional business names; the console falls back to the physical name when absent. */
  table_label?: string | null;
  column_label?: string | null;
  checked_count: number;
  actual_value: number;
  score: number | null;
}
interface QualityReport {
  date: string;
  datasource_errors: {
    datasource_name: string;
    level: string;
    level_label: string;
    error_count: number;
  }[];
  rule_errors: {
    dimension: string;
    dimension_label: string;
    rule_name: string;
    level: string;
    level_label: string;
    error_count: number;
  }[];
  dimension_sections: {
    dimension: string;
    dimension_label: string;
    rows: ReportRow[];
  }[];
  total_checked: number;
  total_errors: number;
  score: number | null;
  level_label: string;
}
interface QualityStatistics {
  tree: { dimension: string; dimension_label: string; error_count: number }[];
  items: QualityResult[];
}
interface RulePreview {
  actual_sql: string;
  total_sql: string;
  invalidate_sql: string;
}
/** The failure sample is a read-only projection; both the grid and the object form are accepted. */
interface FailureSample {
  columns?: string[];
  rows?: unknown[][];
  sql?: string;
  /** Set when the row set was cut short by the requested limit. */
  truncated?: boolean;
  elapsed_ms?: number;
  /** Chinese note from the backend, e.g. the lake-source scan cap. */
  warning?: string;
}

/* ------------------------------------------------------------------ constants */

const TABS = [
  { id: "rules", label: "质量规则管理", icon: ListChecks },
  { id: "schedules", label: "质量调度管理", icon: CalendarClock },
  { id: "report", label: "质量报告分析", icon: FileBarChart },
  { id: "statistics", label: "质量统计分析", icon: PieChart },
  { id: "executions", label: "质量执行日志", icon: ScrollText },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** The six categories of §1; used until GET /api/quality/metrics answers. */
const FALLBACK_DIMENSIONS: CatalogDimension[] = [
  { id: "uniqueness", label: "唯一性校验", metrics: [] },
  { id: "completeness", label: "完整性校验", metrics: [] },
  { id: "accuracy", label: "准确性校验", metrics: [] },
  { id: "standard", label: "数据标准校验", metrics: [] },
  { id: "relation", label: "关联性校验", metrics: [] },
  { id: "timeliness", label: "及时性校验", metrics: [] },
];
const FALLBACK_LEVELS: Option[] = [
  { value: "high", label: "高" },
  { value: "medium", label: "中" },
  { value: "low", label: "低" },
];
const FALLBACK_OPERATORS: Option[] = [
  { value: "lte", label: "小于等于" },
  { value: "lt", label: "小于" },
  { value: "gte", label: "大于等于" },
  { value: "gt", label: "大于" },
  { value: "eq", label: "等于" },
  { value: "ne", label: "不等于" },
];
const FALLBACK_EXPECTED_TYPES: Option[] = [
  { value: "fix_value", label: "固定值" },
  { value: "table_total_rows", label: "表总行数" },
];
const FALLBACK_FORMULAS: Option[] = [
  { value: "actual", label: "不合规数量" },
  { value: "percentage", label: "不合规占比（%）" },
];
const EXECUTION_STATUSES: Option[] = [
  { value: "0", label: "运行中" },
  { value: "1", label: "成功" },
  { value: "2", label: "失败" },
];
const PAGE_SIZES = [10, 20, 50, 100];

/* ------------------------------------------------------------------ helpers */

function toOptions(raw: RawOption[] | undefined, fallback: Option[]): Option[] {
  if (!raw?.length) return fallback;
  return raw.map((item) =>
    Array.isArray(item) ? { value: item[0], label: item[1] } : item,
  );
}
function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString()
    : "—";
}
function formatScore(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(2)
    : "—";
}
function formatElapsed(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
}
/** Today in the browser's own time zone; the scheduler and the report both use local time. */
function todayValue(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 10);
}
function queryString(params: Record<string, string | number>): string {
  const parts = Object.entries(params)
    .filter(([, value]) => value !== "" && value !== undefined)
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

interface QualityQuery<T> {
  data: T | null;
  error: string;
  busy: boolean;
  reload: () => void;
}
/**
 * GET wrapper for the quality API. The monotonic counter drops the answer of a request that a
 * newer filter has already replaced, and rows already on screen stay put while a reload runs.
 */
function useQualityData<T>(path: string): QualityQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [tick, setTick] = useState(0);
  const latest = useRef(0);
  useEffect(() => {
    const requestId = ++latest.current;
    setBusy(true);
    request<T>(path)
      .then((result) => {
        if (requestId !== latest.current) return;
        setData(result);
        setError("");
      })
      .catch((e: unknown) => {
        if (requestId !== latest.current) return;
        setData(null);
        setError(errorMessage(e));
      })
      .finally(() => {
        if (requestId === latest.current) setBusy(false);
      });
  }, [path, tick]);
  return { data, error, busy, reload: () => setTick((value) => value + 1) };
}

function LevelBadge({ level, label }: { level: string; label?: string }) {
  const text = label || level || "—";
  if (level === "high") return <span className="failure-badge">{text}</span>;
  if (level === "low") return <span className="neutral-badge">{text}</span>;
  return <span className="quality-warn-badge">{text}</span>;
}
function EnabledBadge({ state, label }: { state: number; label?: string }) {
  return state === 1 ? (
    <span className="healthy-badge">{label || "启用"}</span>
  ) : (
    <span className="neutral-badge">{label || "禁用"}</span>
  );
}
function RunningBadge({ state, label }: { state: number; label?: string }) {
  return state === 1 ? (
    <span className="healthy-badge">{label || "运行"}</span>
  ) : (
    <span className="quality-warn-badge">{label || "停止"}</span>
  );
}
function RunStatusBadge({ status, label }: { status: string; label?: string }) {
  const className =
    status === "success"
      ? "healthy-badge"
      : status === "failed"
        ? "failure-badge"
        : status === "retrying" || status === "running"
          ? "quality-warn-badge"
          : "neutral-badge";
  return <span className={className}>{label || status}</span>;
}
function ResultBadge({ state, label }: { state: number; label?: string }) {
  return state === 1 ? (
    <span className="healthy-badge">{label || "成功"}</span>
  ) : (
    <span className="failure-badge">{label || "失败"}</span>
  );
}
function ExecutionBadge({ status, label }: { status: number; label?: string }) {
  if (status === 1)
    return <span className="healthy-badge">{label || "成功"}</span>;
  if (status === 2)
    return <span className="failure-badge">{label || "失败"}</span>;
  return <span className="source-badge">{label || "运行中"}</span>;
}

function QualityPagination({
  total,
  page,
  size,
  onPage,
  onSize,
}: {
  total: number;
  page: number;
  size: number;
  onPage: (page: number) => void;
  onSize: (size: number) => void;
}) {
  const [jump, setJump] = useState("");
  const pageCount = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, page), pageCount);
  function go() {
    const target = Number(jump);
    if (!Number.isFinite(target) || target < 1) return;
    onPage(Math.min(Math.trunc(target), pageCount));
    setJump("");
  }
  return (
    <div className="table-footer quality-pagination">
      <span>共 {total.toLocaleString()} 条</span>
      <select
        aria-label="每页条数"
        value={size}
        onChange={(event) => onSize(Number(event.target.value))}
      >
        {PAGE_SIZES.map((value) => (
          <option key={value} value={value}>
            {value}条/页
          </option>
        ))}
      </select>
      <div className="pagination">
        <button
          className="icon-button"
          aria-label="上一页"
          disabled={current <= 1}
          onClick={() => onPage(current - 1)}
        >
          <ChevronLeft size={16} />
        </button>
        <span>
          {current} / {pageCount}
        </span>
        <button
          className="icon-button"
          aria-label="下一页"
          disabled={current >= pageCount}
          onClick={() => onPage(current + 1)}
        >
          <ChevronRight size={16} />
        </button>
      </div>
      <span className="quality-page-jump">
        前往
        <input
          type="number"
          min={1}
          max={pageCount}
          aria-label="跳转到指定页"
          placeholder={String(current)}
          value={jump}
          onChange={(event) => setJump(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") go();
          }}
          onBlur={go}
        />
        页
      </span>
    </div>
  );
}

function DimensionTree({
  title,
  items,
  active,
  allLabel,
  onSelect,
}: {
  title: string;
  items: { id: string; label: string; hint?: ReactNode }[];
  active: string;
  allLabel: string;
  onSelect: (id: string) => void;
}) {
  return (
    <aside className="quality-tree" aria-label={title}>
      <div className="quality-tree-head">
        <ListTree size={14} />
        {title}
      </div>
      <div className="quality-tree-list">
        <button
          className={`quality-tree-item ${active === "" ? "active" : ""}`}
          aria-pressed={active === ""}
          onClick={() => onSelect("")}
        >
          <span>{allLabel}</span>
        </button>
        {items.map((item) => (
          <button
            key={item.id}
            className={`quality-tree-item ${active === item.id ? "active" : ""}`}
            aria-pressed={active === item.id}
            onClick={() => onSelect(item.id)}
          >
            <span>{item.label}</span>
            {item.hint}
          </button>
        ))}
      </div>
    </aside>
  );
}

function QualityField({
  id,
  label,
  required = false,
  help,
  wide = false,
  children,
}: {
  id?: string;
  label: string;
  required?: boolean;
  help?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`quality-field ${wide ? "quality-field-wide" : ""}`}>
      {id ? (
        <label htmlFor={id}>
          {label}
          {required && <b aria-label="必填">*</b>}
        </label>
      ) : (
        <span className="quality-field-label">
          {label}
          {required && <b aria-label="必填">*</b>}
        </span>
      )}
      {children}
      {help && <small>{help}</small>}
    </div>
  );
}

/* ------------------------------------------------------------------ page */

export default function QualityPage({
  sources,
  sourcesError,
  sourceId,
  setSourceId,
  goTo,
}: DataPageProps) {
  const [tab, setTab] = useState<TabId>("rules");
  const catalog = useQualityData<MetricCatalog>("/api/quality/metrics");
  const health = useQualityData<QualityHealth>("/api/quality/health");
  const dimensions = catalog.data?.dimensions?.length
    ? catalog.data.dimensions
    : FALLBACK_DIMENSIONS;
  return (
    <div className="page-content">
      <PageTitle
        title="数据质量"
        description="核查规则、调度任务、质量报告与执行日志，规则在真实数据源上以只读方式执行"
        action={<HealthBadge query={health} />}
      />
      {sourcesError && (
        <ErrorBanner message={`数据源列表不可用：${sourcesError}`} />
      )}
      {catalog.error && (
        <ErrorBanner message={`核查规则类型加载失败：${catalog.error}`} />
      )}
      <div className="quality-pill">
        <ShieldCheck size={13} />
        数据质量管理
      </div>
      <nav className="quality-tabs" role="tablist" aria-label="数据质量功能">
        {TABS.map((item) => (
          <button
            key={item.id}
            id={`quality-tab-${item.id}`}
            role="tab"
            aria-selected={tab === item.id}
            aria-controls={`quality-panel-${item.id}`}
            className={`quality-tab ${tab === item.id ? "active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            <item.icon size={14} />
            {item.label}
          </button>
        ))}
      </nav>
      <div
        id={`quality-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`quality-tab-${tab}`}
      >
        {tab === "rules" && (
          <RulesTab
            catalog={catalog.data}
            dimensions={dimensions}
            sources={sources}
            sourceId={sourceId}
            setSourceId={setSourceId}
            goTo={goTo}
          />
        )}
        {tab === "schedules" && <SchedulesTab />}
        {tab === "report" && <ReportTab />}
        {tab === "statistics" && <StatisticsTab dimensions={dimensions} />}
        {tab === "executions" && <ExecutionsTab />}
      </div>
    </div>
  );
}

function HealthBadge({ query }: { query: QualityQuery<QualityHealth> }) {
  if (query.busy && !query.data && !query.error)
    return <span className="neutral-badge">正在检测质量元数据库…</span>;
  if (query.error)
    return (
      <span className="failure-badge" title={query.error}>
        质量元数据库不可用
      </span>
    );
  const health = query.data;
  if (!health)
    return <span className="neutral-badge">质量元数据库状态未知</span>;
  const store = health.ok
    ? `元数据库已连接 · ${health.database ?? ""}${health.schema ? `.${health.schema}` : ""}`
    : "元数据库连接失败";
  return (
    <span
      className={health.ok ? "healthy-badge" : "failure-badge"}
      title={health.detail ?? ""}
    >
      {store} · {health.scheduler?.running ? "调度运行中" : "调度已停止"}
    </span>
  );
}

/* ------------------------------------------------------------------ 质量规则管理 */

function RulesTab({
  catalog,
  dimensions,
  sources,
  sourceId,
  setSourceId,
  goTo,
}: {
  catalog: MetricCatalog | null;
  dimensions: CatalogDimension[];
  sources: DataSource[];
  sourceId: string;
  setSourceId: (id: string) => void;
  goTo: GoTo;
}) {
  const [dimension, setDimension] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [name, setName] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [selected, setSelected] = useState<number[]>([]);
  const [editing, setEditing] = useState<{ rule: QualityRule | null } | null>(
    null,
  );
  const [preview, setPreview] = useState<{
    rule: QualityRule;
    sql: RulePreview;
  } | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [batch, setBatch] = useState<{ template: QualityTemplate | null } | null>(null);
  const rules = useQualityData<Paged<QualityRule>>(
    `/api/quality/rules${queryString({ dimension, name, page, size })}`,
  );
  const items = rules.data?.items ?? [];
  const total = rules.data?.total ?? items.length;

  function reset() {
    setNameInput("");
    setName("");
    setDimension("");
    setPage(1);
    setSelected([]);
  }
  function search() {
    setName(nameInput.trim());
    setPage(1);
    setSelected([]);
  }
  async function runRule(rule: QualityRule) {
    setBusy(`run-${rule.id}`);
    setError("");
    setNotice("");
    try {
      const answer = await request<{
        execution: QualityExecution | null;
        result: QualityResult | null;
      }>(`/api/quality/rules/${rule.id}/run`, {});
      const detail = answer.result;
      setNotice(
        detail
          ? `规则「${rule.name}」核查${detail.state === 1 ? "通过" : "未通过"}：核查 ${formatCount(detail.checked_count)} 行，不合规 ${formatCount(detail.actual_value)} 行，得分 ${formatScore(detail.score)}。`
          : `规则「${rule.name}」已执行：${answer.execution?.message || "未返回核查结果"}。`,
      );
    } catch (e) {
      setError(`执行规则「${rule.name}」失败：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }
  async function runSelected() {
    setBusy("run-selected");
    setError("");
    setNotice("");
    let passed = 0;
    let failed = 0;
    try {
      for (const id of selected) {
        const answer = await request<{ result: QualityResult | null }>(
          `/api/quality/rules/${id}/run`,
          {},
        );
        if (answer.result?.state === 1) passed += 1;
        else failed += 1;
      }
      setNotice(
        `已执行 ${selected.length} 条规则：通过 ${passed} 条，未通过 ${failed} 条。`,
      );
    } catch (e) {
      setError(`批量执行中断：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }
  async function saveTemplate(rule: QualityRule) {
    setBusy(`template-${rule.id}`);
    setError("");
    setNotice("");
    try {
      const saved = await request<QualityTemplate>(`/api/quality/rules/${rule.id}/template`, {});
      setNotice(`已把规则「${rule.name}」保存为模板「${saved.name}」，可在“规则模板”里批量下发。`);
    } catch (e) {
      setError(`保存模板失败：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }
  async function showSql(rule: QualityRule) {
    setBusy(`sql-${rule.id}`);
    setError("");
    try {
      setPreview({
        rule,
        sql: await request<RulePreview>(
          `/api/quality/rules/${rule.id}/preview`,
          {},
        ),
      });
    } catch (e) {
      setError(`生成规则「${rule.name}」的 SQL 失败：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }
  async function removeRule(rule: QualityRule) {
    if (
      !window.confirm(
        `确认删除核查规则「${rule.name}」？\n仅删除质量元数据库中的规则定义，不会修改目标数据源。`,
      )
    )
      return;
    setBusy(`delete-${rule.id}`);
    setError("");
    setNotice("");
    try {
      await request(`/api/quality/rules/${rule.id}/delete`, {});
      setSelected((current) => current.filter((id) => id !== rule.id));
      setNotice(`已删除核查规则「${rule.name}」。`);
      rules.reload();
    } catch (e) {
      setError(`删除规则「${rule.name}」失败：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }

  if (editing)
    return (
      <RuleEditor
        key={editing.rule ? editing.rule.id : "create"}
        rule={editing.rule}
        catalog={catalog}
        dimensions={dimensions}
        sources={sources}
        sourceId={sourceId}
        setSourceId={setSourceId}
        onCancel={() => setEditing(null)}
        onSaved={(saved, mode) => {
          setEditing(null);
          setError("");
          setNotice(
            mode === "create"
              ? `已新增核查规则「${saved.name}」。`
              : `已更新核查规则「${saved.name}」。`,
          );
          rules.reload();
        }}
      />
    );
  return (
    <div className="quality-layout">
      <DimensionTree
        title="核查规则类型"
        allLabel="全部规则"
        active={dimension}
        items={dimensions.map((item) => ({
          id: item.id,
          label: item.label,
          hint: item.metrics.length ? (
            <small>{item.metrics.length}</small>
          ) : undefined,
        }))}
        onSelect={(id) => {
          setDimension(id);
          setPage(1);
          setSelected([]);
        }}
      />
      <section className="panel quality-main">
        <div className="quality-toolbar">
          <label htmlFor="quality-rule-search">规则名称</label>
          <input
            id="quality-rule-search"
            className="search-input"
            value={nameInput}
            maxLength={200}
            placeholder="请输入规则名称"
            onChange={(event) => setNameInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") search();
            }}
          />
          <button className="primary-button" onClick={search}>
            <Search size={14} />
            搜索
          </button>
          <button className="secondary-button" onClick={reset}>
            <RotateCcw size={14} />
            重置
          </button>
          <div className="quality-toolbar-end">
            {selected.length > 0 && (
              <button
                className="secondary-button"
                disabled={!!busy}
                onClick={() => void runSelected()}
              >
                <Play size={14} />
                {busy === "run-selected"
                  ? "执行中…"
                  : `批量执行（${selected.length}）`}
              </button>
            )}
            <button
              className="secondary-button"
              disabled={rules.busy}
              onClick={rules.reload}
            >
              <RefreshCw size={14} className={rules.busy ? "spin" : ""} />
              刷新
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={!!busy}
              onClick={() => setAiOpen(true)}
            >
              <Sparkles size={14} />
              AI 推荐规则
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={!!busy}
              onClick={() => setTemplatesOpen(true)}
            >
              <ScrollText size={14} />
              规则模板
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={!!busy}
              onClick={() => setBatch({ template: null })}
            >
              <ListTree size={14} />
              批量下发
            </button>
            <button
              className="primary-button"
              onClick={() => setEditing({ rule: null })}
            >
              <Plus size={15} />
              新增
            </button>
          </div>
        </div>
        {error && <ErrorBanner message={error} />}
        {notice && (
          <div className="success-banner" role="status">
            {notice}
          </div>
        )}
        {rules.error && (
          <ErrorBanner message={`核查规则加载失败：${rules.error}`} />
        )}
        {rules.busy && !rules.data ? (
          <Loading text="正在读取核查规则…" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th className="quality-check">
                    <input
                      type="checkbox"
                      aria-label="全选本页规则"
                      checked={
                        items.length > 0 && selected.length === items.length
                      }
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? items.map((item) => item.id)
                            : [],
                        )
                      }
                    />
                  </th>
                  <th>序号</th>
                  <th>规则名称</th>
                  <th>规则类型</th>
                  <th>数据源</th>
                  <th>数据表</th>
                  <th>核查字段</th>
                  <th>规则级别</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((rule, index) => (
                  <tr key={rule.id}>
                    <td className="quality-check">
                      <input
                        type="checkbox"
                        aria-label={`选择规则 ${rule.name}`}
                        checked={selected.includes(rule.id)}
                        onChange={(event) =>
                          setSelected((current) =>
                            event.target.checked
                              ? [...current, rule.id]
                              : current.filter((id) => id !== rule.id),
                          )
                        }
                      />
                    </td>
                    <td>{(page - 1) * size + index + 1}</td>
                    <td title={rule.comment}>{rule.name}</td>
                    <td>
                      <span className="dimension-tag">
                        {rule.dimension_label}
                      </span>{" "}
                      {rule.metric_label}
                    </td>
                    <td>{rule.datasource_name || rule.datasource_id}</td>
                    <td>
                      <span className="table-name">
                        <Table2 size={13} />
                        {rule.schema_name
                          ? `${rule.schema_name}.${rule.table_name}`
                          : rule.table_name}
                      </span>
                    </td>
                    <td>{rule.column_name || "—"}</td>
                    <td>
                      <LevelBadge level={rule.level} label={rule.level_label} />
                    </td>
                    <td>
                      <EnabledBadge
                        state={rule.state}
                        label={rule.state_label}
                      />
                    </td>
                    <td>{formatTime(rule.create_time)}</td>
                    <td>
                      <div className="quality-actions">
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => setEditing({ rule })}
                        >
                          <Pencil size={13} />
                          编辑
                        </button>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void runRule(rule)}
                        >
                          <Play size={13} />
                          {busy === `run-${rule.id}` ? "执行中…" : "执行"}
                        </button>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void showSql(rule)}
                        >
                          <Code2 size={13} />
                          查看SQL
                        </button>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void saveTemplate(rule)}
                        >
                          <ScrollText size={13} />
                          存为模板
                        </button>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void removeRule(rule)}
                        >
                          <Trash2 size={13} />
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!rules.busy && !items.length && !rules.error && (
          <Empty title="还没有核查规则">
            点击“新增”创建核查规则，或用“规则模板”一次下发到多张表；核查类型包括唯一性、完整性、准确性、数据标准、关联性、及时性、跨库比对与自定义 SQL。
          </Empty>
        )}
        {aiOpen && (
          <AiRulesDrawer
            sources={sources}
            sourceId={sourceId}
            onClose={() => setAiOpen(false)}
            onCreated={(count) => {
              setAiOpen(false);
              setError("");
              setNotice(`已根据 AI 建议创建 ${count} 条核查规则。`);
              rules.reload();
            }}
          />
        )}
        {templatesOpen && (
          <TemplatesDrawer
            catalog={catalog}
            dimensions={dimensions}
            sources={sources}
            onClose={() => setTemplatesOpen(false)}
            onApply={(template) => {
              setTemplatesOpen(false);
              setBatch({ template });
            }}
          />
        )}
        {batch && (
          <BatchDrawer
            template={batch.template}
            sources={sources}
            sourceId={sourceId}
            onClose={() => setBatch(null)}
            onDone={(outcome) => {
              setBatch(null);
              setError("");
              setNotice(
                outcome.errors.length
                  ? `批量下发完成：新增 ${outcome.created.length} 条规则，${outcome.errors.length} 个目标失败（${outcome.errors.map((item) => `${item.table_name}：${item.message}`).join("；")}）。`
                  : `批量下发完成：新增 ${outcome.created.length} 条规则。`,
              );
              rules.reload();
            }}
          />
        )}
        <QualityPagination
          total={total}
          page={page}
          size={size}
          onPage={setPage}
          onSize={(next) => {
            setSize(next);
            setPage(1);
          }}
        />
      </section>
      {preview && (
        <Drawer
          id="quality-rule-sql"
          title={`核查 SQL · ${preview.rule.name}`}
          icon={<Code2 size={17} />}
          onClose={() => setPreview(null)}
          wide
        >
          <div className="quality-drawer-body">
            <p className="muted">
              以下语句由核查规则生成，均为只读聚合查询，不会在目标数据源上创建视图或临时表。
            </p>
            {(
              [
                ["核查数量（checked_count）", preview.sql.total_sql],
                ["不合规数量（actual_value）", preview.sql.actual_sql],
                ["错误数据取样", preview.sql.invalidate_sql],
              ] as const
            ).map(([label, sql]) => (
              <div key={label}>
                <h3 className="quality-section-title">{label}</h3>
                <pre className="sql-code">{sql}</pre>
              </div>
            ))}
            <button
              className="secondary-button"
              onClick={() =>
                goTo(
                  "sql",
                  preview.sql.invalidate_sql,
                  preview.rule.datasource_id,
                )
              }
            >
              <Eye size={14} />在 SQL 工作台打开
            </button>
          </div>
        </Drawer>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 核查规则编辑 */

function metricOf(
  dimensions: CatalogDimension[],
  metric: string,
): { dimension: CatalogDimension; metric: CatalogMetric } | undefined {
  for (const dimension of dimensions)
    for (const item of dimension.metrics)
      if (item.id === metric) return { dimension, metric: item };
  return undefined;
}
function configValues(
  metric: CatalogMetric | undefined,
  stored: Record<string, unknown> | undefined,
): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of metric?.fields ?? []) {
    const current = stored?.[field.name];
    if (current !== undefined && current !== null)
      values[field.name] = String(current);
    else
      values[field.name] =
        field.default === undefined || field.default === null
          ? ""
          : String(field.default);
  }
  return values;
}

function RuleEditor({
  rule,
  catalog,
  dimensions,
  sources,
  sourceId,
  setSourceId,
  onCancel,
  onSaved,
}: {
  rule: QualityRule | null;
  catalog: MetricCatalog | null;
  dimensions: CatalogDimension[];
  sources: DataSource[];
  sourceId: string;
  setSourceId: (id: string) => void;
  onCancel: () => void;
  onSaved: (saved: QualityRule, mode: "create" | "edit") => void;
}) {
  const [name, setName] = useState(rule?.name ?? "");
  const [metric, setMetric] = useState(rule?.metric ?? "");
  const [level, setLevel] = useState(rule?.level ?? "medium");
  const [datasource, setDatasource] = useState(
    rule?.datasource_id ?? sourceId ?? "",
  );
  const [schema, setSchema] = useState(rule?.schema_name ?? "");
  const [table, setTable] = useState(rule?.table_name ?? "");
  const [column, setColumn] = useState(rule?.column_name ?? "");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [expectedType, setExpectedType] = useState(
    rule?.expected_type ?? "fix_value",
  );
  const [formula, setFormula] = useState(rule?.result_formula ?? "actual");
  const [operator, setOperator] = useState(rule?.operator ?? "lte");
  const [threshold, setThreshold] = useState(String(rule?.threshold ?? 0));
  const [state, setState] = useState<number>(rule?.state ?? 1);
  const [comment, setComment] = useState(rule?.comment ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const source = sources.find((item) => item.id === datasource);
  const definition = metricOf(dimensions, metric);
  const needsColumn = definition?.metric.needs_column !== false;
  const levels = toOptions(catalog?.levels, FALLBACK_LEVELS);
  const operators = toOptions(catalog?.operators, FALLBACK_OPERATORS);
  const expectedTypes = toOptions(
    catalog?.expected_types,
    FALLBACK_EXPECTED_TYPES,
  );
  const formulas = toOptions(catalog?.result_formulas, FALLBACK_FORMULAS);
  /* Dependent metadata selects clear while the parent changes, which is what useApi does. */
  const schemas = useSchemas(source);
  const tables = useTables(source, schema);
  const detail = useApi<TableDetail>(
    source && table
      ? `${datasourcePath(source.id, "/table")}?schema=${encodeURIComponent(schema)}&name=${encodeURIComponent(table)}`
      : null,
  );
  const columns: ColumnInfo[] = detail.data?.columns ?? [];

  /* The stored config can only be mapped onto form fields once the catalog has arrived. */
  useEffect(() => {
    const current = metricOf(dimensions, metric)?.metric;
    if (!current) return;
    setConfig(
      configValues(current, rule && rule.metric === metric ? rule.config : {}),
    );
  }, [dimensions, metric]);
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema))
      setSchema(preferredSchema(source, items));
  }, [schemas.data]);
  useEffect(() => {
    if (!tables.data) return;
    if (!tables.data.items.some((item) => item.name === table)) setTable("");
  }, [tables.data]);

  function buildConfig(): Record<string, unknown> {
    const fields = definition?.metric.fields ?? [];
    if (!fields.length)
      return rule && rule.metric === metric ? rule.config : {};
    const built: Record<string, unknown> = {};
    for (const field of fields) {
      const text = (config[field.name] ?? "").trim();
      if (!text) continue;
      built[field.name] = field.type === "number" ? Number(text) : text;
    }
    return built;
  }
  function validate(): string {
    if (!name.trim()) return "请填写规则名称。";
    if (!metric) return "请选择核查类型。";
    if (!datasource) return "请选择数据源。";
    if (!table) return "请选择数据表。";
    if (needsColumn && !column) return "请选择核查字段。";
    const missing = (definition?.metric.fields ?? []).filter(
      (field) => field.required && !(config[field.name] ?? "").trim(),
    );
    if (missing.length)
      return `请填写核查配置：${missing.map((field) => field.label).join("、")}。`;
    const numeric = (definition?.metric.fields ?? []).filter(
      (field) =>
        field.type === "number" &&
        (config[field.name] ?? "").trim() !== "" &&
        !Number.isFinite(Number(config[field.name])),
    );
    if (numeric.length)
      return `${numeric.map((field) => field.label).join("、")} 必须是数字。`;
    if (!Number.isFinite(Number(threshold))) return "阈值必须是数字。";
    return "";
  }
  async function save() {
    const message = validate();
    if (message) {
      setError(message);
      return;
    }
    setSaving(true);
    setError("");
    try {
      const body = {
        name: name.trim(),
        metric,
        dimension: definition?.dimension.id ?? rule?.dimension ?? "",
        level,
        datasource_id: datasource,
        datasource_name: source?.name ?? rule?.datasource_name ?? "",
        schema_name: schema || null,
        table_name: table,
        column_name: needsColumn ? column : null,
        config: buildConfig(),
        expected_type: expectedType,
        result_formula: formula,
        operator,
        threshold: Number(threshold),
        state,
        comment: comment.trim(),
      };
      const saved = await request<QualityRule>(
        rule ? `/api/quality/rules/${rule.id}/update` : "/api/quality/rules",
        body,
      );
      onSaved(saved, rule ? "edit" : "create");
    } catch (e) {
      setError(errorMessage(e));
      setSaving(false);
    }
  }
  return (
    <section className="panel quality-form">
      <div className="panel-heading">
        <h2>
          <Pencil size={15} />
          核查规则编辑
        </h2>
        <div className="button-row">
          <button
            className="primary-button"
            disabled={saving}
            onClick={() => void save()}
          >
            <Plus size={14} />
            {saving ? "保存中…" : "保存"}
          </button>
          <button
            className="secondary-button"
            disabled={saving}
            onClick={onCancel}
          >
            <ArrowLeft size={14} />
            返回
          </button>
        </div>
      </div>
      <form
        className="quality-form-body"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <QualityField id="quality-rule-name" label="规则名称" required>
          <input
            id="quality-rule-name"
            value={name}
            maxLength={200}
            placeholder="例如：订单表订单号唯一性核查"
            onChange={(event) => setName(event.target.value)}
          />
        </QualityField>
        <QualityField
          id="quality-rule-metric"
          label="核查类型"
          required
          help={
            dimensions.some((item) => item.metrics.length)
              ? undefined
              : "核查规则类型尚未加载，请确认质量服务可用后重试。"
          }
        >
          <select
            id="quality-rule-metric"
            value={metric}
            onChange={(event) => setMetric(event.target.value)}
          >
            <option value="">请选择核查类型</option>
            {dimensions.map((item) => (
              <optgroup key={item.id} label={item.label}>
                {item.metrics.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </QualityField>
        <QualityField id="quality-rule-level" label="规则级别" required>
          <select
            id="quality-rule-level"
            value={level}
            onChange={(event) => setLevel(event.target.value)}
          >
            {levels.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField id="quality-rule-source" label="数据源" required>
          <select
            id="quality-rule-source"
            value={datasource}
            onChange={(event) => {
              setDatasource(event.target.value);
              setSourceId(event.target.value);
              setSchema("");
              setTable("");
              setColumn("");
            }}
          >
            <option value="">请选择数据源</option>
            {sources.map((item) => (
              <option key={item.id} value={item.id}>
                {sourceLabel(item, sources)}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField
          id="quality-rule-schema"
          label="Schema"
          help={schemas.error || undefined}
        >
          <select
            id="quality-rule-schema"
            value={schema}
            disabled={!schemas.data}
            onChange={(event) => {
              setSchema(event.target.value);
              setTable("");
              setColumn("");
            }}
          >
            {!(schemas.data?.items.length ?? 0) && (
              <option value="">
                {schemas.loading
                  ? "加载中…"
                  : schemas.error
                    ? "加载失败"
                    : source
                      ? "无 Schema"
                      : "请先选择数据源"}
              </option>
            )}
            {schemas.data?.items.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField
          id="quality-rule-table"
          label="数据表"
          required
          help={tables.error || undefined}
        >
          <select
            id="quality-rule-table"
            value={table}
            disabled={!tables.data}
            onChange={(event) => {
              setTable(event.target.value);
              setColumn("");
            }}
          >
            <option value="">
              {tables.loading
                ? "加载中…"
                : tables.data?.items.length
                  ? "请选择数据表"
                  : tables.error
                    ? "加载失败"
                    : "暂无数据表"}
            </option>
            {tables.data?.items.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField
          id="quality-rule-column"
          label="核查字段"
          required={needsColumn}
          help={
            needsColumn
              ? detail.error || undefined
              : "该核查类型作用于整表，可不选择字段。"
          }
        >
          <select
            id="quality-rule-column"
            value={column}
            disabled={!detail.data}
            onChange={(event) => setColumn(event.target.value)}
          >
            <option value="">
              {detail.loading
                ? "加载中…"
                : columns.length
                  ? "请选择核查字段"
                  : detail.error
                    ? "加载失败"
                    : "请先选择数据表"}
            </option>
            {columns.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
                {item.type ? `（${item.type}）` : ""}
              </option>
            ))}
          </select>
        </QualityField>
        <fieldset className="quality-fieldset">
          <legend>核查配置</legend>
          {(definition?.metric.fields ?? []).map((field) => (
            <ConfigFieldControl
              key={field.name}
              field={field}
              sources={sources}
              value={config[field.name] ?? ""}
              onChange={(value) =>
                setConfig((current) => ({ ...current, [field.name]: value }))
              }
            />
          ))}
          {!definition && (
            <p className="muted">选择核查类型后显示该类型的核查参数。</p>
          )}
          <QualityField id="quality-rule-expected" label="期望值类型" required>
            <select
              id="quality-rule-expected"
              value={expectedType}
              onChange={(event) => setExpectedType(event.target.value)}
            >
              {expectedTypes.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </QualityField>
          <QualityField id="quality-rule-formula" label="计算方式" required>
            <select
              id="quality-rule-formula"
              value={formula}
              onChange={(event) => setFormula(event.target.value)}
            >
              {formulas.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </QualityField>
          <QualityField id="quality-rule-operator" label="比较方式" required>
            <select
              id="quality-rule-operator"
              value={operator}
              onChange={(event) => setOperator(event.target.value)}
            >
              {operators.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </QualityField>
          <QualityField
            id="quality-rule-threshold"
            label="阈值"
            required
            help="计算结果与该阈值比较，满足比较方式即判定核查通过。"
          >
            <input
              id="quality-rule-threshold"
              type="number"
              value={threshold}
              onChange={(event) => setThreshold(event.target.value)}
            />
          </QualityField>
        </fieldset>
        <QualityField label="状态" required>
          <div className="quality-radio-row">
            <label>
              <input
                type="radio"
                name="quality-rule-state"
                checked={state === 0}
                onChange={() => setState(0)}
              />
              禁用
            </label>
            <label>
              <input
                type="radio"
                name="quality-rule-state"
                checked={state === 1}
                onChange={() => setState(1)}
              />
              启用
            </label>
          </div>
        </QualityField>
        <QualityField id="quality-rule-comment" label="备注" wide>
          <textarea
            id="quality-rule-comment"
            rows={3}
            value={comment}
            placeholder="可选，说明核查目的或负责人"
            onChange={(event) => setComment(event.target.value)}
          />
        </QualityField>
        {error && <ErrorBanner message={error} />}
      </form>
    </section>
  );
}

function ConfigFieldControl({
  field,
  value,
  onChange,
  sources = [],
}: {
  field: ConfigField;
  value: string;
  onChange: (value: string) => void;
  sources?: DataSource[];
}) {
  const id = `quality-config-${field.name}`;
  let control: ReactNode;
  if (field.type === "datasource") {
    control = (
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">请选择数据源</option>
        {sources.map((item) => (
          <option key={item.id} value={item.id}>
            {item.name}（{item.id}）
          </option>
        ))}
      </select>
    );
  } else if (field.type === "sql") {
    control = (
      <textarea
        id={id}
        rows={6}
        className="code-editor"
        value={value}
        spellCheck={false}
        placeholder={field.placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else if (field.type === "select") {
    control = (
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {!field.required && <option value="">默认</option>}
        {toOptions(field.options, []).map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    );
  } else if (field.type === "textarea") {
    control = (
      <textarea
        id={id}
        rows={3}
        value={value}
        spellCheck={false}
        placeholder={field.placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else {
    control = (
      <input
        id={id}
        type={field.type === "number" ? "number" : "text"}
        value={value}
        spellCheck={false}
        placeholder={field.placeholder}
        autoComplete="off"
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <QualityField
      id={id}
      label={field.label}
      required={!!field.required}
      help={field.help}
      wide={field.type === "textarea" || field.type === "sql"}
    >
      {control}
    </QualityField>
  );
}

/* ------------------------------------------------------------------ 规则模板与批量下发 */

/** Templates carry a rule definition without a target; 下发 applies one to many tables. */
function TemplatesDrawer({
  catalog,
  dimensions,
  sources,
  onClose,
  onApply,
}: {
  catalog: MetricCatalog | null;
  dimensions: CatalogDimension[];
  sources: DataSource[];
  onClose: () => void;
  onApply: (template: QualityTemplate) => void;
}) {
  const templates = useQualityData<Paged<QualityTemplate>>("/api/quality/templates?size=100");
  const [editing, setEditing] = useState<{ template: QualityTemplate | null } | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const items = templates.data?.items ?? [];
  async function remove(template: QualityTemplate) {
    if (!window.confirm(`确认删除规则模板「${template.name}」？已下发的规则不受影响。`)) return;
    setBusy(`delete-${template.id}`);
    setError("");
    try {
      await request(`/api/quality/templates/${template.id}/delete`, {});
      setNotice(`已删除模板「${template.name}」。`);
      templates.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <Drawer id="quality-templates" title="规则模板" icon={<ScrollText size={17} />} onClose={onClose} wide>
      <div className="quality-drawer-body">
        <p className="muted">
          模板是不带目标表的规则定义。内置模板可直接下发，也可以修改或删除；“存为模板”会把已有规则的定义保存到这里。
        </p>
        <div className="quality-toolbar">
          <button type="button" className="primary-button" disabled={!!busy} onClick={() => setEditing({ template: null })}>
            <Plus size={14} />
            新建模板
          </button>
          <button type="button" className="secondary-button" disabled={templates.busy} onClick={templates.reload}>
            <RefreshCw size={14} className={templates.busy ? "spin" : ""} />
            刷新
          </button>
        </div>
        {error && <ErrorBanner message={error} />}
        {notice && (
          <div className="info-banner" role="status">
            {notice}
          </div>
        )}
        {templates.error && <ErrorBanner message={`模板加载失败：${templates.error}`} />}
        {templates.busy && !templates.data ? (
          <Loading text="正在读取模板…" />
        ) : items.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>模板</th>
                  <th>核查类型</th>
                  <th>级别</th>
                  <th>判定</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((template) => (
                  <tr key={template.id}>
                    <td>
                      <div className="quality-task-cell">
                        <span>
                          {template.name}
                          {template.builtin && <span className="neutral-badge">内置</span>}
                        </span>
                        <small className="muted">{template.description}</small>
                      </div>
                    </td>
                    <td>
                      <span className="dimension-tag">{template.dimension_label}</span> {template.metric_label}
                    </td>
                    <td>
                      <LevelBadge level={template.level} label={template.level_label} />
                    </td>
                    <td>
                      <code>
                        {template.result_formula === "percentage" ? "百分比" : "数量"} {template.operator} {template.threshold}
                      </code>
                    </td>
                    <td>
                      <div className="quality-actions">
                        <button type="button" className="text-button" disabled={!!busy} onClick={() => onApply(template)}>
                          <ListTree size={13} />
                          下发
                        </button>
                        <button type="button" className="text-button" disabled={!!busy} onClick={() => setEditing({ template })}>
                          <Pencil size={13} />
                          编辑
                        </button>
                        <button type="button" className="text-button" disabled={!!busy} onClick={() => void remove(template)}>
                          <Trash2 size={13} />
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="还没有规则模板">点击“新建模板”，或在规则列表里把已有规则“存为模板”。</Empty>
        )}
        {editing && (
          <TemplateEditor
            key={editing.template ? editing.template.id : "create"}
            template={editing.template}
            catalog={catalog}
            dimensions={dimensions}
            sources={sources}
            onCancel={() => setEditing(null)}
            onSaved={(saved, mode) => {
              setEditing(null);
              setNotice(mode === "create" ? `已新建模板「${saved.name}」。` : `已更新模板「${saved.name}」。`);
              templates.reload();
            }}
          />
        )}
      </div>
    </Drawer>
  );
}

function TemplateEditor({
  template,
  catalog,
  dimensions,
  sources,
  onCancel,
  onSaved,
}: {
  template: QualityTemplate | null;
  catalog: MetricCatalog | null;
  dimensions: CatalogDimension[];
  sources: DataSource[];
  onCancel: () => void;
  onSaved: (saved: QualityTemplate, mode: "create" | "edit") => void;
}) {
  const [name, setName] = useState(template?.name ?? "");
  const [description, setDescription] = useState(template?.description ?? "");
  const [metric, setMetric] = useState(template?.metric ?? "");
  const [level, setLevel] = useState(template?.level ?? "medium");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [expectedType, setExpectedType] = useState(template?.expected_type ?? "fix_value");
  const [formula, setFormula] = useState(template?.result_formula ?? "actual");
  const [operator, setOperator] = useState(template?.operator ?? "lte");
  const [threshold, setThreshold] = useState(String(template?.threshold ?? 0));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const definition = metricOf(dimensions, metric);
  const levels = toOptions(catalog?.levels, FALLBACK_LEVELS);
  const operators = toOptions(catalog?.operators, FALLBACK_OPERATORS);
  const expectedTypes = toOptions(catalog?.expected_types, FALLBACK_EXPECTED_TYPES);
  const formulas = toOptions(catalog?.result_formulas, FALLBACK_FORMULAS);
  useEffect(() => {
    const current = metricOf(dimensions, metric)?.metric;
    if (!current) return;
    setConfig(configValues(current, template && template.metric === metric ? template.config : {}));
  }, [dimensions, metric]);
  async function save() {
    if (!name.trim()) return setError("请填写模板名称。");
    if (!metric) return setError("请选择核查类型。");
    if (!Number.isFinite(Number(threshold))) return setError("阈值必须是数字。");
    setSaving(true);
    setError("");
    const built: Record<string, unknown> = {};
    for (const field of definition?.metric.fields ?? []) {
      const value = (config[field.name] ?? "").trim();
      if (value) built[field.name] = field.type === "number" ? Number(value) : value;
    }
    try {
      const saved = await request<QualityTemplate>(
        template ? `/api/quality/templates/${template.id}/update` : "/api/quality/templates",
        {
          name: name.trim(),
          description: description.trim(),
          metric,
          level,
          config: built,
          expected_type: expectedType,
          result_formula: formula,
          operator,
          threshold: Number(threshold),
        },
      );
      onSaved(saved, template ? "edit" : "create");
    } catch (e) {
      setError(errorMessage(e));
      setSaving(false);
    }
  }
  return (
    <form
      className="quality-inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>{template ? `编辑模板 · ${template.name}` : "新建模板"}</h3>
      <QualityField id="quality-template-name" label="模板名称" required>
        <input id="quality-template-name" value={name} maxLength={200} onChange={(event) => setName(event.target.value)} />
      </QualityField>
      <QualityField id="quality-template-description" label="说明">
        <input id="quality-template-description" value={description} maxLength={2000} onChange={(event) => setDescription(event.target.value)} />
      </QualityField>
      <QualityField id="quality-template-metric" label="核查类型" required>
        <select id="quality-template-metric" value={metric} onChange={(event) => setMetric(event.target.value)}>
          <option value="">请选择核查类型</option>
          {dimensions.map((dimension) => (
            <optgroup key={dimension.id} label={dimension.label}>
              {dimension.metrics.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </QualityField>
      <QualityField id="quality-template-level" label="规则级别">
        <select id="quality-template-level" value={level} onChange={(event) => setLevel(event.target.value)}>
          {levels.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </QualityField>
      <fieldset className="quality-fieldset">
        <legend>核查配置（下发时可补充或覆盖）</legend>
        {(definition?.metric.fields ?? []).map((field) => (
          <ConfigFieldControl
            key={field.name}
            field={{ ...field, required: false }}
            value={config[field.name] ?? ""}
            sources={sources}
            onChange={(value) => setConfig((current) => ({ ...current, [field.name]: value }))}
          />
        ))}
        {!definition && <p className="muted">选择核查类型后显示该类型的核查参数。</p>}
      </fieldset>
      <div className="quality-field-row">
        <QualityField id="quality-template-expected" label="期望值类型">
          <select id="quality-template-expected" value={expectedType} onChange={(event) => setExpectedType(event.target.value)}>
            {expectedTypes.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField id="quality-template-formula" label="计算方式">
          <select id="quality-template-formula" value={formula} onChange={(event) => setFormula(event.target.value)}>
            {formulas.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField id="quality-template-operator" label="比较方式">
          <select id="quality-template-operator" value={operator} onChange={(event) => setOperator(event.target.value)}>
            {operators.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField id="quality-template-threshold" label="阈值">
          <input id="quality-template-threshold" type="number" value={threshold} onChange={(event) => setThreshold(event.target.value)} />
        </QualityField>
      </div>
      {error && <ErrorBanner message={error} />}
      <div className="button-row">
        <button type="submit" className="primary-button" disabled={saving}>
          {saving ? "保存中…" : "保存模板"}
        </button>
        <button type="button" className="text-button" disabled={saving} onClick={onCancel}>
          取消
        </button>
      </div>
    </form>
  );
}

/** 批量下发: one template, one data source, many tables, optional column and overrides. */
function BatchDrawer({
  template,
  sources,
  sourceId,
  onClose,
  onDone,
}: {
  template: QualityTemplate | null;
  sources: DataSource[];
  sourceId: string;
  onClose: () => void;
  onDone: (outcome: BatchOutcome) => void;
}) {
  const templates = useQualityData<Paged<QualityTemplate>>("/api/quality/templates?size=100");
  const [templateId, setTemplateId] = useState<number | "">(template?.id ?? "");
  const [datasource, setDatasource] = useState(sourceId);
  const [schema, setSchema] = useState("");
  const [tables, setTables] = useState<string[]>([]);
  const [column, setColumn] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [prefix, setPrefix] = useState("");
  const [state, setState] = useState<number>(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [outcome, setOutcome] = useState<BatchOutcome | null>(null);
  const catalog = useQualityData<MetricCatalog>("/api/quality/metrics");
  const dimensions = catalog.data?.dimensions ?? [];
  const chosen = (templates.data?.items ?? []).find((item) => item.id === templateId) ?? template;
  const definition = chosen ? metricOf(dimensions, chosen.metric) : undefined;
  const source = sources.find((item) => item.id === datasource);
  const schemas = useSchemas(source);
  const tableList = useTables(source, schema);
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema)) setSchema(preferredSchema(source, items));
  }, [schemas.data]);
  useEffect(() => {
    setTables([]);
  }, [datasource, schema]);
  useEffect(() => {
    if (!chosen) return;
    const values: Record<string, string> = {};
    for (const [key, value] of Object.entries(chosen.config ?? {})) {
      if (value !== null && value !== undefined && value !== "") values[key] = String(value);
    }
    setOverrides(values);
  }, [chosen?.id]);
  const available = tableList.data?.items ?? [];
  function toggle(name: string) {
    setTables((current) => (current.includes(name) ? current.filter((item) => item !== name) : [...current, name]));
  }
  async function submit() {
    if (!chosen) return setError("请选择规则模板。");
    if (!datasource) return setError("请选择数据源。");
    if (!tables.length) return setError("请至少勾选一张数据表。");
    if (chosen.needs_column && !column.trim()) return setError("该核查类型需要填写核查字段名。");
    setBusy(true);
    setError("");
    const config: Record<string, unknown> = {};
    for (const field of definition?.metric.fields ?? []) {
      const value = (overrides[field.name] ?? "").trim();
      if (value) config[field.name] = field.type === "number" ? Number(value) : value;
    }
    try {
      const result = await request<BatchOutcome>("/api/quality/rules/batch", {
        template_id: chosen.id,
        targets: tables.map((table) => ({
          datasource_id: datasource,
          schema_name: schema || null,
          table_name: table,
          column_name: column.trim() || null,
        })),
        config,
        name_prefix: prefix.trim() || null,
        state,
      });
      setOutcome(result);
      if (!result.errors.length) onDone(result);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Drawer id="quality-batch" title="批量下发规则" icon={<ListTree size={17} />} onClose={onClose} wide closeOnBackdrop={false}>
      <div className="quality-drawer-body">
        <p className="muted">
          选择一个模板和一批数据表，每张表生成一条规则；字段级核查对所有表使用同一个字段名。每个目标单独校验，失败的目标不影响其他表。
        </p>
        <QualityField id="quality-batch-template" label="规则模板" required help={chosen?.description || undefined}>
          <select
            id="quality-batch-template"
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value ? Number(event.target.value) : "")}
          >
            <option value="">请选择模板</option>
            {(templates.data?.items ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}（{item.metric_label}）
              </option>
            ))}
          </select>
        </QualityField>
        <div className="quality-field-row">
          <QualityField id="quality-batch-source" label="数据源" required>
            <SourceSelect id="quality-batch-source" sources={sources} value={datasource} disabled={busy} onChange={setDatasource} />
          </QualityField>
          <QualityField id="quality-batch-schema" label="Schema">
            <select id="quality-batch-schema" value={schema} disabled={!schemas.data} onChange={(event) => setSchema(event.target.value)}>
              {(schemas.data?.items ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </QualityField>
        </div>
        <QualityField
          label="数据表"
          required
          help={available.length ? `已选 ${tables.length} / ${available.length} 张表。` : tableList.error || "选择数据源与 Schema 后列出数据表。"}
        >
          <div className="quality-check-list">
            <label className="quality-check-all">
              <input
                type="checkbox"
                checked={available.length > 0 && tables.length === available.length}
                disabled={!available.length}
                onChange={(event) => setTables(event.target.checked ? available.map((item) => item.name) : [])}
              />
              全选
            </label>
            {available.map((item) => (
              <label key={item.name}>
                <input type="checkbox" checked={tables.includes(item.name)} onChange={() => toggle(item.name)} />
                {item.name}
              </label>
            ))}
            {tableList.loading && <span className="muted">加载中…</span>}
          </div>
        </QualityField>
        <div className="quality-field-row">
          <QualityField
            id="quality-batch-column"
            label="核查字段"
            required={!!chosen?.needs_column}
            help={chosen?.needs_column ? "所有选中的表都按这个字段名核查，例如 id、update_time。" : "该核查类型作用于整表，可留空；自定义 SQL 用到 ${column} 时需填写。"}
          >
            <input id="quality-batch-column" value={column} maxLength={200} placeholder="例如 id" onChange={(event) => setColumn(event.target.value)} />
          </QualityField>
          <QualityField id="quality-batch-prefix" label="规则名前缀" help="规则名为“前缀 · 表名.字段”，留空使用模板名。">
            <input id="quality-batch-prefix" value={prefix} maxLength={200} onChange={(event) => setPrefix(event.target.value)} />
          </QualityField>
        </div>
        {definition && (
          <fieldset className="quality-fieldset">
            <legend>核查配置（对本次下发的所有规则生效）</legend>
            {definition.metric.fields.map((field) => (
              <ConfigFieldControl
                key={field.name}
                field={field}
                value={overrides[field.name] ?? ""}
                sources={sources}
                onChange={(value) => setOverrides((current) => ({ ...current, [field.name]: value }))}
              />
            ))}
          </fieldset>
        )}
        <QualityField label="状态" required>
          <div className="quality-radio-row">
            <label>
              <input type="radio" name="quality-batch-state" checked={state === 1} onChange={() => setState(1)} />
              启用
            </label>
            <label>
              <input type="radio" name="quality-batch-state" checked={state === 0} onChange={() => setState(0)} />
              停用
            </label>
          </div>
        </QualityField>
        {error && <ErrorBanner message={error} />}
        {outcome && outcome.errors.length > 0 && (
          <div className="quality-batch-outcome">
            <div className="info-banner" role="status">
              已新增 {outcome.created.length} 条规则，{outcome.errors.length} 个目标失败：
            </div>
            <ul>
              {outcome.errors.map((item) => (
                <li key={`${item.table_name}-${item.column_name ?? ""}`}>
                  <code>{item.schema_name ? `${item.schema_name}.${item.table_name}` : item.table_name}</code> {item.message}
                </li>
              ))}
            </ul>
            <button type="button" className="secondary-button" onClick={() => onDone(outcome)}>
              关闭并查看规则
            </button>
          </div>
        )}
        <div className="button-row">
          <button type="button" className="primary-button" disabled={busy} onClick={() => void submit()}>
            {busy ? "下发中…" : `下发到 ${tables.length} 张表`}
          </button>
          <button type="button" className="text-button" disabled={busy} onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Drawer>
  );
}
/* ------------------------------------------------------------------ 质量调度管理 */

function SchedulesTab() {
  const [nameInput, setNameInput] = useState("");
  const [name, setName] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [form, setForm] = useState<{ schedule: QualitySchedule | null } | null>(
    null,
  );
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [runs, setRuns] = useState<QualitySchedule | "all" | null>(null);
  const schedules = useQualityData<Paged<QualitySchedule>>(
    `/api/quality/schedules${queryString({ name, page, size })}`,
  );
  const items = schedules.data?.items ?? [];
  const total = schedules.data?.total ?? items.length;

  async function act(
    schedule: QualitySchedule,
    action: "toggle" | "run" | "delete",
  ) {
    if (
      action === "delete" &&
      !window.confirm(
        `确认删除调度任务「${schedule.name}」？\n仅删除质量元数据库中的调度定义，不会修改目标数据源。`,
      )
    )
      return;
    setBusy(`${action}-${schedule.id}`);
    setError("");
    setNotice("");
    try {
      await request(`/api/quality/schedules/${schedule.id}/${action}`, {});
      setNotice(
        action === "delete"
          ? `已删除调度任务「${schedule.name}」。`
          : action === "run"
            ? `已手动触发调度任务「${schedule.name}」。`
            : `已${schedule.state === 1 ? "停止" : "启动"}调度任务「${schedule.name}」。`,
      );
      schedules.reload();
    } catch (e) {
      setError(`操作调度任务「${schedule.name}」失败：${errorMessage(e)}`);
    } finally {
      setBusy("");
    }
  }
  return (
    <section className="panel quality-main">
      <div className="quality-toolbar">
        <label htmlFor="quality-schedule-search">任务名称</label>
        <input
          id="quality-schedule-search"
          className="search-input"
          value={nameInput}
          maxLength={200}
          placeholder="请输入任务名称"
          onChange={(event) => setNameInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              setName(nameInput.trim());
              setPage(1);
            }
          }}
        />
        <button
          className="primary-button"
          onClick={() => {
            setName(nameInput.trim());
            setPage(1);
          }}
        >
          <Search size={14} />
          搜索
        </button>
        <button
          className="secondary-button"
          onClick={() => {
            setNameInput("");
            setName("");
            setPage(1);
          }}
        >
          <RotateCcw size={14} />
          重置
        </button>
        <div className="quality-toolbar-end">
          <button
            className="secondary-button"
            disabled={schedules.busy}
            onClick={schedules.reload}
          >
            <RefreshCw size={14} className={schedules.busy ? "spin" : ""} />
            刷新
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => setRuns("all")}
          >
            <History size={14} />
            执行记录
          </button>
          <button
            className="primary-button"
            onClick={() => setForm({ schedule: null })}
          >
            <Plus size={15} />
            新增
          </button>
        </div>
      </div>
      {error && <ErrorBanner message={error} />}
      {notice && (
        <div className="success-banner" role="status">
          {notice}
        </div>
      )}
      {schedules.error && (
        <ErrorBanner message={`调度任务加载失败：${schedules.error}`} />
      )}
      {schedules.busy && !schedules.data ? (
        <Loading text="正在读取调度任务…" />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>序号</th>
                <th>任务名称</th>
                <th>任务</th>
                <th>参数</th>
                <th>cron表达式</th>
                <th>重试与补跑</th>
                <th>状态</th>
                <th>上次结果</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((schedule, index) => (
                <tr key={schedule.id}>
                  <td>{(page - 1) * size + index + 1}</td>
                  <td
                    title={`上次触发：${formatTime(schedule.last_fire_time)}\n下次触发：${formatTime(schedule.next_fire_time)}`}
                  >
                    {schedule.name}
                  </td>
                  <td>
                    <div className="quality-task-cell">
                      <span>{schedule.task_label || `${schedule.bean_name}.${schedule.method_name}`}</span>
                      <code>{schedule.task || `${schedule.bean_name}.${schedule.method_name}`}</code>
                    </div>
                  </td>
                  <td>{schedule.method_params || "—"}</td>
                  <td>
                    <code>{schedule.cron_expression}</code>
                  </td>
                  <td>
                    <span className="quality-policy-cell">
                      {schedule.retry_limit
                        ? `失败重试 ${schedule.retry_limit} 次，间隔 ${schedule.retry_delay_seconds ?? 60} 秒`
                        : "失败不重试"}
                      <br />
                      {`错过时${schedule.misfire_policy_label || "跳过"}`}
                      {schedule.misfire_policy === "all" ? `，最多 ${schedule.max_backfill ?? 10} 次` : ""}
                    </span>
                  </td>
                  <td>
                    <RunningBadge
                      state={schedule.state}
                      label={schedule.state_label}
                    />
                  </td>
                  <td>
                    {schedule.last_status ? (
                      <span className="quality-outcome-cell" title={schedule.last_message || ""}>
                        <RunStatusBadge status={schedule.last_status} label={schedule.last_status_label} />
                        <small>{schedule.last_message || ""}</small>
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <div className="quality-actions">
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => setForm({ schedule })}
                      >
                        <Pencil size={13} />
                        编辑
                      </button>
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => void act(schedule, "toggle")}
                      >
                        <Power size={13} />
                        {schedule.state === 1 ? "停止" : "启动"}
                      </button>
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => void act(schedule, "run")}
                      >
                        <Play size={13} />
                        {busy === `run-${schedule.id}` ? "执行中…" : "执行"}
                      </button>
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => setRuns(schedule)}
                      >
                        <History size={13} />
                        记录
                      </button>
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => void act(schedule, "delete")}
                      >
                        <Trash2 size={13} />
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!schedules.busy && !items.length && !schedules.error && (
        <Empty title="还没有调度任务">
          点击“新增”创建 cron 调度，表达式支持 Quartz 六位（秒在最前）与 Unix
          五位两种写法。
        </Empty>
      )}
      <QualityPagination
        total={total}
        page={page}
        size={size}
        onPage={setPage}
        onSize={(next) => {
          setSize(next);
          setPage(1);
        }}
      />
      {runs && (
        <TaskRunsDrawer
          schedule={runs === "all" ? null : runs}
          onClose={() => setRuns(null)}
        />
      )}
      {form && (
        <ScheduleFormDrawer
          key={form.schedule ? form.schedule.id : "create"}
          schedule={form.schedule}
          onClose={() => setForm(null)}
          onSaved={(saved, mode) => {
            setForm(null);
            setError("");
            setNotice(
              mode === "create"
                ? `已新增调度任务「${saved.name}」。`
                : `已更新调度任务「${saved.name}」。`,
            );
            schedules.reload();
          }}
        />
      )}
    </section>
  );
}

function ScheduleFormDrawer({
  schedule,
  onClose,
  onSaved,
}: {
  schedule: QualitySchedule | null;
  onClose: () => void;
  onSaved: (saved: QualitySchedule, mode: "create" | "edit") => void;
}) {
  const [name, setName] = useState(schedule?.name ?? "");
  const [bean, setBean] = useState(schedule?.bean_name ?? "QualityTask");
  const [method, setMethod] = useState(schedule?.method_name ?? "run");
  const [params, setParams] = useState(schedule?.method_params ?? "");
  const [cron, setCron] = useState(schedule?.cron_expression ?? "0 0 12 * * ?");
  const [state, setState] = useState<number>(schedule?.state ?? 0);
  const [retryLimit, setRetryLimit] = useState<number>(schedule?.retry_limit ?? 0);
  const [retryDelay, setRetryDelay] = useState<number>(schedule?.retry_delay_seconds ?? 60);
  const [policy, setPolicy] = useState(schedule?.misfire_policy ?? "skip");
  const [maxBackfill, setMaxBackfill] = useState<number>(schedule?.max_backfill ?? 10);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const tasks = useQualityData<QualityTasks>("/api/quality/tasks");
  const options = tasks.data?.items ?? [];
  const selected = options.find((item) => item.bean_name === bean && item.method_name === method);
  function pickTask(key: string) {
    const task = options.find((item) => item.key === key);
    if (!task) return;
    setBean(task.bean_name);
    setMethod(task.method_name);
  }
  async function save() {
    if (!name.trim()) {
      setError("请填写任务名称。");
      return;
    }
    if (selected?.params_required && !params.trim()) {
      setError(`请填写${selected.params_label}：${selected.params_hint}`);
      return;
    }
    if (!cron.trim()) {
      setError("请填写 cron 表达式。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const saved = await request<QualitySchedule>(
        schedule
          ? `/api/quality/schedules/${schedule.id}/update`
          : "/api/quality/schedules",
        {
          name: name.trim(),
          bean_name: bean.trim(),
          method_name: method.trim(),
          method_params: params.trim(),
          cron_expression: cron.trim(),
          state,
          retry_limit: retryLimit,
          retry_delay_seconds: retryDelay,
          misfire_policy: policy,
          max_backfill: maxBackfill,
        },
      );
      onSaved(saved, schedule ? "edit" : "create");
    } catch (e) {
      setError(errorMessage(e));
      setSaving(false);
    }
  }
  return (
    <Drawer
      id="quality-schedule-form"
      title={schedule ? `编辑调度任务 · ${schedule.name}` : "新增调度任务"}
      icon={<CalendarClock size={17} />}
      onClose={onClose}
      wide
      closeOnBackdrop={false}
    >
      <form
        className="quality-drawer-body"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <QualityField id="quality-schedule-name" label="任务名称" required>
          <input
            id="quality-schedule-name"
            value={name}
            maxLength={200}
            placeholder="例如：每日全量核查"
            onChange={(event) => setName(event.target.value)}
          />
        </QualityField>
        <QualityField
          id="quality-schedule-cron"
          label="cron表达式"
          required
          help="Quartz 六位（秒在最前），如 0 0 12 * * ?；也接受 Unix 五位写法。"
        >
          <input
            id="quality-schedule-cron"
            value={cron}
            maxLength={120}
            spellCheck={false}
            onChange={(event) => setCron(event.target.value)}
          />
        </QualityField>
        <QualityField
          id="quality-schedule-task"
          label="任务"
          required
          help={selected?.description || "选择调度器要执行的任务；任务由平台各模块注册。"}
        >
          <select
            id="quality-schedule-task"
            value={selected?.key ?? `${bean}.${method}`}
            onChange={(event) => pickTask(event.target.value)}
          >
            {!selected && <option value={`${bean}.${method}`}>{`${bean}.${method}`}</option>}
            {options.map((task) => (
              <option key={task.key} value={task.key}>
                {task.category} · {task.label}（{task.key}）
              </option>
            ))}
          </select>
        </QualityField>
        <QualityField
          id="quality-schedule-params"
          label={selected?.params_label || "方法参数"}
          required={!!selected?.params_required}
          help={selected?.params_hint || "可选。"}
        >
          <input
            id="quality-schedule-params"
            value={params}
            maxLength={500}
            placeholder={selected?.params_required ? "" : "可选"}
            onChange={(event) => setParams(event.target.value)}
          />
        </QualityField>
        <div className="quality-field-row">
          <QualityField
            id="quality-schedule-retry"
            label="失败重试次数"
            help="0 表示失败后不自动重试；最多 10 次。"
          >
            <input
              id="quality-schedule-retry"
              type="number"
              min={0}
              max={10}
              value={retryLimit}
              onChange={(event) => setRetryLimit(Math.max(0, Math.min(10, Number(event.target.value) || 0)))}
            />
          </QualityField>
          <QualityField
            id="quality-schedule-retry-delay"
            label="重试间隔（秒）"
            help="两次尝试之间的等待时间，1 到 3600 秒。"
          >
            <input
              id="quality-schedule-retry-delay"
              type="number"
              min={1}
              max={3600}
              value={retryDelay}
              disabled={retryLimit === 0}
              onChange={(event) => setRetryDelay(Math.max(1, Math.min(3600, Number(event.target.value) || 60)))}
            />
          </QualityField>
        </div>
        <div className="quality-field-row">
          <QualityField
            id="quality-schedule-misfire"
            label="错过触发时"
            help="服务停机期间错过的触发：跳过并从现在重新计算，补跑一次，或按错过的每个时间点逐次补跑。"
          >
            <select
              id="quality-schedule-misfire"
              value={policy}
              onChange={(event) => setPolicy(event.target.value)}
            >
              {(tasks.data?.misfire_policies ?? [
                { value: "skip", label: "跳过" },
                { value: "once", label: "补跑一次" },
                { value: "all", label: "逐次补跑" },
              ]).map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </QualityField>
          <QualityField
            id="quality-schedule-backfill"
            label="补跑上限"
            help="逐次补跑时最多补跑多少个错过的时间点，1 到 50。"
          >
            <input
              id="quality-schedule-backfill"
              type="number"
              min={1}
              max={50}
              value={maxBackfill}
              disabled={policy !== "all"}
              onChange={(event) => setMaxBackfill(Math.max(1, Math.min(50, Number(event.target.value) || 10)))}
            />
          </QualityField>
        </div>
        <QualityField label="状态" required>
          <div className="quality-radio-row">
            <label>
              <input
                type="radio"
                name="quality-schedule-state"
                checked={state === 0}
                onChange={() => setState(0)}
              />
              停止
            </label>
            <label>
              <input
                type="radio"
                name="quality-schedule-state"
                checked={state === 1}
                onChange={() => setState(1)}
              />
              运行
            </label>
          </div>
        </QualityField>
        {error && <ErrorBanner message={error} />}
        <div className="button-row">
          <button type="submit" className="primary-button" disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </button>
          <button
            type="button"
            className="text-button"
            disabled={saving}
            onClick={onClose}
          >
            取消
          </button>
        </div>
      </form>
    </Drawer>
  );
}

function TaskRunsDrawer({
  schedule,
  onClose,
}: {
  schedule: QualitySchedule | null;
  onClose: () => void;
}) {
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const runs = useQualityData<Paged<QualityTaskRun>>(
    `/api/quality/task-runs${queryString({
      schedule_id: schedule ? schedule.id : "",
      status,
      page,
      size,
    })}`,
  );
  const items = runs.data?.items ?? [];
  return (
    <Drawer
      id="quality-task-runs"
      title={schedule ? `执行记录 · ${schedule.name}` : "任务执行记录"}
      icon={<History size={17} />}
      onClose={onClose}
      wide
    >
      <div className="quality-drawer-body">
        <div className="quality-toolbar">
          <label htmlFor="quality-task-run-status">状态</label>
          <select
            id="quality-task-run-status"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(1);
            }}
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="retrying">等待重试</option>
            <option value="running">运行中</option>
          </select>
          <button
            type="button"
            className="secondary-button"
            disabled={runs.busy}
            onClick={runs.reload}
          >
            <RefreshCw size={14} className={runs.busy ? "spin" : ""} />
            刷新
          </button>
        </div>
        {runs.error && <ErrorBanner message={`执行记录加载失败：${runs.error}`} />}
        {runs.busy && !runs.data ? (
          <Loading text="正在读取执行记录…" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>开始时间</th>
                  {!schedule && <th>调度任务</th>}
                  <th>任务</th>
                  <th>触发</th>
                  <th>次序</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <tr key={run.id}>
                    <td title={run.planned_time ? `计划时间：${formatTime(run.planned_time)}` : ""}>
                      {formatTime(run.start_time)}
                    </td>
                    {!schedule && <td>{run.schedule_name || run.schedule_id || "—"}</td>}
                    <td>
                      <div className="quality-task-cell">
                        <span>{run.task_label || run.task}</span>
                        <code>{run.task}</code>
                      </div>
                    </td>
                    <td>
                      <span className="neutral-badge">{run.trigger_label || run.trigger_type}</span>
                    </td>
                    <td>第 {run.attempt} 次</td>
                    <td>
                      <RunStatusBadge status={run.status} label={run.status_label} />
                    </td>
                    <td>{formatElapsed(run.elapsed_ms)}</td>
                    <td className="quality-run-message">{run.message || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!runs.busy && !items.length && !runs.error && (
          <Empty title="还没有执行记录">
            调度触发、手动执行、失败重试与停机补跑都会记录在这里。
          </Empty>
        )}
        <QualityPagination
          total={runs.data?.total ?? 0}
          page={page}
          size={size}
          onPage={setPage}
          onSize={(next) => {
            setSize(next);
            setPage(1);
          }}
        />
      </div>
    </Drawer>
  );
}

/* ------------------------------------------------------------------ 质量报告分析 */

function ReportTab() {
  const [dateInput, setDateInput] = useState(todayValue);
  const [date, setDate] = useState(dateInput);
  const report = useQualityData<QualityReport>(
    `/api/quality/report${queryString({ date })}`,
  );
  const data = report.data;
  const maxDatasourceError = Math.max(
    1,
    ...(data?.datasource_errors ?? []).map((item) => item.error_count),
  );
  return (
    <>
      <section className="panel quality-main">
        <div className="quality-toolbar">
          <label htmlFor="quality-report-date">报告日期</label>
          <input
            id="quality-report-date"
            type="date"
            value={dateInput}
            onChange={(event) => setDateInput(event.target.value)}
          />
          <button
            className="primary-button"
            onClick={() => setDate(dateInput)}
            disabled={!dateInput}
          >
            <Search size={14} />
            搜索
          </button>
          <div className="quality-toolbar-end">
            <button
              className="secondary-button"
              disabled={report.busy}
              onClick={report.reload}
            >
              <RefreshCw size={14} className={report.busy ? "spin" : ""} />
              刷新
            </button>
          </div>
        </div>
        {report.error && (
          <ErrorBanner message={`质量报告加载失败：${report.error}`} />
        )}
        {report.busy && !data && <Loading text="正在生成质量分析报告…" />}
      </section>
      {data && (
        <>
          <h2 className="quality-report-title">{data.date}质量分析报告</h2>
          <div className="metric-grid">
            {[
              { label: "核查总量", value: formatCount(data.total_checked) },
              { label: "不合规总量", value: formatCount(data.total_errors) },
              { label: "质量得分", value: formatScore(data.score) },
              { label: "质量等级", value: data.level_label || "—" },
            ].map((item) => (
              <div className="metric-card" key={item.label}>
                <div>
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                </div>
              </div>
            ))}
          </div>
          <section className="panel">
            <div className="panel-heading">
              <h2>错误量统计分析</h2>
              <span>{data.date}</span>
            </div>
            <div className="quality-report-grid">
              <div className="quality-report-block">
                <h3 className="quality-section-title">按数据源与规则级别</h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>数据源</th>
                        <th>规则级别</th>
                        <th>错误量</th>
                        <th>占比</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.datasource_errors.map((item, index) => (
                        <tr
                          key={`${item.datasource_name}-${item.level}-${index}`}
                        >
                          <td>{item.datasource_name}</td>
                          <td>
                            <LevelBadge
                              level={item.level}
                              label={item.level_label}
                            />
                          </td>
                          <td>{formatCount(item.error_count)}</td>
                          <td className="quality-bar-cell">
                            <span className="quality-bar">
                              <i
                                style={{
                                  width: `${Math.max(3, (item.error_count / maxDatasourceError) * 100)}%`,
                                }}
                              />
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!data.datasource_errors.length && (
                  <Empty title="当日没有数据源错误量" />
                )}
              </div>
              <div className="quality-report-block">
                <h3 className="quality-section-title">按规则类型与规则名称</h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>规则类型</th>
                        <th>规则名称</th>
                        <th>规则级别</th>
                        <th>错误量</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.rule_errors.map((item, index) => (
                        <tr key={`${item.rule_name}-${index}`}>
                          <td>
                            <span className="dimension-tag">
                              {item.dimension_label}
                            </span>
                          </td>
                          <td>{item.rule_name}</td>
                          <td>
                            <LevelBadge
                              level={item.level}
                              label={item.level_label}
                            />
                          </td>
                          <td>
                            <span className="failure-badge">
                              {formatCount(item.error_count)}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!data.rule_errors.length && (
                  <Empty title="当日没有规则错误量" />
                )}
              </div>
            </div>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>规则类型统计分析</h2>
              <span>{data.dimension_sections.length} 类</span>
            </div>
            {data.dimension_sections.map((section) => (
              <div className="quality-report-section" key={section.dimension}>
                <h3 className="quality-section-title">
                  {section.dimension_label}
                </h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>规则名称</th>
                        <th>数据源</th>
                        <th>数据表(中文)</th>
                        <th>数据表(英文)</th>
                        <th>核查字段(中文)</th>
                        <th>核查字段(英文)</th>
                        <th>核查数</th>
                        <th>不合规数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {section.rows.map((row, index) => (
                        <tr key={`${row.rule_name}-${index}`}>
                          <td>{row.rule_name}</td>
                          <td>{row.datasource_name}</td>
                          <td>{row.table_label || row.table_name}</td>
                          <td>{row.table_name}</td>
                          <td>{row.column_label || row.column_name || "—"}</td>
                          <td>{row.column_name || "—"}</td>
                          <td>{formatCount(row.checked_count)}</td>
                          <td
                            className={
                              row.actual_value > 0 ? "quality-error-cell" : ""
                            }
                          >
                            {formatCount(row.actual_value)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!section.rows.length && (
                  <Empty title={`${section.dimension_label}当日没有核查记录`} />
                )}
              </div>
            ))}
            {!data.dimension_sections.length && (
              <Empty title="当日没有核查记录">
                在“质量规则管理”中执行规则，或启用调度任务后再查看当日报告。
              </Empty>
            )}
          </section>
        </>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 质量统计分析 */

function StatisticsTab({ dimensions }: { dimensions: CatalogDimension[] }) {
  const [dimension, setDimension] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [name, setName] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const statistics = useQualityData<QualityStatistics>(
    `/api/quality/statistics${queryString({ name })}`,
  );
  const tree = statistics.data?.tree?.length
    ? statistics.data.tree
    : dimensions.map((item) => ({
        dimension: item.id,
        dimension_label: item.label,
        error_count: 0,
      }));
  const items = (statistics.data?.items ?? []).filter(
    (item) => !dimension || item.metric_dimension === dimension,
  );
  const visible = items.slice((page - 1) * size, page * size);
  return (
    <div className="quality-layout">
      <DimensionTree
        title="核查规则类型"
        allLabel="全部规则"
        active={dimension}
        items={tree.map((item) => ({
          id: item.dimension,
          label: item.dimension_label,
          hint: (
            <small className="quality-tree-count">
              (错误数: {formatCount(item.error_count)})
            </small>
          ),
        }))}
        onSelect={(id) => {
          setDimension(id);
          setPage(1);
        }}
      />
      <section className="panel quality-main">
        <div className="quality-toolbar">
          <label htmlFor="quality-statistics-search">规则名称</label>
          <input
            id="quality-statistics-search"
            className="search-input"
            value={nameInput}
            maxLength={200}
            placeholder="请输入规则名称"
            onChange={(event) => setNameInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                setName(nameInput.trim());
                setPage(1);
              }
            }}
          />
          <button
            className="primary-button"
            onClick={() => {
              setName(nameInput.trim());
              setPage(1);
            }}
          >
            <Search size={14} />
            搜索
          </button>
          <button
            className="secondary-button"
            onClick={() => {
              setNameInput("");
              setName("");
              setDimension("");
              setPage(1);
            }}
          >
            <RotateCcw size={14} />
            重置
          </button>
          <div className="quality-toolbar-end">
            <button
              className="secondary-button"
              disabled={statistics.busy}
              onClick={statistics.reload}
            >
              <RefreshCw size={14} className={statistics.busy ? "spin" : ""} />
              刷新
            </button>
          </div>
        </div>
        {statistics.error && (
          <ErrorBanner message={`质量统计加载失败：${statistics.error}`} />
        )}
        {statistics.busy && !statistics.data ? (
          <Loading text="正在统计最新核查结果…" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>序号</th>
                  <th>规则名称</th>
                  <th>规则类型</th>
                  <th>数据源</th>
                  <th>数据表</th>
                  <th>核查字段</th>
                  <th>核查数量</th>
                  <th>不合规数量</th>
                  <th>核查时间</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item, index) => (
                  <tr key={item.id}>
                    <td>{(page - 1) * size + index + 1}</td>
                    <td>{item.rule_name}</td>
                    <td>
                      <span className="dimension-tag">
                        {item.dimension_label}
                      </span>{" "}
                      {item.metric_label}
                    </td>
                    <td>{item.datasource_name}</td>
                    <td>
                      <span className="table-name">
                        <Table2 size={13} />
                        {item.database_name
                          ? `${item.database_name}.${item.table_name}`
                          : item.table_name}
                      </span>
                    </td>
                    <td>{item.column_name || "—"}</td>
                    <td>{formatCount(item.checked_count)}</td>
                    <td
                      className={
                        item.actual_value > 0 ? "quality-error-cell" : ""
                      }
                    >
                      {formatCount(item.actual_value)}
                    </td>
                    <td>{formatTime(item.check_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!statistics.busy && !visible.length && !statistics.error && (
          <Empty title="暂无核查结果">
            执行核查规则后，这里显示每条规则最近一次的核查数量与不合规数量。
          </Empty>
        )}
        <QualityPagination
          total={items.length}
          page={page}
          size={size}
          onPage={setPage}
          onSize={(next) => {
            setSize(next);
            setPage(1);
          }}
        />
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ 质量执行日志 */

function ExecutionsTab() {
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [detailId, setDetailId] = useState<number | null>(null);
  const executions = useQualityData<Paged<QualityExecution>>(
    `/api/quality/executions${queryString({ status, page, size })}`,
  );
  const items = executions.data?.items ?? [];
  const total = executions.data?.total ?? items.length;
  return (
    <section className="panel quality-main">
      <div className="quality-toolbar">
        <label htmlFor="quality-execution-status">执行状态</label>
        <select
          id="quality-execution-status"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value);
            setPage(1);
          }}
        >
          <option value="">全部状态</option>
          {EXECUTION_STATUSES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <div className="quality-toolbar-end">
          <button
            className="secondary-button"
            disabled={executions.busy}
            onClick={executions.reload}
          >
            <RefreshCw size={14} className={executions.busy ? "spin" : ""} />
            刷新
          </button>
        </div>
      </div>
      {executions.error && (
        <ErrorBanner message={`执行日志加载失败：${executions.error}`} />
      )}
      {executions.busy && !executions.data ? (
        <Loading text="正在读取执行日志…" />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>序号</th>
                <th>规则名称</th>
                <th>触发方式</th>
                <th>状态</th>
                <th>开始时间</th>
                <th>耗时</th>
                <th>说明</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((execution, index) => (
                <tr key={execution.id}>
                  <td>{(page - 1) * size + index + 1}</td>
                  <td>{execution.rule_name || "全部启用规则"}</td>
                  <td>
                    <span className="neutral-badge">
                      {execution.trigger_label ||
                        (execution.trigger_type === "schedule"
                          ? "调度"
                          : "手动")}
                    </span>
                  </td>
                  <td>
                    <ExecutionBadge
                      status={execution.status}
                      label={execution.status_label}
                    />
                  </td>
                  <td>{formatTime(execution.start_time)}</td>
                  <td>{formatElapsed(execution.elapsed_ms)}</td>
                  <td title={execution.message}>{execution.message || "—"}</td>
                  <td>
                    <div className="quality-actions">
                      <button
                        className="text-button"
                        onClick={() => setDetailId(execution.id)}
                      >
                        <Eye size={13} />
                        详情
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!executions.busy && !items.length && !executions.error && (
        <Empty title="暂无执行记录">
          手动执行核查规则或启用调度任务后，这里记录每一次核查的状态与耗时。
        </Empty>
      )}
      <QualityPagination
        total={total}
        page={page}
        size={size}
        onPage={setPage}
        onSize={(next) => {
          setSize(next);
          setPage(1);
        }}
      />
      {detailId !== null && (
        <ExecutionDrawer
          key={detailId}
          executionId={detailId}
          onClose={() => setDetailId(null)}
        />
      )}
    </section>
  );
}

function ExecutionDrawer({
  executionId,
  onClose,
}: {
  executionId: number;
  onClose: () => void;
}) {
  const execution = useQualityData<QualityExecution>(
    `/api/quality/executions/${executionId}`,
  );
  const [failures, setFailures] = useState<FailureSample | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const data = execution.data;
  const results = data?.results ?? [];
  const ruleId = data?.rule_id ?? results[0]?.rule_id ?? null;

  async function loadFailures(id: number) {
    setBusy(true);
    setError("");
    try {
      setFailures(
        await request<FailureSample>(
          `/api/quality/rules/${id}/failures?limit=20`,
        ),
      );
    } catch (e) {
      setError(`读取错误数据失败：${errorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Drawer
      id="quality-execution-detail"
      title={`执行详情 · #${executionId}`}
      icon={<ScrollText size={17} />}
      onClose={onClose}
      wide
    >
      <div className="quality-drawer-body">
        {execution.error && (
          <ErrorBanner message={`执行详情加载失败：${execution.error}`} />
        )}
        {execution.busy && !data && <Loading text="正在读取执行详情…" />}
        {data && (
          <>
            <dl className="quality-detail">
              <dt>规则名称</dt>
              <dd>{data.rule_name || "全部启用规则"}</dd>
              <dt>触发方式</dt>
              <dd>
                {data.trigger_label ||
                  (data.trigger_type === "schedule" ? "调度" : "手动")}
              </dd>
              <dt>状态</dt>
              <dd>
                <ExecutionBadge
                  status={data.status}
                  label={data.status_label}
                />
              </dd>
              <dt>开始时间</dt>
              <dd>{formatTime(data.start_time)}</dd>
              <dt>结束时间</dt>
              <dd>{formatTime(data.end_time)}</dd>
              <dt>耗时</dt>
              <dd>{formatElapsed(data.elapsed_ms)}</dd>
              <dt>说明</dt>
              <dd>{data.message || "—"}</dd>
            </dl>
            {results.map((result) => (
              <div className="quality-result-card" key={result.id}>
                <div className="quality-result-head">
                  <strong>{result.rule_name}</strong>
                  <ResultBadge
                    state={result.state}
                    label={result.state_label}
                  />
                </div>
                <dl className="quality-detail">
                  <dt>核查类型</dt>
                  <dd>
                    {result.dimension_label} · {result.metric_label}
                  </dd>
                  <dt>核查对象</dt>
                  <dd>
                    {result.datasource_name} ·{" "}
                    {result.database_name
                      ? `${result.database_name}.${result.table_name}`
                      : result.table_name}
                    {result.column_name ? ` · ${result.column_name}` : ""}
                  </dd>
                  <dt>核查数量</dt>
                  <dd>{formatCount(result.checked_count)}</dd>
                  <dt>不合规数量</dt>
                  <dd>{formatCount(result.actual_value)}</dd>
                  <dt>期望值</dt>
                  <dd>{formatCount(result.expected_value)}</dd>
                  <dt>得分</dt>
                  <dd>{formatScore(result.score)}</dd>
                  <dt>核查时间</dt>
                  <dd>{formatTime(result.check_time)}</dd>
                </dl>
                {result.invalidate_sql && (
                  <pre className="sql-code">{result.invalidate_sql}</pre>
                )}
              </div>
            ))}
            {!results.length && !execution.busy && (
              <Empty title="该次执行没有核查结果" />
            )}
            <h3 className="quality-section-title">错误数据</h3>
            <p className="muted">
              仅按只读方式抽样展示最多 20
              行不合规记录，不会向目标数据源写入任何数据。
            </p>
            {ruleId === null ? (
              <p className="muted">
                该次执行覆盖多条规则，请在规则列表中单独取样。
              </p>
            ) : (
              <button
                className="secondary-button"
                disabled={busy}
                onClick={() => void loadFailures(ruleId)}
              >
                <Eye size={14} />
                {busy ? "取样中…" : "查看错误数据"}
              </button>
            )}
            {error && <ErrorBanner message={error} />}
            {failures && <FailureGrid sample={failures} />}
          </>
        )}
      </div>
    </Drawer>
  );
}

function FailureGrid({ sample }: { sample: FailureSample }) {
  const notes = [
    sample.warning,
    sample.truncated ? "仅显示前若干条，结果已截断。" : "",
    typeof sample.elapsed_ms === "number"
      ? `取样耗时 ${sample.elapsed_ms} ms`
      : "",
  ].filter(Boolean);
  if (sample.columns?.length && sample.rows?.length)
    return (
      <>
        <DataGrid columns={sample.columns} rows={sample.rows} />
        {notes.length > 0 && <p className="muted">{notes.join(" · ")}</p>}
        {sample.sql && <pre className="sql-code">{sample.sql}</pre>}
      </>
    );
  return (
    <>
      <Empty title="没有取到不合规记录">
        {sample.warning || "该规则最近一次核查未发现不合规数据。"}
      </Empty>
      {sample.sql && <pre className="sql-code">{sample.sql}</pre>}
    </>
  );
}
