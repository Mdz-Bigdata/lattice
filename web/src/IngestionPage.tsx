// SPDX-License-Identifier: Apache-2.0
import { useState } from "react";
import {
  ArrowRight,
  Layers,
  RefreshCw,
  Table2,
  Trash2,
  Upload,
} from "lucide-react";
import {
  request,
  errorMessage,
  quoteIdent,
  tableReference,
  type IngestCatalog,
  type IngestTable,
} from "./api";
import { Empty, ErrorBanner, Loading, PageTitle } from "./components";
import { SchemaTablePicker, SourceSelect, useApi } from "./SourceBrowser";
import type { DataPageProps } from "./DataPages";

const CATALOG = "lattice";
const ICEBERG_SOURCE = "iceberg-local";

export default function IngestionPage({
  sources,
  sourcesError,
  goTo,
  sourceId,
}: DataPageProps) {
  const candidates = sources.filter((source) => source.type !== "iceberg");
  const [registerSourceId, setRegisterSourceId] = useState(() =>
    candidates.some((source) => source.id === sourceId)
      ? sourceId
      : (candidates[0]?.id ?? ""),
  );
  const source = candidates.find((item) => item.id === registerSourceId);
  const [schema, setSchema] = useState("");
  const [table, setTable] = useState("");
  const [namespace, setNamespace] = useState("");
  const [tableName, setTableName] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [registered, setRegistered] = useState<IngestTable | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const catalog = useApi<IngestCatalog>(
    `/api/ingest/catalog?catalog=${encodeURIComponent(CATALOG)}`,
  );

  async function register() {
    if (!source || !schema || !table) return;
    setBusy("register");
    setError("");
    setRegistered(null);
    try {
      const result = await request<IngestTable>("/api/ingest/register", {
        datasource_id: source.id,
        schema,
        name: table,
        catalog: CATALOG,
        ...(namespace.trim() ? { namespace: namespace.trim() } : {}),
        ...(tableName.trim() ? { table_name: tableName.trim() } : {}),
      });
      setRegistered(result);
      catalog.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  async function unregister(item: IngestTable) {
    const key = tableKey(item);
    if (
      !window.confirm(
        `确认取消注册 ${key}？\n仅从 Polaris Catalog 移除该 Generic Table 记录，不会删除源数据。`,
      )
    )
      return;
    setBusy(`unregister:${key}`);
    setCatalogError("");
    try {
      await request("/api/ingest/unregister", {
        catalog: item.catalog,
        namespace: item.namespace.join("."),
        name: item.name,
      });
      catalog.reload();
    } catch (e) {
      setCatalogError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }
  /** A Generic Table only records where the data lives; the rows stay in the
   *  origin engine.  Querying one therefore targets that data source, not the
   *  Iceberg catalog.  Null when the source is gone (removed or hidden) or the
   *  entry carries no source table, which leaves the row without a query link. */
  function origin(item: IngestTable) {
    if (item.kind !== "generic" || !item.source_datasource) return null;
    const source = sources.find((entry) => entry.id === item.source_datasource);
    const name = item.properties?.["lattice.source-table"] ?? item.source_table;
    if (!source || !name) return null;
    const schema = item.properties?.["lattice.source-schema"] ?? "";
    return { source, schema, name };
  }
  function queryOrigin(item: IngestTable) {
    const target = origin(item);
    if (!target) return;
    goTo(
      "sql",
      `SELECT * FROM ${tableReference(target.source, target.schema, target.name)} LIMIT 100;`,
      target.source.id,
    );
  }
  function querySql(item: IngestTable) {
    goTo(
      "sql",
      `SELECT * FROM ${quoteIdent("duckdb", item.namespace.join("."))}.${quoteIdent("duckdb", item.name)} LIMIT 100;`,
      ICEBERG_SOURCE,
    );
  }
  return (
    <div className="page-content">
      <PageTitle
        title="数据接入"
        description="将外部数据表登记到 Apache Polaris Catalog，统一发现与查询"
      />
      <div className="info-banner">
        <Layers size={16} />
        <span>
          Generic Table 将外部数据源中的表登记到 Polaris
          Catalog，让它们可以被统一发现（数据仍由原引擎存储）；Iceberg 表则通过
          Iceberg REST Catalog 由内置数据源「{ICEBERG_SOURCE}」直接查询。
        </span>
      </div>
      {sourcesError && (
        <ErrorBanner message={`数据源列表不可用：${sourcesError}`} />
      )}
      <section className="panel">
        <div className="panel-heading">
          <h2>注册外部表到 Polaris Catalog</h2>
          <span>Catalog {CATALOG}</span>
        </div>
        <div className="panel-body">
          <div className="form-row picker-row">
            <SourceSelect
              id="ingest-source"
              sources={candidates}
              value={registerSourceId}
              onChange={(id) => {
                setRegisterSourceId(id);
                setSchema("");
                setTable("");
                setRegistered(null);
              }}
            />
            <SchemaTablePicker
              idPrefix="ingest"
              source={source}
              schema={schema}
              table={table}
              onSchema={(value) => {
                setSchema(value);
                setTable("");
              }}
              onTable={setTable}
            />
          </div>
          <div className="form-row picker-row">
            <label htmlFor="ingest-namespace">目标 Namespace</label>
            <input
              id="ingest-namespace"
              value={namespace}
              placeholder="留空使用默认 namespace"
              onChange={(event) => setNamespace(event.target.value)}
            />
            <label htmlFor="ingest-table-name">目标表名</label>
            <input
              id="ingest-table-name"
              value={tableName}
              placeholder={table || "默认与源表同名"}
              onChange={(event) => setTableName(event.target.value)}
            />
            <button
              className="primary-button"
              disabled={!source || !schema || !table || busy === "register"}
              onClick={() => void register()}
            >
              <Upload size={14} />
              {busy === "register" ? "注册中…" : "注册"}
            </button>
          </div>
          <p className="muted">
            注册会创建 Polaris Generic
            Table，并记录来源数据源、Schema、表名与字段定义（lattice.* 属性）。
          </p>
        </div>
        {error && <ErrorBanner message={error} />}
        {registered && (
          <div className="success-banner" role="status">
            已注册：{registered.catalog} / {registered.namespace.join(".")} /{" "}
            {registered.name}（{registered.kind}
            {registered.format ? ` · ${registered.format}` : ""}）
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Polaris Catalog 中的表</h2>
          <button
            className="text-button"
            onClick={catalog.reload}
            disabled={catalog.loading}
          >
            <RefreshCw size={13} className={catalog.loading ? "spin" : ""} />
            刷新
          </button>
        </div>
        {catalog.error && <ErrorBanner message={catalog.error} />}
        {catalogError && <ErrorBanner message={catalogError} />}
        {catalog.loading && <Loading text="正在读取 Polaris Catalog…" />}
        {catalog.data && (
          <>
            <div className="namespace-row">
              <span>Namespace</span>
              {catalog.data.namespaces.length ? (
                catalog.data.namespaces.map((item) => (
                  <code key={item.join(".")}>{item.join(".")}</code>
                ))
              ) : (
                <span className="muted">暂无</span>
              )}
            </div>
            {catalog.data.tables.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Namespace</th>
                      <th>表名</th>
                      <th>类型</th>
                      <th>格式</th>
                      <th>来源</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {catalog.data.tables.map((item) => {
                      const key = tableKey(item);
                      const removing = busy === `unregister:${key}`;
                      return (
                        <tr key={key}>
                          <td>
                            <code>{item.namespace.join(".")}</code>
                          </td>
                          <td>
                            <span className="table-name">
                              <Table2 size={14} />
                              {item.name}
                            </span>
                          </td>
                          <td>
                            <span
                              className={
                                item.kind === "iceberg"
                                  ? "source-badge"
                                  : "neutral-badge"
                              }
                            >
                              {item.kind === "iceberg" ? "Iceberg" : "Generic"}
                            </span>
                          </td>
                          <td>{item.format ?? "—"}</td>
                          <td title={item.base_location}>
                            {item.source_datasource
                              ? `${item.source_datasource}${item.source_table ? ` · ${item.source_table}` : ""}`
                              : (item.base_location ?? "—")}
                          </td>
                          <td>
                            <div className="ingest-actions">
                              {item.kind === "iceberg" ? (
                                <button
                                  className="text-button"
                                  onClick={() => querySql(item)}
                                >
                                  在 SQL 工作台查询 <ArrowRight size={12} />
                                </button>
                              ) : (
                                <>
                                  {origin(item) && (
                                    <button
                                      className="text-button"
                                      onClick={() => queryOrigin(item)}
                                    >
                                      在 SQL 工作台查询 <ArrowRight size={12} />
                                    </button>
                                  )}
                                  <button
                                    className="text-button"
                                    disabled={removing}
                                    onClick={() => void unregister(item)}
                                  >
                                    <Trash2 size={12} />
                                    {removing ? "处理中…" : "取消注册"}
                                  </button>
                                </>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="Catalog 中还没有表">
                注册外部表或通过 Iceberg 写入后，刷新查看。
              </Empty>
            )}
          </>
        )}
      </section>
    </div>
  );
}
function tableKey(item: IngestTable) {
  return `${item.namespace.join(".")}.${item.name}`;
}
