// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据系统 and 元数据工作区: glossaries and terms, classifications and tags, domains and data
 * products, and custom-property definitions. The layouts follow the product screens: a list on the
 * left, the selected object on the right, and creation through a form page or a dialog.
 */
import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, Info, Pencil, Plus, Trash2 } from "lucide-react";
import { errorMessage } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  TERM_STATUS_LABELS,
  TYPE_LABELS,
  entityName,
  mdPost,
  metadataPath,
  navigate,
  openEntity,
  type Entity,
  type EntityRef,
  type TypesResponse,
} from "./metadataApi";
import {
  ColorField,
  Description,
  EntityIcon,
  EntityLink,
  FormRow,
  MarkdownEditor,
  Modal,
  Owners,
  PeoplePickerModal,
  TagChip,
  TagPickerModal,
  Toggle,
} from "./MetadataWidgets";
import type { MetadataViewProps } from "./MetadataPage";

type TreeNode = Entity & { children: TreeNode[]; usage_count?: number };

/* ------------------------------------------------------------------ shared fields */

export function PeopleField({
  label,
  value,
  onChange,
  allowTeams = true,
}: {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
  allowTeams?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="md-people-field">
      <span>{label}</span>
      <span className="md-owners">
        {value.map((fqn) => (
          <span key={fqn} className="md-owner">
            <span className="md-avatar">{fqn.slice(0, 1).toUpperCase()}</span>
            {fqn}
          </span>
        ))}
      </span>
      <button type="button" className="md-plus" aria-label={`选择${label}`} onClick={() => setOpen(true)}>
        <Plus size={13} />
      </button>
      {open && (
        <PeoplePickerModal
          title={`选择${label}`}
          initial={value}
          allowTeams={allowTeams}
          onClose={() => setOpen(false)}
          onSave={(fqns) => onChange(fqns)}
        />
      )}
    </div>
  );
}

function TagsField({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="md-tags-field">
      {value.map((fqn) => (
        <TagChip key={fqn} tag={{ tag_fqn: fqn, source: "classification" }} onRemove={() => onChange(value.filter((item) => item !== fqn))} />
      ))}
      <button type="button" className="md-add" onClick={() => setOpen(true)}>
        <Plus size={12} />
        选择标签
      </button>
      {open && <TagPickerModal title="选择标签" source="classification" initial={value} onClose={() => setOpen(false)} onSave={onChange} />}
    </div>
  );
}

function SplitList({
  title,
  addLabel,
  onAdd,
  children,
}: {
  title: string;
  addLabel: string;
  onAdd: () => void;
  children: ReactNode;
}) {
  return (
    <aside className="md-split-list">
      <div className="md-split-head">{title}</div>
      <button type="button" className="md-split-add" onClick={onAdd}>
        <Plus size={13} />
        {addLabel}
      </button>
      <div className="md-split-items">{children}</div>
    </aside>
  );
}

function useAction(after: () => void) {
  const [error, setError] = useState("");
  async function run(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
      after();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return { error, run, setError };
}

function flatten(nodes: TreeNode[], depth = 0): { node: TreeNode; depth: number }[] {
  return nodes.flatMap((node) => [{ node, depth }, ...flatten(node.children, depth + 1)]);
}

/* ------------------------------------------------------------------ glossary */

export function GlossaryPage({ route }: MetadataViewProps) {
  const glossaries = useApi<{ items: (Entity & { term_count: number; usage_count: number; reviewers: EntityRef[] })[] }>(metadataPath("/glossaries"));
  if (route.parts[0] === "new") return <GlossaryForm onDone={glossaries.reload} />;
  const items = glossaries.data?.items ?? [];
  const selected = items.find((item) => item.fqn === route.parts[0]) ?? items[0];
  return (
    <div className="page-content md-page">
      <div className="md-split">
        <SplitList title="术语库" addLabel="添加术语库" onAdd={() => navigate("glossary", "new")}>
          {glossaries.loading && <Loading />}
          {glossaries.error && <ErrorBanner message={glossaries.error} />}
          {items.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`md-split-item ${selected?.id === item.id ? "active" : ""}`}
              onClick={() => navigate("glossary", item.fqn)}
            >
              <span>{entityName(item)}</span>
              <small>{item.term_count}</small>
            </button>
          ))}
          {glossaries.data && !items.length && <p className="muted md-split-empty">还没有术语库</p>}
        </SplitList>
        <div className="md-split-main">
          {selected ? (
            <GlossaryDetail key={selected.id} glossary={selected} onChanged={glossaries.reload} />
          ) : (
            glossaries.data && (
              <Empty title="还没有术语库">
                术语库是组织内受控的业务词汇集合，AI 助手与智能问数会引用术语的定义理解业务问题。
              </Empty>
            )
          )}
        </div>
      </div>
    </div>
  );
}

function GlossaryDetail({ glossary, onChanged }: { glossary: Entity & { term_count: number; usage_count: number; reviewers: EntityRef[] }; onChanged: () => void }) {
  const terms = useApi<{ terms: TreeNode[]; tree: TreeNode[] }>(metadataPath("/glossaries/terms", { glossary: glossary.fqn }));
  const [termDialog, setTermDialog] = useState<{ term?: TreeNode; parent?: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const action = useAction(() => {
    terms.reload();
    onChanged();
  });
  const rows = flatten(terms.data?.tree ?? []);
  return (
    <>
      <div className="md-object-head">
        <div>
          <h1>
            <EntityIcon type="glossary" size={20} />
            {entityName(glossary)}
            {glossary.mutually_exclusive === true && <span className="source-badge">互斥的</span>}
          </h1>
          <div className="md-object-meta">
            <Owners owners={glossary.owners} />
            <span>审核者：{glossary.reviewers.length ? glossary.reviewers.map(entityName).join("、") : "无"}</span>
            <span>{glossary.term_count} 个术语 · 被引用 {glossary.usage_count} 次</span>
          </div>
        </div>
        <div className="md-actions">
          <button type="button" className="primary-button" onClick={() => setTermDialog({})}>
            <Plus size={14} />
            添加术语
          </button>
          <button type="button" className="small-button" onClick={() => openEntity("glossary", glossary.fqn)}>
            详情与版本
          </button>
          <button
            type="button"
            className="small-button danger-text"
            onClick={() => window.confirm(`永久删除术语库 ${glossary.name} 及其全部术语？资产上的术语引用会一并移除。`) && void action.run(() => mdPost("/entities/delete", { entity_type: "glossary", ref: glossary.fqn, hard: true }).then(() => navigate("glossary")))}
          >
            <Trash2 size={13} />
            删除
          </button>
        </div>
      </div>
      <section className="md-description">
        <div className="md-description-head">
          <span>描述</span>
          <button type="button" className="md-edit" aria-label="编辑描述" onClick={() => setEditing(true)}>
            <Pencil size={12} />
          </button>
        </div>
        <Description text={glossary.description} />
      </section>
      {action.error && <ErrorBanner message={action.error} />}
      <section className="panel">
        {terms.loading && <Loading />}
        {terms.error && <ErrorBanner message={terms.error} />}
        {terms.data && (rows.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>术语</th>
                  <th>显示名称</th>
                  <th>描述</th>
                  <th>同义词</th>
                  <th>状态</th>
                  <th>使用</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ node, depth }) => (
                  <tr key={node.id}>
                    <td style={{ paddingLeft: 12 + depth * 18 }}>
                      <EntityLink entity={node}>{node.name}</EntityLink>
                    </td>
                    <td>{node.display_name || "--"}</td>
                    <td className="md-cell-desc">{node.description ? <Description text={node.description} /> : "无描述"}</td>
                    <td>{Array.isArray(node.synonyms) && node.synonyms.length ? (node.synonyms as string[]).join("、") : "—"}</td>
                    <td>
                      <span className={node.status === "Approved" ? "healthy-badge" : node.status === "Deprecated" || node.status === "Rejected" ? "failure-badge" : "neutral-badge"}>
                        {TERM_STATUS_LABELS[String(node.status)] ?? String(node.status ?? "")}
                      </span>
                    </td>
                    <td>{node.usage_count ?? 0}</td>
                    <td>
                      <div className="md-actions">
                        <button type="button" className="md-edit" aria-label={`添加 ${node.name} 的子术语`} onClick={() => setTermDialog({ parent: node.fqn })}>
                          <Plus size={13} />
                        </button>
                        <button type="button" className="md-edit" aria-label={`编辑 ${node.name}`} onClick={() => setTermDialog({ term: node })}>
                          <Pencil size={13} />
                        </button>
                        <button
                          type="button"
                          className="md-edit"
                          aria-label={`删除 ${node.name}`}
                          onClick={() => window.confirm(`永久删除术语 ${node.fqn} 及其子术语？`) && void action.run(() => mdPost("/entities/delete", { entity_type: "glossaryTerm", ref: node.fqn, hard: true }))}
                        >
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
          <Empty title="术语库中还没有术语">添加术语并在数据资产或字段上引用它们。</Empty>
        ))}
      </section>
      {termDialog && (
        <TermDialog
          glossary={glossary.fqn}
          term={termDialog.term}
          parent={termDialog.parent}
          options={(terms.data?.terms ?? []).map((term) => term.fqn)}
          onClose={() => setTermDialog(null)}
          onDone={() => {
            setTermDialog(null);
            terms.reload();
            onChanged();
          }}
        />
      )}
      {editing && (
        <DescriptionDialog
          title={`编辑 ${entityName(glossary)} 的描述`}
          initial={glossary.description}
          onClose={() => setEditing(false)}
          onSave={(description) => mdPost("/entities/update", { entity_type: "glossary", ref: glossary.fqn, description }).then(onChanged)}
        />
      )}
    </>
  );
}

function DescriptionDialog({ title, initial, onClose, onSave }: { title: string; initial: string; onClose: () => void; onSave: (text: string) => Promise<unknown> }) {
  const [text, setText] = useState(initial);
  const [error, setError] = useState("");
  return (
    <Modal
      title={title}
      width={720}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" onClick={() => onSave(text).then(onClose, (e: unknown) => setError(errorMessage(e)))}>
            保存
          </button>
        </>
      }
    >
      <MarkdownEditor value={text} onChange={setText} rows={8} />
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

function TermDialog({
  glossary,
  term,
  parent,
  options,
  onClose,
  onDone,
}: {
  glossary: string;
  term?: TreeNode;
  parent?: string;
  options: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(term?.name ?? "");
  const [displayName, setDisplayName] = useState(term?.display_name ?? "");
  const [description, setDescription] = useState(term?.description ?? "");
  const [parentFqn, setParentFqn] = useState(parent ?? term?.parent_fqn ?? glossary);
  const [synonyms, setSynonyms] = useState(Array.isArray(term?.synonyms) ? (term?.synonyms as string[]).join(", ") : "");
  const [related, setRelated] = useState(Array.isArray(term?.related_terms) ? (term?.related_terms as unknown[]).map((item) => (typeof item === "string" ? item : (item as EntityRef).fqn)).join(", ") : "");
  const [references, setReferences] = useState(
    Array.isArray(term?.references) ? (term?.references as { name?: string; endpoint?: string }[]).map((item) => `${item.name ?? ""} | ${item.endpoint ?? ""}`).join("\n") : "",
  );
  const [status, setStatus] = useState(String(term?.status ?? "Draft"));
  const [exclusive, setExclusive] = useState(term?.mutually_exclusive === true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const split = (text: string) => text.split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean);
  async function save() {
    setBusy(true);
    setError("");
    const fields = {
      synonyms: split(synonyms),
      references: references
        .split("\n")
        .map((line) => line.split("|").map((part) => part.trim()))
        .filter(([label, url]) => label || url)
        .map(([label, url]) => ({ name: label ?? "", endpoint: url ?? "" })),
      status,
      mutually_exclusive: exclusive,
    };
    try {
      if (term) {
        await mdPost("/entities/update", { entity_type: "glossaryTerm", ref: term.fqn, display_name: displayName, description, related_terms: split(related), fields });
      } else {
        await mdPost("/entities", { entity_type: "glossaryTerm", name: name.trim(), parent_fqn: parentFqn, display_name: displayName, description, related_terms: split(related), fields });
      }
      onDone();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <Modal
      title={term ? `编辑术语 ${term.fqn}` : "添加术语"}
      width={720}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy || (!term && !name.trim())} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="md-form-inline">
        <FormRow label="名称" htmlFor="term-name" required>
          <input id="term-name" value={name} disabled={!!term} onChange={(event) => setName(event.target.value)} placeholder="例如 GMV" />
        </FormRow>
        <FormRow label="显示名称" htmlFor="term-display">
          <input id="term-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如 成交总额" />
        </FormRow>
      </div>
      <FormRow label="描述" required>
        <MarkdownEditor value={description} onChange={setDescription} rows={5} placeholder="术语的业务定义、计算口径与注意事项" />
      </FormRow>
      {!term && (
        <FormRow label="上级" htmlFor="term-parent">
          <select id="term-parent" value={parentFqn} onChange={(event) => setParentFqn(event.target.value)}>
            <option value={glossary}>术语库根目录</option>
            {options.map((fqn) => (
              <option key={fqn} value={fqn}>
                {fqn}
              </option>
            ))}
          </select>
        </FormRow>
      )}
      <div className="md-form-inline">
        <FormRow label="同义词" htmlFor="term-synonyms" help="逗号分隔">
          <input id="term-synonyms" value={synonyms} onChange={(event) => setSynonyms(event.target.value)} placeholder="成交额, 交易总额" />
        </FormRow>
        <FormRow label="状态" htmlFor="term-status">
          <select id="term-status" value={status} onChange={(event) => setStatus(event.target.value)}>
            {Object.entries(TERM_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </FormRow>
      </div>
      <FormRow label="相关术语" htmlFor="term-related" help="术语的完整名称，逗号分隔">
        <input id="term-related" list="term-options" value={related} onChange={(event) => setRelated(event.target.value)} />
        <datalist id="term-options">
          {options.map((fqn) => (
            <option key={fqn} value={fqn} />
          ))}
        </datalist>
      </FormRow>
      <FormRow label="引用" htmlFor="term-references" help="每行一条：名称 | URL">
        <textarea id="term-references" rows={2} value={references} onChange={(event) => setReferences(event.target.value)} placeholder="指标口径文档 | https://wiki.example.com/gmv" />
      </FormRow>
      <Toggle checked={exclusive} onChange={setExclusive} label="子术语互斥（同一资产只能使用其中一个）" />
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

function GlossaryForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [exclusive, setExclusive] = useState(false);
  const [owners, setOwners] = useState<string[]>([]);
  const [reviewers, setReviewers] = useState<string[]>([]);
  const [domain, setDomain] = useState("");
  const domains = useApi<{ domains: Entity[] }>(metadataPath("/domains"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    setError("");
    try {
      const created = await mdPost<Entity>("/entities", {
        entity_type: "glossary",
        name: name.trim(),
        display_name: displayName,
        description,
        owners,
        reviewers,
        tags,
        ...(domain ? { domain } : {}),
        fields: { mutually_exclusive: exclusive },
      });
      onDone();
      navigate("glossary", created.fqn);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <div className="page-content md-page">
      <div className="md-breadcrumbs">
        <button type="button" className="md-link" onClick={() => navigate("glossary")}>
          术语库
        </button>
        <i>/</i>
        <b>添加术语库</b>
      </div>
      <div className="md-form-layout">
        <div className="md-form">
          <h2>添加术语库</h2>
          <FormRow label="名称" htmlFor="glossary-name" required>
            <input id="glossary-name" value={name} placeholder="名称" onChange={(event) => setName(event.target.value)} />
          </FormRow>
          <FormRow label="显示名称" htmlFor="glossary-display">
            <input id="glossary-display" value={displayName} placeholder="显示名称" onChange={(event) => setDisplayName(event.target.value)} />
          </FormRow>
          <FormRow label="描述" required>
            <MarkdownEditor value={description} onChange={setDescription} placeholder="Write your description" />
          </FormRow>
          <FormRow label="标签">
            <TagsField value={tags} onChange={setTags} />
          </FormRow>
          <Toggle checked={exclusive} onChange={setExclusive} label="互斥的" />
          <PeopleField label="所有者" value={owners} onChange={setOwners} />
          <PeopleField label="审核者" value={reviewers} onChange={setReviewers} allowTeams={false} />
          <FormRow label="元数据工作区" htmlFor="glossary-domain">
            <select id="glossary-domain" value={domain} onChange={(event) => setDomain(event.target.value)}>
              <option value="">不指定</option>
              {(domains.data?.domains ?? []).map((item) => (
                <option key={item.id} value={item.fqn}>
                  {entityName(item)}
                </option>
              ))}
            </select>
          </FormRow>
          {error && <ErrorBanner message={error} />}
          <div className="md-form-actions">
            <button type="button" className="text-button" onClick={() => navigate("glossary")}>
              取消
            </button>
            <button type="button" className="primary-button" disabled={busy || !name.trim() || !description.trim()} onClick={() => void save()}>
              保存
            </button>
          </div>
        </div>
        <aside className="md-help">
          <h3>配置术语库</h3>
          <p>
            术语库是用于定义组织中的概念和术语的受控词汇集合。术语库可以特定于某个域（例如业务术语库、技术术语库）。在术语库中，可以定义标准术语、概念及其同义词和相关术语，还可以控制向术语库中添加术语的人员和方式。
          </p>
          <h4>术语在 AI 上下文中的作用</h4>
          <p>资产或字段上引用的术语会连同定义与同义词一起提供给智能问数、AI 助手和 MCP 代理，让它们按业务口径理解问题。</p>
        </aside>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ classification */

interface ClassificationItem extends Entity {
  tag_count: number;
  usage_count: number;
}

export function ClassificationPage({ route }: MetadataViewProps) {
  const classifications = useApi<{ items: ClassificationItem[] }>(metadataPath("/classifications"));
  const [adding, setAdding] = useState(false);
  const items = classifications.data?.items ?? [];
  const selected = items.find((item) => item.fqn === route.parts[0]) ?? items[0];
  return (
    <div className="page-content md-page">
      <div className="md-split">
        <SplitList title="分类" addLabel="添加分类" onAdd={() => setAdding(true)}>
          {classifications.loading && <Loading />}
          {classifications.error && <ErrorBanner message={classifications.error} />}
          {items.map((item) => (
            <button type="button" key={item.id} className={`md-split-item ${selected?.id === item.id ? "active" : ""}`} onClick={() => navigate("classification", item.fqn)}>
              <span>{entityName(item)}</span>
              <small>{item.tag_count}</small>
            </button>
          ))}
        </SplitList>
        <div className="md-split-main">
          {selected && <ClassificationDetail key={selected.id} classification={selected} onChanged={classifications.reload} />}
        </div>
      </div>
      {adding && (
        <ClassificationDialog
          onClose={() => setAdding(false)}
          onDone={(fqn) => {
            setAdding(false);
            classifications.reload();
            navigate("classification", fqn);
          }}
        />
      )}
    </div>
  );
}

function ClassificationDetail({ classification, onChanged }: { classification: ClassificationItem; onChanged: () => void }) {
  const tags = useApi<{ tags: TreeNode[]; tree: TreeNode[] }>(metadataPath("/classifications/tags", { classification: classification.fqn }));
  const [dialog, setDialog] = useState<{ tag?: TreeNode; parent?: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const action = useAction(() => {
    tags.reload();
    onChanged();
  });
  const system = classification.provider === "system";
  const rows = flatten(tags.data?.tree ?? []);
  return (
    <>
      <div className="md-object-head">
        <div>
          <h1>
            <EntityIcon type="classification" size={20} />
            {entityName(classification)}
            {system && <span className="healthy-badge">System</span>}
            {classification.mutually_exclusive === true && <span className="source-badge">互斥的</span>}
          </h1>
          <div className="md-object-meta">
            <span>
              {classification.tag_count} 个标签 · 被引用 {classification.usage_count} 次 · 版本 {classification.version}
            </span>
          </div>
        </div>
        <div className="md-actions">
          <button type="button" className="primary-button" onClick={() => setDialog({ parent: classification.fqn })}>
            <Plus size={14} />
            添加标签
          </button>
          <button type="button" className="small-button" onClick={() => openEntity("classification", classification.fqn)}>
            详情与版本
          </button>
          {!system && (
            <button
              type="button"
              className="small-button danger-text"
              onClick={() => window.confirm(`永久删除分类 ${classification.name} 及其全部标签？`) && void action.run(() => mdPost("/entities/delete", { entity_type: "classification", ref: classification.fqn, hard: true }).then(() => navigate("classification")))}
            >
              <Trash2 size={13} />
              删除
            </button>
          )}
        </div>
      </div>
      <section className="md-description">
        <div className="md-description-head">
          <span>描述</span>
          <button type="button" className="md-edit" aria-label="编辑描述" onClick={() => setEditing(true)}>
            <Pencil size={12} />
          </button>
        </div>
        <Description text={classification.description} />
      </section>
      {action.error && <ErrorBanner message={action.error} />}
      <section className="panel">
        {tags.loading && <Loading />}
        {tags.error && <ErrorBanner message={tags.error} />}
        {tags.data && (rows.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>标签</th>
                  <th>显示名称</th>
                  <th>描述</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ node, depth }) => (
                  <tr key={node.id}>
                    <td style={{ paddingLeft: 12 + depth * 18 }}>
                      <span className="md-tag-name">
                        {node.style?.color && <i style={{ background: node.style.color }} />}
                        <EntityLink entity={node}>{node.name}</EntityLink>
                      </span>
                    </td>
                    <td>{node.display_name || "--"}</td>
                    <td className="md-cell-desc">
                      {node.description ? <Description text={node.description} /> : "无描述"}
                      <small className="md-block-muted">使用率：{node.usage_count ? `${node.usage_count} 处` : "未使用"}</small>
                    </td>
                    <td>
                      <div className="md-actions">
                        <button type="button" className="md-edit" aria-label={`编辑 ${node.name}`} onClick={() => setDialog({ tag: node })}>
                          <Pencil size={13} />
                        </button>
                        <button
                          type="button"
                          className="md-edit"
                          aria-label={`删除 ${node.name}`}
                          disabled={node.provider === "system"}
                          title={node.provider === "system" ? "系统标签不能删除" : undefined}
                          onClick={() => window.confirm(`永久删除标签 ${node.fqn}？资产上的引用会一并移除。`) && void action.run(() => mdPost("/entities/delete", { entity_type: "tag", ref: node.fqn, hard: true }))}
                        >
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
          <Empty title="该分类下还没有标签" />
        ))}
      </section>
      {dialog && (
        <TagDialog
          classification={classification}
          tag={dialog.tag}
          parent={dialog.parent ?? classification.fqn}
          onClose={() => setDialog(null)}
          onDone={() => {
            setDialog(null);
            tags.reload();
            onChanged();
          }}
        />
      )}
      {editing && (
        <DescriptionDialog
          title={`编辑 ${entityName(classification)} 的描述`}
          initial={classification.description}
          onClose={() => setEditing(false)}
          onSave={(description) => mdPost("/entities/update", { entity_type: "classification", ref: classification.fqn, description }).then(onChanged)}
        />
      )}
    </>
  );
}

function ClassificationDialog({ onClose, onDone }: { onClose: () => void; onDone: (fqn: string) => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [exclusive, setExclusive] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setError("");
    try {
      const created = await mdPost<Entity>("/entities", { entity_type: "classification", name: name.trim(), display_name: displayName, description, fields: { mutually_exclusive: exclusive } });
      onDone(created.fqn);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Modal
      title="正在添加新分类"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary-button" disabled={!name.trim() || !description.trim()} onClick={() => void save()}>
            保存
          </button>
        </>
      }
    >
      <FormRow label="名称" htmlFor="classification-name" required>
        <input id="classification-name" value={name} placeholder="名称" onChange={(event) => setName(event.target.value)} />
      </FormRow>
      <FormRow label="显示名称" htmlFor="classification-display">
        <input id="classification-display" value={displayName} placeholder="显示名称" onChange={(event) => setDisplayName(event.target.value)} />
      </FormRow>
      <FormRow label="描述" required>
        <MarkdownEditor value={description} onChange={setDescription} placeholder="Write your description" rows={8} />
      </FormRow>
      <Toggle checked={exclusive} onChange={setExclusive} label="互斥的" />
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

function TagDialog({
  classification,
  tag,
  parent,
  onClose,
  onDone,
}: {
  classification: Entity;
  tag?: TreeNode;
  parent: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(tag?.name ?? "");
  const [displayName, setDisplayName] = useState(tag?.display_name ?? "");
  const [description, setDescription] = useState(tag?.description ?? "");
  const [iconUrl, setIconUrl] = useState(tag?.style?.icon_url ?? "");
  const [color, setColor] = useState(tag?.style?.color ?? "");
  const [error, setError] = useState("");
  async function save() {
    setError("");
    const style = { ...(color ? { color } : {}), ...(iconUrl ? { icon_url: iconUrl } : {}) };
    try {
      if (tag) await mdPost("/entities/update", { entity_type: "tag", ref: tag.fqn, display_name: displayName, description, style });
      else await mdPost("/entities", { entity_type: "tag", name: name.trim(), parent_fqn: parent, display_name: displayName, description, style });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Modal
      title={tag ? `编辑标签 ${tag.fqn}` : `在${entityName(classification)}上添加新标签`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary-button" disabled={!tag && (!name.trim() || !description.trim())} onClick={() => void save()}>
            保存
          </button>
        </>
      }
    >
      <FormRow label="名称" htmlFor="tag-name" required>
        <input id="tag-name" value={name} disabled={!!tag} onChange={(event) => setName(event.target.value)} />
      </FormRow>
      <FormRow label="显示名称" htmlFor="tag-display">
        <input id="tag-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
      </FormRow>
      <FormRow label="描述" required>
        <MarkdownEditor value={description} onChange={setDescription} placeholder="Write your description" rows={7} />
      </FormRow>
      <FormRow label="Icon URL" htmlFor="tag-icon">
        <input id="tag-icon" value={iconUrl} placeholder="Icon URL" onChange={(event) => setIconUrl(event.target.value)} />
      </FormRow>
      <FormRow label="颜色" htmlFor="tag-color">
        <ColorField id="tag-color" value={color} onChange={setColor} />
      </FormRow>
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

/* ------------------------------------------------------------------ domains */

interface DomainItem extends Entity {
  asset_count: number;
  experts: EntityRef[];
  data_products?: DomainItem[];
}

const DOMAIN_TYPE_HELP: Record<string, string> = {
  Aggregate: "聚合：更接近在线服务和事务数据库的域，包括事件和事务数据",
  "Consumer-aligned": "使用者对齐：从多个来源收集和整理数据的域，以提供汇总数据和数据产品，如客户 360、客户会话等，供其他域使用",
  "Source-aligned": "Source-aligned：面向用户的域，在这些域中，来自不同域的数据组合的最终产品可供商业用户或数据公民进行数据驱动决策",
};

export function DomainsPage({ route }: MetadataViewProps) {
  const domains = useApi<{ domains: DomainItem[]; tree: (DomainItem & { children: DomainItem[] })[]; data_products: DomainItem[] }>(metadataPath("/domains"));
  if (route.parts[0] === "new") return <DomainForm parents={domains.data?.domains ?? []} onDone={domains.reload} />;
  const items = domains.data?.domains ?? [];
  const selected = items.find((item) => item.fqn === route.parts[0]) ?? items[0];
  return (
    <div className="page-content md-page">
      <div className="md-split">
        <SplitList title="元数据工作区" addLabel="添加元数据工作区" onAdd={() => navigate("domains", "new")}>
          {domains.loading && <Loading />}
          {domains.error && <ErrorBanner message={domains.error} />}
          {items.map((item) => (
            <button type="button" key={item.id} className={`md-split-item ${selected?.id === item.id ? "active" : ""}`} onClick={() => navigate("domains", item.fqn)}>
              <span style={{ paddingLeft: (item.fqn.split(".").length - 1) * 12 }}>
                {item.style?.color && <i className="md-swatch" style={{ background: item.style.color }} />}
                {entityName(item)}
              </span>
              <small>{item.asset_count}</small>
            </button>
          ))}
          {domains.data && !items.length && <p className="muted md-split-empty">还没有元数据工作区</p>}
        </SplitList>
        <div className="md-split-main">
          {selected ? (
            <DomainDetail key={selected.id} domain={selected} products={(domains.data?.data_products ?? []).filter((product) => product.domain?.fqn === selected.fqn)} onChanged={domains.reload} />
          ) : (
            domains.data && (
              <Empty title="还没有元数据工作区">
                元数据工作区（数据域）按业务领域组织数据资产，每个域由团队负责，并以数据产品的形式对外提供数据。
              </Empty>
            )
          )}
        </div>
      </div>
    </div>
  );
}

function DomainDetail({ domain, products, onChanged }: { domain: DomainItem; products: DomainItem[]; onChanged: () => void }) {
  const assets = useApi<{ items: Entity[]; total: number }>(metadataPath("/entity/assets", { entity_type: "domain", ref: domain.fqn }));
  const [addingProduct, setAddingProduct] = useState(false);
  const action = useAction(onChanged);
  return (
    <>
      <div className="md-object-head">
        <div>
          <h1>
            {domain.style?.icon_url ? <img src={domain.style.icon_url} alt="" className="md-domain-icon" /> : <EntityIcon type="domain" size={20} />}
            {entityName(domain)}
            <span className="source-badge">{String(domain.domain_type ?? "")}</span>
          </h1>
          <div className="md-object-meta">
            <Owners owners={domain.owners} />
            <span>专家：{domain.experts.length ? domain.experts.map(entityName).join("、") : "无"}</span>
            <span>{domain.asset_count} 个资产 · {products.length} 个数据产品</span>
          </div>
        </div>
        <div className="md-actions">
          <button type="button" className="primary-button" onClick={() => setAddingProduct(true)}>
            <Plus size={14} />
            添加数据产品
          </button>
          <button type="button" className="small-button" onClick={() => openEntity("domain", domain.fqn)}>
            详情与版本
          </button>
          <button
            type="button"
            className="small-button danger-text"
            onClick={() => window.confirm(`永久删除元数据工作区 ${domain.name}？资产会被移出该工作区。`) && void action.run(() => mdPost("/entities/delete", { entity_type: "domain", ref: domain.fqn, hard: true }).then(() => navigate("domains")))}
          >
            <Trash2 size={13} />
            删除
          </button>
        </div>
      </div>
      <section className="md-description">
        <Description text={domain.description} />
      </section>
      {action.error && <ErrorBanner message={action.error} />}
      <section className="panel">
        <div className="panel-heading">
          <h2>数据产品</h2>
          <span>{products.length} 个</span>
        </div>
        <div className="panel-body">
          {products.length ? (
            <div className="md-card-grid">
              {products.map((product) => (
                <div key={product.id} className="md-product" onClick={() => openEntity("dataProduct", product.fqn)}>
                  <EntityIcon type="dataProduct" size={18} />
                  <strong>{entityName(product)}</strong>
                  <small>{product.asset_count} 个资产</small>
                  {product.description && <p>{product.description.slice(0, 120)}</p>}
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">还没有数据产品。数据产品把一组资产作为可复用的整体交付给使用者。</p>
          )}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>资产</h2>
          <span>{assets.data?.total ?? 0} 个 · 在资产详情页设置元数据工作区即可加入</span>
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
        </ul>
      </section>
      {addingProduct && (
        <ProductDialog
          domain={domain.fqn}
          onClose={() => setAddingProduct(false)}
          onDone={() => {
            setAddingProduct(false);
            onChanged();
          }}
        />
      )}
    </>
  );
}

function ProductDialog({ domain, onClose, onDone }: { domain: string; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [experts, setExperts] = useState<string[]>([]);
  const [error, setError] = useState("");
  async function save() {
    setError("");
    try {
      await mdPost("/entities", { entity_type: "dataProduct", name: name.trim(), display_name: displayName, description, domain, experts });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Modal
      title="添加数据产品"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={!name.trim()} onClick={() => void save()}>
            保存
          </button>
        </>
      }
    >
      <FormRow label="名称" htmlFor="product-name" required>
        <input id="product-name" value={name} onChange={(event) => setName(event.target.value)} />
      </FormRow>
      <FormRow label="显示名称" htmlFor="product-display">
        <input id="product-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
      </FormRow>
      <FormRow label="描述">
        <MarkdownEditor value={description} onChange={setDescription} rows={5} />
      </FormRow>
      <PeopleField label="专家" value={experts} onChange={setExperts} allowTeams={false} />
      {error && <ErrorBanner message={error} />}
    </Modal>
  );
}

function DomainForm({ parents, onDone }: { parents: Entity[]; onDone: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [iconUrl, setIconUrl] = useState("");
  const [color, setColor] = useState("");
  const [domainType, setDomainType] = useState("");
  const [parent, setParent] = useState("");
  const [owners, setOwners] = useState<string[]>([]);
  const [experts, setExperts] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    setError("");
    try {
      const created = await mdPost<Entity>("/entities", {
        entity_type: "domain",
        name: name.trim(),
        ...(parent ? { parent_fqn: parent } : {}),
        display_name: displayName,
        description,
        owners,
        experts,
        style: { ...(color ? { color } : {}), ...(iconUrl ? { icon_url: iconUrl } : {}) },
        fields: { domain_type: domainType },
      });
      onDone();
      navigate("domains", created.fqn);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return (
    <div className="page-content md-page">
      <div className="md-breadcrumbs">
        <button type="button" className="md-link" onClick={() => navigate("domains")}>
          元数据工作区
        </button>
        <i>/</i>
        <b>添加元数据工作区</b>
      </div>
      <div className="md-form-layout">
        <div className="md-form">
          <h2>添加元数据工作区</h2>
          <FormRow label="名称" htmlFor="domain-name" required>
            <input id="domain-name" value={name} placeholder="名称" onChange={(event) => setName(event.target.value)} />
          </FormRow>
          <FormRow label="显示名称" htmlFor="domain-display">
            <input id="domain-display" value={displayName} placeholder="显示名称" onChange={(event) => setDisplayName(event.target.value)} />
          </FormRow>
          <FormRow label="描述" required>
            <MarkdownEditor value={description} onChange={setDescription} placeholder="Write your description" />
          </FormRow>
          <FormRow label="Icon URL" htmlFor="domain-icon">
            <input id="domain-icon" value={iconUrl} placeholder="Icon URL" onChange={(event) => setIconUrl(event.target.value)} />
          </FormRow>
          <FormRow label="颜色" htmlFor="domain-color">
            <ColorField id="domain-color" value={color} onChange={setColor} />
          </FormRow>
          <FormRow label="域类型" htmlFor="domain-type" required>
            <select id="domain-type" value={domainType} onChange={(event) => setDomainType(event.target.value)}>
              <option value="" />
              {Object.keys(DOMAIN_TYPE_HELP).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </FormRow>
          {parents.length > 0 && (
            <FormRow label="上级工作区" htmlFor="domain-parent" help="可选；设置后成为子域">
              <select id="domain-parent" value={parent} onChange={(event) => setParent(event.target.value)}>
                <option value="">无（顶级工作区）</option>
                {parents.map((item) => (
                  <option key={item.id} value={item.fqn}>
                    {item.fqn}
                  </option>
                ))}
              </select>
            </FormRow>
          )}
          <PeopleField label="所有者" value={owners} onChange={setOwners} />
          <PeopleField label="专家" value={experts} onChange={setExperts} allowTeams={false} />
          {error && <ErrorBanner message={error} />}
          <div className="md-form-actions">
            <button type="button" className="text-button" onClick={() => navigate("domains")}>
              取消
            </button>
            <button type="button" className="primary-button" disabled={busy || !name.trim() || !description.trim() || !domainType} onClick={() => void save()}>
              保 存
            </button>
          </div>
        </div>
        <aside className="md-help">
          <h3>配置元数据工作区</h3>
          <p>
            A data mesh is a decentralized data architecture that organizes data by a specific business domain following the concepts of domain-oriented design. Teams take ownership of both operational and analytical data that belongs to the domain.
          </p>
          <h3>域类型</h3>
          <p>There are three types of domains: Aggregate, Consumer-aligned and Source-aligned.</p>
          {Object.entries(DOMAIN_TYPE_HELP).map(([key, text]) => (
            <div key={key}>
              <h4>{key}：</h4>
              <p>{text.split("：").slice(1).join("：")}</p>
            </div>
          ))}
        </aside>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ custom properties */

interface PropertyDefinition {
  entity_type: string;
  name: string;
  display_name: string;
  property_type: string;
  description: string;
  config: { values?: string[]; multi_select?: boolean; entity_types?: string[] };
}

const PROPERTY_ENTITY_TYPES = Object.keys(TYPE_LABELS).filter((type) => !["kpi", "eventSubscription"].includes(type));

function referenceTypesLabel(types: string[], dataAssets: string[]) {
  if (dataAssets.length && types.length === dataAssets.length && dataAssets.every((type) => types.includes(type))) return "全部数据资产";
  return types.map((type) => TYPE_LABELS[type] ?? type).join("、");
}

export function PropertiesPage(_props: MetadataViewProps) {
  const types = useApi<TypesResponse>(metadataPath("/types"));
  const [entityType, setEntityType] = useState("table");
  const definitions = useApi<{ items: PropertyDefinition[] }>(metadataPath("/properties", { entity_type: entityType }));
  const dataAssets = (types.data?.entity_types ?? []).filter((type) => type.data_asset).map((type) => type.id);
  const [editing, setEditing] = useState<PropertyDefinition | null>(null);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [propertyType, setPropertyType] = useState("string");
  const [description, setDescription] = useState("");
  const [values, setValues] = useState("");
  const [multi, setMulti] = useState(false);
  const [refTypes, setRefTypes] = useState<string[] | null>(null);
  const action = useAction(definitions.reload);
  const chosenRefTypes = refTypes ?? dataAssets;
  function reset() {
    setEditing(null);
    setName("");
    setDisplayName("");
    setPropertyType("string");
    setDescription("");
    setValues("");
    setMulti(false);
    setRefTypes(null);
  }
  useEffect(() => {
    action.setError("");
    reset();
  }, [entityType]);
  function edit(item: PropertyDefinition) {
    action.setError("");
    setEditing(item);
    setName(item.name);
    setDisplayName(item.display_name);
    setPropertyType(item.property_type);
    setDescription(item.description);
    setValues((item.config.values ?? []).join("，"));
    setMulti(!!item.config.multi_select);
    setRefTypes(item.config.entity_types ?? null);
  }
  function config() {
    if (propertyType === "enum") return { values: values.split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean), multi_select: multi };
    if (propertyType === "entityReference") return { entity_types: chosenRefTypes };
    return {};
  }
  async function save() {
    await action.run(async () => {
      if (editing) {
        await mdPost("/properties/update", { entity_type: entityType, name: editing.name, display_name: displayName, description, config: config() });
      } else {
        await mdPost("/properties", { entity_type: entityType, name: name.trim(), display_name: displayName, property_type: propertyType, description, config: config() });
      }
      reset();
    });
  }
  function toggleRefType(type: string, checked: boolean) {
    setRefTypes(checked ? [...chosenRefTypes, type] : chosenRefTypes.filter((item) => item !== type));
  }
  const missingValues = propertyType === "enum" && !values.trim();
  const missingRefTypes = propertyType === "entityReference" && !chosenRefTypes.length;
  return (
    <div className="page-content md-page">
      <div className="md-page-head">
        <div>
          <h1>自定义属性</h1>
          <p>为不同类型的元数据扩展业务属性，例如数据保留期、合规等级、成本中心；属性值在资产详情页填写并纳入版本历史</p>
        </div>
      </div>
      <div className="md-split">
        <aside className="md-split-list">
          <div className="md-split-head">实体类型</div>
          <div className="md-split-items">
            {PROPERTY_ENTITY_TYPES.map((type) => (
              <button type="button" key={type} className={`md-split-item ${entityType === type ? "active" : ""}`} onClick={() => setEntityType(type)}>
                <span>
                  <EntityIcon type={type} size={14} /> {TYPE_LABELS[type]}
                </span>
              </button>
            ))}
          </div>
        </aside>
        <div className="md-split-main">
          <section className="panel">
            <div className="panel-heading">
              <h2>{TYPE_LABELS[entityType]}的属性</h2>
              <span>{definitions.data?.items.length ?? 0} 个</span>
            </div>
            {definitions.error && <ErrorBanner message={definitions.error} />}
            {definitions.data && (definitions.data.items.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>显示名称</th>
                      <th>类型</th>
                      <th>描述</th>
                      <th>取值</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {definitions.data.items.map((item) => (
                      <tr key={item.name}>
                        <td>
                          <code>{item.name}</code>
                        </td>
                        <td>{item.display_name || "--"}</td>
                        <td>{item.property_type}</td>
                        <td>{item.description || "—"}</td>
                        <td>
                          {item.config.values
                            ? `${item.config.values.join("、")}${item.config.multi_select ? "（多选）" : ""}`
                            : item.config.entity_types
                              ? referenceTypesLabel(item.config.entity_types, dataAssets)
                              : "—"}
                        </td>
                        <td>
                          <div className="md-actions">
                            <button type="button" className="md-edit" aria-label={`编辑属性 ${item.name}`} onClick={() => edit(item)}>
                              <Pencil size={13} />
                            </button>
                            <button
                              type="button"
                              className="md-edit"
                              aria-label={`删除属性 ${item.name}`}
                              onClick={() =>
                                window.confirm(`删除属性定义 ${item.name}？所有${TYPE_LABELS[entityType]}上已填写的该属性值会一并清除。`) &&
                                void action.run(async () => {
                                  await mdPost("/properties/delete", { entity_type: entityType, name: item.name });
                                  if (editing?.name === item.name) reset();
                                })
                              }
                            >
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
              <Empty title="还没有自定义属性" />
            ))}
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>{editing ? `编辑属性 ${editing.name}` : "添加属性"}</h2>
            </div>
            <div className="panel-body md-properties">
              <div className="md-form-inline">
                <FormRow label="属性名" htmlFor="property-name" required help={editing ? "属性名创建后不能修改" : "字母开头，只含字母、数字和下划线"}>
                  <input id="property-name" value={name} disabled={!!editing} onChange={(event) => setName(event.target.value)} placeholder="retention_days" />
                </FormRow>
                <FormRow label="显示名称" htmlFor="property-display">
                  <input id="property-display" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="保留天数" />
                </FormRow>
              </div>
              <FormRow label="类型" htmlFor="property-type" help={editing ? "类型创建后不能修改，如需更换请删除后重新添加" : undefined}>
                <select id="property-type" value={propertyType} disabled={!!editing} onChange={(event) => setPropertyType(event.target.value)}>
                  {(types.data?.custom_property_types ?? ["string", "markdown", "integer", "number", "boolean", "date", "enum", "entityReference"]).map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </FormRow>
              {propertyType === "enum" && (
                <>
                  <FormRow label="可选值" htmlFor="property-values" required help={editing ? "逗号分隔；已被资产使用的取值不能删除" : "逗号分隔"}>
                    <input id="property-values" value={values} onChange={(event) => setValues(event.target.value)} placeholder="公开, 内部, 机密" />
                  </FormRow>
                  <Toggle checked={multi} onChange={setMulti} label="允许多选" />
                </>
              )}
              {propertyType === "entityReference" && (
                <FormRow label="可引用的类型" required help="属性值只能引用这些类型的对象；默认是全部数据资产">
                  <div className="md-checks">
                    {PROPERTY_ENTITY_TYPES.map((type) => (
                      <label key={type}>
                        <input type="checkbox" checked={chosenRefTypes.includes(type)} onChange={(event) => toggleRefType(type, event.target.checked)} />
                        {TYPE_LABELS[type]}
                      </label>
                    ))}
                  </div>
                </FormRow>
              )}
              <FormRow label="描述" htmlFor="property-description">
                <input id="property-description" value={description} onChange={(event) => setDescription(event.target.value)} />
              </FormRow>
              {action.error && <ErrorBanner message={action.error} />}
              <div className="md-form-actions">
                <span className="muted">
                  <Info size={12} /> 属性值会随资产一起导出，并出现在 AI 上下文中。
                </span>
                {editing && (
                  <button type="button" className="small-button" onClick={reset}>
                    取消
                  </button>
                )}
                <button type="button" className="primary-button" disabled={!name.trim() || missingValues || missingRefTypes} onClick={() => void save()}>
                  {editing ? <Pencil size={14} /> : <Plus size={14} />}
                  {editing ? "保存修改" : "添加"}
                </button>
              </div>
            </div>
          </section>
          <button type="button" className="text-button" onClick={() => navigate("explore")}>
            <ArrowLeft size={12} />
            返回元数据资产
          </button>
        </div>
      </div>
    </div>
  );
}
