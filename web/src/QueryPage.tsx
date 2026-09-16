// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  BarChart3,
  ChartNoAxesColumnIncreasing,
  ChartPie,
  ChevronDown,
  ChevronRight,
  Clock3,
  Code2,
  Database,
  History,
  ListFilter,
  LoaderCircle,
  Plug,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  Table2,
  TrendingUp,
} from "lucide-react";
import {
  formatAge,
  request,
  errorMessage,
  streamQuery,
  StreamUnavailableError,
  LOCAL_SAMPLE_ID,
  SECRET_MASK,
  type Bootstrap,
  type DataSource,
  type Health,
  type LlmStatus,
  type LlmTestResult,
  type QueryResult,
} from "./api";
import { Drawer, Empty, ErrorBanner, Loading } from "./components";
import ResultChart, { type ChartMode } from "./ResultChart";
import { SourceSelect, useDefaultTables } from "./SourceBrowser";
import "./model-settings.css";

const chartModes = [
  { mode: "line", icon: TrendingUp, label: "折线图" },
  { mode: "bar", icon: ChartNoAxesColumnIncreasing, label: "柱状图" },
  { mode: "horizontal", icon: ListFilter, label: "条形图" },
  { mode: "pie", icon: ChartPie, label: "饼图" },
  { mode: "table", icon: Table2, label: "数据表" },
] as const;
const DEFAULT_QUESTION = "月度销售额趋势";
const GENERIC_QUESTIONS = [
  "按月统计订单金额",
  "各类别销售额排名",
  "各地区客户数量分布",
  "订单状态占比",
];

function providerLabel(provider?: string) {
  if (provider === "sql") return "用户 SQL";
  if (provider === "rules") return "本地规则";
  if (provider === "llm") return "模型生成";
  return provider ? "未知生成方式" : "待查询";
}
function linkPort(link: string) {
  const index = link.lastIndexOf(":");
  return index >= 0 ? link.slice(index) : link;
}

export function QueryReport({
  result,
  defaultMode = "bar",
  onRefresh,
}: {
  result: QueryResult;
  defaultMode?: ChartMode;
  /** Re-run the query bypassing the result cache. */
  onRefresh?: () => void;
}) {
  const canChart =
    result.columns.includes(result.chart?.dimension) &&
    result.columns.includes(result.chart?.metric);
  const [mode, setMode] = useState<ChartMode>(canChart ? defaultMode : "table");
  const [parameters, setParameters] = useState(true);
  const [sqlOpen, setSqlOpen] = useState(false);
  return (
    <section className="report-card" aria-label="查询结果">
      <div className="report-heading">
        <h2>{result.title}</h2>
        <span className="report-meta">
          {result.rows.length} 条结果 · {result.elapsed_ms.toLocaleString()} ms
          {result.cached && (
            <span
              className="neutral-badge"
              title="结果来自查询结果缓存；需要最新数据时点击“刷新”重新执行"
            >
              缓存 · {formatAge(result.cache_age_seconds)}
            </span>
          )}
          {onRefresh && (
            <button
              type="button"
              className="text-button"
              title="跳过缓存重新执行"
              onClick={onRefresh}
            >
              <RefreshCw size={12} />
              刷新
            </button>
          )}
        </span>
      </div>
      {result.truncated && (
        <div className="info-banner" role="status">
          查询结果已截断，当前仅显示前 {result.rows.length.toLocaleString()}{" "}
          条记录。 请添加筛选条件或聚合后再查询。
        </div>
      )}
      <div className="chart-toolbar">
        <div className="segmented-control" role="group" aria-label="图表类型">
          {chartModes.map(({ mode: value, icon: Icon, label }) => (
            <button
              key={value}
              className={mode === value ? "selected" : ""}
              title={label}
              aria-label={label}
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
              disabled={!canChart && value !== "table"}
            >
              <Icon size={16} />
            </button>
          ))}
        </div>
        <button
          className="small-button"
          onClick={() => setParameters(!parameters)}
          aria-expanded={parameters}
        >
          分析参数{" "}
          <ChevronDown size={13} className={parameters ? "rotate" : ""} />
        </button>
      </div>
      {parameters && (
        <div className="analysis-parameters">
          <div>
            <span>维度</span>
            <code className="dimension-tag">
              {result.chart?.dimension || "无"}
            </code>
          </div>
          <div>
            <span>指标</span>
            <code className="metric-tag">
              {result.chart?.metric || "查询字段"}
            </code>
          </div>
        </div>
      )}
      <button
        className="sql-toggle"
        onClick={() => setSqlOpen(!sqlOpen)}
        aria-expanded={sqlOpen}
      >
        {sqlOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}生成的
        SQL
      </button>
      {sqlOpen && (
        <pre className="sql-code">
          <code>{result.sql}</code>
        </pre>
      )}
      <ResultChart result={result} mode={mode} />
      <div className="source-line">
        <DatabaseMark />
        {result.source}
        <span>·</span>
        {providerLabel(result.provider)}
        <span>·</span>
        {new Date(result.created_at).toLocaleString("zh-CN")}
      </div>
    </section>
  );
}
function DatabaseMark() {
  return <BarChart3 size={12} />;
}

type Phase = "idle" | "thinking" | "answered";
type Outcome = "pending" | "result" | "error";

export default function QueryPage({
  bootstrap,
  health,
  result,
  setResult,
  sources,
  sourceId,
  setSourceId,
}: {
  bootstrap: Bootstrap | null;
  health: Health | null;
  result: QueryResult | null;
  setResult: (result: QueryResult) => void;
  sources: DataSource[];
  sourceId: string;
  setSourceId: (id: string) => void;
}) {
  const [question, setQuestion] = useState("");
  const [submitted, setSubmitted] = useState(result?.title ?? DEFAULT_QUESTION);
  const [phase, setPhase] = useState<Phase>(result ? "answered" : "idle");
  const [steps, setSteps] = useState<string[]>(result?.steps ?? []);
  const [pendingSql, setPendingSql] = useState<{
    sql: string;
    title: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [stepsOpen, setStepsOpen] = useState(true);
  const [llm, setLlm] = useState<LlmStatus | null>(bootstrap?.llm ?? null);
  const [providers, setProviders] =
    useState<CatalogProvider[]>(FALLBACK_PROVIDERS);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<QueryResult[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const busy = phase === "thinking";
  const source = sources.find((item) => item.id === sourceId);
  const isLocal = sourceId === LOCAL_SAMPLE_ID;
  const defaults = useDefaultTables(isLocal ? undefined : source);
  const tableCount = isLocal
    ? bootstrap?.tables.length
    : (defaults.tables?.length ?? source?.last_test?.table_count);
  const links = bootstrap?.links;
  const chain = links
    ? `webapi ${linkPort(links.webapi)} / Polaris ${linkPort(links.polaris)}`
    : "webapi / Polaris";
  const online = isLocal
    ? health?.status === "ok"
    : source?.last_test?.status === "online";
  const examples = isLocal
    ? (bootstrap?.example_questions ?? [DEFAULT_QUESTION])
    : GENERIC_QUESTIONS;
  useEffect(() => {
    if (bootstrap?.llm) setLlm(bootstrap.llm);
  }, [bootstrap]);
  useEffect(() => {
    let active = true;
    void loadCatalog().then((items) => {
      if (active) setProviders(items);
    });
    return () => {
      active = false;
    };
  }, []);

  async function ask(value: string, refresh = false) {
    const text = value.trim();
    if (!text || busy) return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setPhase("thinking");
    setError("");
    setSteps([]);
    setPendingSql(null);
    setSubmitted(text);
    const body = { question: text, datasource_id: sourceId, refresh };
    let outcome: Outcome = "pending";
    function finish(answer: QueryResult) {
      outcome = "result";
      setResult(answer);
      setSteps((current) => (current.length ? current : answer.steps));
      setQuestion("");
      setPhase("answered");
    }
    function fail(message: string) {
      outcome = "error";
      setError(message);
      setPhase("idle");
    }
    try {
      await streamQuery(
        body,
        {
          onStep: (step) => setSteps((current) => [...current, step]),
          onSql: (sql, title) => setPendingSql({ sql, title }),
          onResult: finish,
          onError: fail,
        },
        abort.signal,
      );
      if (outcome === "pending") fail("流式查询已结束，但没有返回结果。");
    } catch (e) {
      if (abort.signal.aborted) return;
      if (!(e instanceof StreamUnavailableError)) {
        fail(errorMessage(e));
        return;
      }
      try {
        finish(await request<QueryResult>("/api/query", body));
      } catch (inner) {
        if (!abort.signal.aborted) fail(errorMessage(inner));
      }
    }
  }
  useEffect(() => {
    if (!result && sourceId === LOCAL_SAMPLE_ID) void ask(DEFAULT_QUESTION);
    return () => controller.current?.abort();
  }, []);
  async function loadHistory() {
    setHistoryOpen(true);
    setHistoryBusy(true);
    setHistoryError("");
    try {
      setHistory(
        (await request<{ items: QueryResult[] }>("/api/history")).items,
      );
    } catch (e) {
      setHistoryError(errorMessage(e));
    } finally {
      setHistoryBusy(false);
    }
  }
  const showSteps = busy || steps.length > 0;
  return (
    <div className="query-workspace">
      <div className="query-heading">
        <h1>智能问数 · LLM 生成 SQL 直查数据源</h1>
        <div className="query-status">
          <span>已注册 {tableCount ?? "—"} 张业务表</span>
          <span className="status-separator">·</span>
          <span className="status-links">链路: {chain}</span>
          <span className="status-separator">·</span>
          <span className="source-badge">SSE 流式</span>
          <SourceSelect
            id="query-source"
            compact
            sources={sources}
            value={sourceId}
            disabled={busy}
            onChange={(id) => {
              setSourceId(id);
              setError("");
            }}
          />
          <button
            type="button"
            className={`model-badge llm-model-button ${llm?.configured ? "" : "llm-model-off"}`}
            aria-haspopup="dialog"
            title={llm?.detail ?? "点击配置模型"}
            onClick={() => setSettingsOpen(true)}
          >
            <Sparkles size={12} />
            <span className="llm-model-name">
              {modelBadgeText(providers, llm)}
            </span>
            <Settings2 size={12} />
          </button>
          <button className="text-button" onClick={() => void loadHistory()}>
            <History size={13} />
            历史问答
          </button>
        </div>
      </div>
      <div className="conversation-scroll" aria-busy={busy}>
        <div className="question-row">
          <span>{submitted}</span>
        </div>
        {error && <ErrorBanner message={error} />}
        {showSteps && (
          <div className="thinking-panel">
            <button
              onClick={() => setStepsOpen(!stepsOpen)}
              aria-expanded={stepsOpen}
            >
              {stepsOpen ? (
                <ChevronDown size={13} />
              ) : (
                <ChevronRight size={13} />
              )}
              思考过程
              {busy && <LoaderCircle className="spin" size={12} />}
              <span>{steps.length} 步</span>
            </button>
            {stepsOpen && (
              <ol aria-live="polite">
                {steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
                {busy && (
                  <li className="thinking-live">
                    {steps.length
                      ? "继续分析并执行查询…"
                      : `正在连接「${source?.name ?? sourceId}」并生成 SQL…`}
                  </li>
                )}
              </ol>
            )}
            {stepsOpen && pendingSql && busy && (
              <pre className="sql-code">
                <code>{pendingSql.sql}</code>
              </pre>
            )}
          </div>
        )}
        {phase === "answered" && result && !error && (
          <QueryReport
            key={result.id}
            result={result}
            onRefresh={() => void ask(submitted, true)}
          />
        )}
        {phase === "idle" && !result && !error && (
          <Empty title="开始探索你的数据">
            选择下方示例问题，或输入一个数据问题。
          </Empty>
        )}
      </div>
      <div className="composer-section">
        <div className="example-questions">
          <span>试着问</span>
          {examples.slice(0, 4).map((example) => (
            <button
              key={example}
              disabled={busy}
              onClick={() => void ask(example)}
            >
              {example}
            </button>
          ))}
          <span className="connection-state">
            <i className={online ? "online" : ""} />
            {source?.name ?? (isLocal ? "本地 DuckDB" : sourceId)}
          </span>
        </div>
        <form
          className="question-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void ask(question);
          }}
        >
          <textarea
            aria-label="用自然语言提问"
            placeholder="用自然语言提问，Enter 发送 / Shift+Enter 换行"
            rows={1}
            value={question}
            disabled={busy}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void ask(question);
              }
            }}
          />
          <button
            className="primary-button"
            type="submit"
            disabled={busy || !question.trim()}
          >
            {busy ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <Send size={14} />
            )}
            发送
          </button>
        </form>
      </div>
      {historyOpen && (
        <Drawer
          id="history-dialog"
          title="历史问答"
          icon={<History size={17} />}
          onClose={() => setHistoryOpen(false)}
        >
          {historyBusy ? (
            <Loading />
          ) : (
            <>
              {historyError && <ErrorBanner message={historyError} />}
              {!historyError && !history.length && (
                <Empty title="还没有历史问答" />
              )}
              {history.map((item) => (
                <button
                  className="history-item"
                  key={item.id}
                  onClick={() => {
                    setResult(item);
                    setSteps(item.steps);
                    setPendingSql(null);
                    setSubmitted(item.title);
                    setError("");
                    setPhase("answered");
                    setHistoryOpen(false);
                  }}
                >
                  <strong>{item.title}</strong>
                  <span>
                    <Database size={12} />
                    {item.datasource_name || item.source}
                  </span>
                  <span>
                    <Clock3 size={12} />
                    {new Date(item.created_at).toLocaleString("zh-CN")}
                  </span>
                  <span>
                    <Code2 size={12} />
                    {item.rows.length} 条结果 · {providerLabel(item.provider)}
                  </span>
                </button>
              ))}
            </>
          )}
        </Drawer>
      )}
      {settingsOpen && (
        <LlmSettingsDrawer
          providers={providers}
          onClose={() => setSettingsOpen(false)}
          onSaved={setLlm}
        />
      )}
    </div>
  );
}

/** One model offered by a provider in GET /api/llm/catalog. */
interface CatalogModel {
  id: string;
  label: string;
}
/** Provider entry of GET /api/llm/catalog; an older backend does not serve it yet. */
interface CatalogProvider {
  id: string;
  label: string;
  needs_base_url: boolean;
  default_base_url: string;
  needs_api_key: boolean;
  default_model: string;
  hint: string;
  models: CatalogModel[];
}
const NONE_PROVIDER: CatalogProvider = {
  id: "none",
  label: "不使用模型（本地规则）",
  needs_base_url: false,
  default_base_url: "",
  needs_api_key: false,
  default_model: "",
  hint: "问数使用本地规则，仅支持示例数据的固定问题。",
  models: [],
};
/** Used until /api/llm/catalog answers: the two providers the backend always supported. */
const FALLBACK_PROVIDERS: CatalogProvider[] = [
  {
    id: "anthropic",
    label: "Anthropic Claude（官方 SDK）",
    needs_base_url: false,
    default_base_url: "",
    needs_api_key: true,
    default_model: "claude-opus-5",
    hint: "使用 Anthropic 官方 SDK 调用 Claude 模型；未填写 API Key 时会读取服务端的 ANTHROPIC_API_KEY 环境变量。",
    models: [
      { id: "claude-opus-5", label: "Claude Opus 5" },
      { id: "claude-fable-5", label: "Claude Fable 5" },
    ],
  },
  {
    id: "openai",
    label: "OpenAI 兼容接口（DeepSeek / Qwen / Ollama…）",
    needs_base_url: true,
    default_base_url: "",
    needs_api_key: true,
    default_model: "",
    hint: "任何提供 /v1/chat/completions 的服务；需要填写接口地址与模型名称。",
    models: [
      { id: "deepseek-chat", label: "DeepSeek Chat" },
      { id: "qwen-plus", label: "通义千问 qwen-plus" },
    ],
  },
  NONE_PROVIDER,
];
/** Sentinel value of the 模型 <select> that reveals the free-text model input. */
const CUSTOM_MODEL = "__custom__";

function normalizeModel(raw: unknown): CatalogModel | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  if (!id) return null;
  return {
    id,
    label: typeof item.label === "string" && item.label ? item.label : id,
  };
}
function apiKeyFlag(item: Record<string, unknown>, id: string): boolean {
  if ("needs_api_key" in item) return Boolean(item.needs_api_key);
  if ("requires_api_key" in item) return Boolean(item.requires_api_key);
  return id !== NONE_PROVIDER.id;
}
function normalizeProvider(raw: unknown): CatalogProvider | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  if (!id) return null;
  return {
    id,
    label: typeof item.label === "string" && item.label ? item.label : id,
    needs_base_url: Boolean(item.needs_base_url),
    default_base_url:
      typeof item.default_base_url === "string" ? item.default_base_url : "",
    // The backend spells this flag "requires_api_key"; accept both names.
    needs_api_key: apiKeyFlag(item, id),
    default_model:
      typeof item.default_model === "string" ? item.default_model : "",
    hint: typeof item.hint === "string" ? item.hint : "",
    models: Array.isArray(item.models)
      ? item.models
          .map(normalizeModel)
          .filter((model): model is CatalogModel => model !== null)
      : [],
  };
}
let catalogRequest: Promise<CatalogProvider[]> | null = null;
/** Reads the provider catalog once per page load; without the endpoint the fallback list is used. */
function loadCatalog(): Promise<CatalogProvider[]> {
  catalogRequest ??= request<{ providers?: unknown }>("/api/llm/catalog")
    .then((data) => {
      const items = Array.isArray(data.providers)
        ? data.providers
            .map(normalizeProvider)
            .filter((item): item is CatalogProvider => item !== null)
        : [];
      if (!items.length) return FALLBACK_PROVIDERS;
      return items.some((item) => item.id === NONE_PROVIDER.id)
        ? items
        : [...items, NONE_PROVIDER];
    })
    .catch(() => FALLBACK_PROVIDERS);
  return catalogRequest;
}
function findProvider(providers: CatalogProvider[], id: string) {
  return (
    providers.find((item) => item.id === id) ??
    providers.find((item) => item.id === NONE_PROVIDER.id) ??
    NONE_PROVIDER
  );
}
/** Drops the parenthesised explanation so the label fits in the header badge. */
function shortLabel(label: string) {
  return label.split(/[（(]/)[0].trim() || label;
}
function modelBadgeText(providers: CatalogProvider[], llm: LlmStatus | null) {
  if (!llm?.configured) return "本地规则（未配置模型）";
  const provider = providers.find((item) => item.id === llm.provider);
  return `${provider ? shortLabel(provider.label) : llm.provider} · ${llm.model}`;
}

interface LlmForm {
  provider: string;
  /** A model id from the catalog, or CUSTOM_MODEL when custom_model is used. */
  model: string;
  custom_model: string;
  base_url: string;
  api_key: string;
}
/** Selects `wanted` in the 模型 dropdown, or falls back to the custom input / first model. */
function pickModel(provider: CatalogProvider, wanted: string) {
  const target = wanted.trim();
  if (target) {
    return provider.models.some((model) => model.id === target)
      ? { model: target, custom_model: "" }
      : { model: CUSTOM_MODEL, custom_model: target };
  }
  const first = provider.models[0]?.id;
  return first
    ? { model: first, custom_model: "" }
    : { model: CUSTOM_MODEL, custom_model: "" };
}
function defaultForm(provider: CatalogProvider): LlmForm {
  return {
    provider: provider.id,
    ...pickModel(provider, provider.default_model),
    base_url: provider.default_base_url,
    api_key: "",
  };
}
function formFromStatus(
  status: LlmStatus,
  providers: CatalogProvider[],
): LlmForm {
  const provider = findProvider(providers, status.provider);
  return {
    provider: provider.id,
    ...pickModel(provider, status.model ?? ""),
    base_url: status.base_url ?? "",
    api_key: status.api_key_masked ? SECRET_MASK : "",
  };
}
function sameForm(a: LlmForm, b: LlmForm) {
  return (
    a.provider === b.provider &&
    a.model === b.model &&
    a.custom_model.trim() === b.custom_model.trim() &&
    a.base_url.trim() === b.base_url.trim() &&
    a.api_key === b.api_key
  );
}

function LlmSettingsDrawer({
  providers,
  onClose,
  onSaved,
}: {
  providers: CatalogProvider[];
  onClose: () => void;
  onSaved: (status: LlmStatus) => void;
}) {
  const [current, setCurrent] = useState<LlmStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<LlmForm>(() =>
    defaultForm(findProvider(providers, NONE_PROVIDER.id)),
  );
  const [busy, setBusy] = useState<"" | "save" | "test" | "models">("");
  /** Models reported by the provider itself, keyed by provider id. */
  const [liveModels, setLiveModels] = useState<Record<string, CatalogModel[]>>(
    {},
  );
  const [modelNotice, setModelNotice] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    // Read once when the drawer opens; the catalog is already loaded by then.
    request<LlmStatus>("/api/llm")
      .then((status) => {
        if (!alive.current) return;
        setCurrent(status);
        setForm(formFromStatus(status, providers));
      })
      .catch((e: unknown) => {
        if (alive.current) setError(errorMessage(e));
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
  }, []);

  const provider = findProvider(providers, form.provider);
  const isNone = provider.id === NONE_PROVIDER.id;
  // What the provider reported wins over the curated list, which stays as the
  // offline fallback so a model is always selectable.
  const modelOptions = liveModels[provider.id] ?? provider.models;
  const custom = form.model === CUSTOM_MODEL;
  const model = (custom ? form.custom_model : form.model).trim();
  // A stored endpoint stays visible even when the provider does not require one.
  const showBaseUrl =
    !isNone && (provider.needs_base_url || form.base_url.trim() !== "");
  const showApiKey = !isNone && provider.needs_api_key;
  const dirty =
    !!current && !sameForm(form, formFromStatus(current, providers));

  function update(patch: Partial<LlmForm>) {
    setForm((value) => ({ ...value, ...patch }));
    setNotice("");
  }
  /** Switching providers drops the previous provider's model, endpoint and key. */
  function chooseProvider(id: string) {
    const next = findProvider(providers, id);
    setForm(
      current && current.provider === next.id
        ? formFromStatus(current, providers)
        : defaultForm(next),
    );
    setNotice("");
    setError("");
    setModelNotice("");
    setTestResult(null);
  }
  /** Ask the provider which models it serves and merge them into the dropdown. */
  async function loadModels() {
    if (isNone || busy) return;
    setBusy("models");
    setError("");
    setModelNotice("");
    try {
      const data = await request<{
        models?: unknown;
        live?: boolean;
        detail?: string;
      }>("/api/llm/models", {
        provider: provider.id,
        base_url: form.base_url.trim(),
        api_key: form.api_key,
      });
      if (!alive.current) return;
      const listed = Array.isArray(data.models)
        ? data.models
            .map(normalizeModel)
            .filter((item): item is CatalogModel => !!item)
        : [];
      if (listed.length)
        setLiveModels((value) => ({ ...value, [provider.id]: listed }));
      setModelNotice(data.detail ?? "");
    } catch (e) {
      if (alive.current) setModelNotice(errorMessage(e));
    } finally {
      if (alive.current) setBusy("");
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!isNone && !model) {
      setError(custom ? "请填写自定义模型名称。" : "请选择模型。");
      return;
    }
    if (provider.needs_base_url && !form.base_url.trim()) {
      setError(`「${provider.label}」需要填写接口地址。`);
      return;
    }
    setBusy("save");
    setError("");
    setNotice("");
    setTestResult(null);
    try {
      const status = await request<LlmStatus>("/api/llm", {
        provider: provider.id,
        model: isNone ? "" : model,
        base_url: showBaseUrl ? form.base_url.trim() : "",
        api_key: showApiKey ? form.api_key : "",
      });
      if (!alive.current) return;
      setCurrent(status);
      setForm(formFromStatus(status, providers));
      onSaved(status);
      setNotice(
        status.configured
          ? `已保存：${shortLabel(provider.label)} · ${status.model}。可点击“测试连接”验证。`
          : `已保存：${status.detail}`,
      );
    } catch (e) {
      if (alive.current) setError(errorMessage(e));
    } finally {
      if (alive.current) setBusy("");
    }
  }
  async function test() {
    setBusy("test");
    setError("");
    setNotice("");
    setTestResult(null);
    try {
      const result = await request<LlmTestResult>("/api/llm/test", {});
      if (alive.current) setTestResult(result);
    } catch (e) {
      if (alive.current) setError(`模型连接失败：${errorMessage(e)}`);
    } finally {
      if (alive.current) setBusy("");
    }
  }
  return (
    <Drawer
      id="llm-settings"
      title="模型设置"
      icon={<Sparkles size={17} />}
      onClose={onClose}
      wide
      closeOnBackdrop={false}
    >
      <form
        className="drawer-form llm-settings"
        onSubmit={(event) => void save(event)}
      >
        <div className="drawer-body">
          {loading ? (
            <Loading text="正在读取模型配置…" />
          ) : (
            <>
              {current && (
                <div className="llm-status-card">
                  <span
                    className={
                      current.configured ? "healthy-badge" : "neutral-badge"
                    }
                  >
                    {current.configured ? "已配置" : "未配置"}
                  </span>
                  <div>
                    <strong>{modelBadgeText(providers, current)}</strong>
                    <p>{current.detail}</p>
                  </div>
                </div>
              )}
              <div className="llm-grid">
                <div className="field">
                  <label htmlFor="llm-provider">
                    服务商<b aria-label="必填">*</b>
                  </label>
                  <select
                    id="llm-provider"
                    value={form.provider}
                    onChange={(event) => chooseProvider(event.target.value)}
                  >
                    {providers.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </div>
                {!isNone && (
                  <div className="field">
                    <label htmlFor="llm-model">
                      模型<b aria-label="必填">*</b>
                    </label>
                    <select
                      id="llm-model"
                      value={form.model}
                      onChange={(event) =>
                        update({ model: event.target.value })
                      }
                    >
                      {modelOptions.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}
                        </option>
                      ))}
                      <option value={CUSTOM_MODEL}>自定义…</option>
                    </select>
                    <div className="llm-model-actions">
                      <button
                        type="button"
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => void loadModels()}
                      >
                        <RefreshCw
                          size={13}
                          className={busy === "models" ? "spin" : ""}
                        />
                        {busy === "models" ? "读取中…" : "读取服务商模型列表"}
                      </button>
                      <span className="muted">
                        {modelNotice ||
                          `共 ${modelOptions.length} 个可选模型，可读取服务商的完整列表。`}
                      </span>
                    </div>
                  </div>
                )}
                {!isNone && custom && (
                  <div className="field llm-span">
                    <label htmlFor="llm-model-custom">
                      自定义模型名称<b aria-label="必填">*</b>
                    </label>
                    <input
                      id="llm-model-custom"
                      value={form.custom_model}
                      spellCheck={false}
                      autoComplete="off"
                      aria-required="true"
                      placeholder="填写服务商接口使用的模型 ID"
                      onChange={(event) =>
                        update({ custom_model: event.target.value })
                      }
                    />
                  </div>
                )}
                {showBaseUrl && (
                  <div className="field llm-span">
                    <label htmlFor="llm-base-url">
                      接口地址
                      {provider.needs_base_url && <b aria-label="必填">*</b>}
                    </label>
                    <input
                      id="llm-base-url"
                      value={form.base_url}
                      spellCheck={false}
                      autoComplete="off"
                      aria-required={provider.needs_base_url}
                      placeholder="https://api.deepseek.com/v1"
                      onChange={(event) =>
                        update({ base_url: event.target.value })
                      }
                    />
                    <small className="llm-note">
                      {provider.needs_base_url
                        ? "指向服务的 /v1 根地址，不要带 /chat/completions。"
                        : "留空即使用服务商的默认地址。"}
                    </small>
                  </div>
                )}
                {showApiKey && (
                  <div className="field llm-span">
                    <label htmlFor="llm-api-key">API Key</label>
                    <input
                      id="llm-api-key"
                      type="password"
                      value={form.api_key}
                      autoComplete="new-password"
                      onFocus={(event) => {
                        if (form.api_key === SECRET_MASK) event.target.select();
                      }}
                      onChange={(event) =>
                        update({ api_key: event.target.value })
                      }
                    />
                    <small className="llm-note">
                      {form.api_key === SECRET_MASK
                        ? `保持 ${SECRET_MASK} 表示沿用已保存的密钥，清空则删除。`
                        : "密钥只保存在本地服务，用于调用模型接口。"}
                    </small>
                  </div>
                )}
              </div>
              {provider.hint && <p className="llm-hint">{provider.hint}</p>}
              <div className="llm-banners">
                {error && <ErrorBanner message={error} />}
                {notice && (
                  <div className="success-banner" role="status">
                    {notice}
                  </div>
                )}
                {testResult && (
                  <div
                    className={`${testResult.ok ? "success-banner" : "error-banner"} llm-result`}
                    role="status"
                  >
                    <div>
                      <strong>
                        {testResult.ok ? "模型连接成功" : "模型连接失败"} ·{" "}
                        {Math.round(testResult.latency_ms)} ms
                        {testResult.model ? ` · ${testResult.model}` : ""}
                      </strong>
                      <p>{testResult.detail}</p>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
        <div className="drawer-footer llm-footer">
          <button
            type="button"
            className="secondary-button"
            onClick={() => void test()}
            disabled={!!busy || loading}
          >
            <Plug size={14} />
            {busy === "test" ? "测试中…" : "测试连接"}
          </button>
          <button
            type="submit"
            className="primary-button"
            disabled={!!busy || loading}
          >
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
          <span className="muted">
            {dirty
              ? "配置已修改；测试连接使用已保存的配置，请先保存。"
              : "测试连接会用已保存的配置真实调用一次模型。"}
          </span>
        </div>
      </form>
    </Drawer>
  );
}
