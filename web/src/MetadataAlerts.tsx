// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据检测: alert subscriptions on catalog change events (in-app and signed webhooks), the
 * notification inbox and the organisation-wide activity feed with open tasks.
 */
import { useEffect, useState } from "react";
import { ArrowLeft, BellRing, CheckCheck, Pencil, Plus, Send, Trash2, X } from "lucide-react";
import { errorMessage } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  formatTime,
  mdGet,
  mdPost,
  metadataPath,
  navigate,
  timeAgo,
  typeLabel,
  type Entity,
  type FeedEvent,
  type Notification,
  type Paged,
  type Thread,
  type TypesResponse,
} from "./metadataApi";
import { EntityLink, FormRow, MarkdownEditor, Pager, Toggle } from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";

const CHANGE_KIND_LABELS: Record<string, string> = {
  created: "创建",
  description: "描述",
  displayName: "显示名称",
  owners: "所有者",
  tags: "标签",
  tier: "分级",
  domain: "元数据工作区",
  columns: "字段",
  extension: "自定义属性",
  lineage: "血缘",
  glossaryTerms: "术语",
  custom: "其他",
};
const WATCHED_TYPES = [
  "table", "topic", "dashboard", "chart", "pipeline", "mlmodel", "container", "searchIndex", "apiEndpoint",
  "semanticModel", "databaseService", "database", "databaseSchema", "glossary", "glossaryTerm", "classification",
  "tag", "domain", "dataProduct",
];

interface AlertEntity extends Entity {
  enabled: boolean;
  filters: { entity_types?: string[]; event_types?: string[]; change_kinds?: string[]; fqn_prefix?: string; users?: string[] };
  destinations: { type: string; url?: string; secret?: string }[];
}

export default function AlertsPage({ route }: MetadataViewProps) {
  if (route.view === "notifications") return <NotificationsView />;
  if (route.view === "activity") return <ActivityView />;
  if (route.parts[0] === "new") return <AlertForm />;
  if (route.parts[0]) return <AlertForm fqn={route.parts[0]} />;
  return <AlertList />;
}

function AlertList() {
  const alerts = useApi<Paged<AlertEntity> & { status: { running: boolean; delivered: number; last_error: string; notifications: Record<string, number> } }>(metadataPath("/alerts"));
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function act(action: () => Promise<unknown>, done: string) {
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(done);
      alerts.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const status = alerts.data?.status;
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>告警订阅</h1>
          <p>订阅元数据变更事件：描述、所有者、标签、字段或血缘变化时发送站内通知或回调 Webhook</p>
        </div>
        <button type="button" className="primary-button" onClick={() => navigate("alerts", "new")}>
          <Plus size={14} />
          添加告警
        </button>
      </div>
      {status && (
        <div className="info-banner">
          <BellRing size={15} />
          <span>
            投递线程{status.running ? "运行中" : "未运行"} · 已投递 {status.delivered} 条 · 未读 {status.notifications.unread ?? 0} · 失败 {status.notifications.failed ?? 0}
            {status.last_error ? ` · 最近错误：${status.last_error}` : ""}
          </span>
        </div>
      )}
      {message && <div className="success-banner">{message}</div>}
      {error && <ErrorBanner message={error} />}
      <section className="panel">
        {alerts.loading && <Loading />}
        {alerts.error && <ErrorBanner message={alerts.error} />}
        {alerts.data && (alerts.data.items.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>触发条件</th>
                  <th>通知方式</th>
                  <th>启用</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {alerts.data.items.map((alert) => (
                  <tr key={alert.id}>
                    <td>
                      <strong>{alert.display_name || alert.name}</strong>
                      {alert.description && <small className="md-block-muted">{alert.description}</small>}
                    </td>
                    <td className="md-cell-desc">{describeFilters(alert.filters)}</td>
                    <td>{alert.destinations.map((item) => (item.type === "webhook" ? `Webhook ${item.url}` : "站内通知")).join("；")}</td>
                    <td>
                      <Toggle
                        checked={alert.enabled}
                        onChange={(enabled) => void act(() => mdPost("/alerts/update", { ref: alert.fqn, enabled }), enabled ? "已启用" : "已停用")}
                      />
                    </td>
                    <td>
                      <div className="md-actions">
                        <button type="button" className="text-button" onClick={() => navigate("alerts", alert.fqn)}>
                          <Pencil size={12} />
                          编辑
                        </button>
                        <button type="button" className="text-button" onClick={() => void act(() => mdPost("/alerts/test", { ref: alert.fqn }), "测试通知已发送")}>
                          <Send size={12} />
                          测试
                        </button>
                        <button
                          type="button"
                          className="text-button danger-text"
                          onClick={() => window.confirm(`删除告警 ${alert.name}？`) && void act(() => mdPost("/entities/delete", { entity_type: "eventSubscription", ref: alert.fqn, hard: true }), "已删除")}
                        >
                          <Trash2 size={12} />
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
          <Empty title="还没有告警订阅">添加一条告警，在数据表描述、所有者或血缘变化时收到通知。</Empty>
        ))}
      </section>
    </div>
  );
}

function describeFilters(filters: AlertEntity["filters"]): string {
  const parts = [
    filters.entity_types?.length ? `对象：${filters.entity_types.map(typeLabel).join("、")}` : "全部对象",
    filters.event_types?.length ? `事件：${filters.event_types.join("、")}` : "",
    filters.change_kinds?.length ? `变更：${filters.change_kinds.map((kind) => CHANGE_KIND_LABELS[kind] ?? kind).join("、")}` : "",
    filters.fqn_prefix ? `范围：${filters.fqn_prefix}` : "",
    filters.users?.length ? `用户：${filters.users.join("、")}` : "",
  ];
  return parts.filter(Boolean).join("；");
}

function toggleIn(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function AlertForm({ fqn }: { fqn?: string }) {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const [loaded, setLoaded] = useState(!fqn);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [entityTypes, setEntityTypes] = useState<string[]>(["table"]);
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [kinds, setKinds] = useState<string[]>([]);
  const [prefix, setPrefix] = useState("");
  const [users, setUsers] = useState("");
  const [inApp, setInApp] = useState(true);
  const [hooks, setHooks] = useState<{ url: string; secret: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!fqn) return;
    mdGet<AlertEntity>("/entity", { entity_type: "eventSubscription", ref: fqn })
      .then((alert) => {
        setName(alert.name);
        setDisplayName(alert.display_name);
        setDescription(alert.description);
        setEnabled(alert.enabled);
        setEntityTypes(alert.filters.entity_types ?? []);
        setEventTypes(alert.filters.event_types ?? []);
        setKinds(alert.filters.change_kinds ?? []);
        setPrefix(alert.filters.fqn_prefix ?? "");
        setUsers((alert.filters.users ?? []).join(", "));
        setInApp(alert.destinations.some((item) => item.type === "in_app"));
        setHooks(alert.destinations.filter((item) => item.type === "webhook").map((item) => ({ url: item.url ?? "", secret: item.secret ?? "" })));
        setLoaded(true);
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [fqn]);
  async function save() {
    setBusy(true);
    setError("");
    const body = {
      display_name: displayName,
      description,
      enabled,
      filters: { entity_types: entityTypes, event_types: eventTypes, change_kinds: kinds, fqn_prefix: prefix.trim(), users: users.split(/[,，\s]+/).filter(Boolean) },
      destinations: [...(inApp ? [{ type: "in_app" }] : []), ...hooks.filter((hook) => hook.url.trim()).map((hook) => ({ type: "webhook", url: hook.url.trim(), secret: hook.secret }))],
    };
    try {
      if (fqn) await mdPost("/alerts/update", { ref: fqn, ...body });
      else await mdPost("/alerts", { name: name.trim(), ...body });
      navigate("alerts");
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  if (!loaded) return error ? <ErrorBanner message={error} /> : <Loading />;
  return (
    <div className="page-content md-page">
      <div className="md-breadcrumbs">
        <button type="button" className="md-link" onClick={() => navigate("alerts")}>
          告警订阅
        </button>
        <i>/</i>
        <b>{fqn ? `编辑 ${fqn}` : "添加告警"}</b>
      </div>
      <div className="md-form-layout">
        <div className="md-form">
          <h2>{fqn ? "编辑告警" : "添加告警"}</h2>
          <FormRow label="名称" htmlFor="alert-name" required>
            <input id="alert-name" value={name} disabled={!!fqn} onChange={(event) => setName(event.target.value)} placeholder="例如 core-table-changes" />
          </FormRow>
          <FormRow label="显示名称" htmlFor="alert-display">
            <input id="alert-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="显示名称" />
          </FormRow>
          <FormRow label="描述">
            <MarkdownEditor value={description} onChange={setDescription} rows={3} />
          </FormRow>
          <Toggle checked={enabled} onChange={setEnabled} label="启用" />
          <FormRow label="监控的对象类型" help="不选表示全部类型。">
            <div className="md-checks">
              {WATCHED_TYPES.map((type) => (
                <label key={type}>
                  <input type="checkbox" checked={entityTypes.includes(type)} onChange={() => setEntityTypes(toggleIn(entityTypes, type))} />
                  {typeLabel(type)}
                </label>
              ))}
            </div>
          </FormRow>
          <FormRow label="事件类型" help="不选表示全部事件。">
            <div className="md-checks">
              {(types.data?.event_types ?? []).map((item) => (
                <label key={item.id}>
                  <input type="checkbox" checked={eventTypes.includes(item.id)} onChange={() => setEventTypes(toggleIn(eventTypes, item.id))} />
                  {item.label}
                </label>
              ))}
            </div>
          </FormRow>
          <FormRow label="变更内容" help="只在这些字段变化时通知；不选表示任何变化。">
            <div className="md-checks">
              {Object.entries(CHANGE_KIND_LABELS).map(([kind, label]) => (
                <label key={kind}>
                  <input type="checkbox" checked={kinds.includes(kind)} onChange={() => setKinds(toggleIn(kinds, kind))} />
                  {label}
                </label>
              ))}
            </div>
          </FormRow>
          <div className="md-form-inline">
            <FormRow label="完整名称前缀" htmlFor="alert-prefix" help="例如 mysql-local.default.lattice_demo">
              <input id="alert-prefix" value={prefix} onChange={(event) => setPrefix(event.target.value)} />
            </FormRow>
            <FormRow label="操作用户" htmlFor="alert-users" help="逗号分隔，例如 ingestion-bot, mcp-agent">
              <input id="alert-users" value={users} onChange={(event) => setUsers(event.target.value)} />
            </FormRow>
          </div>
          <FormRow label="通知方式">
            <label className="md-check">
              <input type="checkbox" checked={inApp} onChange={(event) => setInApp(event.target.checked)} />
              站内通知（通知中心）
            </label>
            {hooks.map((hook, index) => (
              <div key={index} className="md-destination">
                <input value={hook.url} placeholder="https://example.com/hooks/metadata" onChange={(event) => setHooks(hooks.map((item, i) => (i === index ? { ...item, url: event.target.value } : item)))} />
                <input value={hook.secret} placeholder="签名密钥（可选）" onChange={(event) => setHooks(hooks.map((item, i) => (i === index ? { ...item, secret: event.target.value } : item)))} />
                <button type="button" className="icon-button" aria-label="移除 Webhook" onClick={() => setHooks(hooks.filter((_, i) => i !== index))}>
                  <X size={14} />
                </button>
              </div>
            ))}
            <button type="button" className="md-add" onClick={() => setHooks([...hooks, { url: "", secret: "" }])}>
              <Plus size={12} />
              添加 Webhook
            </button>
          </FormRow>
          {error && <ErrorBanner message={error} />}
          <div className="md-form-actions">
            <button type="button" className="text-button" onClick={() => navigate("alerts")}>
              取消
            </button>
            <button type="button" className="primary-button" disabled={busy || (!fqn && !name.trim())} onClick={() => void save()}>
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
        <aside className="md-help">
          <h3>告警订阅</h3>
          <p>每次元数据发生变化都会产生一条变更事件（创建、更新、删除、恢复）。符合条件的事件会投递到站内通知中心，或以 JSON POST 到 Webhook。</p>
          <h4>Webhook 签名</h4>
          <p>
            填写签名密钥后，请求头 <code>X-Lattice-Signature</code> 为 <code>sha256=</code> 加请求体的 HMAC-SHA256 摘要，接收方可据此校验来源。
          </p>
          <h4>常见用法</h4>
          <p>监控 Tier1 表的字段变更、在 AI 代理（mcp-agent）修改描述时通知负责人、在拾取任务删除资产时告警。</p>
        </aside>
      </div>
    </div>
  );
}

function NotificationsView() {
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const size = 20;
  const listing = useApi<Paged<Notification> & { counts: Record<string, number> }>(metadataPath("/notifications", { status, page, size }));
  const [error, setError] = useState("");
  async function read(ids: string[], all = false) {
    setError("");
    try {
      await mdPost("/notifications/read", { ids, all });
      listing.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const counts = listing.data?.counts ?? {};
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>通知中心</h1>
          <p>告警订阅投递的站内通知与 Webhook 投递记录</p>
        </div>
        <button type="button" className="secondary-button" onClick={() => void read([], true)}>
          <CheckCheck size={14} />
          全部标为已读
        </button>
      </div>
      <div className="segmented-control md-segments">
        {[["", "全部"], ["unread", `未读 ${counts.unread ?? 0}`], ["read", `已读 ${counts.read ?? 0}`], ["delivered", `Webhook 成功 ${counts.delivered ?? 0}`], ["failed", `失败 ${counts.failed ?? 0}`]].map(([value, label]) => (
          <button key={value} type="button" className={status === value ? "selected" : ""} onClick={() => { setStatus(value); setPage(1); }}>
            {label}
          </button>
        ))}
      </div>
      {error && <ErrorBanner message={error} />}
      <section className="panel">
        {listing.loading && <Loading />}
        {listing.error && <ErrorBanner message={listing.error} />}
        {listing.data && !listing.data.items.length && <Empty title="暂无通知" />}
        <ul className="md-notifications">
          {(listing.data?.items ?? []).map((item) => (
            <li key={item.id} className={item.status === "unread" ? "unread" : ""}>
              <div>
                <strong>{item.json.subscription_name ?? item.subscription?.name}</strong>
                <span className={item.status === "failed" ? "failure-badge" : item.status === "delivered" ? "healthy-badge" : "neutral-badge"}>
                  {item.json.destination === "webhook" ? "Webhook" : "站内"} · {item.status}
                </span>
                <small>{timeAgo(item.ts)}</small>
              </div>
              <p>
                {item.json.user_name && <b>{item.json.user_name}</b>} {item.json.summary}{" "}
                {item.json.entity_fqn && item.json.entity_type && (
                  <EntityLink entity={{ entity_type: item.json.entity_type, fqn: item.json.entity_fqn }}>{item.json.entity_fqn}</EntityLink>
                )}
              </p>
              {item.detail && <small className="md-block-muted">{item.detail}</small>}
              {item.status === "unread" && (
                <button type="button" className="text-button" onClick={() => void read([item.id])}>
                  标为已读
                </button>
              )}
            </li>
          ))}
        </ul>
        {listing.data && <Pager total={listing.data.total} page={page} size={size} onPage={setPage} />}
      </section>
    </div>
  );
}

function ActivityView() {
  const [eventType, setEventType] = useState("");
  const [entityType, setEntityType] = useState("");
  const [page, setPage] = useState(1);
  const size = 30;
  const feed = useApi<Paged<FeedEvent>>(metadataPath("/feed", { event_type: eventType, entity_type: entityType, page, size }));
  const tasks = useApi<Paged<Thread>>(metadataPath("/threads", { thread_type: "Task", resolved: false, size: 20 }));
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>活动信息流</h1>
          <p>全部元数据变更事件，以及等待处理的描述与标签任务</p>
        </div>
        <button type="button" className="text-button" onClick={() => navigate("explore")}>
          <ArrowLeft size={12} />
          返回资产
        </button>
      </div>
      {(tasks.data?.items ?? []).length > 0 && (
        <section className="panel">
          <div className="panel-heading">
            <h2>待处理任务</h2>
            <span>{tasks.data?.total} 个</span>
          </div>
          <ul className="md-impact-list panel-body">
            {(tasks.data?.items ?? []).map((task) => (
              <li key={task.id}>
                <span className="quality-warn-badge">{task.task?.task_type}</span>
                <EntityLink entity={{ entity_type: task.about_type || "table", fqn: task.about_fqn }}>{task.about_fqn}</EntityLink>
                <span className="muted">
                  {task.message} · {task.created_by} · {timeAgo(task.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="panel">
        <div className="panel-heading">
          <div className="md-actions">
            <select value={eventType} onChange={(event) => { setEventType(event.target.value); setPage(1); }} aria-label="事件类型">
              <option value="">全部事件</option>
              <option value="entityCreated">创建</option>
              <option value="entityUpdated">更新</option>
              <option value="entitySoftDeleted">删除（可恢复）</option>
              <option value="entityRestored">恢复</option>
              <option value="entityDeleted">永久删除</option>
            </select>
            <select value={entityType} onChange={(event) => { setEntityType(event.target.value); setPage(1); }} aria-label="对象类型">
              <option value="">全部对象</option>
              {WATCHED_TYPES.map((type) => (
                <option key={type} value={type}>
                  {typeLabel(type)}
                </option>
              ))}
            </select>
          </div>
          <span>{feed.data?.total ?? 0} 条</span>
        </div>
        {feed.loading && <Loading />}
        {feed.error && <ErrorBanner message={feed.error} />}
        <ol className="md-timeline">
          {(feed.data?.items ?? []).map((event) => (
            <li key={event.id}>
              <span className={`md-event md-event-${event.event_type}`}>{event.event_label}</span>
              <div>
                <p>
                  <strong>{event.user_name || "系统"}</strong> {event.summary}
                </p>
                <small>
                  {event.type_label}{" "}
                  {event.event_type === "entityDeleted" ? event.entity_fqn : <EntityLink entity={{ entity_type: event.entity_type, fqn: event.entity_fqn }}>{event.entity_fqn}</EntityLink>} · {formatTime(event.ts)}
                </small>
              </div>
            </li>
          ))}
        </ol>
        {feed.data && <Pager total={feed.data.total} page={page} size={size} onPage={setPage} />}
      </section>
    </div>
  );
}
