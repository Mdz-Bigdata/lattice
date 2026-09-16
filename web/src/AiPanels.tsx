// SPDX-License-Identifier: Apache-2.0
/**
 * AI 增强 panels: rule suggestions for a table, a semantic model draft from a set of
 * tables, and lineage root-cause analysis for an asset. Each works without a model
 * (heuristics) and uses the configured model when there is one.
 */
import { useEffect, useState } from "react";
import { Sparkles, Wand2 } from "lucide-react";
import { errorMessage, request, type DataSource } from "./api";
import { Drawer, Empty, ErrorBanner, Loading } from "./components";
import { SourceSelect, useSchemas, useTables } from "./SourceBrowser";

interface AiStatus {
  model_configured: boolean;
  provider: string;
  model: string;
}
interface Suggestion {
  name: string;
  metric: string;
  column_name: string | null;
  config: Record<string, unknown>;
  expected_type: string;
  result_formula: string;
  operator: string;
  threshold: number;
  level: string;
  reason: string;
  source: string;
  valid: boolean;
  message: string;
  rule: Record<string, unknown> | null;
}
interface RuleAnswer {
  table: { table_name: string; schema_name: string; rows: number | null };
  suggestions: Suggestion[];
  used_model: boolean;
  provider: string;
  model: string;
}

export function useAiStatus(): AiStatus | null {
  const [status, setStatus] = useState<AiStatus | null>(null);
  useEffect(() => {
    request<AiStatus>("/api/ai/status").then(setStatus).catch(() => setStatus({ model_configured: false, provider: "", model: "" }));
  }, []);
  return status;
}

function ModelNote({ status }: { status: AiStatus | null }) {
  if (!status) return null;
  return (
    <p className="muted">
      {status.model_configured
        ? `已配置模型 ${status.provider} / ${status.model}：规则建议由启发式规则与模型共同给出。`
        : "未配置模型：仅按字段命名与类型给出启发式建议；在「智能问数 → 模型设置」配置模型后会更完整。"}
    </p>
  );
}

/** 规则推荐: pick a table, review the proposals, create the ones you keep. */
export function AiRulesDrawer({
  sources,
  sourceId,
  onClose,
  onCreated,
}: {
  sources: DataSource[];
  sourceId: string;
  onClose: () => void;
  onCreated: (count: number) => void;
}) {
  const status = useAiStatus();
  const [datasource, setDatasource] = useState(sourceId);
  const [schema, setSchema] = useState("");
  const [table, setTable] = useState("");
  const [useModel, setUseModel] = useState(true);
  const [answer, setAnswer] = useState<RuleAnswer | null>(null);
  const [chosen, setChosen] = useState<number[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const source = sources.find((item) => item.id === datasource);
  const schemas = useSchemas(source);
  const tables = useTables(source, schema);
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema)) setSchema(items[0]?.name ?? "");
  }, [schemas.data]);
  async function suggest() {
    if (!table) return setError("请选择数据表。");
    setBusy("suggest");
    setError("");
    setAnswer(null);
    try {
      const result = await request<RuleAnswer>("/api/ai/quality-rules/suggest", { datasource_id: datasource, schema_name: schema || null, table_name: table, use_model: useModel });
      setAnswer(result);
      setChosen(result.suggestions.map((item, index) => (item.valid ? index : -1)).filter((index) => index >= 0));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function apply() {
    if (!answer || !chosen.length) return;
    setBusy("apply");
    setError("");
    try {
      const outcome = await request<{ created: unknown[]; errors: { name: string; message: string }[] }>("/api/ai/quality-rules/apply", {
        rules: chosen.map((index) => answer.suggestions[index].rule).filter(Boolean),
      });
      if (outcome.errors.length) setError(`部分规则未能创建：${outcome.errors.map((item) => `${item.name}：${item.message}`).join("；")}`);
      onCreated(outcome.created.length);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <Drawer id="ai-rules" title="AI 推荐核查规则" icon={<Sparkles size={17} />} onClose={onClose} wide closeOnBackdrop={false}>
      <div className="quality-drawer-body">
        <ModelNote status={status} />
        <div className="quality-field-row">
          <div className="quality-field">
            <label htmlFor="ai-rules-source">数据源</label>
            <SourceSelect id="ai-rules-source" sources={sources} value={datasource} disabled={!!busy} onChange={setDatasource} />
          </div>
          <div className="quality-field">
            <label htmlFor="ai-rules-schema">Schema</label>
            <select id="ai-rules-schema" value={schema} disabled={!schemas.data} onChange={(event) => setSchema(event.target.value)}>
              {(schemas.data?.items ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="quality-field">
            <label htmlFor="ai-rules-table">数据表</label>
            <select id="ai-rules-table" value={table} disabled={!tables.data} onChange={(event) => setTable(event.target.value)}>
              <option value="">请选择</option>
              {(tables.data?.items ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="button-row">
          <label className="checkbox-row">
            <input type="checkbox" checked={useModel} disabled={!status?.model_configured} onChange={(event) => setUseModel(event.target.checked)} />
            使用模型补充建议
          </label>
          <button type="button" className="primary-button" disabled={!!busy || !table} onClick={() => void suggest()}>
            <Wand2 size={14} />
            {busy === "suggest" ? "分析中…" : "生成建议"}
          </button>
        </div>
        {error && <ErrorBanner message={error} />}
        {busy === "suggest" && <Loading text="正在分析表结构…" />}
        {answer && (
          <>
            <p className="muted">
              {answer.table.schema_name ? `${answer.table.schema_name}.` : ""}
              {answer.table.table_name} · {answer.suggestions.length} 条建议{answer.used_model ? `（含模型 ${answer.model} 的建议）` : "（启发式）"}
            </p>
            {answer.suggestions.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th />
                      <th>规则</th>
                      <th>核查</th>
                      <th>字段</th>
                      <th>判定</th>
                      <th>理由</th>
                      <th>来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    {answer.suggestions.map((item, index) => (
                      <tr key={`${item.metric}-${item.column_name ?? ""}-${index}`} className={item.valid ? "" : "ai-invalid"}>
                        <td>
                          <input
                            type="checkbox"
                            checked={chosen.includes(index)}
                            disabled={!item.valid}
                            onChange={(event) => setChosen((current) => (event.target.checked ? [...current, index] : current.filter((i) => i !== index)))}
                          />
                        </td>
                        <td>{item.name}</td>
                        <td>
                          <code>{item.metric}</code>
                          {Object.keys(item.config).length > 0 && <small className="muted"> {JSON.stringify(item.config)}</small>}
                        </td>
                        <td>{item.column_name || "整表"}</td>
                        <td>
                          {item.result_formula === "percentage" ? "百分比" : "数量"} {item.operator} {item.threshold} · {item.level}
                        </td>
                        <td className="metric-desc">{item.valid ? item.reason : `无法创建：${item.message}`}</td>
                        <td>
                          <span className="neutral-badge">{item.source === "model" ? "模型" : "启发式"}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="没有可推荐的规则">这张表的字段没有匹配到任何启发式规则；配置模型后可获得更多建议。</Empty>
            )}
            <div className="button-row">
              <button type="button" className="primary-button" disabled={!!busy || !chosen.length} onClick={() => void apply()}>
                {busy === "apply" ? "创建中…" : `创建所选 ${chosen.length} 条规则`}
              </button>
              <button type="button" className="text-button" onClick={onClose}>
                关闭
              </button>
            </div>
          </>
        )}
      </div>
    </Drawer>
  );
}

interface Draft {
  name: string;
  yaml: string;
  valid: boolean;
  errors: string[];
  warnings: string[];
  summary: { datasets: number; relationships: number; metrics: number };
  used_model: boolean;
  model: string;
}

/** 语义模型草稿: choose tables, get an Ossie model, load it into the editor. */
export function AiModelDrawer({
  sources,
  sourceId,
  onClose,
  onLoad,
}: {
  sources: DataSource[];
  sourceId: string;
  onClose: () => void;
  onLoad: (name: string, yaml: string) => void;
}) {
  const status = useAiStatus();
  const [datasource, setDatasource] = useState(sourceId);
  const [schema, setSchema] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [useModel, setUseModel] = useState(true);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const source = sources.find((item) => item.id === datasource);
  const schemas = useSchemas(source);
  const tables = useTables(source, schema);
  useEffect(() => {
    if (!schemas.data) return;
    const items = schemas.data.items;
    if (!items.some((item) => item.name === schema)) setSchema(items[0]?.name ?? "");
  }, [schemas.data]);
  useEffect(() => {
    setPicked([]);
  }, [datasource, schema]);
  async function generate() {
    if (!picked.length) return setError("请至少勾选一张表。");
    setBusy(true);
    setError("");
    try {
      setDraft(await request<Draft>("/api/ai/semantic-model/suggest", { datasource_id: datasource, schema_name: schema || null, tables: picked, name: name.trim() || null, use_model: useModel }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  const available = tables.data?.items ?? [];
  return (
    <Drawer id="ai-model" title="AI 生成语义模型草稿" icon={<Sparkles size={17} />} onClose={onClose} wide closeOnBackdrop={false}>
      <div className="quality-drawer-body">
        <ModelNote status={status} />
        <div className="quality-field-row">
          <div className="quality-field">
            <label htmlFor="ai-model-source">数据源</label>
            <SourceSelect id="ai-model-source" sources={sources} value={datasource} disabled={busy} onChange={setDatasource} />
          </div>
          <div className="quality-field">
            <label htmlFor="ai-model-schema">Schema</label>
            <select id="ai-model-schema" value={schema} disabled={!schemas.data} onChange={(event) => setSchema(event.target.value)}>
              {(schemas.data?.items ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="quality-field">
            <label htmlFor="ai-model-name">模型名称</label>
            <input id="ai-model-name" value={name} placeholder="留空自动命名" onChange={(event) => setName(event.target.value)} />
          </div>
        </div>
        <div className="quality-field">
          <label>数据表（勾选要纳入模型的表，关系按主键 / 外键命名推断）</label>
          <div className="quality-check-list">
            <label className="quality-check-all">
              <input type="checkbox" checked={available.length > 0 && picked.length === available.length} disabled={!available.length} onChange={(event) => setPicked(event.target.checked ? available.map((item) => item.name) : [])} />
              全选
            </label>
            {available.map((item) => (
              <label key={item.name}>
                <input type="checkbox" checked={picked.includes(item.name)} onChange={() => setPicked((current) => (current.includes(item.name) ? current.filter((n) => n !== item.name) : [...current, item.name]))} />
                {item.name}
              </label>
            ))}
          </div>
        </div>
        <div className="button-row">
          <label className="checkbox-row">
            <input type="checkbox" checked={useModel} disabled={!status?.model_configured} onChange={(event) => setUseModel(event.target.checked)} />
            让模型补充描述、同义词与指标
          </label>
          <button type="button" className="primary-button" disabled={busy || !picked.length} onClick={() => void generate()}>
            <Wand2 size={14} />
            {busy ? "生成中…" : "生成草稿"}
          </button>
        </div>
        {error && <ErrorBanner message={error} />}
        {draft && (
          <>
            <div className={draft.valid ? "info-banner" : "error-banner"} role="status">
              {draft.valid ? "草稿通过 Ossie 校验" : "草稿未通过校验，仍可载入编辑器修改"}：{draft.summary.datasets} 个数据集、{draft.summary.relationships} 条关系、{draft.summary.metrics} 个指标
              {draft.used_model ? `（模型 ${draft.model} 已补充语义）` : ""}
            </div>
            {draft.errors.length > 0 && <ErrorBanner message={draft.errors.join("；")} />}
            <pre className="sql-code ai-yaml">{draft.yaml}</pre>
            <div className="button-row">
              <button type="button" className="primary-button" onClick={() => onLoad(draft.name, draft.yaml)}>
                载入编辑器
              </button>
              <button type="button" className="text-button" onClick={onClose}>
                关闭
              </button>
            </div>
          </>
        )}
      </div>
    </Drawer>
  );
}

interface Suspect {
  fqn: string;
  name: string;
  entity_type: string;
  depth: number;
  score: number;
  signals: { kind: string; label: string; detail: string }[];
}
interface RootCause {
  target: { fqn: string; name: string; entity_type: string };
  window_days: number;
  upstream_count: number;
  suspects: Suspect[];
  signals_checked: string[];
  summary: string;
  used_model: boolean;
  model: string;
}

/** 根因分析 for one asset, shown beside impact analysis on the lineage page. */
export function RootCausePanel({ entityType, fqn }: { entityType: string; fqn: string }) {
  const status = useAiStatus();
  const [days, setDays] = useState(7);
  const [report, setReport] = useState<RootCause | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function analyse() {
    setBusy(true);
    setError("");
    try {
      setReport(await request<RootCause>("/api/ai/root-cause", { entity_type: entityType, ref: fqn, days, use_model: !!status?.model_configured }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="md-panel">
      <h2>根因分析</h2>
      <p className="muted">沿上游血缘收集质量核查失败、表结构变更、资产下线、拾取失败与近期修改，按强度和距离排序。</p>
      <div className="button-row">
        <label htmlFor="rc-days">最近</label>
        <select id="rc-days" value={days} onChange={(event) => setDays(Number(event.target.value))}>
          {[1, 3, 7, 14, 30].map((value) => (
            <option key={value} value={value}>
              {value} 天
            </option>
          ))}
        </select>
        <button type="button" className="primary-button" disabled={busy} onClick={() => void analyse()}>
          <Sparkles size={14} />
          {busy ? "分析中…" : "开始分析"}
        </button>
      </div>
      {error && <ErrorBanner message={error} />}
      {report && (
        <>
          <div className="info-banner" role="status">
            {report.summary}
            {report.used_model ? <small>（模型 {report.model} 撰写）</small> : null}
          </div>
          <ul className="rc-list">
            {report.suspects.map((suspect) => (
              <li key={suspect.fqn} className={suspect.signals.length ? "flagged" : ""}>
                <div className="rc-head">
                  <code>{suspect.fqn}</code>
                  <span className="neutral-badge">{suspect.depth === 0 ? "目标" : `上游 ${Math.abs(suspect.depth)} 层`}</span>
                  <b>{suspect.score}</b>
                </div>
                {suspect.signals.length ? (
                  <ul>
                    {suspect.signals.map((signal, index) => (
                      <li key={index}>
                        <span className={signal.kind === "deleted" || signal.kind === "quality" ? "failure-badge" : signal.kind === "edit" ? "neutral-badge" : "quality-warn-badge"}>{signal.label}</span> {signal.detail}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <small className="muted">没有信号</small>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
