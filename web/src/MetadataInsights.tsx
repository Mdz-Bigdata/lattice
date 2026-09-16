// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据洞察 (OpenMetadata's Data Insights): catalog health over time, application analytics and
 * KPIs. Charts are hand-drawn SVG: one series per chart on one axis, 2px lines with a 10% wash,
 * solid hairline grids, a crosshair tooltip that snaps to the nearest day, and a table twin for
 * every chart. Filters sit in one row above everything they scope; a refetch keeps the previous
 * frame dimmed instead of flashing a loader.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  CircleAlert,
  CircleCheck,
  Clock,
  Pencil,
  Plus,
  Target,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { errorMessage } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  entityName,
  mdGet,
  mdPost,
  metadataPath,
  navigate,
  openEntity,
  typeLabel,
  type Entity,
  type InsightFigures,
  type Insights,
  type Kpi,
  type TypesResponse,
} from "./metadataApi";
import { FormRow, MarkdownEditor, SearchBox } from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";

const RANGES = [7, 14, 30, 90];
const percent = (value: number) => `${value.toFixed(value % 1 ? 2 : 0)}%`;
const count = (value: number) => Math.round(value).toLocaleString();

export default function InsightsPage({ route }: MetadataViewProps) {
  if (route.parts[0] === "kpi-new") return <KpiForm />;
  if (route.parts[0] === "kpi" && route.parts[1]) return <KpiForm fqn={route.parts[1]} />;
  return <InsightsOverview tab={route.parts[0] === "usage" || route.parts[0] === "kpis" ? route.parts[0] : "assets"} />;
}

function InsightsOverview({ tab }: { tab: string }) {
  const [days, setDays] = useState(7);
  const [team, setTeam] = useState("");
  const [tier, setTier] = useState("");
  const [domain, setDomain] = useState("");
  const teams = useApi<{ teams: Entity[] }>(metadataPath("/teams"));
  const domains = useApi<{ domains: Entity[] }>(metadataPath("/domains"));
  const insights = useApi<Insights>(metadataPath("/insights", { days, team, tier, domain }));
  const [frame, setFrame] = useState<Insights | null>(null);
  useEffect(() => {
    if (insights.data) setFrame(insights.data);
  }, [insights.data]);
  const data = insights.data ?? frame;
  return (
    <div className="page-content md-page md-viz">
      <div className="md-page-head">
        <div>
          <h1>数据洞察</h1>
          <p>查看所有数据资产的健康状况</p>
        </div>
        <button type="button" className="primary-button" onClick={() => navigate("insights", "kpi-new")}>
          添加KPI
        </button>
      </div>
      <nav className="quality-tabs md-tabs" role="tablist" aria-label="数据洞察">
        {[["assets", "数据资产"], ["usage", "应用分析"], ["kpis", "KPIs"]].map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={`quality-tab ${tab === id ? "active" : ""}`} onClick={() => navigate("insights", id)}>
            {label}
          </button>
        ))}
      </nav>
      <div className="md-viz-filters">
        <label>
          日期范围
          <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
            {RANGES.map((value) => (
              <option key={value} value={value}>
                最近 {value} 天
              </option>
            ))}
          </select>
        </label>
        <label>
          团队
          <select value={team} onChange={(event) => setTeam(event.target.value)}>
            <option value="">全部</option>
            {(teams.data?.teams ?? []).map((item) => (
              <option key={item.id} value={item.fqn}>
                {entityName(item)}
              </option>
            ))}
          </select>
        </label>
        <label>
          分级
          <select value={tier} onChange={(event) => setTier(event.target.value)}>
            <option value="">全部</option>
            {[1, 2, 3, 4, 5].map((level) => (
              <option key={level} value={`Tier.Tier${level}`}>
                Tier{level}
              </option>
            ))}
          </select>
        </label>
        <label>
          元数据工作区
          <select value={domain} onChange={(event) => setDomain(event.target.value)}>
            <option value="">全部</option>
            {(domains.data?.domains ?? []).map((item) => (
              <option key={item.id} value={item.fqn}>
                {entityName(item)}
              </option>
            ))}
          </select>
        </label>
        {data && (
          <span className="md-viz-period">
            {data.filtered ? "已筛选：只显示当前值，历史快照不含筛选条件" : `${data.period.from} ~ ${data.period.to}`}
          </span>
        )}
      </div>
      {insights.error && <ErrorBanner message={insights.error} />}
      {!data && insights.loading && <Loading text="正在计算目录健康度…" />}
      {data && (
        <div className={insights.loading ? "md-refetching" : ""}>
          {tab === "assets" && <AssetsView data={data} />}
          {tab === "usage" && <UsageView data={data} />}
          {tab === "kpis" && <KpiTable kpis={data.kpis} onChanged={insights.reload} />}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ views */

function AssetsView({ data }: { data: Insights }) {
  const current = data.current;
  return (
    <>
      <h2 className="md-viz-section">组织的数据资产健康一览</h2>
      <div className="md-stat-grid">
        <StatTile label="所有数据资产" value={count(current.assets)} delta={data.change.assets} neutral format={count} since={data.period.from} />
        <StatTile label="带有描述信息的数据资产" value={percent(current.description_percent)} delta={data.change.description_percent} format={(value) => `${value.toFixed(2)} 个百分点`} since={data.period.from} />
        <StatTile label="带有所有者信息的数据资产" value={percent(current.owner_percent)} delta={data.change.owner_percent} format={(value) => `${value.toFixed(2)} 个百分点`} since={data.period.from} />
        <StatTile label="带有分级信息的数据资产" value={percent(current.tier_percent)} delta={data.change.tier_percent} format={(value) => `${value.toFixed(2)} 个百分点`} since={data.period.from} />
      </div>
      <section className="panel md-viz-panel">
        <div className="panel-heading">
          <div>
            <h2>关键绩效指标 (KPI)</h2>
            <span>确定最能反映数据资产健康状况的关键绩效指标 (KPI)</span>
          </div>
        </div>
        <div className="panel-body">
          {data.kpis.length ? (
            <div className="md-kpi-grid">
              {data.kpis.map((kpi) => (
                <KpiCard key={kpi.id} kpi={kpi} />
              ))}
            </div>
          ) : (
            <div className="md-kpi-empty">
              <Target size={40} strokeWidth={1.2} />
              <p>没有可用的 KPI，请单击添加 KPI 按钮添加一个</p>
              <button type="button" className="secondary-button" onClick={() => navigate("insights", "kpi-new")}>
                <Plus size={13} />
                添加KPI
              </button>
            </div>
          )}
        </div>
      </section>
      <TrendPanel
        title="所有数据资产"
        subtitle="按类型显示最新的数据资产数量"
        series={data.series.map((item) => ({ day: item.day ?? "", value: item.assets }))}
        format={count}
        headline={count(current.assets)}
        headlineLabel="所有资产"
        bars={current.by_type.map((row) => ({ key: row.entity_type, label: row.label, value: row.total }))}
        barFormat={count}
      />
      <CoveragePanel title="带有描述信息的数据资产占比" subtitle="按类型显示具有描述的数据资产百分比" field="description_percent" headlineLabel="已完成描述" data={data} />
      <CoveragePanel title="带有所有者信息的数据资产占比" subtitle="按类型显示具有所有者的数据资产百分比" field="owner_percent" headlineLabel="已设置所有者" data={data} />
      <CoveragePanel title="带有分级信息的数据资产占比" subtitle="按类型显示已分级的数据资产百分比" field="tier_percent" headlineLabel="已分级" data={data} />
    </>
  );
}

function CoveragePanel({ title, subtitle, field, headlineLabel, data }: { title: string; subtitle: string; field: "description_percent" | "owner_percent" | "tier_percent"; headlineLabel: string; data: Insights }) {
  return (
    <TrendPanel
      title={title}
      subtitle={subtitle}
      series={data.series.map((item) => ({ day: item.day ?? "", value: item[field] }))}
      format={percent}
      max={100}
      headline={percent(data.current[field])}
      headlineLabel={headlineLabel}
      bars={data.current.by_type.map((row) => ({ key: row.entity_type, label: row.label, value: row[field], hint: `${row.total} 个` }))}
      barFormat={percent}
      barMax={100}
    />
  );
}

function UsageView({ data }: { data: Insights }) {
  const current: InsightFigures = data.current;
  const events = Object.entries(data.events);
  const eventLabels: Record<string, string> = { entityCreated: "创建", entityUpdated: "更新", entitySoftDeleted: "删除（可恢复）", entityRestored: "恢复", entityDeleted: "永久删除" };
  return (
    <>
      <h2 className="md-viz-section">应用分析</h2>
      <div className="md-stat-grid">
        <StatTile label={`近 ${data.period.days} 天的元数据变更`} value={count(events.reduce((sum, [, value]) => sum + value, 0))} />
        <StatTile label="活跃用户" value={count(data.active_users.length)} />
        <StatTile label="近 30 天被使用的资产" value={count(current.used_30d)} />
        <StatTile label="近 30 天未被使用的资产" value={count(current.unused_30d)} />
        <StatTile label="带有标签的数据资产" value={percent(current.tag_percent)} />
        <StatTile label="具有血缘的数据资产" value={percent(current.lineage_percent)} />
      </div>
      <div className="md-viz-columns">
        <section className="panel md-viz-panel">
          <div className="panel-heading">
            <h2>最常浏览的资产</h2>
          </div>
          <div className="panel-body">
            <BarList items={data.top_viewed.map((item) => ({ key: item.target_fqn, label: item.entity ? entityName(item.entity) : item.target_fqn, hint: item.target_fqn, value: item.total, onClick: item.entity ? () => openEntity(item.entity!.entity_type, item.entity!.fqn) : undefined }))} format={(value) => `${count(value)} 次`} empty="近期还没有浏览记录" />
          </div>
        </section>
        <section className="panel md-viz-panel">
          <div className="panel-heading">
            <h2>最常查询的资产</h2>
          </div>
          <div className="panel-body">
            <BarList items={data.top_queried.map((item) => ({ key: item.target_fqn, label: item.entity ? entityName(item.entity) : item.target_fqn, hint: item.target_fqn, value: item.total, onClick: item.entity ? () => openEntity(item.entity!.entity_type, item.entity!.fqn) : undefined }))} format={(value) => `${count(value)} 次`} empty="近期还没有查询记录" />
          </div>
        </section>
      </div>
      <div className="md-viz-columns">
        <section className="panel md-viz-panel">
          <div className="panel-heading">
            <h2>变更事件</h2>
            <span>最近 {data.period.days} 天</span>
          </div>
          <div className="panel-body">
            <BarList items={events.map(([key, value]) => ({ key, label: eventLabels[key] ?? key, value }))} format={count} empty="没有变更事件" />
          </div>
        </section>
        <section className="panel md-viz-panel">
          <div className="panel-heading">
            <h2>活跃用户</h2>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th className="md-num">变更次数</th>
                  <th>最近活动</th>
                </tr>
              </thead>
              <tbody>
                {data.active_users.map((user) => (
                  <tr key={user.user_name}>
                    <td>{user.user_name}</td>
                    <td className="md-num">{count(user.total)}</td>
                    <td>{new Date(user.last_ts).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ figures */

function StatTile({ label, value, delta, format, since, neutral = false }: { label: string; value: string; delta?: number; format?: (value: number) => string; since?: string; neutral?: boolean }) {
  const direction = delta === undefined || delta === 0 ? "flat" : delta > 0 ? "up" : "down";
  const tone = neutral || direction === "flat" ? "neutral" : direction === "up" ? "good" : "bad";
  return (
    <div className="md-stat">
      <span>{label}</span>
      <strong>{value}</strong>
      {delta !== undefined && format && (
        <small className={`md-delta ${tone}`}>
          {direction === "up" ? <ArrowUp size={12} /> : direction === "down" ? <ArrowDown size={12} /> : null}
          {direction === "flat" ? "无变化" : `${delta > 0 ? "+" : "−"}${format(Math.abs(delta))}`}
          {since && <em>较 {since}</em>}
        </small>
      )}
    </div>
  );
}

function TrendPanel({
  title,
  subtitle,
  series,
  format,
  max,
  headline,
  headlineLabel,
  bars,
  barFormat,
  barMax,
}: {
  title: string;
  subtitle: string;
  series: { day: string; value: number }[];
  format: (value: number) => string;
  max?: number;
  headline: string;
  headlineLabel: string;
  bars: { key: string; label: string; value: number; hint?: string }[];
  barFormat: (value: number) => string;
  barMax?: number;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const [query, setQuery] = useState("");
  const shown = bars.filter((bar) => !query || bar.label.toLowerCase().includes(query.toLowerCase()) || bar.key.toLowerCase().includes(query.toLowerCase()));
  return (
    <section className="panel md-viz-panel">
      <div className="panel-heading">
        <div>
          <h2>{title}</h2>
          <span>{subtitle}</span>
        </div>
        <div className="segmented-control">
          <button type="button" className={view === "chart" ? "selected" : ""} onClick={() => setView("chart")}>
            图表
          </button>
          <button type="button" className={view === "table" ? "selected" : ""} onClick={() => setView("table")}>
            表格
          </button>
        </div>
      </div>
      <div className="md-trend">
        <div className="md-trend-chart">
          {view === "chart" ? (
            <LineChart points={series} format={format} max={max} label={title} />
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>日期</th>
                    <th className="md-num">{title}</th>
                  </tr>
                </thead>
                <tbody>
                  {series.map((point) => (
                    <tr key={point.day}>
                      <td>{point.day}</td>
                      <td className="md-num">{format(point.value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <aside className="md-trend-side">
          <span>{headlineLabel}</span>
          <strong>{headline}</strong>
          <SearchBox value={query} onChange={setQuery} placeholder="搜索类型" />
          <BarList items={shown.map((bar) => ({ ...bar, label: bar.label || typeLabel(bar.key) }))} format={barFormat} max={barMax} empty="没有匹配的类型" />
        </aside>
      </div>
    </section>
  );
}

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * magnitude >= value / 4) ?? 10;
  return Math.ceil(value / (step * magnitude)) * step * magnitude;
}

function LineChart({ points, format, max, label }: { points: { day: string; value: number }[]; format: (value: number) => string; max?: number; label: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [active, setActive] = useState<number | null>(null);
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => setWidth(Math.max(280, entries[0]?.contentRect.width ?? 640)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const height = 240;
  const margin = { top: 14, right: 60, bottom: 28, left: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const top = max ?? niceMax(Math.max(...points.map((point) => point.value), 0) * 1.1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ratio * top);
  const x = (index: number) => margin.left + (points.length <= 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const y = (value: number) => margin.top + plotHeight - (value / top) * plotHeight;
  const path = useMemo(() => points.map((point, index) => `${index ? "L" : "M"} ${x(index)} ${y(point.value)}`).join(" "), [points, width, top]);
  const area = points.length > 1 ? `${path} L ${x(points.length - 1)} ${margin.top + plotHeight} L ${x(0)} ${margin.top + plotHeight} Z` : "";
  const labels = points.length <= 3 ? points.map((_, index) => index) : [0, Math.floor((points.length - 1) / 2), points.length - 1];
  const last = points.length - 1;
  function nearest(clientX: number) {
    const rect = box.current?.getBoundingClientRect();
    if (!rect || !points.length) return;
    const relative = clientX - rect.left - margin.left;
    const index = points.length <= 1 ? 0 : Math.round((relative / plotWidth) * (points.length - 1));
    setActive(Math.min(points.length - 1, Math.max(0, index)));
  }
  if (!points.length) return <Empty title="暂无数据" />;
  const shown = active ?? null;
  return (
    <div
      ref={box}
      className="md-line-chart"
      tabIndex={0}
      role="img"
      aria-label={`${label}：${points.map((point) => `${point.day} ${format(point.value)}`).join("，")}`}
      onPointerMove={(event) => nearest(event.clientX)}
      onPointerLeave={() => setActive(null)}
      onFocus={() => setActive(last)}
      onBlur={() => setActive(null)}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") setActive((index) => Math.max(0, (index ?? last) - 1));
        if (event.key === "ArrowRight") setActive((index) => Math.min(last, (index ?? last) + 1));
      }}
    >
      <svg width={width} height={height} aria-hidden="true">
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="md-grid" x1={margin.left} x2={margin.left + plotWidth} y1={y(tick)} y2={y(tick)} />
            <text className="md-axis-label" x={margin.left - 8} y={y(tick) + 4} textAnchor="end">
              {format(tick)}
            </text>
          </g>
        ))}
        <line className="md-baseline" x1={margin.left} x2={margin.left + plotWidth} y1={margin.top + plotHeight} y2={margin.top + plotHeight} />
        {labels.map((index) => (
          <text key={index} className="md-axis-label" x={x(index)} y={height - 8} textAnchor={index === 0 && points.length > 1 ? "start" : index === last && points.length > 1 ? "end" : "middle"}>
            {points[index]?.day.slice(5)}
          </text>
        ))}
        {area && <path className="md-series-area" d={area} />}
        {points.length > 1 && <path className="md-series-line" d={path} />}
        {shown !== null && <line className="md-crosshair" x1={x(shown)} x2={x(shown)} y1={margin.top} y2={margin.top + plotHeight} />}
        <circle className="md-series-dot" cx={x(shown ?? last)} cy={y(points[shown ?? last]?.value ?? 0)} r={4} />
        <text className="md-end-label" x={x(last) + 10} y={y(points[last]?.value ?? 0) + 4}>
          {format(points[last]?.value ?? 0)}
        </text>
      </svg>
      {shown !== null && points[shown] && (
        <div className="md-tooltip" style={{ left: Math.min(width - 150, Math.max(0, x(shown) + 10)), top: Math.max(0, y(points[shown].value) - 54) }}>
          <strong>{format(points[shown].value)}</strong>
          <span>
            <i />
            {label}
          </span>
          <small>{points[shown].day}</small>
        </div>
      )}
    </div>
  );
}

function BarList({ items, format, max, empty }: { items: { key: string; label: string; value: number; hint?: string; onClick?: () => void }[]; format: (value: number) => string; max?: number; empty: string }) {
  if (!items.length) return <p className="muted">{empty}</p>;
  const top = max ?? Math.max(...items.map((item) => item.value), 1);
  return (
    <ul className="md-bars">
      {items.map((item) => (
        <li key={item.key} title={`${item.hint ?? item.label}：${format(item.value)}`}>
          {item.onClick ? (
            <button type="button" className="md-link md-bar-label" onClick={item.onClick}>
              {item.label}
            </button>
          ) : (
            <span className="md-bar-label">{item.label}</span>
          )}
          <span className="md-bar-track">
            <i style={{ width: `${Math.max(0, Math.min(100, (item.value / top) * 100))}%` }} />
          </span>
          <span className="md-bar-value">{format(item.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ KPIs */

const STATUS: Record<string, { label: string; icon: typeof CircleCheck; tone: string }> = {
  achieved: { label: "已达成", icon: CircleCheck, tone: "good" },
  on_track: { label: "进展正常", icon: CircleCheck, tone: "accent" },
  at_risk: { label: "存在风险", icon: TriangleAlert, tone: "warning" },
  expired: { label: "已到期未达成", icon: CircleAlert, tone: "critical" },
  pending: { label: "未开始", icon: Clock, tone: "neutral" },
};

function KpiMeter({ kpi }: { kpi: Kpi }) {
  const status = STATUS[kpi.status] ?? STATUS.pending;
  const unit = kpi.metric_type === "PERCENTAGE" ? percent : count;
  return (
    <div className="md-kpi-meter">
      <div className={`md-meter ${status.tone}`} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={kpi.progress} aria-label={`${entityName(kpi)} 完成度`}>
        <i style={{ width: `${kpi.progress}%` }} />
      </div>
      <span className={`md-kpi-status ${status.tone}`}>
        <status.icon size={13} />
        {status.label}
      </span>
      <small>
        当前 {unit(kpi.current_value)} / 目标 {unit(kpi.target_value)} · 完成 {kpi.progress.toFixed(0)}%
      </small>
    </div>
  );
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  return (
    <div className="md-kpi-card">
      <div>
        <strong>{entityName(kpi)}</strong>
        <small>{kpi.chart_label}</small>
      </div>
      <KpiMeter kpi={kpi} />
      <small className="md-block-muted">
        {String(kpi.start_date)} ~ {String(kpi.end_date)} · 剩余 {kpi.days_left} 天
      </small>
    </div>
  );
}

function KpiTable({ kpis, onChanged }: { kpis: Kpi[]; onChanged: () => void }) {
  const [error, setError] = useState("");
  async function remove(kpi: Kpi) {
    if (!window.confirm(`删除 KPI ${entityName(kpi)}？`)) return;
    try {
      await mdPost("/entities/delete", { entity_type: "kpi", ref: kpi.fqn, hard: true });
      onChanged();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>KPI 列表</h2>
        <button type="button" className="primary-button" onClick={() => navigate("insights", "kpi-new")}>
          <Plus size={13} />
          添加KPI
        </button>
      </div>
      {error && <ErrorBanner message={error} />}
      {kpis.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>图表</th>
                <th>进度</th>
                <th>日期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {kpis.map((kpi) => (
                <tr key={kpi.id}>
                  <td>
                    <strong>{entityName(kpi)}</strong>
                  </td>
                  <td>{kpi.chart_label}</td>
                  <td className="md-kpi-cell">
                    <KpiMeter kpi={kpi} />
                  </td>
                  <td>
                    {String(kpi.start_date)} ~ {String(kpi.end_date)}
                  </td>
                  <td>
                    <div className="md-actions">
                      <button type="button" className="md-edit" aria-label={`编辑 ${entityName(kpi)}`} onClick={() => navigate("insights", "kpi", kpi.fqn)}>
                        <Pencil size={13} />
                      </button>
                      <button type="button" className="md-edit" aria-label={`删除 ${entityName(kpi)}`} onClick={() => void remove(kpi)}>
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title="还没有 KPI">KPI 为描述、所有者或分级的覆盖率设定目标与期限，洞察页会跟踪进展。</Empty>
      )}
    </section>
  );
}

function today(offset = 0): string {
  const date = new Date(Date.now() + offset * 86400000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function KpiForm({ fqn }: { fqn?: string }) {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const charts = types.data?.kpi_charts ?? [];
  const [chart, setChart] = useState("percentage_of_entities_with_description_by_type");
  const [displayName, setDisplayName] = useState("");
  const [target, setTarget] = useState(80);
  const [start, setStart] = useState(today());
  const [end, setEnd] = useState(today(30));
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(!fqn);
  const metric = charts.find((item) => item.id === chart)?.metric_type ?? "PERCENTAGE";
  useEffect(() => {
    if (!fqn) return;
    mdGet<Kpi>("/entity", { entity_type: "kpi", ref: fqn })
      .then((kpi) => {
        setChart(String(kpi.chart));
        setDisplayName(kpi.display_name);
        setTarget(Number(kpi.target_value));
        setStart(String(kpi.start_date));
        setEnd(String(kpi.end_date));
        setDescription(kpi.description);
        setLoaded(true);
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [fqn]);
  async function save() {
    setBusy(true);
    setError("");
    const fields = { chart, metric_type: metric, target_value: target, start_date: start, end_date: end };
    try {
      if (fqn) await mdPost("/entities/update", { entity_type: "kpi", ref: fqn, display_name: displayName, description, fields });
      else {
        const slug = displayName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
        await mdPost("/entities", { entity_type: "kpi", name: slug || `kpi-${Date.now().toString(36)}`, display_name: displayName, description, fields });
      }
      navigate("insights", "kpis");
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  if (!loaded) return error ? <ErrorBanner message={error} /> : <Loading />;
  return (
    <div className="page-content md-page">
      <div className="md-breadcrumbs">
        <button type="button" className="md-link" onClick={() => navigate("insights")}>
          数据洞察
        </button>
        <i>/</i>
        <button type="button" className="md-link" onClick={() => navigate("insights", "kpis")}>
          KPI 列表
        </button>
        <i>/</i>
        <b>{fqn ? "编辑KPI" : "添加新KPI"}</b>
      </div>
      <div className="md-form-layout">
        <div className="md-form">
          <h2>{fqn ? "编辑KPI" : "添加新KPI"}</h2>
          <FormRow label="选择图表" htmlFor="kpi-chart" required>
            <select id="kpi-chart" value={chart} onChange={(event) => setChart(event.target.value)}>
              {charts.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </FormRow>
          <FormRow label="显示名称" htmlFor="kpi-name" required>
            <input id="kpi-name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如 Q4 描述覆盖率" />
          </FormRow>
          <FormRow label="指标类型" htmlFor="kpi-metric">
            <select id="kpi-metric" value={metric} disabled>
              <option value="PERCENTAGE">百分比</option>
              <option value="NUMBER">数值</option>
            </select>
          </FormRow>
          <FormRow label="指标值" htmlFor="kpi-target" required>
            <div className="md-slider">
              {metric === "PERCENTAGE" && <input type="range" min={0} max={100} value={target} aria-label="目标百分比" onChange={(event) => setTarget(Number(event.target.value))} />}
              <input id="kpi-target" type="number" min={0} max={metric === "PERCENTAGE" ? 100 : undefined} value={target} onChange={(event) => setTarget(Number(event.target.value))} />
              {metric === "PERCENTAGE" && <span>%</span>}
            </div>
          </FormRow>
          <div className="md-form-inline">
            <FormRow label="开始日期" htmlFor="kpi-start" required>
              <input id="kpi-start" type="date" value={start} onChange={(event) => setStart(event.target.value)} />
            </FormRow>
            <FormRow label="结束日期" htmlFor="kpi-end" required>
              <input id="kpi-end" type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
            </FormRow>
          </div>
          <FormRow label="描述">
            <MarkdownEditor value={description} onChange={setDescription} />
          </FormRow>
          {error && <ErrorBanner message={error} />}
          <div className="md-form-actions">
            <button type="button" className="text-button" onClick={() => navigate("insights", "kpis")}>
              取消
            </button>
            <button type="button" className="primary-button" disabled={busy || !displayName.trim()} onClick={() => void save()}>
              提 交
            </button>
          </div>
        </div>
        <aside className="md-help">
          <h3>添加KPI</h3>
          <p>
            确定最能反映数据资产健康状况的关键绩效指标（KPI）。基于描述信息、所有权和数据分级来审查您的数据资产。定义您的目标指标（绝对值或百分比），以跟踪您的进展。最后，设置开始和结束日期以实现您的数据目标。
          </p>
          <h4>进度状态</h4>
          <p>未开始 · 进展正常（完成度不低于已过时间的九成）· 存在风险 · 已达成 · 已到期未达成。</p>
        </aside>
      </div>
    </div>
  );
}
