// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, type ReactNode } from "react";
import {
  ArrowRight,
  Columns3,
  Cpu,
  Database,
  Eye,
  FolderTree,
  HardDrive,
  Layers,
  RefreshCw,
  Table2,
  Upload,
  Warehouse,
} from "lucide-react";
import {
  request,
  errorMessage,
  datasourcePath,
  previewSql,
  type DataSource,
  type IngestTable,
  type QueryResult,
  type SchemaInfo,
  type TableDetail,
  type TableInfo,
  type TestResult,
} from "./api";
import { DataGrid, Empty, ErrorBanner, Loading } from "./components";

export type GoTo = (page: string, sql?: string, sourceId?: string) => void;

export interface ApiState<T> {
  data: T | null;
  error: string;
  loading: boolean;
  reload: () => void;
}
/** Loads a GET endpoint whenever `path` changes; `null` clears the state without a request. */
export function useApi<T>(path: string | null): ApiState<T> {
  const [state, setState] = useState<Omit<ApiState<T>, "reload">>({
    data: null,
    error: "",
    loading: path !== null,
  });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (path === null) {
      setState({ data: null, error: "", loading: false });
      return;
    }
    let active = true;
    setState({ data: null, error: "", loading: true });
    request<T>(path)
      .then((data) => {
        if (active) setState({ data, error: "", loading: false });
      })
      .catch((e: unknown) => {
        if (active)
          setState({ data: null, error: errorMessage(e), loading: false });
      });
    return () => {
      active = false;
    };
  }, [path, tick]);
  return { ...state, reload: () => setTick((value) => value + 1) };
}

export function categoryIcon(category: string, size = 18): ReactNode {
  const Icon = category.includes("本地")
    ? HardDrive
    : category.includes("OLAP")
      ? Cpu
      : category.includes("数仓")
        ? Warehouse
        : category.includes("表格式") || category.includes("数据湖")
          ? Layers
          : Database;
  return <Icon size={size} />;
}
/** Trailing detail of a connection summary, e.g. "lattice_demo" of "127.0.0.1:5432 / lattice_demo". */
function summaryDetail(source: DataSource): string {
  const parts = (source.summary ?? "").split("/");
  return (parts[parts.length - 1] ?? "").trim();
}
export function sourceLabel(source: DataSource, all: DataSource[] = []) {
  // The engine is what a reader picks between, and the source name only repeats
  // it ("本地 ClickHouse（示例数据）" beside "ClickHouse"), so the engine alone is
  // the label. Two sources on the same engine would otherwise become identical
  // entries, so those gain the smallest suffix that separates them: the database
  // they point at, or their own name when even that matches.
  const label = source.type_label;
  const siblings = all.filter((item) => item.type_label === label);
  if (siblings.length < 2) return label;
  const detail = summaryDetail(source);
  const detailIsUnique =
    !!detail &&
    siblings.filter((item) => summaryDetail(item) === detail).length === 1;
  if (detailIsUnique) return `${label}（${detail}）`;
  // Same engine and the same database: only the name separates them, and it
  // already carries the engine, so it is used as-is rather than nested inside it.
  return source.name;
}
export function SourceSelect({
  id,
  sources,
  value,
  onChange,
  disabled = false,
  label = "数据源",
  compact = false,
}: {
  id: string;
  sources: DataSource[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  label?: string;
  compact?: boolean;
}) {
  const known = sources.some((source) => source.id === value);
  return (
    <>
      {!compact && <label htmlFor={id}>{label}</label>}
      <select
        id={id}
        className={compact ? "status-select" : undefined}
        aria-label={compact ? `选择${label}` : undefined}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {!known && value && <option value={value}>{value}</option>}
        {!sources.length && !value && <option value="">暂无数据源</option>}
        {sources.map((source) => (
          <option key={source.id} value={source.id}>
            {sourceLabel(source, sources)}
          </option>
        ))}
      </select>
    </>
  );
}
export function StatusBadge({ status }: { status?: string | null }) {
  if (status === "online") return <span className="healthy-badge">在线</span>;
  if (status === "offline") return <span className="failure-badge">离线</span>;
  if (status === "error") return <span className="failure-badge">异常</span>;
  return (
    <span className="neutral-badge">
      {status && status !== "unknown" && status !== "untested"
        ? status
        : "未测试"}
    </span>
  );
}
export function SourceStatusBadge({ test }: { test?: TestResult | null }) {
  if (!test) return <span className="neutral-badge">未测试</span>;
  const text =
    test.status === "online"
      ? "在线"
      : test.status === "offline"
        ? "离线"
        : "异常";
  return (
    <span
      className={test.status === "online" ? "healthy-badge" : "failure-badge"}
      title={`${test.detail}（${new Date(test.tested_at).toLocaleString("zh-CN")}）`}
    >
      {text} · {Math.round(test.latency_ms)} ms
      {test.server_version ? ` · ${test.server_version}` : ""}
    </span>
  );
}
/** Picks the schema the source is configured for, falling back to common defaults. */
export function preferredSchema(
  source: DataSource | undefined,
  schemas: SchemaInfo[],
): string {
  const config = source?.config ?? {};
  const candidates = [
    config.schema,
    config.namespace,
    config.database,
    "main",
    "public",
    "default",
  ].filter(
    (value): value is string => typeof value === "string" && value !== "",
  );
  return (
    candidates.find((name) => schemas.some((schema) => schema.name === name)) ??
    schemas[0]?.name ??
    ""
  );
}
export function useSchemas(source: DataSource | undefined) {
  return useApi<{ items: SchemaInfo[] }>(
    source ? datasourcePath(source.id, "/schemas") : null,
  );
}
export function useTables(source: DataSource | undefined, schema: string) {
  return useApi<{ items: TableInfo[] }>(
    source && schema
      ? `${datasourcePath(source.id, "/tables")}?schema=${encodeURIComponent(schema)}`
      : null,
  );
}
/** Tables of the source's default schema (used for the 智能问数 table count). */
export function useDefaultTables(source: DataSource | undefined) {
  const schemas = useSchemas(source);
  const schema = schemas.data
    ? preferredSchema(source, schemas.data.items)
    : "";
  const tables = useTables(source, schema);
  return {
    schema,
    tables: tables.data?.items ?? null,
    loading: schemas.loading || tables.loading,
    error: schemas.error || tables.error,
  };
}
export function SchemaTablePicker({
  source,
  schema,
  table,
  onSchema,
  onTable,
  idPrefix,
  autoSelectTable = true,
  tablePlaceholder = "选择数据表",
  disabled = false,
}: {
  source: DataSource | undefined;
  schema: string;
  table: string;
  onSchema: (schema: string) => void;
  onTable: (table: string) => void;
  idPrefix: string;
  autoSelectTable?: boolean;
  tablePlaceholder?: string;
  disabled?: boolean;
}) {
  const schemas = useSchemas(source);
  const tables = useTables(source, schema);
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema))
      onSchema(preferredSchema(source, items));
  }, [schemas.data]);
  useEffect(() => {
    if (!tables.data) return;
    const items = tables.data.items;
    if (items.some((item) => item.name === table)) return;
    onTable(autoSelectTable ? (items[0]?.name ?? "") : "");
  }, [tables.data]);
  const schemaItems = schemas.data?.items ?? [];
  const tableItems = tables.data?.items ?? [];
  return (
    <>
      <label htmlFor={`${idPrefix}-schema`}>Schema</label>
      <select
        id={`${idPrefix}-schema`}
        value={schema}
        disabled={disabled || !schemas.data}
        onChange={(event) => {
          onSchema(event.target.value);
          onTable("");
        }}
      >
        {!schemaItems.length && (
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
        {schemaItems.map((item) => (
          <option key={item.name} value={item.name}>
            {item.name}
            {typeof item.table_count === "number"
              ? `（${item.table_count}）`
              : ""}
          </option>
        ))}
      </select>
      <label htmlFor={`${idPrefix}-table`}>数据表</label>
      <select
        id={`${idPrefix}-table`}
        value={table}
        disabled={disabled || !tables.data}
        onChange={(event) => onTable(event.target.value)}
      >
        {(!autoSelectTable || !table) && (
          <option value="" disabled={autoSelectTable && tableItems.length > 0}>
            {tables.loading
              ? "加载中…"
              : tableItems.length
                ? tablePlaceholder
                : tables.error
                  ? "加载失败"
                  : "暂无数据表"}
          </option>
        )}
        {tableItems.map((item) => (
          <option key={item.name} value={item.name}>
            {item.name}
            {item.kind && item.kind !== "table" ? `（${item.kind}）` : ""}
          </option>
        ))}
      </select>
      {(schemas.error || tables.error) && (
        <span className="failure-badge picker-error" role="alert">
          {schemas.error || tables.error}
        </span>
      )}
    </>
  );
}

function rowsLabel(rows: number | null | undefined) {
  return typeof rows === "number" ? `${rows.toLocaleString()} 行` : "行数未知";
}

/** Three-column metadata browser: schemas → tables → table detail with preview and Polaris registration. */
export function SourceBrowser({
  source,
  goTo,
}: {
  source: DataSource;
  goTo: GoTo;
}) {
  const [schema, setSchema] = useState("");
  const [table, setTable] = useState("");
  const [filter, setFilter] = useState("");
  const schemas = useSchemas(source);
  const tables = useTables(source, schema);
  const detail = useApi<TableDetail>(
    schema && table
      ? `${datasourcePath(source.id, "/table")}?schema=${encodeURIComponent(schema)}&name=${encodeURIComponent(table)}`
      : null,
  );
  const [preview, setPreview] = useState<{
    busy: boolean;
    error: string;
    result: QueryResult | null;
  }>({ busy: false, error: "", result: null });
  const [register, setRegister] = useState<{
    busy: boolean;
    error: string;
    result: IngestTable | null;
  }>({ busy: false, error: "", result: null });
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema))
      setSchema(preferredSchema(source, items));
  }, [schemas.data]);
  useEffect(() => {
    setPreview({ busy: false, error: "", result: null });
    setRegister({ busy: false, error: "", result: null });
  }, [schema, table]);
  const visibleTables = (tables.data?.items ?? []).filter((item) =>
    item.name.toLowerCase().includes(filter.toLowerCase()),
  );
  async function runPreview() {
    setPreview({ busy: true, error: "", result: null });
    try {
      const result = await request<QueryResult>(
        datasourcePath(source.id, "/preview"),
        { schema, name: table, limit: 100 },
      );
      setPreview({ busy: false, error: "", result });
    } catch (e) {
      setPreview({ busy: false, error: errorMessage(e), result: null });
    }
  }
  async function registerTable() {
    setRegister({ busy: true, error: "", result: null });
    try {
      const result = await request<IngestTable>("/api/ingest/register", {
        datasource_id: source.id,
        schema,
        name: table,
      });
      setRegister({ busy: false, error: "", result });
    } catch (e) {
      setRegister({ busy: false, error: errorMessage(e), result: null });
    }
  }
  return (
    <div className="browser-layout">
      <aside className="browser-column" aria-label="Schema 列表">
        <div className="browser-column-heading">
          <FolderTree size={14} />
          Schema
          <span>{schemas.data ? schemas.data.items.length : ""}</span>
          <button
            className="icon-button"
            aria-label="刷新 Schema 列表"
            onClick={schemas.reload}
            disabled={schemas.loading}
          >
            <RefreshCw size={13} className={schemas.loading ? "spin" : ""} />
          </button>
        </div>
        {schemas.loading && <Loading text="读取 Schema…" />}
        {schemas.error && <ErrorBanner message={schemas.error} />}
        <div className="browser-list">
          {schemas.data?.items.map((item) => (
            <button
              key={item.name}
              className={`browser-item ${item.name === schema ? "active" : ""}`}
              aria-pressed={item.name === schema}
              onClick={() => {
                setSchema(item.name);
                setTable("");
              }}
            >
              <span>{item.name}</span>
              {typeof item.table_count === "number" && (
                <small>{item.table_count} 表</small>
              )}
            </button>
          ))}
        </div>
        {schemas.data && !schemas.data.items.length && (
          <Empty title="没有 Schema" />
        )}
      </aside>
      <aside className="browser-column" aria-label="数据表列表">
        <div className="browser-column-heading">
          <Table2 size={14} />
          数据表
          <span>{tables.data ? tables.data.items.length : ""}</span>
          <button
            className="icon-button"
            aria-label="刷新数据表列表"
            onClick={tables.reload}
            disabled={tables.loading || !schema}
          >
            <RefreshCw size={13} className={tables.loading ? "spin" : ""} />
          </button>
        </div>
        <input
          className="browser-filter"
          aria-label="筛选数据表"
          placeholder="筛选表名…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        {tables.loading && <Loading text="读取数据表…" />}
        {tables.error && <ErrorBanner message={tables.error} />}
        <div className="browser-list">
          {visibleTables.map((item) => (
            <button
              key={item.name}
              className={`browser-item ${item.name === table ? "active" : ""}`}
              aria-pressed={item.name === table}
              title={item.comment || item.name}
              onClick={() => setTable(item.name)}
            >
              <span>{item.name}</span>
              {item.kind && item.kind !== "table" && (
                <small className="neutral-badge">{item.kind}</small>
              )}
              <small>{rowsLabel(item.rows)}</small>
            </button>
          ))}
        </div>
        {tables.data && !visibleTables.length && (
          <Empty title={filter ? "没有匹配的表" : "该 Schema 下没有表"} />
        )}
      </aside>
      <section className="browser-detail" aria-label="表详情">
        {!table ? (
          <Empty title="选择一张表查看字段">
            左侧选择 Schema 与数据表后，可预览数据、在 SQL 工作台查询或注册到
            Polaris。
          </Empty>
        ) : (
          <>
            <div className="browser-detail-heading">
              <div>
                <h2>
                  <Table2 size={15} />
                  {schema}.{table}
                </h2>
                <span>
                  {detail.data?.kind ?? "table"} ·{" "}
                  {rowsLabel(detail.data?.rows)}
                </span>
              </div>
              <div className="button-row">
                <button
                  className="small-button"
                  onClick={() => void runPreview()}
                  disabled={preview.busy}
                >
                  <Eye size={13} />
                  {preview.busy ? "预览中…" : "预览数据"}
                </button>
                <button
                  className="small-button"
                  onClick={() =>
                    goTo("sql", previewSql(source, schema, table), source.id)
                  }
                >
                  <ArrowRight size={13} />在 SQL 工作台查询
                </button>
                {source.type !== "iceberg" && (
                  <button
                    className="small-button"
                    onClick={() => void registerTable()}
                    disabled={register.busy}
                  >
                    <Upload size={13} />
                    {register.busy ? "注册中…" : "注册到 Polaris"}
                  </button>
                )}
              </div>
            </div>
            {register.error && <ErrorBanner message={register.error} />}
            {register.result && (
              <div className="success-banner" role="status">
                已注册为 Polaris Generic Table：{register.result.catalog} /{" "}
                {register.result.namespace.join(".")} / {register.result.name}
                {register.result.format ? `（${register.result.format}）` : ""}
              </div>
            )}
            {detail.loading && <Loading text="读取表结构…" />}
            {detail.error && <ErrorBanner message={detail.error} />}
            {detail.data && (
              <>
                <h3 className="browser-subheading">
                  <Columns3 size={14} />
                  字段
                  <span>{detail.data.columns.length} 个</span>
                </h3>
                <DataGrid
                  columns={["字段", "类型", "可空", "主键", "说明"]}
                  rows={detail.data.columns.map((column) => [
                    column.name,
                    column.type,
                    column.nullable === undefined
                      ? "—"
                      : column.nullable
                        ? "是"
                        : "否",
                    column.primary_key ? "是" : "",
                    column.comment ?? "",
                  ])}
                />
                {detail.data.properties &&
                  Object.keys(detail.data.properties).length > 0 && (
                    <details className="response-headers">
                      <summary>表属性</summary>
                      <pre>
                        {JSON.stringify(detail.data.properties, null, 2)}
                      </pre>
                    </details>
                  )}
              </>
            )}
            {preview.busy && <Loading text="正在预览数据…" />}
            {preview.error && <ErrorBanner message={preview.error} />}
            {preview.result && (
              <div className="browser-preview">
                <h3 className="browser-subheading">
                  <Eye size={14} />
                  数据预览
                  <span>
                    {preview.result.rows.length} 条 ·{" "}
                    {preview.result.elapsed_ms.toLocaleString()} ms
                  </span>
                </h3>
                <DataGrid
                  columns={preview.result.columns}
                  rows={preview.result.rows}
                />
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
