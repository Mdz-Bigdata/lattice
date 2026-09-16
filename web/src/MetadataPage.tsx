// SPDX-License-Identifier: Apache-2.0
/**
 * 元数据管理: the OpenMetadata-style catalog, lineage and context layer of the platform.
 *
 * One page with its own second-level menu, like the product screens: assets (explore), lineage,
 * observability (alerts, notifications, activity), insights, domains (元数据工作区), governance
 * (glossaries, classifications, custom properties), the AI context layer and ingestion settings
 * (元数据拾取). The sub-route lives in the URL hash after `#metadata/`, so the browser's back
 * button, a reload and a copied link all keep the screen; switching away and back to this tab
 * restores the last sub-route.
 */
import { useEffect, useState } from "react";
import {
  Bell,
  Bot,
  ChevronDown,
  ChevronRight,
  Globe,
  Lightbulb,
  Network,
  Plug,
  Search,
  Tags,
  type LucideIcon,
} from "lucide-react";
import type { DataPageProps } from "./DataPages";
import { useApi } from "./SourceBrowser";
import {
  METADATA_PAGE,
  metadataPath,
  navigate,
  parseRoute,
  routeHash,
  type MetadataHealth,
  type MetadataRoute,
} from "./metadataApi";
import ExplorePage from "./MetadataExplore";
import EntityPage from "./MetadataEntity";
import { LineagePage } from "./MetadataLineage";
import InsightsPage from "./MetadataInsights";
import {
  ClassificationPage,
  DomainsPage,
  GlossaryPage,
  PropertiesPage,
} from "./MetadataGovernance";
import SettingsPage from "./MetadataSettings";
import AlertsPage from "./MetadataAlerts";
import ContextPage from "./MetadataContext";
import "./metadata.css";

export interface MetadataViewProps extends DataPageProps {
  route: MetadataRoute;
}

interface MenuItem {
  view: string;
  label: string;
  icon: LucideIcon;
  children?: { view: string; label: string }[];
  /** Views that also light this entry up (an entity page belongs to 元数据资产). */
  also?: string[];
}

const MENU: MenuItem[] = [
  { view: "explore", label: "元数据资产", icon: Search, also: ["entity"] },
  { view: "lineage", label: "数据血缘", icon: Network },
  {
    view: "alerts",
    label: "元数据检测",
    icon: Bell,
    children: [
      { view: "alerts", label: "告警订阅" },
      { view: "notifications", label: "通知中心" },
      { view: "activity", label: "活动信息流" },
    ],
  },
  { view: "insights", label: "元数据洞察", icon: Lightbulb },
  { view: "domains", label: "元数据工作区", icon: Globe },
  {
    view: "glossary",
    label: "元数据系统",
    icon: Tags,
    children: [
      { view: "glossary", label: "术语库" },
      { view: "classification", label: "分类" },
      { view: "properties", label: "自定义属性" },
    ],
  },
  { view: "context", label: "AI 上下文", icon: Bot },
  { view: "settings", label: "元数据拾取", icon: Plug },
];

let lastRoute: MetadataRoute | null = null;

function currentRoute(): MetadataRoute {
  const head = window.location.hash.slice(1).split("/")[0];
  if (head === METADATA_PAGE && window.location.hash.includes("/")) return parseRoute();
  return lastRoute ?? { view: "explore", parts: [] };
}

function belongs(item: MenuItem, view: string): boolean {
  return (
    item.view === view ||
    (item.also ?? []).includes(view) ||
    (item.children ?? []).some((child) => child.view === view)
  );
}

export default function MetadataPage(props: DataPageProps) {
  const [route, setRoute] = useState<MetadataRoute>(currentRoute);
  const [open, setOpen] = useState<Record<string, boolean>>({ alerts: true, glossary: true });
  const health = useApi<MetadataHealth>(metadataPath("/health"));

  useEffect(() => {
    // Returning to the tab restores the last screen without adding a history entry.
    if (lastRoute && window.location.hash === `#${METADATA_PAGE}`) {
      window.history.replaceState(null, "", `#${routeHash(lastRoute.view, ...lastRoute.parts)}`);
    }
    const onHash = () => {
      const head = window.location.hash.slice(1).split("/")[0];
      if (head !== METADATA_PAGE) return;
      const next = window.location.hash.includes("/") ? parseRoute() : { view: "explore", parts: [] };
      lastRoute = next;
      setRoute(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    lastRoute = route;
  }, [route]);

  const view = route.view;
  const shared: MetadataViewProps = { ...props, route };
  let content;
  switch (view) {
    case "entity":
      content = <EntityPage key={`${route.parts[0]}:${route.parts[1]}`} {...shared} />;
      break;
    case "lineage":
      content = <LineagePage {...shared} />;
      break;
    case "alerts":
    case "notifications":
    case "activity":
      content = <AlertsPage {...shared} />;
      break;
    case "insights":
      content = <InsightsPage {...shared} />;
      break;
    case "domains":
      content = <DomainsPage {...shared} />;
      break;
    case "glossary":
      content = <GlossaryPage {...shared} />;
      break;
    case "classification":
      content = <ClassificationPage {...shared} />;
      break;
    case "properties":
      content = <PropertiesPage {...shared} />;
      break;
    case "context":
      content = <ContextPage {...shared} />;
      break;
    case "settings":
      content = <SettingsPage {...shared} />;
      break;
    default:
      content = <ExplorePage {...shared} />;
  }
  const store = health.data;
  return (
    <div className="md-shell">
      <aside className="md-menu" aria-label="元数据管理菜单">
        {MENU.map((item) => {
          const active = belongs(item, view);
          const expanded = item.children ? open[item.view] !== false : false;
          return (
            <div key={item.label} className="md-menu-group">
              <button
                type="button"
                className={`md-menu-item ${active && !item.children ? "active" : ""} ${active && item.children ? "within" : ""}`}
                aria-current={active && !item.children ? "page" : undefined}
                onClick={() => {
                  if (item.children) setOpen((current) => ({ ...current, [item.view]: !expanded }));
                  else navigate(item.view);
                }}
              >
                <item.icon size={18} strokeWidth={1.6} />
                <span>{item.label}</span>
                {item.children && (expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />)}
              </button>
              {expanded &&
                item.children?.map((child) => (
                  <button
                    type="button"
                    key={child.view}
                    className={`md-menu-child ${view === child.view ? "active" : ""}`}
                    aria-current={view === child.view ? "page" : undefined}
                    onClick={() => navigate(child.view)}
                  >
                    {child.label}
                  </button>
                ))}
            </div>
          );
        })}
        <div className="md-menu-foot" title={store?.detail || health.error}>
          <i className={store?.ok ? "online" : ""} />
          {store
            ? store.backend === "postgres"
              ? `PostgreSQL · ${store.schema}`
              : store.fallback
                ? "SQLite（PostgreSQL 不可达）"
                : "SQLite 本地文件"
            : health.error
              ? "元数据存储不可用"
              : "正在检测存储…"}
        </div>
      </aside>
      <section className="md-content">{content}</section>
    </div>
  );
}
