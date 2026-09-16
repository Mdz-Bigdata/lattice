// SPDX-License-Identifier: Apache-2.0
/**
 * 数据血缘: a layered lineage graph (upstream on the left, downstream on the right) with column-level
 * mappings, editing (add/remove edges, derive edges from SQL) and impact analysis.
 *
 * The layout is computed here instead of by a graph library: nodes sit in one column per lineage
 * depth, ordered to follow their neighbours, and edges are cubic curves drawn in an SVG under the
 * node cards. Expanding two connected nodes draws their column-to-column mappings as well.
 */
import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Code,
  Maximize2,
  Minus,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { errorMessage } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  entityName,
  leafName,
  mdPost,
  metadataPath,
  navigate,
  openEntity,
  typeLabel,
  type Entity,
  type Impact,
  type LineageEdge,
  type LineageGraph,
  type LineageNode,
  type SearchResult,
  type SqlLineage,
} from "./metadataApi";
import { EntityIcon, EntityLink, Modal, SearchBox, useDebounced } from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";
import { RootCausePanel } from "./AiPanels";

const NODE_WIDTH = 230;
const NODE_HEAD = 58;
const COLUMN_ROW = 22;
const MAX_COLUMNS = 12;
const GAP_X = 120;
const GAP_Y = 22;
const PAD = 24;
const SOURCE_LABELS: Record<string, string> = {
  manual: "手工登记",
  query: "SQL 解析",
  view: "视图定义",
  pipeline: "工作流",
  semantic: "语义模型",
  polaris: "Polaris 接入",
  import: "导入",
  dbt: "dbt",
};

interface Placed {
  node: LineageNode;
  x: number;
  y: number;
  height: number;
  expanded: boolean;
}

function layout(graph: LineageGraph, expanded: Set<string>): { placed: Map<string, Placed>; width: number; height: number } {
  const columns = new Map<number, LineageNode[]>();
  for (const node of graph.nodes) columns.set(node.depth, [...(columns.get(node.depth) ?? []), node]);
  const depths = [...columns.keys()].sort((a, b) => a - b);
  const order = new Map<string, number>();
  const neighbours = new Map<string, string[]>();
  for (const edge of graph.edges) {
    neighbours.set(edge.to_fqn, [...(neighbours.get(edge.to_fqn) ?? []), edge.from_fqn]);
    neighbours.set(edge.from_fqn, [...(neighbours.get(edge.from_fqn) ?? []), edge.to_fqn]);
  }
  const placed = new Map<string, Placed>();
  let height = 0;
  depths.forEach((depth, columnIndex) => {
    const nodes = [...(columns.get(depth) ?? [])];
    // Order each column by the average position of already placed neighbours to limit crossings.
    nodes.sort((a, b) => {
      const score = (node: LineageNode) => {
        const known = (neighbours.get(node.fqn) ?? []).map((fqn) => order.get(fqn)).filter((value): value is number => value !== undefined);
        return known.length ? known.reduce((sum, value) => sum + value, 0) / known.length : Number.MAX_SAFE_INTEGER;
      };
      return score(a) - score(b) || a.fqn.localeCompare(b.fqn);
    });
    let y = PAD;
    nodes.forEach((node, index) => {
      order.set(node.fqn, index);
      const open = expanded.has(node.fqn) && node.columns.length > 0;
      const nodeHeight = NODE_HEAD + (open ? Math.min(node.columns.length, MAX_COLUMNS) * COLUMN_ROW + 8 : 0);
      placed.set(node.fqn, { node, x: PAD + columnIndex * (NODE_WIDTH + GAP_X), y, height: nodeHeight, expanded: open });
      y += nodeHeight + GAP_Y;
    });
    height = Math.max(height, y);
  });
  const width = PAD * 2 + depths.length * NODE_WIDTH + Math.max(0, depths.length - 1) * GAP_X;
  return { placed, width, height: height + PAD };
}

function curve(x1: number, y1: number, x2: number, y2: number): string {
  const bend = Math.max(40, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
}

function columnY(place: Placed, fqn: string): number | null {
  if (!place.expanded) return null;
  const index = place.node.columns.indexOf(leafName(fqn));
  if (index < 0 || index >= MAX_COLUMNS) return null;
  return place.y + NODE_HEAD + index * COLUMN_ROW + COLUMN_ROW / 2 + 2;
}

export function LineageGraphView({
  entityType,
  fqn,
  editable = true,
  serviceFqn,
}: {
  entityType: string;
  fqn: string;
  editable?: boolean;
  serviceFqn?: string;
}) {
  const [upstream, setUpstream] = useState(2);
  const [downstream, setDownstream] = useState(2);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [zoom, setZoom] = useState(1);
  const [selectedEdge, setSelectedEdge] = useState<LineageEdge | null>(null);
  const [focusColumn, setFocusColumn] = useState("");
  const [dialog, setDialog] = useState<"" | "upstream" | "downstream" | "sql">("");
  const [error, setError] = useState("");
  const graph = useApi<LineageGraph>(
    metadataPath("/lineage", { entity_type: entityType, ref: fqn, upstream_depth: upstream, downstream_depth: downstream }),
  );
  const view = useMemo(() => (graph.data ? layout(graph.data, expanded) : null), [graph.data, expanded]);
  function toggle(nodeFqn: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(nodeFqn)) next.delete(nodeFqn);
      else next.add(nodeFqn);
      return next;
    });
  }
  async function removeEdge(edge: LineageEdge) {
    if (!window.confirm(`确认删除血缘 ${edge.from_fqn} → ${edge.to_fqn}？`)) return;
    try {
      await mdPost("/lineage/edges/delete", { from_fqn: edge.from_fqn, to_fqn: edge.to_fqn });
      setSelectedEdge(null);
      graph.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const highlighted = (edge: LineageEdge) =>
    !!focusColumn && edge.columns.some((mapping) => mapping.to_column === focusColumn || mapping.from_columns.includes(focusColumn));
  return (
    <div className="md-lineage">
      <div className="md-lineage-toolbar">
        <label>
          上游层数
          <select value={upstream} onChange={(event) => setUpstream(Number(event.target.value))}>
            {[0, 1, 2, 3, 4, 5, 6].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label>
          下游层数
          <select value={downstream} onChange={(event) => setDownstream(Number(event.target.value))}>
            {[0, 1, 2, 3, 4, 5, 6].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <span className="md-lineage-counts">
          上游 {graph.data?.upstream_count ?? 0} · 下游 {graph.data?.downstream_count ?? 0}
          {graph.data?.truncated && "（节点过多，已截断）"}
        </span>
        <span className="md-actions">
          {editable && (
            <>
              <button type="button" className="small-button" onClick={() => setDialog("upstream")}>
                <Plus size={12} />
                添加上游
              </button>
              <button type="button" className="small-button" onClick={() => setDialog("downstream")}>
                <Plus size={12} />
                添加下游
              </button>
              <button type="button" className="small-button" onClick={() => setDialog("sql")}>
                <Code size={12} />
                从 SQL 解析
              </button>
            </>
          )}
          <button type="button" className="icon-button" aria-label="缩小" onClick={() => setZoom((value) => Math.max(0.4, value - 0.1))}>
            <Minus size={14} />
          </button>
          <span className="md-zoom">{Math.round(zoom * 100)}%</span>
          <button type="button" className="icon-button" aria-label="放大" onClick={() => setZoom((value) => Math.min(1.6, value + 0.1))}>
            <Plus size={14} />
          </button>
          <button type="button" className="icon-button" aria-label="重置缩放" onClick={() => setZoom(1)}>
            <Maximize2 size={14} />
          </button>
          <button type="button" className="icon-button" aria-label="刷新血缘" onClick={graph.reload}>
            <RefreshCw size={14} className={graph.loading ? "spin" : ""} />
          </button>
        </span>
      </div>
      {error && <ErrorBanner message={error} />}
      {graph.error && <ErrorBanner message={graph.error} />}
      {graph.loading && !graph.data && <Loading text="正在读取血缘…" />}
      {graph.data && view && (
        <div className="md-lineage-body">
          <div className="md-lineage-canvas">
            {graph.data.nodes.length <= 1 && (
              <p className="md-lineage-hint">暂无血缘关系。可以手工添加上下游，或从 SQL / 视图定义中解析。</p>
            )}
            <div className="md-lineage-stage" style={{ width: view.width * zoom, height: view.height * zoom }}>
              <div style={{ width: view.width, height: view.height, transform: `scale(${zoom})`, transformOrigin: "0 0", position: "relative" }}>
                <svg className="md-lineage-svg" width={view.width} height={view.height} aria-hidden="true">
                  <defs>
                    <marker id="md-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
                    </marker>
                  </defs>
                  {graph.data.edges.map((edge) => {
                    const from = view.placed.get(edge.from_fqn);
                    const to = view.placed.get(edge.to_fqn);
                    if (!from || !to) return null;
                    const selected = selectedEdge?.id === edge.id;
                    const mappings = from.expanded && to.expanded ? edge.columns : [];
                    return (
                      <g key={edge.id} className={`md-edge ${selected ? "selected" : ""} ${highlighted(edge) ? "highlight" : ""}`}>
                        <path
                          d={curve(from.x + NODE_WIDTH, from.y + NODE_HEAD / 2, to.x, to.y + NODE_HEAD / 2)}
                          markerEnd="url(#md-arrow)"
                        />
                        <path
                          className="md-edge-hit"
                          d={curve(from.x + NODE_WIDTH, from.y + NODE_HEAD / 2, to.x, to.y + NODE_HEAD / 2)}
                          onClick={() => setSelectedEdge(edge)}
                        />
                        {mappings.flatMap((mapping) =>
                          mapping.from_columns.map((source) => {
                            const y1 = columnY(from, source);
                            const y2 = columnY(to, mapping.to_column);
                            if (y1 === null || y2 === null) return null;
                            const active = focusColumn === source || focusColumn === mapping.to_column;
                            return (
                              <path
                                key={`${source}->${mapping.to_column}`}
                                className={`md-column-edge ${active ? "active" : ""}`}
                                d={curve(from.x + NODE_WIDTH, y1, to.x, y2)}
                              />
                            );
                          }),
                        )}
                      </g>
                    );
                  })}
                </svg>
                {[...view.placed.values()].map((place) => (
                  <LineageCard
                    key={place.node.fqn}
                    place={place}
                    root={place.node.fqn === graph.data?.entity.fqn}
                    focusColumn={focusColumn}
                    onToggle={() => toggle(place.node.fqn)}
                    onColumn={(column) => setFocusColumn((current) => (current === column ? "" : column))}
                  />
                ))}
              </div>
            </div>
          </div>
          {selectedEdge && (
            <EdgePanel edge={selectedEdge} editable={editable} onClose={() => setSelectedEdge(null)} onDelete={() => void removeEdge(selectedEdge)} />
          )}
        </div>
      )}
      {(dialog === "upstream" || dialog === "downstream") && graph.data && (
        <AddEdgeModal
          root={graph.data.entity}
          direction={dialog}
          onClose={() => setDialog("")}
          onDone={() => {
            setDialog("");
            graph.reload();
          }}
        />
      )}
      {dialog === "sql" && graph.data && (
        <SqlLineageModal
          target={graph.data.entity.fqn}
          serviceFqn={serviceFqn}
          onClose={() => setDialog("")}
          onDone={() => {
            setDialog("");
            graph.reload();
          }}
        />
      )}
    </div>
  );
}

function LineageCard({
  place,
  root,
  focusColumn,
  onToggle,
  onColumn,
}: {
  place: Placed;
  root: boolean;
  focusColumn: string;
  onToggle: () => void;
  onColumn: (fqn: string) => void;
}) {
  const { node } = place;
  return (
    <div
      className={`md-node ${root ? "root" : ""} ${node.deleted ? "deleted" : ""} ${node.missing ? "missing" : ""}`}
      style={{ left: place.x, top: place.y, width: NODE_WIDTH, height: place.height }}
    >
      <div className="md-node-head">
        <EntityIcon type={node.entity_type || "table"} size={15} />
        <div>
          {node.missing ? (
            <strong title={node.fqn}>{node.display_name}</strong>
          ) : (
            <EntityLink entity={node}>{entityName(node)}</EntityLink>
          )}
          <small title={node.fqn}>
            {node.type_label}
            {node.service_type ? ` · ${node.service_type}` : ""}
          </small>
        </div>
        {node.columns.length > 0 && (
          <button type="button" className="icon-button" aria-label={place.expanded ? "收起字段" : "展开字段"} onClick={onToggle}>
            {place.expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        )}
      </div>
      {place.expanded && (
        <ul className="md-node-columns">
          {node.columns.slice(0, MAX_COLUMNS).map((column) => {
            const columnFqn = `${node.fqn}.${column}`;
            return (
              <li key={column}>
                <button type="button" className={focusColumn === columnFqn ? "active" : ""} onClick={() => onColumn(columnFqn)}>
                  {column}
                </button>
              </li>
            );
          })}
          {node.columns.length > MAX_COLUMNS && <li className="muted">…另有 {node.columns.length - MAX_COLUMNS} 个字段</li>}
        </ul>
      )}
    </div>
  );
}

function EdgePanel({ edge, editable, onClose, onDelete }: { edge: LineageEdge; editable: boolean; onClose: () => void; onDelete: () => void }) {
  return (
    <aside className="md-edge-panel">
      <div className="md-summary-head">
        <strong>血缘详情</strong>
        <button type="button" className="icon-button" aria-label="关闭血缘详情" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      <p className="md-edge-ends">
        <EntityLink entity={{ entity_type: edge.from_type, fqn: edge.from_fqn }}>{edge.from_fqn}</EntityLink>
        <span>→</span>
        <EntityLink entity={{ entity_type: edge.to_type, fqn: edge.to_fqn }}>{edge.to_fqn}</EntityLink>
      </p>
      <p className="muted">来源：{SOURCE_LABELS[edge.source] ?? edge.source}</p>
      {edge.description && <p>{edge.description}</p>}
      {edge.pipeline_fqn && (
        <p>
          工作流：<EntityLink entity={{ entity_type: "pipeline", fqn: edge.pipeline_fqn }} />
        </p>
      )}
      {edge.columns.length > 0 && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>上游字段</th>
                <th>下游字段</th>
                <th>转换</th>
              </tr>
            </thead>
            <tbody>
              {edge.columns.map((mapping) => (
                <tr key={mapping.to_column}>
                  <td>{mapping.from_columns.map(leafName).join("、")}</td>
                  <td>{leafName(mapping.to_column)}</td>
                  <td>{mapping.function ? <code>{mapping.function}</code> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {edge.sql && <pre className="sql-code">{edge.sql}</pre>}
      {editable && (
        <button type="button" className="danger-button" onClick={onDelete}>
          <Trash2 size={13} />
          删除这条血缘
        </button>
      )}
    </aside>
  );
}

function AssetSearch({ exclude, onPick }: { exclude: string; onPick: (entity: Entity) => void }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query, 250);
  const result = useApi<SearchResult>(metadataPath("/search", { q, size: 12 }));
  return (
    <>
      <SearchBox value={query} onChange={setQuery} placeholder="搜索数据资产" />
      {result.error && <ErrorBanner message={result.error} />}
      <div className="md-picker-list">
        {(result.data?.items ?? [])
          .filter((item) => item.fqn !== exclude)
          .map((item) => (
            <button type="button" key={item.id} className="md-picker-item md-picker-button" onClick={() => onPick(item)}>
              <EntityIcon type={item.entity_type} size={14} />
              <span>
                <strong>{entityName(item)}</strong>
                <small>
                  {typeLabel(item.entity_type)} · {item.fqn}
                </small>
              </span>
            </button>
          ))}
        {result.loading && <Loading text="正在搜索…" />}
      </div>
    </>
  );
}

function AddEdgeModal({
  root,
  direction,
  onClose,
  onDone,
}: {
  root: { entity_type: string; fqn: string };
  direction: "upstream" | "downstream";
  onClose: () => void;
  onDone: () => void;
}) {
  const [other, setOther] = useState<Entity | null>(null);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    if (!other) return;
    setBusy(true);
    setError("");
    const [from, to] = direction === "upstream" ? [other, root] : [root, other];
    try {
      await mdPost("/lineage/edges", {
        from_fqn: from.fqn,
        from_type: from.entity_type,
        to_fqn: to.fqn,
        to_type: to.entity_type,
        description,
      });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <Modal
      title={direction === "upstream" ? "添加上游对象" : "添加下游对象"}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={!other || busy} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      {other ? (
        <div className="md-chosen">
          <EntityIcon type={other.entity_type} />
          <span>
            {direction === "upstream" ? `${other.fqn} → ${root.fqn}` : `${root.fqn} → ${other.fqn}`}
          </span>
          <button type="button" className="text-button" onClick={() => setOther(null)}>
            重新选择
          </button>
        </div>
      ) : (
        <AssetSearch exclude={root.fqn} onPick={setOther} />
      )}
      <label className="md-form-row">
        <span>描述（可选）</span>
        <input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="例如：每日汇总任务写入" />
      </label>
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

function SqlLineageModal({
  target,
  serviceFqn,
  onClose,
  onDone,
}: {
  target?: string;
  serviceFqn?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [sql, setSql] = useState("");
  const [asTarget, setAsTarget] = useState(!!target);
  const [preview, setPreview] = useState<SqlLineage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function run(apply: boolean) {
    setBusy(true);
    setError("");
    try {
      const result = await mdPost<SqlLineage>("/lineage/sql", {
        sql,
        service_fqn: serviceFqn || undefined,
        target: asTarget ? target : undefined,
        apply,
      });
      setPreview(result);
      if (apply) onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="从 SQL 解析血缘"
      width={720}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="secondary-button" disabled={!sql.trim() || busy} onClick={() => void run(false)}>
            预览
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={!sql.trim() || busy || (preview !== null && !preview.target)}
            onClick={() => void run(true)}
          >
            写入血缘
          </button>
        </>
      }
    >
      <p className="muted">
        支持 INSERT … SELECT、CREATE TABLE … AS、CREATE VIEW 与 MERGE；纯 SELECT 语句需要指定写入目标。表名按{serviceFqn ? `服务 ${serviceFqn} ` : "全部服务"}中已登记的数据表解析。
      </p>
      <textarea className="code-editor" rows={8} value={sql} placeholder="INSERT INTO mart.daily_sales SELECT … FROM orders JOIN …" onChange={(event) => setSql(event.target.value)} />
      {target && (
        <label className="md-check">
          <input type="checkbox" checked={asTarget} onChange={(event) => setAsTarget(event.target.checked)} />
          将当前对象 {target} 作为写入目标（语句本身未写明目标时使用）
        </label>
      )}
      {error && <ErrorBanner message={error} />}
      {preview && (
        <div className="md-sql-preview">
          <p>
            目标：{preview.target ? <strong>{preview.target.fqn}</strong> : <span className="failure-badge">未识别{preview.target_reference ? `（${preview.target_reference} 未登记）` : ""}</span>}
            {preview.applied > 0 && <span className="healthy-badge">已写入 {preview.applied} 条</span>}
          </p>
          <p>
            上游：
            {preview.sources.length ? preview.sources.map((source) => <code key={source.fqn}>{source.fqn}</code>) : "无"}
          </p>
          {preview.unresolved.length > 0 && <p className="quality-warn-badge">未登记的表：{preview.unresolved.join("、")}</p>}
          {preview.columns.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>上游字段</th>
                    <th>下游字段</th>
                    <th>转换</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.columns.map((mapping) => (
                    <tr key={mapping.to_column}>
                      <td>{mapping.from_columns.join("、")}</td>
                      <td>{mapping.to_column}</td>
                      <td>{mapping.function || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ page */

export function LineagePage({ route }: MetadataViewProps) {
  const [entityType, fqn] = route.parts;
  const [sqlOpen, setSqlOpen] = useState(false);
  const summary = useApi<{ total: number; by_source: Record<string, number> }>(metadataPath("/lineage/summary"));
  const entity = useApi<Entity>(entityType && fqn ? metadataPath("/entity", { entity_type: entityType, ref: fqn }) : null);
  const impact = useApi<Impact>(entityType && fqn ? metadataPath("/lineage/impact", { entity_type: entityType, ref: fqn }) : null);
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>数据血缘</h1>
          <p>表级与字段级血缘：来自 SQL 解析、视图定义、Polaris 数据接入、语义模型与手工登记</p>
        </div>
        <div className="md-actions">
          {summary.data && (
            <span className="neutral-badge">
              共 {summary.data.total} 条
              {Object.entries(summary.data.by_source)
                .map(([source, count]) => ` · ${SOURCE_LABELS[source] ?? source} ${count}`)
                .join("")}
            </span>
          )}
          <button type="button" className="secondary-button" onClick={() => setSqlOpen(true)}>
            <Code size={14} />
            从 SQL 解析
          </button>
        </div>
      </div>
      <section className="panel">
        <div className="panel-body md-lineage-pick">
          <span>选择数据资产查看血缘：</span>
          <div className="md-lineage-picker">
            <AssetSearch exclude={fqn ?? ""} onPick={(item) => navigate("lineage", item.entity_type, item.fqn)} />
          </div>
        </div>
      </section>
      {!fqn && <Empty title="尚未选择数据资产">在上方搜索并选择一个数据表、仪表板、工作流或语义模型。</Empty>}
      {entityType && fqn && (
        <>
          <section className="panel">
            <div className="panel-heading">
              <h2>
                <EntityIcon type={entityType} />{" "}
                {entity.data ? <EntityLink entity={entity.data}>{entityName(entity.data)}</EntityLink> : fqn}
              </h2>
              <button type="button" className="text-button" onClick={() => openEntity(entityType, fqn, "lineage")}>
                打开资产详情
              </button>
            </div>
            <LineageGraphView key={`${entityType}:${fqn}`} entityType={entityType} fqn={fqn} serviceFqn={entity.data?.service_fqn} />
          </section>
          <section className="panel">
            <RootCausePanel entityType={entityType} fqn={fqn} />
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>影响分析</h2>
              <span>修改该对象会影响的全部下游对象</span>
            </div>
            <div className="panel-body">
              {impact.error && <ErrorBanner message={impact.error} />}
              {impact.data && (
                <>
                  <div className="md-impact-counts">
                    <strong>{impact.data.total}</strong> 个下游对象
                    {impact.data.by_type.map((item) => (
                      <span key={item.entity_type} className="neutral-badge">
                        {item.label} {item.count}
                      </span>
                    ))}
                  </div>
                  <ul className="md-impact-list">
                    {impact.data.items.map((item) => (
                      <li key={item.fqn}>
                        <span className="md-depth">第 {item.depth} 层</span>
                        <EntityIcon type={item.entity_type || "table"} size={14} />
                        {item.missing ? item.fqn : <EntityLink entity={item}>{item.fqn}</EntityLink>}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </section>
        </>
      )}
      {sqlOpen && (
        <SqlLineageModal
          onClose={() => setSqlOpen(false)}
          onDone={() => {
            setSqlOpen(false);
            summary.reload();
          }}
        />
      )}
    </div>
  );
}
