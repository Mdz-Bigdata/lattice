// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  Code2,
  Layers,
  Play,
  RefreshCw,
  Search,
  Users,
} from "lucide-react";
import {
  request,
  errorMessage,
  formatValue,
  type ApiSpec,
  type Operation,
  type Overview,
} from "./api";
import {
  Empty,
  ErrorBanner,
  Loading,
  ObjectGrid,
  PageTitle,
} from "./components";

export function PolarisOverviewPage({
  identities = false,
  explore,
}: {
  identities?: boolean;
  explore: (search: string) => void;
}) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    setBusy(true);
    setError("");
    try {
      setOverview(await request<Overview>("/api/polaris/overview"));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  const sections = identities
    ? [
        {
          title: "Principals · 身份",
          items: overview?.principals,
          search: "principal",
        },
        {
          title: "Principal Roles · 身份角色",
          items: overview?.principal_roles,
          search: "principal-role",
        },
      ]
    : [
        {
          title: "Catalogs · 数据目录",
          items: overview?.catalogs,
          search: "catalog",
        },
      ];
  return (
    <div className="page-content">
      <PageTitle
        title={identities ? "身份与权限" : "Polaris Catalog"}
        description={
          identities
            ? "管理服务身份、角色分配及 Catalog 权限"
            : "Apache Polaris 开放数据目录 · 使用本地 Polaris 服务的实时数据"
        }
        action={
          <button
            className="secondary-button"
            onClick={() => void load()}
            disabled={busy}
          >
            <RefreshCw size={14} className={busy ? "spin" : ""} />
            刷新
          </button>
        }
      />
      <div className="polaris-intro">
        {identities ? <Users size={27} /> : <Layers size={27} />}
        <div>
          <h2>{identities ? "统一管理访问权限" : "Apache Polaris"}</h2>
          <p>
            {identities
              ? "通过官方 Management API 创建身份、设置角色、分配或撤销授权。"
              : "支持 Iceberg REST Catalog，以及 Namespace、Table、View 和管理 API。"}
          </p>
        </div>
        <button
          className="primary-button"
          onClick={() => explore(identities ? "principal" : "catalog")}
        >
          打开管理 API <ArrowRight size={14} />
        </button>
      </div>
      {error && <ErrorBanner message={error} />}
      {busy && <Loading text="正在连接 Polaris…" />}
      {overview?.errors.map((message, index) => (
        <ErrorBanner key={index} message={formatValue(message)} />
      ))}
      {!busy &&
        sections.map((section) => (
          <section className="panel" key={section.title}>
            <div className="panel-heading">
              <h2>{section.title}</h2>
              <button
                className="text-button"
                onClick={() => explore(section.search)}
              >
                管理操作 <ArrowRight size={13} />
              </button>
            </div>
            {section.items?.length ? (
              <ObjectGrid items={section.items} />
            ) : (
              <Empty
                title={
                  error || overview?.errors.length
                    ? "暂时无法读取数据"
                    : "暂无记录"
                }
              >
                通过管理 API 创建并配置后，刷新查看实时结果。
              </Empty>
            )}
          </section>
        ))}
    </div>
  );
}

export function PolarisExplorer({
  initialSearch = "",
}: {
  initialSearch?: string;
}) {
  const [specs, setSpecs] = useState<ApiSpec[]>([]);
  const [specId, setSpecId] = useState("");
  const [operationId, setOperationId] = useState("");
  const [search, setSearch] = useState(initialSearch);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  async function load() {
    setLoading(true);
    setLoadError("");
    try {
      const result = await request<{ specs: ApiSpec[] }>("/api/polaris/specs");
      setSpecs(result.specs);
      const matching = initialSearch
        ? result.specs.filter((spec) =>
            spec.operations.some((operation) =>
              matches(operation, initialSearch),
            ),
          )
        : result.specs;
      const first =
        matching.find((spec) => spec.id.toLowerCase().includes("management")) ??
        matching[0] ??
        result.specs[0];
      if (first) {
        setSpecId(first.id);
        setOperationId(
          first.operations.find((operation) =>
            matches(operation, initialSearch),
          )?.id ??
            first.operations[0]?.id ??
            "",
        );
      }
    } catch (e) {
      setLoadError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  const spec = specs.find((item) => item.id === specId);
  const operations =
    spec?.operations.filter((operation) => matches(operation, search)) ?? [];
  const operation = spec?.operations.find((item) => item.id === operationId);
  const totalOperations = specs.reduce(
    (sum, item) => sum + item.operations.length,
    0,
  );
  const gatewayOperations = specs.reduce(
    (sum, item) => sum + item.operations.filter(isGatewayImplemented).length,
    0,
  );
  function changeSpec(value: string) {
    setSpecId(value);
    setSearch("");
    setOperationId(
      specs.find((item) => item.id === value)?.operations[0]?.id ?? "",
    );
  }
  function changeSearch(value: string) {
    setSearch(value);
    const filtered =
      spec?.operations.filter((item) => matches(item, value)) ?? [];
    if (!filtered.some((item) => item.id === operationId))
      setOperationId(filtered[0]?.id ?? "");
  }
  return (
    <div className="page-content api-page">
      <PageTitle
        title="Polaris API 控制台"
        description="基于当前集成版本的完整 OpenAPI 定义，直接调用真实 Polaris 服务"
        action={
          <span className="neutral-badge">
            <BookOpen size={13} />
            {totalOperations} 项 API 定义 · 全部可调用
            {gatewayOperations > 0 &&
              ` · ${totalOperations - gatewayOperations} 项由 Polaris 实现 · ${gatewayOperations} 项由 Lattice 网关实现`}
          </span>
        }
      />
      {loading && <Loading text="正在读取官方 API 定义…" />}
      {loadError && (
        <>
          <ErrorBanner message={loadError} />
          <button className="secondary-button" onClick={() => void load()}>
            重试
          </button>
        </>
      )}
      {!loading && !loadError && (
        <div className="api-layout">
          <aside className="api-catalog">
            <label htmlFor="api-spec">API 规范</label>
            <select
              id="api-spec"
              value={specId}
              onChange={(event) => changeSpec(event.target.value)}
            >
              {specs.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.title} · {item.version}
                </option>
              ))}
            </select>
            <div className="api-search">
              <Search size={14} />
              <input
                aria-label="搜索 API 操作"
                placeholder="搜索接口、方法、分组…"
                value={search}
                onChange={(event) => changeSearch(event.target.value)}
              />
            </div>
            <div className="operation-count">{operations.length} 个操作</div>
            <div className="operations-list">
              {operations.map((item) => (
                <button
                  className={`operation-item ${operationId === item.id ? "active" : ""}`}
                  key={item.id}
                  onClick={() => setOperationId(item.id)}
                >
                  <div>
                    <span
                      className={`method-tag method-${item.method.toLowerCase()}`}
                    >
                      {item.method.toUpperCase()}
                    </span>
                    <strong>{item.summary || item.id}</strong>
                  </div>
                  <code>{item.path}</code>
                  {isGatewayImplemented(item) && (
                    <span className="source-badge">Lattice 网关实现</span>
                  )}
                </button>
              ))}
            </div>
            {!operations.length && <Empty title="没有匹配的 API" />}
          </aside>
          <div className="api-detail">
            {spec && operation ? (
              <OperationForm
                key={`${spec.id}:${operation.id}`}
                spec={spec.id}
                operation={operation}
              />
            ) : (
              <Empty title="选择一项 API 操作">
                在左侧选择规范或调整搜索条件。
              </Empty>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
function matches(operation: Operation, search: string) {
  return `${operation.id} ${operation.path} ${operation.method} ${operation.summary} ${operation.tag}`
    .toLowerCase()
    .includes(search.toLowerCase());
}

function isGatewayImplemented(operation: Operation) {
  return operation.implementation === "lattice-gateway";
}

function OperationForm({
  spec,
  operation,
}: {
  spec: string;
  operation: Operation;
}) {
  const visibleParameters = operation.parameters.filter(
    (parameter) =>
      ["path", "query", "header"].includes(parameter.in) &&
      !["authorization", "host", "cookie"].includes(
        parameter.name.toLowerCase(),
      ),
  );
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      visibleParameters.map((parameter) => [
        `${parameter.in}:${parameter.name}`,
        parameter.schema?.default === undefined
          ? ""
          : String(parameter.schema.default),
      ]),
    ),
  );
  const [body, setBody] = useState(
    operation.request_example === null ||
      operation.request_example === undefined
      ? ""
      : JSON.stringify(operation.request_example, null, 2),
  );
  const [response, setResponse] = useState<{
    status: number;
    body: unknown;
    headers?: Record<string, string>;
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const hasBody = !["GET", "HEAD"].includes(operation.method.toUpperCase());
  async function execute() {
    if (busy) return;
    setError("");
    let parsedBody: unknown;
    if (hasBody && body.trim()) {
      try {
        parsedBody = JSON.parse(body);
      } catch {
        setError("请求体不是有效的 JSON，请检查后再执行。");
        return;
      }
    }
    const missing = visibleParameters.filter(
      (parameter) =>
        parameter.required &&
        !values[`${parameter.in}:${parameter.name}`]?.trim(),
    );
    if (missing.length) {
      setError(
        `请填写必填参数：${missing.map((parameter) => parameter.name).join("、")}`,
      );
      return;
    }
    if (
      operation.method.toUpperCase() === "DELETE" &&
      !window.confirm(
        `确认执行删除操作？\n${operation.summary || operation.id}\n${operation.path}\n该操作将修改真实 Polaris 数据。`,
      )
    )
      return;
    const parameters = (location: string) =>
      Object.fromEntries(
        visibleParameters
          .filter(
            (parameter) =>
              parameter.in === location &&
              values[`${parameter.in}:${parameter.name}`] !== "",
          )
          .map((parameter) => [
            parameter.name,
            values[`${parameter.in}:${parameter.name}`],
          ]),
      );
    setBusy(true);
    setResponse(null);
    try {
      setResponse(
        await request("/api/polaris/request", {
          spec,
          operation_id: operation.id,
          path_params: parameters("path"),
          query: parameters("query"),
          headers: parameters("header"),
          ...(parsedBody !== undefined ? { body: parsedBody } : {}),
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <section className="panel">
        <div className="operation-heading">
          <span
            className={`method-tag method-${operation.method.toLowerCase()}`}
          >
            {operation.method.toUpperCase()}
          </span>
          <code>{operation.path}</code>
        </div>
        <div className="operation-description">
          <h2>{operation.summary || operation.id}</h2>
          <p>
            <Code2 size={13} />
            {operation.id}
            {operation.tag && (
              <span className="neutral-badge">{operation.tag}</span>
            )}
          </p>
        </div>
        {isGatewayImplemented(operation) && (
          <div className="info-banner" role="status">
            由 Lattice 网关实现：Apache Polaris 1.7.0 自带的适配器对该接口仍返回
            HTTP 501，本项目按官方 OpenAPI 定义在同源网关中实现了它。请求校验
            Apache Ossie 文档、要求命名空间存在、使用 entity-version
            乐观并发，模型以 Generic Table 记录保存在真实 Polaris
            中；外部客户端也可携带 Polaris 令牌直接调用本服务的同名路径。
          </div>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void execute();
          }}
        >
          <div className="operation-form">
            {visibleParameters.length > 0 && <h3>请求参数</h3>}
            {visibleParameters.map((parameter) => (
              <label
                className="parameter-field"
                key={`${parameter.in}:${parameter.name}`}
              >
                <span>
                  <code>{parameter.name}</code>
                  {parameter.required && <b aria-label="必填">*</b>}
                  <small>
                    {parameter.in} ·{" "}
                    {String(parameter.schema?.type ?? "string")}
                  </small>
                </span>
                {Array.isArray(parameter.schema?.enum) ? (
                  <select
                    value={values[`${parameter.in}:${parameter.name}`]}
                    onChange={(event) =>
                      setValues({
                        ...values,
                        [`${parameter.in}:${parameter.name}`]:
                          event.target.value,
                      })
                    }
                    required={parameter.required}
                  >
                    <option value="">请选择</option>
                    {parameter.schema.enum.map((value) => (
                      <option key={String(value)} value={String(value)}>
                        {String(value)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={values[`${parameter.in}:${parameter.name}`]}
                    onChange={(event) =>
                      setValues({
                        ...values,
                        [`${parameter.in}:${parameter.name}`]:
                          event.target.value,
                      })
                    }
                    required={parameter.required}
                    placeholder={parameter.required ? "必填" : "可选"}
                    autoComplete="off"
                  />
                )}
              </label>
            ))}
            {hasBody && (
              <label className="request-body-label">
                请求体 <span>JSON</span>
                <textarea
                  className="code-editor request-editor"
                  aria-label="API 请求体 JSON"
                  spellCheck={false}
                  value={body}
                  onChange={(event) => setBody(event.target.value)}
                  placeholder="此操作无请求体时留空"
                />
              </label>
            )}
            {operation.request_schema !== undefined && (
              <details className="response-headers">
                <summary>查看官方请求结构</summary>
                <pre>{JSON.stringify(operation.request_schema, null, 2)}</pre>
              </details>
            )}
            {!visibleParameters.length && !hasBody && (
              <p className="muted">此操作无需填写参数。</p>
            )}
          </div>
          <div className="panel-footer">
            <button
              className={
                operation.method.toUpperCase() === "DELETE"
                  ? "danger-button"
                  : "primary-button"
              }
              type="submit"
              disabled={busy}
            >
              <Play size={14} />
              {busy ? "执行中…" : "执行请求"}
            </button>
            <span className="muted">
              {isGatewayImplemented(operation)
                ? "由 Lattice 网关处理，模型记录写入真实 Polaris"
                : "将请求发送到真实 Polaris 服务"}
            </span>
          </div>
        </form>
      </section>
      {error && <ErrorBanner message={error} />}
      {busy && <Loading text="等待 Polaris 响应…" />}
      {response && (
        <section className="panel">
          <div className="panel-heading">
            <h2>响应结果</h2>
            <span
              className={
                response.status < 400 ? "healthy-badge" : "failure-badge"
              }
            >
              HTTP {response.status}
            </span>
          </div>
          <pre className="response-code">
            <code>
              {typeof response.body === "string"
                ? response.body
                : JSON.stringify(response.body, null, 2)}
            </code>
          </pre>
          {response.headers && (
            <details className="response-headers">
              <summary>响应头</summary>
              <pre>{JSON.stringify(response.headers, null, 2)}</pre>
            </details>
          )}
        </section>
      )}
    </>
  );
}
