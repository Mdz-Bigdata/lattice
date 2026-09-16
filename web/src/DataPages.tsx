// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from "react";
import {
  ArrowRight,
  Database,
  FileCode2,
  Gauge,
  Layers,
  Play,
  Plug,
  RefreshCw,
  ShieldCheck,
  Table2,
  Trash2,
} from "lucide-react";
import {
  request,
  errorMessage,
  quoteIdentifier,
  previewSql,
  datasourcePath,
  formatAge,
  LOCAL_SAMPLE_ID,
  type Bootstrap,
  type BootstrapSource,
  type DataSource,
  type DataTable,
  type CacheEntry,
  type ConsoleContext,
  type CacheStats,
  type Health,
  type QueryResult,
  type SourceType,
} from "./api";
import { DataGrid, Drawer, Empty, ErrorBanner, Loading, PageTitle } from "./components";
import { QueryReport } from "./QueryPage";
import {
  SchemaTablePicker,
  SourceBrowser,
  SourceSelect,
  StatusBadge,
} from "./SourceBrowser";

export interface DataPageProps {
  bootstrap: Bootstrap;
  health: Health | null;
  goTo: (page: string, sql?: string, sourceId?: string) => void;
  /** Open the API console, optionally prefilling the request from a context. */
  explore: (search: string, context?: ConsoleContext) => void;
  /** Registered data sources from GET /api/datasources (empty until loaded or on failure). */
  sources: DataSource[];
  sourceTypes: SourceType[];
  sourcesError: string;
  /** Data source currently selected across pages. */
  sourceId: string;
  setSourceId: (id: string) => void;
  reloadSources: () => Promise<void>;
}
export { default as SourcesPage } from "./SourcesPage";
export { default as IngestionPage } from "./IngestionPage";
export { default as SemanticPage } from "./SemanticPage";

export function OverviewPage({
  bootstrap,
  health,
  goTo,
  sources,
}: DataPageProps) {
  const rowCount = bootstrap.tables.reduce(
    (sum, table) => sum + Number(table.rows),
    0,
  );
  const columnCount = bootstrap.tables.reduce(
    (sum, table) => sum + table.columns.length,
    0,
  );
  const summary = bootstrap.datasources;
  const items: BootstrapSource[] =
    summary?.items ??
    sources.map((source) => ({
      id: source.id,
      name: source.name,
      type: source.type,
      type_label: source.type_label,
      status: source.last_test?.status ?? "untested",
    }));
  const total = summary?.count ?? items.length;
  const online =
    summary?.online ?? items.filter((item) => item.status === "online").length;
  return (
    <div className="page-content">
      <PageTitle
        title="指标总览"
        description="本地工作区的数据、数据源与服务状态"
        action={<span className="source-badge">示例数据工作区</span>}
      />
      <div className="metric-grid metric-grid-5">
        {[
          { label: "业务数据表", value: bootstrap.tables.length, icon: Table2 },
          {
            label: "数据记录",
            value: rowCount.toLocaleString(),
            icon: Database,
          },
          { label: "字段总数", value: columnCount, icon: Layers },
          { label: "数据源", value: `${online}/${total} 在线`, icon: Plug },
          {
            label: "Polaris 服务",
            value: polarisStatus(health),
            icon: ShieldCheck,
          },
        ].map((item) => (
          <div className="metric-card" key={item.label}>
            <div>
              <span>{item.label}</span>
              <strong>{item.value}</strong>
            </div>
            <item.icon size={25} />
          </div>
        ))}
      </div>
      <section className="panel">
        <div className="panel-heading">
          <h2>数据源</h2>
          <button className="text-button" onClick={() => goTo("sources")}>
            管理数据源 <ArrowRight size={14} />
          </button>
        </div>
        {items.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>ID</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.name}</td>
                    <td>
                      <code>{item.id}</code>
                    </td>
                    <td>{item.type_label}</td>
                    <td>
                      <StatusBadge status={item.status} />
                    </td>
                    <td>
                      <div className="button-row">
                        <button
                          className="text-button"
                          onClick={() => goTo("map", undefined, item.id)}
                        >
                          浏览数据
                        </button>
                        <button
                          className="text-button"
                          onClick={() => goTo("sql", undefined, item.id)}
                        >
                          SQL 查询 <ArrowRight size={12} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="暂无数据源信息">
            数据源列表尚未加载，或后端未提供数据源摘要。
          </Empty>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>业务数据</h2>
          <button className="text-button" onClick={() => goTo("map")}>
            查看数据地图 <ArrowRight size={14} />
          </button>
        </div>
        <TableCatalog tables={bootstrap.tables} goTo={goTo} />
      </section>
      <div className="quick-actions">
        <button onClick={() => goTo("questions")}>
          <FileCode2 size={21} />
          <span>
            <strong>智能问数</strong>
            <small>自然语言生成 SQL，查看图表与数据</small>
          </span>
          <ArrowRight size={17} />
        </button>
        <button onClick={() => goTo("catalogs")}>
          <Layers size={21} />
          <span>
            <strong>Apache Polaris</strong>
            <small>管理 Catalog、身份、权限及 Iceberg API</small>
          </span>
          <ArrowRight size={17} />
        </button>
      </div>
    </div>
  );
}
function polarisStatus(health: Health | null) {
  const status = health?.services?.polaris?.status;
  return status === "online" ||
    status === "ok" ||
    status === "ready" ||
    status === "healthy" ||
    status === "up"
    ? "已连接"
    : status === "disabled"
      ? "未启用"
      : (status ?? "检测中");
}
function TableCatalog({
  tables,
  goTo,
}: {
  tables: DataTable[];
  goTo: DataPageProps["goTo"];
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>表名</th>
            <th>说明</th>
            <th>字段数</th>
            <th>数据行数</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {tables.map((table) => (
            <tr key={table.name}>
              <td>
                <span className="table-name">
                  <Table2 size={14} />
                  {table.name}
                </span>
              </td>
              <td>{table.label}</td>
              <td>{table.columns.length}</td>
              <td>{Number(table.rows).toLocaleString()}</td>
              <td>
                <button
                  className="text-button"
                  onClick={() =>
                    goTo(
                      "sql",
                      `SELECT * FROM ${quoteIdentifier(table.name)} LIMIT 100;`,
                      LOCAL_SAMPLE_ID,
                    )
                  }
                >
                  查询数据 <ArrowRight size={12} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function MapPage({
  bootstrap,
  goTo,
  sources,
  sourceId,
  setSourceId,
}: DataPageProps) {
  const [filter, setFilter] = useState("");
  const source = sources.find((item) => item.id === sourceId);
  const remote = source && source.id !== LOCAL_SAMPLE_ID ? source : undefined;
  const tables = bootstrap.tables.filter((table) =>
    `${table.name} ${table.label} ${table.columns.map((column) => column.name).join(" ")}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  return (
    <div className="page-content">
      <PageTitle
        title="数据地图"
        description={
          remote
            ? `浏览「${remote.name}」的 Schema、数据表与字段元数据`
            : "浏览实际注册的数据表及字段元数据"
        }
        action={
          <div className="form-row">
            <SourceSelect
              id="map-source"
              sources={sources}
              value={sourceId}
              onChange={setSourceId}
            />
            {!remote && (
              <input
                className="search-input"
                aria-label="搜索数据表或字段"
                placeholder="搜索表名、字段…"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              />
            )}
          </div>
        }
      />
      {remote ? (
        <section className="panel">
          <SourceBrowser key={remote.id} source={remote} goTo={goTo} />
        </section>
      ) : (
        <>
          <div className="schema-grid">
            {tables.map((table) => (
              <section className="schema-card" key={table.name}>
                <div className="schema-title">
                  <Database size={18} />
                  <div>
                    <h2>{table.name}</h2>
                    <span>
                      {table.label} · {Number(table.rows).toLocaleString()} 行
                    </span>
                  </div>
                </div>
                <div className="schema-columns">
                  {table.columns.map((column) => (
                    <div key={column.name}>
                      <code>{column.name}</code>
                      <span>{column.type}</span>
                    </div>
                  ))}
                </div>
                <button
                  className="schema-footer text-button"
                  onClick={() =>
                    goTo(
                      "sql",
                      `SELECT * FROM ${quoteIdentifier(table.name)} LIMIT 100;`,
                      LOCAL_SAMPLE_ID,
                    )
                  }
                >
                  预览数据 <ArrowRight size={13} />
                </button>
              </section>
            ))}
          </div>
          {!tables.length && (
            <Empty title="没有匹配的数据表">请尝试其他关键词。</Empty>
          )}
        </>
      )}
    </div>
  );
}
export { default as QualityPage } from "./QualityPage";
export function StandardsPage({ bootstrap }: DataPageProps) {
  return (
    <div className="page-content">
      <PageTitle
        title="标准规范"
        description="当前业务数据字段字典与数据类型定义"
      />
      <section className="panel">
        <DataGrid
          columns={["业务表", "字段", "数据类型"]}
          rows={bootstrap.tables.flatMap((table) =>
            table.columns.map((column) => [
              table.name,
              column.name,
              column.type,
            ]),
          )}
        />
      </section>
    </div>
  );
}
export function SqlPage({
  bootstrap,
  initialSql,
  sources,
  sourcesError,
  sourceId,
  setSourceId,
}: DataPageProps & { initialSql: string }) {
  const [selected, setSelected] = useState(sourceId);
  const source = sources.find((item) => item.id === selected);
  const [schema, setSchema] = useState("");
  const [table, setTable] = useState("");
  const [sql, setSql] = useState(
    initialSql ||
      `SELECT * FROM ${quoteIdentifier(bootstrap.tables[0]?.name ?? "t_lattice_orders")} LIMIT 100;`,
  );
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cacheOpen, setCacheOpen] = useState(false);
  function changeSource(id: string) {
    setSelected(id);
    setSourceId(id);
    setSchema("");
    setTable("");
  }
  function insertTable(name: string) {
    setTable(name);
    if (name) setSql(previewSql(source, schema, name));
  }
  async function run(refresh = false) {
    if (busy || !sql.trim()) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(
        await request<QueryResult>(datasourcePath(selected, "/query"), {
          sql,
          refresh,
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="page-content sql-page">
      <PageTitle
        title="SQL 工作台"
        description="在所选数据源上执行只读查询（最多 1000 行），支持 Ctrl / ⌘ + Enter"
        action={
          <div className="button-row">
            <span className="source-badge">
              <Database size={12} />
              {source ? source.name : selected}
            </span>
            <span className="neutral-badge">
              方言 {source?.dialect ?? "duckdb"}
            </span>
          </div>
        }
      />
      {sourcesError && (
        <ErrorBanner message={`数据源列表不可用：${sourcesError}`} />
      )}
      <section className="panel">
        <div className="sql-editor-toolbar">
          <SourceSelect
            id="sql-source"
            sources={sources}
            value={selected}
            disabled={busy}
            onChange={changeSource}
          />
          <SchemaTablePicker
            idPrefix="sql"
            source={source}
            schema={schema}
            table={table}
            autoSelectTable={false}
            tablePlaceholder="选择一张表插入查询"
            disabled={busy}
            onSchema={(value) => {
              setSchema(value);
              setTable("");
            }}
            onTable={insertTable}
          />
          <button
            className="primary-button"
            disabled={busy || !sql.trim()}
            onClick={() => void run()}
          >
            <Play size={14} />
            {busy ? "运行中…" : "运行 SQL"}
          </button>
          <button
            type="button"
            className="secondary-button"
            title="查看与管理查询结果缓存"
            onClick={() => setCacheOpen(true)}
          >
            <Gauge size={14} />
            查询缓存
          </button>
        </div>
        <textarea
          spellCheck={false}
          aria-label="SQL 查询语句"
          className="code-editor sql-editor"
          value={sql}
          onChange={(event) => setSql(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void run();
            }
          }}
        />
      </section>
      {error && <ErrorBanner message={error} />}
      {busy && <Loading text="正在执行查询…" />}
      {result && (
        <QueryReport
          key={result.id}
          result={result}
          defaultMode="table"
          onRefresh={() => void run(true)}
        />
      )}
      {cacheOpen && (
        <CachePanel
          sourceId={selected}
          sources={sources}
          onClose={() => setCacheOpen(false)}
        />
      )}
    </div>
  );
}

/** 查询缓存: hit statistics, the live entries of one source or all, targeted clearing and TTL. */
function CachePanel({
  sourceId,
  sources,
  onClose,
}: {
  sourceId: string;
  sources: DataSource[];
  onClose: () => void;
}) {
  const [scope, setScope] = useState<"source" | "all">("source");
  const [data, setData] = useState<{ stats: CacheStats; items: CacheEntry[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [ttl, setTtl] = useState(600);
  const [dirty, setDirty] = useState(false);
  const names = new Map(sources.map((item) => [item.id, item.name]));
  async function load() {
    setBusy(true);
    setError("");
    try {
      const query = scope === "source" ? `?datasource_id=${encodeURIComponent(sourceId)}&limit=100` : "?limit=100";
      const next = await request<{ stats: CacheStats; items: CacheEntry[] }>(`/api/query/cache${query}`);
      setData(next);
      if (!dirty) {
        setEnabled(next.stats.enabled);
        setTtl(next.stats.ttl_seconds);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, sourceId]);
  async function act(body: Record<string, unknown>, done: (removed: number) => string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const outcome = await request<{ removed: number }>("/api/query/cache/invalidate", body);
      setNotice(done(outcome.removed));
      await load();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  async function saveSettings() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await request<CacheStats>("/api/query/cache/settings", { enabled, ttl_seconds: ttl });
      setDirty(false);
      setNotice(enabled ? `已保存：缓存开启，有效期 ${ttl} 秒。` : "已保存：缓存已关闭，现有结果已清空。");
      await load();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  const stats = data?.stats;
  const items = data?.items ?? [];
  return (
    <Drawer id="query-cache-panel" title="查询结果缓存" icon={<Gauge size={17} />} onClose={onClose} wide>
      <div className="cache-panel">
        <p className="muted">
          智能问数、SQL 工作台与报表的只读查询结果按数据源、语句与行数上限缓存；
          到期、超出条目上限、数据源配置变更或手动清理时失效，任何查询都可以“刷新”跳过缓存。
        </p>
        {error && <ErrorBanner message={error} />}
        {notice && (
          <div className="info-banner" role="status">
            {notice}
          </div>
        )}
        {stats && (
          <div className="cache-stats">
            <div>
              <b>{stats.entries}</b>
              <span>缓存条目 / 上限 {stats.max_entries}</span>
            </div>
            <div>
              <b>{Math.round(stats.hit_rate * 100)}%</b>
              <span>
                命中率（命中 {stats.hits} · 未命中 {stats.misses}）
              </span>
            </div>
            <div>
              <b>{stats.rows.toLocaleString()}</b>
              <span>缓存行数 / 单条上限 {stats.max_rows.toLocaleString()}</span>
            </div>
            <div>
              <b>{stats.evictions + stats.invalidations}</b>
              <span>
                淘汰 {stats.evictions} · 失效 {stats.invalidations}
              </span>
            </div>
          </div>
        )}
        <div className="cache-settings">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => {
                setEnabled(event.target.checked);
                setDirty(true);
              }}
            />
            启用查询结果缓存
          </label>
          <label htmlFor="query-cache-ttl">有效期（秒）</label>
          <input
            id="query-cache-ttl"
            type="number"
            min={10}
            max={604800}
            value={ttl}
            disabled={!enabled}
            onChange={(event) => {
              setTtl(Math.max(10, Math.min(604800, Number(event.target.value) || 600)));
              setDirty(true);
            }}
          />
          <button type="button" className="secondary-button" disabled={busy || !dirty} onClick={() => void saveSettings()}>
            保存设置
          </button>
        </div>
        <div className="cache-toolbar">
          <div className="segmented-control" role="group" aria-label="缓存范围">
            <button type="button" className={scope === "source" ? "selected" : ""} onClick={() => setScope("source")}>
              当前数据源
            </button>
            <button type="button" className={scope === "all" ? "selected" : ""} onClick={() => setScope("all")}>
              全部数据源
            </button>
          </div>
          <button type="button" className="secondary-button" disabled={busy} onClick={() => void load()}>
            <RefreshCw size={14} className={busy ? "spin" : ""} />
            刷新
          </button>
          <button
            type="button"
            className="secondary-button"
            disabled={busy || !items.length}
            onClick={() =>
              void act(
                scope === "source" ? { datasource_id: sourceId } : {},
                (removed) => `已清空 ${removed} 条缓存结果。`,
              )
            }
          >
            <Trash2 size={14} />
            {scope === "source" ? "清空当前数据源" : "清空全部"}
          </button>
        </div>
        {busy && !data ? (
          <Loading text="正在读取缓存…" />
        ) : items.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {scope === "all" && <th>数据源</th>}
                  <th>SQL</th>
                  <th>涉及表</th>
                  <th>行数</th>
                  <th>命中</th>
                  <th>存入</th>
                  <th>剩余</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.key}>
                    {scope === "all" && <td>{names.get(item.datasource_id) ?? item.datasource_id}</td>}
                    <td className="cache-sql">
                      <code title={item.sql}>{item.sql}</code>
                    </td>
                    <td>{item.tables.join("、") || "—"}</td>
                    <td>{item.rows.toLocaleString()}</td>
                    <td>{item.hits}</td>
                    <td>{formatAge(item.age_seconds)}</td>
                    <td>{item.expires_in_seconds} 秒</td>
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        disabled={busy}
                        onClick={() => void act({ key: item.key }, () => "已删除该缓存结果。")}
                      >
                        <Trash2 size={13} />
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="没有缓存的查询结果">
            {stats?.enabled ? "执行一次查询后，相同的语句在有效期内会直接返回缓存结果。" : "缓存已关闭。"}
          </Empty>
        )}
      </div>
    </Drawer>
  );
}
const localApis = [
  { method: "GET", path: "/api/health", text: "服务健康状态" },
  {
    method: "GET",
    path: "/api/bootstrap",
    text: "数据表、字段、数据源摘要、模型状态与链路地址",
  },
  { method: "GET", path: "/api/datasources", text: "数据源列表与可用类型定义" },
  { method: "POST", path: "/api/datasources", text: "新增数据源" },
  { method: "POST", path: "/api/datasources/test", text: "测试连接配置" },
  { method: "POST", path: "/api/datasources/{id}/update", text: "更新数据源" },
  {
    method: "POST",
    path: "/api/datasources/{id}/delete",
    text: "删除数据源（内置数据源除外）",
  },
  {
    method: "POST",
    path: "/api/datasources/{id}/test",
    text: "测试已注册数据源",
  },
  { method: "GET", path: "/api/datasources/{id}/schemas", text: "Schema 列表" },
  {
    method: "GET",
    path: "/api/datasources/{id}/tables",
    text: "数据表列表（schema 参数）",
  },
  { method: "GET", path: "/api/datasources/{id}/table", text: "表结构与字段" },
  { method: "POST", path: "/api/datasources/{id}/preview", text: "预览表数据" },
  {
    method: "POST",
    path: "/api/datasources/{id}/query",
    text: "在指定数据源执行只读 SQL",
  },
  { method: "POST", path: "/api/query", text: "自然语言或 SQL 查询" },
  {
    method: "POST",
    path: "/api/query/stream",
    text: "自然语言查询（SSE 流式输出思考过程）",
  },
  { method: "GET", path: "/api/history", text: "查询历史" },
  { method: "GET", path: "/api/llm", text: "模型配置状态" },
  { method: "POST", path: "/api/llm", text: "保存模型配置" },
  { method: "POST", path: "/api/llm/test", text: "测试模型连接" },
  {
    method: "GET",
    path: "/api/ingest/catalog",
    text: "Polaris Catalog 中的表（catalog 参数）",
  },
  {
    method: "POST",
    path: "/api/ingest/register",
    text: "注册外部表为 Polaris Generic Table",
  },
  {
    method: "POST",
    path: "/api/ingest/unregister",
    text: "取消注册 Generic Table",
  },
  { method: "POST", path: "/api/validate", text: "校验语义模型 YAML" },
];
export function ServicesPage({ goTo }: DataPageProps) {
  return (
    <div className="page-content">
      <PageTitle
        title="数据服务"
        description="本地数据 API 与 Apache Polaris 官方 API"
      />
      <section className="panel">
        <div className="panel-heading">
          <h2>本地数据 API</h2>
          <span>{localApis.length} 个接口</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>方法</th>
                <th>接口</th>
                <th>用途</th>
              </tr>
            </thead>
            <tbody>
              {localApis.map((item) => (
                <tr key={`${item.method} ${item.path}`}>
                  <td>
                    <span
                      className={`method-tag method-${item.method.toLowerCase()}`}
                    >
                      {item.method}
                    </span>
                  </td>
                  <td>
                    <code>
                      {item.method === "GET" && !item.path.includes("{") ? (
                        <a href={item.path} target="_blank" rel="noreferrer">
                          {item.path}
                        </a>
                      ) : (
                        item.path
                      )}
                    </code>
                  </td>
                  <td>{item.text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Apache Polaris API</h2>
        </div>
        <div className="panel-body">
          <p>
            通过完整 OpenAPI 操作目录调用 Management、Iceberg REST
            和其他已注册接口。API 响应直接来自本地 Polaris 服务。
          </p>
          <button className="primary-button" onClick={() => goTo("explorer")}>
            浏览全部 Polaris API <ArrowRight size={14} />
          </button>
        </div>
      </section>
    </div>
  );
}
