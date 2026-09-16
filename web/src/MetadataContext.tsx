// SPDX-License-Identifier: Apache-2.0
/**
 * AI 上下文: the catalog as a context layer for people, assistants and agents. Four views: the
 * catalog assistant (a question answered from catalog context by the configured model), the MCP
 * server with ready-to-paste client configurations, a tool playground, and the business semantics
 * each data source hands to 智能问数.
 */
import { useState } from "react";
import { ArrowRight, Bot, Play, Sparkles, X } from "lucide-react";
import { errorMessage, type LlmStatus } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { SourceSelect, useApi } from "./SourceBrowser";
import {
  entityName,
  mdPost,
  metadataPath,
  navigate,
  typeLabel,
  type AskResult,
  type Entity,
  type McpStatus,
  type SearchResult,
  type ToolDefinition,
} from "./metadataApi";
import { CopyButton, EntityIcon, EntityLink, MarkdownView, SearchBox, useDebounced } from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";

const EXAMPLES = [
  "订单表的 status 字段是什么含义？",
  "哪些表包含个人敏感信息（PII）？",
  "GMV 的业务口径是什么，对应哪些表和字段？",
  "修改订单表会影响哪些下游对象，应该通知谁？",
];

export default function ContextPage(props: MetadataViewProps) {
  const tab = ["mcp", "tools", "semantics"].includes(props.route.parts[0] ?? "") ? props.route.parts[0] : "assistant";
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>AI 上下文</h1>
          <p>面向人、AI 助手与智能代理的数据上下文层：业务语义、术语定义、责任人与血缘，通过助手、智能问数与 MCP 服务提供</p>
        </div>
      </div>
      <nav className="quality-tabs md-tabs" role="tablist" aria-label="AI 上下文">
        {[["assistant", "元数据助手"], ["mcp", "MCP 服务"], ["tools", "工具调试"], ["semantics", "数据源语义"]].map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={`quality-tab ${tab === id ? "active" : ""}`} onClick={() => navigate("context", id === "assistant" ? undefined : id)}>
            {label}
          </button>
        ))}
      </nav>
      {tab === "assistant" && <AssistantView {...props} />}
      {tab === "mcp" && <McpView />}
      {tab === "tools" && <ToolsView />}
      {tab === "semantics" && <SemanticsView {...props} />}
    </div>
  );
}

function FocusPicker({ value, onChange }: { value: Entity | null; onChange: (entity: Entity | null) => void }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query, 250);
  const result = useApi<SearchResult>(q ? metadataPath("/search", { q, size: 6 }) : null);
  if (value)
    return (
      <div className="md-chosen">
        <EntityIcon type={value.entity_type} size={14} />
        <span>{value.fqn}</span>
        <button type="button" className="icon-button" aria-label="取消关注对象" onClick={() => onChange(null)}>
          <X size={13} />
        </button>
      </div>
    );
  return (
    <div className="md-focus-picker">
      <SearchBox value={query} onChange={setQuery} placeholder="可选：限定到某个数据资产" />
      {q && (
        <div className="md-picker-list">
          {(result.data?.items ?? []).map((item) => (
            <button type="button" key={item.id} className="md-picker-item md-picker-button" onClick={() => { onChange(item); setQuery(""); }}>
              <EntityIcon type={item.entity_type} size={14} />
              <span>
                <strong>{entityName(item)}</strong>
                <small>{typeLabel(item.entity_type)} · {item.fqn}</small>
              </span>
            </button>
          ))}
          {result.data && !result.data.items.length && <p className="muted">没有匹配的资产</p>}
        </div>
      )}
    </div>
  );
}

function AssistantView({ sources, goTo }: MetadataViewProps) {
  const llm = useApi<LlmStatus>("/api/llm");
  const [question, setQuestion] = useState("");
  const [focus, setFocus] = useState<Entity | null>(null);
  const [datasource, setDatasource] = useState("");
  const [answer, setAnswer] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function ask(text = question) {
    if (!text.trim()) return;
    setBusy(true);
    setError("");
    try {
      setAnswer(
        await mdPost<AskResult>("/context/ask", {
          question: text.trim(),
          ...(focus ? { entity_type: focus.entity_type, ref: focus.fqn } : {}),
          ...(datasource ? { datasource_id: datasource } : {}),
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="md-context">
      <section className="panel">
        <div className="panel-body md-ask">
          <div className="md-ask-status">
            <Bot size={15} />
            {llm.data?.configured ? (
              <span>
                使用 {llm.data.provider} · {llm.data.model} 回答；只依据目录中的元数据，不读取业务数据。
              </span>
            ) : (
              <span>
                尚未配置模型：仍可查看助手会检索到的目录上下文。
                <button type="button" className="text-button" onClick={() => goTo("questions")}>
                  去配置模型 <ArrowRight size={12} />
                </button>
              </span>
            )}
          </div>
          <textarea
            rows={3}
            value={question}
            placeholder="用自然语言询问数据资产的含义、口径、责任人或影响范围…"
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void ask();
            }}
          />
          <div className="md-ask-options">
            <FocusPicker value={focus} onChange={setFocus} />
            <div className="md-ask-source">
              <SourceSelect id="context-source" sources={sources} value={datasource} onChange={setDatasource} label="数据源语义（可选）" />
              {datasource && (
                <button type="button" className="icon-button" aria-label="不使用数据源语义" onClick={() => setDatasource("")}>
                  <X size={13} />
                </button>
              )}
            </div>
            <button type="button" className="primary-button" disabled={busy || !question.trim()} onClick={() => void ask()}>
              <Sparkles size={14} />
              {busy ? "检索中…" : "提问"}
            </button>
          </div>
          <div className="example-questions">
            <span>试试：</span>
            {EXAMPLES.map((example) => (
              <button key={example} type="button" onClick={() => { setQuestion(example); void ask(example); }}>
                {example}
              </button>
            ))}
          </div>
          {error && <ErrorBanner message={error} />}
        </div>
      </section>
      {busy && !answer && <Loading text="正在检索目录上下文…" />}
      {answer && (
        <div className="md-answer">
          <section className="panel">
            <div className="panel-heading">
              <h2>回答</h2>
              {answer.configured && <span>{answer.model}</span>}
            </div>
            <div className="panel-body">
              {answer.answer ? <MarkdownView text={answer.answer} /> : <p className="muted">{answer.detail || "模型没有给出回答。"}</p>}
            </div>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>引用的目录上下文</h2>
              <CopyButton text={answer.context.markdown} label="复制上下文" />
            </div>
            <div className="panel-body">
              {answer.context.entities.length > 0 && (
                <ul className="md-impact-list">
                  {answer.context.entities.map((hit) => (
                    <li key={hit.fqn}>
                      <EntityIcon type={hit.entity_type} size={14} />
                      <EntityLink entity={hit}>{hit.fqn}</EntityLink>
                      <span className="muted">{hit.description.slice(0, 80)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {answer.context.terms.length > 0 && (
                <ul className="md-impact-list">
                  {answer.context.terms.map((term) => (
                    <li key={term.fqn}>
                      <EntityIcon type="glossaryTerm" size={14} />
                      <EntityLink entity={{ entity_type: "glossaryTerm", fqn: term.fqn, display_name: term.display_name }} />
                      <span className="muted">{term.description.slice(0, 80)}</span>
                    </li>
                  ))}
                </ul>
              )}
              <details className="md-context-details">
                <summary>查看完整上下文</summary>
                <MarkdownView className="md-context-card" text={answer.context.markdown} />
              </details>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function McpView() {
  const status = useApi<McpStatus>(metadataPath("/context/mcp"));
  if (status.loading) return <Loading />;
  if (status.error) return <ErrorBanner message={status.error} />;
  const data = status.data;
  if (!data) return null;
  const snippets: [string, string][] = [
    ["Claude Code", String(data.clients.claude_code ?? "")],
    ["Claude Desktop（通过 mcp-remote）", JSON.stringify(data.clients.claude_desktop, null, 2)],
    ["Cursor / 支持远程 MCP 的客户端", JSON.stringify(data.clients.cursor, null, 2)],
  ];
  return (
    <div className="md-context">
      <section className="panel">
        <div className="panel-heading">
          <h2>MCP 服务</h2>
          <span className="healthy-badge">运行中</span>
        </div>
        <div className="panel-body">
          <div className="md-stat-grid">
            <div className="md-stat">
              <span>端点（Streamable HTTP）</span>
              <strong className="md-stat-code">{data.url}</strong>
            </div>
            <div className="md-stat">
              <span>协议版本</span>
              <strong className="md-stat-code">{data.protocol_versions.join(" / ")}</strong>
            </div>
            <div className="md-stat">
              <span>会话 · 工具调用</span>
              <strong>
                {data.sessions} · {data.calls}
              </strong>
            </div>
          </div>
          <p className="muted">
            服务只监听本机回环地址，拒绝跨来源请求。写操作（修改描述与标签、创建术语、登记血缘）以用户 <code>{data.user}</code> 的身份执行并记录到活动信息流；<code>query_datasource</code> 与 SQL 工作台使用同一套只读校验。
          </p>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>接入客户端</h2>
        </div>
        <div className="panel-body md-snippets">
          {snippets.map(([title, text]) => (
            <div key={title} className="md-snippet">
              <div>
                <strong>{title}</strong>
                <CopyButton text={text} />
              </div>
              <pre className="sql-code">{text}</pre>
            </div>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>工具</h2>
          <span>{data.tool_definitions.length} 个</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>说明</th>
                <th>参数</th>
              </tr>
            </thead>
            <tbody>
              {data.tool_definitions.map((tool) => (
                <tr key={tool.name}>
                  <td>
                    <code>{tool.name}</code>
                  </td>
                  <td>{tool.description}</td>
                  <td>
                    {Object.keys(tool.inputSchema.properties ?? {}).map((name) => (
                      <code key={name} className={`md-param ${(tool.inputSchema.required ?? []).includes(name) ? "required" : ""}`}>
                        {name}
                      </code>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <div className="md-viz-columns">
        <section className="panel">
          <div className="panel-heading">
            <h2>资源</h2>
          </div>
          <ul className="md-impact-list panel-body">
            {data.resources.map((item) => (
              <li key={item.uri}>
                <code>{item.uri}</code>
                <span className="muted">{item.description}</span>
              </li>
            ))}
            {data.resource_templates.map((item) => (
              <li key={item.uriTemplate}>
                <code>{item.uriTemplate}</code>
                <span className="muted">{item.description}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>提示模板</h2>
          </div>
          <ul className="md-impact-list panel-body">
            {data.prompts.map((item) => (
              <li key={item.name}>
                <code>{item.name}</code>
                <span className="muted">{item.description}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

function example(tool: ToolDefinition): string {
  const samples: Record<string, unknown> = {
    query: "订单",
    fqn: "local-sample.sample.main.t_lattice_orders",
    datasource_id: "local-sample",
    sql: "SELECT status, count(*) AS n FROM t_lattice_orders GROUP BY status",
    glossary: "Sales",
    name: "GMV",
    description: "成交总额",
    from_fqn: "local-sample.sample.main.t_lattice_order_items",
    to_fqn: "local-sample.sample.main.t_lattice_orders",
  };
  const args: Record<string, unknown> = {};
  for (const name of tool.inputSchema.required ?? []) args[name] = samples[name] ?? "";
  return JSON.stringify(args, null, 2);
}

function ToolsView() {
  const tools = useApi<{ mcp: ToolDefinition[]; openai: unknown[]; anthropic: unknown[] }>(metadataPath("/context/tools"));
  const [name, setName] = useState("");
  const [args, setArgs] = useState("{}");
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const list = tools.data?.mcp ?? [];
  const tool = list.find((item) => item.name === name) ?? list[0];
  async function run() {
    if (!tool) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const parsed: unknown = JSON.parse(args || "{}");
      setResult(await mdPost("/context/tools/call", { name: tool.name, arguments: parsed }));
    } catch (e) {
      setError(e instanceof SyntaxError ? `参数不是合法的 JSON：${e.message}` : errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  if (tools.loading) return <Loading />;
  if (tools.error) return <ErrorBanner message={tools.error} />;
  return (
    <div className="md-context">
      <section className="panel">
        <div className="panel-heading">
          <h2>调用工具</h2>
          <span>与 MCP tools/call 使用同一实现</span>
        </div>
        <div className="panel-body md-tool-form">
          <label className="md-form-row">
            <span>工具</span>
            <select
              value={tool?.name ?? ""}
              onChange={(event) => {
                setName(event.target.value);
                const next = list.find((item) => item.name === event.target.value);
                if (next) setArgs(example(next));
              }}
            >
              {list.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          {tool && <p className="muted">{tool.description}</p>}
          <label className="md-form-row">
            <span>参数（JSON）</span>
            <textarea className="code-editor" rows={6} value={args} onChange={(event) => setArgs(event.target.value)} />
          </label>
          <div className="md-form-actions">
            {tool && (
              <button type="button" className="text-button" onClick={() => setArgs(example(tool))}>
                填入示例参数
              </button>
            )}
            <button type="button" className="primary-button" disabled={busy || !tool} onClick={() => void run()}>
              <Play size={13} />
              {busy ? "调用中…" : "调用"}
            </button>
          </div>
          {error && <ErrorBanner message={error} />}
          {result !== null && <pre className="md-json md-result-json">{JSON.stringify(result, null, 2)}</pre>}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>函数调用格式</h2>
          <span>可直接用于 OpenAI 兼容接口或 Anthropic Messages API 的 tools 参数</span>
        </div>
        <div className="panel-body md-snippets">
          {[["OpenAI 兼容（tools）", tools.data?.openai], ["Anthropic（tools）", tools.data?.anthropic]].map(([title, value]) => {
            const text = JSON.stringify(value, null, 2);
            return (
              <div key={String(title)} className="md-snippet">
                <div>
                  <strong>{String(title)}</strong>
                  <CopyButton text={text} />
                </div>
                <pre className="sql-code md-snippet-long">{text}</pre>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function SemanticsView({ sources, sourceId, setSourceId }: MetadataViewProps) {
  const [datasource, setDatasource] = useState(sourceId);
  const context = useApi<{ name: string; schema: string; semantics: string }>(datasource ? metadataPath("/context/datasource", { datasource_id: datasource }) : null);
  return (
    <div className="md-context">
      <section className="panel">
        <div className="panel-body md-form-inline">
          <div className="md-form-row">
            <SourceSelect
              id="semantics-source"
              sources={sources}
              value={datasource}
              onChange={(id) => {
                setDatasource(id);
                setSourceId(id);
              }}
            />
          </div>
          <p className="muted">
            智能问数把「表结构」与「业务语义」一起交给模型。业务语义来自元数据目录：表与字段描述、责任人、分级、标签和术语定义；在资产详情页补充描述后，模型生成 SQL 时即可使用。
          </p>
        </div>
      </section>
      {context.loading && <Loading text="正在读取表结构与业务语义…" />}
      {context.error && <ErrorBanner message={context.error} />}
      {context.data && (
        <div className="md-viz-columns">
          <section className="panel">
            <div className="panel-heading">
              <h2>表结构</h2>
              <CopyButton text={context.data.schema} />
            </div>
            <pre className="md-json md-semantics">{context.data.schema || "（未能读取表结构）"}</pre>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>业务语义</h2>
              <CopyButton text={context.data.semantics} />
            </div>
            {context.data.semantics ? (
              <pre className="md-json md-semantics">{context.data.semantics}</pre>
            ) : (
              <Empty title="该数据源还没有业务语义">先在「元数据拾取」中拾取该数据源，再为表和字段补充描述、术语与责任人。</Empty>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
