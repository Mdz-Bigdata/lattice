// SPDX-License-Identifier: Apache-2.0
export interface DataTable {
  name: string;
  label: string;
  columns: { name: string; type: string }[];
  rows: number;
}
export interface SourceField {
  name: string;
  label: string;
  type: "text" | "number" | "password" | "select" | "checkbox" | "textarea";
  required: boolean;
  default?: string | number | boolean;
  placeholder?: string;
  help?: string;
  options?: { value: string; label: string }[];
}
export interface SourceType {
  id: string;
  label: string;
  category: string;
  description: string;
  driver: string;
  dialect: string;
  default_port?: number;
  fields: SourceField[];
  local_demo?: boolean;
}
export interface TestResult {
  ok: boolean;
  status: "online" | "offline" | "error";
  latency_ms: number;
  server_version?: string;
  detail: string;
  tested_at: string;
  table_count?: number;
}
export interface DataSource {
  id: string;
  name: string;
  type: string;
  type_label: string;
  category: string;
  dialect: string;
  builtin: boolean;
  description?: string;
  config: Record<string, unknown>;
  summary: string;
  created_at: string;
  updated_at: string;
  last_test?: TestResult | null;
}
export interface SchemaInfo {
  name: string;
  table_count?: number | null;
}
export interface TableInfo {
  schema: string;
  name: string;
  kind: string;
  rows?: number | null;
  comment?: string;
}
export interface ColumnInfo {
  name: string;
  type: string;
  nullable?: boolean;
  comment?: string;
  primary_key?: boolean;
}
export interface TableDetail {
  schema: string;
  name: string;
  kind: string;
  columns: ColumnInfo[];
  rows?: number | null;
  properties?: Record<string, string>;
}
export interface LlmStatus {
  configured: boolean;
  provider: string;
  model: string;
  base_url?: string;
  api_key_masked?: string;
  detail: string;
}
export interface LlmTestResult {
  ok: boolean;
  detail: string;
  latency_ms: number;
  model: string;
}
export interface IngestTable {
  catalog: string;
  namespace: string[];
  name: string;
  kind: "iceberg" | "generic";
  format?: string;
  source_datasource?: string;
  source_table?: string;
  base_location?: string;
  properties?: Record<string, string>;
}
export interface IngestCatalog {
  catalog: string;
  namespaces: string[][];
  tables: IngestTable[];
}
export interface BootstrapSource {
  id: string;
  name: string;
  type: string;
  type_label: string;
  status: string;
}
export interface Bootstrap {
  tables: DataTable[];
  example_questions: string[];
  model_yaml: string;
  polaris: Record<string, unknown>;
  capabilities: string[];
  /* Added by the multi-datasource backend; optional so an older API still renders. */
  datasources?: { count: number; online: number; items: BootstrapSource[] };
  llm?: LlmStatus;
  links?: { webapi: string; polaris: string; polaris_health: string };
}
export interface QueryResult {
  id: string;
  title: string;
  sql: string;
  columns: string[];
  rows: unknown[][];
  chart: { dimension: string; metric: string; type: string };
  steps: string[];
  source: string;
  elapsed_ms: number;
  provider: string;
  truncated?: boolean;
  created_at: string;
  datasource_id: string;
  datasource_name: string;
  dialect: string;
}
export interface QueryRequest {
  question?: string;
  sql?: string;
  datasource_id?: string;
}
export interface Health {
  status: string;
  /** Digest of the built entry document; a change means this tab is stale. */
  build?: string;
  services: { polaris: { status: string; version?: string; detail?: string } };
  demo: boolean;
}
export interface Operation {
  id: string;
  method: string;
  path: string;
  summary: string;
  tag: string;
  /** "upstream" when real Polaris serves it, "lattice-gateway" when this project implements it. */
  implementation?: string;
  parameters: {
    name: string;
    in: string;
    required: boolean;
    schema: Record<string, unknown>;
  }[];
  request_example: unknown;
  request_schema?: unknown;
}
export interface ApiSpec {
  id: string;
  title: string;
  version: string;
  operations: Operation[];
}
export interface Overview {
  catalogs: Record<string, unknown>[];
  principals: Record<string, unknown>[];
  principal_roles: Record<string, unknown>[];
  status: string;
  errors: unknown[];
}
/** Literal returned by the backend in place of stored secrets; sending it back keeps the secret. */
export const SECRET_MASK = "••••••";
/** Builtin DuckDB sample database that is always registered. */
export const LOCAL_SAMPLE_ID = "local-sample";

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
export async function request<T>(path: string, body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let data: unknown;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(
        `服务返回了非 JSON 内容（HTTP ${response.status}），请检查后端服务。`,
      );
    }
    if (!response.ok) {
      const detail = (data as { detail?: unknown }).detail;
      throw new Error(
        typeof detail === "string" ? detail : JSON.stringify(detail ?? data),
      );
    }
    return data as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请检查服务状态后重试。");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
export function quoteIdentifier(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}
const backtickDialects = new Set([
  "mysql",
  "starrocks",
  "doris",
  "hive",
  "clickhouse",
]);
/** Quotes an identifier for the SQL dialect of a data source (backticks vs double quotes). */
export function quoteIdent(dialect: string, name: string): string {
  return backtickDialects.has(dialect.toLowerCase())
    ? `\`${name.replaceAll("`", "``")}\``
    : `"${name.replaceAll('"', '""')}"`;
}
/** Fully qualified table reference; DuckDB tables in `main` keep the unqualified form. */
export function tableReference(
  source: Pick<DataSource, "type" | "dialect"> | undefined,
  schema: string,
  name: string,
): string {
  const dialect = source?.dialect ?? "duckdb";
  const type = source?.type ?? "duckdb";
  if (!schema || (type === "duckdb" && schema === "main"))
    return quoteIdent(dialect, name);
  return `${quoteIdent(dialect, schema)}.${quoteIdent(dialect, name)}`;
}
export function previewSql(
  source: Pick<DataSource, "type" | "dialect"> | undefined,
  schema: string,
  name: string,
): string {
  return `SELECT * FROM ${tableReference(source, schema, name)} LIMIT 100;`;
}
export function datasourcePath(id: string, suffix = ""): string {
  return `/api/datasources/${encodeURIComponent(id)}${suffix}`;
}

/** Thrown when the SSE endpoint cannot be used; callers fall back to POST /api/query. */
export class StreamUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StreamUnavailableError";
  }
}
export interface StreamHandlers {
  onStep?: (text: string) => void;
  onSql?: (sql: string, title: string) => void;
  onResult?: (result: QueryResult) => void;
  onError?: (detail: string) => void;
  onDone?: () => void;
}
function dispatchFrame(frame: string, handlers: StreamHandlers): boolean {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  let payload: Record<string, unknown> = {};
  if (data.length) {
    try {
      const parsed: unknown = JSON.parse(data.join("\n"));
      if (parsed && typeof parsed === "object")
        payload = parsed as Record<string, unknown>;
    } catch {
      if (event === "error") handlers.onError?.("流式响应格式错误。");
      return false;
    }
  }
  switch (event) {
    case "step":
      handlers.onStep?.(String(payload.text ?? ""));
      return false;
    case "sql":
      handlers.onSql?.(String(payload.sql ?? ""), String(payload.title ?? ""));
      return false;
    case "result":
      handlers.onResult?.(payload as unknown as QueryResult);
      return false;
    case "error":
      handlers.onError?.(
        typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload),
      );
      return false;
    case "done":
      handlers.onDone?.();
      return true;
    default:
      return false;
  }
}
/**
 * POSTs to /api/query/stream and dispatches SSE events (step / sql / result / error / done).
 * Frames may be split across chunks; the buffer is only flushed on blank-line boundaries.
 */
export async function streamQuery(
  body: QueryRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/query/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new StreamUnavailableError(
      `无法连接流式查询接口：${errorMessage(error)}`,
    );
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (
    !response.ok ||
    !contentType.includes("text/event-stream") ||
    !response.body
  ) {
    let detail = `流式接口不可用（HTTP ${response.status}）`;
    try {
      const text = await response.text();
      const parsed = text ? (JSON.parse(text) as { detail?: unknown }) : {};
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* keep the HTTP status message */
    }
    throw new StreamUnavailableError(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let received = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder
        .decode(value, { stream: true })
        .replaceAll("\r\n", "\n");
      buffer = buffer.replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        received = true;
        if (dispatchFrame(frame, handlers)) {
          await reader.cancel().catch(() => undefined);
          return;
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatchFrame(buffer, handlers);
  } catch (error) {
    if (signal?.aborted) throw error;
    const message = `流式连接中断：${errorMessage(error)}`;
    if (!received) throw new StreamUnavailableError(message);
    throw new Error(message);
  }
}
