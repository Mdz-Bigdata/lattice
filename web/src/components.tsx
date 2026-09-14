// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Database,
  LoaderCircle,
  X,
} from "lucide-react";
import { formatValue } from "./api";

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="error-banner" role="alert">
      <AlertCircle size={17} />
      <span>{message}</span>
    </div>
  );
}
export function Loading({ text = "正在加载数据…" }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={19} />
      {text}
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Database size={29} />
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function PageTitle({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {action}
    </div>
  );
}
/** Right-hand modal drawer with Escape to close, focus trap and focus restore. */
export function Drawer({
  id,
  title,
  icon,
  onClose,
  children,
  wide = false,
  closeOnBackdrop = true,
}: {
  id: string;
  title: string;
  icon?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
  closeOnBackdrop?: boolean;
}) {
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  });
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = document.querySelectorAll<HTMLElement>(
        `#${id} button:not(:disabled), #${id} input:not(:disabled), #${id} select:not(:disabled), #${id} textarea:not(:disabled), #${id} a[href]`,
      );
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, [id]);
  return (
    <div
      className="drawer-backdrop"
      onClick={closeOnBackdrop ? onClose : undefined}
    >
      <aside
        id={id}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`history-drawer ${wide ? "form-drawer" : ""}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="drawer-header">
          <h2>
            {icon}
            {title}
          </h2>
          <button
            className="icon-button"
            aria-label={`关闭${title}`}
            onClick={onClose}
            autoFocus
          >
            <X size={19} />
          </button>
        </div>
        {children}
      </aside>
    </div>
  );
}
export function DataGrid({
  columns,
  rows,
}: {
  columns: string[];
  rows: unknown[][];
}) {
  const [page, setPage] = useState(0);
  const pageSize = 25;
  const lastPage = Math.max(0, Math.ceil(rows.length / pageSize) - 1);
  const currentPage = Math.min(page, lastPage);
  return (
    <div className="data-grid">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((column, i) => (
                <th key={`${column}-${i}`}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows
              .slice(currentPage * pageSize, (currentPage + 1) * pageSize)
              .map((row, i) => (
                <tr key={i}>
                  {columns.map((_, j) => (
                    <td key={j} title={formatValue(row[j])}>
                      {formatValue(row[j])}
                    </td>
                  ))}
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && (
        <Empty title="暂无记录">当前查询未返回数据。</Empty>
      )}
      <div className="table-footer">
        <span>共 {rows.length.toLocaleString()} 条记录</span>
        <div className="pagination">
          <button
            className="icon-button"
            aria-label="上一页"
            disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <span>
            {currentPage + 1} / {lastPage + 1}
          </span>
          <button
            className="icon-button"
            aria-label="下一页"
            disabled={currentPage >= lastPage}
            onClick={() => setPage(currentPage + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
export function ObjectGrid({ items }: { items: Record<string, unknown>[] }) {
  const columns = [...new Set(items.flatMap((item) => Object.keys(item)))];
  return (
    <DataGrid
      columns={columns}
      rows={items.map((item) => columns.map((column) => item[column]))}
    />
  );
}
