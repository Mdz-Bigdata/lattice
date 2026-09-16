// SPDX-License-Identifier: Apache-2.0
/**
 * 指标平台: the metric registry read from the semantic models (the single source of
 * truth) and the semantic-layer query builder that computes a metric by dimensions,
 * time grain and filters on any registered data source.
 */
import { useEffect, useState } from "react";
import { BookOpenCheck, Play, RefreshCw, Search, Sigma, Sparkles } from "lucide-react";
import { errorMessage, request, type DataSource, type QueryResult } from "./api";
import { Empty, ErrorBanner, Loading, PageTitle } from "./components";
import type { DataPageProps } from "./DataPages";
import { QueryReport } from "./QueryPage";
import { SourceSelect } from "./SourceBrowser";

interface MetricEntry {
  model: string;
  model_id: string;
  storage: string;
  name: string;
  description: string;
  expression: string;
  datatype: string;
  datasets: string[];
  synonyms: string[];
}
interface ModelEntry {
  id: string;
  name: string;
  storage: string;
  storage_label: string;
  description: string;
  datasets: number | null;
  metrics: number | null;
  dimensions?: number;
}
interface ModelDetail {
  id: string;
  name: string;
  storage: string;
  description: string;
  datasets: { name: string; source: string; description: string; fields: { name: string; field: string; datatype: string; is_time: boolean; is_dimension: boolean; description: string; expression: string }[] }[];
  relationships: { name: string; from: string; to: string; from_columns: string[]; to_columns: string[] }[];
  metrics: MetricEntry[];
  dimensions: { field: string; dataset: string; name: string; datatype: string; is_time: boolean; description: string }[];
  grains: { value: string; label: string }[];
  operators: string[];
}
interface Filter {
  field: string;
  op: string;
  value: string;
}
interface Compiled {
  sql: string;
  dialect: string;
  dialect_label: string;
  datasets: string[];
  joins: string[];
}

const OPERATOR_LABELS: Record<string, string> = {
  "=": "等于",
  "!=": "不等于",
  ">": "大于",
  ">=": "大于等于",
  "<": "小于",
  "<=": "小于等于",
  in: "属于（逗号分隔）",
  not_in: "不属于（逗号分隔）",
  like: "模糊匹配",
  between: "介于（逗号分隔两值）",
  is_null: "为空",
  not_null: "不为空",
};

export default function MetricsPage({ sources, sourceId, setSourceId }: DataPageProps) {
  const [tab, setTab] = useState<"catalog" | "query">("catalog");
  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<{ models: ModelEntry[]; metrics: MetricEntry[] } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [picked, setPicked] = useState<{ model: string; metric: string } | null>(null);
  async function loadCatalog(needle = query) {
    setBusy(true);
    setError("");
    try {
      setCatalog(await request(`/api/metrics/catalog${needle ? `?query=${encodeURIComponent(needle)}` : ""}`));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void loadCatalog("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="page-content">
      <PageTitle
        title="指标平台"
        description="指标定义来自 Apache Ossie 语义模型，这里是唯一口径：按维度、时间粒度与筛选条件计算，并供智能问数与 MCP 智能体消费"
        action={
          <nav className="quality-tabs md-tabs">
            <button type="button" className={`quality-tab ${tab === "catalog" ? "active" : ""}`} onClick={() => setTab("catalog")}>
              <BookOpenCheck size={14} /> 指标目录
            </button>
            <button type="button" className={`quality-tab ${tab === "query" ? "active" : ""}`} onClick={() => setTab("query")}>
              <Sigma size={14} /> 指标查询
            </button>
          </nav>
        }
      />
      {error && <ErrorBanner message={error} />}
      {tab === "catalog" ? (
        <CatalogTab
          catalog={catalog}
          busy={busy}
          query={query}
          setQuery={setQuery}
          onSearch={() => void loadCatalog()}
          onRefresh={async () => {
            await request("/api/metrics/refresh", {}).catch(() => undefined);
            await loadCatalog();
          }}
          onPick={(model, metric) => {
            setPicked({ model, metric });
            setTab("query");
          }}
        />
      ) : (
        <QueryTab models={catalog?.models ?? []} picked={picked} sources={sources} sourceId={sourceId} setSourceId={setSourceId} />
      )}
    </div>
  );
}

function CatalogTab({
  catalog,
  busy,
  query,
  setQuery,
  onSearch,
  onRefresh,
  onPick,
}: {
  catalog: { models: ModelEntry[]; metrics: MetricEntry[] } | null;
  busy: boolean;
  query: string;
  setQuery: (value: string) => void;
  onSearch: () => void;
  onRefresh: () => Promise<void>;
  onPick: (model: string, metric: string) => void;
}) {
  return (
    <>
      <section className="panel">
        <div className="quality-toolbar">
          <label htmlFor="metrics-search">搜索指标</label>
          <input
            id="metrics-search"
            value={query}
            placeholder="名称、同义词或说明，例如 GMV"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onSearch();
            }}
          />
          <button type="button" className="secondary-button" disabled={busy} onClick={onSearch}>
            <Search size={14} />
            搜索
          </button>
          <button type="button" className="secondary-button" disabled={busy} onClick={() => void onRefresh()}>
            <RefreshCw size={14} className={busy ? "spin" : ""} />
            重新读取模型
          </button>
        </div>
        {!catalog ? (
          <Loading text="正在读取语义模型…" />
        ) : (
          <div className="metric-models">
            {catalog.models.map((model) => (
              <div key={model.id || model.name} className={`metric-model ${model.storage === "error" ? "failed" : ""}`}>
                <b>{model.name}</b>
                <small>{model.storage_label}</small>
                {model.metrics !== null && (
                  <span>
                    {model.datasets} 个数据集 · {model.metrics} 个指标{typeof model.dimensions === "number" ? ` · ${model.dimensions} 个维度` : ""}
                  </span>
                )}
                {model.description && <p>{model.description}</p>}
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="panel">
        <h2>
          <Sigma size={16} /> 指标（{catalog?.metrics.length ?? 0}）
        </h2>
        {catalog && !catalog.metrics.length ? (
          <Empty title="没有匹配的指标">在“语义模型”页发布带 metrics 的模型后，指标会出现在这里。</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  <th>含义</th>
                  <th>计算表达式</th>
                  <th>数据集</th>
                  <th>模型</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {(catalog?.metrics ?? []).map((metric) => (
                  <tr key={`${metric.model_id}:${metric.name}`}>
                    <td>
                      <div className="quality-task-cell">
                        <span>{metric.name}</span>
                        {metric.synonyms.length > 0 && <small className="muted">{metric.synonyms.join(" / ")}</small>}
                      </div>
                    </td>
                    <td className="metric-desc">{metric.description || "—"}</td>
                    <td>
                      <code>{metric.expression}</code>
                    </td>
                    <td>{metric.datasets.join("、") || "—"}</td>
                    <td>{metric.model}</td>
                    <td>
                      <button type="button" className="text-button" onClick={() => onPick(metric.model_id, metric.name)}>
                        <Play size={13} />
                        查询
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function QueryTab({
  models,
  picked,
  sources,
  sourceId,
  setSourceId,
}: {
  models: ModelEntry[];
  picked: { model: string; metric: string } | null;
  sources: DataSource[];
  sourceId: string;
  setSourceId: (id: string) => void;
}) {
  const usable = models.filter((model) => model.id && model.storage !== "error");
  const [modelId, setModelId] = useState(picked?.model ?? usable[0]?.id ?? "");
  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [metrics, setMetrics] = useState<string[]>(picked ? [picked.metric] : []);
  const [dimensions, setDimensions] = useState<{ field: string; grain: string }[]>([]);
  const [filters, setFilters] = useState<Filter[]>([]);
  const [limit, setLimit] = useState(200);
  const [datasource, setDatasource] = useState(sourceId);
  const [compiled, setCompiled] = useState<Compiled | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!modelId && usable[0]) setModelId(usable[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [usable.length]);
  useEffect(() => {
    if (!modelId) return;
    setDetail(null);
    setCompiled(null);
    request<ModelDetail>(`/api/metrics/model?ref=${encodeURIComponent(modelId)}`)
      .then((loaded) => {
        setDetail(loaded);
        setMetrics((current) => current.filter((name) => loaded.metrics.some((metric) => metric.name === name)));
        setDimensions((current) => current.filter((item) => loaded.dimensions.some((dimension) => dimension.field === item.field)));
      })
      .catch((e) => setError(errorMessage(e)));
  }, [modelId]);
  useEffect(() => {
    if (picked) {
      setModelId(picked.model);
      setMetrics([picked.metric]);
    }
  }, [picked]);
  function body(refresh = false) {
    return {
      model: modelId,
      metrics,
      dimensions: dimensions.map((item) => (item.grain ? { field: item.field, grain: item.grain } : { field: item.field })),
      filters: filters
        .filter((item) => item.field)
        .map((item) => ({ field: item.field, op: item.op, value: item.op === "is_null" || item.op === "not_null" ? null : item.value })),
      limit,
      datasource_id: datasource,
      refresh,
    };
  }
  async function compile() {
    setBusy("compile");
    setError("");
    try {
      setCompiled(await request<Compiled>("/api/metrics/compile", body()));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function run(refresh = false) {
    if (!metrics.length) return setError("请至少选择一个指标。");
    setBusy("run");
    setError("");
    try {
      const [answer, plan] = await Promise.all([request<QueryResult>("/api/metrics/query", body(refresh)), request<Compiled>("/api/metrics/compile", body())]);
      setResult(answer);
      setCompiled(plan);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  function toggleMetric(name: string) {
    setMetrics((current) => (current.includes(name) ? current.filter((item) => item !== name) : [...current, name]));
  }
  function toggleDimension(field: string) {
    setDimensions((current) => (current.some((item) => item.field === field) ? current.filter((item) => item.field !== field) : [...current, { field, grain: "" }]));
  }
  const dimensionOf = (field: string) => detail?.dimensions.find((item) => item.field === field);
  return (
    <>
      <section className="panel metric-builder">
        <div className="quality-field-row">
          <div className="quality-field">
            <label htmlFor="metric-model">语义模型</label>
            <select id="metric-model" value={modelId} onChange={(event) => setModelId(event.target.value)}>
              {usable.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}（{model.storage_label}）
                </option>
              ))}
            </select>
            {detail?.description && <small>{detail.description}</small>}
          </div>
          <div className="quality-field">
            <label htmlFor="metric-source">执行数据源</label>
            <SourceSelect
              id="metric-source"
              sources={sources}
              value={datasource}
              disabled={!!busy}
              onChange={(id) => {
                setDatasource(id);
                setSourceId(id);
              }}
            />
            <small>模型数据集的 source 在该数据源里应存在（本地示例数据对应内置演示模型）。</small>
          </div>
          <div className="quality-field">
            <label htmlFor="metric-limit">返回行数上限</label>
            <input id="metric-limit" type="number" min={1} max={5000} value={limit} onChange={(event) => setLimit(Math.max(1, Math.min(5000, Number(event.target.value) || 200)))} />
          </div>
        </div>
        {!detail ? (
          modelId ? <Loading text="正在读取模型定义…" /> : <Empty title="没有可查询的语义模型">先在“语义模型”页保存或发布一个带 metrics 的模型。</Empty>
        ) : (
          <>
            <div className="metric-pick">
              <h3>
                <Sigma size={15} /> 指标 <small>{metrics.length ? `已选 ${metrics.length} 个` : "请至少选择一个"}</small>
              </h3>
              <div className="metric-chips">
                {detail.metrics.map((metric) => (
                  <button
                    key={metric.name}
                    type="button"
                    className={`metric-chip ${metrics.includes(metric.name) ? "selected" : ""}`}
                    title={`${metric.description || ""}\n${metric.expression}`}
                    onClick={() => toggleMetric(metric.name)}
                  >
                    {metric.name}
                    {metric.description && <small>{metric.description}</small>}
                  </button>
                ))}
              </div>
            </div>
            <div className="metric-pick">
              <h3>
                维度 <small>按数据集分组，时间字段可选粒度</small>
              </h3>
              <div className="metric-chips">
                {detail.dimensions.map((dimension) => {
                  const chosen = dimensions.find((item) => item.field === dimension.field);
                  return (
                    <span key={dimension.field} className={`metric-chip ${chosen ? "selected" : ""}`}>
                      <button type="button" onClick={() => toggleDimension(dimension.field)} title={dimension.description || dimension.datatype}>
                        {dimension.field}
                        {dimension.is_time && <small>时间</small>}
                      </button>
                      {chosen && dimension.is_time && (
                        <select
                          value={chosen.grain}
                          aria-label={`${dimension.field} 的时间粒度`}
                          onChange={(event) => setDimensions((current) => current.map((item) => (item.field === dimension.field ? { ...item, grain: event.target.value } : item)))}
                        >
                          <option value="">原值</option>
                          {detail.grains.map((grain) => (
                            <option key={grain.value} value={grain.value}>
                              {grain.label}
                            </option>
                          ))}
                        </select>
                      )}
                    </span>
                  );
                })}
              </div>
            </div>
            <div className="metric-pick">
              <h3>
                筛选 <small>作用于维度字段，或对指标值做 HAVING</small>
              </h3>
              {filters.map((filter, index) => (
                <div key={index} className="metric-filter">
                  <select value={filter.field} onChange={(event) => setFilters((current) => current.map((item, i) => (i === index ? { ...item, field: event.target.value } : item)))}>
                    <option value="">选择字段</option>
                    <optgroup label="维度">
                      {detail.dimensions.map((dimension) => (
                        <option key={dimension.field} value={dimension.field}>
                          {dimension.field}
                        </option>
                      ))}
                    </optgroup>
                    <optgroup label="其他字段">
                      {detail.datasets.flatMap((dataset) => dataset.fields.filter((field) => !field.is_dimension).map((field) => (
                        <option key={field.field} value={field.field}>
                          {field.field}
                        </option>
                      )))}
                    </optgroup>
                    <optgroup label="指标（HAVING）">
                      {detail.metrics.map((metric) => (
                        <option key={metric.name} value={metric.name}>
                          {metric.name}
                        </option>
                      ))}
                    </optgroup>
                  </select>
                  <select value={filter.op} onChange={(event) => setFilters((current) => current.map((item, i) => (i === index ? { ...item, op: event.target.value } : item)))}>
                    {detail.operators.map((op) => (
                      <option key={op} value={op}>
                        {OPERATOR_LABELS[op] ?? op}
                      </option>
                    ))}
                  </select>
                  <input
                    value={filter.value}
                    disabled={filter.op === "is_null" || filter.op === "not_null"}
                    placeholder={dimensionOf(filter.field)?.is_time ? "2024-01-01" : "值"}
                    onChange={(event) => setFilters((current) => current.map((item, i) => (i === index ? { ...item, value: event.target.value } : item)))}
                  />
                  <button type="button" className="text-button" onClick={() => setFilters((current) => current.filter((_, i) => i !== index))}>
                    移除
                  </button>
                </div>
              ))}
              <button type="button" className="text-button" onClick={() => setFilters((current) => [...current, { field: "", op: "=", value: "" }])}>
                + 添加筛选条件
              </button>
            </div>
            <div className="button-row">
              <button type="button" className="primary-button" disabled={!!busy || !metrics.length} onClick={() => void run()}>
                <Play size={14} />
                {busy === "run" ? "计算中…" : "计算指标"}
              </button>
              <button type="button" className="secondary-button" disabled={!!busy || !metrics.length} onClick={() => void compile()}>
                <Sparkles size={14} />
                {busy === "compile" ? "生成中…" : "只看 SQL"}
              </button>
            </div>
          </>
        )}
      </section>
      {error && <ErrorBanner message={error} />}
      {compiled && (
        <section className="panel">
          <h2>生成的 SQL（{compiled.dialect_label}）</h2>
          <p className="muted">
            数据集：{compiled.datasets.join(" → ")}
            {compiled.joins.length ? ` · 关联：${compiled.joins.join("；")}` : ""}
          </p>
          <pre className="sql-code">{compiled.sql}</pre>
        </section>
      )}
      {result && <QueryReport key={result.id} result={result} defaultMode={result.chart?.type === "line" ? "line" : "bar"} onRefresh={() => void run(true)} />}
    </>
  );
}
