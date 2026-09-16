// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据资产 (OpenMetadata's Explore): the asset tree on the left, faceted search in the middle and
 * a summary of the selected asset on the right. Filters survive leaving the screen and coming back.
 */
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  ASSET_TYPES,
  CHILD_TYPES,
  SERVICE_ENTITY_TYPES,
  entityName,
  metadataPath,
  navigate,
  openEntity,
  splitFqn,
  tierLabel,
  typeLabel,
  type ChildrenResponse,
  type Entity,
  type Facet,
  type SearchResult,
  type TreeResponse,
} from "./metadataApi";
import {
  Description,
  EntityIcon,
  EntityLink,
  Facts,
  Owners,
  Pager,
  SearchBox,
  TagList,
  TierBadge,
  Toggle,
  useDebounced,
} from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";

interface Filters {
  q: string;
  types: string;
  serviceType: string;
  service: string;
  owner: string;
  tag: string;
  tier: string;
  domain: string;
  deleted: boolean;
  sort: string;
  page: number;
}
const EMPTY: Filters = {
  q: "",
  types: "",
  serviceType: "",
  service: "",
  owner: "",
  tag: "",
  tier: "",
  domain: "",
  deleted: false,
  sort: "name",
  page: 1,
};
const PAGE_SIZE = 15;
const CATEGORY_TYPES: Record<string, string[]> = {
  database: ["table", "storedProcedure"],
  messaging: ["topic"],
  dashboard: ["dashboard", "chart", "dashboardDataModel"],
  pipeline: ["pipeline"],
  mlmodel: ["mlmodel"],
  storage: ["container"],
  search: ["searchIndex"],
  api: ["apiCollection", "apiEndpoint"],
  metadata: [],
};
const GOVERNANCE_ROUTES: Record<string, string> = {
  glossary: "glossary",
  classification: "classification",
  domain: "domains",
  dataProduct: "domains",
};
let saved: Filters = EMPTY;
let savedOpen: Record<string, boolean> = { "category:database": true };

export default function ExplorePage(_props: MetadataViewProps) {
  const [filters, setFilters] = useState<Filters>(saved);
  const [selected, setSelected] = useState<Entity | null>(null);
  const [showSummary, setShowSummary] = useState(true);
  const q = useDebounced(filters.q, 300);
  useEffect(() => {
    saved = filters;
  }, [filters]);
  const result = useApi<SearchResult>(
    metadataPath("/search", {
      q,
      entity_types: filters.types,
      service_type: filters.serviceType,
      service_fqn: filters.service,
      owner: filters.owner,
      tag: filters.tag,
      tier: filters.tier,
      domain: filters.domain,
      deleted: filters.deleted || undefined,
      sort: filters.sort,
      page: filters.page,
      size: PAGE_SIZE,
    }),
  );
  useEffect(() => {
    const items = result.data?.items ?? [];
    setSelected((current) =>
      current && items.some((item) => item.id === current.id) ? current : (items[0] ?? null),
    );
  }, [result.data]);
  function update(patch: Partial<Filters>) {
    setFilters((current) => ({ ...current, page: 1, ...patch }));
  }
  const facets = result.data?.facets ?? {};
  const active = Object.entries(filters).some(
    ([key, value]) => key !== "sort" && key !== "page" && value !== EMPTY[key as keyof Filters],
  );
  return (
    <div className="md-explore">
      <AssetTree filters={filters} onFilter={update} />
      <div className="md-explore-main">
        <div className="md-explore-search">
          <SearchBox
            value={filters.q}
            onChange={(value) => update({ q: value })}
            placeholder="搜索表、字段、仪表板、工作流、术语…"
          />
        </div>
        <div className="md-filterbar">
          <FilterSelect
            label="数据资产"
            value={filters.types}
            options={ASSET_TYPES.map((type) => ({ value: type, label: typeLabel(type), count: count(facets.entity_type, type) }))}
            onChange={(value) => update({ types: value })}
          />
          <FilterSelect label="元数据工作区" value={filters.domain} options={facetOptions(facets.domain, filters.domain)} onChange={(value) => update({ domain: value })} />
          <FilterSelect label="所有者" value={filters.owner} options={facetOptions(facets.owner, filters.owner)} onChange={(value) => update({ owner: value })} />
          <FilterSelect label="标签" value={filters.tag} options={facetOptions(facets.tag, filters.tag)} onChange={(value) => update({ tag: value })} />
          <FilterSelect
            label="分级"
            value={filters.tier}
            options={[1, 2, 3, 4, 5].map((level) => ({ value: `Tier.Tier${level}`, label: `Tier${level}`, count: count(facets.tier, `Tier.Tier${level}`) }))}
            onChange={(value) => update({ tier: value })}
          />
          <FilterSelect label="服务" value={filters.service} options={facetOptions(facets.service, filters.service)} onChange={(value) => update({ service: value })} />
          <FilterSelect label="服务类型" value={filters.serviceType} options={facetOptions(facets.service_type, filters.serviceType)} onChange={(value) => update({ serviceType: value })} />
          <div className="md-filterbar-end">
            <Toggle checked={filters.deleted} onChange={(value) => update({ deleted: value })} label="已删除" />
            {active && (
              <button type="button" className="text-button" onClick={() => setFilters(EMPTY)}>
                <RotateCcw size={12} />
                清除
              </button>
            )}
            <select
              aria-label="排序"
              className="md-sort"
              value={filters.sort}
              onChange={(event) => update({ sort: event.target.value })}
            >
              <option value="name">按名称</option>
              <option value="updated">最近更新</option>
              <option value="created">最近创建</option>
            </select>
            <button
              type="button"
              className="icon-button"
              aria-label={showSummary ? "收起摘要" : "展开摘要"}
              onClick={() => setShowSummary(!showSummary)}
            >
              {showSummary ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
            </button>
          </div>
        </div>
        <div className={`md-results-wrap ${showSummary && selected ? "with-summary" : ""}`}>
          <div className="md-results">
            {result.loading && <Loading text="正在搜索元数据…" />}
            {result.error && <ErrorBanner message={result.error} />}
            {result.data && !result.data.items.length && (
              <Empty title="没有找到数据资产">
                可以调整筛选条件，或在「元数据拾取」中运行服务的拾取任务。
              </Empty>
            )}
            {result.data?.items.map((item) => (
              <ResultCard
                key={item.id}
                item={item}
                selected={selected?.id === item.id}
                onSelect={() => setSelected(item)}
              />
            ))}
            {result.data && (
              <Pager
                total={result.data.total}
                page={filters.page}
                size={PAGE_SIZE}
                onPage={(page) => setFilters((current) => ({ ...current, page }))}
              />
            )}
          </div>
          {showSummary && selected && (
            <SummaryPanel key={selected.id} item={selected} onClose={() => setShowSummary(false)} />
          )}
        </div>
      </div>
    </div>
  );
}

function count(facet: Facet[] | undefined, value: string): number | undefined {
  return facet?.find((item) => item.value === value)?.count;
}
function facetOptions(facet: Facet[] | undefined, current: string) {
  const options = (facet ?? []).map((item) => ({ value: item.value, label: item.value, count: item.count }));
  if (current && !options.some((item) => item.value === current)) options.unshift({ value: current, label: current, count: 0 });
  return options;
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string; count?: number }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className={`md-filter ${value ? "active" : ""}`}>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">全部</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
            {option.count !== undefined ? `（${option.count}）` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function ResultCard({ item, selected, onSelect }: { item: Entity; selected: boolean; onSelect: () => void }) {
  const parts = splitFqn(item.fqn);
  const trail = [item.service_type || typeLabel(item.entity_type), ...parts.slice(0, -1)];
  const labels = [...item.tags, ...item.glossary_terms];
  return (
    <article className={`md-result ${selected ? "selected" : ""}`} onClick={onSelect}>
      <div className="md-result-trail">
        <EntityIcon type={item.entity_type} size={14} />
        {trail.map((part, index) => (
          <span key={`${part}-${index}`}>
            {part}
            {index < trail.length - 1 && <i>/</i>}
          </span>
        ))}
      </div>
      <h3>
        <EntityLink entity={item}>{entityName(item)}</EntityLink>
        <span className="md-type-pill">{item.type_label}</span>
        {item.deleted && <span className="failure-badge">已删除</span>}
      </h3>
      <div className="md-result-desc">
        <Description text={item.description.length > 400 ? `${item.description.slice(0, 400)}…` : item.description} />
      </div>
      <div className="md-result-meta">
        <Owners owners={item.owners} />
        <span>·</span>
        <span>{item.domain ? entityName(item.domain) : "没有元数据工作区"}</span>
        <span>·</span>
        <TierBadge tier={item.tier} />
        {item.column_count !== undefined && (
          <>
            <span>·</span>
            <span>{item.column_count} 个字段</span>
          </>
        )}
        {labels.length > 0 && <TagList tags={labels.slice(0, 4)} />}
      </div>
    </article>
  );
}

function SummaryPanel({ item, onClose }: { item: Entity; onClose: () => void }) {
  const full = useApi<Entity>(metadataPath("/entity", { entity_type: item.entity_type, ref: item.fqn }));
  const entity = full.data ?? item;
  const usage = entity.usage ?? { queries: 0, views: 0 };
  return (
    <aside className="md-summary">
      <div className="md-summary-head">
        <EntityIcon type={entity.entity_type} />
        <EntityLink entity={entity}>{entityName(entity)}</EntityLink>
        <button type="button" className="icon-button" aria-label="收起摘要" onClick={onClose}>
          <PanelRightClose size={15} />
        </button>
      </div>
      <Owners owners={entity.owners} />
      <Facts
        items={[
          ["分级", entity.tier ? tierLabel(entity.tier) : "—"],
          ["服务", entity.service ? <EntityLink entity={entity.service} /> : entity.service_fqn || "—"],
          ["上级", entity.parent ? <EntityLink entity={entity.parent} /> : "—"],
          ["使用率", `近 30 天查询 ${usage.queries} 次，浏览 ${usage.views} 次`],
          ["版本", String(entity.version)],
        ]}
      />
      <h4>标签</h4>
      <TagList tags={entity.tags} empty="没有标签" />
      <h4>术语</h4>
      <TagList tags={entity.glossary_terms} empty="没有术语" />
      <h4>描述</h4>
      <Description text={entity.description} empty="未找到数据" />
      {entity.columns && entity.columns.length > 0 && (
        <>
          <h4>字段（{entity.columns.length}）</h4>
          <ul className="md-summary-columns">
            {entity.columns.slice(0, 15).map((column) => (
              <li key={column.name}>
                <code>{column.name}</code>
                <span>{column.data_type_display || column.data_type}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {full.error && <ErrorBanner message={full.error} />}
    </aside>
  );
}

/* ------------------------------------------------------------------ tree */

function AssetTree({ filters, onFilter }: { filters: Filters; onFilter: (patch: Partial<Filters>) => void }) {
  const tree = useApi<TreeResponse>(metadataPath("/tree"));
  const [open, setOpen] = useState<Record<string, boolean>>(savedOpen);
  useEffect(() => {
    savedOpen = open;
  }, [open]);
  function toggle(key: string) {
    setOpen((current) => ({ ...current, [key]: !current[key] }));
  }
  return (
    <aside className="md-tree">
      <div className="md-tree-head">
        <span>数据资产</span>
        <button type="button" className="icon-button" aria-label="刷新资产树" onClick={tree.reload}>
          <RefreshCw size={13} className={tree.loading ? "spin" : ""} />
        </button>
      </div>
      <div className="md-tree-body">
        {tree.error && <ErrorBanner message={tree.error} />}
        {tree.data?.categories.map((category) => {
          const key = `category:${category.id}`;
          const types = (CATEGORY_TYPES[category.id] ?? []).join(",");
          return (
            <Branch
              key={key}
              depth={0}
              open={!!open[key]}
              expandable={category.services.length > 0}
              onToggle={() => toggle(key)}
              active={!!types && filters.types === types && !filters.service}
              onSelect={() => onFilter({ types, service: "" })}
              icon={<EntityIcon type={category.entity_type} size={14} />}
              label={category.label}
              count={category.asset_count}
            >
              {category.services.map((service) => (
                <LazyNode
                  key={service.id}
                  entity={service}
                  depth={1}
                  activeService={filters.service}
                  onService={(fqn) => onFilter({ service: fqn, types: "" })}
                />
              ))}
            </Branch>
          );
        })}
        {tree.data && (
          <Branch
            depth={0}
            open={!!open.governance}
            expandable
            onToggle={() => toggle("governance")}
            active={false}
            onSelect={() => toggle("governance")}
            icon={<EntityIcon type="glossary" size={14} />}
            label="数据治理"
          >
            {tree.data.governance.map((item) => (
              <Branch
                key={item.id}
                depth={1}
                open={false}
                expandable={false}
                onToggle={() => undefined}
                active={item.id === "semanticModel" && filters.types === "semanticModel"}
                onSelect={() =>
                  item.id === "semanticModel"
                    ? onFilter({ types: "semanticModel", service: "" })
                    : navigate(GOVERNANCE_ROUTES[item.id] ?? "glossary")
                }
                icon={<EntityIcon type={item.entity_type} size={14} />}
                label={item.label}
                count={item.count}
              />
            ))}
          </Branch>
        )}
      </div>
    </aside>
  );
}

function Branch({
  depth,
  open,
  expandable,
  onToggle,
  active,
  onSelect,
  icon,
  label,
  count,
  title,
  children,
}: {
  depth: number;
  open: boolean;
  expandable: boolean;
  onToggle: () => void;
  active: boolean;
  onSelect: () => void;
  icon: ReactNode;
  label: string;
  count?: number;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="md-tree-node">
      <div className={`md-tree-row ${active ? "active" : ""}`} style={{ "--depth": depth } as CSSProperties}>
        <button
          type="button"
          className="md-tree-caret"
          disabled={!expandable}
          aria-label={open ? `折叠${label}` : `展开${label}`}
          onClick={onToggle}
        >
          {expandable ? open ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : <i />}
        </button>
        <button type="button" className="md-tree-label" title={title ?? label} onClick={onSelect}>
          {icon}
          <span>{label}</span>
          {count ? <small>{count}</small> : null}
        </button>
      </div>
      {open && children}
    </div>
  );
}

function LazyNode({
  entity,
  depth,
  activeService,
  onService,
}: {
  entity: Entity;
  depth: number;
  activeService: string;
  onService: (fqn: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const expandable = (entity.children_count ?? 0) > 0 && !!CHILD_TYPES[entity.entity_type];
  const children = useApi<ChildrenResponse>(
    open && expandable
      ? metadataPath("/entity/children", { entity_type: entity.entity_type, ref: entity.fqn })
      : null,
  );
  const groups = children.data?.groups ?? [];
  const isService = SERVICE_ENTITY_TYPES.includes(entity.entity_type);
  return (
    <Branch
      depth={depth}
      open={open}
      expandable={expandable}
      onToggle={() => setOpen(!open)}
      active={isService && activeService === entity.fqn}
      onSelect={() => (isService ? onService(entity.fqn) : openEntity(entity.entity_type, entity.fqn))}
      icon={<EntityIcon type={entity.entity_type} size={14} />}
      label={entityName(entity)}
      title={entity.fqn}
      count={entity.children_count}
    >
      {children.loading && (
        <div className="md-tree-loading" style={{ "--depth": depth + 1 } as CSSProperties}>
          加载中…
        </div>
      )}
      {children.error && <ErrorBanner message={children.error} />}
      {groups.map((group) => (
        <div key={group.entity_type}>
          {group.items.map((child) => (
            <LazyNode key={child.id} entity={child} depth={depth + 1} activeService={activeService} onService={onService} />
          ))}
          {group.total > group.items.length && (
            <button
              type="button"
              className="md-tree-more"
              style={{ "--depth": depth + 1 } as CSSProperties}
              onClick={() => openEntity(entity.entity_type, entity.fqn)}
            >
              查看全部 {group.total} 个{group.label}
            </button>
          )}
        </div>
      ))}
    </Branch>
  );
}
