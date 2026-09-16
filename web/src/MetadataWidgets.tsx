// SPDX-License-Identifier: Apache-2.0
/** Shared building blocks of the 元数据管理 screens: icons, chips, pickers, editors, dialogs. */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Archive,
  ArrowLeft,
  ArrowRight,
  BellRing,
  Bold,
  BookMarked,
  BookOpen,
  Boxes,
  BrainCircuit,
  Braces,
  ChartColumn,
  Check,
  Code,
  Copy,
  Database,
  FolderTree,
  Globe,
  Heading,
  Italic,
  LayoutDashboard,
  Link2,
  List,
  ListOrdered,
  Minus,
  Network,
  Package,
  Plug,
  Quote,
  Radio,
  Search,
  Server,
  SquareCode,
  Strikethrough,
  Table2,
  Tag,
  Tags,
  Target,
  User,
  Users,
  Webhook,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import { errorMessage } from "./api";
import { ErrorBanner, Loading } from "./components";
import { useApi } from "./SourceBrowser";
import {
  entityName,
  metadataPath,
  openEntity,
  tierLabel,
  TERM_STATUS_LABELS,
  type EntityRef,
  type PersonOption,
  type TagLabel,
  type TagOption,
} from "./metadataApi";

/* ------------------------------------------------------------------ icons */

const ICONS: Record<string, LucideIcon> = {
  databaseService: Server,
  database: Database,
  databaseSchema: FolderTree,
  table: Table2,
  storedProcedure: Code,
  messagingService: Radio,
  topic: Radio,
  dashboardService: LayoutDashboard,
  dashboard: LayoutDashboard,
  chart: ChartColumn,
  dashboardDataModel: Boxes,
  pipelineService: Workflow,
  pipeline: Workflow,
  mlmodelService: BrainCircuit,
  mlmodel: BrainCircuit,
  storageService: Archive,
  container: Archive,
  searchService: Search,
  searchIndex: Search,
  apiService: Plug,
  apiCollection: Webhook,
  apiEndpoint: Webhook,
  metadataService: Braces,
  semanticModel: Network,
  glossary: BookOpen,
  glossaryTerm: BookMarked,
  classification: Tags,
  tag: Tag,
  domain: Globe,
  dataProduct: Package,
  team: Users,
  user: User,
  kpi: Target,
  eventSubscription: BellRing,
};

export function EntityIcon({ type, size = 16 }: { type: string; size?: number }) {
  const Icon = ICONS[type] ?? Database;
  return (
    <Icon
      size={size}
      className={`md-entity-icon md-icon-${type}`}
      aria-hidden="true"
    />
  );
}

/* ------------------------------------------------------------------ small pieces */

export function useDebounced<T>(value: T, delay = 300): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setCurrent(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return current;
}

export function EntityLink({
  entity,
  children,
}: {
  entity: { entity_type: string; fqn: string; display_name?: string; name?: string };
  children?: ReactNode;
}) {
  return (
    <button
      type="button"
      className="md-link"
      title={entity.fqn}
      onClick={() => openEntity(entity.entity_type, entity.fqn)}
    >
      {children ?? (entity.display_name || entity.name || entity.fqn)}
    </button>
  );
}

export function Breadcrumbs({ items, current }: { items: EntityRef[]; current?: string }) {
  return (
    <nav className="md-breadcrumbs" aria-label="层级">
      {items.map((item) => (
        <span key={item.id}>
          <EntityLink entity={item}>{entityName(item)}</EntityLink>
          <i>/</i>
        </span>
      ))}
      {current && <b>{current}</b>}
    </nav>
  );
}

export function TagChip({
  tag,
  onRemove,
}: {
  tag: Pick<TagLabel, "tag_fqn" | "source"> & Partial<Pick<TagLabel, "description" | "style" | "state">>;
  onRemove?: () => void;
}) {
  const color = tag.style?.color;
  return (
    <span
      className={`md-tag ${tag.source === "glossary" ? "md-tag-term" : ""} ${tag.state === "suggested" ? "md-tag-suggested" : ""}`}
      title={tag.description || tag.tag_fqn}
      style={color ? { borderColor: color, boxShadow: `inset 3px 0 0 ${color}` } : undefined}
    >
      {tag.source === "glossary" ? <BookMarked size={11} /> : <Tag size={11} />}
      <span>{tag.tag_fqn}</span>
      {onRemove && (
        <button type="button" aria-label={`移除 ${tag.tag_fqn}`} onClick={onRemove}>
          <X size={10} />
        </button>
      )}
    </span>
  );
}

export function TagList({ tags, empty = "—" }: { tags: TagLabel[]; empty?: string }) {
  if (!tags.length) return <span className="md-muted-inline">{empty}</span>;
  return (
    <span className="md-tag-list">
      {tags.map((tag) => (
        <TagChip key={`${tag.source}:${tag.tag_fqn}`} tag={tag} />
      ))}
    </span>
  );
}

export function Owners({ owners, empty = "没有所有者" }: { owners: EntityRef[]; empty?: string }) {
  if (!owners.length)
    return (
      <span className="md-muted-inline">
        <User size={13} />
        {empty}
      </span>
    );
  return (
    <span className="md-owners">
      {owners.map((owner) => (
        <span key={owner.id} className="md-owner" title={owner.fqn}>
          <span className={`md-avatar ${owner.entity_type === "team" ? "team" : ""}`}>
            {entityName(owner).slice(0, 1).toUpperCase()}
          </span>
          {entityName(owner)}
        </span>
      ))}
    </span>
  );
}

export function TierBadge({ tier }: { tier?: string | null }) {
  return tier ? (
    <span className="md-tier">{tierLabel(tier)}</span>
  ) : (
    <span className="md-muted-inline">没有分级</span>
  );
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="md-facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <label className={`md-toggle ${disabled ? "disabled" : ""}`}>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="md-toggle-track">
        <i />
      </span>
      {label && <span>{label}</span>}
    </label>
  );
}

export function SearchBox({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label?: string;
}) {
  return (
    <div className="md-search">
      <Search size={14} />
      <input
        aria-label={label ?? placeholder}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {value && (
        <button type="button" aria-label="清除搜索" onClick={() => onChange("")}>
          <X size={12} />
        </button>
      )}
    </div>
  );
}

export function Pager({
  total,
  page,
  size,
  onPage,
}: {
  total: number;
  page: number;
  size: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  if (total <= size && page === 1) return null;
  return (
    <div className="md-pager">
      <button
        type="button"
        className="secondary-button"
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
      >
        <ArrowLeft size={13} />
        上一步
      </button>
      <span>
        {page}/{pages} 页
      </span>
      <button
        type="button"
        className="secondary-button"
        disabled={page >= pages}
        onClick={() => onPage(page + 1)}
      >
        下一步
        <ArrowRight size={13} />
      </button>
      <span className="muted">共 {total.toLocaleString()} 条</span>
    </div>
  );
}

export function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="small-button"
      onClick={() => {
        void navigator.clipboard?.writeText(text)?.then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        });
      }}
    >
      {done ? <Check size={12} /> : <Copy size={12} />}
      {done ? "已复制" : label}
    </button>
  );
}

export function RunStatus({ status }: { status?: string | null }) {
  const labels: Record<string, [string, string]> = {
    success: ["成功", "healthy-badge"],
    partial: ["部分成功", "quality-warn-badge"],
    failed: ["失败", "failure-badge"],
    running: ["运行中", "neutral-badge"],
    skipped: ["已跳过", "neutral-badge"],
  };
  const [label, className] = labels[status ?? ""] ?? ["未运行", "neutral-badge"];
  return <span className={className}>{label}</span>;
}

export function ColorField({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="md-color">
      <input
        id={id}
        type="color"
        aria-label="选择颜色"
        value={/^#[0-9a-f]{6}$/i.test(value) ? value : "#000000"}
        onChange={(event) => onChange(event.target.value)}
      />
      <input
        aria-label="HEX 颜色代码"
        value={value}
        placeholder="选择或输入 HEX 颜色代码"
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ dialogs */

export function Modal({
  title,
  onClose,
  children,
  footer,
  width = 600,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="md-modal-backdrop" onMouseDown={onClose}>
      <div
        className="md-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{ maxWidth: width }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="md-modal-head">
          <h2>{title}</h2>
          <button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={17} />
          </button>
        </div>
        <div className="md-modal-body">{children}</div>
        {footer && <div className="md-modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

function useSaving(onSave: () => Promise<void> | void, onClose: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      await onSave();
      onClose();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }
  return { busy, error, save };
}

export function TagPickerModal({
  title,
  source,
  initial,
  onClose,
  onSave,
}: {
  title: string;
  source?: "classification" | "glossary";
  initial: string[];
  onClose: () => void;
  onSave: (fqns: string[]) => Promise<void> | void;
}) {
  const options = useApi<{ items: TagOption[] }>(metadataPath("/tags/options"));
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>(initial);
  const saving = useSaving(() => onSave(selected), onClose);
  const q = query.trim().toLowerCase();
  const groups = useMemo(() => {
    const result = new Map<string, TagOption[]>();
    for (const item of options.data?.items ?? []) {
      if (source && item.source !== source) continue;
      if (q && !item.fqn.toLowerCase().includes(q) && !item.display_name.toLowerCase().includes(q) && !item.description.toLowerCase().includes(q))
        continue;
      const key = `${item.source === "glossary" ? "术语库" : "分类"} · ${item.root}`;
      result.set(key, [...(result.get(key) ?? []), item]);
    }
    return [...result.entries()];
  }, [options.data, source, q]);
  function toggle(fqn: string) {
    setSelected((current) =>
      current.includes(fqn) ? current.filter((item) => item !== fqn) : [...current, fqn],
    );
  }
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={saving.busy}
            onClick={() => void saving.save()}
          >
            {saving.busy ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="md-picker-selected">
        {selected.length ? (
          selected.map((fqn) => (
            <TagChip
              key={fqn}
              tag={{ tag_fqn: fqn, source: (options.data?.items ?? []).find((item) => item.fqn === fqn)?.source ?? "classification" }}
              onRemove={() => toggle(fqn)}
            />
          ))
        ) : (
          <span className="muted">尚未选择</span>
        )}
      </div>
      <SearchBox value={query} onChange={setQuery} placeholder="搜索标签或术语" />
      {options.loading && <Loading text="正在读取标签…" />}
      {options.error && <ErrorBanner message={options.error} />}
      <div className="md-picker-list">
        {groups.map(([group, items]) => (
          <div key={group}>
            <h4>{group}</h4>
            {items.map((item) => (
              <label key={item.fqn} className="md-picker-item">
                <input
                  type="checkbox"
                  checked={selected.includes(item.fqn)}
                  onChange={() => toggle(item.fqn)}
                />
                <span>
                  <strong>{item.display_name}</strong>
                  <small>
                    {item.fqn}
                    {item.status ? ` · ${TERM_STATUS_LABELS[item.status] ?? item.status}` : ""}
                  </small>
                  {item.description && <em>{item.description}</em>}
                </span>
              </label>
            ))}
          </div>
        ))}
        {!options.loading && !groups.length && <p className="muted">没有匹配的标签。</p>}
      </div>
      {saving.error && <ErrorBanner message={saving.error} />}
    </Modal>
  );
}

export function PeoplePickerModal({
  title,
  initial,
  allowTeams = true,
  single = false,
  onClose,
  onSave,
}: {
  title: string;
  initial: string[];
  allowTeams?: boolean;
  single?: boolean;
  onClose: () => void;
  onSave: (fqns: string[]) => Promise<void> | void;
}) {
  const options = useApi<{ items: PersonOption[] }>(metadataPath("/people/options"));
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>(initial);
  const saving = useSaving(() => onSave(selected), onClose);
  const q = query.trim().toLowerCase();
  const items = (options.data?.items ?? [])
    .filter((item) => allowTeams || item.entity_type === "user")
    .filter((item) => !q || item.fqn.toLowerCase().includes(q) || item.display_name.toLowerCase().includes(q));
  function toggle(fqn: string) {
    setSelected((current) =>
      single
        ? current.includes(fqn)
          ? []
          : [fqn]
        : current.includes(fqn)
          ? current.filter((item) => item !== fqn)
          : [...current, fqn],
    );
  }
  return (
    <Modal
      title={title}
      onClose={onClose}
      width={520}
      footer={
        <>
          <button type="button" className="text-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={saving.busy}
            onClick={() => void saving.save()}
          >
            {saving.busy ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <SearchBox value={query} onChange={setQuery} placeholder={allowTeams ? "搜索用户或团队" : "搜索用户"} />
      {options.loading && <Loading text="正在读取用户与团队…" />}
      {options.error && <ErrorBanner message={options.error} />}
      <div className="md-picker-list">
        {items.map((item) => (
          <label key={item.id} className="md-picker-item">
            <input
              type={single ? "radio" : "checkbox"}
              name="md-people"
              checked={selected.includes(item.fqn)}
              onChange={() => toggle(item.fqn)}
            />
            <span className={`md-avatar ${item.entity_type === "team" ? "team" : ""}`}>
              {entityName(item).slice(0, 1).toUpperCase()}
            </span>
            <span>
              <strong>{entityName(item)}</strong>
              <small>
                {item.entity_type === "team" ? "团队" : item.is_bot ? "机器人" : "用户"} · {item.fqn}
              </small>
            </span>
          </label>
        ))}
        {!options.loading && !items.length && <p className="muted">没有匹配的用户或团队。</p>}
      </div>
      {saving.error && <ErrorBanner message={saving.error} />}
    </Modal>
  );
}

/* ------------------------------------------------------------------ markdown */

const INLINE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(~~[^~]+~~)|(\*[^*\s][^*]*\*)|(\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;

function inline(text: string, prefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let count = 0;
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(text.slice(last, start));
    const token = match[0];
    const key = `${prefix}-${count++}`;
    if (match[1]) nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (match[2]) nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    else if (match[3]) nodes.push(<del key={key}>{token.slice(2, -2)}</del>);
    else if (match[4]) nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    else
      nodes.push(
        <a key={key} href={match[7]} target="_blank" rel="noreferrer">
          {match[6]}
        </a>,
      );
    last = start + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

const BLOCK_START = /^(#{1,4}\s|```|>|\s*[-*]\s|\s*\d+[.)]\s|\s*\|)/;

function parseMarkdown(text: string): ReactNode[] {
  const lines = text.replaceAll("\r\n", "\n").split("\n");
  const out: ReactNode[] = [];
  let index = 0;
  let key = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(
        <pre key={key++}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const content = inline(heading[2], `h${key}`);
      out.push(heading[1].length <= 2 ? <h3 key={key++}>{content}</h3> : <h4 key={key++}>{content}</h4>);
      index += 1;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      out.push(<hr key={key++} />);
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*]\s+/;
      const items: ReactNode[] = [];
      while (index < lines.length && pattern.test(lines[index])) {
        items.push(<li key={index}>{inline(lines[index].replace(pattern, ""), `li${index}`)}</li>);
        index += 1;
      }
      out.push(ordered ? <ol key={key++}>{items}</ol> : <ul key={key++}>{items}</ul>);
      continue;
    }
    if (line.startsWith(">")) {
      const quote: string[] = [];
      while (index < lines.length && lines[index].startsWith(">")) {
        quote.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      out.push(<blockquote key={key++}>{inline(quote.join(" "), `q${key}`)}</blockquote>);
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        const cells = lines[index].trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
        if (!cells.every((cell) => /^:?-{2,}:?$/.test(cell))) rows.push(cells);
        index += 1;
      }
      const [head, ...body] = rows;
      out.push(
        <div className="table-scroll" key={key++}>
          <table>
            <thead>
              <tr>
                {(head ?? []).map((cell, i) => (
                  <th key={i}>{inline(cell, `th${i}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{inline(cell, `td${i}-${j}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const paragraph: string[] = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !BLOCK_START.test(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    out.push(<p key={key++}>{inline(paragraph.join(" "), `p${key}`)}</p>);
  }
  return out;
}

export function MarkdownView({ text, className = "" }: { text: string; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(text), [text]);
  return <div className={`md-markdown ${className}`}>{blocks}</div>;
}

export function Description({ text, empty = "无描述" }: { text?: string | null; empty?: string }) {
  return text && text.trim() ? <MarkdownView text={text} /> : <span className="md-muted-inline">{empty}</span>;
}

export function MarkdownEditor({
  id,
  value,
  onChange,
  placeholder = "编写您的描述",
  rows = 6,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  const [mode, setMode] = useState<"write" | "preview">("write");
  const ref = useRef<HTMLTextAreaElement>(null);
  function select(start: number, end: number) {
    window.requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(start, end);
    });
  }
  function wrap(before: string, after: string, sample: string) {
    const element = ref.current;
    const start = element?.selectionStart ?? value.length;
    const end = element?.selectionEnd ?? value.length;
    const chosen = value.slice(start, end) || sample;
    onChange(value.slice(0, start) + before + chosen + after + value.slice(end));
    select(start + before.length, start + before.length + chosen.length);
  }
  function prefix(marker: string) {
    const element = ref.current;
    const start = element?.selectionStart ?? value.length;
    const lineStart = value.lastIndexOf("\n", start - 1) + 1;
    onChange(value.slice(0, lineStart) + marker + value.slice(lineStart));
    select(start + marker.length, start + marker.length);
  }
  const tools: { label: string; icon: LucideIcon; run: () => void }[] = [
    { label: "标题", icon: Heading, run: () => prefix("### ") },
    { label: "加粗", icon: Bold, run: () => wrap("**", "**", "加粗文本") },
    { label: "斜体", icon: Italic, run: () => wrap("*", "*", "斜体文本") },
    { label: "删除线", icon: Strikethrough, run: () => wrap("~~", "~~", "删除的文本") },
    { label: "无序列表", icon: List, run: () => prefix("- ") },
    { label: "有序列表", icon: ListOrdered, run: () => prefix("1. ") },
    { label: "链接", icon: Link2, run: () => wrap("[", "](https://)", "链接文字") },
    { label: "分隔线", icon: Minus, run: () => wrap("\n---\n", "", "") },
    { label: "引用", icon: Quote, run: () => prefix("> ") },
    { label: "行内代码", icon: Code, run: () => wrap("`", "`", "code") },
    { label: "代码块", icon: SquareCode, run: () => wrap("```\n", "\n```", "SELECT 1") },
  ];
  return (
    <div className="md-editor">
      <div className="md-editor-bar">
        <button type="button" className={mode === "write" ? "active" : ""} onClick={() => setMode("write")}>
          Write
        </button>
        <button type="button" className={mode === "preview" ? "active" : ""} onClick={() => setMode("preview")}>
          Preview
        </button>
        <span className="md-editor-tools">
          {tools.map((tool) => (
            <button
              key={tool.label}
              type="button"
              title={tool.label}
              aria-label={tool.label}
              disabled={mode !== "write"}
              onClick={tool.run}
            >
              <tool.icon size={14} />
            </button>
          ))}
        </span>
      </div>
      {mode === "write" ? (
        <textarea
          id={id}
          ref={ref}
          rows={rows}
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <div className="md-editor-preview">
          {value.trim() ? <MarkdownView text={value} /> : <span className="muted">暂无内容</span>}
        </div>
      )}
    </div>
  );
}

/** A labelled form row; `required` adds the red asterisk the product screens use. */
export function FormRow({
  label,
  htmlFor,
  required = false,
  help,
  children,
}: {
  label: string;
  htmlFor?: string;
  required?: boolean;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="md-form-row">
      <label htmlFor={htmlFor}>
        {required && <b>*</b>}
        {label}
      </label>
      {children}
      {help && <small>{help}</small>}
    </div>
  );
}
