// SPDX-License-Identifier: Apache-2.0
/**
 * 运行观测: request metrics per route, component health, structured-log events,
 * recent errors and slow requests, and the trace of any recent request with its spans.
 */
import { useEffect, useState } from "react";
import { Activity, AlertTriangle, Gauge, RefreshCw, Route, Timer, Waypoints } from "lucide-react";
import { errorMessage, request } from "./api";
import { Drawer, Empty, ErrorBanner, Loading, PageTitle } from "./components";

interface RouteStat {
  method: string;
  route: string;
  total: number;
  errors: number;
  by_status: Record<string, number>;
  avg_ms: number;
  p95_ms: number;
  max_ms: number;
}
interface SpanStat {
  name: string;
  count: number;
  avg_ms: number;
  p95_ms: number;
}
interface RecentItem {
  id: string;
  route: string;
  method: string;
  duration_ms?: number;
  status?: number;
  error?: string;
  at: number;
  user: string;
}
interface Summary {
  metrics: {
    uptime_seconds: number;
    requests: number;
    errors: number;
    error_rate: number;
    routes: RouteStat[];
    slowest_routes: RouteStat[];
    spans: SpanStat[];
    events: Record<string, number>;
    recent_slow: RecentItem[];
    recent_errors: RecentItem[];
    json_logs: boolean;
    log_level: string;
  };
  process: { pid: number; threads: number; python: string; memory_mb?: number; cpu_seconds?: number; thread_names: string[] };
  components: { name: string; label: string; ok?: boolean; detail?: string; [key: string]: unknown }[];
  build: string;
  instance_id: string;
}
interface Span {
  name: string;
  kind: string;
  duration_ms: number | null;
  error: string;
  attributes: Record<string, unknown>;
}
interface Trace {
  id: string;
  method: string;
  path: string;
  route: string;
  user: string;
  status: number;
  started_at: number;
  duration_ms: number;
  error: string;
  spans: Span[];
}

function stamp(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function uptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`;
  return `${(seconds / 86400).toFixed(1)} 天`;
}
function StatusDot({ ok }: { ok?: boolean }) {
  return ok ? <span className="healthy-badge">正常</span> : <span className="failure-badge">异常</span>;
}

export default function OpsPage() {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [traces, setTraces] = useState<Trace[] | null>(null);
  const [traceFilter, setTraceFilter] = useState({ route: "", min_ms: 0, status: "" });
  const [selected, setSelected] = useState<Trace | null>(null);
  const [auto, setAuto] = useState(true);
  async function load() {
    setBusy(true);
    try {
      const [summary, listing] = await Promise.all([
        request<Summary>("/api/observability/summary"),
        request<{ items: Trace[] }>(
          `/api/observability/traces?limit=50${traceFilter.route ? `&route=${encodeURIComponent(traceFilter.route)}` : ""}${traceFilter.min_ms ? `&min_ms=${traceFilter.min_ms}` : ""}${traceFilter.status ? `&status=${traceFilter.status}` : ""}`,
        ),
      ]);
      setData(summary);
      setTraces(listing.items);
      setError("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void load();
    if (!auto) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 10_000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, traceFilter]);
  async function setLevel(level: string) {
    setNotice("");
    try {
      await request("/api/observability/log-level", { level });
      setNotice(`日志级别已切换为 ${level}。`);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  async function reset() {
    if (!window.confirm("确认清空当前进程内的计数与追踪记录？不影响运行。")) return;
    try {
      await request("/api/observability/reset", {});
      setNotice("计数与追踪已清空。");
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const metrics = data?.metrics;
  return (
    <div className="page-content">
      <PageTitle
        title="运行观测"
        description="进程内的请求指标、组件健康、结构化日志事件与最近请求的链路追踪；Prometheus 指标见 /api/observability/metrics"
        action={
          <div className="button-row">
            <label className="checkbox-row">
              <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
              每 10 秒刷新
            </label>
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void load()}>
              <RefreshCw size={14} className={busy ? "spin" : ""} />
              刷新
            </button>
            <a className="secondary-button" href="/api/observability/metrics" target="_blank" rel="noreferrer">
              <Gauge size={14} />
              Prometheus 指标
            </a>
            <button type="button" className="secondary-button" onClick={() => void reset()}>
              清空计数
            </button>
          </div>
        }
      />
      {error && <ErrorBanner message={error} />}
      {notice && (
        <div className="info-banner" role="status">
          {notice}
        </div>
      )}
      {!data ? (
        <Loading text="正在读取运行指标…" />
      ) : (
        <>
          <div className="cache-stats ops-stats">
            <div>
              <b>{uptime(metrics!.uptime_seconds)}</b>
              <span>运行时长 · 构建 {data.build.slice(0, 8)}</span>
            </div>
            <div>
              <b>{metrics!.requests.toLocaleString()}</b>
              <span>API 请求（本次计数以来）</span>
            </div>
            <div>
              <b>{(metrics!.error_rate * 100).toFixed(1)}%</b>
              <span>错误率（{metrics!.errors} 次 5xx / 异常）</span>
            </div>
            <div>
              <b>{data.process.memory_mb ? `${data.process.memory_mb} MB` : "—"}</b>
              <span>
                内存峰值 · {data.process.threads} 线程 · PID {data.process.pid}
              </span>
            </div>
          </div>
          <section className="panel">
            <h2>
              <Activity size={16} /> 组件健康
            </h2>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>组件</th>
                    <th>状态</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {data.components.map((item) => (
                    <tr key={item.name}>
                      <td>{item.label}</td>
                      <td>
                        <StatusDot ok={item.ok !== false} />
                      </td>
                      <td className="ops-detail">{componentDetail(item)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <div className="ops-grid">
            <section className="panel">
              <h2>
                <Route size={16} /> 请求量最高的路由
              </h2>
              <RouteTable rows={metrics!.routes} />
            </section>
            <section className="panel">
              <h2>
                <Timer size={16} /> 最慢的路由（p95）
              </h2>
              <RouteTable rows={metrics!.slowest_routes} />
            </section>
          </div>
          <div className="ops-grid">
            <section className="panel">
              <h2>
                <Waypoints size={16} /> 内部步骤耗时
              </h2>
              {metrics!.spans.length ? (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>步骤</th>
                        <th>次数</th>
                        <th>平均</th>
                        <th>p95</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metrics!.spans.map((span) => (
                        <tr key={span.name}>
                          <td>
                            <code>{span.name}</code>
                          </td>
                          <td>{span.count}</td>
                          <td>{span.avg_ms} ms</td>
                          <td>{span.p95_ms} ms</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="还没有记录到内部步骤">执行查询、核查或模型调用后会出现在这里。</Empty>
              )}
            </section>
            <section className="panel">
              <h2>
                <AlertTriangle size={16} /> 最近的错误与慢请求
              </h2>
              <ul className="ops-list">
                {metrics!.recent_errors.map((item) => (
                  <li key={`e-${item.id}`}>
                    <span className="failure-badge">{item.status}</span>
                    <code>
                      {item.method} {item.route}
                    </code>
                    <small>
                      {stamp(item.at)} · {item.user || "匿名"} · {item.error || "—"}
                    </small>
                  </li>
                ))}
                {metrics!.recent_slow.map((item) => (
                  <li key={`s-${item.id}`}>
                    <span className="quality-warn-badge">{item.duration_ms} ms</span>
                    <code>
                      {item.method} {item.route}
                    </code>
                    <small>
                      {stamp(item.at)} · {item.user || "匿名"}
                    </small>
                  </li>
                ))}
                {!metrics!.recent_errors.length && !metrics!.recent_slow.length && <li className="muted">没有错误，也没有超过 1 秒的请求。</li>}
              </ul>
            </section>
          </div>
          <section className="panel">
            <h2>
              <Waypoints size={16} /> 链路追踪（最近 {traces?.length ?? 0} 条）
            </h2>
            <div className="quality-toolbar">
              <label htmlFor="ops-route">路由包含</label>
              <input id="ops-route" value={traceFilter.route} placeholder="/api/query" onChange={(event) => setTraceFilter({ ...traceFilter, route: event.target.value })} />
              <label htmlFor="ops-min">耗时 ≥</label>
              <input id="ops-min" type="number" min={0} value={traceFilter.min_ms} onChange={(event) => setTraceFilter({ ...traceFilter, min_ms: Number(event.target.value) || 0 })} />
              <span className="muted">ms</span>
              <label htmlFor="ops-status">状态码</label>
              <input id="ops-status" value={traceFilter.status} placeholder="全部" onChange={(event) => setTraceFilter({ ...traceFilter, status: event.target.value.replace(/[^0-9]/g, "") })} />
            </div>
            {traces && traces.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>请求</th>
                      <th>用户</th>
                      <th>状态</th>
                      <th>耗时</th>
                      <th>步骤</th>
                      <th>追踪 ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {traces.map((trace) => (
                      <tr key={trace.id} className="ops-row" onClick={() => setSelected(trace)}>
                        <td>{stamp(trace.started_at)}</td>
                        <td>
                          <code>
                            {trace.method} {trace.path}
                          </code>
                        </td>
                        <td>{trace.user || "匿名"}</td>
                        <td>{trace.status >= 500 ? <span className="failure-badge">{trace.status}</span> : trace.status >= 400 ? <span className="quality-warn-badge">{trace.status}</span> : <span className="healthy-badge">{trace.status}</span>}</td>
                        <td>{trace.duration_ms} ms</td>
                        <td>{trace.spans.length}</td>
                        <td>
                          <code>{trace.id}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="没有匹配的追踪记录">追踪只保留最近 200 个请求。</Empty>
            )}
          </section>
          <section className="panel">
            <h2>
              <Gauge size={16} /> 日志
            </h2>
            <p className="muted">
              每个请求写一行结构化日志到服务标准输出（{metrics!.json_logs ? "JSON 格式" : "单行文本，设置 LATTICE_LOG_JSON=1 切换为 JSON"}），字段包含追踪 ID、路由模板、状态、耗时与用户，不含查询语句。当前级别：<b>{metrics!.log_level}</b>
            </p>
            <div className="button-row">
              {["DEBUG", "INFO", "WARNING", "ERROR"].map((level) => (
                <button key={level} type="button" className={level === metrics!.log_level ? "primary-button" : "secondary-button"} onClick={() => void setLevel(level)}>
                  {level}
                </button>
              ))}
            </div>
            <ul className="ops-events">
              {Object.entries(metrics!.events).map(([event, count]) => (
                <li key={event}>
                  <code>{event}</code> × {count}
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
      {selected && (
        <Drawer id="ops-trace" title={`追踪 · ${selected.id}`} icon={<Waypoints size={17} />} onClose={() => setSelected(null)} wide>
          <div className="quality-drawer-body">
            <dl className="ops-dl">
              <dt>请求</dt>
              <dd>
                <code>
                  {selected.method} {selected.path}
                </code>
              </dd>
              <dt>路由模板</dt>
              <dd>
                <code>{selected.route}</code>
              </dd>
              <dt>用户</dt>
              <dd>{selected.user || "匿名"}</dd>
              <dt>状态 / 耗时</dt>
              <dd>
                {selected.status} · {selected.duration_ms} ms
              </dd>
              {selected.error && (
                <>
                  <dt>错误</dt>
                  <dd className="failure-text">{selected.error}</dd>
                </>
              )}
            </dl>
            <h3>步骤</h3>
            {selected.spans.length ? (
              <div className="ops-spans">
                {selected.spans.map((span, index) => {
                  const width = selected.duration_ms > 0 && span.duration_ms ? Math.max(2, Math.min(100, (span.duration_ms / selected.duration_ms) * 100)) : 2;
                  return (
                    <div key={index} className="ops-span">
                      <div className="ops-span-head">
                        <code>
                          {span.kind}:{span.name}
                        </code>
                        <span>{span.duration_ms ?? "—"} ms</span>
                      </div>
                      <div className="ops-span-bar">
                        <i style={{ width: `${width}%` }} className={span.error ? "failed" : ""} />
                      </div>
                      {(Object.keys(span.attributes).length > 0 || span.error) && (
                        <small>
                          {Object.entries(span.attributes)
                            .map(([key, value]) => `${key}=${String(value)}`)
                            .join(" · ")}
                          {span.error ? ` · 错误：${span.error}` : ""}
                        </small>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="muted">这个请求没有记录内部步骤。</p>
            )}
          </div>
        </Drawer>
      )}
    </div>
  );
}

function RouteTable({ rows }: { rows: RouteStat[] }) {
  if (!rows.length) return <Empty title="还没有请求">发起 API 请求后会出现在这里。</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>路由</th>
            <th>次数</th>
            <th>错误</th>
            <th>平均</th>
            <th>p95</th>
            <th>最大</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.method} ${row.route}`}>
              <td>
                <code>
                  {row.method} {row.route}
                </code>
              </td>
              <td>{row.total}</td>
              <td>{row.errors ? <span className="failure-badge">{row.errors}</span> : 0}</td>
              <td>{row.avg_ms} ms</td>
              <td>{row.p95_ms} ms</td>
              <td>{row.max_ms} ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function componentDetail(item: Summary["components"][number]): string {
  if (item.detail) return String(item.detail);
  const parts: string[] = [];
  for (const [key, value] of Object.entries(item)) {
    if (["name", "label", "ok", "detail", "jobs", "by_datasource", "tasks"].includes(key)) continue;
    if (value === null || value === undefined || typeof value === "object") continue;
    parts.push(`${key}=${String(value)}`);
    if (parts.length >= 6) break;
  }
  return parts.join(" · ") || "—";
}
