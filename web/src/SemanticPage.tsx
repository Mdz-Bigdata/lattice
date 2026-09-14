// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import {
  Database,
  Download,
  FolderOpen,
  Layers,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { request, errorMessage } from "./api";
import type { DataPageProps } from "./DataPages";
import { Empty, ErrorBanner, Loading, PageTitle } from "./components";
import NativeModelsPanel from "./NativeModelsPanel";

interface Validation {
  valid: boolean;
  errors: string[];
  warnings: string[];
}
interface SavedModel {
  id: string;
  name: string;
  created_at: string;
  sha256: string;
  size_bytes: number;
  catalog: string;
  namespace: string;
  storage: string;
  yaml?: string;
}
interface ModelList {
  items: SavedModel[];
  warnings?: string[];
  truncated?: boolean;
}

const DRAFT_KEY = "lattice.semantic-model-draft.v1";
let memoryDraft: { name: string; yaml: string } | null = null;

function readDraft(exampleYaml: string) {
  if (memoryDraft) return memoryDraft;
  try {
    const saved = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? "null");
    if (
      saved &&
      typeof saved.name === "string" &&
      typeof saved.yaml === "string"
    ) {
      memoryDraft = { name: saved.name, yaml: saved.yaml };
      return memoryDraft;
    }
  } catch {
    // In-memory persistence still protects drafts when browser storage is blocked.
  }
  memoryDraft = { name: "销售语义模型", yaml: exampleYaml };
  return memoryDraft;
}

function rememberDraft(patch: Partial<{ name: string; yaml: string }>) {
  memoryDraft = { name: "销售语义模型", yaml: "", ...memoryDraft, ...patch };
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(memoryDraft));
  } catch {
    /* Keep the in-memory draft across page unmounts when storage is full. */
  }
}

export default function SemanticPage({ bootstrap, explore }: DataPageProps) {
  const [initialDraft] = useState(() => readDraft(bootstrap.model_yaml));
  const [yaml, setYamlState] = useState(initialDraft.yaml);
  const [name, setNameState] = useState(initialDraft.name);
  function setYaml(value: string) {
    rememberDraft({ yaml: value });
    setYamlState(value);
  }
  function setName(value: string) {
    rememberDraft({ name: value });
    setNameState(value);
  }
  const [baseline, setBaseline] = useState({
    yaml: bootstrap.model_yaml,
    name: "销售语义模型",
  });
  const [loadedId, setLoadedId] = useState("");
  const [validation, setValidation] = useState<Validation | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [models, setModels] = useState<SavedModel[]>([]);
  const [listBusy, setListBusy] = useState(false);
  const [listError, setListError] = useState("");
  const [listNotice, setListNotice] = useState("");
  const listRequest = useRef(0);
  const dirty = yaml !== baseline.yaml || name !== baseline.name;

  async function refreshModels() {
    const requestId = ++listRequest.current;
    setListBusy(true);
    setListError("");
    try {
      const data = await request<ModelList>("/api/models");
      if (requestId !== listRequest.current) return;
      setModels(data.items);
      setListNotice(
        [
          ...(data.warnings ?? []),
          ...(data.truncated
            ? ["列表已达到本地展示上限，仅显示部分模型记录。"]
            : []),
        ].join(" "),
      );
    } catch (e) {
      if (requestId === listRequest.current) setListError(errorMessage(e));
    } finally {
      if (requestId === listRequest.current) setListBusy(false);
    }
  }
  useEffect(() => {
    void refreshModels();
  }, []);

  async function validate() {
    setBusy("validate");
    setError("");
    setSuccess("");
    setValidation(null);
    try {
      setValidation(await request<Validation>("/api/validate", { yaml }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function save() {
    if (busy || !name.trim() || !yaml.trim()) return;
    setBusy("save");
    setError("");
    setSuccess("");
    setValidation(null);
    try {
      const checked = await request<Validation>("/api/validate", { yaml });
      setValidation(checked);
      if (!checked.valid) return;
      const model = await request<SavedModel>("/api/models", {
        name: name.trim(),
        yaml,
      });
      if (typeof model.yaml !== "string")
        throw new Error(
          "模型已提交，但服务器读回内容缺失。请刷新模型列表确认保存结果。",
        );
      setYaml(model.yaml);
      setName(model.name);
      setBaseline({ yaml: model.yaml, name: model.name });
      setLoadedId(model.id);
      setSuccess(
        `已保存并从真实 Polaris 读回验证：${model.name} · 版本 ${model.id.slice(-12)}。每次保存均创建独立版本。`,
      );
      await refreshModels();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function load(model: SavedModel) {
    if (
      busy ||
      (dirty &&
        !window.confirm(
          "当前编辑内容尚未保存，确认加载所选版本并替换编辑器内容？",
        ))
    )
      return;
    setBusy("load");
    setError("");
    setSuccess("");
    try {
      const saved = await request<SavedModel>(
        `/api/models/${encodeURIComponent(model.id)}`,
      );
      if (typeof saved.yaml !== "string")
        throw new Error("服务器返回的模型内容无效。");
      setYaml(saved.yaml);
      setName(saved.name);
      setBaseline({ yaml: saved.yaml, name: saved.name });
      setLoadedId(saved.id);
      setValidation(null);
      setSuccess(
        `已从 Polaris 加载 ${saved.name} · 版本 ${saved.id.slice(-12)}。`,
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function remove(model: SavedModel) {
    if (
      busy ||
      !window.confirm(
        `确认删除 Polaris 中的模型版本？\n${model.name}\n版本：${model.id.slice(-12)}\n此操作仅删除该扩展模型记录，不删除业务数据。`,
      )
    )
      return;
    setBusy("delete");
    setError("");
    setSuccess("");
    try {
      await request(`/api/models/${encodeURIComponent(model.id)}/delete`, {
        sha256: model.sha256,
      });
      if (loadedId === model.id) setLoadedId("");
      setSuccess(
        `已从 Polaris 删除 ${model.name} 的所选版本；编辑器内容保留。`,
      );
      await refreshModels();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  function reset() {
    if (
      dirty &&
      !window.confirm("确认放弃当前未保存的编辑，恢复本地示例模型？")
    )
      return;
    setYaml(bootstrap.model_yaml);
    setName("销售语义模型");
    setBaseline({ yaml: bootstrap.model_yaml, name: "销售语义模型" });
    setLoadedId("");
    setValidation(null);
    setError("");
    setSuccess("");
  }
  function download() {
    const url = URL.createObjectURL(
      new Blob([yaml], { type: "application/yaml" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "lattice-semantic-model.yaml";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="page-content">
      <PageTitle
        title="语义模型"
        description="Lattice 扩展 · 使用真实 Polaris Generic Table 保存 YAML，每次保存创建独立版本"
        action={
          <div className="button-row">
            <button
              className="secondary-button"
              onClick={reset}
              disabled={!!busy}
            >
              <RefreshCw size={14} />
              恢复示例
            </button>
            <button className="secondary-button" onClick={download}>
              <Download size={14} />
              导出 YAML
            </button>
            <button
              className="secondary-button"
              onClick={() => void validate()}
              disabled={!!busy}
            >
              <ShieldCheck size={14} />
              {busy === "validate" ? "校验中…" : "校验模型"}
            </button>
          </div>
        }
      />
      <div className="info-banner">
        <Layers size={16} />
        <span>
          Polaris 原生语义模型接口（Semantic Model API）的 5 个操作已由 Lattice
          网关实现，可在下方直接发布、加载和删除；本页的“版本存档”保存功能由
          Lattice 扩展提供，数据同样存储在真实 Polaris 的 lattice / demo 中。
        </span>
      </div>
      <section className="panel">
        <div className="panel-body">
          <div className="form-row">
            <label htmlFor="model-name">模型名称</label>
            <input
              id="model-name"
              value={name}
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              disabled={!!busy}
              placeholder="为这个模型版本命名"
            />
            <button
              className="primary-button"
              onClick={() => void save()}
              disabled={!!busy || !name.trim() || !yaml.trim()}
            >
              <Save size={14} />
              {busy === "save" ? "校验并保存中…" : "校验并保存新版本"}
            </button>
            <span className="neutral-badge">
              <Database size={12} />
              Polaris Generic Table
            </span>
          </div>
          <p className="muted">
            最多 256 KiB UTF-8 · 保存不会覆盖已有版本。
            {loadedId
              ? ` 当前加载版本：${loadedId.slice(-12)}。`
              : " 当前为本地编辑内容。"}
            {dirty && " 有未保存的修改。"}
          </p>
        </div>
      </section>
      {error && <ErrorBanner message={error} />}
      {success && (
        <div className="success-banner" role="status">
          {success}
        </div>
      )}
      {validation && (
        <div
          className={validation.valid ? "success-banner" : "error-banner"}
          role="status"
        >
          <div>
            <strong>
              {validation.valid ? "模型校验通过" : "模型校验未通过"}
            </strong>
            {validation.errors.map((message, index) => (
              <p key={index}>{message}</p>
            ))}
            {validation.warnings.map((message, index) => (
              <p key={index}>提示：{message}</p>
            ))}
          </div>
        </div>
      )}
      <textarea
        className="code-editor semantic-editor"
        spellCheck={false}
        aria-label="语义模型 YAML"
        value={yaml}
        disabled={!!busy}
        onChange={(event) => {
          setYaml(event.target.value);
          setValidation(null);
          setSuccess("");
        }}
      />
      <NativeModelsPanel
        yaml={yaml}
        draftName={name}
        editorBusy={!!busy}
        explore={explore}
        onLoad={(loadedYaml, loadedName) => {
          if (
            dirty &&
            !window.confirm(
              "当前编辑内容尚未保存，确认加载所选原生模型并替换编辑器内容？",
            )
          )
            return false;
          setYaml(loadedYaml);
          setName(loadedName);
          setBaseline({ yaml: loadedYaml, name: loadedName });
          setLoadedId("");
          setValidation(null);
          setError("");
          setSuccess("");
          return true;
        }}
      />
      <section className="panel">
        <div className="panel-heading">
          <h2>已保存到 Polaris 的模型版本（Lattice 扩展存档）</h2>
          <button
            className="text-button"
            onClick={() => void refreshModels()}
            disabled={listBusy || !!busy}
          >
            <RefreshCw size={13} className={listBusy ? "spin" : ""} />
            刷新
          </button>
        </div>
        {listError && <ErrorBanner message={listError} />}
        {listNotice && <div className="info-banner">{listNotice}</div>}
        {listBusy ? (
          <Loading text="正在读取 Polaris 模型记录…" />
        ) : models.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>模型名称</th>
                  <th>独立版本</th>
                  <th>保存时间</th>
                  <th>YAML 大小</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {models.map((model) => (
                  <tr key={model.id}>
                    <td>
                      {model.name}
                      {loadedId === model.id && (
                        <span className="source-badge">当前加载</span>
                      )}
                    </td>
                    <td>
                      <code title={model.id}>{model.id.slice(-12)}</code>
                    </td>
                    <td>
                      {new Date(model.created_at).toLocaleString("zh-CN")}
                    </td>
                    <td>{(model.size_bytes / 1024).toFixed(1)} KiB</td>
                    <td>
                      <div className="button-row">
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void load(model)}
                        >
                          <FolderOpen size={13} />
                          加载
                        </button>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => void remove(model)}
                        >
                          <Trash2 size={13} />
                          删除此版本
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
            <Empty title="还没有保存的模型版本">
              填写模型名称后，使用“校验并保存新版本”将 YAML 保存到真实 Polaris。
            </Empty>
          )
        )}
      </section>
    </div>
  );
}
