// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  FolderOpen,
  RefreshCw,
  Send,
  Server,
  Trash2,
} from "lucide-react";
import { request, errorMessage, type ConsoleContext } from "./api";
import { Empty, ErrorBanner, Loading } from "./components";

interface NativeModel {
  name: string;
  entity_version: string;
  document_version: string;
  model_name: string;
  created_at: string;
  updated_at: string;
  size_bytes: number;
}
interface NativeListing {
  catalog: string;
  namespace: string[];
  items: NativeModel[];
  implementation: string;
  storage: string;
}
interface LoadedNative {
  name: string;
  entity_version: string;
  yaml: string;
}
interface Published {
  name: string;
  action: "created" | "updated";
  entity_version: string;
}

const NAME_PATTERN = /^[A-Za-z0-9\-_]+$/;

export function nativeName(label: string): string {
  const cleaned = label
    .trim()
    .replace(/[^A-Za-z0-9\-_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || "semantic_model";
}

interface Props {
  yaml: string;
  draftName: string;
  editorBusy: boolean;
  onLoad: (yaml: string, name: string) => boolean;
  explore: (search: string, context?: ConsoleContext) => void;
}

/**
 * The five native Polaris semantic-model operations (createSemanticModel, listSemanticModels,
 * loadSemanticModel, updateSemanticModel, dropSemanticModel) as implemented by the Lattice gateway.
 */
export default function NativeModelsPanel({
  yaml,
  draftName,
  editorBusy,
  onLoad,
  explore,
}: Props) {
  const [catalog, setCatalog] = useState("lattice");
  const [namespace, setNamespace] = useState("demo");
  const [target, setTarget] = useState({
    catalog: "lattice",
    namespace: "demo",
  });
  const [modelName, setModelName] = useState(() => nativeName(draftName));
  const [touched, setTouched] = useState(false);
  /** entity-version of the model currently in the editor, from the load that put it there. */
  const [loadedVersion, setLoadedVersion] = useState<{
    name: string;
    version: string;
  } | null>(null);
  const [items, setItems] = useState<NativeModel[]>([]);
  const [listBusy, setListBusy] = useState(false);
  const [listError, setListError] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const listRequest = useRef(0);

  useEffect(() => {
    if (!touched) setModelName(nativeName(draftName));
  }, [draftName, touched]);
  const location = `${target.catalog}.${target.namespace.replace(/\u001f/g, ".")}`;
  /** The first name at this target that no model holds yet.
   *  createSemanticModel's example carries one fixed name, so it answers 409
   *  AlreadyExists from the moment a model of that name exists - including the
   *  moment right after the example itself was run once. */
  function freeName() {
    const base = NAME_PATTERN.test(modelName.trim())
      ? modelName.trim()
      : "semantic_model";
    const taken = new Set(items.map((item) => item.name));
    if (!taken.has(base)) return base;
    for (let suffix = 2; suffix <= items.length + 2; suffix += 1)
      if (!taken.has(`${base}_${suffix}`)) return `${base}_${suffix}`;
    return base;
  }

  /** Open the console on this panel's own target instead of placeholder values.
   *  The console sends path parameters verbatim, and both the gateway and the
   *  Polaris client read %1F as the Iceberg namespace separator. A spec example
   *  can only hold fixed values, so the two that no real call can use - the
   *  sample entity-version and the already-taken model name - are replaced by
   *  what this panel knows about the target. */
  function openConsole() {
    const name = modelName.trim();
    const path: Record<string, string> = {
      prefix: target.catalog,
      namespace: target.namespace.replace(/\u001f/g, "%1F"),
    };
    if (name) path["semantic-model-name"] = name;
    const known =
      loadedVersion?.name === name
        ? loadedVersion.version
        : items.find((item) => item.name === name)?.entity_version;
    explore("semantic", {
      path,
      body: {
        createSemanticModel: { name: freeName() },
        ...(known
          ? { updateSemanticModel: { "entity-version": known } }
          : {}),
      },
    });
  }

  async function refresh(next = target) {
    const requestId = ++listRequest.current;
    setListBusy(true);
    setListError("");
    try {
      const data = await request<NativeListing>(
        `/api/semantic-models?catalog=${encodeURIComponent(next.catalog)}&namespace=${encodeURIComponent(next.namespace)}`,
      );
      if (requestId !== listRequest.current) return;
      setItems(data.items);
    } catch (e) {
      if (requestId === listRequest.current) {
        setItems([]);
        setListError(errorMessage(e));
      }
    } finally {
      if (requestId === listRequest.current) setListBusy(false);
    }
  }
  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  function applyTarget() {
    const next = {
      catalog: catalog.trim(),
      namespace: namespace.trim().replace(/\./g, "\u001f"),
    };
    if (!next.catalog || !next.namespace) return;
    setTarget(next);
  }

  async function publish() {
    const name = modelName.trim();
    if (busy || editorBusy || !yaml.trim()) return;
    if (!NAME_PATTERN.test(name)) {
      setError("原生模型名称只能包含字母、数字、连字符和下划线。");
      return;
    }
    setBusy("publish");
    setError("");
    setSuccess("");
    try {
      // Prefer the version from the load that produced the editor content, so a
      // background refresh cannot make this overwrite someone else's newer edit.
      const known =
        loadedVersion?.name === name
          ? loadedVersion.version
          : items.find((item) => item.name === name)?.entity_version;
      const result = await request<Published>("/api/semantic-models/publish", {
        catalog: target.catalog,
        namespace: target.namespace,
        name,
        yaml,
        entity_version: known ?? null,
      });
      setLoadedVersion({ name, version: result.entity_version });
      setSuccess(
        result.action === "created"
          ? `createSemanticModel 成功：${location}.${name} · entity-version ${result.entity_version}`
          : `updateSemanticModel 成功：${location}.${name} · 新 entity-version ${result.entity_version}`,
      );
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  async function load(item: NativeModel) {
    if (busy || editorBusy) return;
    setBusy("load");
    setError("");
    setSuccess("");
    try {
      const loaded = await request<LoadedNative>(
        `/api/semantic-models/load?catalog=${encodeURIComponent(target.catalog)}&namespace=${encodeURIComponent(target.namespace)}&name=${encodeURIComponent(item.name)}`,
      );
      if (onLoad(loaded.yaml, item.model_name || item.name)) {
        setTouched(true);
        setModelName(item.name);
        setLoadedVersion({ name: item.name, version: loaded.entity_version });
        setSuccess(
          `loadSemanticModel 成功：已将 ${item.name}（entity-version ${loaded.entity_version}）载入编辑器。`,
        );
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  async function remove(item: NativeModel) {
    if (
      busy ||
      editorBusy ||
      !window.confirm(
        `确认通过 dropSemanticModel 删除 ${location}.${item.name}？\n该操作只删除 Polaris 中的语义模型记录。`,
      )
    )
      return;
    setBusy("delete");
    setError("");
    setSuccess("");
    try {
      await request("/api/semantic-models/delete", {
        catalog: target.catalog,
        namespace: target.namespace,
        name: item.name,
        entity_version: item.entity_version,
      });
      if (loadedVersion?.name === item.name) setLoadedVersion(null);
      setSuccess(`dropSemanticModel 成功：${item.name} 已从 Polaris 删除。`);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  const disabled = !!busy || editorBusy;
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>
          <Server size={15} /> Polaris 原生语义模型接口（Semantic Model API）
        </h2>
        <div className="button-row">
          <span className="source-badge">Lattice 网关实现 · 5/5 接口可用</span>
          <button
            className="text-button"
            onClick={openConsole}
            disabled={disabled}
          >
            在 API 控制台查看 <ArrowRight size={13} />
          </button>
        </div>
      </div>
      <div className="panel-body">
        <p className="muted">
          Apache Polaris 1.7.0 自带的适配器对 createSemanticModel、
          listSemanticModels、loadSemanticModel、updateSemanticModel 和
          dropSemanticModel 仍返回 HTTP 501。本项目在同源网关中按官方 OpenAPI
          定义实现了这五个接口：校验 Apache Ossie 文档、要求命名空间存在、使用
          entity-version 乐观并发，记录保存在真实 Polaris 的目标命名空间中。
        </p>
        <div className="form-row">
          <label htmlFor="native-catalog">Catalog</label>
          <input
            id="native-catalog"
            value={catalog}
            maxLength={120}
            disabled={disabled}
            onChange={(event) => setCatalog(event.target.value)}
          />
          <label htmlFor="native-namespace">命名空间</label>
          <input
            id="native-namespace"
            value={namespace}
            maxLength={200}
            disabled={disabled}
            placeholder="多级用 . 分隔，如 sales.north"
            onChange={(event) => setNamespace(event.target.value)}
          />
          <button
            className="secondary-button"
            onClick={applyTarget}
            disabled={disabled || listBusy}
          >
            <RefreshCw size={14} className={listBusy ? "spin" : ""} />
            listSemanticModels
          </button>
        </div>
        <div className="form-row">
          <label htmlFor="native-name">原生模型名称</label>
          <input
            id="native-name"
            value={modelName}
            maxLength={200}
            disabled={disabled}
            placeholder="字母、数字、- 和 _"
            onChange={(event) => {
              setTouched(true);
              setModelName(event.target.value);
            }}
          />
          <button
            className="primary-button"
            onClick={() => void publish()}
            disabled={disabled || !yaml.trim() || !modelName.trim()}
          >
            <Send size={14} />
            {busy === "publish"
              ? "提交中…"
              : items.some((item) => item.name === modelName.trim())
                ? "updateSemanticModel（更新）"
                : "createSemanticModel（发布当前 YAML）"}
          </button>
        </div>
        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="success-banner" role="status">
            {success}
          </div>
        )}
        {listError && <ErrorBanner message={listError} />}
        {listBusy ? (
          <Loading text="正在调用 listSemanticModels…" />
        ) : items.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>模型</th>
                  <th>文档内模型名</th>
                  <th>entity-version</th>
                  <th>规范版本</th>
                  <th>更新时间</th>
                  <th>大小</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.name}>
                    <td>
                      <code>{item.name}</code>
                    </td>
                    <td>{item.model_name || "—"}</td>
                    <td>
                      <code title={item.entity_version}>
                        {item.entity_version.length > 20
                          ? item.entity_version.slice(-20)
                          : item.entity_version}
                      </code>
                    </td>
                    <td>{item.document_version}</td>
                    <td>
                      {item.updated_at
                        ? new Date(item.updated_at).toLocaleString("zh-CN")
                        : "—"}
                    </td>
                    <td>{(item.size_bytes / 1024).toFixed(1)} KiB</td>
                    <td>
                      <div className="button-row">
                        <button
                          className="text-button"
                          disabled={disabled}
                          onClick={() => void load(item)}
                        >
                          <FolderOpen size={13} />
                          loadSemanticModel
                        </button>
                        <button
                          className="text-button"
                          disabled={disabled}
                          onClick={() => void remove(item)}
                        >
                          <Trash2 size={13} />
                          dropSemanticModel
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !listError && (
            <Empty title={`${location} 中还没有原生语义模型`}>
              输入原生模型名称后点击“createSemanticModel”即可把当前编辑器中的
              YAML 发布为 Polaris 语义模型。
            </Empty>
          )
        )}
      </div>
    </section>
  );
}
