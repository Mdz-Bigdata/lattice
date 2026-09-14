// SPDX-License-Identifier: Apache-2.0
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  ChevronLeft,
  Database,
  FolderTree,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  RotateCcw,
  ServerCog,
  TerminalSquare,
  Trash2,
  X,
} from "lucide-react";
import {
  request,
  errorMessage,
  datasourcePath,
  LOCAL_SAMPLE_ID,
  SECRET_MASK,
  type DataSource,
  type SourceField,
  type SourceType,
  type TestResult,
} from "./api";
import { Drawer, Empty, ErrorBanner, PageTitle } from "./components";
import {
  categoryIcon,
  SourceBrowser,
  SourceStatusBadge,
} from "./SourceBrowser";
import type { DataPageProps } from "./DataPages";
import "./sources.css";

type FieldValue = string | number | boolean;
type DrawerState =
  | { mode: "create" }
  | { mode: "edit"; source: DataSource }
  | null;

export default function SourcesPage({
  sources,
  sourceTypes,
  sourcesError,
  reloadSources,
  goTo,
  sourceId,
  setSourceId,
}: DataPageProps) {
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [browsing, setBrowsing] = useState("");
  const [testing, setTesting] = useState("");
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const browsingSource = sources.find((source) => source.id === browsing);

  async function refresh() {
    setRefreshing(true);
    try {
      await reloadSources();
    } finally {
      setRefreshing(false);
    }
  }
  async function test(source: DataSource) {
    setTesting(source.id);
    setError("");
    setNotice("");
    try {
      const result = await request<TestResult>(
        datasourcePath(source.id, "/test"),
        {},
      );
      setTests((current) => ({ ...current, [source.id]: result }));
      await reloadSources();
    } catch (e) {
      setError(`测试「${source.name}」失败：${errorMessage(e)}`);
    } finally {
      setTesting("");
    }
  }
  async function remove(source: DataSource) {
    const note = source.builtin
      ? "该数据源由本机运行的引擎自动登记，删除后将从列表中隐藏，可用「恢复内置数据源」找回。"
      : "仅删除本地保存的连接配置。";
    if (
      !window.confirm(
        `确认删除数据源「${source.name}」？\n${note}不会修改远端数据。`,
      )
    )
      return;
    setError("");
    setNotice("");
    try {
      await request(datasourcePath(source.id, "/delete"), {});
      if (browsing === source.id) setBrowsing("");
      if (sourceId === source.id) setSourceId(LOCAL_SAMPLE_ID);
      setNotice(
        source.builtin
          ? `已隐藏内置数据源「${source.name}」，可用「恢复内置数据源」找回。`
          : `已删除数据源「${source.name}」。`,
      );
      await reloadSources();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  /** Bring back builtin sources that were removed from the list. */
  async function restore() {
    setError("");
    setNotice("");
    try {
      const result = await request<{ restored?: string[] }>(
        "/api/datasources/restore",
        {},
      );
      const count = result.restored?.length ?? 0;
      setNotice(
        count ? `已恢复 ${count} 个内置数据源。` : "没有被隐藏的内置数据源。",
      );
      await reloadSources();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <div className="page-content">
      <PageTitle
        title="数据源"
        description="管理 DuckDB、MySQL、PostgreSQL、ClickHouse、StarRocks、Doris、Hive、Iceberg、Paimon 数据源，测试连接并浏览元数据"
        action={
          <div className="button-row">
            <button
              className="secondary-button"
              onClick={() => void restore()}
              disabled={refreshing}
              title="把被删除的内置数据源重新显示出来"
            >
              <RotateCcw size={14} />
              恢复内置数据源
            </button>
            <button
              className="secondary-button"
              onClick={() => void refresh()}
              disabled={refreshing}
            >
              <RefreshCw size={14} className={refreshing ? "spin" : ""} />
              刷新
            </button>
            <button
              className="primary-button"
              onClick={() => setDrawer({ mode: "create" })}
              disabled={!sourceTypes.length}
            >
              <Plus size={15} />
              新增数据源
            </button>
          </div>
        }
      />
      {sourcesError && (
        <ErrorBanner message={`数据源列表不可用：${sourcesError}`} />
      )}
      {error && <ErrorBanner message={error} />}
      {notice && (
        <div className="success-banner" role="status">
          {notice}
        </div>
      )}
      <div className="source-grid">
        {sources.map((source) => (
          <SourceCard
            key={source.id}
            source={source}
            test={tests[source.id] ?? source.last_test}
            busy={testing === source.id}
            onTest={() => void test(source)}
            onBrowse={() => setBrowsing(source.id)}
            onEdit={() => setDrawer({ mode: "edit", source })}
            onDelete={() => void remove(source)}
            onSql={() => goTo("sql", undefined, source.id)}
          />
        ))}
      </div>
      {!sources.length && !sourcesError && (
        <Empty title="还没有数据源">
          点击“新增数据源”注册 MySQL、PostgreSQL、ClickHouse 等连接。
        </Empty>
      )}
      {browsingSource && (
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <FolderTree size={15} />
              浏览数据 · {browsingSource.name}
            </h2>
            <button
              className="icon-button"
              aria-label="关闭数据浏览"
              onClick={() => setBrowsing("")}
            >
              <X size={16} />
            </button>
          </div>
          <SourceBrowser
            key={browsingSource.id}
            source={browsingSource}
            goTo={goTo}
          />
        </section>
      )}
      {drawer && (
        <SourceFormDrawer
          key={drawer.mode === "edit" ? drawer.source.id : "create"}
          types={sourceTypes}
          presets={sources}
          source={drawer.mode === "edit" ? drawer.source : undefined}
          onClose={() => setDrawer(null)}
          onSaved={async (saved, mode) => {
            setDrawer(null);
            setError("");
            setNotice(
              mode === "create"
                ? `已新增数据源「${saved.name}」。`
                : `已更新数据源「${saved.name}」。`,
            );
            await reloadSources();
          }}
        />
      )}
    </div>
  );
}

function SourceCard({
  source,
  test,
  busy,
  onTest,
  onBrowse,
  onEdit,
  onDelete,
  onSql,
}: {
  source: DataSource;
  test?: TestResult | null;
  busy: boolean;
  onTest: () => void;
  onBrowse: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onSql: () => void;
}) {
  return (
    <article className="source-card" aria-label={source.name}>
      <div className="source-card-head">
        <span className="source-card-icon">
          {categoryIcon(source.category, 20)}
        </span>
        <div>
          <h2>{source.name}</h2>
          <span>
            {source.type_label} · {source.category}
          </span>
        </div>
        {source.builtin && <span className="neutral-badge">内置</span>}
      </div>
      <code className="source-summary" title={source.summary}>
        {source.summary || source.id}
      </code>
      {source.description && <p>{source.description}</p>}
      <div className="source-card-status">
        <SourceStatusBadge test={test} />
        {test?.detail && <small title={test.detail}>{test.detail}</small>}
      </div>
      <div className="source-card-actions">
        <button className="small-button" disabled={busy} onClick={onTest}>
          <Plug size={13} />
          {busy ? "测试中…" : "测试连接"}
        </button>
        <button className="small-button" onClick={onBrowse}>
          <FolderTree size={13} />
          浏览数据
        </button>
        <button className="small-button" onClick={onSql}>
          <TerminalSquare size={13} />在 SQL 工作台打开
        </button>
        {source.id !== LOCAL_SAMPLE_ID && (
          <button className="small-button" onClick={onEdit}>
            <Pencil size={13} />
            编辑
          </button>
        )}
        {
          <button className="small-button danger-text" onClick={onDelete}>
            <Trash2 size={13} />
            删除
          </button>
        }
      </div>
    </article>
  );
}

function groupByCategory(types: SourceType[]): [string, SourceType[]][] {
  const groups = new Map<string, SourceType[]>();
  for (const type of types)
    groups.set(type.category, [...(groups.get(type.category) ?? []), type]);
  return [...groups.entries()];
}
/** True when the backend marks the field as a secret; those are never copied from a preset. */
function isSecret(field: SourceField): boolean {
  return field.type === "password";
}
/** A descriptor with options is always a dropdown, even if the type says otherwise. */
function isChoice(field: SourceField): boolean {
  return field.type === "select" || (field.options?.length ?? 0) > 0;
}
/**
 * Seeds the form from a data source. `template` copies another source's
 * configuration (环境预设) and therefore blanks every secret and masked value.
 */
function fieldValues(
  type: SourceType,
  source?: DataSource,
  template = false,
): Record<string, FieldValue> {
  const values: Record<string, FieldValue> = {};
  for (const field of type.fields) {
    const stored = source?.config[field.name];
    const secret = template && (isSecret(field) || stored === SECRET_MASK);
    if (!secret && stored !== undefined && stored !== null) {
      values[field.name] =
        typeof stored === "boolean" || typeof stored === "number"
          ? stored
          : String(stored);
    } else if (secret) values[field.name] = "";
    else if (field.default !== undefined) values[field.name] = field.default;
    else if (field.name === "port" && typeof type.default_port === "number")
      values[field.name] = type.default_port;
    else values[field.name] = field.type === "checkbox" ? false : "";
  }
  return values;
}
function buildConfig(
  type: SourceType,
  values: Record<string, FieldValue>,
  source?: DataSource,
): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  if (source)
    for (const [key, value] of Object.entries(source.config))
      if (!type.fields.some((field) => field.name === key)) config[key] = value;
  for (const field of type.fields) {
    const raw = values[field.name];
    if (field.type === "checkbox") config[field.name] = Boolean(raw);
    else if (field.type === "number") {
      const text = String(raw ?? "").trim();
      if (text) config[field.name] = Number(text);
    } else {
      const text = isSecret(field)
        ? String(raw ?? "")
        : String(raw ?? "").trim();
      if (text) config[field.name] = text;
    }
  }
  return config;
}

function SourceFormDrawer({
  types,
  presets,
  source,
  onClose,
  onSaved,
}: {
  types: SourceType[];
  /** Registered sources from GET /api/datasources; same-type entries become 环境预设. */
  presets: DataSource[];
  source?: DataSource;
  onClose: () => void;
  onSaved: (saved: DataSource, mode: "create" | "edit") => void | Promise<void>;
}) {
  const [typeId, setTypeId] = useState(source?.type ?? "");
  const type = types.find((item) => item.id === typeId);
  const [name, setName] = useState(source?.name ?? "");
  const [description, setDescription] = useState(source?.description ?? "");
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    type ? fieldValues(type, source) : {},
  );
  const [presetId, setPresetId] = useState("");
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState<"" | "test" | "save">("");
  const [error, setError] = useState("");
  const editing = source !== undefined;
  /** Presets of the current type; other types carry connection keys this type rejects. */
  const usable = useMemo(
    () =>
      presets.filter((item) => item.type === typeId && item.id !== source?.id),
    [presets, typeId, source?.id],
  );
  const presetCount = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of presets)
      counts.set(item.type, (counts.get(item.type) ?? 0) + 1);
    return counts;
  }, [presets]);
  const applied = usable.find((item) => item.id === presetId);
  const blanked = applied
    ? (type?.fields ?? [])
        .filter(
          (field) =>
            isSecret(field) && String(applied.config[field.name] ?? "") !== "",
        )
        .map((field) => field.label)
    : [];

  function chooseType(next: SourceType) {
    setTypeId(next.id);
    setValues(fieldValues(next));
    setPresetId("");
    setTestResult(null);
    setError("");
  }
  function usePreset(nextId: string) {
    if (!type) return;
    const preset = usable.find((item) => item.id === nextId);
    setPresetId(preset ? preset.id : "");
    setTestResult(null);
    setError("");
    setValues(fieldValues(type, preset ?? source, preset !== undefined));
    if (preset && !editing && !name.trim()) setName(`${preset.name}（副本）`);
  }
  function prepare(): { name: string; config: Record<string, unknown> } | null {
    if (!type) return null;
    if (!name.trim()) {
      setError("请填写数据源名称。");
      return null;
    }
    const missing = type.fields.filter(
      (field) =>
        field.required &&
        field.type !== "checkbox" &&
        String(values[field.name] ?? "").trim() === "",
    );
    if (missing.length) {
      setError(
        `请填写必填项：${missing.map((field) => field.label).join("、")}`,
      );
      return null;
    }
    const invalid = type.fields.filter(
      (field) =>
        field.type === "number" &&
        String(values[field.name] ?? "").trim() !== "" &&
        !Number.isFinite(Number(values[field.name])),
    );
    if (invalid.length) {
      setError(
        `${invalid.map((field) => field.label).join("、")} 必须是数字。`,
      );
      return null;
    }
    return { name: name.trim(), config: buildConfig(type, values, source) };
  }
  async function test() {
    const prepared = prepare();
    if (!prepared || !type) return;
    setBusy("test");
    setError("");
    setTestResult(null);
    try {
      setTestResult(
        await request<TestResult>("/api/datasources/test", {
          type: type.id,
          config: prepared.config,
          ...(source ? { id: source.id } : {}),
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    const prepared = prepare();
    if (!prepared || !type) return;
    setBusy("save");
    setError("");
    try {
      const saved = source
        ? await request<DataSource>(datasourcePath(source.id, "/update"), {
            name: prepared.name,
            description: description.trim() || undefined,
            config: prepared.config,
          })
        : await request<DataSource>("/api/datasources", {
            name: prepared.name,
            type: type.id,
            config: prepared.config,
            description: description.trim() || undefined,
          });
      await onSaved(saved, source ? "edit" : "create");
    } catch (e) {
      setError(errorMessage(e));
      setBusy("");
    }
  }
  return (
    <Drawer
      id="source-form"
      title={editing ? `编辑数据源 · ${source.name}` : "新增数据源"}
      icon={<Database size={17} />}
      onClose={onClose}
      wide
      closeOnBackdrop={false}
    >
      {!type ? (
        <div className="drawer-body">
          <p className="muted">
            选择数据源类型，随后可直接套用本机已注册的环境，无需手工记忆连接串。
          </p>
          {groupByCategory(types).map(([category, items]) => (
            <div className="type-group" key={category}>
              <h3>
                {categoryIcon(category, 14)}
                {category}
                <span className="type-group-count">{items.length}</span>
              </h3>
              <div className="type-picker type-picker-compact">
                {items.map((item) => (
                  <button
                    type="button"
                    className="type-card type-card-compact"
                    key={item.id}
                    onClick={() => chooseType(item)}
                  >
                    <span className="type-card-head">
                      <span className="type-card-icon">
                        {categoryIcon(item.category, 15)}
                      </span>
                      <strong>{item.label}</strong>
                    </span>
                    <span className="type-card-desc">{item.description}</span>
                    <span className="type-card-meta">
                      <span className="type-chip" title="驱动">
                        {item.driver}
                      </span>
                      <span className="type-chip">
                        {typeof item.default_port === "number"
                          ? `默认端口 ${item.default_port}`
                          : "无需端口"}
                      </span>
                      {!!presetCount.get(item.id) && (
                        <span className="type-chip type-chip-ready">
                          本机 {presetCount.get(item.id)} 个环境
                        </span>
                      )}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}
          {!types.length && <Empty title="暂无可用的数据源类型" />}
        </div>
      ) : (
        <form className="drawer-form" onSubmit={(event) => void save(event)}>
          <div className="drawer-body">
            <div className="type-summary">
              <span className="source-card-icon">
                {categoryIcon(type.category, 18)}
              </span>
              <div>
                <strong>{type.label}</strong>
                <small>
                  {type.driver} · 方言 {type.dialect}
                  {typeof type.default_port === "number"
                    ? ` · 默认端口 ${type.default_port}`
                    : ""}
                </small>
              </div>
              {!editing && (
                <button
                  type="button"
                  className="text-button"
                  disabled={!!busy}
                  onClick={() => setTypeId("")}
                >
                  <ChevronLeft size={13} />
                  更换类型
                </button>
              )}
            </div>
            <section className="form-block">
              <h3 className="form-block-title">
                <ServerCog size={14} />
                环境预设
              </h3>
              <div className="preset-row">
                <select
                  id="source-preset"
                  className="preset-select"
                  aria-label="环境预设"
                  value={presetId}
                  disabled={!usable.length || !!busy}
                  onChange={(event) => usePreset(event.target.value)}
                >
                  <option value="">
                    {!usable.length
                      ? "本机没有同类型的已注册环境"
                      : editing
                        ? "不套用预设（保留已保存的配置）"
                        : "不套用预设（使用类型默认值）"}
                  </option>
                  {usable.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} · {item.summary || item.id}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="text-button"
                  disabled={!presetId || !!busy}
                  onClick={() => usePreset("")}
                >
                  <RotateCcw size={13} />
                  还原
                </button>
              </div>
              <p className="preset-hint">
                以此为模板：复制所选数据源在本机真实可用的连接配置（主机、端口、库名等），
                密钥类字段一律留空，请自行填写。
              </p>
              {applied && (
                <p className="preset-note" role="status">
                  已套用「{applied.name}」
                  {blanked.length
                    ? `，仍需填写：${blanked.join("、")}`
                    : "，可直接测试连接。"}
                </p>
              )}
            </section>
            <section className="form-block">
              <h3 className="form-block-title">
                <Database size={14} />
                基本信息
              </h3>
              <div className="field-grid">
                <div className="field required">
                  <label htmlFor="source-name">
                    名称<b aria-label="必填">*</b>
                  </label>
                  <input
                    id="source-name"
                    value={name}
                    maxLength={80}
                    disabled={!!busy}
                    aria-required="true"
                    placeholder="例如：生产 MySQL"
                    onChange={(event) => setName(event.target.value)}
                  />
                </div>
                <div className="field field-wide">
                  <label htmlFor="source-description">备注</label>
                  <textarea
                    id="source-description"
                    rows={2}
                    value={description}
                    disabled={!!busy}
                    placeholder="可选，说明用途或负责人"
                    onChange={(event) => setDescription(event.target.value)}
                  />
                </div>
              </div>
            </section>
            <section className="form-block">
              <h3 className="form-block-title">
                <Plug size={14} />
                连接参数
              </h3>
              <div className="field-grid">
                {type.fields.map((field) => (
                  <FieldControl
                    key={field.name}
                    field={field}
                    value={values[field.name]}
                    editing={editing}
                    disabled={!!busy}
                    onChange={(value) =>
                      setValues((current) => ({
                        ...current,
                        [field.name]: value,
                      }))
                    }
                  />
                ))}
              </div>
            </section>
            {error && <ErrorBanner message={error} />}
            {testResult && <TestResultBanner result={testResult} />}
          </div>
          <div className="drawer-footer">
            <button
              type="button"
              className="secondary-button"
              onClick={() => void test()}
              disabled={!!busy}
            >
              <Plug size={14} />
              {busy === "test" ? "测试中…" : "测试连接"}
            </button>
            <button type="submit" className="primary-button" disabled={!!busy}>
              {busy === "save" ? "保存中…" : "保存"}
            </button>
            <button
              type="button"
              className="text-button"
              onClick={onClose}
              disabled={!!busy}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </Drawer>
  );
}

function FieldControl({
  field,
  value,
  editing,
  disabled,
  onChange,
}: {
  field: SourceField;
  value: FieldValue | undefined;
  editing: boolean;
  disabled: boolean;
  onChange: (value: FieldValue) => void;
}) {
  const id = `source-field-${field.name}`;
  const text = value === undefined ? "" : String(value);
  if (field.type === "checkbox") {
    return (
      <div className="field field-check">
        <label htmlFor={id}>
          <input
            id={id}
            type="checkbox"
            checked={Boolean(value)}
            disabled={disabled}
            onChange={(event) => onChange(event.target.checked)}
          />
          {field.label}
        </label>
        {field.help && <small>{field.help}</small>}
      </div>
    );
  }
  const masked = isSecret(field) && editing && text === SECRET_MASK;
  const help = masked
    ? `${field.help ? `${field.help} ` : ""}保持 ${SECRET_MASK} 表示沿用已保存的密钥。`
    : field.help;
  let control: ReactNode;
  if (isChoice(field)) {
    const options = field.options ?? [];
    const known = options.some((option) => option.value === text);
    control = (
      <select
        id={id}
        value={text}
        disabled={disabled}
        aria-required={field.required}
        onChange={(event) => onChange(event.target.value)}
      >
        {!field.required && <option value="">默认</option>}
        {field.required && !known && <option value="">请选择</option>}
        {!known && text !== "" && (
          <option value={text}>{text}（当前值）</option>
        )}
        {options.map((option) => (
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
        value={text}
        spellCheck={false}
        disabled={disabled}
        aria-required={field.required}
        placeholder={field.placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else {
    control = (
      <input
        id={id}
        type={
          field.type === "password"
            ? "password"
            : field.type === "number"
              ? "number"
              : "text"
        }
        value={text}
        spellCheck={false}
        disabled={disabled}
        inputMode={field.type === "number" ? "numeric" : undefined}
        aria-required={field.required}
        placeholder={field.placeholder}
        autoComplete={field.type === "password" ? "new-password" : "off"}
        onFocus={(event) => {
          if (masked) event.target.select();
        }}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <div
      className={`field ${field.required ? "required" : ""} ${field.type === "textarea" ? "field-wide" : ""}`}
    >
      <label htmlFor={id}>
        {field.label}
        {field.required && <b aria-label="必填">*</b>}
      </label>
      {control}
      {help && <small>{help}</small>}
    </div>
  );
}

function TestResultBanner({ result }: { result: TestResult }) {
  const tested = new Date(result.tested_at);
  const metrics: [string, string][] = [
    ["响应耗时", `${Math.round(result.latency_ms)} ms`],
  ];
  if (result.server_version)
    metrics.push(["服务端版本", result.server_version]);
  if (typeof result.table_count === "number")
    metrics.push(["可见表", `${result.table_count} 张`]);
  if (!Number.isNaN(tested.getTime()))
    metrics.push(["测试时间", tested.toLocaleTimeString("zh-CN")]);
  return (
    <div
      className={result.ok ? "success-banner" : "error-banner"}
      role="status"
    >
      <div className="test-summary">
        <strong>
          {result.ok
            ? "连接成功"
            : result.status === "offline"
              ? "无法连接"
              : "连接异常"}
        </strong>
        <p>{result.detail}</p>
        <dl className="test-metrics">
          {metrics.map(([label, text]) => (
            <div className="test-metric" key={label}>
              <dt>{label}</dt>
              <dd>{text}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
