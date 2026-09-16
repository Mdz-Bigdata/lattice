// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据拾取 (OpenMetadata's Settings): services by category with the 添加新服务 wizard, manual asset
 * registration for connectors that cannot be crawled, teams and users, ingestion run history, and
 * import/export of Lattice and OpenMetadata bundles.
 */
import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Download,
  History,
  LayoutGrid,
  List,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
  Users,
} from "lucide-react";
import { datasourcePath, errorMessage, request, type SourceType, type TestResult } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { SourceStatusBadge, useApi } from "./SourceBrowser";
import {
  CHILD_TYPES,
  COLUMN_TYPES,
  RUN_STATUS_LABELS,
  entityName,
  formatDuration,
  formatTime,
  mdGet,
  mdPost,
  metadataPath,
  navigate,
  openEntity,
  timeAgo,
  typeLabel,
  type Entity,
  type IngestionRun,
  type IngestionService,
  type Paged,
  type ServiceCategory,
  type TypesResponse,
} from "./metadataApi";
import {
  Description,
  EntityIcon,
  EntityLink,
  FormRow,
  MarkdownEditor,
  Modal,
  Owners,
  Pager,
  PeoplePickerModal,
  RunStatus,
  SearchBox,
  Toggle,
} from "./MetadataWidgets";
import { PeopleField } from "./MetadataGovernance";
import type { MetadataViewProps } from "./MetadataPage";

const CATEGORY_TITLES: Record<string, string> = {
  database: "Database Services",
  messaging: "Messaging Services",
  dashboard: "Dashboard Services",
  pipeline: "Pipeline Services",
  mlmodel: "Mlmodel Services",
  storage: "Storage Services",
  search: "Search Services",
  metadata: "Metadata Services",
  api: "API Services",
};
const INTERVALS: [number, string][] = [
  [0, "仅手动"],
  [60, "每小时"],
  [360, "每 6 小时"],
  [1440, "每天"],
  [10080, "每周"],
];

export default function SettingsPage(props: MetadataViewProps) {
  const [section, category, action] = props.route.parts;
  if (section === "services" && category && action === "new") return <ServiceWizard {...props} category={category} />;
  if (section === "services" && category) return <ServiceList category={category} />;
  if (section === "services") return <CategoryCards />;
  if (section === "teams") return <TeamsView />;
  if (section === "runs") return <RunsView />;
  if (section === "import") return <ImportExportView />;
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>元数据拾取</h1>
          <p>能够根据需要配置 Lattice 元数据目录</p>
        </div>
      </div>
      <div className="md-card-grid">
        {[
          ["services", "服务", "设置连接器并从不同来源提取元数据", "databaseService"],
          ["teams", "团队和用户管理", "简化对元数据目录用户和团队的访问", "team"],
          ["runs", "拾取记录", "查看每次拾取的结果、耗时与错误", "pipeline"],
          ["import", "导入导出", "导出目录，或导入 Lattice / OpenMetadata 的 JSON", "metadataService"],
        ].map(([id, title, text, icon]) => (
          <button type="button" key={id} className="md-card" onClick={() => navigate("settings", id)}>
            <span className="md-card-icon">
              <EntityIcon type={icon} size={26} />
            </span>
            <strong>{title}</strong>
            <small>{text}</small>
          </button>
        ))}
      </div>
    </div>
  );
}

function Crumbs({ items }: { items: [string, (() => void) | null][] }) {
  return (
    <div className="md-breadcrumbs">
      {items.map(([label, onClick], index) => (
        <span key={label}>
          {onClick ? (
            <button type="button" className="md-link" onClick={onClick}>
              {label}
            </button>
          ) : (
            <b>{label}</b>
          )}
          {index < items.length - 1 && <i>/</i>}
        </span>
      ))}
    </div>
  );
}

function CategoryCards() {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const services = useApi<{ items: IngestionService[] }>(metadataPath("/ingestion/services"));
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["服务", null]]} />
      <div className="md-page-head">
        <div>
          <h1>服务</h1>
          <p>设置连接器并从不同来源提取元数据</p>
        </div>
      </div>
      {types.error && <ErrorBanner message={types.error} />}
      <div className="md-card-grid">
        {(types.data?.service_categories ?? []).map((category) => {
          const total = (services.data?.items ?? []).filter((item) => item.entity_type === category.entity_type).length;
          return (
            <button type="button" key={category.id} className="md-card" onClick={() => navigate("settings", "services", category.id)}>
              <span className="md-card-icon">
                <EntityIcon type={category.entity_type} size={26} />
              </span>
              <strong>
                {category.label}
                {category.id === "api" && <span className="source-badge">Beta</span>}
              </strong>
              <small>{category.description}</small>
              <small>{total} 个服务</small>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ServiceList({ category }: { category: string }) {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const services = useApi<{ items: IngestionService[] }>(metadataPath("/ingestion/services"));
  const [query, setQuery] = useState("");
  const [deleted, setDeleted] = useState(false);
  const [grid, setGrid] = useState(false);
  const [page, setPage] = useState(1);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [asset, setAsset] = useState<IngestionService | null>(null);
  const deletedList = useApi<Paged<Entity>>(deleted ? metadataPath("/entities", { entity_type: types.data?.service_categories.find((item) => item.id === category)?.entity_type ?? "databaseService", deleted: true, size: 200 }) : null);
  const spec = types.data?.service_categories.find((item) => item.id === category);
  const running = (services.data?.items ?? []).some((item) => item.running);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(services.reload, 3000);
    return () => window.clearInterval(timer);
  }, [running]);
  const size = 15;
  const q = query.trim().toLowerCase();
  const rows: Entity[] = deleted
    ? deletedList.data?.items ?? []
    : (services.data?.items ?? []).filter((item) => item.entity_type === spec?.entity_type);
  const filtered = rows.filter((item) => !q || item.fqn.toLowerCase().includes(q) || item.display_name.toLowerCase().includes(q) || item.service_type.toLowerCase().includes(q));
  const shown = filtered.slice((page - 1) * size, page * size);
  async function act(action: () => Promise<unknown>, done: string) {
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(done);
      services.reload();
      deletedList.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["服务", () => navigate("settings", "services")], [CATEGORY_TITLES[category] ?? category, null]]} />
      <nav className="quality-tabs md-tabs">
        <button type="button" className="quality-tab active">
          Services
        </button>
      </nav>
      <div className="md-page-head">
        <div>
          <h1>{spec?.label ?? category}</h1>
          <p>{spec?.description}</p>
        </div>
        <div className="md-actions">
          {category === "database" && (
            <>
              <button type="button" className="secondary-button" onClick={() => void act(() => mdPost("/ingestion/sync-datasources", { force: true }), "已按平台数据源同步数据库服务")}>
                <RefreshCw size={13} />
                同步平台数据源
              </button>
              <button type="button" className="secondary-button" onClick={() => void act(() => mdPost("/ingestion/run-all", {}), "已开始拾取全部可自动拾取的服务")}>
                <Play size={13} />
                全部拾取
              </button>
            </>
          )}
          <button type="button" className="primary-button" onClick={() => navigate("settings", "services", category, "new")}>
            添加新服务
          </button>
        </div>
      </div>
      {message && <div className="success-banner">{message}</div>}
      {error && <ErrorBanner message={error} />}
      <div className="md-list-tools">
        <div className="md-list-search">
          <SearchBox value={query} onChange={(value) => { setQuery(value); setPage(1); }} placeholder="搜索服务" />
        </div>
        <Toggle checked={deleted} onChange={(value) => { setDeleted(value); setPage(1); }} label="已删除" />
        <div className="md-view-toggle">
          <button type="button" className={grid ? "active" : ""} aria-label="卡片视图" onClick={() => setGrid(true)}>
            <LayoutGrid size={14} />
          </button>
          <button type="button" className={!grid ? "active" : ""} aria-label="列表视图" onClick={() => setGrid(false)}>
            <List size={14} />
          </button>
        </div>
      </div>
      <section className="panel">
        {(services.loading || deletedList.loading) && !rows.length && <Loading />}
        {services.error && <ErrorBanner message={services.error} />}
        {!shown.length && !services.loading && <Empty title={deleted ? "没有已删除的服务" : "还没有服务"}>{deleted ? "" : "点击「添加新服务」登记一个服务。"}</Empty>}
        {shown.length > 0 && grid && (
          <div className="md-card-grid panel-body">
            {shown.map((item) => (
              <button type="button" key={item.id} className="md-card md-service-card" onClick={() => openEntity(item.entity_type, item.fqn)}>
                <strong>{entityName(item)}</strong>
                <small>{item.service_type}</small>
                <small>{item.description ? item.description.slice(0, 80) : "无描述"}</small>
              </button>
            ))}
          </div>
        )}
        {shown.length > 0 && !grid && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>描述</th>
                  <th>类型</th>
                  <th>所有者</th>
                  {!deleted && <th>拾取</th>}
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((item) => {
                  const service = item as IngestionService;
                  return (
                    <tr key={item.id}>
                      <td>
                        <span className="md-service-name">
                          <EntityIcon type={item.entity_type} size={15} />
                          <EntityLink entity={item}>{item.name}</EntityLink>
                        </span>
                      </td>
                      <td className="md-cell-desc">{item.description ? <Description text={item.description} /> : "无描述"}</td>
                      <td>{item.service_type}</td>
                      <td>
                        <Owners owners={item.owners} />
                      </td>
                      {!deleted && (
                        <td className="md-run-cell">
                          {service.automated ? (
                            <>
                              {service.running ? <span className="neutral-badge">拾取中…</span> : <RunStatus status={service.last_run?.status} />}
                              <small>
                                {service.ingestion.enabled && service.ingestion.interval_minutes ? INTERVALS.find(([minutes]) => minutes === service.ingestion.interval_minutes)?.[1] ?? `每 ${service.ingestion.interval_minutes} 分钟` : "仅手动"}
                                {service.last_run ? ` · ${timeAgo(service.last_run.finished_at ?? service.last_run.started_at)} · ${service.asset_count} 个资产` : ""}
                              </small>
                              {service.datasource && <SourceStatusBadge test={{ status: service.datasource.status as TestResult["status"], ok: service.datasource.status === "online", latency_ms: 0, detail: service.datasource.name, tested_at: "" }} />}
                            </>
                          ) : (
                            <small>手工登记 · {service.asset_count} 个资产</small>
                          )}
                        </td>
                      )}
                      <td>
                        <div className="md-actions">
                          {deleted ? (
                            <button type="button" className="text-button" onClick={() => void act(() => mdPost("/entities/restore", { entity_type: item.entity_type, ref: item.fqn }), "已恢复")}>
                              恢复
                            </button>
                          ) : (
                            <>
                              {service.automated ? (
                                <>
                                  <button type="button" className="text-button" disabled={service.running} onClick={() => void act(() => mdPost("/ingestion/run", { service_fqn: item.fqn }), `已开始拾取 ${item.name}`)}>
                                    <Play size={12} />
                                    拾取
                                  </button>
                                  <ScheduleSelect service={service} onSaved={services.reload} onError={setError} />
                                </>
                              ) : (
                                <button type="button" className="text-button" onClick={() => setAsset(service)}>
                                  <Plus size={12} />
                                  登记资产
                                </button>
                              )}
                              <button
                                type="button"
                                className="text-button danger-text"
                                onClick={() => window.confirm(`删除服务 ${item.name} 及其全部资产？删除后可在「已删除」中恢复。`) && void act(() => mdPost("/entities/delete", { entity_type: item.entity_type, ref: item.fqn }), "已删除")}
                              >
                                <Trash2 size={12} />
                                删除
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <Pager total={filtered.length} page={page} size={size} onPage={setPage} />
      </section>
      {asset && spec && (
        <AssetDialog
          service={asset}
          category={spec}
          onClose={() => setAsset(null)}
          onDone={(created) => {
            setAsset(null);
            openEntity(created.entity_type, created.fqn);
          }}
        />
      )}
    </div>
  );
}

function ScheduleSelect({ service, onSaved, onError }: { service: IngestionService; onSaved: () => void; onError: (message: string) => void }) {
  const value = service.ingestion.enabled ? service.ingestion.interval_minutes : 0;
  return (
    <select
      className="md-schedule"
      aria-label={`${service.name} 的拾取计划`}
      value={INTERVALS.some(([minutes]) => minutes === value) ? value : 1440}
      onChange={(event) => {
        const minutes = Number(event.target.value);
        mdPost("/ingestion/schedule", { service_fqn: service.fqn, interval_minutes: minutes, enabled: minutes > 0 }).then(onSaved, (e: unknown) => onError(errorMessage(e)));
      }}
    >
      {INTERVALS.map(([minutes, label]) => (
        <option key={minutes} value={minutes}>
          {label}
        </option>
      ))}
    </select>
  );
}

function descendantTypes(category: ServiceCategory): string[] {
  const result: string[] = [];
  const visit = (type: string) => {
    for (const child of CHILD_TYPES[type] ?? []) {
      if (!result.includes(child)) {
        result.push(child);
        visit(child);
      }
    }
  };
  visit(category.entity_type);
  return result;
}

function AssetDialog({ service, category, onClose, onDone }: { service: IngestionService; category: ServiceCategory; onClose: () => void; onDone: (entity: Entity) => void }) {
  const options = descendantTypes(category);
  const [entityType, setEntityType] = useState(options[0] ?? "");
  const [parent, setParent] = useState(service.fqn);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [lines, setLines] = useState("");
  const [error, setError] = useState("");
  const listField = entityType === "pipeline" ? "tasks" : entityType === "dashboard" ? "charts" : COLUMN_TYPES.has(entityType) ? "columns" : "";
  async function save() {
    setError("");
    const rows = lines.split("\n").map((line) => line.trim()).filter(Boolean);
    const fields: Record<string, unknown> = {};
    const body: Record<string, unknown> = { entity_type: entityType, name: name.trim(), parent_fqn: parent.trim(), display_name: displayName, description };
    if (sourceUrl.trim()) fields.source_url = sourceUrl.trim();
    if (listField === "columns") body.columns = rows.map((line) => {
      const [columnName, type = "", ...rest] = line.split(/\s+/);
      return { name: columnName, type, description: rest.join(" ") };
    });
    if (listField === "tasks") fields.tasks = rows.map((line) => ({ name: line }));
    if (listField === "charts") fields.charts = rows;
    try {
      onDone(await mdPost<Entity>("/entities", { ...body, fields }));
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Modal
      title={`在 ${service.name} 下登记资产`}
      width={680}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={!name.trim() || !entityType} onClick={() => void save()}>
            保存
          </button>
        </>
      }
    >
      <div className="md-form-inline">
        <FormRow label="类型" htmlFor="asset-type" required>
          <select id="asset-type" value={entityType} onChange={(event) => setEntityType(event.target.value)}>
            {options.map((type) => (
              <option key={type} value={type}>
                {typeLabel(type)}
              </option>
            ))}
          </select>
        </FormRow>
        <FormRow label="上级对象" htmlFor="asset-parent" required help="服务或上级资产的完整名称">
          <input id="asset-parent" value={parent} onChange={(event) => setParent(event.target.value)} />
        </FormRow>
      </div>
      <div className="md-form-inline">
        <FormRow label="名称" htmlFor="asset-name" required>
          <input id="asset-name" value={name} onChange={(event) => setName(event.target.value)} />
        </FormRow>
        <FormRow label="显示名称" htmlFor="asset-display">
          <input id="asset-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
        </FormRow>
      </div>
      <FormRow label="描述">
        <MarkdownEditor value={description} onChange={setDescription} rows={4} />
      </FormRow>
      <FormRow label="来源地址" htmlFor="asset-url" help="例如仪表板或工作流在原系统中的链接">
        <input id="asset-url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://" />
      </FormRow>
      {listField && (
        <FormRow
          label={listField === "columns" ? "字段" : listField === "tasks" ? "任务" : "图表"}
          htmlFor="asset-lines"
          help={listField === "columns" ? "每行一个：字段名 类型 描述" : listField === "tasks" ? "每行一个任务名" : "每行一个图表的完整名称"}
        >
          <textarea id="asset-lines" rows={5} value={lines} onChange={(event) => setLines(event.target.value)} placeholder={listField === "columns" ? "order_id bigint 订单编号\nstatus string 订单状态" : ""} />
        </FormRow>
      )}
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

/* ------------------------------------------------------------------ wizard */

function ServiceWizard({ category, sources, sourceTypes, reloadSources }: MetadataViewProps & { category: string }) {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const [step, setStep] = useState(0);
  const [categoryId, setCategoryId] = useState(category);
  const [connector, setConnector] = useState("");
  const [query, setQuery] = useState("");
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [owners, setOwners] = useState<string[]>([]);
  const [mode, setMode] = useState<"bind" | "create">("bind");
  const [datasourceId, setDatasourceId] = useState("");
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [connection, setConnection] = useState<[string, string][]>([["host", ""], ["port", ""]]);
  const [interval, setInterval] = useState(1440);
  const [runNow, setRunNow] = useState(true);
  const [test, setTest] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const spec = types.data?.service_categories.find((item) => item.id === categoryId);
  const latticeType = Object.entries(types.data?.lattice_connectors ?? {}).find(([, value]) => value === connector)?.[0];
  const sourceType: SourceType | undefined = sourceTypes.find((item) => item.id === latticeType);
  const candidates = sources.filter((item) => item.type === latticeType);
  const automated = categoryId === "database" && !!latticeType;
  const connectors = (spec?.connectors ?? []).filter((item) => !query || item.id.toLowerCase().includes(query.toLowerCase()));
  useEffect(() => {
    setDatasourceId(candidates[0]?.id ?? "");
    setMode(candidates.length ? "bind" : "create");
    const defaults: Record<string, unknown> = {};
    for (const field of sourceType?.fields ?? []) if (field.default !== undefined) defaults[field.name] = field.default;
    setConfig(defaults);
    setTest(null);
  }, [connector, sourceTypes.length]);
  async function testConnection() {
    if (!latticeType) return;
    setTest(null);
    setError("");
    try {
      setTest(await request<TestResult>("/api/datasources/test", { type: latticeType, config }));
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  async function save() {
    setBusy(true);
    setError("");
    // A data source created here is removed again if the service cannot be registered, so a
    // failed attempt never leaves a source behind for the next data-source sync to pick up.
    let createdId = "";
    try {
      if (spec) {
        const taken = await mdGet<Entity>("/entity", { entity_type: spec.entity_type, ref: name.trim() }).then(
          () => true,
          () => false,
        );
        if (taken) throw new Error(`服务 ${name.trim()} 已存在，请换一个名称。`);
      }
      let boundId = "";
      if (automated) {
        if (mode === "create") {
          const created = await request<{ id: string }>("/api/datasources", { name: displayName || name, type: latticeType, config, description: "由元数据拾取的添加新服务向导创建" });
          createdId = created.id;
          boundId = created.id;
        } else boundId = datasourceId;
      }
      const service = await mdPost<Entity>("/ingestion/services", {
        category: categoryId,
        service_type: connector,
        name: name.trim(),
        display_name: displayName,
        description,
        owners,
        ...(boundId ? { datasource_id: boundId, interval_minutes: interval, schedule_enabled: interval > 0, run_now: runNow } : {}),
        connection: automated ? {} : Object.fromEntries(connection.filter(([key, value]) => key.trim() && value.trim())),
      });
      if (createdId) await reloadSources();
      openEntity(service.entity_type, service.fqn, "details");
    } catch (e) {
      if (createdId) await request(datasourcePath(createdId, "/delete"), {}).catch(() => undefined);
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  const steps = ["选择服务类型", "配置服务", "连接详情"];
  const canNext = step === 0 ? !!connector && connector !== "Polaris" : step === 1 ? !!name.trim() : true;
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["服务", () => navigate("settings", "services")], [CATEGORY_TITLES[categoryId] ?? categoryId, () => navigate("settings", "services", categoryId)], ["添加新服务", null]]} />
      <div className="md-wizard">
        <h1>添加新服务</h1>
        <ol className="md-steps">
          {steps.map((label, index) => (
            <li key={label} className={index === step ? "active" : index < step ? "done" : ""}>
              <i>{index < step ? <Check size={12} /> : null}</i>
              <span>{label}</span>
            </li>
          ))}
        </ol>
        {types.loading && <Loading />}
        {step === 0 && spec && (
          <>
            <select className="md-wizard-select" value={categoryId} onChange={(event) => { setCategoryId(event.target.value); setConnector(""); }} aria-label="服务类别">
              {(types.data?.service_categories ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {CATEGORY_TITLES[item.id] ?? item.label}
                </option>
              ))}
            </select>
            <SearchBox value={query} onChange={setQuery} placeholder="搜索连接器" />
            <div className="md-connectors">
              {connectors.map((item) => (
                <button type="button" key={item.id} className={`md-connector ${connector === item.id ? "selected" : ""}`} onClick={() => setConnector(item.id)}>
                  <span className="md-connector-mark">{item.id.replace(/^Custom/, "").slice(0, 2)}</span>
                  <span>{item.id.replace(/([a-z])([A-Z])/g, "$1 $2")}</span>
                  {item.automated && <small className="healthy-badge">可自动拾取</small>}
                </button>
              ))}
            </div>
            {connector === "Polaris" && (
              <div className="info-banner">
                <span>本地 Apache Polaris 已作为服务 polaris 自动登记，可在数据库服务列表中直接拾取。</span>
              </div>
            )}
          </>
        )}
        {step === 1 && (
          <div className="md-wizard-form">
            <FormRow label="服务名称" htmlFor="service-name" required help="完整名称的第一段，创建后可重命名">
              <input id="service-name" value={name} onChange={(event) => setName(event.target.value)} placeholder={`例如 ${connector.toLowerCase()}-prod`} />
            </FormRow>
            <FormRow label="显示名称" htmlFor="service-display">
              <input id="service-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
            </FormRow>
            <FormRow label="描述">
              <MarkdownEditor value={description} onChange={setDescription} rows={4} />
            </FormRow>
            <PeopleField label="所有者" value={owners} onChange={setOwners} />
          </div>
        )}
        {step === 2 && (
          <div className="md-wizard-form">
            {automated ? (
              <>
                <div className="md-bind">
                  <label className="md-check">
                    <input type="radio" name="bind" checked={mode === "bind"} disabled={!candidates.length} onChange={() => setMode("bind")} />
                    绑定已有的平台数据源{!candidates.length && "（暂无该类型的数据源）"}
                  </label>
                  <label className="md-check">
                    <input type="radio" name="bind" checked={mode === "create"} onChange={() => setMode("create")} />
                    新建平台数据源
                  </label>
                </div>
                {mode === "bind" ? (
                  <FormRow label="平台数据源" htmlFor="service-datasource" required>
                    <select id="service-datasource" value={datasourceId} onChange={(event) => setDatasourceId(event.target.value)}>
                      {candidates.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name} · {item.summary}
                        </option>
                      ))}
                    </select>
                  </FormRow>
                ) : (
                  <>
                    {(sourceType?.fields ?? []).map((field) => (
                      <FormRow key={field.name} label={field.label} htmlFor={`field-${field.name}`} required={field.required} help={field.help}>
                        {field.type === "checkbox" ? (
                          <input id={`field-${field.name}`} type="checkbox" checked={config[field.name] === true} onChange={(event) => setConfig({ ...config, [field.name]: event.target.checked })} />
                        ) : field.type === "select" ? (
                          <select id={`field-${field.name}`} value={String(config[field.name] ?? "")} onChange={(event) => setConfig({ ...config, [field.name]: event.target.value })}>
                            {(field.options ?? []).map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        ) : field.type === "textarea" ? (
                          <textarea id={`field-${field.name}`} rows={3} value={String(config[field.name] ?? "")} placeholder={field.placeholder} onChange={(event) => setConfig({ ...config, [field.name]: event.target.value })} />
                        ) : (
                          <input
                            id={`field-${field.name}`}
                            type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
                            value={String(config[field.name] ?? "")}
                            placeholder={field.placeholder}
                            onChange={(event) => setConfig({ ...config, [field.name]: field.type === "number" && event.target.value ? Number(event.target.value) : event.target.value })}
                          />
                        )}
                      </FormRow>
                    ))}
                    <div className="md-actions">
                      <button type="button" className="secondary-button" onClick={() => void testConnection()}>
                        测试连接
                      </button>
                      {test && <span className={test.ok ? "healthy-badge" : "failure-badge"}>{test.detail}</span>}
                    </div>
                    <p className="muted">连接信息保存在平台数据源中（仅本机、密钥掩码显示），元数据目录只记录数据源的编号。</p>
                  </>
                )}
                <div className="md-form-inline">
                  <FormRow label="拾取计划" htmlFor="service-interval">
                    <select id="service-interval" value={interval} onChange={(event) => setInterval(Number(event.target.value))}>
                      {INTERVALS.map(([minutes, label]) => (
                        <option key={minutes} value={minutes}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </FormRow>
                  <label className="md-check md-run-now">
                    <input type="checkbox" checked={runNow} onChange={(event) => setRunNow(event.target.checked)} />
                    保存后立即拾取
                  </label>
                </div>
              </>
            ) : (
              <>
                <div className="info-banner">
                  <span>
                    {connector} 暂不支持自动拾取。可以记录非机密的连接信息，然后在服务列表中「登记资产」、通过 MCP 工具写入，或在「导入导出」中导入 OpenMetadata 的 JSON。
                  </span>
                </div>
                {connection.map(([key, value], index) => (
                  <div key={index} className="md-kv-row">
                    <input aria-label="连接属性名" value={key} placeholder="属性名，例如 host" onChange={(event) => setConnection(connection.map((item, i) => (i === index ? [event.target.value, item[1]] : item)))} />
                    <input aria-label="连接属性值" value={value} placeholder="属性值" onChange={(event) => setConnection(connection.map((item, i) => (i === index ? [item[0], event.target.value] : item)))} />
                    <button type="button" className="icon-button" aria-label="移除该属性" onClick={() => setConnection(connection.filter((_, i) => i !== index))}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
                <button type="button" className="md-add" onClick={() => setConnection([...connection, ["", ""]])}>
                  <Plus size={12} />
                  添加属性
                </button>
                <p className="muted">密码、令牌、密钥等机密字段不会被保存。</p>
              </>
            )}
          </div>
        )}
        {error && <ErrorBanner message={error} />}
        <div className="md-form-actions">
          <button type="button" className="text-button" onClick={() => (step ? setStep(step - 1) : navigate("settings", "services", categoryId))}>
            {step ? "上一步" : "取消"}
          </button>
          {step < 2 ? (
            <button type="button" className="primary-button" disabled={!canNext} onClick={() => setStep(step + 1)}>
              下一步
            </button>
          ) : (
            <button type="button" className="primary-button" disabled={busy || (automated && mode === "bind" && !datasourceId)} onClick={() => void save()}>
              {busy ? "保存中…" : "保存"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ teams and users */

interface TeamRow extends Entity {
  users: Entity[];
  user_count: number;
  parent_team: Entity | null;
  owns_count: number;
}
interface UserRow extends Entity {
  teams: Entity[];
  owns_count: number;
}

function TeamsView() {
  const data = useApi<{ teams: TeamRow[]; users: UserRow[] }>(metadataPath("/teams"));
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const [tab, setTab] = useState<"teams" | "users">("teams");
  const [dialog, setDialog] = useState<"team" | "user" | null>(null);
  const [members, setMembers] = useState<TeamRow | null>(null);
  const [userTeams, setUserTeams] = useState<UserRow | null>(null);
  const [error, setError] = useState("");
  async function act(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
      data.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["团队和用户管理", null]]} />
      <div className="md-page-head">
        <div>
          <h1>团队和用户管理</h1>
          <p>团队和用户可以成为资产的所有者、术语的审核者和数据域的专家</p>
        </div>
        <button type="button" className="primary-button" onClick={() => setDialog(tab === "teams" ? "team" : "user")}>
          <Plus size={14} />
          {tab === "teams" ? "添加团队" : "添加用户"}
        </button>
      </div>
      <nav className="quality-tabs md-tabs">
        <button type="button" className={`quality-tab ${tab === "teams" ? "active" : ""}`} onClick={() => setTab("teams")}>
          <Users size={14} /> 团队 {data.data && <small className="md-count">{data.data.teams.length}</small>}
        </button>
        <button type="button" className={`quality-tab ${tab === "users" ? "active" : ""}`} onClick={() => setTab("users")}>
          用户 {data.data && <small className="md-count">{data.data.users.length}</small>}
        </button>
      </nav>
      {error && <ErrorBanner message={error} />}
      {data.loading && <Loading />}
      {data.error && <ErrorBanner message={data.error} />}
      {data.data && tab === "teams" && (
        <section className="panel">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>类型</th>
                  <th>上级团队</th>
                  <th>成员</th>
                  <th className="md-num">拥有资产</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {data.data.teams.map((team) => (
                  <tr key={team.id}>
                    <td>
                      <EntityLink entity={team}>{entityName(team)}</EntityLink>
                      <small className="md-block-muted">{team.name}</small>
                    </td>
                    <td>{String(team.team_type ?? "")}</td>
                    <td>{team.parent_team ? entityName(team.parent_team) : "—"}</td>
                    <td>{team.users.map(entityName).join("、") || "—"}</td>
                    <td className="md-num">{team.owns_count}</td>
                    <td>
                      <div className="md-actions">
                        <button type="button" className="text-button" onClick={() => setMembers(team)}>
                          成员
                        </button>
                        {team.name !== "Organization" && (
                          <button type="button" className="text-button danger-text" onClick={() => window.confirm(`永久删除团队 ${team.name}？`) && void act(() => mdPost("/entities/delete", { entity_type: "team", ref: team.fqn, hard: true }))}>
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {data.data && tab === "users" && (
        <section className="panel">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th>邮箱</th>
                  <th>团队</th>
                  <th>角色</th>
                  <th className="md-num">拥有资产</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {data.data.users.map((user) => (
                  <tr key={user.id}>
                    <td>
                      <span className="md-owner">
                        <span className="md-avatar">{entityName(user).slice(0, 1).toUpperCase()}</span>
                        <EntityLink entity={user}>{entityName(user)}</EntityLink>
                      </span>
                      <small className="md-block-muted">{user.name}</small>
                    </td>
                    <td>{String(user.email ?? "") || "—"}</td>
                    <td>{user.teams.map(entityName).join("、") || "—"}</td>
                    <td>
                      {user.is_admin === true && <span className="source-badge">管理员</span>} {user.is_bot === true && <span className="neutral-badge">机器人</span>}
                    </td>
                    <td className="md-num">{user.owns_count}</td>
                    <td>
                      <div className="md-actions">
                        <button type="button" className="text-button" onClick={() => setUserTeams(user)}>
                          团队
                        </button>
                        {!["lattice", "ingestion-bot", "mcp-agent"].includes(user.name) && (
                          <button type="button" className="text-button danger-text" onClick={() => window.confirm(`永久删除用户 ${user.name}？`) && void act(() => mdPost("/entities/delete", { entity_type: "user", ref: user.fqn, hard: true }))}>
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {dialog && (
        <PersonDialog
          kind={dialog}
          teams={data.data?.teams ?? []}
          teamTypes={types.data?.team_types ?? ["Organization", "BusinessUnit", "Division", "Department", "Group"]}
          onClose={() => setDialog(null)}
          onDone={() => {
            setDialog(null);
            data.reload();
          }}
        />
      )}
      {members && (
        <PeoplePickerModal
          title={`${entityName(members)} 的成员`}
          allowTeams={false}
          initial={members.users.map((user) => user.fqn)}
          onClose={() => setMembers(null)}
          onSave={(users) => act(() => mdPost("/entities/update", { entity_type: "team", ref: members.fqn, users }))}
        />
      )}
      {userTeams && (
        <TeamPicker
          user={userTeams}
          teams={data.data?.teams ?? []}
          onClose={() => setUserTeams(null)}
          onSave={(teams) => act(() => mdPost("/entities/update", { entity_type: "user", ref: userTeams.fqn, teams }))}
        />
      )}
    </div>
  );
}

function TeamPicker({ user, teams, onClose, onSave }: { user: UserRow; teams: TeamRow[]; onClose: () => void; onSave: (teams: string[]) => Promise<void> }) {
  const [chosen, setChosen] = useState(user.teams.map((team) => team.fqn));
  return (
    <Modal
      title={`${entityName(user)} 所属的团队`}
      width={480}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" onClick={() => void onSave(chosen).then(onClose)}>
            保存
          </button>
        </>
      }
    >
      {teams.map((team) => (
        <label key={team.id} className="md-picker-item">
          <input type="checkbox" checked={chosen.includes(team.fqn)} onChange={() => setChosen(chosen.includes(team.fqn) ? chosen.filter((item) => item !== team.fqn) : [...chosen, team.fqn])} />
          <span>
            <strong>{entityName(team)}</strong>
            <small>{String(team.team_type ?? "")}</small>
          </span>
        </label>
      ))}
    </Modal>
  );
}

function PersonDialog({ kind, teams, teamTypes, onClose, onDone }: { kind: "team" | "user"; teams: TeamRow[]; teamTypes: string[]; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [teamType, setTeamType] = useState("Group");
  const [parent, setParent] = useState("Organization");
  const [chosen, setChosen] = useState<string[]>([]);
  const [admin, setAdmin] = useState(false);
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  async function save() {
    setError("");
    try {
      if (kind === "team") await mdPost("/entities", { entity_type: "team", name: name.trim(), display_name: displayName, description, parent_team: parent || undefined, fields: { team_type: teamType, email } });
      else await mdPost("/entities", { entity_type: "user", name: name.trim(), display_name: displayName, description, teams: chosen, fields: { email, is_admin: admin } });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Modal
      title={kind === "team" ? "添加团队" : "添加用户"}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={!name.trim()} onClick={() => void save()}>
            保存
          </button>
        </>
      }
    >
      <div className="md-form-inline">
        <FormRow label="名称" htmlFor="person-name" required help={kind === "user" ? "小写字母、数字、点、下划线或连字符" : undefined}>
          <input id="person-name" value={name} onChange={(event) => setName(event.target.value)} />
        </FormRow>
        <FormRow label="显示名称" htmlFor="person-display">
          <input id="person-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
        </FormRow>
      </div>
      <FormRow label="邮箱" htmlFor="person-email">
        <input id="person-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
      </FormRow>
      {kind === "team" ? (
        <div className="md-form-inline">
          <FormRow label="团队类型" htmlFor="team-type">
            <select id="team-type" value={teamType} onChange={(event) => setTeamType(event.target.value)}>
              {teamTypes.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
          </FormRow>
          <FormRow label="上级团队" htmlFor="team-parent">
            <select id="team-parent" value={parent} onChange={(event) => setParent(event.target.value)}>
              <option value="">无</option>
              {teams.map((team) => (
                <option key={team.id} value={team.fqn}>
                  {entityName(team)}
                </option>
              ))}
            </select>
          </FormRow>
        </div>
      ) : (
        <>
          <FormRow label="团队">
            <div className="md-checks">
              {teams.map((team) => (
                <label key={team.id}>
                  <input type="checkbox" checked={chosen.includes(team.fqn)} onChange={() => setChosen(chosen.includes(team.fqn) ? chosen.filter((item) => item !== team.fqn) : [...chosen, team.fqn])} />
                  {entityName(team)}
                </label>
              ))}
            </div>
          </FormRow>
          <Toggle checked={admin} onChange={setAdmin} label="管理员" />
        </>
      )}
      <FormRow label="描述" htmlFor="person-description">
        <textarea id="person-description" rows={2} value={description} onChange={(event) => setDescription(event.target.value)} />
      </FormRow>
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

/* ------------------------------------------------------------------ runs and bundles */

function RunsView() {
  const [service, setService] = useState("");
  const [page, setPage] = useState(1);
  const size = 20;
  const runs = useApi<Paged<IngestionRun>>(metadataPath("/ingestion/runs", { service_fqn: service, page, size }));
  const services = useApi<{ items: IngestionService[] }>(metadataPath("/ingestion/services"));
  const running = (runs.data?.items ?? []).some((run) => run.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(runs.reload, 3000);
    return () => window.clearInterval(timer);
  }, [running]);
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["拾取记录", null]]} />
      <div className="md-page-head">
        <div>
          <h1>拾取记录</h1>
          <p>每次拾取写入的数据库、模式、数据表、字段与血缘数量，以及跳过的错误</p>
        </div>
        <div className="md-actions">
          <select value={service} onChange={(event) => { setService(event.target.value); setPage(1); }} aria-label="按服务筛选">
            <option value="">全部服务</option>
            {(services.data?.items ?? []).filter((item) => item.automated).map((item) => (
              <option key={item.id} value={item.fqn}>
                {item.fqn}
              </option>
            ))}
          </select>
          <button type="button" className="icon-button" aria-label="刷新" onClick={runs.reload}>
            <RefreshCw size={14} className={runs.loading ? "spin" : ""} />
          </button>
        </div>
      </div>
      <section className="panel">
        {runs.error && <ErrorBanner message={runs.error} />}
        {runs.data && !runs.data.items.length && <Empty title="还没有拾取记录">在服务列表中点击「拾取」开始第一次拾取。</Empty>}
        {runs.data && runs.data.items.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>服务</th>
                  <th>状态</th>
                  <th>触发</th>
                  <th>开始时间</th>
                  <th className="md-num">耗时</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.id}>
                    <td>{run.service ? <EntityLink entity={run.service}>{run.service_fqn}</EntityLink> : run.service_fqn}</td>
                    <td>
                      <RunStatus status={run.status} />
                    </td>
                    <td>{{ manual: "手动", scheduled: "定时", initial: "首次" }[run.trigger] ?? run.trigger}</td>
                    <td>{formatTime(run.started_at)}</td>
                    <td className="md-num">{formatDuration(run.elapsed_ms)}</td>
                    <td className="md-cell-desc">
                      {run.message || RUN_STATUS_LABELS[run.status]}
                      {(run.summary.errors ?? []).length > 0 && (
                        <details className="md-run-errors">
                          <summary>{run.summary.errors?.length} 处错误</summary>
                          <ul>
                            {(run.summary.errors ?? []).map((item, index) => (
                              <li key={index}>{item}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {runs.data && <Pager total={runs.data.total} page={page} size={size} onPage={setPage} />}
      </section>
    </div>
  );
}

function ImportExportView() {
  const [types, setTypes] = useState("");
  const [serviceFqn, setServiceFqn] = useState("");
  const [format, setFormat] = useState("openmetadata");
  const [text, setText] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [result, setResult] = useState<{ created: number; updated: number; skipped: number; tags: number; lineage: number; errors: string[] } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  async function exportBundle() {
    setBusy("export");
    setError("");
    try {
      const bundle = await mdGet<unknown>("/export", { entity_types: types, service_fqn: serviceFqn });
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `lattice-metadata-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function importBundle() {
    setBusy("import");
    setError("");
    setResult(null);
    try {
      const bundle: unknown = JSON.parse(text);
      setResult(await mdPost("/import", { format, bundle, dry_run: dryRun }));
    } catch (e) {
      setError(e instanceof SyntaxError ? `不是合法的 JSON：${e.message}` : errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="page-content md-page">
      <Crumbs items={[["元数据拾取", () => navigate("settings")], ["导入导出", null]]} />
      <div className="md-page-head">
        <div>
          <h1>导入导出</h1>
          <p>导出目录中的实体、标签与血缘；导入 Lattice 导出文件或 OpenMetadata API 返回的 JSON</p>
        </div>
      </div>
      {error && <ErrorBanner message={error} />}
      <div className="md-viz-columns">
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <Download size={15} /> 导出
            </h2>
          </div>
          <div className="panel-body md-properties">
            <FormRow label="实体类型" htmlFor="export-types" help="逗号分隔，留空导出除 KPI 与告警外的全部类型，例如 table,glossary,glossaryTerm">
              <input id="export-types" value={types} onChange={(event) => setTypes(event.target.value)} />
            </FormRow>
            <FormRow label="服务" htmlFor="export-service" help="可选：只导出一个服务下的资产">
              <input id="export-service" value={serviceFqn} onChange={(event) => setServiceFqn(event.target.value)} placeholder="例如 mysql-local" />
            </FormRow>
            <div className="md-form-actions">
              <button type="button" className="primary-button" disabled={busy === "export"} onClick={() => void exportBundle()}>
                <Download size={14} />
                {busy === "export" ? "导出中…" : "下载 JSON"}
              </button>
            </div>
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <Upload size={15} /> 导入
            </h2>
          </div>
          <div className="panel-body md-properties">
            <div className="md-form-inline">
              <FormRow label="格式" htmlFor="import-format">
                <select id="import-format" value={format} onChange={(event) => setFormat(event.target.value)}>
                  <option value="openmetadata">OpenMetadata（entities / data 数组，camelCase）</option>
                  <option value="lattice">Lattice 导出文件</option>
                </select>
              </FormRow>
              <FormRow label="文件" htmlFor="import-file" help="不超过 1 MB">
                <input
                  id="import-file"
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void file.text().then(setText);
                  }}
                />
              </FormRow>
            </div>
            <textarea className="code-editor" rows={8} value={text} onChange={(event) => setText(event.target.value)} placeholder='{"entities": [{"entityType": "table", "fullyQualifiedName": "svc.db.schema.orders", ...}], "lineage": [...]}' />
            <div className="md-form-actions">
              <Toggle checked={dryRun} onChange={setDryRun} label="只校验，不写入" />
              <button type="button" className="primary-button" disabled={!text.trim() || busy === "import"} onClick={() => void importBundle()}>
                <Upload size={14} />
                {busy === "import" ? "导入中…" : dryRun ? "校验" : "导入"}
              </button>
            </div>
            {result && (
              <div className={result.errors.length ? "quality-warn-badge md-import-result" : "success-banner md-import-result"}>
                {dryRun ? "校验结果" : "导入完成"}：新增 {result.created}，更新 {result.updated}，跳过 {result.skipped}，标签 {result.tags}，血缘 {result.lineage}
                {result.errors.length > 0 && (
                  <details className="md-run-errors">
                    <summary>{result.errors.length} 处错误</summary>
                    <ul>
                      {result.errors.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}
          </div>
        </section>
      </div>
      <button type="button" className="text-button" onClick={() => navigate("settings", "runs")}>
        <History size={12} />
        查看拾取记录 <ArrowRight size={12} />
      </button>
      <button type="button" className="text-button" onClick={() => navigate("settings")}>
        <ArrowLeft size={12} />
        返回
      </button>
    </div>
  );
}
