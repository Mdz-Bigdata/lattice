// SPDX-License-Identifier: Apache-2.0
/**
 * The detail page of one catalog entity: header with ownership, tier and domain, type-specific tabs
 * (schema, children, details, activity, sample data, queries, lineage, AI context, usage, custom
 * properties) and the governance side panel (data products, tags, glossary terms).
 */
import { useEffect, useState } from "react";
import {
  ArrowRight,
  Bot,
  History,
  Key,
  Pencil,
  Plus,
  RotateCcw,
  Share2,
  Sparkles,
  Star,
  Trash2,
} from "lucide-react";
import { errorMessage } from "./api";
import { DataGrid, Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  CHILD_TYPES,
  COLUMN_TYPES,
  SERVICE_ENTITY_TYPES,
  entityName,
  formatTime,
  isAsset,
  mdGet,
  mdPost,
  metadataPath,
  navigate,
  openEntity,
  routeHash,
  timeAgo,
  typeLabel,
  type Column,
  type ContextCard,
  type Entity,
  type FeedEvent,
  type IngestionService,
  type Paged,
  type Suggestion,
  type TagLabel,
  type Thread,
  type VersionItem,
} from "./metadataApi";
import {
  Breadcrumbs,
  CopyButton,
  Description,
  EntityIcon,
  EntityLink,
  MarkdownEditor,
  MarkdownView,
  Modal,
  Owners,
  Pager,
  PeoplePickerModal,
  RunStatus,
  SearchBox,
  TagChip,
  TagList,
  TagPickerModal,
  TierBadge,
  Toggle,
} from "./MetadataWidgets";
import { LineageGraphView } from "./MetadataLineage";
import type { MetadataViewProps } from "./MetadataPage";

type Dialog =
  | { kind: "description" | "owners" | "tier" | "domain" | "tags" | "terms" | "products" | "versions" | "suggest" }
  | { kind: "column"; column: Column }
  | { kind: "columnTags"; column: Column; source: "classification" | "glossary" };

const BASE_KEYS = new Set([
  "id", "entity_type", "type_label", "name", "display_name", "fqn", "parent_fqn", "service_fqn", "service_type",
  "description", "tier", "domain", "deleted", "version", "updated_at", "updated_by", "created_at", "owners", "tags",
  "glossary_terms", "columns", "column_count", "data_products", "followers", "followers_count", "children_count",
  "usage", "service", "parent", "breadcrumb", "extension", "style", "ingestion",
]);

function tabsFor(entity: Entity) {
  const type = entity.entity_type;
  const tabs: { id: string; label: string }[] = [];
  if (COLUMN_TYPES.has(type)) tabs.push({ id: "schema", label: type === "table" ? "数据模式" : "字段" });
  for (const child of CHILD_TYPES[type] ?? []) tabs.push({ id: `children:${child}`, label: typeLabel(child) });
  tabs.push({ id: "details", label: "详情" });
  tabs.push({ id: "activity", label: "活动信息流" });
  if (type === "table") tabs.push({ id: "sample", label: "样本数据" }, { id: "queries", label: "查询" });
  if (isAsset(type)) tabs.push({ id: "lineage", label: "血缘关系" });
  if (type === "tag" || type === "glossaryTerm") tabs.push({ id: "usage", label: "使用情况" });
  if (type === "user" || type === "team") tabs.push({ id: "owned", label: "拥有的资产" });
  if (type === "domain" || type === "dataProduct") tabs.push({ id: "assets", label: "资产" });
  tabs.push({ id: "context", label: "AI 上下文" });
  tabs.push({ id: "properties", label: "自定义属性" });
  return tabs;
}

export default function EntityPage(props: MetadataViewProps) {
  const [entityType, fqn, tabId] = props.route.parts;
  const [entity, setEntity] = useState<Entity | null>(null);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<Dialog | null>(null);
  useEffect(() => {
    let active = true;
    if (!entityType || !fqn) return;
    mdGet<Entity>("/entity", { entity_type: entityType, ref: fqn, view: true })
      .then((data) => active && setEntity(data))
      .catch((e: unknown) => active && setLoadError(errorMessage(e)));
    return () => {
      active = false;
    };
  }, [entityType, fqn]);
  if (!entityType || !fqn) return <Empty title="未指定数据资产" />;
  if (loadError) return <ErrorBanner message={loadError} />;
  if (!entity) return <Loading text="正在读取元数据…" />;
  const current: Entity = entity;
  const tabs = tabsFor(current);
  const tab = tabs.some((item) => item.id === tabId) ? tabId : tabs[0]?.id;
  async function run(action: () => Promise<Entity>) {
    setError("");
    try {
      setEntity(await action());
    } catch (e) {
      setError(errorMessage(e));
      throw e;
    }
  }
  const update = (patch: Record<string, unknown>) =>
    run(() => mdPost<Entity>("/entities/update", { entity_type: current.entity_type, ref: current.fqn, ...patch }));
  const setTags = (target: string, tags: { tag_fqn: string; source: string }[]) =>
    mdPost<Entity>("/entities/tags", { target_fqn: target, tags });
  const classificationOf = (labels: TagLabel[]) => labels.map((tag) => ({ tag_fqn: tag.tag_fqn, source: "classification" }));
  const termsOf = (labels: TagLabel[]) => labels.map((tag) => ({ tag_fqn: tag.tag_fqn, source: "glossary" }));
  async function remove(hard: boolean) {
    const message = hard
      ? `永久删除 ${current.fqn} 及其全部下级对象？标签、血缘与版本历史会一并删除，无法恢复。`
      : `删除 ${current.fqn}？删除后可在「已删除」中恢复。`;
    if (!window.confirm(message)) return;
    try {
      await mdPost("/entities/delete", { entity_type: current.entity_type, ref: current.fqn, hard });
      if (hard) navigate("explore");
      else setEntity(await mdGet<Entity>("/entity", { entity_type: current.entity_type, ref: current.id }));
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  async function rename() {
    const name = window.prompt("新的名称（会同步修改全部下级对象的完整名称）", current.name);
    if (!name || name === current.name) return;
    try {
      const renamed = await mdPost<Entity>("/entities/rename", { entity_type: current.entity_type, ref: current.fqn, name });
      openEntity(renamed.entity_type, renamed.fqn, tab);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const following = (current.followers ?? []).some((user) => user.name === "lattice");
  const asset = isAsset(current.entity_type);
  return (
    <div className="page-content md-page md-entity">
      <div className="md-entity-head">
        <div className="md-entity-title">
          <Breadcrumbs items={current.breadcrumb ?? []} />
          <h1>
            <EntityIcon type={current.entity_type} size={26} />
            <span>{entityName(current)}</span>
            {current.display_name && current.display_name !== current.name && <small>{current.name}</small>}
            <span className="md-type-pill">{current.type_label}</span>
            {current.deleted && <span className="failure-badge">已删除</span>}
          </h1>
          <div className="md-entity-meta">
            <span>
              <EntityIcon type="domain" size={13} />
              {current.domain ? <EntityLink entity={current.domain} /> : <span className="md-muted-inline">没有元数据工作区</span>}
              <button type="button" className="md-edit" aria-label="编辑元数据工作区" onClick={() => setDialog({ kind: "domain" })}>
                <Pencil size={12} />
              </button>
            </span>
            <span>
              <Owners owners={current.owners} />
              <button type="button" className="md-edit" aria-label="编辑所有者" onClick={() => setDialog({ kind: "owners" })}>
                <Pencil size={12} />
              </button>
            </span>
            {(asset || SERVICE_ENTITY_TYPES.includes(current.entity_type)) && (
              <span>
                <TierBadge tier={current.tier} />
                <button type="button" className="md-edit" aria-label="编辑分级" onClick={() => setDialog({ kind: "tier" })}>
                  <Pencil size={12} />
                </button>
              </span>
            )}
            {typeof current.table_type === "string" && <span>类型：{current.table_type}</span>}
            {typeof current.service_type === "string" && current.service_type && <span>服务类型：{current.service_type}</span>}
            {asset && current.usage && (
              <span>
                使用率：近 30 天查询 {current.usage.queries} 次 · 浏览 {current.usage.views} 次
              </span>
            )}
          </div>
        </div>
        <div className="md-entity-actions">
          <button type="button" className="small-button" title="关注" onClick={() => void run(() => mdPost<Entity>("/entities/follow", { entity_type: current.entity_type, ref: current.fqn, follow: !following }))}>
            <Star size={13} fill={following ? "currentColor" : "none"} />
            {current.followers_count ?? 0}
          </button>
          <button type="button" className="small-button" title="版本历史" onClick={() => setDialog({ kind: "versions" })}>
            <History size={13} />
            {current.version}
          </button>
          <CopyButton text={`${window.location.origin}/#${routeHash("entity", current.entity_type, current.fqn)}`} label="链接" />
          {current.deleted ? (
            <button type="button" className="small-button" onClick={() => void run(() => mdPost<Entity>("/entities/restore", { entity_type: current.entity_type, ref: current.fqn }))}>
              <RotateCcw size={13} />
              恢复
            </button>
          ) : (
            <>
              <button type="button" className="small-button" onClick={() => void rename()}>
                <Share2 size={13} />
                重命名
              </button>
              <button type="button" className="small-button" onClick={() => void remove(false)}>
                <Trash2 size={13} />
                删除
              </button>
            </>
          )}
          <button type="button" className="small-button danger-text" onClick={() => void remove(true)}>
            永久删除
          </button>
        </div>
      </div>
      {error && <ErrorBanner message={error} />}
      <nav className="quality-tabs md-tabs" role="tablist" aria-label="资产详情">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={`quality-tab ${tab === item.id ? "active" : ""}`}
            onClick={() => openEntity(current.entity_type, current.fqn, item.id)}
          >
            {item.label}
            {item.id === "schema" && <small className="md-count">{current.columns?.length ?? 0}</small>}
          </button>
        ))}
      </nav>
      <div className="md-entity-body">
        <div className="md-entity-main">
          {!["lineage", "context"].includes(tab ?? "") && (
            <section className="md-description">
              <div className="md-description-head">
                <span>描述</span>
                <button type="button" className="md-edit" aria-label="编辑描述" onClick={() => setDialog({ kind: "description" })}>
                  <Pencil size={12} />
                </button>
                {(asset || current.entity_type === "databaseSchema") && (
                  <button type="button" className="text-button" onClick={() => setDialog({ kind: "suggest" })}>
                    <Sparkles size={12} />
                    AI 生成描述
                  </button>
                )}
              </div>
              <Description text={current.description} />
            </section>
          )}
          {tab === "schema" && (
            <SchemaTab
              entity={current}
              onDescription={(column) => setDialog({ kind: "column", column })}
              onTags={(column, source) => setDialog({ kind: "columnTags", column, source })}
            />
          )}
          {tab?.startsWith("children:") && <ChildrenTab key={tab} parent={current} childType={tab.slice("children:".length)} />}
          {tab === "details" && <DetailsTab entity={current} />}
          {tab === "activity" && <ActivityTab entity={current} />}
          {tab === "sample" && <SampleTab entity={current} goTo={props.goTo} />}
          {tab === "queries" && <QueriesTab entity={current} goTo={props.goTo} />}
          {tab === "lineage" && (
            <section className="panel">
              <LineageGraphView entityType={current.entity_type} fqn={current.fqn} serviceFqn={current.service_fqn} editable={!current.deleted} />
            </section>
          )}
          {tab === "usage" && <UsageTab entity={current} />}
          {tab === "owned" && <OwnedTab entity={current} />}
          {tab === "assets" && <AssetsTab entity={current} />}
          {tab === "context" && <ContextTab entity={current} />}
          {tab === "properties" && <PropertiesTab entity={current} onSave={(extension) => update({ extension })} />}
        </div>
        <aside className="md-entity-side">
          {asset && (
            <SideBlock title="数据产品" onAdd={() => setDialog({ kind: "products" })}>
              {(current.data_products ?? []).length ? (
                <span className="md-tag-list">
                  {(current.data_products ?? []).map((product) => (
                    <EntityLink key={product.id} entity={product} />
                  ))}
                </span>
              ) : (
                <span className="md-muted-inline">--</span>
              )}
            </SideBlock>
          )}
          <SideBlock title="标签" onAdd={() => setDialog({ kind: "tags" })}>
            <TagList tags={current.tags} empty="没有标签" />
          </SideBlock>
          <SideBlock title="术语" onAdd={() => setDialog({ kind: "terms" })}>
            <TagList tags={current.glossary_terms} empty="没有术语" />
          </SideBlock>
          <SideBlock title="更新">
            <span className="md-muted-inline">
              {current.updated_by} · {timeAgo(current.updated_at)}
            </span>
          </SideBlock>
        </aside>
      </div>

      {dialog?.kind === "description" && (
        <TextDialog title={`编辑 ${entityName(current)} 的描述`} initial={current.description} onClose={() => setDialog(null)} onSave={(description) => update({ description })} />
      )}
      {dialog?.kind === "column" && (
        <TextDialog
          title={`编辑字段 ${dialog.column.name} 的描述`}
          initial={dialog.column.description}
          onClose={() => setDialog(null)}
          onSave={(description) => update({ columns: [{ name: dialog.column.name, description }] })}
        />
      )}
      {dialog?.kind === "owners" && (
        <PeoplePickerModal title="编辑所有者" initial={current.owners.map((owner) => owner.fqn)} onClose={() => setDialog(null)} onSave={(owners) => update({ owners })} />
      )}
      {dialog?.kind === "tags" && (
        <TagPickerModal
          title="编辑标签"
          source="classification"
          initial={current.tags.map((tag) => tag.tag_fqn)}
          onClose={() => setDialog(null)}
          onSave={(fqns) => run(() => setTags(current.fqn, [...fqns.map((tag_fqn) => ({ tag_fqn, source: "classification" })), ...termsOf(current.glossary_terms)]))}
        />
      )}
      {dialog?.kind === "terms" && (
        <TagPickerModal
          title="编辑术语"
          source="glossary"
          initial={current.glossary_terms.map((tag) => tag.tag_fqn)}
          onClose={() => setDialog(null)}
          onSave={(fqns) => run(() => setTags(current.fqn, [...classificationOf(current.tags), ...fqns.map((tag_fqn) => ({ tag_fqn, source: "glossary" }))]))}
        />
      )}
      {dialog?.kind === "columnTags" && (
        <TagPickerModal
          title={`字段 ${dialog.column.name} 的${dialog.source === "glossary" ? "术语" : "标签"}`}
          source={dialog.source}
          initial={(dialog.source === "glossary" ? dialog.column.glossary_terms : dialog.column.tags).map((tag) => tag.tag_fqn)}
          onClose={() => setDialog(null)}
          onSave={(fqns) => {
            const chosen = fqns.map((tag_fqn) => ({ tag_fqn, source: dialog.source }));
            const kept = dialog.source === "glossary" ? classificationOf(dialog.column.tags) : termsOf(dialog.column.glossary_terms);
            return run(() => setTags(dialog.column.fqn, [...kept, ...chosen]));
          }}
        />
      )}
      {dialog?.kind === "tier" && <TierDialog current={current.tier} onClose={() => setDialog(null)} onSave={(tier) => update({ tier })} />}
      {dialog?.kind === "domain" && <DomainDialog entity={current} onClose={() => setDialog(null)} onSave={(patch) => update(patch)} />}
      {dialog?.kind === "products" && <DomainDialog entity={current} products onClose={() => setDialog(null)} onSave={(patch) => update(patch)} />}
      {dialog?.kind === "versions" && <VersionsDialog entity={current} onClose={() => setDialog(null)} />}
      {dialog?.kind === "suggest" && <SuggestDialog entity={current} onClose={() => setDialog(null)} onApply={(patch) => update(patch)} />}
    </div>
  );
}

function SideBlock({ title, onAdd, children }: { title: string; onAdd?: () => void; children: React.ReactNode }) {
  return (
    <section className="md-side-block">
      <h3>{title}</h3>
      {children}
      {onAdd && (
        <button type="button" className="md-add" onClick={onAdd}>
          <Plus size={12} />
          添加
        </button>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ dialogs */

function TextDialog({ title, initial, onClose, onSave }: { title: string; initial: string; onClose: () => void; onSave: (text: string) => Promise<void> }) {
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      title={title}
      width={760}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              onSave(text).then(onClose, () => setBusy(false));
            }}
          >
            保存
          </button>
        </>
      }
    >
      <MarkdownEditor value={text} onChange={setText} rows={10} />
    </Modal>
  );
}

function TierDialog({ current, onClose, onSave }: { current: string | null; onClose: () => void; onSave: (tier: string | null) => Promise<void> }) {
  const [tier, setTier] = useState(current ?? "");
  const descriptions: Record<string, string> = {
    Tier1: "关键业务资产：影响收入、合规或核心决策",
    Tier2: "重要资产：被多个团队或产品依赖",
    Tier3: "部门级资产：在单个团队内使用",
    Tier4: "个人或临时资产",
    Tier5: "可废弃的资产",
  };
  return (
    <Modal
      title="编辑分级"
      width={480}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" onClick={() => onSave(tier || null).then(onClose, () => undefined)}>
            保存
          </button>
        </>
      }
    >
      {["", "Tier.Tier1", "Tier.Tier2", "Tier.Tier3", "Tier.Tier4", "Tier.Tier5"].map((value) => (
        <label key={value || "none"} className="md-picker-item">
          <input type="radio" name="tier" checked={tier === value} onChange={() => setTier(value)} />
          <span>
            <strong>{value ? value.split(".")[1] : "无分级"}</strong>
            <small>{value ? descriptions[value.split(".")[1] ?? ""] : "清除当前分级"}</small>
          </span>
        </label>
      ))}
    </Modal>
  );
}

function DomainDialog({
  entity,
  products = false,
  onClose,
  onSave,
}: {
  entity: Entity;
  products?: boolean;
  onClose: () => void;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
}) {
  const domains = useApi<{ domains: Entity[]; data_products: Entity[] }>(metadataPath("/domains"));
  const [domain, setDomain] = useState(entity.domain?.fqn ?? "");
  const [chosen, setChosen] = useState<string[]>((entity.data_products ?? []).map((item) => item.fqn));
  const options = products ? domains.data?.data_products ?? [] : domains.data?.domains ?? [];
  return (
    <Modal
      title={products ? "编辑数据产品" : "编辑元数据工作区"}
      width={520}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => onSave(products ? { data_products: chosen } : { domain: domain || null }).then(onClose, () => undefined)}
          >
            保存
          </button>
        </>
      }
    >
      {domains.loading && <Loading />}
      {domains.error && <ErrorBanner message={domains.error} />}
      {!products && (
        <label className="md-picker-item">
          <input type="radio" name="domain" checked={!domain} onChange={() => setDomain("")} />
          <span>
            <strong>不属于任何工作区</strong>
          </span>
        </label>
      )}
      {options.map((item) => (
        <label key={item.id} className="md-picker-item">
          <input
            type={products ? "checkbox" : "radio"}
            name="domain"
            checked={products ? chosen.includes(item.fqn) : domain === item.fqn}
            onChange={() =>
              products
                ? setChosen((current) => (current.includes(item.fqn) ? current.filter((value) => value !== item.fqn) : [...current, item.fqn]))
                : setDomain(item.fqn)
            }
          />
          <span>
            <strong>{entityName(item)}</strong>
            <small>{item.fqn}</small>
          </span>
        </label>
      ))}
      {domains.data && !options.length && (
        <p className="muted">
          还没有{products ? "数据产品" : "元数据工作区"}，请先在「元数据工作区」中创建。
        </p>
      )}
    </Modal>
  );
}

function VersionsDialog({ entity, onClose }: { entity: Entity; onClose: () => void }) {
  const versions = useApi<{ versions: VersionItem[] }>(metadataPath("/entity/versions", { entity_type: entity.entity_type, ref: entity.fqn }));
  return (
    <Modal title={`${entityName(entity)} 的版本历史`} width={720} onClose={onClose}>
      {versions.loading && <Loading />}
      {versions.error && <ErrorBanner message={versions.error} />}
      <ol className="md-versions">
        {(versions.data?.versions ?? []).map((item) => (
          <li key={item.version}>
            <div>
              <strong>v{item.version}</strong>
              <span>{item.summary}</span>
            </div>
            <small>
              {item.updated_by} · {formatTime(item.updated_at)}
            </small>
            {[...(item.change.fields_added ?? []), ...(item.change.fields_updated ?? []), ...(item.change.fields_deleted ?? [])].length > 0 && (
              <details>
                <summary>变更明细</summary>
                <pre>{JSON.stringify(item.change, null, 2)}</pre>
              </details>
            )}
          </li>
        ))}
      </ol>
    </Modal>
  );
}

function SuggestDialog({ entity, onClose, onApply }: { entity: Entity; onClose: () => void; onApply: (patch: Record<string, unknown>) => Promise<void> }) {
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [error, setError] = useState("");
  const [useDescription, setUseDescription] = useState(true);
  const [columns, setColumns] = useState<string[]>([]);
  useEffect(() => {
    mdPost<Suggestion>("/context/suggest", { entity_type: entity.entity_type, ref: entity.fqn })
      .then((data) => {
        setSuggestion(data);
        setColumns(data.columns.map((column) => column.name));
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [entity.entity_type, entity.fqn]);
  function apply() {
    if (!suggestion) return;
    const patch: Record<string, unknown> = {};
    if (useDescription && suggestion.description) patch.description = suggestion.description;
    const chosen = suggestion.columns.filter((column) => columns.includes(column.name));
    if (chosen.length) patch.columns = chosen.map((column) => ({ name: column.name, description: column.description }));
    if (!Object.keys(patch).length) return onClose();
    void onApply(patch).then(onClose, () => undefined);
  }
  return (
    <Modal
      title="AI 生成描述"
      width={760}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={!suggestion} onClick={apply}>
            应用所选
          </button>
        </>
      }
    >
      {!suggestion && !error && <Loading text="模型正在根据元数据撰写描述…" />}
      {error && <ErrorBanner message={error} />}
      {suggestion && (
        <>
          <p className="muted">
            <Bot size={13} /> 由 {suggestion.model || suggestion.provider} 根据目录中的名称、字段、术语与血缘生成，请核对后再应用。
          </p>
          <label className="md-suggest">
            <input type="checkbox" checked={useDescription} onChange={(event) => setUseDescription(event.target.checked)} />
            <span>
              <strong>资产描述</strong>
              <MarkdownView text={suggestion.description || "（模型未给出描述）"} />
              {suggestion.current_description && <small>当前：{suggestion.current_description}</small>}
            </span>
          </label>
          {suggestion.columns.map((column) => (
            <label key={column.name} className="md-suggest">
              <input
                type="checkbox"
                checked={columns.includes(column.name)}
                onChange={() => setColumns((current) => (current.includes(column.name) ? current.filter((name) => name !== column.name) : [...current, column.name]))}
              />
              <span>
                <strong>{column.name}</strong>
                {column.description}
                {column.current && <small>当前：{column.current}</small>}
              </span>
            </label>
          ))}
        </>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ tabs */

function SchemaTab({ entity, onDescription, onTags }: { entity: Entity; onDescription: (column: Column) => void; onTags: (column: Column, source: "classification" | "glossary") => void }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const columns = (entity.columns ?? []).filter((column) => !q || column.name.toLowerCase().includes(q) || column.description.toLowerCase().includes(q));
  return (
    <section className="panel">
      <div className="panel-body md-schema-tools">
        <SearchBox value={query} onChange={setQuery} placeholder="在数据表中查找" />
      </div>
      {columns.length ? (
        <div className="table-scroll">
          <table className="md-schema">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>描述</th>
                <th>标签</th>
                <th>术语</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((column) => (
                <tr key={column.name}>
                  <td>
                    <span className="md-column-name">
                      {column.constraint === "PRIMARY_KEY" && <Key size={13} aria-label="主键" />}
                      {column.name}
                    </span>
                    {column.display_name && <small>{column.display_name}</small>}
                  </td>
                  <td>
                    <code>{column.data_type_display || column.data_type}</code>
                    {column.constraint === "NOT_NULL" && <small>NOT NULL</small>}
                  </td>
                  <td className="md-editable">
                    <Description text={column.description} empty="—" />
                    <button type="button" className="md-edit" aria-label={`编辑 ${column.name} 的描述`} onClick={() => onDescription(column)}>
                      <Pencil size={12} />
                    </button>
                  </td>
                  <td>
                    <span className="md-tag-list">
                      {column.tags.map((tag) => (
                        <TagChip key={tag.tag_fqn} tag={tag} />
                      ))}
                    </span>
                    <button type="button" className="md-add" onClick={() => onTags(column, "classification")}>
                      <Plus size={11} />
                      添加
                    </button>
                  </td>
                  <td>
                    <span className="md-tag-list">
                      {column.glossary_terms.map((tag) => (
                        <TagChip key={tag.tag_fqn} tag={tag} />
                      ))}
                    </span>
                    <button type="button" className="md-add" onClick={() => onTags(column, "glossary")}>
                      <Plus size={11} />
                      添加
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title="没有字段" />
      )}
    </section>
  );
}

function ChildrenTab({ parent, childType }: { parent: Entity; childType: string }) {
  const [page, setPage] = useState(1);
  const [deleted, setDeleted] = useState(false);
  const size = 15;
  const listing = useApi<Paged<Entity>>(metadataPath("/entities", { entity_type: childType, parent_fqn: parent.fqn, deleted, page, size }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>
          {typeLabel(childType)} {listing.data && <small className="md-count">{listing.data.total}</small>}
        </h2>
        <Toggle checked={deleted} onChange={(value) => { setDeleted(value); setPage(1); }} label="已删除" />
      </div>
      {listing.loading && <Loading />}
      {listing.error && <ErrorBanner message={listing.error} />}
      {listing.data && (listing.data.items.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>描述</th>
                <th>所有者</th>
                <th>标签</th>
              </tr>
            </thead>
            <tbody>
              {listing.data.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <EntityLink entity={item} />
                  </td>
                  <td className="md-cell-desc">{item.description ? <Description text={item.description} /> : "无描述"}</td>
                  <td>
                    <Owners owners={item.owners} empty="—" />
                  </td>
                  <td>
                    <TagList tags={[...item.tags, ...item.glossary_terms]} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title={`没有${typeLabel(childType)}`} />
      ))}
      {listing.data && <Pager total={listing.data.total} page={page} size={size} onPage={setPage} />}
    </section>
  );
}

function renderValue(key: string, value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" && ["view_definition", "code", "sql", "schema_text", "yaml", "schema_definition"].includes(key))
    return <pre className="sql-code">{value}</pre>;
  if (Array.isArray(value) && value.length && value.every((item) => item && typeof item === "object")) {
    const rows = value as Record<string, unknown>[];
    const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].slice(0, 8);
    return (
      <div className="table-scroll">
        <table>
          <thead>
            <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
          </thead>
          <tbody>
            {rows.slice(0, 200).map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column}>{typeof row[column] === "object" ? JSON.stringify(row[column]) : String(row[column] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (Array.isArray(value)) return value.map(String).join("、") || "—";
  if (typeof value === "object") return <pre className="md-json">{JSON.stringify(value, null, 2)}</pre>;
  return String(value);
}

function DetailsTab({ entity }: { entity: Entity }) {
  const entries = Object.entries(entity).filter(([key, value]) => !BASE_KEYS.has(key) && value !== "" && !(Array.isArray(value) && !value.length));
  const isService = SERVICE_ENTITY_TYPES.includes(entity.entity_type);
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>详情</h2>
        <span>
          创建于 {formatTime(entity.created_at)} · 版本 {entity.version}
        </span>
      </div>
      <div className="panel-body">
        {isService && <IngestionCard entity={entity} />}
        <dl className="md-details">
          <div>
            <dt>完整名称</dt>
            <dd>
              <code>{entity.fqn}</code>
            </dd>
          </div>
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{renderValue(key, value)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

function IngestionCard({ entity }: { entity: Entity }) {
  const services = useApi<{ items: IngestionService[] }>(metadataPath("/ingestion/services"));
  const [message, setMessage] = useState("");
  const service = services.data?.items.find((item) => item.fqn === entity.fqn);
  if (!service) return services.loading ? <Loading /> : null;
  async function runNow() {
    setMessage("");
    try {
      await mdPost("/ingestion/run", { service_fqn: entity.fqn });
      setMessage("已开始拾取，完成后刷新即可看到结果。");
      window.setTimeout(services.reload, 1500);
    } catch (e) {
      setMessage(errorMessage(e));
    }
  }
  return (
    <div className="md-ingestion-card">
      <div>
        <strong>元数据拾取</strong>
        <span>
          {service.automated ? `${service.ingestion.enabled && service.ingestion.interval_minutes ? `每 ${service.ingestion.interval_minutes} 分钟` : "仅手动"} · 上次 ` : "该连接器需要手工登记或导入资产"}
          {service.automated && (service.last_run ? <RunStatus status={service.last_run.status} /> : "未运行")}
          {service.last_run && ` ${timeAgo(service.last_run.finished_at ?? service.last_run.started_at)} · ${service.last_run.message}`}
        </span>
      </div>
      <div className="md-actions">
        {service.automated && (
          <button type="button" className="primary-button" disabled={service.running} onClick={() => void runNow()}>
            {service.running ? "拾取中…" : "立即拾取"}
          </button>
        )}
        <button type="button" className="text-button" onClick={() => navigate("settings", "services")}>
          拾取设置 <ArrowRight size={12} />
        </button>
      </div>
      {message && <p className="muted">{message}</p>}
    </div>
  );
}

function ActivityTab({ entity }: { entity: Entity }) {
  const feed = useApi<Paged<FeedEvent>>(metadataPath("/feed", { entity_fqn: entity.fqn, include_children: true, size: 50 }));
  const threads = useApi<Paged<Thread>>(metadataPath("/threads", { about_fqn: entity.fqn, include_children: true, size: 50 }));
  const [kind, setKind] = useState("Conversation");
  const [taskType, setTaskType] = useState("RequestDescription");
  const [message, setMessage] = useState("");
  const [suggestion, setSuggestion] = useState("");
  const [error, setError] = useState("");
  async function post() {
    setError("");
    try {
      await mdPost("/threads", {
        thread_type: kind,
        about_fqn: entity.fqn,
        about_type: entity.entity_type,
        message,
        ...(kind === "Task" ? { task_type: taskType, suggestion: taskType.endsWith("Tag") ? suggestion.split(/[,，\s]+/).filter(Boolean) : suggestion, assignees: entity.owners.map((owner) => owner.fqn) } : {}),
        ...(kind === "Announcement" ? { title: message.slice(0, 60) } : {}),
      });
      setMessage("");
      setSuggestion("");
      threads.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  async function act(path: string, body: unknown = {}) {
    setError("");
    try {
      await mdPost(path, body);
      threads.reload();
      feed.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <div className="md-activity">
      <section className="panel">
        <div className="panel-body md-composer">
          <div className="segmented-control">
            {[["Conversation", "对话"], ["Task", "任务"], ["Announcement", "公告"]].map(([value, label]) => (
              <button key={value} type="button" className={kind === value ? "selected" : ""} onClick={() => setKind(value)}>
                {label}
              </button>
            ))}
          </div>
          {kind === "Task" && (
            <div className="md-form-inline">
              <select value={taskType} onChange={(event) => setTaskType(event.target.value)}>
                <option value="RequestDescription">请求补充描述</option>
                <option value="UpdateDescription">建议更新描述</option>
                <option value="RequestTag">请求添加标签</option>
                <option value="UpdateTag">建议更新标签</option>
              </select>
              <input value={suggestion} onChange={(event) => setSuggestion(event.target.value)} placeholder={taskType.endsWith("Tag") ? "建议的标签 FQN，逗号分隔" : "建议的描述（处理人接受后生效）"} />
            </div>
          )}
          <textarea rows={3} value={message} onChange={(event) => setMessage(event.target.value)} placeholder={kind === "Announcement" ? "公告内容" : "输入内容，所有者会在活动信息流中看到"} />
          <div className="md-form-actions">
            <button type="button" className="primary-button" disabled={kind !== "Task" && !message.trim()} onClick={() => void post()}>
              发布
            </button>
          </div>
          {error && <ErrorBanner message={error} />}
        </div>
      </section>
      {(threads.data?.items ?? []).map((thread) => (
        <ThreadCard key={thread.id} thread={thread} onAct={act} />
      ))}
      <section className="panel">
        <div className="panel-heading">
          <h2>变更记录</h2>
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
                  {event.type_label} {event.entity_fqn !== entity.fqn ? <EntityLink entity={{ entity_type: event.entity_type, fqn: event.entity_fqn }}>{event.entity_fqn}</EntityLink> : ""} · {formatTime(event.ts)}
                  {event.current_version !== null && event.previous_version !== event.current_version ? ` · v${event.previous_version ?? "—"} → v${event.current_version}` : ""}
                </small>
              </div>
            </li>
          ))}
        </ol>
        {feed.data && !feed.data.items.length && <Empty title="暂无活动" />}
      </section>
    </div>
  );
}

function ThreadCard({ thread, onAct }: { thread: Thread; onAct: (path: string, body?: unknown) => Promise<void> }) {
  const [reply, setReply] = useState("");
  const labels: Record<string, string> = { Conversation: "对话", Task: "任务", Announcement: "公告" };
  const open = thread.task?.status === "Open";
  return (
    <section className={`panel md-thread ${thread.resolved ? "resolved" : ""}`}>
      <div className="panel-body">
        <div className="md-thread-head">
          <span className="source-badge">{labels[thread.thread_type]}</span>
          {thread.task && <span className={open ? "quality-warn-badge" : "healthy-badge"}>{thread.task.task_type} · {thread.task.status}</span>}
          <strong>{thread.created_by}</strong>
          <small>{timeAgo(thread.created_at)}</small>
          <button type="button" className="text-button danger-text" onClick={() => void onAct(`/threads/${thread.id}/delete`)}>
            删除
          </button>
        </div>
        {thread.message && <MarkdownView text={thread.message} />}
        {thread.task?.suggestion !== undefined && thread.task?.suggestion !== null && thread.task.suggestion !== "" && (
          <p className="md-suggestion">建议：{Array.isArray(thread.task.suggestion) ? thread.task.suggestion.join("、") : String(thread.task.suggestion)}</p>
        )}
        {thread.posts.map((item) => (
          <div key={item.id} className="md-post">
            <strong>{item.from}</strong>
            <span>{item.message}</span>
            <small>{timeAgo(item.ts)}</small>
          </div>
        ))}
        <div className="md-reply">
          <input value={reply} onChange={(event) => setReply(event.target.value)} placeholder="回复…" />
          <button type="button" className="small-button" disabled={!reply.trim()} onClick={() => void onAct(`/threads/${thread.id}/posts`, { message: reply }).then(() => setReply(""))}>
            回复
          </button>
          {open && (
            <>
              <button type="button" className="small-button" onClick={() => void onAct(`/threads/${thread.id}/resolve`, { accept: true })}>
                接受并应用
              </button>
              <button type="button" className="small-button" onClick={() => void onAct(`/threads/${thread.id}/resolve`, { accept: false })}>
                拒绝
              </button>
            </>
          )}
          {!thread.resolved && !thread.task && (
            <button type="button" className="small-button" onClick={() => void onAct(`/threads/${thread.id}/close`)}>
              关闭
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

function SampleTab({ entity, goTo }: { entity: Entity; goTo: MetadataViewProps["goTo"] }) {
  const sample = useApi<{ columns: string[]; rows: unknown[][]; sql: string; datasource_id: string }>(metadataPath("/entity/sample", { ref: entity.fqn, limit: 100 }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>样本数据</h2>
        {sample.data && (
          <button type="button" className="text-button" onClick={() => goTo("sql", `${sample.data?.sql};`, sample.data?.datasource_id)}>
            在 SQL 工作台查询 <ArrowRight size={12} />
          </button>
        )}
      </div>
      {sample.loading && <Loading text="正在从数据源读取样本…" />}
      {sample.error && <ErrorBanner message={sample.error} />}
      {sample.data && <DataGrid columns={sample.data.columns} rows={sample.data.rows} />}
    </section>
  );
}

function QueriesTab({ entity, goTo }: { entity: Entity; goTo: MetadataViewProps["goTo"] }) {
  const queries = useApi<{ items: { query_id: string; sql: string; datasource_id: string; ts: number }[] }>(metadataPath("/entity/queries", { entity_type: entity.entity_type, ref: entity.fqn }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>查询</h2>
        <span>智能问数、SQL 工作台与 MCP 在该表上执行过的查询</span>
      </div>
      {queries.loading && <Loading />}
      {queries.error && <ErrorBanner message={queries.error} />}
      <div className="panel-body md-queries">
        {(queries.data?.items ?? []).map((query) => (
          <div key={query.query_id} className="md-query">
            <pre className="sql-code">{query.sql}</pre>
            <div>
              <small>
                {query.datasource_id} · {formatTime(query.ts)}
              </small>
              <button type="button" className="text-button" onClick={() => goTo("sql", query.sql, query.datasource_id)}>
                在 SQL 工作台打开 <ArrowRight size={12} />
              </button>
            </div>
          </div>
        ))}
        {queries.data && !queries.data.items.length && <Empty title="暂无查询记录">在智能问数或 SQL 工作台查询该表后，这里会显示查询语句。</Empty>}
      </div>
    </section>
  );
}

function UsageTab({ entity }: { entity: Entity }) {
  const usage = useApi<{ items: { target_fqn: string; entity: Entity | null; column?: string; label_type: string }[] }>(metadataPath("/tags/usage", { tag: entity.fqn }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>使用情况</h2>
        <span>{usage.data?.items.length ?? 0} 处</span>
      </div>
      {usage.error && <ErrorBanner message={usage.error} />}
      <ul className="md-impact-list panel-body">
        {(usage.data?.items ?? []).map((item) => (
          <li key={item.target_fqn}>
            {item.entity ? <EntityLink entity={item.entity}>{item.target_fqn}</EntityLink> : item.target_fqn}
            {item.column && <span className="neutral-badge">字段 {item.column}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function OwnedTab({ entity }: { entity: Entity }) {
  const owned = useApi<{ owns: Entity[]; follows: Entity[] }>(metadataPath("/entity/owned", { entity_type: entity.entity_type, ref: entity.fqn }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>拥有的资产</h2>
      </div>
      {owned.error && <ErrorBanner message={owned.error} />}
      <ul className="md-impact-list panel-body">
        {(owned.data?.owns ?? []).map((item) => (
          <li key={item.id}>
            <EntityIcon type={item.entity_type} size={14} />
            <EntityLink entity={item}>{item.fqn}</EntityLink>
          </li>
        ))}
        {owned.data && !owned.data.owns.length && <li className="muted">暂无</li>}
      </ul>
      {(owned.data?.follows ?? []).length > 0 && (
        <>
          <div className="panel-heading">
            <h2>关注的资产</h2>
          </div>
          <ul className="md-impact-list panel-body">
            {(owned.data?.follows ?? []).map((item) => (
              <li key={item.id}>
                <EntityLink entity={item}>{item.fqn}</EntityLink>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function AssetsTab({ entity }: { entity: Entity }) {
  const assets = useApi<{ items: Entity[]; total: number }>(metadataPath("/entity/assets", { entity_type: entity.entity_type, ref: entity.fqn }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>资产</h2>
        <span>{assets.data?.total ?? 0} 个</span>
      </div>
      {assets.error && <ErrorBanner message={assets.error} />}
      <ul className="md-impact-list panel-body">
        {(assets.data?.items ?? []).map((item) => (
          <li key={item.id}>
            <EntityIcon type={item.entity_type} size={14} />
            <EntityLink entity={item}>{item.fqn}</EntityLink>
            <span className="md-type-pill">{item.type_label}</span>
          </li>
        ))}
        {assets.data && !assets.data.items.length && <li className="muted">还没有资产。可在资产详情页设置元数据工作区或数据产品。</li>}
      </ul>
    </section>
  );
}

function ContextTab({ entity }: { entity: Entity }) {
  const card = useApi<ContextCard>(metadataPath("/entity/context", { entity_type: entity.entity_type, ref: entity.fqn }));
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>AI 上下文</h2>
        <div className="md-actions">
          {card.data && <CopyButton text={card.data.markdown} label="复制 Markdown" />}
          <button type="button" className="text-button" onClick={() => navigate("context")}>
            MCP 与助手 <ArrowRight size={12} />
          </button>
        </div>
      </div>
      <div className="panel-body">
        <p className="muted">
          这是 AI 助手、智能问数与 MCP 代理读取该资产时得到的上下文：描述、责任人、术语定义、字段、血缘与近期查询。
        </p>
        {card.loading && <Loading />}
        {card.error && <ErrorBanner message={card.error} />}
        {card.data && <MarkdownView className="md-context-card" text={card.data.markdown} />}
      </div>
    </section>
  );
}

interface PropertyDefinition {
  name: string;
  display_name: string;
  property_type: string;
  description: string;
  config: { values?: string[]; multi_select?: boolean };
}

function PropertiesTab({ entity, onSave }: { entity: Entity; onSave: (extension: Record<string, unknown>) => Promise<void> }) {
  const definitions = useApi<{ items: PropertyDefinition[] }>(metadataPath("/properties", { entity_type: entity.entity_type }));
  const [values, setValues] = useState<Record<string, unknown>>(entity.extension ?? {});
  const [saved, setSaved] = useState(false);
  const items = definitions.data?.items ?? [];
  function set(name: string, value: unknown) {
    setSaved(false);
    setValues((current) => ({ ...current, [name]: value }));
  }
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>自定义属性</h2>
        <button type="button" className="text-button" onClick={() => navigate("properties")}>
          管理属性定义 <ArrowRight size={12} />
        </button>
      </div>
      {definitions.error && <ErrorBanner message={definitions.error} />}
      <div className="panel-body md-properties">
        {items.map((definition) => {
          const value = values[definition.name];
          return (
            <label key={definition.name} className="md-form-row">
              <span>
                {definition.display_name || definition.name} <small>{definition.property_type}</small>
              </span>
              {definition.property_type === "boolean" ? (
                <input type="checkbox" checked={value === true} onChange={(event) => set(definition.name, event.target.checked)} />
              ) : definition.property_type === "enum" ? (
                <select
                  multiple={definition.config.multi_select}
                  value={definition.config.multi_select ? ((value as string[] | undefined) ?? []) : String(value ?? "")}
                  onChange={(event) =>
                    set(definition.name, definition.config.multi_select ? [...event.target.selectedOptions].map((option) => option.value) : event.target.value)
                  }
                >
                  {!definition.config.multi_select && <option value="">未设置</option>}
                  {(definition.config.values ?? []).map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : definition.property_type === "markdown" ? (
                <textarea rows={3} value={String(value ?? "")} onChange={(event) => set(definition.name, event.target.value)} />
              ) : (
                <input
                  type={definition.property_type === "date" ? "date" : ["integer", "number"].includes(definition.property_type) ? "number" : "text"}
                  value={typeof value === "object" && value ? String((value as { fqn?: string }).fqn ?? "") : String(value ?? "")}
                  placeholder={definition.property_type === "entityReference" ? "资产的完整名称" : definition.description}
                  onChange={(event) => set(definition.name, event.target.value)}
                />
              )}
              {definition.description && <small>{definition.description}</small>}
            </label>
          );
        })}
        {definitions.data && !items.length && <Empty title="该类型还没有自定义属性">在「元数据系统 → 自定义属性」中为{typeLabel(entity.entity_type)}定义属性。</Empty>}
        {items.length > 0 && (
          <div className="md-form-actions">
            {saved && <span className="healthy-badge">已保存</span>}
            <button type="button" className="primary-button" onClick={() => void onSave(values).then(() => setSaved(true), () => undefined)}>
              保存属性
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
